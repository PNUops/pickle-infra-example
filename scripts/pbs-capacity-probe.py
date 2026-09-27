#!/usr/bin/env python3
"""Read capacity counters from one TLS-pinned backup datastore."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re
import socket
import ssl
import stat
import sys

CONFIG = Path('/etc/pickle-example-capacity/config.json')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$')
AUTH = re.compile(r'^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+![A-Za-z0-9._-]+$')
PIN = re.compile(r'^(?:[0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2}$')
TOKEN = re.compile(r'^[A-Za-z0-9_-]{16,256}$')


class ProbeFailure(Exception):
    pass


def protected(path: Path) -> bytes:
    info = path.stat()
    if path.is_symlink() or not path.is_file() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ProbeFailure('Protected file owner or mode is invalid')
    return path.read_bytes()


def token_value(raw: bytes) -> str:
    try:
        token = raw.decode('ascii')
    except UnicodeError as error:
        raise ProbeFailure('invalid token value') from error
    if not TOKEN.fullmatch(token):
        raise ProbeFailure('invalid token value')
    return token


def settings(path: Path) -> dict:
    try:
        value = json.loads(protected(path))
        expected = {'server', 'port', 'datastore', 'auth_id', 'token_file', 'fingerprint',
                    'warning_bytes', 'critical_bytes'}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError('unexpected fields')
        if not isinstance(value['server'], str) or not value['server'].endswith('.example.invalid'):
            raise ValueError('server must use the reserved example domain')
        if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
            raise ValueError('invalid port')
        if not isinstance(value['datastore'], str) or not NAME.fullmatch(value['datastore']):
            raise ValueError('invalid datastore')
        if not isinstance(value['auth_id'], str) or not AUTH.fullmatch(value['auth_id']):
            raise ValueError('invalid token identity')
        token_path = Path(value['token_file'])
        if not token_path.is_absolute() or '..' in token_path.parts:
            raise ValueError('invalid token path')
        token = token_value(protected(token_path))
        if not isinstance(value['fingerprint'], str) or not PIN.fullmatch(value['fingerprint']):
            raise ValueError('invalid certificate pin')
        for key in ('warning_bytes', 'critical_bytes'):
            if type(value[key]) is not int or value[key] < 0:
                raise ValueError('invalid capacity threshold')
        if value['warning_bytes'] <= value['critical_bytes']:
            raise ValueError('warning must exceed critical')
        value['token'] = token
        return value
    except (OSError, ValueError, UnicodeError, KeyError, TypeError) as error:
        raise ProbeFailure('Configuration or protected token is invalid') from error


def request(config: dict, suffix: str) -> tuple[int, bytes]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(config['server'], config['port'],
                                             timeout=10, context=context)
    try:
        connection.connect()
        if connection.sock is None:
            raise ProbeFailure('TLS peer certificate is unavailable')
        actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if actual != config['fingerprint'].replace(':', '').lower():
            raise ProbeFailure('TLS certificate pin does not match')
        connection.request('GET', suffix, headers={
            'Authorization': f"PBSAPIToken={config['auth_id']}:{config['token']}",
            'Accept': 'application/json', 'Host': config['server']})
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise ProbeFailure('Response exceeded the size limit')
        return response.status, body
    except (OSError, socket.timeout, http.client.HTTPException) as error:
        raise ProbeFailure('HTTPS request failed') from error
    finally:
        connection.close()


def counters(payload: object, datastore: str) -> tuple[int, int, int]:
    if not isinstance(payload, dict) or set(payload) != {'data'} or not isinstance(payload['data'], dict):
        raise ProbeFailure('Unexpected response structure')
    data = payload['data']
    if not set(data).issubset({'total', 'used', 'avail', 'gc-status', 'counts',
                               'store', 'datastore', 'name'}):
        raise ProbeFailure('Unexpected datastore fields')
    for key in ('store', 'datastore', 'name'):
        if key in data and data[key] != datastore:
            raise ProbeFailure('Response datastore identity does not match the example config')
    values = [data.get(key) for key in ('total', 'used', 'avail')]
    if any(type(number) is not int or number < 0 for number in values):
        raise ProbeFailure('Capacity counters must be non-negative integers')
    total, used, avail = values
    if total == 0 or used > total or avail > total or used + avail > total:
        raise ProbeFailure('Capacity counters are inconsistent')
    return total, used, avail


def capacity_path(datastore: str) -> str:
    return f"/api2/json/admin/datastore/{datastore}/status?verbose=false"


def fetch_counters(config: dict, requester=request) -> tuple[int, int, int]:
    status, body = requester(config, capacity_path(config['datastore']))
    if status != 200:
        raise ProbeFailure(f'Capacity endpoint returned HTTP {status}')
    return counters(json.loads(body), config['datastore'])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--check-read-denied', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = settings(args.config)
        if args.check:
            print(json.dumps({'status': 'CONFIG_OK', 'datastore': config['datastore']}))
            return 0
        if args.check_read_denied:
            path = (f"/api2/json/admin/datastore/{config['datastore']}/download"
                    '?backup-type=host&backup-id=capacity-deny-probe'
                    '&backup-time=1&file-name=index.json.blob')
            status, _ = request(config, path)
            if status != 403:
                raise ProbeFailure(f'Expected explicit permission denial, got HTTP {status}')
            print(json.dumps({'status': 'READ_DENIED', 'http_status': status}))
            return 0
        total, used, avail = fetch_counters(config)
        if config['warning_bytes'] >= total:
            raise ProbeFailure('Warning threshold exceeds datastore size')
        result = {'total_bytes': total, 'used_bytes': used, 'avail_bytes': avail,
                  'status': 'CRITICAL' if avail <= config['critical_bytes'] else
                  'WARNING' if avail <= config['warning_bytes'] else 'OK'}
        print(json.dumps(result, sort_keys=True))
        return {'OK': 0, 'WARNING': 1, 'CRITICAL': 2}[result['status']]
    except (ProbeFailure, ValueError, UnicodeError) as error:
        print(json.dumps({'status': 'ERROR', 'error': str(error)}, sort_keys=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
