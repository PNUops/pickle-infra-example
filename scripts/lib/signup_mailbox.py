#!/usr/bin/env python3
"""Read one signup verification mail out of the smoke mailbox, read-only.

smoke-signup.sh signs up a fresh address on the domain whose mail is routed to
the operator mailbox, and the api stores only the hash of the verification
token, so the token has to come from the mail itself. This module finds that
one mail and prints the token on stdout; everything else goes to stderr.

What it will not do, because the mailbox also holds mail for every other
account on that domain (the seeded administrators' password-reset links
among them):

- change anything. Folders are opened with EXAMINE (read-only) and bodies are
  fetched with BODY.PEEK, so not even the \\Seen flag moves. There is no STORE,
  COPY, MOVE or EXPUNGE anywhere in this file, and the tests fail if one is
  sent.
- look at mail for any other address. The server search narrows by recipient,
  and every candidate is then checked here against the exact To address,
  because server-side TO search is a substring or token match.
- accept an old mail. A candidate must have arrived at or after the moment the
  signup was sent (less a small clock allowance), so a mail from an earlier run
  can never supply the token.
- print the password or the token anywhere but the token on stdout.

Exit status: 0 token printed; 2 credential missing or unreadable; 3 no
matching mail before the deadline; 4 a matching mail was wrong (ambiguous,
malformed, or the already-registered notice); 5 IMAP failure.
"""
from __future__ import annotations

import argparse
import email
from email import policy
from email.utils import getaddresses
import html
import imaplib
import re
import sys
import time

# Subjects the api sends to a signup address (VerificationMailComposer). The
# notice goes out instead of a verification mail when the address already has
# an account, which for a run-unique address means something is wrong.
VERIFY_SUBJECT = '[Pickle] 부산대학교 클라우드 플랫폼 이메일 인증'
NOTICE_SUBJECT = '[Pickle] 부산대학교 클라우드 플랫폼 가입 안내'

# TokenHasher.newToken: 32 random bytes, base64url without padding.
TOKEN_RE = r'[A-Za-z0-9_-]{43}'

# How far before the signup a mail's arrival time may read and still count.
# The address is unique to the run, so this bounds clock disagreement between
# the host and the mail server, not the search.
CLOCK_ALLOWANCE_S = 60


class MailError(Exception):
    """A mail addressed to this run exists but cannot supply a token."""


class CredentialError(Exception):
    pass


def read_password(path: str) -> str:
    try:
        with open(path, encoding='utf-8') as fh:
            value = fh.read()
    except OSError as exc:
        raise CredentialError(f'cannot read the mailbox app password at {path} ({exc.strerror})') from None
    # App passwords are shown in groups of four; the spaces are display only.
    value = ''.join(value.split())
    if not value:
        raise CredentialError(f'the mailbox app password file {path} is empty')
    return value


def recipients(msg) -> list[str]:
    return [addr.lower() for _, addr in getaddresses(msg.get_all('To', [])) if addr]


