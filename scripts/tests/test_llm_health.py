#!/usr/bin/env python3
"""Offline fixtures for generation stalls, quotas, observations and delivery safety."""
from copy import deepcopy
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import PropertyMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import llm_health as common


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load('llm_probe', 'llm-health-probe.py')
collector = load('health_collector', 'service-health-monitor.py')
SECRET = 'fixture-private-token-do-not-publish'


def answer(content='4', output=1, prompt=18, finish='stop'):
    return {'choices': [{'message': {'content': content}, 'finish_reason': finish}],
            'usage': {'completion_tokens': output, 'prompt_tokens': prompt}}


def config(kind='generation', profile='upstream'):
    return {'source': 'upstream-generation', 'kind': kind, 'profile': profile,
            'base_url': 'http://127.0.0.1:8000/v1', 'model': 'fixture-model'}


def source():
    return {'name': 'upstream-generation', 'path': '/var/lib/fixture/result.json',
            'freshness_seconds': 720, 'failure_count': 2}


def monitor_config(enabled=False):
    return {'notifications_enabled': enabled, 'sources': [source()]}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.token = self.root / 'token'
        self.token.write_text(SECRET)
        self.token.chmod(0o600)


class GenerationTests(TempCase):
    def test_private_authentication_rejection_is_observer_error_for_query_and_generation(self):
        code = [401]
        class Connection:
            def __init__(self, *args, **kwargs):
                self.sock = self
            def connect(self):
                pass
            def settimeout(self, value):
                pass
            def request(self, *args):
                pass
            def getresponse(self):
                return self
            @property
            def status(self):
                return code[0]
            def read(self, limit):
                return b'{}'
            def close(self):
                pass
        for kind in ('host-query', 'generation'):
            for status in (401, 403):
                code[0] = status
                with self.subTest(kind=kind, status=status), patch.object(probe.http.client, 'HTTPConnection', Connection):
                    result = probe.http_worker({'config': config(kind, 'local'), 'token': SECRET})
                self.assertEqual((result['status'], result['reason']), ('OBSERVER_ERROR', 'CREDENTIAL_REJECTED'))

    def test_known_answer_requires_usage_and_finish(self):
        self.assertEqual(probe.checked_generation(answer())['status'], 'HEALTHY')
        for document in (answer('14'), answer('The answer is 4'), answer(''), answer(output=0),
                         answer(output=9), answer(output=True), answer(prompt=0),
                         answer(finish='length'), {'choices': []}, answer(content=None)):
            with self.subTest(document=document):
                self.assertEqual(probe.checked_generation(document)['status'], 'FAILED')

    def test_no_private_response_text_is_returned(self):
        result = probe.checked_generation(answer(SECRET))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_globally_routable_classification_is_rejected_without_a_live_address(self):
        selected = config()
        selected['base_url'] = 'http://198.51.100.10:8000/v1'
        address = probe.ipaddress.ip_address('198.51.100.10')
        # Python classifies documentation networks as private. Override only
        # that property to test public-address rejection without a live IP.
        with patch.object(type(address), 'is_private', new_callable=PropertyMock, return_value=False):
            with self.assertRaisesRegex(common.HealthError, 'LITERAL_PRIVATE_UPSTREAM_REQUIRED'):
                probe.validate(selected)

    def test_commercial_hostname_redirect_and_credentials_in_url_are_rejected(self):
        for endpoint in ('https://api.example.com/v1',
                         'http://user:password@127.0.0.1:8000/v1',
                         'http://127.0.0.1:8000/v1?next=bad', 'http://localhost:8000/v1'):
            selected = config()
            selected['base_url'] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises((common.HealthError, TypeError)):
                probe.validate(selected)

    def test_http_redirect_does_not_forward_credentials(self):
        class Connection:
            def __init__(self, *args, **kwargs):
                self.sock = self
            def connect(self):
                pass
            def settimeout(self, value):
                pass
            def request(self, method, path, body, headers):
                self.path = path
            def getresponse(self):
                return self
            @property
            def status(self):
                return 302
            def read(self, maximum):
                return b'{"redirect":"https://untrusted.example.com"}'
            def close(self):
                pass
        with patch.object(probe.http.client, 'HTTPConnection', Connection):
            result = probe.http_worker({'config': config(), 'token': SECRET})
        self.assertEqual(result['http_status'], 302)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_quota_reservation_survives_failure_and_restart(self):
        selected = config()
        selected['credential_file'] = str(self.token)
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            return {'status': 'FAILED', 'reason': 'TOTAL_TIMEOUT'}
        directory = self.root / 'probe'
        first = probe.probe(selected, directory, now=10000, requester=fail)
        probe.probe(selected, directory, now=10001, requester=fail)
        second = probe.probe(selected, directory, now=10300, requester=fail)
        self.assertEqual((first['status'], second['status'], len(calls)), ('FAILED', 'FAILED', 2))
        self.assertEqual(common.read_json(directory / 'budget.json')['attempts'], 2)
        self.assertEqual(common.read_json(directory / 'result.json')['observed_at'], 10300)

    def test_skip_does_not_hide_actual_failed_result(self):
        selected = config()
        selected['credential_file'] = str(self.token)
        directory = self.root / 'probe'
        probe.probe(selected, directory, now=10000, requester=lambda *a: {'status': 'FAILED'})
        skipped = probe.probe(selected, directory, now=10001, requester=lambda *a: self.fail())
        self.assertEqual(skipped['status'], 'BUDGET_LIMIT')
        self.assertEqual(common.read_json(directory / 'result.json')['status'], 'FAILED')

    def test_local_daily_cap_and_clock_rollback(self):
        selected = config(profile='local')
        selected['credential_file'] = str(self.token)
        directory = self.root / 'probe'
        common.private_directory(directory)
        common.atomic_json(directory / 'budget.json', {'day': '1970-01-01', 'attempts': 120})
        self.assertEqual(probe.probe(selected, directory, now=10000)['status'], 'BUDGET_LIMIT')
        with self.assertRaises(common.HealthError):
            probe.probe(selected, directory, now=9000)

    def test_secret_permissions_and_symlink_are_rejected(self):
        self.token.chmod(0o644)
        with self.assertRaises(common.HealthError):
            common.private_read(self.token)
        self.token.chmod(0o600)
        link = self.root / 'link'
        link.symlink_to(self.token)
        with self.assertRaises(OSError):
            common.private_read(link)

    def test_stalled_generation_after_models_success_has_total_deadline(self):
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = json.dumps({'data': [{'id': 'fixture-model'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                length = int(self.headers['Content-Length'])
                seen.append(json.loads(self.rfile.read(length)))
                time.sleep(0.8)
        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
        server = Server(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        selected = config(kind='host-query', profile='local')
        selected['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
        self.assertEqual(probe.transport(selected, SECRET, timeout=2)['status'], 'HEALTHY')
        selected['kind'] = 'generation'
        start = time.monotonic()
        result = probe.transport(selected, SECRET, timeout=0.2)
        self.assertEqual(result['reason'], 'TOTAL_TIMEOUT')
        self.assertLess(time.monotonic() - start, 0.65)
        self.assertEqual(seen[0]['max_completion_tokens'], 8)
        self.assertEqual(seen[0]['chat_template_kwargs'], {'enable_thinking': False})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_invalid_worker_output_does_not_leak_stderr(self):
        with patch.object(probe.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, SECRET, SECRET)):
            value = probe.transport(config(), SECRET)
        self.assertEqual(value['status'], 'OBSERVER_ERROR')
        self.assertNotIn(SECRET, json.dumps(value))

    def test_metrics_hang_cannot_replace_verified_query_with_failure(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == '/metrics':
                    time.sleep(0.8)
                    return
                body = json.dumps({'data': [{'id': 'fixture-model'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
        server = Server(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        selected = config(kind='host-query', profile='local')
        selected['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
        start = time.monotonic()
        result = probe.transport(selected, SECRET, timeout=0.2)
        self.assertEqual(result['status'], 'HEALTHY')
        self.assertEqual(result['metrics_status'], 'OBSERVER_ERROR')
        self.assertLess(time.monotonic() - start, 0.65)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_cold_loading_does_not_claim_recovery(self):
        selected = config(profile='local')
        selected['credential_file'] = str(self.token)
        value = probe.probe(selected, self.root / 'probe', now=10000,
            requester=lambda *args: {'status': 'FAILED', 'reason': 'TOTAL_TIMEOUT'},
            host_reader=lambda now: {'status': 'LOADING', 'boot_id': 'fixture-boot'})
        self.assertEqual(value['status'], 'LOADING')


class HostTests(TempCase):
    def test_unified_gpu_memory_is_unsupported(self):
        (self.root / 'sys/kernel/random').mkdir(parents=True)
        (self.root / 'sys/kernel/random/boot_id').write_text('fixture-boot')
        (self.root / 'uptime').write_text('7000 30000')
        (self.root / 'meminfo').write_text('MemTotal: 124000000 kB\nMemAvailable: 32000000 kB\n')
        def runner(argv):
            return ('ActiveState=active\nActiveEnterTimestampMonotonic=1000000' if
                    argv[0] == 'systemctl' else '55, 96, [N/A]')
        result = probe.host_report(10000, runner=runner, proc=self.root)
        self.assertEqual(result['gpu_memory'], 'UNSUPPORTED')
        self.assertEqual(result['status'], 'HEALTHY')
        self.assertEqual(result['gpu_utilization_percent'], 96)

    def test_counter_boot_reset_and_decrease_do_not_become_negative_progress(self):
        state = {}
        self.assertTrue(probe.counters(state, {'generation_tokens': 100}, 'boot-a')['counter_reset'])
        self.assertEqual(probe.counters(state, {'generation_tokens': 103}, 'boot-a')['generation_counter_delta'], 3)
        self.assertTrue(probe.counters(state, {'generation_tokens': 1}, 'boot-b')['counter_reset'])
        self.assertIsNone(probe.counters(state, {'generation_tokens': 0}, 'boot-b')['generation_counter_delta'])

    def test_metrics_only_accept_known_finite_nonnegative_numbers(self):
        result = probe.parse_metrics('vllm:generation_tokens_total{model_name="fixture"} 42\nother 99')
        self.assertEqual(result, {'generation_tokens': 42})
        for value in ('NaN', '-1', 'Inf'):
            with self.assertRaises(common.HealthError):
                probe.parse_metrics('vllm:generation_tokens_total ' + value)


class IncidentTests(unittest.TestCase):
    def observe(self, state, status, now):
        return common.observe(state, status, now, failure_count=2, freshness=720)

    def test_two_independent_failures_open_once_and_duplicate_does_not_count(self):
        state = {}
        self.assertEqual(self.observe(state, 'FAILED', 1000), 'UNCHANGED')
        self.assertEqual(self.observe(state, 'FAILED', 1000), 'DUPLICATE')
        self.assertEqual(self.observe(state, 'FAILED', 1300), 'FAILED')
        self.assertEqual(self.observe(state, 'FAILED', 1600), 'UNCHANGED')
        self.assertEqual(state['sequence'], 1)

    def test_flapping_keeps_incident_and_restarts_sixty_minute_stable_window(self):
        state = {}
        self.observe(state, 'FAILED', 1000)
        self.observe(state, 'FAILED', 1300)
        self.observe(state, 'HEALTHY', 1600)
        self.observe(state, 'FAILED', 1900)
        for now in range(2200, 5800, 300):
            self.assertEqual(self.observe(state, 'HEALTHY', now), 'UNCHANGED')
        self.assertEqual(self.observe(state, 'HEALTHY', 5800), 'RECOVERED')
        self.assertEqual(state['sequence'], 1)
        self.assertEqual(self.observe(state, 'HEALTHY', 6100), 'UNCHANGED')

    def test_unknown_and_gaps_invalidate_stability_without_closing(self):
        for unknown in (True, False):
            state = {}
            self.observe(state, 'FAILED', 1000)
            self.observe(state, 'FAILED', 1300)
            self.observe(state, 'HEALTHY', 1600)
            if unknown:
                self.observe(state, 'OBSERVER_ERROR', 1900)
            self.assertEqual(self.observe(state, 'HEALTHY', 6000), 'UNCHANGED')
            self.assertEqual(state['healthy_since'], 6000)
            self.assertEqual(state['incident'], 1)

    def test_restart_preserves_active_incident(self):
        state = {}
        self.observe(state, 'FAILED', 1000)
        self.observe(state, 'FAILED', 1300)
        restored = json.loads(json.dumps(state))
        self.assertEqual(self.observe(restored, 'FAILED', 1600), 'UNCHANGED')
        self.assertEqual(restored['incident'], 1)

    def test_closed_incident_is_not_reopened(self):
        state = {}
        self.observe(state, 'FAILED', 1000)
        self.observe(state, 'FAILED', 1300)
        for now in range(1600, 5500, 300):
            self.observe(state, 'HEALTHY', now)
        self.observe(state, 'FAILED', 5500)
        self.assertEqual(self.observe(state, 'FAILED', 5800), 'FAILED')
        self.assertEqual(state['incident'], 2)


class DeliveryTests(unittest.TestCase):
    def test_attempt_is_persisted_before_send_and_uncertain_is_never_replayed(self):
        delivery = {'status': 'NEW', 'attempts': 0}
        snapshots = []
        def send(value):
            self.assertEqual(snapshots[-1]['status'], 'ATTEMPTED')
            raise common.MailUncertain()
        common.deliver(delivery, 1000, send, lambda: snapshots.append(deepcopy(delivery)))
        self.assertEqual(delivery['status'], 'UNCERTAIN')
        common.deliver(delivery, 2000, lambda value: self.fail(), lambda: None)

    def test_interrupted_attempt_never_replays(self):
        common.deliver({'status': 'ATTEMPTED', 'attempts': 1}, 1000,
                       lambda value: self.fail(), lambda: self.fail())

    def test_definite_failure_only_retries_at_one_and_five_minutes_then_stops(self):
        delivery = {'status': 'NEW', 'attempts': 0}
        calls = []
        def send(value):
            calls.append(1)
            raise common.MailNotSent()
        for now in (1000, 1059, 1060, 1359, 1360, 2000):
            common.deliver(delivery, now, send, lambda: None)
        self.assertEqual((len(calls), delivery['status']), (3, 'EXHAUSTED'))


class CollectorTests(TempCase):
    def correlated_config(self, *, enabled=False, children=1):
        parent = {'name': 'import-aws', 'path': '/var/lib/fixture/import.json',
            'expected_kind': 'observer-report', 'freshness_seconds': 180, 'failure_count': 3}
        selected = [parent]
        for number in range(children):
            child = source()
            child.update(name='child-' + str(number), path='/var/lib/fixture/child-' + str(number) + '.json',
                         observer_parent='import-aws')
            selected.append(child)
        return {'notifications_enabled': enabled, 'sources': selected}

    def correlated_reader(self, now, *, parent='OBSERVER_ERROR', child=None, parent_at=None):
        def read(path):
            if path.name == 'import.json':
                if parent is None:
                    raise FileNotFoundError(path)
                return {'version': 1, 'source': 'import-aws', 'kind': 'observer-report',
                    'observed_at': now if parent_at is None else parent_at, 'status': parent,
                    'reason': 'RESULT_IMPORT_VERIFIED' if parent == 'HEALTHY' else 'RESULT_IMPORT_UNAVAILABLE'}
            if child is None:
                raise FileNotFoundError(path)
            return dict(child, source=path.stem)
        return read

    def test_observer_parent_configuration_rejects_missing_self_chains_cycles_and_wrong_kind(self):
        selected = self.correlated_config()
        collector.validate(selected)
        cases = []
        for parent in ('missing', 'child-0', None, 4):
            candidate = deepcopy(selected)
            candidate['sources'][1]['observer_parent'] = parent
            cases.append(candidate)
        for expected in (None, 'tls', 'generation'):
            candidate = deepcopy(selected)
            candidate['sources'][0]['expected_kind'] = expected
            cases.append(candidate)
        chain = deepcopy(selected)
        chain['sources'].append(dict(chain['sources'][0], name='import-other', path='/var/lib/fixture/other.json'))
        chain['sources'][0]['observer_parent'] = 'import-other'
        cases.append(chain)
        cycle = deepcopy(selected)
        cycle['sources'][0]['observer_parent'] = 'child-0'
        cases.append(cycle)
        for candidate in cases:
            with self.subTest(config=candidate), self.assertRaises(common.HealthError):
                collector.validate(candidate)

    def test_whole_bundle_loss_opens_only_common_observer_and_keeps_unknown_evidence(self):
        selected = self.correlated_config(children=4)
        state = {}
        for now in (1000, 1060, 1120):
            report = collector.collect(selected, state, now, reader=self.correlated_reader(now))
        self.assertEqual(state['sources']['import-aws']['observer']['incident'], 1)
        for number in range(4):
            name = 'child-' + str(number)
            entry = state['sources'][name]
            self.assertNotIn('incident', entry)
            self.assertNotIn('incident', entry['observer'])
            self.assertEqual(entry['observer']['latest_status'], 'OBSERVER_ERROR')
            evidence = report['sources'][name]
            self.assertEqual((evidence['status'], evidence['raw_status'], evidence['suppressed_by'],
                              evidence['parent_status']), ('OBSERVER_ERROR', 'UNKNOWN', 'import-aws', 'OBSERVER_ERROR'))

    def test_missing_or_stale_parent_suppresses_only_missing_or_stale_children(self):
        for parent, parent_at in ((None, None), ('HEALTHY', 1)):
            with self.subTest(parent=parent):
                state = {}
                for now in (1000, 1060, 1120):
                    stale = {'version': 1, 'observed_at': 10, 'status': 'HEALTHY', 'reason': 'GENERATION_VERIFIED'}
                    report = collector.collect(self.correlated_config(), state, now,
                        reader=self.correlated_reader(now, parent=parent, parent_at=parent_at, child=stale))
                child = report['sources']['child-0']
                self.assertEqual(child['suppressed_by'], 'import-aws')
                self.assertEqual((child['source_sample_at'], child['source_reason']), (10, 'GENERATION_VERIFIED'))
                self.assertEqual(child['raw_status'], 'UNKNOWN')
                self.assertNotIn('incident', state['sources']['child-0']['observer'])

    def test_healthy_parent_does_not_hide_individual_child_outage_during_parent_recovery(self):
        state = {}
        for now in (1000, 1060, 1120):
            collector.collect(self.correlated_config(), state, now, reader=self.correlated_reader(now))
        for now in (1180, 1240, 1300):
            report = collector.collect(self.correlated_config(), state, now,
                reader=self.correlated_reader(now, parent='HEALTHY'))
        self.assertEqual(state['sources']['import-aws']['observer']['incident'], 1)
        self.assertEqual(state['sources']['child-0']['observer']['incident'], 1)
        self.assertIsNone(report['sources']['child-0']['suppressed_by'])

    def test_fresh_service_tls_and_probe_observer_failures_are_not_suppressed(self):
        for kind in ('service', 'tls', 'probe'):
            with self.subTest(kind=kind):
                selected = self.correlated_config()
                child = selected['sources'][1]
                if kind == 'tls':
                    child.update(component='critical-tls', expected_kind='tls', failure_count=1)
                state = {}
                for now in (1000, 1060, 1120):
                    value = {'version': 1, 'observed_at': now, 'status': 'OBSERVER_ERROR' if kind == 'probe' else 'FAILED',
                        'kind': 'tls' if kind == 'tls' else 'generation',
                        'reason': 'CREDENTIAL_REJECTED' if kind == 'probe' else 'TLS_CERTIFICATE_VALIDATION_FAILED' if kind == 'tls' else 'GENERATION_INVALID'}
                    report = collector.collect(selected, state, now,
                        reader=self.correlated_reader(now, child=value))
                entry = state['sources']['child-0']
                target = entry['observer'] if kind == 'probe' else entry
                self.assertEqual(target['incident'], 1)
                self.assertIsNone(report['sources']['child-0']['suppressed_by'])

    def test_suppression_resets_existing_service_and_observer_recovery_without_renumbering(self):
        state = {'sources': {'child-0': {'incident': 4, 'sequence': 4, 'sample_at': 900,
            'healthy_since': 100, 'healthy_samples': 20, 'failures': 1,
            'observer': {'incident': 7, 'sequence': 7, 'sample_at': 900,
                'healthy_since': 100, 'healthy_samples': 20, 'failures': 2}}}}
        collector.collect(self.correlated_config(), state, 1000, reader=self.correlated_reader(1000))
        entry = state['sources']['child-0']
        for target, incident in ((entry, 4), (entry['observer'], 7)):
            self.assertEqual((target['incident'], target['sequence']), (incident, incident))
            self.assertEqual((target['healthy_since'], target['healthy_samples'], target['failures']), (None, 0, 0))

    def test_suppression_with_already_consumed_epoch_interrupts_both_recovery_windows(self):
        entry = {'incident': 1, 'sequence': 1, 'sample_at': 1000, 'healthy_since': 100,
            'healthy_samples': 20, 'failures': 2, 'latest_status': 'HEALTHY'}
        observer = deepcopy(entry)
        entry['observer'] = observer
        state = {'last_collection': 1000, 'sources': {'child-0': entry}}
        report = collector.collect(self.correlated_config(), state, 1000, reader=self.correlated_reader(1000))
        self.assertEqual(report['sources']['child-0']['observer_event'], 'DUPLICATE')
        for target in (entry, observer):
            self.assertEqual((target['healthy_since'], target['healthy_samples'], target['failures']), (None, 0, 0))
            self.assertEqual((target['incident'], target['sequence']), (1, 1))

    def test_suppression_does_not_hide_fresh_malformed_or_future_child_evidence(self):
        for child in ({}, {'version': 1, 'observed_at': 3000, 'status': 'HEALTHY'},
                      {'version': 1, 'observed_at': 1000, 'status': 'BOGUS'}):
            with self.subTest(child=child):
                state = {}
                for now in (1000, 1060, 1120):
                    report = collector.collect(self.correlated_config(), state, now,
                        reader=self.correlated_reader(now, child=child))
                self.assertIsNone(report['sources']['child-0']['suppressed_by'])
                self.assertEqual(state['sources']['child-0']['observer']['incident'], 1)

    def test_stale_malformed_child_is_not_correlated_as_transport_loss(self):
        cases = [
            ({}, {'status': 'BOGUS'}),
            ({}, {}),
            ({'component': 'host'}, {'status': 'HEALTHY'}),
            ({'component': 'host'}, {'host': {'status': 'BOGUS'}}),
            ({'component': 'critical-tls', 'expected_kind': 'tls', 'failure_count': 1},
             {'kind': 'generation', 'status': 'FAILED', 'reason': 'TLS_CERTIFICATE_VALIDATION_FAILED'}),
            ({'component': 'critical-tls', 'expected_kind': 'tls', 'failure_count': 1},
             {'kind': 'tls', 'status': 'FAILED', 'reason': 'TCP_TIMEOUT'}),
            ({'component': 'critical-tls', 'expected_kind': 'tls', 'failure_count': 1},
             {'kind': 'tls', 'status': 'LOADING', 'reason': 'TLS_CERTIFICATE_VERIFIED'}),
            ({'expected_kind': 'observer-report'},
             {'kind': 'observer-report', 'status': 'HEALTHY', 'reason': 'GENERATION_VERIFIED'}),
        ]
        for selection, document in cases:
            with self.subTest(selection=selection, document=document):
                selected = self.correlated_config()
                selected['sources'][1].update(selection)
                value = dict(document, version=1, observed_at=10)
                state = {}
                for now in (1000, 1060, 1120):
                    report = collector.collect(selected, state, now,
                        reader=self.correlated_reader(now, child=value))
                child = report['sources']['child-0']
                self.assertEqual(child['unknown_reason'], 'RESULT_INVALID')
                self.assertIsNone(child['suppressed_by'])
                self.assertEqual(child['source_sample_at'], 10)
                self.assertEqual(state['sources']['child-0']['observer']['incident'], 1)

    def test_suppressed_pending_observer_failure_resumes_after_parent_returns(self):
        directory = self.root / 'resume-pending'
        common.private_directory(directory)
        pending = {'status': 'NOT_SENT', 'incident': 5, 'event': 'FAILED',
            'target': 'observer-child-0', 'attempts': 1, 'retry_after': 900}
        common.atomic_json(directory / 'state.json', {'sources': {'child-0': {'observer': {
            'incident': 5, 'sequence': 5, 'sample_at': 900, 'deliveries': {'FAILED': pending}}}}})
        messages = []
        sender = lambda value: messages.append(deepcopy(value))
        collector.run(self.correlated_config(enabled=True), directory, now=1000,
            reader=self.correlated_reader(1000), sender=sender)
        self.assertEqual(messages, [])
        collector.run(self.correlated_config(enabled=True), directory, now=1060,
            reader=self.correlated_reader(1060, parent='HEALTHY'), sender=sender)
        self.assertEqual([(message['target'], message['event'], message['incident']) for message in messages],
                         [('observer-child-0', 'FAILED', 5)])

    def test_suppression_preserves_accepted_delivery_matrix_and_cancels_unsent_recovery(self):
        for layer in ('service', 'observer'):
            for event in ('FAILED', 'RECOVERED'):
                for delivery_status in ('NEW', 'NOT_SENT', 'ATTEMPTED', 'SENT', 'UNCERTAIN', 'EXHAUSTED'):
                    with self.subTest(layer=layer, event=event, delivery_status=delivery_status):
                        delivery = {'status': delivery_status, 'incident': 3, 'event': event,
                            'target': 'child-0' if layer == 'service' else 'observer-child-0', 'attempts': 1}
                        target = {'incident': 3, 'sequence': 3, 'sample_at': 900,
                                  'deliveries': {event: delivery}}
                        entry = target if layer == 'service' else {'observer': target}
                        state = {'sources': {'child-0': entry}}
                        collector.collect(self.correlated_config(enabled=True), state, 1000,
                            reader=self.correlated_reader(1000), notifications=True)
                        expected = ('SUPERSEDED' if event == 'RECOVERED'
                                    and delivery_status in ('NEW', 'NOT_SENT') else delivery_status)
                        self.assertEqual(delivery['status'], expected)
                        self.assertEqual((target['incident'], target['sequence']), (3, 3))

    def test_fresh_child_and_invalid_parent_kind_do_not_turn_child_health_unknown(self):
        selected = self.correlated_config()
        for parent in ({}, {'version': 1, 'source': 'import-aws', 'observed_at': 1000,
                'status': 'HEALTHY', 'kind': 'generation', 'reason': 'RESULT_IMPORT_VERIFIED'}):
            with self.subTest(parent=parent):
                def read(path):
                    return parent if path.name == 'import.json' else {
                        'version': 1, 'source': 'child-0', 'observed_at': 1000, 'status': 'HEALTHY'}
                report = collector.collect(selected, {}, 1000, reader=read)
                self.assertEqual(report['sources']['import-aws']['status'], 'OBSERVER_ERROR')
                self.assertEqual(report['sources']['child-0']['status'], 'HEALTHY')
                self.assertIsNone(report['sources']['child-0']['suppressed_by'])

    def test_suppressed_child_pending_observer_mail_is_held_and_accepted_records_are_preserved(self):
        for delivery_status in ('NEW', 'NOT_SENT', 'SENT', 'UNCERTAIN'):
            with self.subTest(delivery_status=delivery_status):
                pending = {'status': delivery_status, 'incident': 5, 'event': 'FAILED',
                    'target': 'observer-child-0', 'attempts': 0}
                original = deepcopy(pending)
                state = {'sources': {'child-0': {'observer': {'incident': 5, 'sequence': 5,
                    'sample_at': 900, 'deliveries': {'FAILED': pending}}}}}
                directory = self.root / ('suppression-' + delivery_status)
                common.private_directory(directory)
                common.atomic_json(directory / 'state.json', state)
                messages = []
                collector.run(self.correlated_config(enabled=True), directory, now=1000,
                    reader=self.correlated_reader(1000), sender=lambda value: messages.append(deepcopy(value)))
                self.assertEqual(messages, [])
                saved = common.read_json(directory / 'state.json')['sources']['child-0']['observer']
                self.assertEqual(saved['deliveries']['FAILED'], original)
                self.assertEqual((saved['incident'], saved['sequence']), (5, 5))

    def test_suppressed_child_without_incident_cannot_send_orphan_recovery(self):
        directory = self.root / 'no-orphan-recovery'
        messages = []
        for now in (1000, 1060, 1120):
            collector.run(self.correlated_config(enabled=True), directory, now=now,
                reader=self.correlated_reader(now), sender=lambda value: messages.append(deepcopy(value)))
        for now in range(1180, 4900, 60):
            child = {'version': 1, 'observed_at': now, 'status': 'HEALTHY'}
            collector.run(self.correlated_config(enabled=True), directory, now=now,
                reader=self.correlated_reader(now, parent='HEALTHY', child=child),
                sender=lambda value: messages.append(deepcopy(value)))
        self.assertEqual([(message['target'], message['event']) for message in messages],
                         [('observer-import-aws', 'FAILED'), ('observer-import-aws', 'RECOVERED')])

    def test_suppression_is_order_independent_and_restart_preserves_same_epoch(self):
        selected = self.correlated_config()
        backwards = deepcopy(selected)
        backwards['sources'].reverse()
        first, second = {}, {}
        for now in (1000, 1060, 1120):
            one = collector.collect(selected, first, now, reader=self.correlated_reader(now))
            two = collector.collect(backwards, second, now, reader=self.correlated_reader(now))
            self.assertEqual((first, one), (second, two))
        restored = json.loads(json.dumps(first))
        collector.collect(selected, restored, 1120, reader=self.correlated_reader(1120))
        self.assertEqual(restored, first)

    def test_collection_reads_shared_result_path_once_and_freezes_before_classification(self):
        parent = self.correlated_config()['sources'][0]
        host, query = source(), source()
        host.update(name='local-host', report_source='host-query', component='host')
        query.update(name='local-query', report_source='host-query')
        selected = {'sources': [parent, host, query]}
        calls = []
        def read(path):
            calls.append(path)
            return {'version': 1, 'source': 'import-aws' if path.name == 'import.json' else 'host-query',
                'kind': 'observer-report' if path.name == 'import.json' else 'host-query',
                'observed_at': 1000, 'status': 'HEALTHY', 'reason': 'RESULT_IMPORT_VERIFIED',
                'host': {'status': 'HEALTHY'}}
        report = collector.collect(selected, {}, 1000, reader=read)
        self.assertEqual(len(calls), 2)
        self.assertEqual(report['sources']['local-host']['status'], 'HEALTHY')
        self.assertEqual(report['sources']['local-query']['status'], 'HEALTHY')

    def test_retired_source_and_observer_delivery_states_are_preserved_without_sending(self):
        for layer in ('service', 'observer'):
            for event in ('FAILED', 'RECOVERED'):
                for delivery_status in ('NEW', 'NOT_SENT', 'SENT', 'UNCERTAIN'):
                    with self.subTest(layer=layer, event=event, delivery_status=delivery_status):
                        delivery = {'target': 'retired-generation', 'event': event, 'incident': 1,
                            'status': delivery_status, 'attempts': 0, 'retry_after': 900}
                        retired = {'sequence': 1, 'sample_at': 900, 'latest_status': 'FAILED',
                                   'incident': 1, 'deliveries': {event: delivery}}
                        if layer == 'observer':
                            retired = {'observer': retired}
                        original = deepcopy(retired)
                        directory = self.root / ('retired-' + layer + '-' + event + '-' + delivery_status)
                        common.private_directory(directory)
                        common.atomic_json(directory / 'state.json', {'last_collection': 900,
                            'sources': {'retired-generation': retired}})
                        messages = []
                        collector.run(monitor_config(True), directory, now=1000,
                            reader=lambda path: self.result(1000),
                            sender=lambda value: messages.append(deepcopy(value)))
                        self.assertEqual(messages, [])
                        saved = common.read_json(directory / 'state.json')
                        self.assertEqual(saved['sources']['retired-generation'], original)

    def test_active_source_still_sends_once_with_retired_pending_state_present(self):
        directory = self.root / 'active-with-retired'
        common.private_directory(directory)
        retired = {'deliveries': {'FAILED': {'target': 'retired-generation', 'event': 'FAILED',
                    'incident': 1, 'status': 'NEW', 'attempts': 0}}}
        common.atomic_json(directory / 'state.json', {'sources': {'retired-generation': retired}})
        messages = []
        for now in (1000, 1300, 1600):
            collector.run(monitor_config(True), directory, now=now,
                reader=lambda path: self.result(now, 'FAILED'),
                sender=lambda value: messages.append((value['target'], value['event'], value['incident'])))
        self.assertEqual(messages, [('upstream-generation', 'FAILED', 1)])
        self.assertEqual(common.read_json(directory / 'state.json')['sources']['retired-generation'], retired)

    def test_rejected_probe_credential_only_opens_observer_incident(self):
        state = {}
        for now in (1000, 1060, 1120):
            value = self.result(now, 'OBSERVER_ERROR')
            value.update(reason='CREDENTIAL_REJECTED', http_status=401)
            collector.collect(monitor_config(), state, now, reader=lambda path: value)
        entry = state['sources']['upstream-generation']
        self.assertNotIn('incident', entry)
        self.assertEqual(entry['observer']['incident'], 1)

    def test_critical_tls_uses_independent_security_incident_and_single_failure_only(self):
        http_source = source()
        tls_source = {'name': 'public-tls-production', 'component': 'critical-tls', 'expected_kind': 'tls',
            'path': '/var/lib/fixture/tls.json', 'freshness_seconds': 180, 'failure_count': 1}
        selected = {'sources': [http_source, tls_source], 'notifications_enabled': False}
        state = {}
        tls = {'version': 1, 'source': 'public-tls-production', 'kind': 'tls', 'observed_at': 1000,
               'status': 'HEALTHY', 'reason': 'TLS_CERTIFICATE_VERIFIED'}
        for now in (1000, 1300):
            tls['observed_at'] = now
            collector.collect(selected, state, now,
                reader=lambda path: tls if path.name == 'tls.json' else self.result(now, 'FAILED'))
        self.assertEqual(state['sources']['upstream-generation']['incident'], 1)
        tls.update(observed_at=1360, status='FAILED', reason='TLS_CERTIFICATE_VALIDATION_FAILED')
        report = collector.collect(selected, state, 1360,
            reader=lambda path: tls if path.name == 'tls.json' else self.result(1300, 'FAILED'))
        self.assertEqual(report['sources']['public-tls-production']['event'], 'FAILED')
        self.assertEqual(state['sources']['public-tls-production']['incident'], 1)
        self.assertEqual(state['sources']['upstream-generation']['sequence'], 1)
        tls.update(observed_at=1420, reason='TLS_CERTIFICATE_EXPIRING')
        collector.collect(selected, state, 1420,
            reader=lambda path: tls if path.name == 'tls.json' else self.result(1300, 'FAILED'))
        self.assertEqual(state['sources']['public-tls-production']['sequence'], 1)

    def test_failure_count_one_requires_trusted_tls_kind_and_security_reason(self):
        ordinary = monitor_config()
        ordinary['sources'][0]['failure_count'] = 1
        with self.assertRaises(common.HealthError):
            collector.validate(ordinary)
        tls = {'name': 'public-tls-production', 'component': 'critical-tls', 'expected_kind': 'tls',
            'path': '/var/lib/fixture/tls.json', 'freshness_seconds': 180, 'failure_count': 1}
        collector.validate({'sources': [tls]})
        for kind, reason in (('public-http', 'TLS_CERTIFICATE_EXPIRING'), ('tls', 'TCP_TIMEOUT'),
                             ('tls', 'HTTP_ERROR'), ('tls', 'TLS_HANDSHAKE_FAILED')):
            value = {'version': 1, 'source': tls['name'], 'kind': kind, 'observed_at': 1000,
                     'status': 'FAILED', 'reason': reason}
            with self.subTest(kind=kind, reason=reason):
                self.assertEqual(collector.sample(tls, 1000, reader=lambda path: value)[0], 'OBSERVER_ERROR')

    def result(self, now, status='HEALTHY'):
        return {'version': 1, 'source': 'upstream-generation', 'observed_at': now, 'status': status}

    def test_observer_failure_is_separate_from_service_fault(self):
        state = {}
        selected = monitor_config()
        for now in (1000, 1060, 1120):
            report = collector.collect(selected, state, now, reader=lambda path: {})
        entry = report['sources']['upstream-generation']
        self.assertEqual(entry['status'], 'OBSERVER_ERROR')
        self.assertIsNone(entry['incident'])
        self.assertEqual(entry['observer_incident'], 1)

    def test_duplicate_fresh_result_does_not_open_service_incident(self):
        state = {}
        for now in (1000, 1060, 1120):
            collector.collect(monitor_config(), state, now,
                reader=lambda path: self.result(1000, 'FAILED'))
        self.assertNotIn('incident', state['sources']['upstream-generation'])

    def test_expired_or_future_result_is_unknown(self):
        for timestamp in (1, 2000):
            report = collector.collect(monitor_config(), {}, 1000,
                reader=lambda path: self.result(timestamp))
            self.assertEqual(report['sources']['upstream-generation']['status'], 'OBSERVER_ERROR')

    def test_dry_run_writes_nothing_and_never_reads_mail_or_calls_sender(self):
        directory = self.root / 'not-created'
        collector.run(monitor_config(True), directory, dry_run=True, now=1000,
            reader=lambda path: self.result(1000, 'FAILED'), sender=lambda value: self.fail())
        self.assertFalse(directory.exists())

    def test_disabled_notification_does_not_read_smtp_or_reserve_delivery(self):
        directory = self.root / 'state'
        for now in (1000, 1300):
            collector.run(monitor_config(), directory, now=now,
                reader=lambda path: self.result(now, 'FAILED'), sender=lambda value: self.fail())
        self.assertNotIn('deliveries', common.read_json(directory / 'state.json')['sources']['upstream-generation'])

    def test_enable_after_observation_reports_existing_incident_once(self):
        state = {}
        for now in (1000, 1300):
            collector.collect(monitor_config(), state, now,
                reader=lambda path: self.result(now, 'FAILED'))
        collector.collect(monitor_config(True), state, 1360, notifications=True,
            reader=lambda path: self.result(1300, 'FAILED'))
        self.assertEqual(state['sources']['upstream-generation']['deliveries']['FAILED']['incident'], 1)

    def test_collector_lock_is_nonblocking(self):
        directory = self.root / 'state'
        with common.locked(directory):
            with self.assertRaisesRegex(common.HealthError, 'ALREADY_RUNNING'):
                collector.run(monitor_config(), directory, now=1000)

    def test_expiry_interrupts_recovery_even_if_timestamp_was_already_consumed(self):
        state = {}
        for now in (1000, 1300):
            collector.collect(monitor_config(), state, now,
                reader=lambda path: self.result(now, 'FAILED'))
        collector.collect(monitor_config(), state, 1600,
            reader=lambda path: self.result(1600))
        collector.collect(monitor_config(), state, 2400,
            reader=lambda path: self.result(1600))
        self.assertIsNone(state['sources']['upstream-generation']['healthy_since'])

    def test_new_episode_and_recovery_cannot_replay_old_failure(self):
        state = {}
        selected = monitor_config(True)
        for now in (1000, 1300):
            collector.collect(selected, state, now, notifications=True,
                reader=lambda path: self.result(now, 'FAILED'))
        entry = state['sources']['upstream-generation']
        entry['deliveries']['FAILED']['status'] = 'NOT_SENT'
        for now in range(1600, 5500, 300):
            collector.collect(selected, state, now, notifications=True,
                reader=lambda path: self.result(now))
        self.assertEqual(entry['deliveries']['FAILED']['status'], 'SUPERSEDED')
        self.assertEqual(entry['deliveries']['RECOVERED']['incident'], 1)

    def test_clock_rollback_fails_closed(self):
        with self.assertRaises(common.HealthError):
            collector.collect(monitor_config(), {'last_collection': 2000}, 1000)

    def test_new_failure_cancels_unsent_recovery_from_previous_incident(self):
        state = {}
        selected = monitor_config(True)
        for now in (1000, 1300):
            collector.collect(selected, state, now, notifications=True,
                reader=lambda path: self.result(now, 'FAILED'))
        for now in range(1600, 5500, 300):
            collector.collect(selected, state, now, notifications=True,
                reader=lambda path: self.result(now))
        entry = state['sources']['upstream-generation']
        entry['deliveries']['FAILED']['status'] = 'SENT'
        recovery = entry['deliveries']['RECOVERED']
        recovery.update(status='NOT_SENT', attempts=1, retry_after=6000)
        for now in (5500, 5800):
            collector.collect(selected, state, now, notifications=True,
                reader=lambda path: self.result(now, 'FAILED'))
        messages = []
        for delivery in entry['deliveries'].values():
            common.deliver(delivery, 6100,
                lambda delivery: messages.append((delivery['event'], delivery['incident'])), lambda: None)
        self.assertEqual(recovery['status'], 'SUPERSEDED')
        self.assertEqual(messages, [('FAILED', 2)])

    def test_new_failure_preserves_accepted_and_uncertain_recovery_records(self):
        for delivery_status in ('NEW', 'NOT_SENT', 'ATTEMPTED', 'SENT', 'UNCERTAIN', 'EXHAUSTED'):
            with self.subTest(delivery_status=delivery_status):
                recovery = {'status': delivery_status, 'incident': 1}
                state = {'sources': {'upstream-generation': {
                    'sequence': 1, 'sample_at': 5200, 'latest_status': 'HEALTHY',
                    'deliveries': {'RECOVERED': recovery}}}}
                collector.collect(monitor_config(True), state, 5500, notifications=True,
                    reader=lambda path: self.result(5500, 'FAILED'))
                expected = 'SUPERSEDED' if delivery_status in ('NEW', 'NOT_SENT') else delivery_status
                self.assertEqual(recovery['status'], expected)

    def test_unknown_or_loading_cannot_send_a_pending_old_recovery(self):
        for status in ('OBSERVER_ERROR', 'LOADING'):
            with self.subTest(status=status):
                recovery = {'status': 'NOT_SENT', 'incident': 1, 'attempts': 1}
                state = {'sources': {'upstream-generation': {
                    'sequence': 1, 'sample_at': 5200, 'latest_status': 'HEALTHY',
                    'deliveries': {'RECOVERED': recovery}}}}
                collector.collect(monitor_config(True), state, 5500, notifications=True,
                    reader=lambda path: self.result(5500, status))
                common.deliver(recovery, 6000, lambda value: self.fail(), lambda: None)
                self.assertEqual(recovery['status'], 'SUPERSEDED')

    def test_recovery_supersession_matrix_for_service_and_observer(self):
        delivery_states = ('NEW', 'NOT_SENT', 'ATTEMPTED', 'SENT', 'UNCERTAIN', 'EXHAUSTED', 'SUPERSEDED')
        for layer in ('service', 'observer'):
            for delivery_status in delivery_states:
                with self.subTest(layer=layer, delivery_status=delivery_status):
                    failure = {'status': delivery_status, 'incident': 1, 'event': 'FAILED'}
                    target = {'incident': 1, 'sequence': 1, 'sample_at': 5140,
                        'healthy_since': 1600, 'healthy_samples': 12,
                        'deliveries': {'FAILED': failure}}
                    entry = target if layer == 'service' else {'observer': target}
                    state = {'sources': {'upstream-generation': entry}}
                    collector.collect(monitor_config(True), state, 5200, notifications=True,
                        reader=lambda path: self.result(5200))
                    expected = 'SUPERSEDED' if delivery_status in ('NEW', 'NOT_SENT') else delivery_status
                    self.assertEqual(failure['status'], expected)
                    self.assertEqual(target['deliveries']['RECOVERED']['incident'], 1)

    def test_shadow_resume_matches_current_incident_delivery_matrix(self):
        delivery_states = ('NEW', 'NOT_SENT', 'ATTEMPTED', 'SENT', 'UNCERTAIN', 'EXHAUSTED', 'SUPERSEDED')
        for layer in ('service', 'observer'):
            for delivery_status in delivery_states:
                for previous_incident in (1, 2):
                    with self.subTest(layer=layer, delivery_status=delivery_status,
                                      previous_incident=previous_incident):
                        previous = {'status': delivery_status, 'incident': previous_incident,
                                    'event': 'FAILED', 'target': 'upstream-generation'}
                        target = {'incident': 2, 'sequence': 2, 'sample_at': 5500,
                                  'deliveries': {'FAILED': previous}}
                        entry = target if layer == 'service' else {'observer': target}
                        state = {'sources': {'upstream-generation': entry}}
                        status = 'FAILED' if layer == 'service' else 'OBSERVER_ERROR'
                        collector.collect(monitor_config(True), state, 5500, notifications=True,
                            reader=lambda path: self.result(5500, status))
                        current = target['deliveries']['FAILED']
                        self.assertEqual(current['incident'], 2)
                        if previous_incident == 2:
                            self.assertIs(current, previous)
                            self.assertEqual(current['status'], delivery_status)
                        else:
                            self.assertEqual(current['status'], 'NEW')

    def test_notification_config_failure_then_recovery_never_sends_stale_failure(self):
        state = {}
        selected = monitor_config(True)
        for now in (1000, 1300):
            collector.collect(selected, state, now, notifications=True,
                reader=lambda path: self.result(now, 'FAILED'))
        entry = state['sources']['upstream-generation']
        self.assertEqual(entry['deliveries']['FAILED']['status'], 'NEW')
        for now in range(1600, 5500, 300):
            collector.collect(selected, state, now, notifications=True,
                reader=lambda path: self.result(now))
        messages = []
        for delivery in entry['deliveries'].values():
            common.deliver(delivery, 5500,
                lambda delivery: messages.append((delivery['event'], delivery['incident'])), lambda: None)
        self.assertEqual(messages, [('RECOVERED', 1)])

    def test_shadow_incident_after_sent_episode_is_reported_on_resume(self):
        directory = self.root / 'state'
        messages = []
        sender = lambda delivery: messages.append((delivery['event'], delivery['incident']))
        for now in (1000, 1300):
            collector.run(monitor_config(True), directory, now=now,
                reader=lambda path: self.result(now, 'FAILED'), sender=sender)
        for now in range(1600, 5500, 300):
            collector.run(monitor_config(True), directory, now=now,
                reader=lambda path: self.result(now), sender=sender)
        for now in (5500, 5800):
            collector.run(monitor_config(), directory, now=now,
                reader=lambda path: self.result(now, 'FAILED'), sender=sender)
        collector.run(monitor_config(True), directory, now=5860,
            reader=lambda path: self.result(5800, 'FAILED'), sender=sender)
        self.assertEqual(messages, [('FAILED', 1), ('RECOVERED', 1), ('FAILED', 2)])

    def test_recovery_while_notifications_disabled_cancels_pending_failure_matrix(self):
        for layer in ('service', 'observer'):
            for delivery_status in ('NEW', 'NOT_SENT'):
                with self.subTest(layer=layer, delivery_status=delivery_status):
                    pending = {'status': delivery_status, 'incident': 1, 'attempts': 0,
                               'event': 'FAILED', 'target': 'upstream-generation'}
                    target = {'incident': 1, 'sequence': 1, 'sample_at': 5140,
                        'healthy_since': 1600, 'healthy_samples': 12,
                        'deliveries': {'FAILED': pending}}
                    entry = target if layer == 'service' else {'observer': target}
                    state = {'sources': {'upstream-generation': entry}}
                    collector.collect(monitor_config(), state, 5200, notifications=False,
                        reader=lambda path: self.result(5200))
                    collector.collect(monitor_config(True), state, 5260, notifications=True,
                        reader=lambda path: self.result(5260))
                    self.assertEqual(pending['status'], 'SUPERSEDED')
                    common.deliver(pending, 5300, lambda value: self.fail(), lambda: None)


if __name__ == '__main__':
    unittest.main()
