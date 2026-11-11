"""A container is running and the services it exists for are active."""
from __future__ import annotations

from . import _pve, bad, ok

KIND = 'container'
DETAIL = {'failed_units': [str]}
REASONS = {
    'CONTAINER_STOPPED': ('컨테이너가 실행 중이 아니에요.', '서비스 중지'),
    'UNIT_INACTIVE': ('다음 서비스가 멈춰 있어요: {failed_units}', '서비스 중지'),
}
# A unit that is reloading still serves; activating means it is (re)starting,
# which is how a crash loop looks from outside, so it counts as down.
UP = ('active', 'reloading')


def validate(args):
    _pve.ct_id(args['ct'])
    units = args.get('units', [])
    if not isinstance(units, list) or len(units) > 8 or len(set(units)) != len(units):
        raise ValueError('units must be a list of up to 8 names')
    for unit in units:
        if not isinstance(unit, str) or not _pve.UNIT.fullmatch(unit):
            raise ValueError('units must be systemd unit names')


def run(args, ctx):
    ct = args['ct']
    if ctx.run(['pct', 'status', str(ct)]) != 'status: running':
        return bad('FAIL', 'CONTAINER_STOPPED')
    failed = []
    for unit in args.get('units', []):
        # `systemctl is-active` exits nonzero for an inactive unit, which the
        # runner would read as a failed command; `show` reports the state and exits 0.
        state = ctx.pct(ct, ['systemctl', 'show', '--property=ActiveState', '--value', unit])
        if state not in UP:
            failed.append(unit)
    if failed:
        return bad('FAIL', 'UNIT_INACTIVE', failed_units=failed)
    return ok()
