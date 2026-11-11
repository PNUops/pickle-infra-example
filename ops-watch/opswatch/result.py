"""The one result shape every check produces and the receiver accepts."""
from __future__ import annotations

import math
import re

from .checks import REGISTRY, STATUSES

REASON = re.compile(r'[A-Z][A-Z0-9_]{0,47}\Z')
ITEM_ID = re.compile(r'[a-z][a-z0-9-]{0,47}\Z')
# Detail strings are labels such as a unit name, a check name or a phase. They
# must never carry an address, so anything shaped like an IPv4 or IPv6 literal
# is refused even when the character set would allow it. A slash is not allowed
# either, so no detail can form a clickable URL in an embed.
SAFE_TEXT = re.compile(r'[A-Za-z0-9가-힣 _.:@+-]{0,80}\Z')
IPV4 = re.compile(r'\d{1,3}(\.\d{1,3}){3}')
IPV6 = re.compile(r'[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,}')
MAX_LIST = 16


class ResultError(ValueError):
    pass


def _value(kind, value):
    if kind is bool:
        if type(value) is not bool:
            raise ResultError('detail type')
        return value
    if kind is int:
        if type(value) is not int or abs(value) > 2**62:
            raise ResultError('detail type')
        return value
    if kind is float:
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e15:
            raise ResultError('detail type')
        return float(value)
    if kind is str:
        if not isinstance(value, str) or not SAFE_TEXT.fullmatch(value) or IPV4.search(value) or IPV6.search(value):
            raise ResultError('detail text')
        return value
    if kind == [str]:
        if not isinstance(value, list) or len(value) > MAX_LIST:
            raise ResultError('detail list')
        return [_value(str, item) for item in value]
    raise ResultError('detail schema')


def clean(result, check_kind, *, item_id=None, sample_at=None):
    """Return a validated copy of a result, or raise ResultError.

    The receiver calls this on everything a host sends, so a compromised or
    buggy agent can only deliver the fields its check type declares.
    """
    if not isinstance(result, dict):
        raise ResultError('result shape')
    module = REGISTRY.get(check_kind)
    if module is None:
        raise ResultError('unknown check')
    status, reason, detail = result.get('status'), result.get('reason'), result.get('detail', {})
    if status not in STATUSES or not isinstance(reason, str) or not REASON.fullmatch(reason):
        raise ResultError('status or reason')
    if not isinstance(detail, dict) or len(detail) > 16:
        raise ResultError('detail shape')
    allowed = module.DETAIL
    cleaned = {}
    for key, value in detail.items():
        if key not in allowed:
            raise ResultError('detail field')
        cleaned[key] = _value(allowed[key], value)
    out = {'status': status, 'reason': reason, 'detail': cleaned}
    item_id = result.get('id') if item_id is None else item_id
    sample_at = result.get('sample_at') if sample_at is None else sample_at
    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
        raise ResultError('item id')
    if type(sample_at) not in (int, float) or not math.isfinite(sample_at) or not 0 < sample_at < 4102444800:
        raise ResultError('sample time')
    out['id'] = item_id
    out['sample_at'] = float(sample_at)
    return out
