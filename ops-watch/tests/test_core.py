#!/usr/bin/env python3
"""Judge, receiver, rendering and delivery tests for ops-watch."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opswatch import catalog as catalog_mod, ingest, judge, notify, render  # noqa: E402
from opswatch.store import Store  # noqa: E402

T0 = 1794402000.0  # 2026-11-11 22:00 KST


def write_catalog(root, items=None):
    root = Path(root)
    (root / 'items').mkdir(parents=True, exist_ok=True)
    (root / 'hosts.json').write_text(json.dumps({
        'judge_host': 'aws',
        'hosts': {
            'aws': {'label': 'AWS 감시 서버', 'campus': False, 'hostname': 'aws-box'},
            'site2': {'label': 'site2 운영 서버', 'campus': True, 'hostname': 'site2'},
            'site3': {'label': 'site3 감시 서버', 'campus': True, 'hostname': 'site3'},
        }}))
    base = {'check': 'http', 'every': 60, 'what': '응답 확인', 'impact': '사용자가 접속하지 못해요.',
            'args': {'url': 'https://console.example.org/'}}
    items = items or [
        dict(base, id='console', host='aws', group='user', target='운영 콘솔 (console.example.org)',
             page_label='운영 콘솔'),
        dict(base, id='api', host='aws', group='user', target='운영 API (console.example.org)',
             page_label='운영 API', fail_after=2),
        dict(base, id='db', host='site2', group='internal', target='운영 DB', page_label='데이터베이스'),
        dict(base, id='backup', host='site3', group='backup', target='코어 백업', page_label='코어 백업', every=300),
        dict(base, id='staging', host='aws', group='nonprod', target='스테이징', page_label='스테이징',
             user_facing=False),
    ]
    (root / 'items' / 'all.json').write_text(json.dumps(items, ensure_ascii=False))
    return catalog_mod.load(root)


def bundle(host, now, results):
    return {'host': host, 'received_at': now,
            'results': {item_id: {'id': item_id, 'status': status, 'reason': reason, 'detail': detail,
                                  'sample_at': now} for item_id, (status, reason, detail) in results.items()}}


OK = ('OK', 'HEALTHY', {})
DOWN = ('FAIL', 'HTTP_STATUS', {'http_status': 503})


class Harness:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.catalog = write_catalog(Path(self.tmp.name) / 'catalog')
        self.store = Store(Path(self.tmp.name) / 'judge.sqlite')
        test.addCleanup(self.store.close)
        self.now = T0

    def round(self, aws=None, site2=None, site3=None, advance=60, present=('aws', 'site2', 'site3')):
        self.now += advance
        defaults = {'aws': {'console': OK, 'api': OK, 'staging': OK}, 'site2': {'db': OK}, 'site3': {'backup': OK}}
        given = {'aws': aws, 'site2': site2, 'site3': site3}
        bundles = {}
        for host in ('aws', 'site2', 'site3'):
            results = dict(defaults[host])
            results.update(given[host] or {})
            bundles[host] = bundle(host, self.now if host in present else self.now - 600, results)
        self.store.begin()
        events = judge.evaluate(self.catalog, bundles, self.store, self.now)
        self.store.commit()
        return events


def kinds(events):
    return [(event['kind'], (event['item'] or {}).get('id') or tuple(event.get('hosts', []))) for event in events]


class JudgeTests(unittest.TestCase):
    def test_first_run_is_silent_even_when_failing(self):
        h = Harness(self)
        self.assertEqual(h.round(aws={'console': DOWN}), [])
        self.assertEqual(h.store.item('console')['state'], 'FAIL')
        self.assertEqual(kinds(h.round(aws={'console': OK})), [('RECOVER', 'console')])

    def test_failure_and_recovery_are_immediate(self):
        h = Harness(self)
        h.round()
        self.assertEqual(kinds(h.round(aws={'console': DOWN})), [('FAIL', 'console')])
        self.assertEqual(h.round(aws={'console': DOWN}), [])
        events = h.round(aws={'console': OK})
        self.assertEqual(kinds(events), [('RECOVER', 'console')])
        self.assertEqual(events[0]['since'], T0 + 120)
        self.assertEqual(kinds(h.round(aws={'console': DOWN})), [('FAIL', 'console')])

    def test_fail_after_counts_new_samples_only(self):
        h = Harness(self)
        h.round()
        self.assertEqual(h.round(aws={'api': DOWN}), [])
        self.assertEqual(kinds(h.round(aws={'api': DOWN})), [('FAIL', 'api')])

    def test_repeated_sample_is_not_counted_twice(self):
        h = Harness(self)
        h.round()
        h.round(aws={'api': DOWN})
        # The same bundle read again (no new sample) must not reach fail_after.
        bundles = {name: bundle(name, h.now, {}) for name in ('aws', 'site2', 'site3')}
        bundles['aws']['results'] = bundle('aws', h.now - 0, {'api': DOWN, 'console': OK, 'staging': OK})['results']
        for result in bundles['aws']['results'].values():
            result['sample_at'] = h.now
        bundles['site2'] = bundle('site2', h.now, {'db': OK})
        bundles['site3'] = bundle('site3', h.now, {'backup': OK})
        for result in list(bundles['site2']['results'].values()) + list(bundles['site3']['results'].values()):
            result['sample_at'] = h.now - 60
        h.store.begin()
        events = judge.evaluate(h.catalog, bundles, h.store, h.now + 30)
        h.store.commit()
        self.assertEqual(events, [])
        self.assertEqual(h.store.item('api')['fails'], 1)

    def test_warn_then_fail_reports_the_change(self):
        h = Harness(self)
        h.round()
        warn = ('WARN', 'HTTP_STATUS', {'http_status': 429})
        self.assertEqual(kinds(h.round(aws={'console': warn})), [('WARN', 'console')])
        events = h.round(aws={'console': DOWN})
        self.assertEqual(kinds(events), [('FAIL', 'console')])
        self.assertEqual(events[0]['previous'], 'WARN')
        self.assertEqual(events[0]['since'], T0 + 120)

    def test_check_error_is_a_monitoring_problem_not_a_failure(self):
        h = Harness(self)
        h.round()
        events = h.round(site2={'db': ('ERROR', 'COMMAND_FAILED', {})})
        self.assertEqual(kinds(events), [('UNOBSERVED', 'db')])
        self.assertEqual(h.store.item('db')['state'], 'OK')
        self.assertEqual(h.round(site2={'db': ('ERROR', 'COMMAND_FAILED', {})}), [])
        self.assertEqual(kinds(h.round()), [('RESUMED', 'db')])

    def test_error_does_not_close_an_open_failure(self):
        h = Harness(self)
        h.round()
        h.round(site2={'db': DOWN})
        h.round(site2={'db': ('ERROR', 'COMMAND_FAILED', {})})
        self.assertEqual(h.store.item('db')['state'], 'FAIL')
        self.assertEqual(kinds(h.round(site2={'db': OK})), [('RECOVER', 'db')])

    def test_stale_item_on_a_live_host(self):
        h = Harness(self)
        h.round()
        for _ in range(5):
            # site2 keeps pushing, but the db sample never changes.
            h.now += 60
            bundles = {'aws': bundle('aws', h.now, {'console': OK, 'api': OK, 'staging': OK}),
                       'site3': bundle('site3', h.now, {'backup': OK}),
                       'site2': bundle('site2', h.now, {'db': OK})}
            bundles['site2']['results']['db']['sample_at'] = T0 + 60
            h.store.begin()
            events = judge.evaluate(h.catalog, bundles, h.store, h.now)
            h.store.commit()
            if events:
                break
        self.assertEqual(kinds(events), [('UNOBSERVED', 'db')])
        self.assertEqual(events[0]['reason'], 'RESULT_STALE')

    def test_one_missing_host(self):
        h = Harness(self)
        h.round()
        self.assertEqual(kinds(h.round(present=('aws', 'site3'))), [('HOST_MISSING', ('site2',))])
        # Its items are frozen, not failed, while it is gone.
        self.assertEqual(h.round(present=('aws', 'site3'), site2={'db': DOWN}), [])
        self.assertEqual(kinds(h.round()), [('HOST_BACK', ('site2',))])

    def test_campus_outage_is_one_red_event(self):
        h = Harness(self)
        h.round()
        h.round(aws={'console': DOWN, 'api': DOWN})
        h.round(aws={'console': DOWN, 'api': DOWN})
        events = h.round(aws={'console': DOWN, 'api': DOWN}, present=('aws',))
        self.assertEqual([event['kind'] for event in events], ['CAMPUS_DOWN'])
        self.assertEqual(sorted(i['id'] for i in events[0]['failing']), ['api', 'console'])
        self.assertEqual(h.round(aws={'console': DOWN, 'api': DOWN}, present=('aws',)), [])
        back = h.round()
        self.assertEqual([event['kind'] for event in back][0], 'CAMPUS_BACK')
        self.assertIn(('RECOVER', 'console'), kinds(back))

    def test_campus_uplink_only(self):
        h = Harness(self)
        h.round()
        events = h.round(present=('aws',))
        self.assertEqual([event['kind'] for event in events], ['CAMPUS_UPLINK'])

    def test_after_campus_returns_only_changed_items_are_reported(self):
        h = Harness(self)
        h.round()
        h.round(present=('aws',))
        events = h.round(site2={'db': DOWN})
        self.assertEqual([event['kind'] for event in events], ['CAMPUS_BACK', 'FAIL'])

    def test_host_still_dark_when_campus_returns_is_announced(self):
        h = Harness(self)
        h.round()
        h.round(present=('aws',))
        events = h.round(present=('aws', 'site3'))
        self.assertEqual([e['kind'] for e in events], ['CAMPUS_BACK', 'HOST_MISSING'])
        self.assertEqual(events[1]['hosts'], ['site2'])

    def test_host_clock_stepping_back_does_not_hide_results(self):
        h = Harness(self)
        h.round()
        h.now += 60
        bundles = {'aws': bundle('aws', h.now, {'console': OK, 'api': OK, 'staging': OK}),
                   'site3': bundle('site3', h.now, {'backup': OK}),
                   'site2': bundle('site2', h.now, {'db': DOWN})}
        bundles['site2']['results']['db']['sample_at'] = h.now - 3600  # host clock stepped back an hour
        h.store.begin()
        events = judge.evaluate(h.catalog, bundles, h.store, h.now)
        h.store.commit()
        self.assertEqual(kinds(events), [('FAIL', 'db')])

    def test_removed_item_closes_its_incident(self):
        h = Harness(self)
        h.round()
        h.round(site2={'db': DOWN})
        h.catalog['items'].pop('db')
        h.round()
        self.assertEqual([i for i in h.store.recent_incidents(0) if i['ended'] is None], [])

    def test_daily_accounting(self):
        h = Harness(self)
        h.round()
        h.round(aws={'console': DOWN})
        h.round(aws={'console': DOWN})
        rows = {row['item']: row for row in h.store.daily('2026-01-01')}
        self.assertEqual(rows['console']['down'], 120)
        self.assertEqual(rows['db']['up'], 120)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog_dir = Path(self.tmp.name) / 'catalog'
        write_catalog(self.catalog_dir)
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()

    def send(self, host, results):
        data = json.dumps({'host': host, 'version': 1, 'results': results}).encode()
        return ingest.accept(data, host, self.catalog_dir, self.state, T0)

    def result(self, item_id, **extra):
        value = {'id': item_id, 'status': 'OK', 'reason': 'HEALTHY', 'detail': {}, 'sample_at': T0}
        value.update(extra)
        return value

    def test_accepts_own_items(self):
        self.send('site2', [self.result('db')])
        stored = json.loads((self.state / 'inbox' / 'site2.json').read_text())
        self.assertEqual(stored['received_at'], T0)
        self.assertIn('db', stored['results'])

    def test_another_hosts_item_is_dropped_not_stored(self):
        self.send('site2', [self.result('console'), self.result('db')])
        stored = json.loads((self.state / 'inbox' / 'site2.json').read_text())
        self.assertEqual(sorted(stored['results']), ['db'])

    def test_refuses_undeclared_detail_and_addresses(self):
        with self.assertRaises(ingest.IngestError):
            self.send('site2', [self.result('db', detail={'body': 'x'})])
        with self.assertRaises(ingest.IngestError):
            self.send('site2', [self.result('db', detail={'phase': '192.0.2.1'})])
        with self.assertRaises(ingest.IngestError):
            self.send('site2', [self.result('db', reason='lower case')])

    def test_host_comes_from_the_forced_command(self):
        data = json.dumps({'host': 'site3', 'version': 1, 'results': []}).encode()
        with self.assertRaises(ingest.IngestError):
            ingest.accept(data, 'site2', self.catalog_dir, self.state, T0)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = write_catalog(Path(self.tmp.name) / 'c')

    def event(self, kind, item_id, reason='HTTP_STATUS', detail=None, **extra):
        value = {'kind': kind, 'item': self.catalog['items'][item_id], 'reason': reason,
                 'detail': detail if detail is not None else {'http_status': 503}, 'since': T0, 'at': T0 + 660,
                 'previous': 'OK'}
        value.update(extra)
        return value

    def test_failure_embed_names_target_cause_and_impact(self):
        embed = render.embeds_for(self.catalog, [self.event('FAIL', 'console')])[0]
        self.assertEqual(embed['title'], '🔴 장애 · 운영 콘솔 (console.example.org)')
        self.assertEqual(embed['description'], 'HTTP 503 응답이 왔어요.')
        self.assertEqual(embed['color'], render.RED)
        self.assertIn('사용자가 접속하지 못해요.', [f['value'] for f in embed['fields']])

    def test_recovery_carries_the_period(self):
        embed = render.embeds_for(self.catalog, [self.event('RECOVER', 'console')])[0]
        period = [f['value'] for f in embed['fields'] if f['name'] == '기간'][0]
        self.assertEqual(period, '11-11 22:00 ~ 22:11 (11분)')

    def test_nonprod_says_no_user_impact(self):
        embed = render.embeds_for(self.catalog, [self.event('FAIL', 'staging')])[0]
        self.assertTrue(embed['description'].startswith('사용자 영향 없음.'))

    def test_same_group_failures_merge(self):
        embeds = render.embeds_for(self.catalog, [self.event('FAIL', 'console'), self.event('FAIL', 'api')])
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0]['title'], '🔴 장애 · 사용자 서비스 2개')

    def test_unknown_reason_and_missing_detail_fall_back(self):
        self.assertEqual(render.sentence('NOT_A_CODE', {}), render.NO_CAUSE)
        self.assertEqual(render.sentence('HTTP_STATUS', {}), render.NO_CAUSE)

    def test_payload_split_and_mentions_blocked(self):
        embeds = [render.test_embed('x') for _ in range(23)]
        payloads = render.payloads(embeds)
        self.assertEqual([len(p['embeds']) for p in payloads], [10, 10, 3])
        self.assertTrue(all(p['allowed_mentions'] == {'parse': []} for p in payloads))

    def test_catalog_refuses_markdown_and_mentions(self):
        root = Path(self.tmp.name) / 'bad'
        base = {'id': 'x', 'host': 'aws', 'check': 'http', 'every': 60, 'group': 'user', 'what': 'w',
                'impact': 'i', 'page_label': 'p', 'args': {'url': 'https://a.example.org/'}}
        for target in ('@everyone', '<@123>', 'a`b', 'two\nlines'):
            with self.assertRaises(catalog_mod.CatalogError):
                write_catalog(root, [dict(base, target=target)])


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 's.sqlite')
        self.addCleanup(self.store.close)
        self.hook = 'https://discord.com/api/webhooks/1234567890123456789/' + 'a' * 68

    def test_uncertain_is_never_resent(self):
        self.store.enqueue({'content': 'x', 'embeds': []}, T0)
        calls = []
        notify.dispatch(self.store, self.hook, T0, sender=lambda w, p: calls.append(p) or {'delivery': 'UNCERTAIN', 'code': 'RESPONSE_UNKNOWN'})
        notify.dispatch(self.store, self.hook, T0 + 600, sender=lambda w, p: calls.append(p) or {'delivery': 'SENT'})
        self.assertEqual(len(calls), 1)

    def test_rate_limit_pauses_the_webhook(self):
        self.store.enqueue({'content': 'a'}, T0)
        self.store.enqueue({'content': 'b'}, T0)
        calls = []

        def limited(webhook, payload):
            calls.append(payload['content'])
            return {'delivery': 'PENDING', 'code': 'RATE_LIMITED', 'retry_after': 30}

        notify.dispatch(self.store, self.hook, T0, sender=limited)
        notify.dispatch(self.store, self.hook, T0 + 10, sender=limited)
        self.assertEqual(calls, ['a'])
        sent = []
        notify.dispatch(self.store, self.hook, T0 + 31, sender=lambda w, p: sent.append(p['content']) or {'delivery': 'SENT'})
        self.assertEqual(sent, ['a', 'b'])

    def test_connection_never_opened_is_retried(self):
        import socket
        import urllib.error

        class Refusing:
            def open(self, request, timeout):
                raise urllib.error.URLError(ConnectionRefusedError())

        self.assertEqual(notify.send(self.hook, {'content': 'a'}, opener=Refusing())['delivery'], 'PENDING')
        self.store.enqueue({'content': 'a'}, T0)
        outcomes = iter([{'delivery': 'PENDING', 'code': 'NOT_CONNECTED'}, {'delivery': 'SENT'}])
        notify.dispatch(self.store, self.hook, T0, sender=lambda w, p: next(outcomes))
        notify.dispatch(self.store, self.hook, T0 + 61, sender=lambda w, p: next(outcomes))
        self.assertEqual(len(self.store.outbox(['SENT'])), 1)

        class Timeout:
            def open(self, request, timeout):
                raise urllib.error.URLError(socket.timeout())

        self.assertEqual(notify.send(self.hook, {'content': 'a'}, opener=Timeout())['delivery'], 'UNCERTAIN')

    def test_interrupted_attempt_becomes_uncertain(self):
        self.store.enqueue({'content': 'a'}, T0)
        row = self.store.outbox(['PENDING'])[0]
        self.store.update_outbox(row['id'], delivery='ATTEMPTED', attempts=1)
        notify.dispatch(self.store, self.hook, T0, sender=lambda w, p: {'delivery': 'SENT'})
        self.assertEqual(self.store.outbox(['UNCERTAIN'])[0]['code'], 'INTERRUPTED')


if __name__ == '__main__':
    os.environ.setdefault('TZ', 'UTC')
    unittest.main(verbosity=1)
