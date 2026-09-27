#!/usr/bin/env python3
"""Record datastore status transitions and send bounded operator alerts."""
from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
import fcntl
import json
import math
import os
import re
from pathlib import Path
import smtplib
import socket
import ssl
import stat
import subprocess
import tempfile

STATE = Path('/var/lib/pickle-example-capacity')
CONFIG = Path('/etc/pickle-example-capacity/config.json')
MAIL = Path('/etc/pickle-example/mail.json')
PROBE = Path('/usr/local/libexec/pickle-example/pbs-capacity-probe.py')
RETRY_SECONDS = (60, 300)
MAX_ATTEMPTS = 3
STATUSES = {'OK', 'WARNING', 'CRITICAL', 'ERROR'}
DELIVERIES = {'BASELINE', 'NO_ALERT', 'ATTEMPTED', 'SENT', 'NOT_SENT',
              'UNCERTAIN', 'RETRIES_EXHAUSTED'}


class MonitorFailure(Exception):
    pass


class SubmissionRejected(MonitorFailure):
    pass


class SubmissionUnknown(MonitorFailure):
    pass


def private_json(path: Path) -> dict:
    if path.is_symlink():
        raise MonitorFailure('Protected settings must not be a symlink')
    info = path.stat()
    if not path.is_file() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise MonitorFailure('Protected settings owner or mode is invalid')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise MonitorFailure('Protected settings must be an object')
    return value


def require_state_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise MonitorFailure('State directory must be root-owned mode 0700')


def load_state(path: Path) -> dict:
    if not path.exists() and not path.is_symlink():
        return {}
    return validate_state(private_json(path))


def validate_state(value: object) -> dict:
    if not isinstance(value, dict):
        raise MonitorFailure('State receipt must be an object')
    if not value:
        return {}
    if value.get('status') not in STATUSES:
        raise MonitorFailure('State receipt has an invalid status')
    if type(value.get('sequence')) is not int or value['sequence'] < 1:
        raise MonitorFailure('State receipt has an invalid sequence')
    if value.get('delivery') not in DELIVERIES:
        raise MonitorFailure('State receipt has an invalid delivery state')
    if type(value.get('attempts')) is not int or not 0 <= value['attempts'] <= MAX_ATTEMPTS:
        raise MonitorFailure('State receipt has an invalid attempt count')
    for key in ('last_check_epoch', 'retry_after'):
        if key in value and (type(value[key]) not in (int, float) or not math.isfinite(value[key])):
            raise MonitorFailure(f'State receipt has an invalid {key} value')
    return value


def read_status(now: datetime) -> dict:
    try:
        result = subprocess.run(['/usr/bin/python3', str(PROBE), '--config', str(CONFIG)],
                                check=False, capture_output=True, text=True, timeout=20)
        value = json.loads(result.stdout)
        if value.get('status') not in ('OK', 'WARNING', 'CRITICAL'):
            return {'status': 'ERROR', 'checked_at': now.isoformat(),
                    'error': str(value.get('error', 'Capacity check failed'))}
        return {'status': value['status'], 'checked_at': now.astimezone(timezone.utc).isoformat(),
                **{key: value[key] for key in ('total_bytes', 'used_bytes', 'avail_bytes')}}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as error:
        return {'status': 'ERROR', 'checked_at': now.astimezone(timezone.utc).isoformat(),
                'error': type(error).__name__}


def transition(previous: dict, current: dict) -> tuple[dict, str | None]:
    old, new = previous.get('status'), current['status']
    if old == new:
        result = dict(previous)
        result['report'] = current
        return result, None
    sequence = previous.get('sequence', 0) + 1
    if old is None and new == 'OK':
        return {'status': new, 'sequence': sequence, 'delivery': 'BASELINE',
                'attempts': 0, 'event': 'BASELINE', 'report': current}, None
    levels = {'OK': 0, 'WARNING': 1, 'CRITICAL': 2, 'ERROR': 3}
    event = ('RECOVERED' if new == 'OK' and old else new
             if old is None or levels[new] > levels[old] or
             (old == 'ERROR' and new in ('WARNING', 'CRITICAL')) else None)
    return {'status': new, 'sequence': sequence,
            'delivery': 'ATTEMPTED' if event else 'NO_ALERT', 'attempts': 0,
            'event': event or 'STATUS_CHANGED', 'report': current}, event


