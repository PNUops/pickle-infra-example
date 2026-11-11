"""Discord embeds. All wording lives here and in each check's REASONS table."""
from __future__ import annotations

import string

from .catalog import GROUPS
from .checks import reasons
from .util import duration, kst

RED, YELLOW, GREEN, GREY, BLUE = 0xE5484D, 0xF5A524, 0x30A46C, 0x8B8D98, 0x3E63DD
NO_CAUSE = '원인 정보를 받지 못했어요.'
NO_USER_IMPACT = '사용자 영향 없음. '
MAX_EMBEDS = 10
MAX_MESSAGE_CHARS = 5500

GENERIC = {
    'RESULT_STALE': ('점검 결과가 한동안 갱신되지 않았어요. 점검이 멈췄을 수 있어요.', '점검 멈춤'),
}

TITLES = {
    'FAIL': ('🔴 장애', RED),
    'WARN': ('🟡 주의', YELLOW),
    'RECOVER': ('🟢 복구', GREEN),
    'UNOBSERVED': ('⚙️ 감시 문제', GREY),
    'RESUMED': ('🟢 감시 재개', GREEN),
}


class _Safe(string.Formatter):
    def get_value(self, key, args, kwargs):
        value = kwargs[key]
        if isinstance(value, list):
            return ', '.join(str(part) for part in value)
        return value


def sentence(reason, detail):
    table = dict(GENERIC)
    table.update(reasons())
    template = table.get(reason, (None, None))[0]
    if template is None:
        return NO_CAUSE
    try:
        return _Safe().format(template, **(detail or {}))
    except (KeyError, IndexError, ValueError):
        return NO_CAUSE


def category(reason):
    table = dict(GENERIC)
    table.update(reasons())
    return table.get(reason, (None, '점검 실패'))[1]


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _field(name, value):
    return {'name': name, 'value': _clip(value, 1024), 'inline': False}


def _period(since, at):
    if not since:
        return kst(at, '%H:%M')
    return f'{kst(since)} ~ {kst(at, "%H:%M")} ({duration(at - since)})'


def item_embed(event):
    item = event['item']
    label, color = TITLES[event['kind']]
    kind = event['kind']
    prefix = '' if item['user_facing'] else NO_USER_IMPACT
    if kind == 'RECOVER':
        description = '다시 정상으로 확인됐어요.'
    elif kind == 'RESUMED':
        description = '점검 결과가 다시 들어오고 있어요.'
    elif kind == 'UNOBSERVED':
        description = sentence(event['reason'], event['detail']) + '\n장애로 확인된 것은 아니에요. 감시 쪽 문제예요.'
    else:
        description = sentence(event['reason'], event['detail'])
    fields = []
    if kind in ('FAIL', 'WARN'):
        if item['user_facing']:
            fields.append(_field('영향', item['impact']))
        if event.get('previous') in ('FAIL', 'WARN'):
            fields.append(_field('변화', '장애에서 주의로 바뀌었어요.' if kind == 'WARN' else '주의에서 장애로 바뀌었어요.'))
        fields.append(_field('시작', kst(event['since'] or event['at'])))
    if kind == 'RECOVER':
        fields.append(_field('기간', _period(event['since'], event['at'])))
    fields.append(_field('점검', item['what']))
    return {'title': _clip(f'{label} · {item["target"]}', 256), 'description': _clip(prefix + description, 1500),
            'color': color, 'fields': fields}


def grouped_embed(kind, group, events):
    label, color = TITLES[kind]
    lines = []
    for event in events:
        item = event['item']
        cause = '다시 정상이에요.' if kind == 'RECOVER' else sentence(event['reason'], event['detail'])
        lines.append(f'• {item["target"]}: {cause}')
    impacts = [] if kind == 'RECOVER' else sorted({e['item']['impact'] for e in events if e['item']['user_facing']})
    fields = [_field('영향', '\n'.join(impacts))] if impacts else []
    since = min((e['since'] or e['at']) for e in events)
    fields.append(_field('기간' if kind == 'RECOVER' else '시작',
                         _period(since, events[0]['at']) if kind == 'RECOVER' else kst(since)))
    return {'title': _clip(f'{label} · {GROUPS[group]} {len(events)}개', 256),
            'description': _clip('\n'.join(lines), 3000), 'color': color, 'fields': fields}


