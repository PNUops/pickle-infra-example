"""TLS handshake from outside: verified chain and hostname, and days until expiry."""
from __future__ import annotations

import socket
import ssl

from . import bad, ok
from .http import HOST, REASONS as HTTP_REASONS

KIND = 'tls'
DETAIL = {'days_left': int}
REASONS = {
    'CERT_EXPIRING': ('인증서가 {days_left}일 뒤에 만료돼요.', '인증서 만료 임박'),
    'CERT_EXPIRED_SOON': ('인증서 만료까지 {days_left}일밖에 남지 않았어요.', '인증서 만료 임박'),
    'CERT_INVALID': ('인증서 검증에 실패했어요.', '인증서 오류'),
    'TLS_CONNECT': ('보안 연결을 맺지 못했어요.', '접속 불가'),
    'DNS_FAILED': HTTP_REASONS['DNS_FAILED'],
}
DAY = 86400


def validate(args):
    if not isinstance(args.get('host'), str) or not HOST.fullmatch(args['host']):
        raise ValueError('host must be a lowercase DNS name')
    port = args.get('port', 443)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('port')
    warn_days, fail_days = args.get('warn_days', 20), args.get('fail_days', 7)
    if type(warn_days) is not int or type(fail_days) is not int or not 0 <= fail_days < warn_days <= 90:
        raise ValueError('need 0 <= fail_days < warn_days <= 90')
    timeout = args.get('timeout', 10)
    if type(timeout) is not int or not 2 <= timeout <= 30:
        raise ValueError('timeout')


def _handshake(host, port, timeout):
    """Return the verified peer certificate; raises what the socket layer raises."""
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as sock:
            return sock.getpeercert()


def run(args, ctx):
    try:
        certificate = _handshake(args['host'], args.get('port', 443), args.get('timeout', 10))
    except socket.gaierror:
        return bad('FAIL', 'DNS_FAILED')
    except ssl.SSLCertVerificationError:
        return bad('FAIL', 'CERT_INVALID')
    except (OSError, ValueError):
        # ssl.SSLError and socket.timeout are OSError subclasses: the target did
        # not complete a handshake, which is an outage seen from outside.
        return bad('FAIL', 'TLS_CONNECT')
    try:
        expires = ssl.cert_time_to_seconds(certificate['notAfter'])
    except (KeyError, TypeError, ValueError):
        return bad('FAIL', 'CERT_INVALID')
    days_left = int((expires - ctx.now) // DAY)
    if days_left < args.get('fail_days', 7):
        return bad('FAIL', 'CERT_EXPIRED_SOON', days_left=max(0, days_left))
    if days_left < args.get('warn_days', 20):
        return bad('WARN', 'CERT_EXPIRING', days_left=days_left)
    return ok(days_left=days_left)
