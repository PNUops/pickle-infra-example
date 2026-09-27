#!/usr/bin/env python3
"""Independently check daily encrypted core CT archives visible on pve-node-3."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
from email.message import EmailMessage
import fcntl
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

CTIDS = (1200, 1201, 1202, 1204)
FINGERPRINT = 'aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa:aa'
STATE_DIR = Path('/var/lib/pickle-core-pbs-monitor')
MAIL_CONFIG = Path('/etc/pickle-example/mail.json')
KST = ZoneInfo('Asia/Seoul')
DEADLINE = time(4, 30)
FIRST_DUE_DATE = date(2025, 1, 2)
VOLID = re.compile(r'^pbs-example-core-read:backup/ct/(1200|1201|1202|1204)/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$')
MANUAL = {
    1200: 'pbs-example-core-read:backup/ct/1200/2025-01-01T12:43:02Z',
    1201: 'pbs-example-core-read:backup/ct/1201/2025-01-01T11:55:44Z',
    1202: 'pbs-example-core-read:backup/ct/1202/2025-01-01T12:46:30Z',
    1204: 'pbs-example-core-read:backup/ct/1204/2025-01-01T12:50:42Z',
}


class MonitorError(RuntimeError):
    pass


class MailUncertain(MonitorError):
    pass


class MailNotSent(MonitorError):
    """SMTP definitely did not accept a message."""


def private_bytes(path: Path) -> bytes:
    info = path.stat()
    if not path.is_file() or info.st_uid != 0 or info.st_mode & 0o077:
        raise MonitorError('Private mail configuration permissions are invalid')
    return path.read_bytes()


def atomic_json(path: Path, value: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix='.monitor-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fetch_content() -> list:
    command = ['pvesh', 'get', '/nodes/pve-node-3/storage/pbs-example-core-read/content',
               '--content', 'backup', '--output-format', 'json']
    try:
        result = subprocess.run(command, check=True, capture_output=True, timeout=90)
        content = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise MonitorError('PBS content query failed') from error
    if not isinstance(content, list):
        raise MonitorError('PBS content response is not a list')
    return content


def inspect(content: list, now: datetime, *, deadline: time = DEADLINE,
            first_due_date: date = FIRST_DUE_DATE) -> dict:
    now = now.astimezone(KST)
    cutoff = datetime.combine(now.date(), deadline, KST)
    window = datetime.combine(now.date(), time(3, 17), KST).timestamp()
    failures = []
    baseline_failures = []
    latest = {}
    def valid(row: dict, ctid: int) -> bool:
        volid = row.get('volid')
        match = VOLID.fullmatch(volid) if isinstance(volid, str) else None
        if not match or int(match.group(1)) != ctid:
            return False
        try:
            snapshot_epoch = int(datetime.strptime(match.group(2), '%Y-%m-%dT%H:%M:%SZ')
                                 .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            return False
        return (snapshot_epoch == row.get('ctime')
                and row.get('format') == 'pbs-ct' and row.get('subtype') == 'lxc'
                and row.get('content') == 'backup' and row.get('encrypted') == FINGERPRINT
                and type(row.get('size')) is int and row['size'] > 0
                and type(row.get('ctime')) is int and row['ctime'] <= now.timestamp())
    for ctid in CTIDS:
        rows = [row for row in content if isinstance(row, dict) and row.get('vmid') == ctid]
        manual = [row for row in rows if row.get('volid') == MANUAL[ctid]
                  and row.get('protected') == 1 and valid(row, ctid)]
        if not manual:
            baseline_failures.append(f'CT{ctid}:PROTECTED_BASELINE_MISSING')
        if now.date() < first_due_date:
            continue
        rows = [row for row in rows if row.get('volid') != MANUAL[ctid]
                and type(row.get('ctime')) is int]
        if not rows:
            failures.append(f'CT{ctid}:MISSING')
            continue
        row = max(rows, key=lambda item: item['ctime'])
        if not valid(row, ctid):
            failures.append(f'CT{ctid}:INVALID_METADATA')
            continue
        if row['ctime'] < window:
            failures.append(f'CT{ctid}:STALE')
            continue
        latest[str(ctid)] = row['ctime']
    status = ('FAILED' if baseline_failures or failures and now >= cutoff else
              'PENDING' if failures else 'HEALTHY')
    reasons = baseline_failures + failures
    return {'status': status, 'date': now.date().isoformat(),
            'reason': ','.join(reasons) if reasons else 'ALL_CORE_ARCHIVES_PRESENT',
            'latest': latest}


def host_clock_ready() -> bool:
    try:
        result = subprocess.run(['timedatectl', 'show', '-p', 'Timezone', '-p', 'NTPSynchronized'],
                                check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return 'Timezone=Asia/Seoul' in result.stdout.splitlines() and 'NTPSynchronized=yes' in result.stdout.splitlines()


def require_root() -> None:
    if os.geteuid() != 0:
        raise MonitorError('Run as root on pve-node-3')


def settings() -> dict:
    try:
        value = json.loads(private_bytes(MAIL_CONFIG))
    except (OSError, ValueError) as error:
        raise MonitorError('Mail configuration unavailable') from error
    if not isinstance(value, dict):
        raise MonitorError('Mail configuration must be an object')
    if value.get('enabled') is not True or value.get('tls_mode') not in ('starttls', 'tls'):
        raise MonitorError('Mail transport is not enabled with TLS')
    for key in ('host', 'username', 'sender', 'recipient', 'password_file'):
        if not isinstance(value.get(key), str) or not value[key] or '\r' in value[key] or '\n' in value[key]:
            raise MonitorError('Mail configuration field is invalid')
    for key in ('sender', 'recipient'):
        if not re.fullmatch(r'[^\s<>,;@]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value[key]):
            raise MonitorError('Mail address is invalid')
    if type(value.get('port')) is not int or not 1 <= value['port'] <= 65535:
        raise MonitorError('Mail port is invalid')
    password_path = Path(value['password_file'])
    if not password_path.is_absolute() or '..' in password_path.parts:
        raise MonitorError('Mail password path is invalid')
    value['password'] = private_bytes(password_path).decode().rstrip('\n')
    return value


def send_mail(report: dict, event: str) -> None:
    try:
        config = settings()
    except (MonitorError, OSError, ValueError, UnicodeError) as error:
        raise MailNotSent('Mail settings are unavailable before submission') from error
    message = EmailMessage()
    message['From'] = config['sender']
    message['To'] = config['recipient']
    message['Subject'] = '[Pickle 코어 PBS 백업] ' + ('장애' if event == 'FAILED' else '복구')
    message.set_content('pve-node-3 코어 LXC 백업 감시 상태가 변경됐습니다.\n'
                        f"상태: {event}\n기준일: {report['date']} KST\n판정: {report['reason']}\n"
                        f"CT별 최근 아카이브 시각(epoch): {json.dumps(report['latest'], sort_keys=True)}\n")
    smtp = None
    try:
        context = ssl.create_default_context()
        smtp = (smtplib.SMTP_SSL(config['host'], config['port'], timeout=20, context=context)
                if config['tls_mode'] == 'tls' else smtplib.SMTP(config['host'], config['port'], timeout=20))
        if config['tls_mode'] == 'starttls':
            smtp.starttls(context=context)
        smtp.login(config['username'], config['password'])
    except (OSError, smtplib.SMTPException) as error:
        if smtp is not None:
            try:
                smtp.close()
            except (OSError, smtplib.SMTPException):
                pass
        raise MailNotSent('SMTP connection or authentication failed before submission') from error
    try:
        smtp.send_message(message)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
        raise MailNotSent('SMTP explicitly rejected message submission') from error
    except (OSError, smtplib.SMTPException) as error:
        raise MailUncertain('SMTP acceptance is unknown; inspect receipt before retry') from error
    finally:
        if smtp is not None:
            try:
                smtp.close()
            except (OSError, smtplib.SMTPException):
                pass


def transition(state: dict, report: dict) -> tuple[dict, str]:
    previous = state.get('status')
    current = report['status']
    if previous == current:
        return state, 'UNCHANGED'
    sequence = state.get('sequence', 0) + 1
    event = ('BASELINE' if previous is None and current == 'HEALTHY' else
             'FAILED' if current == 'FAILED' else 'RECOVERED')
    updated = {'status': current, 'sequence': sequence, 'event': event,
               'delivery': 'BASELINE' if event == 'BASELINE' else 'ATTEMPTED',
               'attempts': 0, 'report': report}
    if event == 'BASELINE':
        return updated, event
    # The caller persists ATTEMPTED before invoking the SMTP sender.
    return updated, event


def run(now: datetime | None = None, *, fetcher=fetch_content, sender=send_mail,
        state_dir: Path = STATE_DIR, deadline: time = DEADLINE,
        first_due_date: date = FIRST_DUE_DATE) -> tuple[dict, str]:
    require_root()
    if not state_dir.exists():
        state_dir.mkdir(mode=0o700)
    info = state_dir.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise MonitorError('Monitor state directory permissions are invalid')
    if now is None and not host_clock_ready():
        raise MonitorError('pve-node-3 clock timezone or NTP synchronization is invalid')
    now = now or datetime.now(timezone.utc)
    with (state_dir / 'monitor.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = state_dir / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        if now.timestamp() + 1 < state.get('last_check_epoch', 0):
            raise MonitorError('System clock moved backwards')
        try:
            report = inspect(fetcher(), now, deadline=deadline, first_due_date=first_due_date)
        except MonitorError:
            report = {'status': 'FAILED', 'date': now.astimezone(KST).date().isoformat(),
                      'reason': 'PBS_QUERY_UNAVAILABLE', 'latest': {}}
        if report['status'] == 'PENDING':
            state['last_check_epoch'] = now.timestamp()
            atomic_json(path, state)
            return report, 'PENDING'
        updated, event = transition(state, report)
        if event == 'UNCHANGED':
            state['last_check_epoch'] = now.timestamp()
            if (state.get('delivery') == 'NOT_SENT'
                    and type(state.get('attempts')) is int and state['attempts'] < 3
                    and now.timestamp() >= state.get('retry_after', 0)):
                updated = state
                updated['report'] = report
                event = state['event']
            else:
                atomic_json(path, state)
                return report, 'RETRY_PENDING' if state.get('delivery') == 'NOT_SENT' else event
        updated['last_check_epoch'] = now.timestamp()
        if event in ('FAILED', 'RECOVERED'):
            updated['attempts'] = updated.get('attempts', 0) + 1
            updated['delivery'] = 'ATTEMPTED'
            updated.pop('retry_after', None)
            atomic_json(path, updated)
            try:
                sender(report, event)
            except MailNotSent:
                updated['delivery'] = 'RETRIES_EXHAUSTED' if updated['attempts'] >= 3 else 'NOT_SENT'
                if updated['delivery'] == 'NOT_SENT':
                    updated['retry_after'] = now.timestamp() + (60 if updated['attempts'] == 1 else 300)
                atomic_json(path, updated)
                raise
            except (MailUncertain, OSError, smtplib.SMTPException):
                updated['delivery'] = 'UNCERTAIN'
                atomic_json(path, updated)
                raise MailUncertain('SMTP acceptance is unknown; inspect receipt before retry') from None
            updated['delivery'] = 'SENT'
            atomic_json(path, updated)
        else:
            atomic_json(path, updated)
        return report, event


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    parser.add_argument('--deadline', default='04:30', help='KST daily alert deadline, HH:MM')
    parser.add_argument('--first-due-date', default=FIRST_DUE_DATE.isoformat(),
                        help='First KST date that requires the scheduled daily set, YYYY-MM-DD')
    args = parser.parse_args()
    try:
        deadline = time.fromisoformat(args.deadline)
        if deadline.second or deadline.microsecond:
            raise MonitorError('Deadline must be HH:MM')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.first_due_date):
            raise MonitorError('First due date must be YYYY-MM-DD')
        first_due_date = date.fromisoformat(args.first_due_date)
        report, event = run(state_dir=args.state_dir, deadline=deadline,
                            first_due_date=first_due_date)
    except (MonitorError, OSError, ValueError) as error:
        print(f'core PBS monitor error: {error}', file=sys.stderr)
        return 2
    print(json.dumps({'status': report['status'], 'reason': report['reason'], 'event': event}))
    return 1 if report['status'] == 'FAILED' else 0


if __name__ == '__main__':
    sys.exit(main())