def extract_token(raw: bytes, address: str, verify_base: str) -> str | None:
    """Token from one raw message, None if the mail is not for this run.

    Raises MailError when the mail is addressed to this run but is not a
    usable verification mail.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)
    if recipients(msg) != [address.lower()]:
        return None
    subject = str(msg.get('Subject', '')).strip()
    if subject == NOTICE_SUBJECT:
        raise MailError('the address received the already-registered notice, not a verification mail '
                        '(signup found an existing account for it)')
    if subject != VERIFY_SUBJECT:
        raise MailError(f'a mail to this address has an unexpected subject: {subject!r}')

    link_re = re.compile('^' + re.escape(verify_base) + r'\?token=(' + TOKEN_RE + ')$')
    text_tokens: set[str] = set()
    html_tokens: set[str] = set()
    foreign_bases: set[str] = set()
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart() or ctype not in ('text/plain', 'text/html'):
            continue
        body = part.get_content()
        if ctype == 'text/plain':
            for line in body.splitlines():
                line = line.strip()
                m = link_re.match(line)
                if m:
                    text_tokens.add(m.group(1))
                elif '?token=' in line:
                    foreign_bases.add(line.split('?token=', 1)[0])
        else:
            for href in re.findall(r'href="([^"]*)"', body):
                m = link_re.match(html.unescape(href))
                if m:
                    html_tokens.add(m.group(1))
                elif '?token=' in href:
                    foreign_bases.add(html.unescape(href).split('?token=', 1)[0])

    if not text_tokens:
        hint = f' (links with another base were present: {sorted(foreign_bases)})' if foreign_bases else ''
        raise MailError(f'the verification mail has no {verify_base}?token=… line in its text part{hint}')
    if len(text_tokens) != 1:
        raise MailError('the verification mail carries more than one token in its text part')
    if html_tokens and html_tokens != text_tokens:
        raise MailError('the HTML part links a different token than the text part')
    return next(iter(text_tokens))


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')


def folders(conn) -> list[str]:
    """Folders to search: Gmail's All Mail and Spam when advertised, else INBOX.

    All Mail holds every message outside Spam and Trash, so a mail archived or
    labelled away from the inbox is still found; Spam is searched because a
    forwarded mail may be filed there. Names are passed back exactly as the
    server listed them, localised and encoded names included.
    """
    typ, lines = conn.list()
    if typ != 'OK':
        raise imaplib.IMAP4.error('LIST failed')
    found: dict[str, str] = {}
    for line in lines or []:
        if not isinstance(line, bytes):
            continue
        m = _LIST_RE.match(line)
        if not m:
            continue
        flags = m.group('flags').decode('ascii', 'replace').split()
        for flag in ('\\All', '\\Junk'):
            if flag in flags and flag not in found:
                found[flag] = m.group('name').decode('ascii', 'replace')
    if '\\All' not in found:
        return ['INBOX'] + ([found['\\Junk']] if '\\Junk' in found else [])
    return [found['\\All']] + ([found['\\Junk']] if '\\Junk' in found else [])


def _search_date(epoch: float) -> str:
    # SINCE is a date in the server's zone, so go back a day to be safe; the
    # exact arrival-time check happens per message.
    return time.strftime('%d-%b-%Y', time.gmtime(epoch - 86400))


def scan_once(conn, address: str, verify_base: str, not_before: float) -> tuple[list[str], list[str]]:
    """One pass over the folders. Returns (tokens, folder names they came from)."""
    tokens: list[str] = []
    where: list[str] = []
    seen_ids: set[str] = set()
    for folder in folders(conn):
        typ, _ = conn.select(folder, readonly=True)
        if typ != 'OK':
            continue
        typ, data = conn.uid('SEARCH', 'SINCE', _search_date(not_before), 'TO', f'"{address}"')
        if typ != 'OK':
            raise imaplib.IMAP4.error(f'SEARCH failed in {folder}')
        uids = (data[0] or b'').split() if data else []
        for uid in uids:
            typ, parts = conn.uid('FETCH', uid, '(INTERNALDATE BODY.PEEK[])')
            if typ != 'OK':
                raise imaplib.IMAP4.error(f'FETCH failed in {folder}')
            raw = None
            arrived = None
            for item in parts or []:
                if isinstance(item, tuple) and len(item) == 2:
                    stamp = imaplib.Internaldate2tuple(item[0])
                    arrived = time.mktime(stamp) if stamp else None
                    raw = item[1]
            if raw is None or arrived is None:
                raise MailError(f'could not read message {uid.decode()} in {folder}')
            if arrived < not_before - CLOCK_ALLOWANCE_S:
                continue
            msg_id = str(email.message_from_bytes(raw, policy=policy.default).get('Message-ID', '')).strip()
            if msg_id and msg_id in seen_ids:
                continue  # the same mail seen through a second folder
            token = extract_token(raw, address, verify_base)
            if token is None:
                continue
            if msg_id:
                seen_ids.add(msg_id)
            tokens.append(token)
            where.append(folder)
    return tokens, where


def wait_for_token(conn, address: str, verify_base: str, not_before: float,
                   timeout: float, interval: float = 5.0, sleep=time.sleep, clock=time.monotonic) -> tuple[str, str]:
    deadline = clock() + timeout
    while True:
        tokens, where = scan_once(conn, address, verify_base, not_before)
        if len(tokens) > 1:
            raise MailError(f'{len(tokens)} verification mails reached {address}; expected exactly one')
        if tokens:
            return tokens[0], where[0]
        if clock() >= deadline:
            raise TimeoutError(f'no verification mail to {address} within {int(timeout)}s')
        sleep(interval)


def connect(host: str, port: int, user: str, password: str):
    conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    conn.login(user, password)
    return conn


def main(argv: list[str] | None = None, connector=connect) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--host', default='imap.example.com')
    ap.add_argument('--port', type=int, default=993)
    ap.add_argument('--user', required=True)
    ap.add_argument('--password-file', required=True)
    ap.add_argument('--address')
    ap.add_argument('--verify-base')
    ap.add_argument('--not-before', type=float)
    ap.add_argument('--timeout', type=float, default=120)
    ap.add_argument('--check-login', action='store_true',
                    help='only prove the credential signs in, then log out')
    args = ap.parse_args(argv)

    try:
        password = read_password(args.password_file)
    except CredentialError as exc:
        print(f'mailbox: {exc}', file=sys.stderr)
        return 2
    if not args.check_login and not (args.address and args.verify_base and args.not_before is not None):
        print('mailbox: --address, --verify-base and --not-before are required', file=sys.stderr)
        return 4

    try:
        conn = connector(args.host, args.port, args.user, password)
    except (imaplib.IMAP4.error, OSError) as exc:
        # The exception text is the server's reply, which never echoes the password.
        print(f'mailbox: could not sign in to {args.host} as {args.user}: {exc}', file=sys.stderr)
        return 5
    finally:
        password = ''
    try:
        if args.check_login:
            return 0
        token, folder = wait_for_token(conn, args.address.lower(), args.verify_base,
                                       args.not_before, args.timeout)
        print(f'mailbox: verification mail found in {folder}', file=sys.stderr)
        print(token)
        return 0
    except TimeoutError as exc:
        print(f'mailbox: {exc}', file=sys.stderr)
        return 3
    except MailError as exc:
        print(f'mailbox: {exc}', file=sys.stderr)
        return 4
    except (imaplib.IMAP4.error, OSError) as exc:
        print(f'mailbox: IMAP failure: {exc}', file=sys.stderr)
        return 5
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - logout failure changes nothing here
            pass


if __name__ == '__main__':
    sys.exit(main())
