"""Offline evidence for signal isolation, durable claims and webhook outcomes."""
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

SPEC = importlib.util.spec_from_file_location('discord_ops_alerts', Path(__file__).resolve().parents[1] / 'discord-ops-alerts.py')
alerts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alerts)
URL = 'https://discord.com/api/webhooks/' + '1' * 18 + '/' + 'a' * 64


def source(kind='monitor_state', path='/protected/state.json'):
    return {'name': 'core', 'label': '코어 백업', 'type': kind, 'path': path, 'max_age': 600}


def config():
    return {'host_label': '감시 서버', 'expected_hostname': 'example', 'sources': [source()],
            'webhook_file': '/protected/webhook', 'state_dir': '/protected/state'}


class DiscordAlertsTest(unittest.TestCase):
    def test_healthy_baseline_is_silent_and_stale_recovery_is_independent_of_source_sequence(self):
        state = alerts.new_state()
        for dimension in ('condition', 'freshness'):
            alerts.enqueue(state, config(), source(), dimension, 'NORMAL', 1000)
        self.assertEqual(state['outbox'], [])
        alerts.enqueue(state, config(), source(), 'condition', 'ALARM', 1001)
        alerts.enqueue(state, config(), source(), 'condition', 'ALARM', 1002)
        alerts.enqueue(state, config(), source(), 'freshness', 'ALARM', 2000)
        alerts.enqueue(state, config(), source(), 'freshness', 'NORMAL', 2001)
        alerts.enqueue(state, config(), source(), 'condition', 'NORMAL', 2002)
        self.assertEqual(len(state['outbox']), 4)
        self.assertEqual(len({row['id'] for row in state['outbox']}), 4)
        self.assertIn('갱신 지연', state['outbox'][1]['content'])
        self.assertIn('갱신 복구', state['outbox'][2]['content'])

    def test_initial_abnormal_alerts_and_missing_signal_does_not_claim_recovery(self):
        state = alerts.new_state()
        alerts.enqueue(state, config(), source(), 'condition', 'ALARM', 1000)
        alerts.enqueue(state, config(), source(), 'condition', 'UNKNOWN', 1001)
        self.assertEqual(len(state['outbox']), 1)
        self.assertEqual(state['observations']['core:condition']['status'], 'ALARM')

    def test_monitor_status_and_staging_level_do_not_copy_reports_or_sequences(self):
        for classification in ({'status': 'HEALTHY'}, {'level': 'FAIL'}):
            raw = dict(classification, last_check_epoch=1000, sequence=999,
                       report={'password': 'must-never-appear', 'reason': 'internal.example'})
            with patch.object(alerts, 'read_file', return_value=(json.dumps(raw).encode(), None)):
                report = alerts.collect(source(), 1100)
            self.assertEqual(report['condition'], 'NORMAL' if 'status' in raw else 'ALARM')
            self.assertNotIn('must-never-appear', json.dumps(report))
            self.assertEqual(alerts.collect(source(), 2000)['freshness'], 'ALARM')

    def test_db_healthy_cache_cannot_prove_current_success_and_future_data_is_unavailable(self):
        item = source('db_cache')
        raw = {'recovery_point_epoch': 1000, 'last_readback_epoch': 1050}
        with patch.object(alerts, 'read_file', return_value=(json.dumps(raw).encode(), SimpleNamespace(st_mtime=1100))):
            self.assertEqual(alerts.collect(item, 1200), {'condition': 'NORMAL', 'freshness': 'NORMAL'})
            self.assertEqual(alerts.collect(item, 1800), {'condition': 'ALARM', 'freshness': 'ALARM'})
            self.assertEqual(alerts.collect(item, 900), {'condition': 'UNKNOWN', 'freshness': 'ALARM'})

    def test_cron_marker_failure_and_success_are_read_without_log_text(self):
        def read(path, **kwargs):
            if str(path).endswith('.fail'):
                return b'opaque log', SimpleNamespace(st_mtime=1000)
            return b'ok', SimpleNamespace(st_mtime=900)
        with patch.object(alerts, 'read_file', side_effect=read):
            self.assertEqual(alerts.collect(source('cron_markers', '/ops/health'), 1100),
                             {'condition': 'ALARM', 'freshness': 'NORMAL'})

    def test_systemd_failure_and_stale_execution_are_separate(self):
        def command(*args, **kwargs):
            return SimpleNamespace(stdout='ActiveState=failed\nResult=exit-code\nExecMainStatus=2\nExecMainExitTimestampMonotonic=1000000000\nExecMainStartTimestampMonotonic=990000000\n')
        item = source('systemd', 'monitor.service')
        self.assertEqual(alerts.collect(item, 0, command=command, monotonic=lambda: 1100),
                         {'condition': 'ALARM', 'freshness': 'NORMAL'})
        self.assertEqual(alerts.collect(item, 0, command=command, monotonic=lambda: 2000),
                         {'condition': 'ALARM', 'freshness': 'ALARM'})

    def queued(self):
        state = alerts.new_state()
        alerts.enqueue(state, config(), source(), 'condition', 'ALARM', 1000)
        return state

    def test_claim_is_durable_before_send_and_interrupted_claim_is_never_replayed(self):
        state = self.queued()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            def sender(url, content):
                stored = json.loads(path.read_text())['outbox'][0]
                self.assertEqual((stored['delivery'], stored['attempts']), ('ATTEMPTED', 1))
                return {'delivery': 'SENT', 'message_id': '2' * 18, 'code': 'ACKNOWLEDGED'}
            self.assertEqual(alerts.dispatch(state, path, URL, 1000, sender=sender), 1)
            self.assertEqual(state['outbox'][0]['message_id'], '2' * 18)
            state['outbox'][0]['delivery'] = 'ATTEMPTED'
            alerts.dispatch(state, path, URL, 2000, sender=lambda *args: self.fail('replayed ambiguous send'))
            self.assertEqual(state['outbox'][0]['delivery'], 'UNCERTAIN')

    def test_definite_rejection_backs_off_and_stops_after_three_attempts(self):
        state = self.queued()
        calls = []
        def sender(*args):
            calls.append(True)
            return {'delivery': 'PENDING', 'code': 'REJECTED'}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            for now in (1000, 1001, 1060, 1100, 1360, 2000):
                alerts.dispatch(state, path, URL, now, sender=sender)
        self.assertEqual(len(calls), 3)
        self.assertEqual(state['outbox'][0]['delivery'], 'FAILED')

    def test_rate_limit_defers_entire_webhook_queue_and_preserves_requested_delay(self):
        state = self.queued()
        alerts.enqueue(state, config(), source(), 'freshness', 'ALARM', 1000)
        calls = []
        def sender(*args):
            calls.append(True)
            return {'delivery': 'PENDING', 'code': 'RATE_LIMITED', 'retry_after': 500}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            alerts.dispatch(state, path, URL, 1000, sender=sender)
            alerts.dispatch(state, path, URL, 1499, sender=sender)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state['webhook_retry_after'], 1500)

    def test_repeated_rate_limits_do_not_exhaust_delivery_retry_budget(self):
        state = self.queued()
        sender = lambda *args: {'delivery': 'PENDING', 'code': 'RATE_LIMITED', 'retry_after': 1}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            for now in (1000, 1001, 1002, 1003):
                alerts.dispatch(state, path, URL, now, sender=sender)
        self.assertEqual(state['outbox'][0]['delivery'], 'PENDING')
        self.assertEqual(state['outbox'][0]['attempts'], 0)
        self.assertEqual(state['outbox'][0]['rate_limit_count'], 4)

    def test_stale_healthy_record_cannot_claim_recovery(self):
        raw = {'status': 'HEALTHY', 'last_check_epoch': 1000}
        with patch.object(alerts, 'read_file', return_value=(json.dumps(raw).encode(), None)):
            self.assertEqual(alerts.collect(source(), 2000), {'condition': 'UNKNOWN', 'freshness': 'ALARM'})

    def test_webhook_request_disables_mentions_waits_for_id_and_rejects_redirects(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps({'id': '2' * 18}).encode()
        class Opener:
            def open(self, request, **kwargs):
                self.request = request
                return Response()
        opener = Opener()
        self.assertEqual(alerts.send_discord(URL, '시험', opener=opener)['delivery'], 'SENT')
        self.assertTrue(opener.request.full_url.endswith('?wait=true'))
        self.assertEqual(json.loads(opener.request.data)['allowed_mentions'], {'parse': []})
        self.assertIsNone(alerts.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.test'))
        for invalid in (URL.replace('discord.com', 'discord.com.example.test'), URL + '?wait=false',
                        URL.replace('https:', 'http:'), URL.replace('/api/', ':443/api/')):
            with self.assertRaises(alerts.AlertError): alerts.send_discord(invalid, '시험')

    def test_http_429_rejection_server_error_and_lost_response_have_distinct_outcomes(self):
        cases = [(429, b'{"retry_after": 2.5}', 'PENDING'), (403, b'{}', 'PENDING'),
                 (500, b'{}', 'UNCERTAIN'), (302, b'{}', 'FAILED')]
        for status, body, expected in cases:
            opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError(URL, status, 'secret error', {}, io.BytesIO(body))))
            outcome = alerts.send_discord(URL, '시험', opener=opener)
            self.assertEqual(outcome['delivery'], expected)
            self.assertNotIn(URL, json.dumps(outcome))
        opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(OSError(URL)))
        self.assertEqual(alerts.send_discord(URL, '시험', opener=opener)['delivery'], 'UNCERTAIN')

    def test_inspect_never_reads_webhook_or_writes_state_and_test_message_is_once(self):
        with patch.object(alerts.os, 'geteuid', return_value=0), patch.object(alerts.socket, 'gethostname', return_value='example'), \
             patch.object(alerts, 'read_file', side_effect=AssertionError('inspect read a protected file')):
            result = alerts.run(config(), inspect=True, collector=lambda *args: {'condition': 'NORMAL', 'freshness': 'NORMAL'})
        self.assertEqual(result['mode'], 'INSPECT')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            webhook = directory / 'webhook'
            webhook.write_text(URL)
            webhook.chmod(0o600)
            cfg = dict(config(), webhook_file=str(webhook), state_dir=str(directory))
            real_read = alerts.read_file
            def read(path, **kwargs):
                return Path(path).read_bytes(), Path(path).stat()
            sent = []
            sender = lambda *args: (sent.append(args) or {'delivery': 'SENT', 'message_id': '2' * 18})
            with patch.object(alerts.os, 'geteuid', return_value=0), patch.object(alerts.socket, 'gethostname', return_value='example'), \
                 patch.object(alerts, 'read_file', side_effect=read), patch.object(alerts.os, 'fstat') as fstat, \
                 patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o40700, st_uid=0)):
                fstat.return_value = SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1)
                for now in (1000, 1100):
                    alerts.run(cfg, test_message=True, now=now, collector=lambda *args: {}, sender=sender)
            self.assertEqual(len(sent), 1)
            self.assertIn('운영 알림 시험', sent[0][1])


if __name__ == '__main__':
    unittest.main()
