#!/usr/bin/env python3
"""Collect local operations signals and dispatch an independent Discord outbox."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

WEBHOOK = re.compile(r'https://discord\.com/api/webhooks/[0-9]{16,22}/[A-Za-z0-9_-]{32,256}\Z')
NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
LABEL = re.compile(r'[가-힣0-9 ()-]{1,60}\Z')
UNIT = re.compile(r'[A-Za-z0-9@_.-]+\.service\Z')
HEALTHY = {'OK', 'HEALTHY'}
ABNORMAL = {'WARN', 'WARNING', 'CRITICAL', 'ERROR', 'FAIL', 'FAILED'}
MAX_ATTEMPTS = 3
MAX_OUTBOX = 1000
MAX_BYTES = 4 * 1024 * 1024


class AlertError(Exception):
    """An error whose details must not disclose protected input."""


def absolute_path(value):
    if not isinstance(value, str) or not value.startswith('/') or '..' in Path(value).parts:
        raise AlertError('절대 경로 설정을 확인하세요.')
    return Path(value)


def read_file(path, *, private=False, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o022 or (private and stat.S_IMODE(info.st_mode) != 0o600)):
            raise AlertError('보호 파일 권한을 확인하세요.')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise AlertError('파일 크기 제한을 초과했습니다.')
        return raw, info
    finally:
        os.close(fd)


def load_config(path):
    value = json.loads(read_file(path, private=True)[0])
    if not isinstance(value, dict) or set(value) != {
            'expected_hostname', 'host_label', 'webhook_file', 'state_dir', 'sources'}:
        raise AlertError('알림 설정 항목을 확인하세요.')
    if not isinstance(value['expected_hostname'], str) or not NAME.fullmatch(value['expected_hostname']):
        raise AlertError('호스트 설정을 확인하세요.')
    if not isinstance(value['host_label'], str) or not LABEL.fullmatch(value['host_label']):
        raise AlertError('호스트 표시 이름은 한글과 숫자로 입력하세요.')
    absolute_path(value['webhook_file'])
    absolute_path(value['state_dir'])
    if not isinstance(value['sources'], list) or not 1 <= len(value['sources']) <= 50:
        raise AlertError('감시 대상 설정을 확인하세요.')
    names = set()
    systemd_count = 0
    for source in value['sources']:
        if not isinstance(source, dict) or set(source) != {'name', 'label', 'type', 'path', 'max_age'}:
            raise AlertError('감시 대상 항목을 확인하세요.')
        if not isinstance(source['name'], str) or not NAME.fullmatch(source['name']) or source['name'] in names:
            raise AlertError('감시 이름은 고유해야 합니다.')
        names.add(source['name'])
        if not isinstance(source['label'], str) or not LABEL.fullmatch(source['label']):
            raise AlertError('감시 표시 이름은 한글과 숫자로 입력하세요.')
        if type(source['max_age']) is not int or not 30 <= source['max_age'] <= 604800:
            raise AlertError('감시 갱신 제한을 확인하세요.')
        if source['type'] == 'systemd':
            systemd_count += 1
            if not isinstance(source['path'], str) or not UNIT.fullmatch(source['path']):
                raise AlertError('감시 서비스 이름을 확인하세요.')
        elif source['type'] in ('cron_markers', 'monitor_state', 'db_cache'):
            absolute_path(source['path'])
        else:
            raise AlertError('지원하지 않는 감시 유형입니다.')
    if systemd_count > 4:
        raise AlertError('실행 시간 제한을 위해 서비스 감시는 네 개까지 설정하세요.')
    return value


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('invalid timestamp')
    return value


def collect(source, now, *, command=subprocess.run, monotonic=time.monotonic):
    """Return only classifications; raw reports never enter the outbox."""
    try:
        kind = source['type']
        if kind == 'systemd':
            fields = ('ActiveState', 'Result', 'ExecMainStatus',
                      'ExecMainExitTimestampMonotonic', 'ExecMainStartTimestampMonotonic')
            result = command(['systemctl', 'show', source['path'], '--no-pager',
                              '--property=' + ','.join(fields)], capture_output=True,
                             text=True, timeout=8, check=True,
                             env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
            values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            finished = int(values['ExecMainExitTimestampMonotonic']) / 1000000
            started = int(values['ExecMainStartTimestampMonotonic']) / 1000000
            stamp = max(finished, started if values['ActiveState'] in ('active', 'activating') else 0)
            if stamp <= 0:
                raise ValueError('no execution')
            age = monotonic() - stamp
            condition = ('ALARM' if values['Result'] != 'success' or int(values['ExecMainStatus']) != 0
                         else 'NORMAL')
        elif kind == 'cron_markers':
            base = source['path']
            found = {}
            for suffix in ('ok', 'fail'):
                try:
                    _, info = read_file(Path(base + '.' + suffix))
                    found[suffix] = info.st_mtime
                except FileNotFoundError:
                    pass
            if not found:
                raise ValueError('no markers')
            age = now - max(found.values())
            condition = 'ALARM' if 'fail' in found else 'NORMAL'
        else:
            raw, info = read_file(Path(source['path']))
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError('invalid state')
            if kind == 'db_cache':
                # This cache advances only after a fully healthy remote read.
                point = number(state['recovery_point_epoch'])
                readback = number(state['last_readback_epoch'])
                if point <= 0 or readback < point or readback > now + 5:
                    raise ValueError('invalid recovery point')
                age = now - info.st_mtime
                condition = 'NORMAL' if -5 <= now - point < source['max_age'] else 'ALARM'
            else:
                status = state.get('status', state.get('level'))
                if status not in HEALTHY | ABNORMAL:
                    raise ValueError('unknown classification')
                age = now - number(state['last_check_epoch'])
                condition = 'NORMAL' if status in HEALTHY else 'ALARM'
        if age < -5:
            raise ValueError('future observation')
        if age > source['max_age'] and condition == 'NORMAL':
            # An old healthy record cannot establish recovery of a newer incident.
            condition = 'UNKNOWN'
        return {'condition': condition, 'freshness': 'ALARM' if age > source['max_age'] else 'NORMAL'}
    except (OSError, ValueError, KeyError, TypeError, AlertError, subprocess.SubprocessError):
        return {'condition': 'UNKNOWN', 'freshness': 'ALARM'}


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.discord-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_state(state):
    if (not isinstance(state, dict) or state.get('schema') != 1
            or not isinstance(state.get('observations'), dict) or not isinstance(state.get('outbox'), list)):
        raise AlertError('알림 상태 파일을 확인하세요.')
    if len(state['outbox']) > MAX_OUTBOX:
        raise AlertError('알림 대기열 제한을 초과했습니다.')
    for item in state['outbox']:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not isinstance(item.get('content'), str)
                or item.get('delivery') not in {'PENDING', 'ATTEMPTED', 'SENT', 'UNCERTAIN', 'FAILED'}
                or type(item.get('attempts')) is not int or not 0 <= item['attempts'] <= MAX_ATTEMPTS):
            raise AlertError('알림 전송 기록을 확인하세요.')
    return state


def new_state():
    return {'schema': 1, 'observations': {}, 'outbox': []}


def enqueue(state, config, source, dimension, current, now):
    key = source['name'] + ':' + dimension
    old = state['observations'].get(key, {})
    if current == 'UNKNOWN' or old.get('status') == current:
        return
    sequence = old.get('sequence', 0) + 1
    state['observations'][key] = {'status': current, 'sequence': sequence}
    if not old and current == 'NORMAL':
        return
    recovered = current == 'NORMAL'
    if dimension == 'freshness':
        label = '감시 결과 갱신 복구' if recovered else '감시 결과 갱신 지연'
    elif source['type'] == 'db_cache':
        label = '정상 확인 복구' if recovered else '정상 확인 지연'
    else:
        label = '정상 복구' if recovered else '장애 감지'
    stamp = datetime.fromtimestamp(now, timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
    content = f"[Pickle 운영 알림] {label}\n대상: {config['host_label']} / {source['label']}\n확인 시각: {stamp} 한국 표준시"
    if source['type'] == 'db_cache':
        content += '\n정상 판정 때만 갱신되는 기록을 확인했습니다. 현재 백업 결과는 별도로 확인하세요.'
    state['outbox'].append({'id': key + ':' + str(sequence), 'content': content,
                            'delivery': 'PENDING', 'attempts': 0, 'next_attempt': now})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def send_discord(webhook, content, *, opener=None):
    """Return fixed outcomes only; exceptions may contain the secret URL."""
    if not WEBHOOK.fullmatch(webhook):
        raise AlertError('웹훅 주소 형식을 확인하세요.')
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    data = json.dumps({'content': content, 'allowed_mentions': {'parse': []}}, ensure_ascii=False).encode()
    request = urllib.request.Request(webhook + '?wait=true', data=data,
        headers={'Content-Type': 'application/json', 'User-Agent': 'PickleOpsAlerts/1'}, method='POST')
    try:
        with opener.open(request, timeout=15) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                return {'delivery': 'UNCERTAIN', 'code': 'INVALID_SUCCESS'}
            message = json.loads(body)
            message_id = message.get('id') if isinstance(message, dict) else None
            if not isinstance(message_id, str) or not re.fullmatch(r'[0-9]{16,22}', message_id):
                return {'delivery': 'UNCERTAIN', 'code': 'NO_MESSAGE_ID'}
            return {'delivery': 'SENT', 'message_id': message_id, 'code': 'ACKNOWLEDGED'}
    except urllib.error.HTTPError as error:
        try:
            if error.code == 429:
                try:
                    value = json.loads(error.read(65537))
                    delay = number(value['retry_after'])
                    if not 0 <= delay <= 604800:
                        raise ValueError('invalid retry delay')
                    return {'delivery': 'PENDING', 'retry_after': max(1, delay), 'code': 'RATE_LIMITED'}
                except (OSError, ValueError, KeyError, TypeError):
                    return {'delivery': 'FAILED', 'code': 'INVALID_RATE_LIMIT'}
            if 400 <= error.code < 500:
                return {'delivery': 'PENDING', 'code': 'REJECTED'}
            return {'delivery': 'UNCERTAIN' if error.code >= 500 else 'FAILED', 'code': 'HTTP_UNCONFIRMED'}
        finally:
            error.close()
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return {'delivery': 'UNCERTAIN', 'code': 'RESPONSE_UNKNOWN'}


def dispatch(state, path, webhook, now, *, sender=send_discord, only_test=False):
    sent = 0
    attempted = 0
    if state.get('webhook_retry_after', 0) > now:
        return sent
    for item in state['outbox']:
        if only_test and item['id'] != 'connection-test':
            continue
        if item['delivery'] == 'ATTEMPTED':
            item.update(delivery='UNCERTAIN', code='INTERRUPTED')
            atomic_json(path, state)
        if item['delivery'] != 'PENDING' or item['next_attempt'] > now:
            continue
        if attempted >= 3:
            break
        if item['attempts'] >= MAX_ATTEMPTS:
            item.update(delivery='FAILED', code='RETRIES_EXHAUSTED')
            atomic_json(path, state)
            continue
        item['attempts'] += 1
        attempted += 1
        item['delivery'] = 'ATTEMPTED'
        atomic_json(path, state)
        try:
            outcome = sender(webhook, item['content'])
        except Exception:
            # A transport failure must not leak its URL or replay an ambiguous send.
            outcome = {'delivery': 'UNCERTAIN', 'code': 'SENDER_INTERRUPTED'}
        item.update(outcome)
        if item.get('code') == 'RATE_LIMITED':
            # An explicit rate limit is a known rejection, not a failed delivery attempt.
            item['attempts'] -= 1
            item['rate_limit_count'] = item.get('rate_limit_count', 0) + 1
        if item['delivery'] == 'PENDING':
            delay = item.pop('retry_after', 60 if item['attempts'] == 1 else 300)
            item['next_attempt'] = now + delay
            if item.get('code') == 'RATE_LIMITED':
                state['webhook_retry_after'] = now + delay
            if item['attempts'] >= MAX_ATTEMPTS:
                item.update(delivery='FAILED', code='RETRIES_EXHAUSTED')
        sent += item['delivery'] == 'SENT'
        atomic_json(path, state)
        # A rate limit applies to the shared webhook, including other queued events.
        if item.get('code') == 'RATE_LIMITED':
            break
    return sent


def run(config, *, inspect=False, test_message=False, now=None, collector=collect, sender=send_discord):
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != config['expected_hostname']:
        raise AlertError('선택한 호스트에서 관리자 권한으로 실행하세요.')
    now = time.time() if now is None else now
    observations = {s['name']: collector(s, now) for s in config['sources']}
    if inspect:
        return {'mode': 'INSPECT', 'sources': observations}
    webhook = read_file(absolute_path(config['webhook_file']), private=True)[0].decode('ascii').rstrip('\n')
    if not WEBHOOK.fullmatch(webhook):
        raise AlertError('웹훅 주소 형식을 확인하세요.')
    directory = absolute_path(config['state_dir'])
    if not directory.exists():
        directory.mkdir(mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise AlertError('알림 상태 디렉터리 권한을 확인하세요.')
    fd = os.open(directory / 'dispatcher.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise AlertError('알림 잠금 파일을 확인하세요.')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / 'state.json'
        state = validate_state(json.loads(read_file(path, private=True, limit=MAX_BYTES)[0])) if path.exists() else new_state()
        if now + 1 < state.get('last_run', 0):
            raise AlertError('시스템 시각이 이전 실행보다 과거입니다.')
        # Keep unresolved evidence and a bounded tail of confirmed deliveries.
        completed = [item for item in state['outbox'] if item['delivery'] == 'SENT'][-100:]
        state['outbox'] = [item for item in state['outbox'] if item['delivery'] != 'SENT'] + completed
        if test_message:
            if not state.get('test_enqueued'):
                state['outbox'].append({'id': 'connection-test', 'content': '[Pickle 운영 알림 시험] 연결 확인\n대상: ' + config['host_label'],
                    'delivery': 'PENDING', 'attempts': 0, 'next_attempt': now})
                state['test_enqueued'] = True
        else:
            for source in config['sources']:
                for dimension, current in observations[source['name']].items():
                    enqueue(state, config, source, dimension, current, now)
        if len(state['outbox']) > MAX_OUTBOX:
            raise AlertError('알림 대기열 제한을 초과했습니다.')
        state['last_run'] = now
        atomic_json(path, state)
        sent = dispatch(state, path, webhook, now, sender=sender, only_test=test_message)
        counts = {label: sum(item['delivery'] == label for item in state['outbox'])
                  for label in ('PENDING', 'SENT', 'UNCERTAIN', 'FAILED')}
        return {'mode': 'TEST' if test_message else 'RUN', 'sent_now': sent, 'deliveries': counts}
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--inspect', '--dry-run', action='store_true', dest='inspect')
    modes.add_argument('--run', action='store_true')
    modes.add_argument('--test-message', action='store_true')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result = run(config, inspect=args.inspect or not (args.run or args.test_message), test_message=args.test_message)
        print(json.dumps(result, sort_keys=True))
        return 2 if any(result.get('deliveries', {}).get(k, 0) for k in ('UNCERTAIN', 'FAILED')) else 0
    except (AlertError, OSError, ValueError, TypeError, KeyError):
        print('운영 알림 처리를 중단했습니다. 보호된 설정과 전송 기록을 확인하세요.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
