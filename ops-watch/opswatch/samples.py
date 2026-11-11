"""Representative messages rendered from synthetic events, for review before sending."""
from __future__ import annotations

from . import render

AT = 1794404160.0  # a fixed instant so previews are stable


def _first(catalog, group):
    for item in catalog['items'].values():
        if item['group'] == group:
            return item
    return next(iter(catalog['items'].values()))


def render_samples(catalog):
    user = _first(catalog, 'user')
    backup = _first(catalog, 'backup')
    nonprod = _first(catalog, 'nonprod')
    users = [item for item in catalog['items'].values() if item['group'] == 'user'][:3]
    campus = [name for name, host in catalog['hosts'].items() if host['campus']]
    events = {
        'fail': [{'kind': 'FAIL', 'item': user, 'reason': 'HTTP_STATUS', 'detail': {'http_status': 503},
                  'since': AT, 'at': AT, 'previous': 'OK'}],
        'warn_nonprod': [{'kind': 'WARN', 'item': nonprod, 'reason': 'CERT_EXPIRING', 'detail': {'days_left': 12},
                          'since': AT, 'at': AT, 'previous': 'OK'}],
        'grouped_fail': [{'kind': 'FAIL', 'item': item, 'reason': 'HTTP_CONNECT', 'detail': {'phase': 'TCP'},
                          'since': AT, 'at': AT, 'previous': 'OK'} for item in users],
        'recover': [{'kind': 'RECOVER', 'item': user, 'reason': 'HEALTHY', 'detail': {},
                     'since': AT - 660, 'at': AT, 'previous': 'FAIL'}],
        'unobserved': [{'kind': 'UNOBSERVED', 'item': backup, 'reason': 'RESULT_STALE', 'detail': {},
                        'since': None, 'at': AT, 'previous': None}],
        'host_missing': [{'kind': 'HOST_MISSING', 'item': None, 'hosts': campus[:1], 'since': AT, 'at': AT}],
        'campus_down': [{'kind': 'CAMPUS_DOWN', 'item': None, 'hosts': campus, 'failing': users,
                         'since': AT, 'at': AT}],
        'campus_back': [{'kind': 'CAMPUS_BACK', 'item': None, 'hosts': campus, 'previous': 'CAMPUS_DOWN',
                         'since': AT - 1800, 'at': AT}],
    }
    out = {name: render.payloads(render.embeds_for(catalog, value), tag='시험') for name, value in events.items()}
    out['summary'] = render.payloads([render.summary_embed(catalog, [], AT)], tag='시험')
    out['fallback'] = render.fallback_payload('unreachable', AT, recovered=False)
    return out
