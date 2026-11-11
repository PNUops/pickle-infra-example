"""The single judge. Reads every host's newest bundle, decides transitions once.

Per item it keeps four things: the announced condition, the count of
consecutive non-OK new samples, when the condition began, and when the newest
sample arrived. There is no stabilisation window: one healthy sample recovers,
and the next failure alerts again.
"""
from __future__ import annotations

import json
from pathlib import Path

from .util import kst_day, kst_hour

# A host whose bundle has not arrived for this long is missing. The agents run
# every minute, so this is three missed pushes plus jitter.
HOST_TIMEOUT = 190
# A check whose newest sample is older than this many periods has stopped running.
ITEM_STALE_PERIODS = 3
ITEM_STALE_FLOOR = 240
# Accounting gaps longer than this (a stopped judge) are not attributed to anyone.
MAX_ACCOUNTING_GAP = 600
SUMMARY_HOUR = 9


def load_inbox(state_dir, hosts):
    bundles = {}
    for name in hosts:
        path = Path(state_dir) / 'inbox' / f'{name}.json'
        try:
            bundles[name] = json.loads(path.read_text())
        except (OSError, ValueError):
            bundles[name] = None
    return bundles


def evaluate(catalog, bundles, store, now):
    """Advance all state for one round and return the events to announce."""
    items = catalog['items']
    hosts = catalog['hosts']
    first_run = store.meta('initialized') is None
    events = []

    # 1. Hosts. A missing host freezes its items rather than failing them.
    present = {}
    for name in hosts:
        bundle = bundles.get(name)
        received = bundle.get('received_at') if isinstance(bundle, dict) else None
        present[name] = type(received) in (int, float) and now - received <= HOST_TIMEOUT
    host_events = _host_transitions(catalog, store, present, now, first_run)

    # 2. Items.
    current = store.items()
    for item_id, item in items.items():
        row = current.get(item_id)
        if row is None:
            row = {'id': item_id, 'state': 'OK', 'fails': 0, 'since': None, 'reason': None, 'detail': {},
                   'last_sample': None, 'seen_at': now, 'unobserved': None}
        if not present[item['host']]:
            store.save_item(row)
            continue
        result = (bundles[item['host']].get('results') or {}).get(item_id)
        events.extend(_advance(item, row, result, now, first_run, store))
        store.save_item(row)
    store.drop_items(set(items), now)

    # 3. Accounting for the status page.
    last = store.meta('last_run')
    if last is not None and 0 < now - last <= MAX_ACCOUNTING_GAP:
        day = kst_day(now)
        for item_id, item in items.items():
            row = store.item(item_id)
            if not present[item['host']] or row['unobserved']:
                bucket = 'unknown'
            else:
                bucket = {'OK': 'up', 'WARN': 'warn', 'FAIL': 'down'}[row['state']]
            store.add_daily(item_id, day, bucket, now - last)
    store.set_meta('last_run', now)
    store.set_meta('initialized', True)
    if first_run:
        return []
    return host_events + events


def _advance(item, row, result, now, first_run, store):
    events = []
    # Any change of sample time is a new sample. Comparing with ">" would trust
    # the host clock for ordering, and a clock stepped back after a campus cut
    # would then hide every result until it caught up again.
    new_sample = (isinstance(result, dict) and type(result.get('sample_at')) in (int, float)
                  and result['sample_at'] != row['last_sample'])
    if not new_sample:
        limit = max(ITEM_STALE_PERIODS * item['every'], ITEM_STALE_FLOOR)
        if row['unobserved'] is None and now - (row['seen_at'] or now) > limit:
            row['unobserved'] = 'RESULT_STALE'
            if not first_run:
                events.append(_event('UNOBSERVED', item, row, now, reason='RESULT_STALE'))
        return events
    row['last_sample'] = result['sample_at']
    row['seen_at'] = now
    status, reason, detail = result['status'], result['reason'], result.get('detail') or {}
    if status == 'ERROR':
        if row['unobserved'] != reason:
            announce = row['unobserved'] is None
            row['unobserved'] = reason
            if announce and not first_run:
                events.append(_event('UNOBSERVED', item, row, now, reason=reason, detail=detail))
        return events
    resumed = row['unobserved'] is not None
    row['unobserved'] = None
    if first_run:
        row.update(state=status, fails=0 if status == 'OK' else item['fail_after'],
                   since=None if status == 'OK' else now, reason=reason, detail=detail)
        if status != 'OK':
            store.open_incident(item['id'], status, now, reason)
        return events
    if status == 'OK':
        row['fails'] = 0
        if row['state'] != 'OK':
            events.append(_event('RECOVER', item, row, now, reason=reason, detail=detail, previous=row['state']))
            store.close_incident(item['id'], now)
            row.update(state='OK', since=None, reason=reason, detail=detail)
        elif resumed:
            events.append(_event('RESUMED', item, row, now))
        return events
    row['fails'] += 1
    if row['fails'] >= item['fail_after'] and row['state'] != status:
        previous = row['state']
        if previous == 'OK':
            row['since'] = now
        else:
            store.close_incident(item['id'], now)
        store.open_incident(item['id'], status, row['since'], reason)
        row.update(state=status, reason=reason, detail=detail)
        events.append(_event(status, item, row, now, reason=reason, detail=detail, previous=previous))
    elif row['state'] == status:
        # Same condition, possibly a different cause; keep the newest cause for
        # the status page and the daily summary without announcing it again.
        row.update(reason=reason, detail=detail)
        if resumed:
            events.append(_event('RESUMED', item, row, now))
    elif resumed:
        events.append(_event('RESUMED', item, row, now))
    return events


