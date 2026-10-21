"""Offline evidence for signal isolation, durable claims and webhook outcomes."""
from copy import deepcopy
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
MONITOR_SPEC = importlib.util.spec_from_file_location('service_health_monitor', Path(__file__).resolve().parents[1] / 'service-health-monitor.py')
monitor = importlib.util.module_from_spec(MONITOR_SPEC)
MONITOR_SPEC.loader.exec_module(monitor)
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


def service_source(name='dgx-query', *, max_age=180):
    return {'name': name, 'label': '연산 서버 질의', 'type': 'service_health',
            'path': '/protected/collector/state.json', 'summary_path': '/protected/collector/summary.json',
            'source_name': name, 'max_age': max_age}


class ServiceHealthAdapterTest(unittest.TestCase):
    def setUp(self):
        self.source = service_source()
        self.producer = {}
        self.producer_config = {'notifications_enabled': False, 'sources': [{
            'name': 'dgx-query', 'path': '/fixture/query', 'freshness_seconds': 180, 'failure_count': 2}]}
        self.consumer = alerts.new_state()
        self.summary = None

    def sample(self, status, now, *, source_stamp=None, reader=None):
        report = {'version': 1, 'source': 'dgx-query', 'status': status,
                  'observed_at': now if source_stamp is None else source_stamp,
                  'report': {'secret': 'never-copy-token', 'address': '192.0.2.17'}}
        self.summary = monitor.collect(self.producer_config, self.producer, now,
                                       reader=reader or (lambda path: deepcopy(report)))
        return alerts.collect_service_health(self.source, now,
                                              snapshot=(deepcopy(self.producer), deepcopy(self.summary)))

    def enqueue(self, report, now):
        return {dimension: alerts.enqueue_service_health(self.consumer, config(), self.source,
                dimension, value, now) for dimension, value in report.items()}

    def open_incident(self):
        self.enqueue(self.sample('FAILED', 1000), 1000)
        self.enqueue(self.sample('FAILED', 1060), 1060)
        return self.consumer['outbox'][-1]

    def deliver(self, now=1060, outcome='SENT'):
        with tempfile.TemporaryDirectory() as temporary:
            alerts.dispatch(self.consumer, Path(temporary) / 'state.json', URL, now,
                sender=lambda *args: {'delivery': outcome, 'message_id': '3' * 18})

    def close_incident(self):
        for now in range(1120, 4721, 60):
            report = self.sample('HEALTHY', now)
        return report

    def test_confirmed_durable_incident_survives_missed_event_restart_and_duplicate_samples(self):
        self.enqueue(self.sample('FAILED', 1000), 1000)
        self.assertEqual(self.consumer['outbox'], [])
        report = self.sample('FAILED', 1060)
        self.summary['sources']['dgx-query']['event'] = 'DUPLICATE'
        report = alerts.collect_service_health(self.source, 1060, snapshot=(self.producer, self.summary))
        self.enqueue(report, 1060)
        self.consumer = json.loads(json.dumps(self.consumer))
        self.enqueue(report, 1061)
        self.enqueue(self.sample('FAILED', 1120), 1120)
        self.assertEqual(len(self.consumer['outbox']), 1)
        self.assertEqual(self.consumer['outbox'][0]['source_incident'], 1)
        self.assertNotIn('never-copy-token', json.dumps(self.consumer))
        self.assertNotIn('192.0.2.17', json.dumps(self.consumer))

    def test_recovery_requires_full_fresh_hour_and_does_not_depend_on_summary_event(self):
        self.open_incident()
        self.deliver()
        for now in range(1120, 4720, 60):
            self.enqueue(self.sample('HEALTHY', now), now)
        self.assertEqual(len(self.consumer['outbox']), 1)
        report = self.sample('HEALTHY', 4720)
        self.summary['sources']['dgx-query']['event'] = 'UNCHANGED'
        report = alerts.collect_service_health(self.source, 4720, snapshot=(self.producer, self.summary))
        self.enqueue(report, 4720)
        self.assertEqual([row['source_event'] for row in self.consumer['outbox']], ['ALARM', 'NORMAL'])
        self.enqueue(self.sample('HEALTHY', 4780), 4780)
        self.assertEqual(len(self.consumer['outbox']), 2)

    def test_unknown_stale_and_loading_never_recover_or_advance_a_recovery_window(self):
        self.open_incident()
        self.deliver()
        for now in range(1120, 4661, 60):
            self.sample('HEALTHY', now)
        stale = self.sample('HEALTHY', 4900, source_stamp=4660)
        self.enqueue(stale, 4900)
        self.assertEqual(stale['condition']['status'], 'UNKNOWN')
        self.assertIsNone(self.producer['sources']['dgx-query']['healthy_since'])
        for now in range(4960, 8501, 60):
            self.enqueue(self.sample('HEALTHY', now), now)
        self.assertEqual(len(self.consumer['outbox']), 1)
        loading = self.sample('LOADING', 8560)
        self.enqueue(loading, 8560)
        self.assertEqual(loading['condition']['status'], 'UNKNOWN')
        self.assertEqual(len(self.consumer['outbox']), 1)

    def test_new_incident_is_not_hidden_by_alarm_to_alarm_polling_gap(self):
        self.open_incident()
        self.deliver()
        self.close_incident()
        self.sample('FAILED', 4780)
        self.enqueue(self.sample('FAILED', 4840), 4840)
        self.assertEqual([row['source_incident'] for row in self.consumer['outbox']], [1, 2])
        self.assertEqual([row['source_event'] for row in self.consumer['outbox']], ['ALARM', 'ALARM'])

    def test_pending_fault_closed_before_send_is_superseded_without_orphan_recovery(self):
        self.open_incident()
        report = self.close_incident()
        self.enqueue(report, 4720)
        self.assertEqual(len(self.consumer['outbox']), 1)
        self.assertEqual(self.consumer['outbox'][0]['delivery'], 'SUPERSEDED')
        alerts.validate_state(self.consumer)
        with tempfile.TemporaryDirectory() as temporary:
            count = alerts.dispatch(self.consumer, Path(temporary) / 'state.json', URL, 4720,
                                    sender=lambda *args: self.fail('sent a closed fault'))
        self.assertEqual(count, 0)

    def test_alarm_receipt_survives_outbox_pruning_and_still_allows_recovery(self):
        self.open_incident()
        self.deliver()
        self.assertEqual(self.consumer['observations']['dgx-query:condition']['alarm_delivery'], 'SENT')
        self.consumer['outbox'] = []
        self.consumer = json.loads(json.dumps(self.consumer))
        self.enqueue(self.close_incident(), 4720)
        self.assertEqual([row['source_event'] for row in self.consumer['outbox']], ['NORMAL'])

    def test_pair_epoch_source_status_identity_and_sequence_rollback_are_deferred(self):
        self.open_incident()
        original = deepcopy(self.consumer)
        for mutation in ('epoch', 'incident', 'status', 'source'):
            producer, summary = deepcopy(self.producer), deepcopy(self.summary)
            if mutation == 'epoch': summary['observed_at'] += 1
            if mutation == 'incident': summary['sources']['dgx-query']['incident'] += 1
            if mutation == 'status': producer['sources']['dgx-query']['latest_status'] = 'HEALTHY'
            if mutation == 'source': summary['sources']['wrong-query'] = summary['sources'].pop('dgx-query')
            report = alerts.collect_service_health(self.source, 1060, snapshot=(producer, summary))
            self.assertEqual(report['condition']['status'], 'UNKNOWN', mutation)
            self.enqueue(report, 1060)
        self.assertEqual(self.consumer, original)
        reset = {}
        summary = monitor.collect(self.producer_config, reset, 1120,
                   reader=lambda path: {'version': 1, 'source': 'dgx-query', 'status': 'HEALTHY', 'observed_at': 1120})
        report = alerts.collect_service_health(self.source, 1120, snapshot=(reset, summary))
        self.assertFalse(self.enqueue(report, 1120)['condition'])
        self.assertEqual(self.consumer['observations']['dgx-query:condition'],
                         original['observations']['dgx-query:condition'])
        self.assertEqual(self.consumer['outbox'], original['outbox'])

    def test_stale_collector_and_original_probe_epoch_cannot_establish_recovery(self):
        self.sample('HEALTHY', 1000)
        for now in (1181, 900):
            report = alerts.collect_service_health(self.source, now, snapshot=(self.producer, self.summary))
            self.assertEqual(report['condition']['status'], 'UNKNOWN')
        self.sample('HEALTHY', 1180, source_stamp=1000)
        report = alerts.collect_service_health(self.source, 1181, snapshot=(self.producer, self.summary))
        self.assertEqual(report['condition']['status'], 'UNKNOWN')
        self.assertEqual(report['observer']['status'], 'UNKNOWN')

    def test_pair_replacement_is_deferred_and_shared_pair_is_read_only_once(self):
        self.sample('HEALTHY', 1000)
        state_raw, summary_raw = json.dumps(self.producer).encode(), json.dumps(self.summary).encode()
        replaced = json.dumps(dict(self.producer, last_collection=1060)).encode()
        with patch.object(alerts, 'read_file', side_effect=[(state_raw, None), (summary_raw, None),
                                                           (replaced, None), (summary_raw, None)]):
            self.assertIsNone(alerts.service_snapshot(self.source))
        cfg = dict(config(), sources=[self.source, dict(self.source, name='same-query')])
        with patch.object(alerts.os, 'geteuid', return_value=0),              patch.object(alerts.socket, 'gethostname', return_value='example'),              patch.object(alerts, 'read_file', side_effect=[(state_raw, None), (summary_raw, None)] * 2) as read:
            report = alerts.run(cfg, inspect=True, now=1000)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(report['sources']['same-query']['condition'], 'NORMAL')

    def test_common_import_loss_suppresses_only_child_observer_and_preserves_fresh_service_fault(self):
        self.producer_config['sources'].append({'name': 'import-dgx', 'path': '/fixture/import',
            'expected_kind': 'observer-report', 'freshness_seconds': 180, 'failure_count': 2})
        self.producer_config['sources'][0]['observer_parent'] = 'import-dgx'
        def read(path):
            if str(path).endswith('/import'):
                return {'version': 1, 'source': 'import-dgx', 'kind': 'observer-report',
                        'status': 'OBSERVER_ERROR', 'reason': 'RESULT_IMPORT_UNAVAILABLE', 'observed_at': self.now}
            raise FileNotFoundError()
        for self.now in (1000, 1060, 1120):
            self.sample('HEALTHY', self.now, reader=read)
        report = alerts.collect_service_health(self.source, 1120, snapshot=(self.producer, self.summary))
        self.assertEqual(report['observer']['status'], 'UNKNOWN')
        self.assertEqual(self.summary['sources']['dgx-query']['suppressed_by'], 'import-dgx')
        def fresh_failure(path):
            if str(path).endswith('/query'):
                return {'version': 1, 'source': 'dgx-query', 'status': 'FAILED', 'observed_at': self.now}
            return read(path)
        for self.now in (1180, 1240):
            report = self.sample('FAILED', self.now, reader=fresh_failure)
        self.assertEqual(report['condition']['status'], 'ALARM')
        self.assertIsNone(self.summary['sources']['dgx-query']['suppressed_by'])
        self.enqueue(report, 1240)
        self.assertEqual(self.consumer['outbox'][0]['source_event'], 'ALARM')

    def test_observer_fault_requires_original_three_samples_and_raw_unknown_is_not_recovery(self):
        def missing(path): raise FileNotFoundError()
        for now in (1000, 1060):
            self.enqueue(self.sample('HEALTHY', now, reader=missing), now)
        self.assertEqual(self.consumer['outbox'], [])
        report = self.sample('HEALTHY', 1120, reader=missing)
        self.enqueue(report, 1120)
        self.assertEqual(len(self.consumer['outbox']), 1)
        self.assertEqual(self.consumer['outbox'][0]['dimension'], 'observer')
        self.assertIn('감시 관측 장애', self.consumer['outbox'][0]['content'])

    def test_recovery_pending_during_rate_limit_is_held_on_unknown_or_renewed_failure(self):
        self.open_incident()
        self.deliver()
        report = self.close_incident()
        self.enqueue(report, 4720)
        recovery = self.consumer['outbox'][-1]
        self.assertTrue(alerts.service_matches(recovery, report['condition']))
        for status in ('LOADING', 'OBSERVER_ERROR', 'FAILED'):
            report = self.sample(status, {'LOADING': 4780, 'OBSERVER_ERROR': 4840, 'FAILED': 4900}[status])
            self.assertFalse(alerts.service_matches(recovery, report['condition']))
            self.enqueue(report, {'LOADING': 4780, 'OBSERVER_ERROR': 4840, 'FAILED': 4900}[status])
        with tempfile.TemporaryDirectory() as temporary:
            alerts.dispatch(self.consumer, Path(temporary) / 'state.json', URL, 5000,
                eligible_service=set(), active_sources={'dgx-query'},
                sender=lambda *args: self.fail('sent a stale recovery'))
        self.assertEqual(recovery['delivery'], 'SUPERSEDED')

    def test_retired_sources_are_not_sent_and_ambiguous_claims_remain_preserved(self):
        self.open_incident()
        item = self.consumer['outbox'][-1]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            alerts.dispatch(self.consumer, path, URL, 1120, active_sources=set(),
                            sender=lambda *args: self.fail('sent a retired source'))
            self.assertEqual(item['delivery'], 'PENDING')
            item['delivery'] = 'ATTEMPTED'
            alerts.dispatch(self.consumer, path, URL, 1180, active_sources=set(),
                            sender=lambda *args: self.fail('replayed an ambiguous send'))
            self.assertEqual(item['delivery'], 'UNCERTAIN')

    def test_connection_test_leaves_unrelated_attempted_claim_unchanged(self):
        self.open_incident()['delivery'] = 'ATTEMPTED'
        with tempfile.TemporaryDirectory() as temporary:
            alerts.dispatch(self.consumer, Path(temporary) / 'state.json', URL, 1120, only_test=True,
                            sender=lambda *args: self.fail('sent an unrelated incident'))
        self.assertEqual(self.consumer['outbox'][0]['delivery'], 'ATTEMPTED')


    def test_generation_duplicate_samples_do_not_shorten_hour_recovery(self):
        self.source['max_age'] = 2100
        self.producer_config['sources'][0]['freshness_seconds'] = 2100
        self.enqueue(self.sample('FAILED', 1000), 1000)
        self.enqueue(self.sample('FAILED', 1900), 1900)
        self.deliver(1900)
        for now in range(1960, 6401, 60):
            original = 1900 + ((now - 1900) // 900) * 900
            report = self.sample('FAILED' if original == 1900 else 'HEALTHY', now, source_stamp=original)
            self.enqueue(report, now)
            self.assertEqual(len(self.consumer['outbox']), 2 if now == 6400 else 1)
        self.assertEqual(self.consumer['outbox'][-1]['source_closed_at'], 6400)

    def test_individual_invalid_probe_is_not_suppressed_by_failed_import(self):
        self.producer_config['sources'].append({'name': 'import-dgx', 'path': '/fixture/import',
            'expected_kind': 'observer-report', 'freshness_seconds': 180, 'failure_count': 2})
        self.producer_config['sources'][0]['observer_parent'] = 'import-dgx'
        for now in (1000, 1060, 1120):
            def read(path):
                if str(path).endswith('/import'):
                    return {'version': 1, 'source': 'import-dgx', 'kind': 'observer-report',
                            'status': 'OBSERVER_ERROR', 'reason': 'RESULT_IMPORT_UNAVAILABLE', 'observed_at': now}
                return {'version': 1, 'source': 'wrong-source', 'status': 'HEALTHY', 'observed_at': now}
            report = self.sample('HEALTHY', now, reader=read)
        self.assertIsNone(self.summary['sources']['dgx-query']['suppressed_by'])
        self.assertEqual(report['observer']['status'], 'ALARM')
        self.enqueue(report, 1120)
        self.assertEqual(self.consumer['outbox'][0]['dimension'], 'observer')

    def test_pair_adapter_does_not_mutate_producer_smtp_or_reports(self):
        self.open_incident()
        self.producer['sources']['dgx-query']['deliveries'] = {'FAILED': {'status': 'UNCERTAIN', 'attempts': 1}}
        producer, summary = deepcopy(self.producer), deepcopy(self.summary)
        before = deepcopy((producer, summary))
        report = alerts.collect_service_health(self.source, 1060, snapshot=(producer, summary))
        self.enqueue(report, 1060)
        self.assertEqual((producer, summary), before)

    def test_service_config_keeps_top_level_exact_keys_and_validates_selection(self):
        cfg = dict(config(), sources=[self.source])
        with patch.object(alerts, 'read_file', return_value=(json.dumps(cfg).encode(), None)):
            self.assertEqual(alerts.load_config(Path('/protected/config')), cfg)
        for mutation in ('missing', 'extra', 'relative', 'same', 'bad-name'):
            candidate = deepcopy(cfg)
            if mutation == 'missing': candidate['sources'][0].pop('source_name')
            if mutation == 'extra': candidate['notifications_enabled'] = True
            if mutation == 'relative': candidate['sources'][0]['summary_path'] = 'summary.json'
            if mutation == 'same': candidate['sources'][0]['summary_path'] = self.source['path']
            if mutation == 'bad-name': candidate['sources'][0]['source_name'] = 'bad name'
            with patch.object(alerts, 'read_file', return_value=(json.dumps(candidate).encode(), None)):
                with self.assertRaises(alerts.AlertError, msg=mutation):
                    alerts.load_config(Path('/protected/config'))


if __name__ == '__main__':
    unittest.main()
