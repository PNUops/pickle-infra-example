"""status.json for the public page: identifier-free labels and categories only."""
from __future__ import annotations

from datetime import datetime, timedelta

from .catalog import GROUP_ORDER, GROUPS
from .render import category
from .util import KST

DAYS = 90


def build(catalog, store, present, now):
    today = datetime.fromtimestamp(now, KST).date()
    first = (today - timedelta(days=DAYS - 1)).isoformat()
    history = {}
    for row in store.daily(first):
        history.setdefault(row['item'], {})[row['day']] = row
    groups = []
    for group in GROUP_ORDER:
        entries = []
        for item_id, item in catalog['items'].items():
            if item['group'] != group:
                continue
            row = store.item(item_id) or {}
            if not present.get(item['host'], False):
                state, note = 'UNKNOWN', '결과 미도착'
            elif row.get('unobserved'):
                state, note = 'UNKNOWN', '감시 문제'
            else:
                state = row.get('state', 'OK')
                note = None if state == 'OK' else category(row.get('reason'))
            days = []
            for offset in range(DAYS):
                day = (today - timedelta(days=DAYS - 1 - offset)).isoformat()
                cell = history.get(item_id, {}).get(day)
                if not cell:
                    days.append(None)
                    continue
                observed = cell['up'] + cell['warn'] + cell['down']
                days.append({'up': round(cell['up'] / observed, 4) if observed else None,
                             'down_minutes': round(cell['down'] / 60), 'warn_minutes': round(cell['warn'] / 60)})
            entries.append({'label': item['page_label'], 'state': state, 'note': note,
                            'since': row.get('since') if state in ('FAIL', 'WARN') else None,
                            'user_facing': item['user_facing'], 'days': days})
        if entries:
            groups.append({'name': GROUPS[group], 'items': entries})
    labels = {item_id: item['page_label'] for item_id, item in catalog['items'].items()}
    incidents = [{'label': labels.get(row['item'], ''), 'kind': row['kind'], 'started': row['started'],
                  'ended': row['ended'], 'category': category(row['reason'])}
                 for row in store.recent_incidents(now - 14 * 86400) if row['item'] in labels]
    campus = [name for name, host in catalog['hosts'].items() if host['campus']]
    return {'generated_at': now, 'days': DAYS, 'first_day': first,
            'campus_results': all(present.get(name, False) for name in campus),
            'groups': groups, 'incidents': incidents}
