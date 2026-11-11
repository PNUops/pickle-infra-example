"""The catalog: which host runs which check, and how each item is presented."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .checks import REGISTRY
from .result import ITEM_ID

HOST_NAME = re.compile(r'[a-z][a-z0-9-]{0,31}\Z')
# Catalog text reaches Discord embeds and a public page. Markdown and mention
# syntax is refused outright rather than escaped, so a label renders as written.
LABEL = re.compile(r'[^\n\r@<>`*_~|\\]{1,120}\Z')
GROUPS = {
    'user': '사용자 서비스',
    'internal': '내부 구성 요소',
    'backup': '백업',
    'nonprod': '운영 외 환경',
    'monitor': '감시 장치',
}
GROUP_ORDER = list(GROUPS)


class CatalogError(ValueError):
    pass


def _label(value, field, limit=120):
    if not isinstance(value, str) or not LABEL.fullmatch(value) or len(value) > limit:
        raise CatalogError(f'{field} must be plain text up to {limit} characters')
    return value


def load(directory):
    directory = Path(directory)
    hosts_doc = json.loads((directory / 'hosts.json').read_text(encoding='utf-8'))
    hosts = {}
    for name, host in hosts_doc.get('hosts', {}).items():
        if not HOST_NAME.fullmatch(name) or not isinstance(host, dict):
            raise CatalogError('host entry')
        if set(host) - {'label', 'campus', 'hostname'} or type(host.get('campus')) is not bool:
            raise CatalogError(f'host {name} fields')
        _label(host.get('label'), f'host {name} label', 40)
        if not isinstance(host.get('hostname'), str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,64}', host['hostname']):
            raise CatalogError(f'host {name} hostname')
        hosts[name] = dict(host)
    judge_host = hosts_doc.get('judge_host')
    if judge_host not in hosts or hosts[judge_host]['campus']:
        raise CatalogError('the judge must run on a host outside campus')
    items = {}
    for path in sorted((directory / 'items').glob('*.json')):
        for raw in json.loads(path.read_text(encoding='utf-8')):
            item = _item(raw, hosts)
            if item['id'] in items:
                raise CatalogError(f'duplicate item {item["id"]}')
            items[item['id']] = item
    if not items:
        raise CatalogError('the catalog has no items')
    return {'hosts': hosts, 'judge_host': judge_host, 'items': items}


def _item(raw, hosts):
    if not isinstance(raw, dict):
        raise CatalogError('item shape')
    allowed = {'id', 'host', 'check', 'every', 'fail_after', 'group', 'target', 'what',
               'impact', 'page_label', 'user_facing', 'args'}
    if set(raw) - allowed:
        raise CatalogError(f'unknown item fields {sorted(set(raw) - allowed)}')
    item_id = raw.get('id')
    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
        raise CatalogError('item id')
    if raw.get('host') not in hosts:
        raise CatalogError(f'{item_id}: unknown host')
    module = REGISTRY.get(raw.get('check'))
    if module is None:
        raise CatalogError(f'{item_id}: unknown check {raw.get("check")}')
    every = raw.get('every')
    if type(every) is not int or not 30 <= every <= 86400:
        raise CatalogError(f'{item_id}: every must be 30..86400 seconds')
    fail_after = raw.get('fail_after', 1)
    if type(fail_after) is not int or not 1 <= fail_after <= 5:
        raise CatalogError(f'{item_id}: fail_after must be 1..5')
    group = raw.get('group')
    if group not in GROUPS:
        raise CatalogError(f'{item_id}: unknown group')
    # user_facing=false is for items whose failure no user would notice (staging,
    # development, a test-only model server). An internal component such as the
    # database is still user-facing: its outage is felt even though users never
    # see it by name.
    user_facing = raw.get('user_facing', group not in ('nonprod', 'monitor'))
    if type(user_facing) is not bool:
        raise CatalogError(f'{item_id}: user_facing')
    args = raw.get('args', {})
    if not isinstance(args, dict):
        raise CatalogError(f'{item_id}: args')
    try:
        module.validate(args)
    except (ValueError, KeyError, TypeError) as error:
        raise CatalogError(f'{item_id}: {error}') from None
    return {
        'id': item_id, 'host': raw['host'], 'check': raw['check'], 'every': every,
        'fail_after': fail_after, 'group': group, 'user_facing': user_facing,
        'target': _label(raw.get('target'), f'{item_id} target', 80),
        'what': _label(raw.get('what'), f'{item_id} what'),
        'impact': _label(raw.get('impact'), f'{item_id} impact'),
        'page_label': _label(raw.get('page_label'), f'{item_id} page_label', 40),
        'args': args,
    }


def for_host(catalog, host):
    return [item for item in catalog['items'].values() if item['host'] == host]
