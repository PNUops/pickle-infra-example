#!/usr/bin/env python3
"""Offline tests for the mailbox reader behind smoke-signup.sh.

The two fixtures under fixtures/ are not hand-written. They were rendered by
compiling the api's own VerificationMailComposer, MailHtmlLayout and
AuthProperties and passing the composer's output through Spring's
MimeMessageHelper in MULTIPART_MODE_MIXED_RELATED, which is exactly what
SmtpMailSender.send does: a base64 text part, a quoted-printable HTML part and
an RFC 2047 subject split over two encoded words. If the api changes its mail,
render them again the same way rather than editing them.

No network is used. The IMAP server is a fake that records every command, so
the tests can also prove the reader never sends a command that changes the
mailbox.
"""
from pathlib import Path
import imaplib
import os
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'lib'))
import signup_mailbox as sm  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
ADDRESS = 'signup-1759200000-12345@example.com'
BASE = 'https://pickle.pusan.ac.kr/verify-email'
TOKEN = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcde'
VERIFY = (FIXTURES / 'signup-verification.eml').read_bytes()
NOTICE = (FIXTURES / 'signup-already-registered.eml').read_bytes()

# Commands that would alter the mailbox. None may ever be sent.
MUTATING = {'STORE', 'COPY', 'MOVE', 'EXPUNGE', 'DELETE', 'APPEND', 'RENAME', 'CREATE', 'UID STORE',
            'UID COPY', 'UID MOVE', 'UID EXPUNGE'}


def readdress(raw: bytes, new: str) -> bytes:
    return raw.replace(ADDRESS.encode(), new.encode())


def strip_message_id(raw: bytes) -> bytes:
    head, sep, body = raw.partition(b'\r\n\r\n')
    lines = [line for line in head.split(b'\r\n') if not line.lower().startswith(b'message-id:')]
    stripped = b'\r\n'.join(lines) + sep + body
    assert b'Message-ID' not in stripped.partition(b'\r\n\r\n')[0]
    return stripped


def internaldate(epoch: float) -> bytes:
    return imaplib.Time2Internaldate(epoch).encode()


class FakeImap:
    """Just enough of imaplib.IMAP4 for the reader, with a command log.

    Folders map to lists of (uid, arrival epoch, raw message). SEARCH mimics a
    server TO match as a substring of the To header, which is looser than the
    reader's own check on purpose.
    """

    def __init__(self, folders, gmail=True):
        self.folders = folders
        self.gmail = gmail
        self.log = []
        self.readonly = []
        self.selected = None
        self.logged_out = False

    def list(self):
        self.log.append('LIST')
        if not self.gmail:
            return 'OK', [b'(\\HasNoChildren) "/" INBOX']
        return 'OK', [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
            b'(\\All \\HasNoChildren) "/" "[Gmail]/&yATMtLz0rQDVaA-"',
            b'(\\HasNoChildren \\Junk) "/" "[Gmail]/&wqTTONVo-"',
            b'(\\HasNoChildren \\Trash) "/" "[Gmail]/&1zTJwNG1-"',
        ]

    def select(self, mailbox, readonly=False):
        self.log.append('EXAMINE' if readonly else 'SELECT')
        self.readonly.append(readonly)
        self.selected = mailbox
        return ('OK', [b'1']) if mailbox in self.folders else ('NO', [b'no such folder'])

    def uid(self, command, *args):
        self.log.append('UID ' + command.upper())
        msgs = self.folders.get(self.selected, [])
        if command.upper() == 'SEARCH':
            want = args[args.index('TO') + 1].strip('"').encode()
            hits = [str(u).encode() for u, _, raw in msgs if want in raw.split(b'\r\n\r\n', 1)[0]]
            return 'OK', [b' '.join(hits)]
        if command.upper() == 'FETCH':
            self.fetch_items = args[1]
            for u, when, raw in msgs:
                if str(u).encode() == args[0]:
                    head = b'%d (UID %d INTERNALDATE %s BODY[] {%d}' % (u, u, internaldate(when), len(raw))
                    return 'OK', [(head, raw), b')']
            return 'OK', [None]
        raise AssertionError(f'unexpected UID command {command}')

    def logout(self):
        self.logged_out = True
        self.log.append('LOGOUT')


