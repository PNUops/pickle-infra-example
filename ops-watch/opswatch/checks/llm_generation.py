"""Known-answer chat completion against an OpenAI-compatible endpoint.

Every run spends a real generation, possibly on a paid route, so the check
bounds itself: an output-token cap on the request, a daily request budget and a
minimum spacing kept in ctx.state. A refused run is a monitoring problem
(ERROR), never an outage.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from . import bad, ok
from .http import HOST, REASONS as HTTP_REASONS
from ..util import OpsError, kst_day, read_protected

KIND = 'llm_generation'
DETAIL = {'http_status': int, 'latency_ms': int, 'completion_tokens': int}
REASONS = {
    'GENERATION_HTTP': ('생성 요청에 HTTP {http_status} 응답이 왔어요.', '생성 오류'),
    'GENERATION_TIMEOUT': ('생성 응답이 제한 시간 안에 오지 않았어요.', '응답 없음'),
    'GENERATION_WRONG_ANSWER': ('생성 응답이 정해진 답이나 형식과 달라요.', '생성 오류'),
    'GENERATION_EMPTY': ('생성 응답에 답변 내용이 없어요.', '생성 오류'),
    'GENERATION_CONNECT': ('생성 서버에 접속하지 못했어요.', '접속 불가'),
    'GENERATION_KEY_MISSING': ('점검용 키를 읽지 못했어요.', '점검 오류'),
    'GENERATION_KEY_REJECTED': ('점검용 키가 거부됐어요.', '점검 오류'),
    'GENERATION_BUDGET': ('오늘 쓸 수 있는 점검 요청 횟수를 넘었어요.', '점검 오류'),
    'TLS_INVALID': HTTP_REASONS['TLS_INVALID'],
}
PROMPT = 'What is 2+2? Reply with the digit only.'
ANSWER = '4'
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z')
# Plain http is allowed only to a host-local or RFC 1918 literal, for a model
# server checked from its own machine (gpu-node publishes vLLM on its campus address).
# Built from integers, as in the older probe, so the public sanitized copy of
# this file carries no address literal. Loopback, the three RFC 1918 blocks and
# the RFC 2544 block: none of them is reachable from the internet.
LOCAL_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    (0x7F000000, 8), (0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16), (0xC6120000, 15)))


def _local_literal(host):
    try:
        address = ipaddress.ip_address(host or '')
    except ValueError:
        return False
    return any(address in network for network in LOCAL_NETWORKS)
MAX_BODY = 256 * 1024
# The agent treats an item as due DUE_SLACK seconds early, so a spacing check
# that demanded the full interval would refuse every other scheduled run.
SCHEDULE_TOLERANCE = 30  # the agent may start a due check a little early or late


def validate(args):
    url = urlsplit(args['url'])
    local = args.get('allow_local_http', False)
    if type(local) is not bool:
        raise ValueError('allow_local_http')
    if url.scheme == 'https':
        if not HOST.fullmatch(url.hostname or '') or url.port not in (None, 443):
            raise ValueError('https URL needs a DNS name on port 443')
    elif not (url.scheme == 'http' and local and _local_literal(url.hostname) and url.port):
        raise ValueError('plain http is allowed only to a local or private address with allow_local_http')
    if url.username or url.password or url.query or url.fragment or not url.path.endswith('/chat/completions'):
        raise ValueError('url must be a chat completions endpoint without credentials or query')
    if not isinstance(args.get('model'), str) or not MODEL.fullmatch(args['model']):
        raise ValueError('model')
    if not isinstance(args.get('key_file'), str) or not args['key_file'].startswith('/'):
        raise ValueError('key_file must be an absolute path')
    bounds = {'max_output_tokens': (128, 1, 256), 'daily_budget': (312, 1, 2000),
              'min_interval': (300, 30, 86400), 'timeout': (30, 5, 40)}
    for name, (default, low, high) in bounds.items():
        value = args.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{name} must be {low}..{high}')
    if type(args.get('disable_thinking', False)) is not bool:
        raise ValueError('disable_thinking')


def _read_key(path):
    try:
        key = read_protected(path, private=True, limit=4096).decode('ascii').strip()
    except (OSError, OpsError, UnicodeError):
        return None
    if not key or any(char.isspace() for char in key):
        return None
    return key


def _admit(args, state, now):
    """Count this run against the budget, or say why it may not run."""
    day = kst_day(now)
    if state.get('day') != day:
        state['day'], state['submissions'] = day, 0
    last = state.get('last_submission')
    if type(last) in (int, float) and now < last:
        return False  # the clock moved backwards; spacing cannot be trusted
    if type(last) in (int, float) and now - last < args.get('min_interval', 300) - SCHEDULE_TOLERANCE:
        return False
    if state['submissions'] >= args.get('daily_budget', 312):
        return False
    state['submissions'] += 1
    state['last_submission'] = now
    return True


def _request(args, key):
    url = urlsplit(args['url'])
    timeout = args.get('timeout', 30)
    if url.scheme == 'https':
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=timeout,
                                                 context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
    payload = {'model': args['model'], 'messages': [{'role': 'user', 'content': PROMPT}],
               'temperature': 0, 'top_p': 1, 'max_completion_tokens': args.get('max_output_tokens', 128),
               'stream': False}
    if args.get('disable_thinking'):
        payload['chat_template_kwargs'] = {'enable_thinking': False}
    try:
        connection.request('POST', url.path, json.dumps(payload).encode(), {
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
            'Accept': 'application/json', 'User-Agent': 'PickleOpsWatch/1', 'Connection': 'close'})
        response = connection.getresponse()
        return response.status, response.read(MAX_BODY + 1)
    finally:
        connection.close()


def judge_answer(document, max_output_tokens):
    """Return (reason, completion_tokens) for a parsed 200 response."""
    try:
        choice = document['choices'][0]
        content = choice['message'].get('content')
        finish = choice.get('finish_reason')
    except (KeyError, IndexError, TypeError, AttributeError):
        return 'GENERATION_EMPTY', None
    usage = document.get('usage') if isinstance(document.get('usage'), dict) else {}
    output = usage.get('completion_tokens')
    tokens = output if type(output) is int and 0 < output <= 1 << 20 else None
    if not isinstance(content, str) or not content.strip():
        return 'GENERATION_EMPTY', tokens
    # finish_reason=stop is what tells an answer from an exhausted reasoning
    # budget, and a reply longer than the cap means the cap was not honoured.
    if (content.strip() != ANSWER or finish != 'stop' or tokens is None or tokens > max_output_tokens):
        return 'GENERATION_WRONG_ANSWER', tokens
    return 'HEALTHY', tokens


def run(args, ctx):
    key = _read_key(args['key_file'])
    if key is None:
        return bad('ERROR', 'GENERATION_KEY_MISSING')
    if not _admit(args, ctx.state, ctx.now):
        return bad('ERROR', 'GENERATION_BUDGET')
    started = time.monotonic()
    try:
        status, body = _request(args, key)
    except ssl.SSLCertVerificationError:
        return bad('FAIL', 'TLS_INVALID')
    except (socket.timeout, TimeoutError):
        return bad('FAIL', 'GENERATION_TIMEOUT')
    except (OSError, http.client.HTTPException):
        return bad('FAIL', 'GENERATION_CONNECT')
    latency = int((time.monotonic() - started) * 1000)
    if status in (401, 403):
        return bad('ERROR', 'GENERATION_KEY_REJECTED', http_status=status)
    if status != 200:
        return bad('FAIL', 'GENERATION_HTTP', http_status=status, latency_ms=latency)
    try:
        if len(body) > MAX_BODY:
            raise ValueError('oversized')
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError('not an object')
    except ValueError:
        return bad('FAIL', 'GENERATION_EMPTY', http_status=status, latency_ms=latency)
    reason, tokens = judge_answer(document, args.get('max_output_tokens', 128))
    detail = {'http_status': status, 'latency_ms': latency}
    if tokens is not None:
        detail['completion_tokens'] = tokens
    if reason != 'HEALTHY':
        return bad('FAIL', reason, **detail)
    return ok(**detail)