def _event(kind, item, row, now, reason=None, detail=None, previous=None):
    return {'kind': kind, 'item': item, 'reason': reason, 'detail': dict(detail or {}),
            'since': row.get('since'), 'at': now, 'previous': previous}


def _host_transitions(catalog, store, present, now, first_run):
    hosts = catalog['hosts']
    went, came = [], []
    for name in hosts:
        row = store.host(name) or {'state': 'UP', 'since': None}
        if not present[name] and row['state'] == 'UP':
            store.save_host(name, 'MISSING', now)
            went.append(name)
        elif present[name] and row['state'] == 'MISSING':
            came.append((name, row['since']))
            store.save_host(name, 'UP', None)
        elif store.host(name) is None:
            store.save_host(name, row['state'], row['since'])
    if first_run:
        return []
    campus = [name for name, host in hosts.items() if host['campus']]
    campus_down = bool(campus) and all(not present[name] for name in campus)
    events = []
    active = store.meta('campus_event')
    if campus_down and active is None and any(name in campus for name in went):
        external_user = [item for item in catalog['items'].values()
                         if item['group'] == 'user' and not hosts[item['host']]['campus']]
        failing = [item for item in external_user if (store.item(item['id']) or {}).get('state') == 'FAIL']
        kind = 'CAMPUS_DOWN' if failing else 'CAMPUS_UPLINK'
        since = min((store.host(name) or {}).get('since') or now for name in campus)
        store.set_meta('campus_event', {'kind': kind, 'since': since})
        events.append({'kind': kind, 'item': None, 'hosts': campus, 'failing': failing,
                       'since': since, 'at': now, 'reason': kind, 'detail': {}})
        went = [name for name in went if name not in campus]
    elif active is not None and campus_down:
        went = [name for name in went if name not in campus]
    if active is not None and not campus_down:
        store.set_meta('campus_event', None)
        events.append({'kind': 'CAMPUS_BACK', 'item': None, 'hosts': [name for name, _ in came if name in campus],
                       'since': active['since'], 'previous': active['kind'], 'at': now, 'reason': 'CAMPUS_BACK',
                       'detail': {}})
        came = [(name, since) for name, since in came if name not in campus]
        # A campus host that is still dark when the rest return was never
        # announced on its own; say so now instead of leaving it silent.
        went = went + [name for name in campus if not present[name] and name not in went]
    if went:
        events.append({'kind': 'HOST_MISSING', 'item': None, 'hosts': went, 'since': now, 'at': now,
                       'reason': 'HOST_MISSING', 'detail': {}})
    if came:
        events.append({'kind': 'HOST_BACK', 'item': None, 'hosts': [name for name, _ in came],
                       'since': min(since or now for _, since in came), 'at': now, 'reason': 'HOST_BACK',
                       'detail': {}})
    return events


def summary_due(store, now):
    """The 09:00 summary: once per KST day, at the first run at or after nine."""
    if kst_hour(now) < SUMMARY_HOUR:
        return False
    return store.meta('summary_day') != kst_day(now)


def open_problems(catalog, store, present):
    problems = []
    for item_id, item in catalog['items'].items():
        row = store.item(item_id)
        if row is None:
            continue
        if not present.get(item['host'], True):
            problems.append(('MISSING', item, row))
        elif row['state'] != 'OK':
            problems.append((row['state'], item, row))
        elif row['unobserved']:
            problems.append(('UNOBSERVED', item, row))
    return problems