def _host_labels(catalog, names):
    return ', '.join(catalog['hosts'][name]['label'] for name in names)


def _unseen(catalog, names):
    """Items that went dark, user-facing ones first, the rest counted per group."""
    items = [item for item in catalog['items'].values() if item['host'] in names]
    targets = []
    for item in items:
        if item['user_facing'] and item['page_label'] not in targets:
            targets.append(item['page_label'])
    lines = [f'• {target}' for target in targets[:12]]
    if len(targets) > 12:
        lines.append(f'외 {len(targets) - 12}개')
    quiet = sum(1 for item in items if not item['user_facing'])
    if quiet:
        lines.append(f'사용자 영향 없는 항목 {quiet}개')
    return '\n'.join(lines) or '없음'


def host_embed(catalog, event):
    kind = event['kind']
    names = event.get('hosts') or []
    if kind == 'CAMPUS_DOWN':
        failing = '\n'.join(f'• {item["target"]}' for item in event.get('failing', [])) or '없음'
        return {'title': '🔴 장애 · 교내 연결 끊김', 'color': RED,
                'description': '교내 서버의 감시 결과가 모두 끊겼고, 바깥에서도 서비스에 접속되지 않아요. '
                               '교내망, 정전, 교내 NAT 장애일 수 있어요.',
                'fields': [_field('바깥에서 접속 불가', failing), _field('시작', kst(event['since'])),
                           _field('확인 못 한 항목', _unseen(catalog, names))]}
    if kind == 'CAMPUS_UPLINK':
        return {'title': '⚙️ 감시 문제 · 교내 감시 결과 미도착', 'color': GREY,
                'description': '교내 서버의 감시 결과가 모두 오지 않아요. 서비스는 바깥에서 정상이에요. '
                               '교내에서 AWS로 나가는 경로가 막혔을 수 있어요.',
                'fields': [_field('시작', kst(event['since'])), _field('확인 못 한 항목', _unseen(catalog, names))]}
    if kind == 'CAMPUS_BACK':
        title = '🟢 복구 · 교내 연결' if event.get('previous') == 'CAMPUS_DOWN' else '🟢 감시 재개 · 교내 감시 결과'
        return {'title': title, 'color': GREEN,
                'description': '교내 감시 결과가 다시 도착해요. 끊긴 동안 상태가 바뀐 항목은 따로 알려요.',
                'fields': [_field('기간', _period(event['since'], event['at']))]}
    if kind == 'HOST_MISSING':
        return {'title': _clip(f'⚙️ 감시 문제 · {_host_labels(catalog, names)} 감시 결과 미도착', 256), 'color': GREY,
                'description': '3분 넘게 감시 결과가 오지 않아요. 서버가 멈췄거나 AWS로 가는 연결이 끊겼을 수 있어요.',
                'fields': [_field('확인 못 한 항목', _unseen(catalog, names))]}
    return {'title': _clip(f'🟢 감시 재개 · {_host_labels(catalog, names)}', 256), 'color': GREEN,
            'description': '감시 결과가 다시 도착해요. 끊긴 동안 상태가 바뀐 항목은 따로 알려요.',
            'fields': [_field('기간', _period(event['since'], event['at']))]}


def embeds_for(catalog, events):
    """One embed per event, except that same-kind failures in one group merge."""
    out = []
    buckets = {}
    for event in events:
        if event['item'] is None:
            out.append(host_embed(catalog, event))
            continue
        if event['kind'] in ('FAIL', 'WARN', 'RECOVER'):
            buckets.setdefault((event['kind'], event['item']['group']), []).append(event)
        else:
            out.append(item_embed(event))
    for (kind, group), members in buckets.items():
        out.append(grouped_embed(kind, group, members) if len(members) > 1 else item_embed(members[0]))
    order = {RED: 0, YELLOW: 1, GREY: 2, GREEN: 3}
    out.sort(key=lambda embed: order.get(embed['color'], 4))
    return out


