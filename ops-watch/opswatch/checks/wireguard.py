"""WireGuard interface present and a peer handshake recent enough."""
from __future__ import annotations

import re

from . import CheckError, bad, ok

KIND = 'wireguard'
DETAIL = {'handshake_age_s': int}
REASONS = {
    'WG_STALE': ('터널 상대와 최근 연결 기록이 없어요.', '터널 끊김'),
    'WG_DOWN': ('터널 인터페이스가 내려가 있어요.', '터널 끊김'),
}
IFACE = re.compile(r'[a-z][a-z0-9_-]{0,14}\Z')


def validate(args):
    if not isinstance(args.get('iface'), str) or not IFACE.fullmatch(args['iface']):
        raise ValueError('iface')
    max_age = args.get('max_age', 180)
    if type(max_age) is not int or not 30 <= max_age <= 3600:
        raise ValueError('max_age must be 30..3600 seconds')
    if 'in_container' in args and (type(args['in_container']) is not int or not 100 <= args['in_container'] <= 999999999):
        raise ValueError('in_container must be a container id')


def run(args, ctx):
    iface = args['iface']
    ct = args.get('in_container')

    def wg(*argv):
        command = ['wg', 'show', *argv]
        return ctx.pct(ct, command) if ct is not None else ctx.run(command)

    # "wg show <iface>" exits non-zero for a missing interface, which ctx.run
    # cannot tell from a missing wg binary. The interface list can.
    if iface not in wg('interfaces').split():
        return bad('FAIL', 'WG_DOWN')
    stamps = []
    for line in wg(iface, 'latest-handshakes').splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise CheckError('COMMAND_FAILED')
        stamps.append(int(parts[1]))
    if not stamps:
        return bad('FAIL', 'WG_DOWN')
    newest = max(stamps)
    if newest == 0:
        # Zero means the peer has never completed a handshake.
        return bad('FAIL', 'WG_STALE')
    age = max(0, int(ctx.now - newest))
    if age > args.get('max_age', 180):
        return bad('FAIL', 'WG_STALE', handshake_age_s=age)
    return ok(handshake_age_s=age)
