#!/usr/bin/env python3
"""Offline public-service expectations, canary custody and DNS deadlines."""
import importlib.util
import io
import json
from pathlib import Path
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
spec = importlib.util.spec_from_file_location('public_probe', ROOT / 'public-service-health-probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
SECRET = 'fixture-diagnostic-secret-never-output'


def config(kind='public-http'):
    value = {'source': 'public-api', 'kind': kind, 'expected_hostname': socket.gethostname(),
        'allowed_hosts': ['api.example.com'], 'url': 'https://api.example.com/actuator/health/readiness',
        'expected_status': 200, 'minimum_certificate_seconds': 604800,
        'response': {'kind': 'json', 'required': {'status': 'UP'}}}
    if kind == 'public-generation':
        value.update(source='public-generation', url='https://api.example.com/v1/chat/completions',
                     model='self-hosted-general', canary_enabled=False)
    elif kind == 'tls':
        value.update(source='public-tls-production', url='https://api.example.com/')
    return value


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)

    def prepared_canary(self):
        selected = config('public-generation')
        token = self.root / 'token'
        token.write_text(SECRET)
        token.chmod(0o600)
        selected.update(canary_enabled=True, credential_file=str(token))
        return selected

    def test_spa_html_cannot_pass_as_api_and_json_shape_is_exact(self):
        selected = config()
        for content_type, body in (('text/html', b'<div id="root"></div>'),
                                   ('application/json', b'{"status":"DOWN"}'),
                                   ('application/json', b'[]')):
            with self.subTest(content_type=content_type, body=body), self.assertRaises(common.HealthError):
                probe.inspect_response(selected, 200, content_type, body)
        self.assertEqual(probe.inspect_response(selected, 200, 'application/json', b'{"status":"UP"}')['status'], 'HEALTHY')

    def test_redirect_and_authentication_are_not_generation_proof(self):
        selected = config('public-generation')
        for status in (302, 401, 403, 502):
            result = probe.inspect_response(selected, status, 'application/json', b'{}')
            self.assertEqual((result['status'], result['http_status']),
                             ('OBSERVER_ERROR' if status in (401, 403) else 'FAILED', status))
            if status in (401, 403):
                self.assertEqual(result['reason'], 'CREDENTIAL_REJECTED')
                self.assertNotIn('completion_tokens', result)
        body = json.dumps({'choices': [{'message': {'content': '4'}, 'finish_reason': 'stop'}], 'usage': {}}).encode()
        self.assertEqual(probe.inspect_response(selected, 200, 'application/json', body)['status'], 'FAILED')

    def test_invalid_urls_and_missing_shapes_are_rejected(self):
        for url in ('http://api.example.com/', 'https://198.51.100.10/',
                    'https://other.example.com/', 'https://u:p@api.example.com/',
                    'https://api.example.com/?next=secret'):
            selected = config()
            selected['url'] = url
            with self.subTest(url=url), self.assertRaises(common.HealthError):
                probe.validate(selected)

    def test_canary_disabled_requires_no_key_and_makes_no_request(self):
        result = probe.probe(config('public-generation'), self.root / 'state', now=10000,
                             requester=lambda *args: self.fail())
        self.assertEqual(result['reason'], 'PUBLIC_CANARY_DISABLED')
        self.assertFalse((self.root / 'state' / 'result.json').exists())

    def test_canary_no_longer_reads_or_accepts_a_policy_receipt(self):
        # The canary runs whatever upstream the model is routed to (paid
        # included), so a receipt naming the self-hosted route must not gate it,
        # and a config that still names one is refused rather than ignored.
        selected = self.prepared_canary()
        directory = self.root / 'state'
        sent = []
        result = probe.probe(selected, directory, now=10000,
                             requester=lambda config, token: sent.append(token) or {'status': 'HEALTHY'})
        self.assertEqual((result['status'], sent), ('HEALTHY', [SECRET]))
        self.assertEqual(common.read_json(directory / 'budget.json')['submissions'], 1)
        selected['policy_attestation'] = str(self.root / 'policy.json')
        with self.assertRaises(common.HealthError):
            probe.validate(selected)

    def test_reasoning_budget_exhaustion_is_not_an_answer(self):
        selected = config('public-generation')
        def body(content, finish, completion):
            return json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': finish}],
                               'usage': {'prompt_tokens': 19, 'completion_tokens': completion}}).encode()
        cases = [(body(None, 'length', 16), 'FAILED'),          # measured at an 8-token cap
                 (body('4', 'stop', 17), 'HEALTHY'),           # measured at a 64-token cap
                 (body('4', 'stop', probe.MAXIMUM_OUTPUT_TOKENS), 'HEALTHY'),
                 (body('4', 'stop', probe.MAXIMUM_OUTPUT_TOKENS + 1), 'FAILED'),
                 (body('4', 'length', 128), 'FAILED')]
        for document, status in cases:
            with self.subTest(document=document):
                self.assertEqual(probe.inspect_response(selected, 200, 'application/json', document)['status'],
                                 status)

    def test_canary_submission_is_reserved_before_failure_and_restart(self):
        selected = self.prepared_canary()
        directory = self.root / 'state'
        def fail_request(config, token):
            self.assertEqual(common.read_json(directory / 'budget.json')['submissions'], 1)
            self.assertEqual(token, SECRET)
            raise RuntimeError('simulated process stop')
        with self.assertRaises(RuntimeError):
            probe.probe(selected, directory, now=10000, requester=fail_request)
        result = probe.probe(selected, directory, now=10001, requester=lambda *args: self.fail())
        self.assertEqual(result['status'], 'BUDGET_LIMIT')
        self.assertEqual(common.read_json(directory / 'budget.json')['submissions'], 1)

    def test_daily_cap_and_clock_rollback_never_submit(self):
        selected = self.prepared_canary()
        directory = self.root / 'state'
        common.private_directory(directory)
        common.atomic_json(directory / 'budget.json', {'day': '1970-01-01', 'submissions': 312})
        self.assertEqual(probe.probe(selected, directory, now=10000)['status'], 'BUDGET_LIMIT')
        with self.assertRaises(common.HealthError):
            probe.probe(selected, directory, now=9999)

    def test_tls_validation_failure_has_no_secret_output(self):
        class Connection:
            def __init__(self, *args, **kwargs):
                context = kwargs['context']
                if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                    raise AssertionError('TLS validation weakened')
            def connect(self):
                raise ssl.SSLCertVerificationError(SECRET)
            def close(self):
                pass
        output = io.StringIO()
        with patch.object(probe.http.client, 'HTTPSConnection', Connection), \
             patch.object(probe.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps({'config': config(), 'token': SECRET}))), \
             patch.object(sys, 'stdout', output):
            probe.worker_main()
        self.assertEqual(json.loads(output.getvalue())['reason'], 'TLS_CERTIFICATE_VALIDATION_FAILED')
        self.assertNotIn(SECRET, output.getvalue())

    def test_real_child_dns_stall_is_killed_at_total_deadline(self):
        worker = self.root / 'stalled_dns.py'
        worker.write_text('import socket,time,runpy\n'
            'socket.getaddrinfo=lambda *args,**kwargs:time.sleep(10)\n'
            'entry=runpy.run_path(' + repr(str(ROOT / 'public-service-health-probe.py')) + ')\n'
            'entry["worker_main"]()\n')
        real_run = subprocess.run
        def local_child(argv, **kwargs):
            return real_run([sys.executable, '-I', '-B', str(worker)], **kwargs)
        started = time.monotonic()
        with patch.object(probe.subprocess, 'run', local_child):
            result = probe.transport(config(), timeout=0.2)
        self.assertEqual(result['reason'], 'DNS_OBSERVATION_UNAVAILABLE')
        self.assertEqual((result['status'], result['phase'], result['target']), ('OBSERVER_ERROR', 'DNS', 'public-api'))
        self.assertLess(time.monotonic() - started, 0.8)

    def test_response_body_and_key_are_not_published(self):
        body = json.dumps({'choices': [{'message': {'content': SECRET}, 'finish_reason': 'stop'}],
                           'usage': {'prompt_tokens': 20, 'completion_tokens': 2}}).encode()
        result = probe.inspect_response(config('public-generation'), 200, 'application/json', body)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertFalse(result['answer_correct'])

    def test_generation_submission_and_tls_expiry_are_bounded_and_explicit(self):
        requests = []
        expiry = [time.time() + 30 * 86400]
        class Connection:
            def __init__(self, *args, **kwargs):
                self.sock = self
            def connect(self):
                pass
            def getpeercert(self):
                return {'notAfter': time.strftime('%b %d %H:%M:%S %Y GMT', time.gmtime(expiry[0]))}
            def settimeout(self, value):
                self.timeout = value
            def request(self, method, path, body, headers):
                requests.append((method, path, json.loads(body), headers))
            def getresponse(self):
                return self
            @property
            def status(self):
                return 200
            def getheader(self, *args):
                return 'application/json; charset=utf-8'
            def read(self, limit):
                return json.dumps({'choices': [{'message': {'content': '4'}, 'finish_reason': 'stop'}],
                    'usage': {'completion_tokens': 2, 'prompt_tokens': 18}}).encode()
            def close(self):
                pass
        with patch.object(probe.http.client, 'HTTPSConnection', Connection), \
             patch.object(probe.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]):
            result = probe.http_worker({'config': config('public-generation'), 'token': SECRET})
            expiry[0] = time.time() + 86400
            expiring = probe.http_worker({'config': config('public-generation'), 'token': SECRET})
        self.assertEqual(result['status'], 'HEALTHY')
        self.assertEqual(expiring['reason'], 'TLS_CERTIFICATE_EXPIRING')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][:2], ('POST', '/v1/chat/completions'))
        self.assertEqual(requests[0][2]['max_completion_tokens'], 128)
        self.assertEqual(requests[0][2], {'model': 'self-hosted-general',
            'messages': [{'role': 'user', 'content': 'What is 2+2? Reply with the digit only.'}],
            'temperature': 0, 'top_p': 1, 'max_completion_tokens': 128, 'stream': False})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_dns_lookup_failure_is_observer_error_with_target_and_phase(self):
        with patch.object(probe.socket, 'getaddrinfo', side_effect=socket.gaierror(SECRET)):
            result = probe.http_worker({'config': config(), 'token': None})
        self.assertEqual(result, {'status': 'OBSERVER_ERROR', 'reason': 'DNS_OBSERVATION_UNAVAILABLE',
                                  'phase': 'DNS', 'target': 'public-api'})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_tls_only_security_and_transport_failure_matrix(self):
        failure = [None]
        expires = [time.time() + 30 * 86400]
        calls = []
        class Connection:
            def __init__(self, *args, **kwargs):
                self.sock = self
            def connect(self):
                if failure[0]:
                    raise failure[0]
            def getpeercert(self):
                return {'notAfter': time.strftime('%b %d %H:%M:%S %Y GMT', time.gmtime(expires[0]))}
            def request(self, *args):
                calls.append(args)
                raise AssertionError('TLS-only probe must not issue HTTP')
            def close(self):
                pass
        selected = config('tls')
        with patch.object(probe.http.client, 'HTTPSConnection', Connection), \
             patch.object(probe.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]):
            for exception, status, reason in (
                    (None, 'HEALTHY', 'TLS_CERTIFICATE_VERIFIED'),
                    (ssl.SSLCertVerificationError('chain or SAN mismatch'), 'FAILED', 'TLS_CERTIFICATE_VALIDATION_FAILED'),
                    (ConnectionRefusedError(), 'OBSERVER_ERROR', 'TLS_OBSERVATION_UNAVAILABLE'),
                    (TimeoutError(), 'OBSERVER_ERROR', 'TLS_OBSERVATION_UNAVAILABLE'),
                    (ssl.SSLError('unavailable handshake'), 'OBSERVER_ERROR', 'TLS_OBSERVATION_UNAVAILABLE')):
                failure[0] = exception
                result = probe.http_worker({'config': selected})
                self.assertEqual((result['status'], result['reason']), (status, reason))
            failure[0] = None
            expires[0] = time.time() + 86400
            self.assertEqual(probe.http_worker({'config': selected})['reason'], 'TLS_CERTIFICATE_EXPIRING')
        self.assertEqual(calls, [])

    def test_tls_deadline_before_certificate_proof_is_only_observer_error(self):
        error = subprocess.TimeoutExpired([], 0.1, output=b'{"phase":"DNS"}\n{"phase":"TLS"}\n')
        with patch.object(probe.subprocess, 'run', side_effect=error):
            value = probe.transport(config('tls'), timeout=0.1)
        self.assertEqual((value['status'], value['reason']), ('OBSERVER_ERROR', 'TLS_OBSERVATION_UNAVAILABLE'))


if __name__ == '__main__':
    unittest.main()