class ExtractTokenTest(unittest.TestCase):
    def test_real_verification_mail_yields_its_token(self):
        self.assertEqual(sm.extract_token(VERIFY, ADDRESS, BASE), TOKEN)

    def test_recipient_match_ignores_case(self):
        self.assertEqual(sm.extract_token(VERIFY, ADDRESS.upper(), BASE), TOKEN)

    def test_mail_for_another_address_is_not_ours(self):
        # A longer address containing ours as a substring is what a server TO
        # search would also return.
        other = readdress(VERIFY, 'x' + ADDRESS)
        self.assertIsNone(sm.extract_token(other, ADDRESS, BASE))
        self.assertIsNone(sm.extract_token(readdress(VERIFY, 'admin@example.com'), ADDRESS, BASE))

    def test_mail_with_a_second_recipient_is_not_ours(self):
        both = VERIFY.replace(b'To: ' + ADDRESS.encode(), b'To: ' + ADDRESS.encode() + b', admin@example.com')
        self.assertIsNone(sm.extract_token(both, ADDRESS, BASE))

    def test_already_registered_notice_is_refused_by_name(self):
        with self.assertRaisesRegex(sm.MailError, 'already-registered'):
            sm.extract_token(NOTICE, ADDRESS, BASE)

    def test_link_with_another_base_is_refused_and_named(self):
        with self.assertRaisesRegex(sm.MailError, 'another base'):
            sm.extract_token(VERIFY, ADDRESS, 'https://pickle.example.com/verify-email')

    def test_html_part_disagreeing_with_text_part_is_refused(self):
        # Rewrite only the decoded HTML part: the raw bytes cannot be edited in
        # place because quoted-printable soft breaks split the token.
        import email
        from email import policy
        msg = email.message_from_bytes(VERIFY, policy=policy.default)
        html_part = next(p for p in msg.walk() if p.get_content_type() == 'text/html')
        body = html_part.get_content()
        self.assertEqual(body.count(TOKEN), 2)  # the button and the copy-paste fallback
        html_part.set_content(body.replace(TOKEN, 'Z' * 43), subtype='html')
        forged = msg.as_bytes()
        self.assertEqual(sm.extract_token(VERIFY, ADDRESS, BASE), TOKEN)
        with self.assertRaisesRegex(sm.MailError, 'different token'):
            sm.extract_token(forged, ADDRESS, BASE)

    def test_unexpected_subject_is_refused(self):
        msg = (b'To: ' + ADDRESS.encode() + b'\r\nSubject: hello\r\n'
               b'Content-Type: text/plain; charset=UTF-8\r\n\r\n' + BASE.encode() + b'?token=' + TOKEN.encode() + b'\r\n')
        with self.assertRaisesRegex(sm.MailError, 'unexpected subject'):
            sm.extract_token(msg, ADDRESS, BASE)


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.start = time.time()

    def fake(self, all_mail=(), spam=(), gmail=True):
        if not gmail:
            return FakeImap({'INBOX': list(all_mail)}, gmail=False)
        return FakeImap({'"[Gmail]/&yATMtLz0rQDVaA-"': list(all_mail), '"[Gmail]/&wqTTONVo-"': list(spam)})

    def assert_read_only(self, imap):
        self.assertTrue(imap.readonly and all(imap.readonly), 'every folder must be opened read-only')
        self.assertFalse(set(imap.log) & MUTATING, imap.log)
        self.assertIn('BODY.PEEK[]', getattr(imap, 'fetch_items', 'BODY.PEEK[]'))

    def test_finds_the_token_in_all_mail_read_only(self):
        imap = self.fake(all_mail=[(7, self.start + 3, VERIFY)])
        token, folder, junk = sm.wait_for_token(imap, ADDRESS, BASE, self.start, 1, sleep=lambda _: None)
        self.assertEqual(token, TOKEN)
        self.assertEqual((folder, junk), ('"[Gmail]/&yATMtLz0rQDVaA-"', False))
        self.assert_read_only(imap)

    def test_finds_the_token_in_spam(self):
        imap = self.fake(spam=[(3, self.start + 3, VERIFY)])
        token, folder, junk = sm.wait_for_token(imap, ADDRESS, BASE, self.start, 1, sleep=lambda _: None)
        self.assertEqual((token, folder, junk), (TOKEN, '"[Gmail]/&wqTTONVo-"', True))
        self.assert_read_only(imap)

    def test_plain_server_falls_back_to_inbox(self):
        imap = self.fake(all_mail=[(1, self.start + 3, VERIFY)], gmail=False)
        self.assertEqual(sm.wait_for_token(imap, ADDRESS, BASE, self.start, 1, sleep=lambda _: None)[1], 'INBOX')

    def test_mail_older_than_the_signup_is_ignored(self):
        imap = self.fake(all_mail=[(7, self.start - sm.CLOCK_ALLOWANCE_S - 5, VERIFY)])
        with self.assertRaises(TimeoutError):
            sm.wait_for_token(imap, ADDRESS, BASE, self.start, 0, sleep=lambda _: None)
        self.assert_read_only(imap)

    def test_other_addresses_are_never_taken(self):
        mails = [(1, self.start + 1, readdress(VERIFY, 'x' + ADDRESS)),
                 (2, self.start + 1, readdress(VERIFY, 'admin@example.com'))]
        imap = self.fake(all_mail=mails)
        with self.assertRaises(TimeoutError):
            sm.wait_for_token(imap, ADDRESS, BASE, self.start, 0, sleep=lambda _: None)

    def test_two_verification_mails_are_ambiguous(self):
        second = VERIFY.replace(b'Message-ID: <', b'Message-ID: <second.')
        imap = self.fake(all_mail=[(1, self.start + 1, VERIFY), (2, self.start + 2, second)])
        with self.assertRaisesRegex(sm.MailError, 'expected exactly one'):
            sm.wait_for_token(imap, ADDRESS, BASE, self.start, 1, sleep=lambda _: None)

    def test_same_message_id_in_two_folders_counts_once(self):
        imap = self.fake(all_mail=[(1, self.start + 1, VERIFY)], spam=[(5, self.start + 1, VERIFY)])
        token, folder, junk = sm.wait_for_token(imap, ADDRESS, BASE, self.start, 1, sleep=lambda _: None)
        self.assertEqual((token, folder, junk), (TOKEN, '"[Gmail]/&yATMtLz0rQDVaA-"', False))

    def test_mail_without_message_id_is_found(self):
        bare = strip_message_id(VERIFY)
        imap = self.fake(all_mail=[(1, self.start + 1, bare)])
        self.assertEqual(sm.wait_for_token(imap, ADDRESS, BASE, self.start, 1, sleep=lambda _: None)[0], TOKEN)

    def test_mail_without_message_id_in_two_folders_is_ambiguous(self):
        # Documented in scan_once: without a Message-ID a second sighting cannot
        # be recognised, so the run fails rather than guessing.
        bare = strip_message_id(VERIFY)
        imap = self.fake(all_mail=[(1, self.start + 1, bare)], spam=[(5, self.start + 1, bare)])
        with self.assertRaisesRegex(sm.MailError, 'expected exactly one'):
            sm.wait_for_token(imap, ADDRESS, BASE, self.start, 1, sleep=lambda _: None)

    def test_polls_until_the_mail_arrives(self):
        imap = self.fake()
        calls = []

        def arrive(_):
            calls.append(1)
            if len(calls) == 2:
                imap.folders['"[Gmail]/&yATMtLz0rQDVaA-"'].append((9, self.start + 10, VERIFY))

        token, _, _ = sm.wait_for_token(imap, ADDRESS, BASE, self.start, 60, sleep=arrive)
        self.assertEqual(token, TOKEN)
        self.assertEqual(len(calls), 2)
        self.assert_read_only(imap)

    def test_times_out_when_nothing_arrives(self):
        imap = self.fake()
        ticks = iter(range(0, 1000, 10))
        with self.assertRaisesRegex(TimeoutError, 'within 30s'):
            sm.wait_for_token(imap, ADDRESS, BASE, self.start, 30, sleep=lambda _: None, clock=lambda: next(ticks))


