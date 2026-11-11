#!/usr/bin/env python3
"""Agent scheduling, push and fallback tests, plus one judge run end to end."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from opswatch import agent, cli, statuspage  # noqa: E402
from opswatch.checks import http as http_check  # noqa: E402
from opswatch.store import Store  # noqa: E402
from test_core import T0, write_catalog  # noqa: E402


def item(**extra):
    value = {'id': 'console', 'host': 'aws', 'check': 'http', 'every': 60, 'args': {'url': 'https://a.example.org/'}}
    value.update(extra)
    return value


class CollectTests(unittest.TestCase):
    def test_runs_only_due_checks_and_keeps_the_newest_result(self):
        calls = []
        with mock.patch.object(http_check, 'run', lambda args, ctx: calls.append(1) or {'status': 'OK', 'reason': 'HEALTHY', 'detail': {}}):
            state = {}
            agent.collect([item(every=300)], state, T0, clock=lambda: T0)
            agent.collect([item(every=300)], state, T0 + 60, clock=lambda: T0 + 60)
            results = agent.collect([item(every=300)], state, T0 + 300, clock=lambda: T0 + 300)
        self.assertEqual(len(calls), 2)
        self.assertEqual(results[0]['sample_at'], T0 + 300)

    def test_undeclared_detail_becomes_a_check_error(self):
        with mock.patch.object(http_check, 'run', lambda args, ctx: {'status': 'OK', 'reason': 'HEALTHY', 'detail': {'secret': 'x'}}):
            result = agent.collect([item()], {}, T0, clock=lambda: T0)[0]
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'CHECK_CRASHED'))

    def test_crash_and_timeout_are_errors(self):
        def crash(args, ctx):
            raise RuntimeError('boom')

        with mock.patch.object(http_check, 'run', crash):
            self.assertEqual(agent.collect([item()], {}, T0, clock=lambda: T0)[0]['reason'], 'CHECK_CRASHED')
        with mock.patch.object(http_check, 'run', lambda args, ctx: time.sleep(2)):
            result = agent.run_check(dict(item(), every=60), agent.Context(T0, {}), timeout=0.2)
        self.assertEqual(result['reason'], 'CHECK_TIMEOUT')

    def test_checks_run_side_by_side_within_the_budget(self):
        items = [item(id=f'c{n}') for n in range(6)]

        def slow(args, ctx):
            time.sleep(0.5)
            return {'status': 'FAIL', 'reason': 'HTTP_CONNECT', 'detail': {}}

        started = time.monotonic()
        with mock.patch.object(http_check, 'run', slow):
            results = agent.collect(items, {}, T0, clock=lambda: T0, budget=0.2)
        self.assertLess(time.monotonic() - started, 0.45)
        self.assertEqual({r['reason'] for r in results}, {'CHECK_TIMEOUT'})

    def test_removed_items_are_forgotten(self):
        state = {'gone': {'last_run': 0}}
        with mock.patch.object(http_check, 'run', lambda args, ctx: {'status': 'OK', 'reason': 'HEALTHY', 'detail': {}}):
            agent.collect([item()], state, T0, clock=lambda: T0)
        self.assertNotIn('gone', state)


class PushTests(unittest.TestCase):
    config = {'push': {'mode': 'ssh', 'key_file': '/k', 'port': 22, 'known_hosts': '/kh', 'destination': 'opswatch@judge'}}

    def test_push_returns_the_judge_response(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout='{"judge_age": 12, "undelivered": 0}'))
        self.assertEqual(agent.push(self.config, {'host': 'site2'}, runner=runner)['judge_age'], 12)
        argv = runner.call_args[0][0]
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertEqual(runner.call_args[1]['timeout'], agent.PUSH_TIMEOUT)

    def test_push_failures_return_none(self):
        for runner in (mock.Mock(side_effect=subprocess.TimeoutExpired('ssh', 10)),
                       mock.Mock(return_value=subprocess.CompletedProcess([], 255, stdout='')),
                       mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout='not json'))):
            self.assertIsNone(agent.push(self.config, {'host': 'site2'}, runner=runner))


class FallbackTests(unittest.TestCase):
    config = {'fallback': {'webhook_file': '/unused'}}

    def run_fallback(self, state, response, sends, delivered=True):
        def sender(webhook, payload):
            sends.append(payload['embeds'][0]['title'])
            return {'delivery': 'SENT' if delivered else 'UNCERTAIN'}

        with mock.patch('opswatch.notify.load_webhook', return_value='hook'):
            return agent.fallback(self.config, state, response, T0, sender=sender)

    def test_three_failed_pushes_alert_once_then_recover(self):
        state, sends = {}, []
        for _ in range(2):
            self.assertIsNone(self.run_fallback(state, None, sends))
        self.assertEqual(self.run_fallback(state, None, sends), 'unreachable')
        self.run_fallback(state, None, sends)
        self.assertEqual(sends, ['⚙️ 감시 문제 · AWS 판정 서버'])
        self.run_fallback(state, {'judge_age': 20, 'undelivered': 0}, sends)
        self.assertEqual(sends[-1], '🟢 감시 재개 · AWS 판정 서버')

    def test_unreachable_discord_means_campus_outbound_and_stays_quiet(self):
        state, sends = {}, []
        for _ in range(4):
            self.run_fallback(state, None, sends, delivered=False)
        self.assertFalse(state['_fallback']['alerted'])

    def test_stopped_judge(self):
        state, sends = {}, []
        self.assertEqual(self.run_fallback(state, {'judge_age': 900, 'undelivered': 0}, sends), 'stopped')

    def test_hosts_without_fallback_do_nothing(self):
        self.assertIsNone(agent.fallback({}, {}, None, T0))


class JudgeRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        write_catalog(root / 'catalog')
        (root / 'www').mkdir()
        self.config = {'catalog': str(root / 'catalog'), 'state_dir': str(root / 'state'),
                       'www_dir': str(root / 'www'), 'webhook_file': str(root / 'hook'), 'send': True}
        self.root = root

    def push(self, host, now, results):
        inbox = self.root / 'state' / 'inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f'{host}.json').write_text(json.dumps({'host': host, 'received_at': now, 'results': {
            item_id: {'id': item_id, 'status': s, 'reason': r, 'detail': d, 'sample_at': now}
            for item_id, (s, r, d) in results.items()}}))

    def all_ok(self, now, console=('OK', 'HEALTHY', {})):
        self.push('aws', now, {'console': console, 'api': ('OK', 'HEALTHY', {}), 'staging': ('OK', 'HEALTHY', {})})
        self.push('site2', now, {'db': ('OK', 'HEALTHY', {})})
        self.push('site3', now, {'backup': ('OK', 'HEALTHY', {})})

    def test_run_sends_embeds_and_writes_the_page(self):
        sent = []
        with mock.patch('opswatch.notify.load_webhook', return_value='hook'):
            self.all_ok(T0)
            cli.run_judge(self.config, now=T0, sender=lambda w, p: sent.append(p) or {'delivery': 'SENT'})
            self.all_ok(T0 + 60, console=('FAIL', 'HTTP_STATUS', {'http_status': 502}))
            result = cli.run_judge(self.config, now=T0 + 60, sender=lambda w, p: sent.append(p) or {'delivery': 'SENT'})
        self.assertEqual(result['events'], 1)
        self.assertEqual(sent[-1]['embeds'][0]['title'], '🔴 장애 · 운영 콘솔 (console.example.org)')
        page = json.loads((self.root / 'www' / 'status.json').read_text())
        console = [i for g in page['groups'] for i in g['items'] if i['label'] == '운영 콘솔'][0]
        self.assertEqual((console['state'], console['note']), ('FAIL', 'HTTP 오류'))
        text = json.dumps(page, ensure_ascii=False)
        self.assertNotIn('console.example.org', text)
        status = json.loads((self.root / 'state' / 'judge-status.json').read_text())
        self.assertEqual(status['last_run'], T0 + 60)

    def test_shadow_mode_sends_nothing(self):
        self.config['send'] = False
        self.all_ok(T0)
        cli.run_judge(self.config, now=T0, sender=lambda w, p: self.fail('sent in shadow mode'))
        self.all_ok(T0 + 60, console=('FAIL', 'HTTP_STATUS', {'http_status': 502}))
        cli.run_judge(self.config, now=T0 + 60, sender=lambda w, p: self.fail('sent in shadow mode'))
        store = Store(self.root / 'state' / 'judge.sqlite')
        self.addCleanup(store.close)
        self.assertEqual(store.item('console')['state'], 'FAIL')
        self.assertEqual(store.outbox(['PENDING', 'SENT']), [])

    def test_daily_summary_once_after_nine(self):
        sent = []
        sender = lambda w, p: sent.append(p) or {'delivery': 'SENT'}  # noqa: E731
        day_eight = T0 - 14 * 3600  # 08:00 KST
        with mock.patch('opswatch.notify.load_webhook', return_value='hook'):
            # A fresh install announces itself once; the first day's 09:00 only
            # records the date, so no daily summary follows on day one.
            for offset in (0, 60, 3660):
                self.all_ok(day_eight + offset)
                cli.run_judge(self.config, now=day_eight + offset, sender=sender)
            titles = [embed['title'] for payload in sent for embed in payload['embeds']]
            self.assertEqual(titles, ['📋 알림 시작'])
            next_day = day_eight + 86400 + 3600
            for offset in (0, 60):
                self.all_ok(next_day + offset)
                cli.run_judge(self.config, now=next_day + offset, sender=sender)
        titles = [embed['title'] for payload in sent for embed in payload['embeds']]
        self.assertEqual(len(titles), 2)
        self.assertTrue(titles[1].startswith('📋 일일 요약'))

    def test_leaving_shadow_mode_lists_what_is_already_wrong(self):
        sent = []
        sender = lambda w, p: sent.append(p) or {'delivery': 'SENT'}  # noqa: E731
        self.config['send'] = False
        self.all_ok(T0)
        cli.run_judge(self.config, now=T0, sender=sender)
        self.all_ok(T0 + 60, console=('FAIL', 'HTTP_STATUS', {'http_status': 502}))
        cli.run_judge(self.config, now=T0 + 60, sender=sender)
        self.config['send'] = True
        self.all_ok(T0 + 120, console=('FAIL', 'HTTP_STATUS', {'http_status': 502}))
        with mock.patch('opswatch.notify.load_webhook', return_value='hook'):
            cli.run_judge(self.config, now=T0 + 120, sender=sender)
        embed = sent[0]['embeds'][0]
        self.assertEqual(embed['title'], '📋 알림 시작 · 진행 중 1건')
        self.assertIn('운영 콘솔', embed['description'])


class StatusPageTests(unittest.TestCase):
    def test_unknown_when_host_missing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        catalog = write_catalog(Path(tmp.name) / 'c')
        store = Store(Path(tmp.name) / 's.sqlite')
        self.addCleanup(store.close)
        page = statuspage.build(catalog, store, {'aws': True, 'site2': False, 'site3': True}, T0)
        db = [i for g in page['groups'] for i in g['items'] if i['label'] == '데이터베이스'][0]
        self.assertEqual(db['state'], 'UNKNOWN')
        self.assertFalse(page['campus_results'])
        self.assertEqual(len(db['days']), statuspage.DAYS)


if __name__ == '__main__':
    unittest.main(verbosity=1)
