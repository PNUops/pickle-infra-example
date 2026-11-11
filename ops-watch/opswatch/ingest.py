"""Receiver on the judge host. Runs as an SSH forced command, one host per key.

The host name comes from the forced command, never from the bundle, so a key
can only ever write its own host's results, and only for items the catalog
assigns to that host.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

from . import catalog as catalog_mod
from .result import ResultError, clean
from .util import atomic_json

MAX_BUNDLE = 512 * 1024


class IngestError(ValueError):
    pass


def accept(data: bytes, host: str, catalog_dir, state_dir, now: float) -> dict:
    if len(data) > MAX_BUNDLE:
        raise IngestError('bundle too large')
    catalog = catalog_mod.load(catalog_dir)
    if host not in catalog['hosts']:
        raise IngestError('unknown host')
    try:
        bundle = json.loads(data)
    except ValueError:
        raise IngestError('bundle is not JSON') from None
    if (not isinstance(bundle, dict) or bundle.get('host') != host or bundle.get('version') != 1
            or not isinstance(bundle.get('results'), list)):
        raise IngestError('bundle shape')
    own = {item['id']: item for item in catalog_mod.for_host(catalog, host)}
    results = {}
    for raw in bundle['results']:
        item = own.get(raw.get('id')) if isinstance(raw, dict) else None
        if item is None:
            # Dropped, not refused: hosts update their catalog one at a time, and
            # refusing the whole bundle would make a host look gone mid-deploy.
            # Nothing is stored, so a key still cannot write another host's item.
            continue
        try:
            results[item['id']] = clean(raw, item['check'])
        except ResultError:
            raise IngestError('result failed validation') from None
    inbox = Path(state_dir) / 'inbox'
    inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    # received_at is the judge's own clock. Host freshness is decided on it,
    # because this host is also the campus hosts' time source and their clocks
    # can drift while campus is cut off.
    atomic_json(inbox / f'{host}.json', {'host': host, 'received_at': now, 'results': results})
    return response(state_dir, now)


def response(state_dir, now):
    path = Path(state_dir) / 'judge-status.json'
    try:
        status = json.loads(path.read_text())
        return {'judge_age': max(0.0, now - float(status['last_run'])),
                'undelivered': int(status.get('undelivered', 0))}
    except (OSError, ValueError, KeyError, TypeError):
        return {'judge_age': None, 'undelivered': 0}


def main(host, catalog_dir, state_dir):
    data = sys.stdin.buffer.read(MAX_BUNDLE + 1)
    try:
        reply = accept(data, host, catalog_dir, state_dir, time.time())
    except IngestError as error:
        print(json.dumps({'error': str(error)}))
        return 2
    print(json.dumps(reply))
    return 0
