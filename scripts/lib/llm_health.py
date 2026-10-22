"""Bounded local health reports and persistent first-only incident delivery."""
from __future__ import annotations

from contextlib import contextmanager
from email.message import EmailMessage
import fcntl
import json
import math
import os
from pathlib import Path
import re
import smtplib
import ssl
import stat
import tempfile

MAX_JSON = 65536
NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}')
STATES = {'HEALTHY', 'FAILED', 'LOADING', 'OBSERVER_ERROR', 'BUDGET_LIMIT'}


class HealthError(RuntimeError):
    pass


class MailNotSent(HealthError):
    pass


class MailUncertain(HealthError):
    pass


def private_read(path: Path, limit: int = MAX_JSON) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise HealthError('PRIVATE_FILE_INVALID')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            value = handle.read(limit + 1)
        if len(value) > limit:
            raise HealthError('PRIVATE_FILE_TOO_LARGE')
        return value
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict:
    value = json.loads(private_read(path))
    if not isinstance(value, dict):
        raise HealthError('JSON_OBJECT_REQUIRED')
    return value


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise HealthError('PRIVATE_DIRECTORY_INVALID')


def atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.health-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, allow_nan=False, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(directory: Path):
    private_directory(directory)
    descriptor = os.open(directory / 'monitor.lock',
                         os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise HealthError('LOCK_INVALID')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HealthError('ALREADY_RUNNING') from error
        yield
    finally:
        os.close(descriptor)


def epoch(value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise HealthError('INVALID_EPOCH')
    return float(value)


def observe(state: dict, status: str, observed: float, *, failure_count: int,
            freshness: int, recovery_samples: int = 2, recovery_seconds: int = 3600) -> str:
    """Only a new source sample advances a streak; gaps invalidate recovery."""
    if status not in STATES:
        raise HealthError('INVALID_STATUS')
    observed = epoch(observed)
    previous = state.get('sample_at')
    if previous is not None and observed <= previous:
        return 'DUPLICATE'
    gap = previous is not None and observed - previous > freshness
    state['sample_at'] = observed
    state['latest_status'] = status
    if gap or status not in ('HEALTHY', 'FAILED'):
        state['failures'] = 0
        state['healthy_since'] = None
        state['healthy_samples'] = 0
    if status == 'FAILED':
        state['healthy_since'] = None
        state['healthy_samples'] = 0
        state['failures'] = state.get('failures', 0) + 1
        state['last_failure'] = observed
        if not state.get('incident') and state['failures'] >= failure_count:
            state['sequence'] = state.get('sequence', 0) + 1
            state['incident'] = state['sequence']
            state['opened_at'] = observed
            return 'FAILED'
    elif status == 'HEALTHY':
        state['failures'] = 0
        if state.get('incident'):
            if state.get('healthy_since') is None:
                state['healthy_since'] = observed
            state['healthy_samples'] = state.get('healthy_samples', 0) + 1
            if (state['healthy_samples'] >= recovery_samples
                    and observed - state['healthy_since'] >= recovery_seconds):
                state['closed_incident'] = state.pop('incident')
                state['closed_at'] = observed
                state['healthy_since'] = None
                return 'RECOVERED'
    return 'UNCHANGED'


def reserve_event(state: dict, target: str, event: str, now: float) -> dict:
    sequence = state.get('incident') if event == 'FAILED' else state.get('closed_incident')
    delivery = {'target': target, 'event': event, 'incident': sequence,
                'attempts': 0, 'status': 'NEW', 'created_at': now}
    state.setdefault('deliveries', {})[event] = delivery
    return delivery


def deliver(delivery: dict, now: float, sender, persist) -> None:
    if delivery['status'] not in ('NEW', 'NOT_SENT'):
        return
    if now < delivery.get('retry_after', 0):
        return
    delivery['attempts'] += 1
    delivery['status'] = 'ATTEMPTED'
    persist()
    try:
        sender(delivery)
    except MailNotSent:
        delivery['status'] = 'EXHAUSTED' if delivery['attempts'] >= 3 else 'NOT_SENT'
        delivery['retry_after'] = now + (60 if delivery['attempts'] == 1 else 300)
    except Exception:
        # A crash persisted as ATTEMPTED is also deliberately never replayed.
        delivery['status'] = 'UNCERTAIN'
    else:
        delivery['status'] = 'SENT'
    persist()


def smtp_sender(config: dict):
    if (config.get('tls_mode') not in ('tls', 'starttls')
            or type(config.get('port')) is not int or not 1 <= config['port'] <= 65535):
        raise HealthError('MAIL_CONFIG_INVALID')
    for name in ('host', 'username', 'sender', 'recipient', 'password_file'):
        value = config.get(name)
        if not isinstance(value, str) or not value or '\r' in value or '\n' in value:
            raise HealthError('MAIL_CONFIG_INVALID')
    for name in ('sender', 'recipient'):
        if not re.fullmatch(r'[^\s<>,;@]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', config[name]):
            raise HealthError('MAIL_ADDRESS_INVALID')
    password = private_read(Path(config['password_file']), 4096).decode().strip()
    if not password:
        raise HealthError('MAIL_PASSWORD_EMPTY')

    def send(delivery: dict):
        message = EmailMessage()
        message['From'] = config['sender']
        message['To'] = config['recipient']
        stage = '장애' if delivery['event'] == 'FAILED' else '안정 복구'
        if delivery.get('severity') == 'warning':
            stage = '주의' if delivery['event'] == 'FAILED' else '주의 해소'
        message['Subject'] = f"[PNU Cloud 감시] {delivery['target']} {stage}"
        message.set_content(f"대상: {delivery['target']}\n상태: {stage}\n"
                            f"incident: {delivery['incident']}\n"
                            '실제 생성·조회·감시자 상태는 개별 결과 파일에서 확인하세요.\n')
        smtp = None
        try:
            context = ssl.create_default_context()
            smtp = (smtplib.SMTP_SSL(config['host'], config['port'], timeout=20, context=context)
                    if config['tls_mode'] == 'tls' else
                    smtplib.SMTP(config['host'], config['port'], timeout=20))
            if config['tls_mode'] == 'starttls':
                smtp.starttls(context=context)
            smtp.login(config['username'], password)
        except (OSError, smtplib.SMTPException) as error:
            if smtp:
                smtp.close()
            raise MailNotSent('SMTP_PRE_SUBMISSION_FAILURE') from error
        try:
            smtp.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError) as error:
            raise MailNotSent('SMTP_EXPLICIT_REJECTION') from error
        except (OSError, smtplib.SMTPException) as error:
            raise MailUncertain('SMTP_ACCEPTANCE_UNKNOWN') from error
        finally:
            smtp.close()
    return send
