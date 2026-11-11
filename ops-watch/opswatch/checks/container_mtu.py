"""Guest eth0 MTU agrees with its host veth and the net0 configuration.

PVE sets net0 mtu= only on the veth it creates; a guest without its if-pre-up
hook drops back to 1500 and large packets vanish silently.
"""
from __future__ import annotations

from . import CheckError, _pve, bad, ok

KIND = 'container_mtu'
DETAIL = {'mismatched': [str]}
REASONS = {
    'MTU_MISMATCH': ('다음 컨테이너의 네트워크 MTU가 설정과 달라요: {mismatched}', '네트워크 설정'),
}


def validate(args):
    _pve.ct_list(args['cts'])


def net0_mtu(config_text):
    """Return net0's mtu= value, or None when the veth inherits the bridge MTU."""
    lines = [line[len('net0: '):] for line in config_text.splitlines() if line.startswith('net0: ')]
    if len(lines) != 1:
        raise CheckError('CHECK_CRASHED')
    values = [part[len('mtu='):] for part in lines[0].split(',') if part.startswith('mtu=')]
    if len(values) > 1 or (values and not values[0].isdigit()):
        raise CheckError('CHECK_CRASHED')
    return int(values[0]) if values else None


def run(args, ctx):
    mismatched = []
    for ct in args['cts']:
        configured = net0_mtu(ctx.run(['pct', 'config', str(ct)]))
        veth = _pve.as_int(ctx.run(['cat', f'/sys/class/net/veth{ct}i0/mtu']))
        guest = _pve.as_int(ctx.pct(ct, ['cat', '/sys/class/net/eth0/mtu']))
        if guest != veth or configured not in (None, veth):
            mismatched.append(_pve.ct_label(ct))
    if mismatched:
        return bad('FAIL', 'MTU_MISMATCH', mismatched=mismatched)
    return ok()
