"""HTTPS request from outside: status code and an expected response shape."""
from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from . import bad, ok

KIND = 'http'
DETAIL = {'http_status': int, 'phase': str, 'latency_ms': int}
REASONS = {
    'HTTP_STATUS': ('HTTP {http_status} 응답이 왔어요.', 'HTTP 오류'),
    'HTTP_TIMEOUT': ('{phase} 단계에서 응답 시간이 초과됐어요.', '응답 없음'),
    'HTTP_CONNECT': ('서버에 접속하지 못했어요.', '접속 불가'),
    'DNS_FAILED': ('도메인 주소를 찾지 못했어요.', 'DNS 오류'),
    'TLS_INVALID': ('인증서 검증에 실패했어요.', '인증서 오류'),
    'BODY_MISMATCH': ('응답은 왔지만 내용이 예상과 달라요.', '응답 이상'),
}
HOST = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z')
MAX_BODY = 256 * 1024


def validate(args):
    url = urlsplit(args['url'])
    if url.scheme != 'https' or not HOST.fullmatch(url.hostname or '') or url.username or url.query or url.fragment:
        raise ValueError('an explicit https URL without credentials or query is required')
    expect = args.get('expect', {'kind': 'status'})
    kind = expect.get('kind')
    if kind not in ('status', 'json', 'html', 'text'):
        raise ValueError('expect.kind')
    if type(args.get('status', 200)) is not int:
        raise ValueError('status')
    if kind == 'json' and (not isinstance(expect.get('fields'), dict) or not expect['fields']):
        raise ValueError('expect.fields')
    if kind in ('html', 'text') and (not isinstance(expect.get('value'), str) or len(expect['value']) < 4):
        raise ValueError('expect.value')
    if 'connect_address' in args and not isinstance(args['connect_address'], str):
        raise ValueError('connect_address')
    timeout = args.get('timeout', 15)
    if type(timeout) is not int or not 2 <= timeout <= 40:
        raise ValueError('timeout')


def _matches(expect, body):
    kind = expect.get('kind', 'status')
    if kind == 'status':
        return True
    text = body.decode('utf-8', 'replace')
    if kind == 'json':
        try:
            value = json.loads(text)
        except ValueError:
            return False
        return isinstance(value, dict) and all(value.get(key) == want for key, want in expect['fields'].items())
    if kind == 'html':
        return expect['value'] in text
    return text.strip() == expect['value']


def run(args, ctx):
    url = urlsplit(args['url'])
    timeout = args.get('timeout', 15)
    started = time.monotonic()
    phase = 'DNS'
    try:
        address = args.get('connect_address') or url.hostname
        infos = socket.getaddrinfo(address, url.port or 443, type=socket.SOCK_STREAM)
        phase = 'TCP'
        raw = socket.create_connection(infos[0][4][:2], timeout=timeout)
        phase = 'TLS'
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=url.hostname)
        phase = 'HTTP'
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=timeout, context=context)
        connection.sock = sock
        connection.request('GET', url.path or '/', headers={'User-Agent': 'PickleOpsWatch/1', 'Accept': '*/*',
                                                             'Connection': 'close'})
        response = connection.getresponse()
        body = response.read(MAX_BODY)
        connection.close()
    except socket.gaierror:
        return bad('FAIL', 'DNS_FAILED')
    except ssl.SSLCertVerificationError:
        return bad('FAIL', 'TLS_INVALID')
    except (socket.timeout, TimeoutError):
        return bad('FAIL', 'HTTP_TIMEOUT', phase=phase)
    except (OSError, http.client.HTTPException):
        return bad('FAIL', 'HTTP_CONNECT', phase=phase)
    latency = int((time.monotonic() - started) * 1000)
    if response.status != args.get('status', 200):
        return bad('FAIL', 'HTTP_STATUS', http_status=response.status, latency_ms=latency)
    if not _matches(args.get('expect', {'kind': 'status'}), body):
        return bad('FAIL', 'BODY_MISMATCH', http_status=response.status, latency_ms=latency)
    return ok(http_status=response.status, latency_ms=latency)
