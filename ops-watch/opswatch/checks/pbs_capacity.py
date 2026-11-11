"""Free space on the PBS datastore, read from the PBS API with a pinned certificate.

Ported from scripts/pbs-capacity-probe.py (configuration rules, pinned TLS,
response validation). The thresholds move from the protected configuration
into catalog args; the transition and mail logic of pbs-capacity-monitor.py
is the judge's job now. The configured server is a tailnet address, so it
never leaves this module.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl

from . import CheckError, bad, ok
from ._pbs import SHARED_REASONS, absolute_path, read_private_json, read_private_text

KIND = 'pbs_capacity'
DETAIL = {'used_pct': int, 'avail_gb': int}
REASONS = dict(SHARED_REASONS, **{
    'PBS_SPACE_LOW': ('백업 보관소 여유 공간이 {avail_gb}GB 남았어요.', '용량 부족'),
    'PBS_IDENTITY_MISMATCH': ('백업 서버(PBS)의 인증서가 등록된 것과 달라요.', '백업 서버 이상'),
    'PBS_AUTH_REJECTED': ('백업 서버(PBS)가 점검용 토큰을 거부했어요.', '점검 오류'),
})
GB = 10**9
HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z')
DATASTORE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
AUTH_ID = re.compile(r'[A-Za-z0-9._-]+@[A-Za-z0-9._-]+![A-Za-z0-9._-]+\Z')
FINGERPRINT = re.compile(r'(?:[0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2}\Z')
TOKEN_SECRET = re.compile(r'[A-Za-z0-9_-]{16,256}\Z')
MAX_BODY = 65536


class Unavailable(Exception):
    """The PBS server did not give a usable answer."""


class IdentityMismatch(Exception):
    pass


def validate(args):
    if not absolute_path(args.get('config', '/etc/pickle-pbs-capacity/config.json')):
        raise ValueError('config')
    warn, fail = args.get('warn_avail_gb', 100), args.get('fail_avail_gb', 50)
    if type(warn) is not int or type(fail) is not int or not 0 <= fail < warn:
        raise ValueError('fail_avail_gb must be below warn_avail_gb')
    timeout = args.get('timeout', 10)
    if type(timeout) is not int or not 2 <= timeout <= 30:
        raise ValueError('timeout')


def load_config(path):
    config = read_private_json(path)
    if (not isinstance(config.get('server'), str) or not HOST.fullmatch(config['server'])
            or type(config.get('port')) is not int or not 1 <= config['port'] <= 65535
            or not isinstance(config.get('datastore'), str) or not DATASTORE.fullmatch(config['datastore'])
            or not isinstance(config.get('auth_id'), str) or not AUTH_ID.fullmatch(config['auth_id'])
            or not isinstance(config.get('tls_fingerprint'), str)
            or not FINGERPRINT.fullmatch(config['tls_fingerprint'])
            or not absolute_path(config.get('token_file'))):
        raise CheckError('BACKUP_CONFIG_INVALID')
    token = read_private_text(config['token_file'])
    if not TOKEN_SECRET.fullmatch(token):
        raise CheckError('BACKUP_CONFIG_INVALID')
    return config, token


def fetch(config, token, timeout):
    """Return (http_status, body bytes) from the datastore status endpoint."""
    context = ssl.create_default_context()
    # Trust is the pinned fingerprint below, exactly as in the original probe.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(config['server'], config['port'], timeout=timeout, context=context)
    try:
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True) if connection.sock else None
        expected = config['tls_fingerprint'].replace(':', '').lower()
        if certificate is None or hashlib.sha256(certificate).hexdigest() != expected:
            raise IdentityMismatch()
        connection.request('GET', f"/api2/json/admin/datastore/{config['datastore']}/status?verbose=false",
                           headers={'Authorization': f"PBSAPIToken={config['auth_id']}:{token}",
                                    'Accept': 'application/json', 'Host': config['server']})
        response = connection.getresponse()
        return response.status, response.read(MAX_BODY + 1)
    except (OSError, socket.timeout, http.client.HTTPException):
        raise Unavailable() from None
    finally:
        connection.close()


def parse(body, datastore):
    """Return (total, used, avail) bytes, or raise Unavailable on a malformed answer."""
    if len(body) > MAX_BODY:
        raise Unavailable()
    try:
        value = json.loads(body)
    except ValueError:
        raise Unavailable() from None
    data = value.get('data') if isinstance(value, dict) else None
    if not isinstance(data, dict):
        raise Unavailable()
    if 'backend-type' in data and data['backend-type'] != 'filesystem':
        raise Unavailable()
    if any(key in data and data[key] != datastore for key in ('store', 'datastore', 'name')):
        raise Unavailable()
    numbers = [data.get(key) for key in ('total', 'used', 'avail')]
    if any(type(number) is not int or number < 0 for number in numbers):
        raise Unavailable()
    total, used, avail = numbers
    if total <= 0 or used > total or avail > total or used + avail > total:
        raise Unavailable()
    return total, used, avail


def run(args, ctx):
    config, token = load_config(args.get('config', '/etc/pickle-pbs-capacity/config.json'))
    try:
        status, body = fetch(config, token, args.get('timeout', 10))
        if status in (401, 403):
            raise CheckError('PBS_AUTH_REJECTED')
        if status != 200:
            raise Unavailable()
        total, used, avail = parse(body, config['datastore'])
    except IdentityMismatch:
        return bad('FAIL', 'PBS_IDENTITY_MISMATCH')
    except Unavailable:
        return bad('FAIL', 'PBS_UNAVAILABLE')
    detail = {'used_pct': round(used * 100 / total), 'avail_gb': avail // GB}
    if avail < args.get('fail_avail_gb', 50) * GB:
        return bad('FAIL', 'PBS_SPACE_LOW', **detail)
    if avail < args.get('warn_avail_gb', 100) * GB:
        return bad('WARN', 'PBS_SPACE_LOW', **detail)
    return ok(**detail)
