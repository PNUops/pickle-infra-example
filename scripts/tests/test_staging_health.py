"""Offline staging response, TLS, warning severity and incident boundaries."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import llm_health as common
import health_result_transport as wire


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load('staging_probe', 'public-service-health-probe.py')
collector = load('staging_collector', 'service-health-monitor.py')
alerts = load('staging_alerts', 'discord-ops-alerts.py')
reader = load('staging_reader', 'health-result-reader.py')
# Synthetic address-classification fixtures, unrelated to operating hosts.
PRIVATE_INGRESS = '.'.join(map(str, (10, 0, 0, 10)))
LINK_LOCAL = '.'.join(map(str, (169, 254, 1, 1)))


def config(kind='public-http'):
    names = {'public-http': 'public-staging', 'tls': 'public-tls-staging',
             'tls-expiry-warning': 'public-staging-expiry-warning', 'access-policy': 'staging-access-policy'}
    value = {'source': names[kind], 'kind': kind, 'expected_hostname': socket.gethostname(),
        'allowed_hosts': ['staging.example.com'], 'required_dns_san': 'staging.example.com',
        'url': 'https://staging.example.com/__ingress_probe', 'expected_status': 200,
        'minimum_certificate_seconds': 604800, 'response': {'kind': 'text', 'exact': 'candidate-ready'}}
    if kind in ('tls', 'tls-expiry-warning'):
        value['url'] = 'https://staging.example.com/'
    if kind == 'tls-expiry-warning':
        value.update(severity='warning', minimum_certificate_seconds=2592000)
    if kind == 'access-policy':
        value.update(connect_address=PRIVATE_INGRESS, expected_status=403, response={'kind': 'status'})
    return value


class StagingTests(unittest.TestCase):
    def test_serial_unit_attempts_every_fixed_target_despite_a_previous_cli_failure(self):
        text = (ROOT.parent / 'hosts/llm-monitor/systemd/pickle-public-observation.service').read_text()
        self.assertIn('Type=oneshot\n', text)
        self.assertIn('TimeoutStartSec=110\n', text)
        starts = [line for line in text.splitlines() if line.startswith('ExecStart=')]
        names = ('console', 'api', 'development', 'tls-production', 'tls-development',
                 'tls-gateway', 'staging', 'tls-staging', 'staging-expiry-warning')
        self.assertEqual(starts, ['ExecStart=/usr/bin/timeout --signal=TERM --kill-after=5s 100s '
                                 '/usr/bin/bash /usr/local/libexec/pickle/public-observation-batch.sh'])
        directories = next(line for line in text.splitlines() if line.startswith('StateDirectory=')).split('=', 1)[1].split()
        self.assertEqual(directories, ['pickle-public-' + name for name in names])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / 'attempts.jsonl'
            fake = directory / 'probe.py'
            fake.write_text('import json,sys\nfrom pathlib import Path\n'
                'name=Path(sys.argv[sys.argv.index("--config")+1]).stem\n'
                'with Path(' + repr(str(log)) + ').open("a") as f:f.write(json.dumps(name)+"\\n")\n'
                'raise SystemExit(2 if name=="public-api" else 0)\n')
            batch = directory / 'batch.sh'
            original = (ROOT / 'public-observation-batch.sh').read_text()
            self.assertEqual(original.count('/usr/bin/python3'), 1)
            self.assertEqual(original.count('/usr/local/libexec/pickle/public-service-health-probe.py'), 1)
            batch.write_text(original.replace('/usr/bin/python3', sys.executable).replace(
                '/usr/local/libexec/pickle/public-service-health-probe.py', str(fake)))
            result = subprocess.run(['bash', str(batch)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                             ['public-' + name for name in names])
            self.assertIn('attempted=9 failed=1', result.stdout)

    def test_outer_timeout_bounds_the_batch_process_group(self):
        timeout = shutil.which('timeout') or shutil.which('gtimeout')
        if timeout is None: self.skipTest('GNU timeout is required for the process-group fixture')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pids = directory / 'pids.json'
            fake = directory / 'probe.py'
            fake.write_text('import json,os,subprocess,sys,time\nfrom pathlib import Path\n'
                'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\n'
                'Path(' + repr(str(pids)) + ').write_text(json.dumps([os.getpid(),child.pid]))\n'
                'time.sleep(30)\n')
            original = (ROOT / 'public-observation-batch.sh').read_text()
            batch = directory / 'batch.sh'
            batch.write_text(original.replace('/usr/bin/python3', sys.executable).replace(
                '/usr/local/libexec/pickle/public-service-health-probe.py', str(fake)))
            started = time.monotonic()
            result = subprocess.run([timeout, '--signal=TERM', '--kill-after=0.2s', '0.5s', 'bash', str(batch)],
                                    capture_output=True, text=True, timeout=3)
            self.assertIn(result.returncode, (124, -signal.SIGKILL, 128 + signal.SIGKILL))
            self.assertLess(time.monotonic() - started, 2)
            for pid in json.loads(pids.read_text()):
                state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith('Z'), 'a batch descendant survived the deadline')

    def exercise(self, selected, *, days=40, status=200, body=b'candidate-ready', names=None, broken=False):
        calls = {}
        class Connection:
            def __init__(self, host, port, **kwargs):
                calls['tls_host'] = host
                self.sock = self
                assert kwargs['context'].verify_mode == ssl.CERT_REQUIRED and kwargs['context'].check_hostname
            def connect(self):
                if broken: raise TimeoutError()
            def getpeercert(self):
                return {'notAfter': time.strftime('%b %d %H:%M:%S %Y GMT', time.gmtime(time.time() + days * 86400)),
                        'subjectAltName': [('DNS', name) for name in (names or ['staging.example.com'])]}
            def settimeout(self, value): pass
            def request(self, method, path, body, headers): calls.update(method=method, path=path)
            def getresponse(self): return self
            @property
            def status(self): return status
            def getheader(self, *args): return 'text/plain; charset=utf-8'
            def read(self, limit): return body
            def close(self): pass
        def lookup(host, *args, **kwargs):
            calls['lookup_host'] = host
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PRIVATE_INGRESS, 443))]
        with patch.object(probe.http.client, 'HTTPSConnection', Connection), patch.object(probe.socket, 'getaddrinfo', lookup):
            return probe.http_worker({'config': selected}), calls

    def test_positive_requires_exact_plain_body_and_rejects_denial_redirect_and_html(self):
        selected = config()
        self.assertEqual(self.exercise(selected)[0]['status'], 'HEALTHY')
        for status in (301, 302, 403, 500):
            self.assertEqual(self.exercise(selected, status=status)[0]['status'], 'FAILED')
        for body in (b'candidate-ready\n', b'<html>candidate-ready</html>', b'other'):
            with self.assertRaises(common.HealthError): self.exercise(selected, body=body)
        with self.assertRaises(common.HealthError):
            probe.inspect_response(selected, 200, 'text/html', b'candidate-ready')

    def test_private_negative_uses_original_tls_host_and_only_403_passes(self):
        selected = config('access-policy')
        result, calls = self.exercise(selected, status=403)
        self.assertEqual(result['reason'], 'ACCESS_POLICY_VERIFIED')
        self.assertEqual((calls['lookup_host'], calls['tls_host'], calls['method']),
                         (PRIVATE_INGRESS, 'staging.example.com', 'GET'))
        for status in (200, 401, 404, 302):
            self.assertEqual(self.exercise(selected, status=status)[0]['reason'], 'ACCESS_POLICY_VIOLATION')
        self.assertEqual(self.exercise(selected, broken=True)[0]['status'], 'OBSERVER_ERROR')
        for address in ('127.0.0.1', LINK_LOCAL, '198.51.100.1', '203.0.113.10', '::1'):
            with self.assertRaises(common.HealthError): probe.validate(dict(selected, connect_address=address))
        for kind in ('public-http', 'tls', 'public-generation'):
            with self.assertRaises(common.HealthError): probe.validate(dict(config(), kind=kind, connect_address=PRIVATE_INGRESS))

    def test_exact_san_is_not_satisfied_by_a_wildcard(self):
        for kind in ('public-http', 'tls', 'access-policy'):
            self.assertEqual(self.exercise(config(kind), names=['*.example.com'])[0]['reason'], 'TLS_CERTIFICATE_VALIDATION_FAILED')
        self.assertEqual(self.exercise(config('tls-expiry-warning'), names=['*.example.com'])[0]['status'], 'OBSERVER_ERROR')

    def test_warning_and_critical_thresholds_are_separate_and_warning_never_downgrades_critical(self):
        for days, expected in ((40, 'HEALTHY'), (29, 'FAILED'), (6, 'FAILED')):
            result, _ = self.exercise(config('tls-expiry-warning'), days=days)
            self.assertEqual((result['status'], result['severity']), (expected, 'warning'))
        self.assertEqual(self.exercise(config('tls'), days=29)[0]['status'], 'HEALTHY')
        self.assertEqual(self.exercise(config('tls'), days=6)[0]['reason'], 'TLS_CERTIFICATE_EXPIRING')
        for field, value in (('minimum_certificate_seconds', 604800), ('severity', 'critical'), ('required_dns_san', None)):
            with self.assertRaises(common.HealthError): probe.validate(dict(config('tls-expiry-warning'), **{field: value}))

    def test_warning_is_explicit_through_transport_collector_and_discord_recovery(self):
        source = {'name': 'expiry-warning', 'path': '/fixture/result.json', 'report_source': 'public-staging-expiry-warning',
                  'expected_kind': 'tls-expiry-warning', 'severity': 'warning', 'freshness_seconds': 180, 'failure_count': 1}
        selected = {'notifications_enabled': False, 'sources': [source]}
        producer, consumer = {}, alerts.new_state()
        destination = {'name': 'expiry-warning', 'label': '인증서 만료 예고', 'type': 'service_health',
            'path': '/fixture/state.json', 'summary_path': '/fixture/summary.json', 'source_name': 'expiry-warning',
            'severity': 'warning', 'max_age': 180}
        base = {'version': 1, 'source': 'public-staging-expiry-warning', 'kind': 'tls-expiry-warning', 'severity': 'warning'}
        def sample(status, now):
            raw = dict(base, observed_at=now, status=status,
                       reason='TLS_CERTIFICATE_EXPIRY_WARNING' if status == 'FAILED' else 'TLS_EXPIRY_WARNING_CLEAR')
            safe = wire.sanitize_report(raw, base['source'], now)
            summary = collector.collect(selected, producer, now, reader=lambda path: deepcopy(safe))
            observed = alerts.collect_service_health(destination, now, snapshot=(deepcopy(producer), summary))
            for dimension, result in observed.items():
                alerts.enqueue_service_health(consumer, {'host_label': '시험 서버'}, destination, dimension, result, now)
            return summary
        sample('HEALTHY', 1000)
        sample('FAILED', 1060)
        sample('FAILED', 1120)
        self.assertEqual(len(consumer['outbox']), 1)
        self.assertIn('] 주의\n', consumer['outbox'][0]['content'])
        self.assertNotIn('장애 감지', consumer['outbox'][0]['content'])
        consumer['outbox'][0]['delivery'] = 'SENT'
        for now in range(1180, 4781, 60): summary = sample('HEALTHY', now)
        self.assertEqual(len(consumer['outbox']), 2)
        self.assertIn('주의 해소', consumer['outbox'][1]['content'])
        wrong = dict(destination)
        wrong.pop('severity')
        self.assertEqual(alerts.collect_service_health(wrong, 4780, snapshot=(producer, summary))['condition']['status'], 'UNKNOWN')

    def test_unlabelled_warning_or_forged_severity_never_becomes_an_ordinary_failure(self):
        raw = {'version': 1, 'source': 'public-staging-expiry-warning', 'kind': 'tls-expiry-warning',
               'observed_at': 1000, 'status': 'FAILED', 'severity': 'warning', 'reason': 'TLS_CERTIFICATE_EXPIRY_WARNING'}
        wrong = {'name': raw['source'], 'path': '/fixture', 'freshness_seconds': 180, 'failure_count': 3}
        self.assertEqual(collector.sample(wrong, 1000, reader=lambda path: raw)[0], 'OBSERVER_ERROR')
        for change in ({'severity': 'critical'}, {'source': 'public-api'}, {'kind': 'public-http'}):
            with self.assertRaises(common.HealthError): wire.sanitize_report(dict(raw, **change), dict(raw, **change)['source'], 1000)
        policy = dict(raw, source='public-api', kind='access-policy', reason='ACCESS_POLICY_VIOLATION')
        policy.pop('severity')
        with self.assertRaises(common.HealthError): wire.sanitize_report(policy, 'public-api', 1000)

    def test_warning_mail_uses_attention_subject_and_observer_mail_keeps_fault_subject(self):
        messages = []
        class SMTP:
            def __init__(self, *args, **kwargs): pass
            def starttls(self, **kwargs): pass
            def login(self, *args): pass
            def send_message(self, message): messages.append(message)
            def close(self): pass
        mail = {'host': 'smtp.example.com', 'port': 587, 'tls_mode': 'starttls',
                'username': 'fixture', 'password_file': '/fixture/password',
                'sender': 'sender@example.com', 'recipient': 'operator@example.com'}
        with patch.object(common, 'private_read', return_value=b'fixture-only'), patch.object(common.smtplib, 'SMTP', SMTP):
            sender = common.smtp_sender(mail)
            sender({'event': 'FAILED', 'target': 'expiry-warning', 'incident': 1, 'severity': 'warning'})
            sender({'event': 'RECOVERED', 'target': 'expiry-warning', 'incident': 1, 'severity': 'warning'})
            sender({'event': 'FAILED', 'target': 'observer-expiry-warning', 'incident': 1})
        self.assertTrue(str(messages[0]['Subject']).endswith(' 주의'))
        self.assertTrue(str(messages[1]['Subject']).endswith(' 주의 해소'))
        self.assertTrue(str(messages[2]['Subject']).endswith(' 장애'))

    def test_reader_staging_opt_in_uses_only_fixed_paths_and_preserves_warning_severity(self):
        paths = []
        def fetch(path):
            paths.append(str(path))
            if 'expiry-warning' not in str(path): raise FileNotFoundError()
            return {'version': 1, 'source': 'public-staging-expiry-warning', 'kind': 'tls-expiry-warning',
                    'observed_at': 1000, 'status': 'FAILED', 'severity': 'warning', 'reason': 'TLS_CERTIFICATE_EXPIRY_WARNING'}
        selected = {'role': 'public-observer', 'expected_hostname': socket.gethostname(),
                    'include_staging': True, 'tls_sources': ['public-tls-staging']}
        value = reader.collect(selected, now=1000, reader=fetch)
        self.assertEqual(value['reports']['public-staging-expiry-warning']['severity'], 'warning')
        self.assertIn('/var/lib/pickle-public-staging/result.json', paths)
        self.assertIn('/var/lib/pickle-public-tls-staging/result.json', paths)
        with self.assertRaises(common.HealthError):
            reader.collect(dict(selected, role='model-host'), now=1000, reader=fetch)


if __name__ == '__main__':
    unittest.main()
