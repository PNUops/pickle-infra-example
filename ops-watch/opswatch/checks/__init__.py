"""Check registry.

A check module exports:

    KIND     the name catalog items use in "check"
    DETAIL   {field: type} the only detail fields its results may carry;
             a type is int, float, bool, or str (a short identifier-free label)
    REASONS  {reason code: (sentence template, page category)}; the template is
             Korean, formats detail fields with str.format, and must stay free
             of host names and addresses because it reaches a public channel
    validate(args)        raise ValueError on a malformed catalog entry
    run(args, ctx) -> dict  {'status': OK|WARN|FAIL|ERROR, 'reason': code, 'detail': {...}}

A check judges nothing over time. It reports what it saw this once.
"""
from __future__ import annotations

import importlib
import pkgutil

STATUSES = ('OK', 'WARN', 'FAIL', 'ERROR')

# Reasons any check may return; the runner itself produces the first two.
COMMON_REASONS = {
    'CHECK_CRASHED': ('점검 스크립트가 오류로 끝났어요.', '점검 오류'),
    'CHECK_TIMEOUT': ('점검이 제한 시간 안에 끝나지 않았어요.', '점검 오류'),
    'HEALTHY': ('정상이에요.', '정상'),
    'COMMAND_FAILED': ('점검에 필요한 명령이 실패했어요.', '점검 오류'),
}


class CheckError(Exception):
    """Raised inside a check when the check itself could not observe."""

    def __init__(self, reason='CHECK_CRASHED'):
        super().__init__(reason)
        self.reason = reason


def ok(**detail):
    return {'status': 'OK', 'reason': 'HEALTHY', 'detail': detail}


def bad(status, reason, **detail):
    return {'status': status, 'reason': reason, 'detail': detail}


def _load():
    modules = {}
    for info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f'{__name__}.{info.name}')
        kind = getattr(module, 'KIND', None)
        if kind is None:
            continue
        if kind in modules:
            raise RuntimeError(f'duplicate check kind {kind}')
        modules[kind] = module
    return modules


REGISTRY = _load()


def reasons():
    table = dict(COMMON_REASONS)
    for module in REGISTRY.values():
        for code, value in module.REASONS.items():
            if code in table and table[code] != value:
                raise RuntimeError(f'reason {code} is defined twice with different text')
            table[code] = value
    return table
