"""Argument and output helpers shared by the Proxmox-side checks (no KIND)."""
from __future__ import annotations

import re

from . import CheckError

UNIT = re.compile(r'[A-Za-z0-9@_.:-]{1,96}\.(service|socket|timer)\Z')


def ct_id(value, field='ct'):
    if type(value) is not int or not 100 <= value <= 999999999:
        raise ValueError(f'{field} must be a container id')
    return value


def ct_list(value, field='cts'):
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ValueError(f'{field} must be a non-empty list')
    for ct in value:
        ct_id(ct, field)
    if len(set(value)) != len(value):
        raise ValueError(f'{field} has duplicates')
    return value


def ct_label(ct):
    return f'CT{ct}'


def number(value, field, low, high, kinds=(int,)):
    if type(value) not in kinds or not low <= value <= high:
        raise ValueError(f'{field} must be {low}..{high}')
    return value


def as_int(text):
    """Parse our own command's integer output; anything else is an observation failure."""
    text = text.strip()
    if not re.fullmatch(r'-?\d{1,18}', text):
        raise CheckError('CHECK_CRASHED')
    return int(text)


def exit_code(ctx, ct, argv, timeout=12):
    """Run argv in a container and return its exit code instead of raising on nonzero.

    The wrapper itself must succeed; if it cannot (container gone, pct broken) the
    check could not observe, which stays a CheckError.
    """
    script = '"$@" >/dev/null 2>&1; echo "$?"'
    return as_int(ctx.pct(ct, ['sh', '-c', script, 'sh', *argv], timeout=timeout))
