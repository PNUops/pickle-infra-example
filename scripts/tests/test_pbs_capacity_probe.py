#!/usr/bin/env python3
"""Exercise capacity response validation with synthetic data."""
import hashlib
import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

MODULE = Path(__file__).parents[1] / 'pbs-capacity-probe.py'
SPEC = importlib.util.spec_from_file_location('example_capacity_probe', MODULE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self, limit):
        return self.body


class FakeConnection:
    def __init__(self, cert, response):
        self.sock = SimpleNamespace(getpeercert=lambda binary_form: cert)
        self.response = response
        self.calls = []
        self.closed = False

    def connect(self):
        pass

    def request(self, method, path, headers):
        self.calls.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class CapacityProbeTests(unittest.TestCase):
    def test_token_rejects_trailing_whitespace_and_newline(self):
        valid = b'abcdefghijklmnop_123456'
        self.assertEqual(probe.token_value(valid), valid.decode())
        for raw in (valid + b' ', valid + b'\n', valid + b'\r\n'):
            with self.subTest(raw=raw), self.assertRaises(probe.ProbeFailure):
                probe.token_value(raw)

    def test_tls_pin_is_checked_before_request_and_status_route_is_exact(self):
        certificate = b'synthetic certificate bytes'
        fingerprint = hashlib.sha256(certificate).hexdigest()
        config = {'server': 'pbs.example.invalid', 'port': 8007,
                  'datastore': 'example-store', 'auth_id': 'capacity@pbs!probe',
                  'token': 'abcdefghijklmnop_123456',
                  'fingerprint': ':'.join(fingerprint[index:index + 2]
                                           for index in range(0, len(fingerprint), 2))}
        body = b'{"data":{"total":1000,"used":400,"avail":500}}'
        connection = FakeConnection(certificate, FakeResponse(200, body))
        with patch.object(probe.ssl, 'create_default_context', return_value=SimpleNamespace()), \
                patch.object(probe.http.client, 'HTTPSConnection', return_value=connection):
            self.assertEqual(probe.fetch_counters(config), (1000, 400, 500))
        self.assertEqual(connection.calls[0][0:2], (
            'GET', '/api2/json/admin/datastore/example-store/status?verbose=false'))
        self.assertTrue(connection.closed)

        bad_pin = dict(config, fingerprint='00:' * 31 + '00')
        rejected = FakeConnection(certificate, FakeResponse(200, body))
        with patch.object(probe.ssl, 'create_default_context', return_value=SimpleNamespace()), \
                patch.object(probe.http.client, 'HTTPSConnection', return_value=rejected):
            with self.assertRaisesRegex(probe.ProbeFailure, 'pin does not match'):
                probe.fetch_counters(bad_pin)
        self.assertEqual(rejected.calls, [])
        self.assertTrue(rejected.closed)

    def test_capacity_route_rejects_non_success_http_status(self):
        config = {'datastore': 'example-store'}
        requester = lambda _config, path: (403, b'{"errors":"denied"}')
        with self.assertRaisesRegex(probe.ProbeFailure, 'HTTP 403'):
            probe.fetch_counters(config, requester=requester)

    def test_accepts_complete_counters_and_ignores_known_metadata(self):
        self.assertEqual(probe.counters({'data': {'total': 1000, 'used': 400,
                                                  'avail': 500, 'gc-status': {}, 'store': 'example-store'}}, 'example-store'),
                         (1000, 400, 500))
        self.assertEqual(probe.counters({'data': {'total': 1000, 'used': 400,
                                                  'avail': 500, 'backend-type': 'filesystem'}},
                                        'example-store'), (1000, 400, 500))

    def test_rejects_missing_or_unexpected_fields(self):
        for response in ({'data': {'total': 1, 'used': 0}},
                         {'data': {'total': 1, 'used': 0, 'avail': 1, 'path': '/tmp'}},
                         *({'data': {'total': 1000, 'used': 400, 'avail': 500,
                                     'backend-type': kind}}
                           for kind in ('other', None, True, 1, {}))):
            with self.subTest(response=response), self.assertRaises(probe.ProbeFailure):
                probe.counters(response, 'example-store')

    def test_rejects_boolean_and_inconsistent_counts(self):
        for counts in ((100, True, 20), (100, 90, 20), (0, 0, 0)):
            value = dict(zip(('total', 'used', 'avail'), counts))
            with self.subTest(counts=counts), self.assertRaises(probe.ProbeFailure):
                probe.counters({'data': value}, 'example-store')


if __name__ == '__main__':
    unittest.main()
