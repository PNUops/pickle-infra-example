"""A TCP connect from inside a container to an internal target.

The target address lives in the catalog arguments only; results never carry it.
"""
from __future__ import annotations

import re

from . import _pve, bad, ok

KIND = 'internal_link'
DETAIL = {'latency_ms': int}
REASONS = {
    'LINK_DOWN': ('운영 서버로 가는 내부 연결이 끊겼어요.', '내부 연결 끊김'),
}
ADDRESS = re.compile(r'(\d{1,3}(\.\d{1,3}){3}|[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?)\Z')
# bash's /dev/tcp needs no extra package in the guest; the outer `echo` keeps
# the exit status 0 so only a broken wrapper reads as an observation failure.
PROBE = ('s=$(date +%s%N); if timeout "$2" bash -c \'exec 3<>"/dev/tcp/$0/$1"\' "$0" "$1" 2>/dev/null; '
         'then echo "up $(( ($(date +%s%N) - s) / 1000000 ))"; else echo down; fi')


def validate(args):
    _pve.ct_id(args['ct'])
    if not isinstance(args['address'], str) or not ADDRESS.fullmatch(args['address']):
        raise ValueError('address must be an IPv4 address or host name')
    _pve.number(args['port'], 'port', 1, 65535)
    _pve.number(args.get('timeout', 5), 'timeout', 1, 20)


def run(args, ctx):
    timeout = args.get('timeout', 5)
    out = ctx.pct(args['ct'], ['bash', '-c', PROBE, args['address'], str(args['port']), str(timeout)],
                  timeout=timeout + 8)
    match = re.fullmatch(r'up (\d{1,9})', out)
    if match:
        return ok(latency_ms=int(match.group(1)))
    if out == 'down':
        return bad('FAIL', 'LINK_DOWN')
    return {'status': 'ERROR', 'reason': 'CHECK_CRASHED', 'detail': {}}
