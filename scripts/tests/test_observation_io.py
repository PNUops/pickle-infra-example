#!/usr/bin/env python3
"""Offline SSH, TLS, deadline and no-disclosure transport fixtures."""

import base64
from pathlib import Path
import ssl
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from observation_io import (IoError, MAX_ACK_BYTES, bounded_process, credential,
                            endpoint, http_worker, private_bytes, ssh_command)
from observation_runtime import fetch_metrics_worker


class FakeResponse:
    def __init__(self, status=200, body=b'{}'):
        self.status, self.body = status, body

    def read(self, maximum):
        return self.body[:maximum]


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False
        self.sock = self

    def connect(self):
        pass

    def settimeout(self, seconds):
        self.timeout = seconds

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ObservationIoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, name, value=b'dummy-material'):
        path = self.root / name
        path.write_bytes(value)
        path.chmod(0o600)
        return path

    def test_private_reader_rejects_symlink_hardlink_and_exposed_permissions(self):
        path = self.file('key')
        link = self.root / 'link'
        link.symlink_to(path)
        with self.assertRaisesRegex(IoError, '^PRIVATE_FILE_INVALID$'):
            private_bytes(link)
        path.chmod(0o644)
        with self.assertRaisesRegex(IoError, '^PRIVATE_FILE_INVALID$'):
            private_bytes(path)
        path.chmod(0o600)
        hard = self.root / 'hard'
        hard.hardlink_to(path)
        with self.assertRaisesRegex(IoError, '^PRIVATE_FILE_INVALID$'):
            private_bytes(path)

    def test_credential_rejects_embedded_newlines_without_echoing_it(self):
        path = self.file('token', b'dummy-token\n')
        self.assertEqual(credential(path), 'dummy-token')
        path.write_bytes(b'dummy-token\nSYNTHETIC_PRIVATE_CANARY')
        with self.assertRaises(IoError) as failure:
            credential(path)
        self.assertNotIn('CANARY', str(failure.exception))

    def test_fixed_ssh_command_keeps_pins_and_disables_ambient_features(self):
        config = {'address': '198.18.0.10', 'port': 22, 'user': 'reader',
                  'identity_file': str(self.file('identity')), 'known_hosts_file': str(self.file('pins'))}
        argv = ssh_command(config)
        self.assertEqual(argv[:3], ['ssh', '-F', '/dev/null'])
        self.assertEqual(argv[-2:], ['198.18.0.10', 'read-inference-observation-v1'])
        for option in ['StrictHostKeyChecking=yes', 'IdentitiesOnly=yes', 'IdentityAgent=none',
                       'ForwardAgent=no', 'ClearAllForwardings=yes', 'ControlMaster=no',
                       'ProxyCommand=none', 'ProxyJump=none', 'PermitLocalCommand=no', 'RequestTTY=no']:
            self.assertIn(option, argv)
        for name, value in [('address', 'example.invalid'), ('port', True), ('user', 'bad;command')]:
            with self.subTest(name=name):
                with self.assertRaises(IoError):
                    ssh_command({**config, name: value})

    def test_metrics_only_allows_fixed_loopback_get_path(self):
        for url in ['http://198.18.0.10:8000/metrics', 'http://127.0.0.1:8000/v1/models',
                    'http://127.0.0.1:8000/metrics?extra=1', 'https://127.0.0.1/metrics']:
            with self.subTest(url=url):
                with self.assertRaisesRegex(IoError, '^ENDPOINT_INVALID$'):
                    endpoint(url, metrics=True)

    def test_api_requires_https_fixed_path_and_no_credentials_or_redirect_parameters(self):
        for url in ['http://api.example.invalid/internal/inference-observations',
                    'https://user:dummy@api.example.invalid/internal/inference-observations',
                    'https://api.example.invalid/other',
                    'https://api.example.invalid/internal/inference-observations?token=dummy',
                    'https://api.example.invalid/internal/inference-observations#part']:
            with self.subTest(url=url):
                with self.assertRaisesRegex(IoError, '^ENDPOINT_INVALID$'):
                    endpoint(url)

    def worker(self, response):
        connection = FakeConnection(response)
        seen = []
        def factory(host, port, **options):
            seen.append((host, port, options))
            return connection
        request = {'url': 'https://api.example.invalid/internal/inference-observations',
                   'token': 'dummy-token', 'body': base64.b64encode(b'{"version":1}').decode()}
        return connection, seen, request, factory

    def test_http_worker_preserves_request_bytes_and_certificate_verification(self):
        connection, seen, request, factory = self.worker(FakeResponse(body=b'{"ack":"dummy"}'))
        result = http_worker(request, connection_factory=factory)
        context = seen[0][2]['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(connection.requests[0][0], ('POST', '/internal/inference-observations'))
        self.assertEqual(connection.requests[0][1]['body'], b'{"version":1}')
        self.assertEqual(result['ack'], {'ack': 'dummy'})
        self.assertTrue(connection.closed)

    def test_http_redirect_is_not_followed_and_error_body_is_not_returned(self):
        connection, _, request, factory = self.worker(FakeResponse(302, b'SYNTHETIC_PRIVATE_CANARY'))
        with self.assertRaisesRegex(IoError, '^HTTP_REDIRECT_DENIED$'):
            http_worker(request, connection_factory=factory)
        self.assertEqual(len(connection.requests), 1)
        self.assertTrue(connection.closed)
        _, _, request, factory = self.worker(FakeResponse(409, b'SYNTHETIC_PRIVATE_CANARY'))
        self.assertEqual(http_worker(request, connection_factory=factory), {'status': 409})

    def test_ack_output_and_json_are_bounded(self):
        for body in [b'x' * (MAX_ACK_BYTES + 1), b'{"x":1,"x":2}', b'{"x":NaN}']:
            with self.subTest(size=len(body)):
                _, _, request, factory = self.worker(FakeResponse(200, body))
                with self.assertRaises(IoError):
                    http_worker(request, connection_factory=factory)

    def test_metrics_get_returns_only_bounded_bytes_to_the_local_parser(self):
        connection = FakeConnection(FakeResponse(200, b'dummy_metric 1\n'))
        result = fetch_metrics_worker({'url': 'http://127.0.0.1:8000/metrics', 'token': None},
                                      connection_factory=lambda *args, **kwargs: connection)
        self.assertEqual(base64.b64decode(result['payload']), b'dummy_metric 1\n')
        self.assertEqual(connection.requests[0][0], ('GET', '/metrics'))
        self.assertTrue(connection.closed)

    def test_nonlinux_transport_refuses_before_starting_a_child(self):
        with patch('observation_io.sys.platform', 'darwin'), patch('observation_io.subprocess.Popen') as popen:
            with self.assertRaisesRegex(IoError, '^TRANSPORT_PLATFORM_UNSUPPORTED$'):
                bounded_process(['dummy-command'], timeout=2, maximum=32)
            popen.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process groups; Mac empty-group killpg returns EPERM')
    def test_bounded_process_uses_only_owned_dummy_children_and_caps_output(self):
        self.assertEqual(bounded_process([sys.executable, '-I', '-B', '-c', 'print("dummy")'],
                                         timeout=2, maximum=32), b'dummy\n')
        with self.assertRaisesRegex(IoError, '^TRANSPORT_TOO_LARGE$'):
            bounded_process([sys.executable, '-I', '-B', '-c', 'print("x"*10000)'], timeout=2, maximum=32)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process groups; Mac empty-group killpg returns EPERM')
    def test_bounded_process_total_timeout_and_stderr_do_not_escape(self):
        started = time.monotonic()
        with self.assertRaisesRegex(IoError, '^TRANSPORT_TIMEOUT$'):
            bounded_process([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(10)'],
                            timeout=0.1, maximum=32)
        self.assertLess(time.monotonic() - started, 2)
        with self.assertRaises(IoError) as failure:
            bounded_process([sys.executable, '-I', '-B', '-c',
                             'import sys; sys.stderr.write("SYNTHETIC_PRIVATE_CANARY"); sys.exit(1)'],
                            timeout=2, maximum=32)
        self.assertEqual(str(failure.exception), 'TRANSPORT_FAILED')


if __name__ == '__main__':
    unittest.main(verbosity=2)
