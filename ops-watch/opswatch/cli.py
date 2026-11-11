"""Command line: `ops-watch agent|ingest|judge|inspect|test-message|render-samples`."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

from . import agent, catalog as catalog_mod, ingest, judge, notify, render, statuspage
from .store import Store
from .util import OpsError, atomic_json, kst_day, read_json

UNDELIVERED_WINDOW = 1800


def run_judge(config, *, now=None, sender=notify.send, inspect=False):
    now = time.time() if now is None else now
    catalog = catalog_mod.load(config['catalog'])
    state_dir = Path(config['state_dir'])
    state_dir.mkdir(mode=0o700, exist_ok=True)
    lock = os.open(state_dir / 'judge.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        return {'mode': 'SKIPPED'}
    store = Store(state_dir / 'judge.sqlite')
    try:
        bundles = judge.load_inbox(state_dir, catalog['hosts'])
        present = {name: isinstance(b, dict) and now - b.get('received_at', 0) <= judge.HOST_TIMEOUT
                   for name, b in bundles.items()}
        if inspect:
            return {'mode': 'INSPECT', 'hosts': present,
                    'items': {item_id: {'host': item['host'],
                                        'result': ((bundles.get(item['host']) or {}).get('results') or {}).get(item_id)}
                              for item_id, item in catalog['items'].items()}}
        store.begin()
        try:
            events = judge.evaluate(catalog, bundles, store, now)
            embeds = render.embeds_for(catalog, events)
            sending = bool(config.get('send', True))
            # The first round that may send (a fresh install, or the switch out of
            # shadow mode) states what is already wrong; those problems were
            # recorded silently and would otherwise wait for the next 09:00.
            starting = sending and not store.meta('sending')
            if starting or (judge.summary_due(store, now) and store.meta('summary_day') is not None):
                embeds.append(render.summary_embed(catalog, judge.open_problems(catalog, store, present), now,
                                                   starting=starting))
            if judge.summary_due(store, now):
                store.set_meta('summary_day', kst_day(now))
            store.set_meta('sending', sending)
            if sending:
                for payload in render.payloads(embeds, tag=config.get('tag')):
                    store.enqueue(payload, now)
            store.prune(kst_day(now - 95 * 86400), now - 30 * 86400)
            store.commit()
        except BaseException:
            store.rollback()
            raise
        sent = 0
        if config.get('send', True):
            sent = notify.dispatch(store, notify.load_webhook(config['webhook_file']), now, sender=sender)
        # Liveness first: a failing page write must not make pve-node-3 report the
        # judge as stopped while alerts are still flowing.
        atomic_json(state_dir / 'judge-status.json',
                    {'last_run': now, 'undelivered': store.undelivered(now - UNDELIVERED_WINDOW)}, mode=0o640)
        page = statuspage.build(catalog, store, present, now)
        atomic_json(Path(config['www_dir']) / 'status.json', page, mode=0o644)
        return {'mode': 'RUN', 'events': len(events), 'sent': sent}
    finally:
        store.close()
        os.close(lock)


def test_message(config, host_label, *, sender=notify.send):
    payload = {'content': '🧪 연결 시험', 'embeds': [render.test_embed(host_label)], 'allowed_mentions': {'parse': []}}
    return sender(notify.load_webhook(config['webhook_file']), payload)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ops-watch')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('agent', 'judge', 'inspect', 'test-message'):
        sub.add_parser(name).add_argument('--config', required=True)
    receive = sub.add_parser('ingest')
    receive.add_argument('--host', required=True)
    receive.add_argument('--catalog', required=True)
    receive.add_argument('--state-dir', required=True)
    samples = sub.add_parser('render-samples')
    samples.add_argument('--catalog', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'ingest':
            return ingest.main(args.host, args.catalog, args.state_dir)
        if args.command == 'render-samples':
            from .samples import render_samples
            print(json.dumps(render_samples(catalog_mod.load(args.catalog)), ensure_ascii=False, indent=2))
            return 0
        config = read_json(args.config, private=True, require_root=False)
        if args.command == 'agent':
            result = agent.main(args.config)
        elif args.command == 'judge':
            result = run_judge(config)
        elif args.command == 'inspect':
            result = run_judge(config, inspect=True)
        else:
            result = test_message(config, config.get('host_label', '판정 서버'))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OpsError, OSError, ValueError, KeyError) as error:
        # The message types above never carry a secret; the class name helps triage.
        print(f'ops-watch {args.command} stopped: {type(error).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