def save(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.capacity-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def send(event: str, report: dict, sequence: int) -> None:
    try:
        config = private_json(MAIL)
        secret_path = Path(config['password_file'])
        secret_info = secret_path.stat()
        if secret_path.is_symlink() or secret_info.st_uid != 0 or stat.S_IMODE(secret_info.st_mode) != 0o600:
            raise ValueError('password file owner or mode is invalid')
        password = secret_path.read_text(encoding='utf-8').rstrip('\n')
        if config.get('enabled') is not True or config['tls_mode'] not in ('tls', 'starttls'):
            raise ValueError('mail must be enabled with TLS')
        address = re.compile(r'^[^\s<>,;@]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')
        if not address.fullmatch(config['sender']) or not address.fullmatch(config['recipient']):
            raise ValueError('invalid mail address')
        if not all(isinstance(config[key], str) and config[key] for key in ('host', 'username')):
            raise ValueError('invalid mail identity')
        if type(config['port']) is not int or not 1 <= config['port'] <= 65535:
            raise ValueError('invalid mail port')
        if not secret_path.is_absolute() or '..' in secret_path.parts:
            raise ValueError('invalid password path')
    except (OSError, ValueError, KeyError, TypeError, MonitorFailure) as error:
        raise SubmissionRejected('Protected mail configuration is invalid') from error
    message = EmailMessage()
    message['From'] = config['sender']
    message['To'] = config['recipient']
    message['Subject'] = f"[Example PBS capacity] {event}"
    message.set_content(json.dumps({'event': event, 'sequence': sequence, **report}, sort_keys=True))
    smtp = None
    try:
        factory = smtplib.SMTP_SSL if config['tls_mode'] == 'tls' else smtplib.SMTP
        options = {'timeout': 20}
        if config['tls_mode'] == 'tls':
            options['context'] = ssl.create_default_context()
        smtp = factory(config['host'], config['port'], **options)
        if config['tls_mode'] == 'starttls':
            smtp.starttls(context=ssl.create_default_context())
        smtp.login(config['username'], password)
    except (OSError, smtplib.SMTPException, KeyError, TypeError) as error:
        if smtp:
            smtp.close()
        raise SubmissionRejected('Mail transport failed before submission') from error
    try:
        smtp.send_message(message)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError, smtplib.SMTPHeloError) as error:
        raise SubmissionRejected('Mail server rejected the message') from error
    except (OSError, smtplib.SMTPException) as error:
        raise SubmissionUnknown('Mail acceptance is unknown; do not retry automatically') from error
    finally:
        smtp.close()


def run(now: datetime | None = None, *, state_dir: Path | None = None,
        status_reader=None, mail_sender=None) -> tuple[dict, str]:
    if os.geteuid() != 0 or socket.gethostname().split('.', 1)[0] != 'pve-node-3':
        raise MonitorFailure('Run as root on the pve-node-3')
    state_dir = state_dir or STATE
    status_reader = status_reader or read_status
    mail_sender = mail_sender or send
    require_state_dir(state_dir)
    now = now or datetime.now(timezone.utc)
    lock = os.open(state_dir / 'monitor.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.fchmod(lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state_dir / 'state.json'
        previous = load_state(path)
        if now.timestamp() + 1 < previous.get('last_check_epoch', 0):
            raise MonitorFailure('System clock moved backwards')
        report = status_reader(now)
        updated, event = transition(previous, report)
        updated['last_check_epoch'] = now.timestamp()
        if event is None and previous.get('delivery') == 'NOT_SENT':
            attempts = previous.get('attempts', 0)
            due = previous.get('retry_after', 0)
            if attempts < MAX_ATTEMPTS and now.timestamp() >= due:
                updated, event = dict(previous), previous.get('event')
                updated['report'] = report
                updated['last_check_epoch'] = now.timestamp()
        if event is None:
            save(path, updated)
            return report, 'UNCHANGED'
        updated['attempts'] = updated.get('attempts', 0) + 1
        updated['delivery'] = 'ATTEMPTED'
        save(path, updated)
        try:
            mail_sender(event, report, updated['sequence'])
        except SubmissionRejected:
            if updated['attempts'] >= MAX_ATTEMPTS:
                updated['delivery'] = 'RETRIES_EXHAUSTED'
            else:
                updated['delivery'] = 'NOT_SENT'
                delay = RETRY_SECONDS[min(updated['attempts'] - 1, len(RETRY_SECONDS) - 1)]
                updated['retry_after'] = now.timestamp() + delay
            save(path, updated)
            raise
        except SubmissionUnknown:
            updated['delivery'] = 'UNCERTAIN'
            save(path, updated)
            raise
        updated['delivery'] = 'SENT'
        save(path, updated)
        return report, event
    finally:
        os.close(lock)


def main() -> int:
    try:
        report, event = run()
        print(json.dumps({'status': report['status'], 'event': event}, sort_keys=True))
        return 2 if report['status'] == 'ERROR' else 1 if report['status'] in ('WARNING', 'CRITICAL') else 0
    except (MonitorFailure, SubmissionRejected, SubmissionUnknown, OSError, ValueError) as error:
        print(json.dumps({'status': 'ERROR', 'detail': str(error)}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