def _size(embed):
    return (len(embed['title']) + len(embed.get('description', ''))
            + sum(len(f['name']) + len(f['value']) for f in embed.get('fields', [])))


def payloads(embeds, tag=None):
    """Split embeds into Discord messages within the per-message limits."""
    messages, current, size = [], [], 0
    for embed in embeds:
        if tag:
            embed = dict(embed, title=_clip(f'[{tag}] {embed["title"]}', 256))
        if current and (len(current) == MAX_EMBEDS or size + _size(embed) > MAX_MESSAGE_CHARS):
            messages.append(current)
            current, size = [], 0
        current.append(embed)
        size += _size(embed)
    if current:
        messages.append(current)
    out = []
    for group in messages:
        content = group[0]['title'] + (f' 외 {len(group) - 1}건' if len(group) > 1 else '')
        out.append({'content': _clip(content, 200), 'embeds': group, 'allowed_mentions': {'parse': []}})
    return out


def summary_embed(catalog, problems, now, starting=False):
    if starting:
        head = '📋 알림 시작'
        note = '지금부터 상태가 바뀔 때마다 알려요.'
        if not problems:
            return {'title': head, 'color': BLUE, 'description': f'모든 감시 항목이 정상이에요. {note}'}
        title = f'{head} · 진행 중 {len(problems)}건'
    else:
        title = f'📋 일일 요약 · {kst(now, "%m-%d")} · 진행 중 {len(problems)}건'
    if not problems:
        return {'title': f'📋 일일 요약 · {kst(now, "%m-%d")}', 'color': BLUE,
                'description': '모든 감시 항목이 정상이에요. 이 요약이 오지 않는 날은 알림 장치를 확인해 주세요.'}
    words = {'FAIL': '장애', 'WARN': '주의', 'UNOBSERVED': '감시 문제', 'MISSING': '결과 미도착'}
    lines = []
    for kind, item, row in sorted(problems, key=lambda p: ['FAIL', 'WARN', 'MISSING', 'UNOBSERVED'].index(p[0])):
        since = f' ({duration(now - row["since"])}째)' if row.get('since') and kind in ('FAIL', 'WARN') else ''
        lines.append(f'• {words[kind]}: {item["target"]}{since}')
    return {'title': title, 'color': BLUE,
            'description': _clip('\n'.join(lines), 3500)}


def test_embed(host_label):
    return {'title': '🧪 연결 시험', 'color': BLUE,
            'description': f'{host_label}에서 보낸 시험 메시지예요. 실제 장애가 아니에요.'}


def fallback_payload(problem, since, *, recovered):
    if recovered:
        embed = {'title': '🟢 감시 재개 · AWS 판정 서버', 'color': GREEN,
                 'description': 'pve-node-3에서 판정 서버에 다시 닿아요. 이후 알림은 판정 서버가 보내요.'}
    else:
        text = {
            'unreachable': 'pve-node-3에서 AWS 판정 서버로 결과를 보내지 못하고 있어요. '
                           'AWS가 멈췄다면 사용자 SSH 접속(ssh.pcl.kr)도 끊겼을 수 있어요.',
            'stopped': 'AWS 판정 서버가 결과를 받지만 판정이 멈춰 있어요. 그동안 장애 알림이 오지 않아요.',
            'undelivered': 'AWS 판정 서버가 Discord로 알림을 보내지 못하고 있어요.',
        }[problem]
        embed = {'title': '⚙️ 감시 문제 · AWS 판정 서버', 'color': GREY,
                 'description': text + '\n이 메시지는 pve-node-3가 직접 보냈어요.',
                 'fields': [_field('시작', kst(since))]}
    return {'content': embed['title'], 'embeds': [embed], 'allowed_mentions': {'parse': []}}
