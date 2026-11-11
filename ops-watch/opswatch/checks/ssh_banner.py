"""TCP connect to an SSH endpoint and require an SSH-2.0 identification line."""
from __future__ import annotations

import socket
import time

from . import bad, ok
from .http import HOST, REASONS as HTTP_REASONS

KIND = 'ssh_banner'
DETAIL = {'latency_ms': int}
REASONS = {
    'SSH_NO_BANNER': ('접속은 됐지만 SSH 응답이 오지 않았어요.', 'SSH 응답 이상'),
    'SSH_CONNECT': ('SSH 서버에 접속하지 못했어요.', '접속 불가'),
    'SSH_TIMEOUT': ('SSH 서버 접속 시간이 초과됐어요.', '응답 없음'),
    'DNS_FAILED': HTTP_REASONS['DNS_FAILED'],
}
# RFC 4253 caps the identification line at 255 bytes including CRLF. Servers
# may send other lines first, so read a little more and take the first SSH- line.
MAX_READ = 1024


def validate(args):
    if not isinstance(args.get('host'), str) or not HOST.fullmatch(args['host']):
        raise ValueError('host must be a lowercase DNS name')
    port = args.get('port', 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('port')
    timeout = args.get('timeout', 10)
    if type(timeout) is not int or not 2 <= timeout <= 30:
        raise ValueError('timeout')


def _read_banner(sock, deadline):
    data = b''
    while len(data) < MAX_READ:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(MAX_READ - len(data))
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        data += chunk
        for line in data.split(b'\n')[:-1]:
            if line.startswith(b'SSH-'):
                return line
    return None


def run(args, ctx):
    timeout = args.get('timeout', 10)
    started = time.monotonic()
    try:
        sock = socket.create_connection((args['host'], args.get('port', 22)), timeout=timeout)
    except socket.gaierror:
        return bad('FAIL', 'DNS_FAILED')
    except (socket.timeout, TimeoutError):
        return bad('FAIL', 'SSH_TIMEOUT')
    except OSError:
        return bad('FAIL', 'SSH_CONNECT')
    try:
        line = _read_banner(sock, started + timeout)
    except OSError:
        line = None
    finally:
        sock.close()
    latency = int((time.monotonic() - started) * 1000)
    if line is None or not line.startswith(b'SSH-2.0-'):
        return bad('FAIL', 'SSH_NO_BANNER', latency_ms=latency)
    return ok(latency_ms=latency)
