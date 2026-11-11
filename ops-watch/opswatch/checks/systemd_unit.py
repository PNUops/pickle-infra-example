"""Whether named systemd services on this host are active."""
from __future__ import annotations

import re

from . import CheckError, bad, ok

KIND = 'systemd_unit'
DETAIL = {'failed_units': [str]}
REASONS = {
    'UNIT_INACTIVE': ('다음 서비스가 멈춰 있어요: {failed_units}', '서비스 중지'),
}
UNIT = re.compile(r'[A-Za-z0-9][A-Za-z0-9@._-]{0,70}\.service\Z')
# reloading is an active unit re-reading its configuration, not an outage.
RUNNING = {'active', 'reloading'}


def validate(args):
    units = args.get('units')
    if not isinstance(units, list) or not 1 <= len(units) <= 16 or len(set(units)) != len(units):
        raise ValueError('units must be 1..16 distinct names')
    for unit in units:
        if not isinstance(unit, str) or not UNIT.fullmatch(unit):
            raise ValueError('unit names must be plain *.service names')


def run(args, ctx):
    units = args['units']
    # systemctl is-active exits non-zero as soon as one unit is down, and a
    # non-zero exit is how ctx.run reports that the command itself failed. show
    # exits zero for any unit, a missing one included (ActiveState=inactive),
    # and prints the units in argument order.
    output = ctx.run(['systemctl', 'show', '--property=ActiveState', '--', *units])
    states = [line.split('=', 1)[1] for line in output.splitlines() if line.startswith('ActiveState=')]
    if len(states) != len(units):
        raise CheckError('COMMAND_FAILED')
    failed = [unit for unit, state in zip(units, states) if state not in RUNNING]
    if failed:
        return bad('FAIL', 'UNIT_INACTIVE', failed_units=failed)
    return ok()