class MainTest(unittest.TestCase):
    def run_main(self, argv, connector):
        from io import StringIO
        out, err = StringIO(), StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = sm.main(argv, connector=connector)
        finally:
            sys.stdout, sys.stderr = old
        return rc, out.getvalue(), err.getvalue()

    def test_missing_credential_fails_before_connecting(self):
        def never(*_):
            raise AssertionError('must not connect without a credential')
        rc, out, err = self.run_main(['--user', 'u', '--password-file', '/nonexistent/x', '--check-login'], never)
        self.assertEqual(rc, 2)
        self.assertEqual(out, '')
        self.assertIn('cannot read', err)

    def test_empty_credential_fails(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as fh:
            fh.write('  \n')
        try:
            rc, _, err = self.run_main(['--user', 'u', '--password-file', fh.name, '--check-login'],
                                       lambda *_: self.fail('connected'))
        finally:
            os.unlink(fh.name)
        self.assertEqual(rc, 2)
        self.assertIn('empty', err)

    def test_password_is_spaced_groups_joined_and_never_printed(self):
        secret = 'qqqq wwww zzzz yyyy'
        seen = {}
        imap = FakeImap({'"[Gmail]/&yATMtLz0rQDVaA-"': [(1, time.time(), VERIFY)], '"[Gmail]/&wqTTONVo-"': []})

        def connector(host, port, user, password):
            seen['password'] = password
            return imap

        with tempfile.NamedTemporaryFile('w', delete=False) as fh:
            fh.write(secret + '\n')
        try:
            rc, out, err = self.run_main(['--user', 'u', '--password-file', fh.name, '--address', ADDRESS,
                                          '--verify-base', BASE, '--not-before', str(time.time() - 5),
                                          '--timeout', '1'], connector)
        finally:
            os.unlink(fh.name)
        self.assertEqual(rc, 0)
        self.assertEqual(seen['password'], 'qqqqwwwwzzzzyyyy')
        self.assertEqual(out.splitlines(), [TOKEN, 'normal', '"[Gmail]/&yATMtLz0rQDVaA-"'])
        for fragment in ('qqqq', 'yyyy', 'qqqqwwwwzzzzyyyy'):
            self.assertNotIn(fragment, out + err)
        self.assertNotIn(TOKEN, err)
        self.assertTrue(imap.logged_out)

    def test_login_refusal_is_exit_5(self):
        def refuse(*_):
            raise imaplib.IMAP4.error('[AUTHENTICATIONFAILED] Invalid credentials')
        with tempfile.NamedTemporaryFile('w', delete=False) as fh:
            fh.write('wrong\n')
        try:
            rc, out, err = self.run_main(['--user', 'u', '--password-file', fh.name, '--check-login'], refuse)
        finally:
            os.unlink(fh.name)
        self.assertEqual((rc, out), (5, ''))
        self.assertNotIn('wrong', err)

    def test_notice_mail_is_exit_4(self):
        imap = FakeImap({'"[Gmail]/&yATMtLz0rQDVaA-"': [(1, time.time(), NOTICE)], '"[Gmail]/&wqTTONVo-"': []})
        with tempfile.NamedTemporaryFile('w', delete=False) as fh:
            fh.write('pw\n')
        try:
            rc, out, err = self.run_main(['--user', 'u', '--password-file', fh.name, '--address', ADDRESS,
                                          '--verify-base', BASE, '--not-before', str(time.time() - 5),
                                          '--timeout', '1'], lambda *_: imap)
        finally:
            os.unlink(fh.name)
        self.assertEqual((rc, out), (4, ''))
        self.assertIn('already-registered', err)


if __name__ == '__main__':
    unittest.main(verbosity=1)
