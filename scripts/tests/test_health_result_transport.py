#!/usr/bin/env python3
"""Offline restricted reader/importer schema, identity, clocks and process bounds."""
import importlib.util
import hashlib
import io
import json
import os
import signal
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import PropertyMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import llm_health as common
import health_result_transport as transport


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reader = load('health_reader', 'health-result-reader.py')
importer = load('health_importer', 'health-result-importer.py')
collector = load('health_collector_for_import', 'service-health-monitor.py')
SECRET = 'fixture-original-body-private-value'


def report(source='upstream-generation', now=1000):
    return {'version': 1, 'source': source, 'kind': 'generation', 'observed_at': now,
            'status': 'HEALTHY', 'reason': 'ANSWER_VERIFIED', 'answer_correct': True,
            'completion_tokens': 2, 'prompt_tokens': 18, 'latency_seconds': 0.1}


def bundle(now=1400):
    return json.dumps({'version': 1, 'generated_at': now,
                       'reports': {'upstream-generation': report()}}).encode()


def policy(now=1400):
    return {'kind': 'selfhost-only-diagnostic-identity-v1', 'valid': True,
        'gateway_origin': 'https://llm.example.com', 'model': 'self-hosted-general',
        'key_sha256': hashlib.sha256(SECRET.encode()).hexdigest(), 'server_policy_sha256': 'a' * 64,
        'issued_at': now, 'expires_at': now + 180, 'student_account': False,
        'server_enforced': {'operations': ['chat'], 'maximum_paid_credits': 0,
            'maximum_daily_tokens': 64000, 'requests_per_minute': 1, 'maximum_concurrency': 1,
            'expires_at': now + 86400, 'effective_models': ['self-hosted-general'],
            'fallback': None, 'route': 'SELF_HOSTED'},
        'probe_enforced': {'maximum_output_tokens': 8, 'maximum_daily_requests': 312,
            'minimum_interval_seconds': 300, 'total_timeout_seconds': 20}}


class ResultTests(unittest.TestCase):
    def test_policy_projection_is_consumer_compatible_without_secret_fields(self):
        raw = policy()
        raw['raw_password'] = SECRET
        raw['server_enforced']['opaque_runtime_dump'] = SECRET
        raw['probe_enforced']['raw_key'] = SECRET
        safe = transport.sanitize_policy(raw, 1400)
        # The public canary no longer consumes this receipt; the projection is
        # still the transport's contract for whatever carries it.
        self.assertEqual(safe, transport.sanitize_policy(safe, 1400))
        self.assertEqual((safe['issued_at'], safe['expires_at']), (1400, 1580))
        self.assertNotIn(SECRET, json.dumps(safe))

    def test_policy_reader_uses_only_two_fixed_roles_and_paths(self):
        for role, expected_path in (
                ('gateway-host', '/var/lib/pickle-public-policy/attestation.json'),
                ('collector-host', '/var/lib/pickle-health-import-gateway/policy-attestation.json')):
            paths = []
            def fetch(path):
                paths.append(str(path))
                if 'summary.json' in str(path):
                    return {'version': 1, 'observed_at': 1400,
                            'sources': {'api': {'status': 'HEALTHY', 'sample_at': 1400}}}
                return policy()
            value = reader.collect({'role': role, 'expected_hostname': socket.gethostname(),
                'include_policy_attestation': True}, now=1400, reader=fetch, runner=lambda *a, **k: bundle())
            self.assertIn(expected_path, paths)
            self.assertEqual(value['policy_attestation'], policy())
        for role in ('model-host', 'gateway-container', 'public-observer'):
            with self.subTest(role=role), self.assertRaises(common.HealthError):
                reader.collect({'role': role, 'expected_hostname': socket.gethostname(),
                    'include_policy_attestation': True}, now=1400)

    def test_policy_missing_at_reader_is_immediate_safe_invalid(self):
        def unreadable(path):
            raise OSError(SECRET)
        value = reader.collect({'role': 'gateway-host', 'expected_hostname': socket.gethostname(),
            'include_policy_attestation': True}, now=1400, reader=unreadable, runner=lambda *a, **k: bundle())
        self.assertIs(value['policy_attestation']['valid'], False)
        self.assertNotIn(SECRET, json.dumps(value))

    def test_policy_two_hop_import_preserves_original_validity(self):
        selected = dict(self.config, include_policy_attestation=True)
        document = json.loads(bundle())
        document['policy_attestation'] = policy()
        first = self.root / 'gateway-import'
        importer.run(selected, first, now=1400, runner=lambda *a, **k: json.dumps(document).encode())
        receipt = common.read_json(first / 'policy-attestation.json')
        forwarded = reader.collect({'role': 'collector-host', 'expected_hostname': socket.gethostname(),
            'include_policy_attestation': True}, now=1430,
            reader=lambda path: receipt if path.name == 'policy-attestation.json' else {
                'version': 1, 'observed_at': 1400, 'sources': {'api': {'status': 'HEALTHY', 'sample_at': 1400}}})
        second = self.root / 'collector-import'
        importer.run(dict(selected, expected_sources=['collector-heartbeat']), second, now=1430,
                     runner=lambda *a, **k: json.dumps(forwarded).encode())
        copied = common.read_json(second / 'policy-attestation.json')
        self.assertEqual(copied, policy())
        self.assertEqual(copied['issued_at'], 1400)

    def test_policy_import_failure_matrix_invalidates_previous_permission(self):
        for failure in ('missing', 'expired', 'shape', 'health', 'ssh', 'identity'):
            with self.subTest(failure=failure):
                directory = self.root / ('failure-' + failure)
                common.private_directory(directory)
                common.atomic_json(directory / 'policy-attestation.json', policy())
                selected = dict(self.config, include_policy_attestation=True)
                value = json.loads(bundle())
                value['policy_attestation'] = policy()
                if failure == 'missing':
                    value.pop('policy_attestation')
                elif failure == 'expired':
                    value['policy_attestation'] = policy(1000)
                elif failure == 'shape':
                    value['policy_attestation']['server_enforced']['fallback'] = 'external'
                elif failure == 'health':
                    value['reports'] = {}
                elif failure == 'identity':
                    selected['identity_file'] = str(self.root / 'missing-key')
                def execute(*args, **kwargs):
                    self.assertIs(common.read_json(directory / 'policy-attestation.json')['valid'], False)
                    if failure == 'ssh':
                        raise common.HealthError('fixture transport failed')
                    return json.dumps(value).encode()
                result = importer.run(selected, directory, now=1400, runner=execute)
                invalid = common.read_json(directory / 'policy-attestation.json')
                self.assertIs(invalid['valid'], False)
                self.assertEqual(result['status'], 'OBSERVER_ERROR')
                self.assertNotIn(SECRET, json.dumps(invalid))

    def test_policy_invalid_projection_and_opt_in_bounds(self):
        value = {'kind': 'selfhost-only-diagnostic-identity-v1', 'valid': False,
                 'reason': 'QUOTA_EXHAUSTED', 'issued_at': 1400, 'expires_at': 1400,
                 'raw_key': SECRET, 'server_enforced': {'raw': SECRET}}
        safe = transport.sanitize_policy(value, 1400)
        self.assertEqual(set(safe), {'kind', 'valid', 'reason', 'issued_at', 'expires_at'})
        document = json.loads(bundle())
        document['policy_attestation'] = value
        with self.assertRaises(common.HealthError):
            transport.inspect_envelope(json.dumps(document).encode(), ['upstream-generation'], 1400)
        document['policy_attestation'] = policy()
        document['padding'] = 'x' * common.MAX_JSON
        with self.assertRaises(common.HealthError):
            transport.inspect_envelope(json.dumps(document).encode(), ['upstream-generation'], 1400,
                                       include_policy_attestation=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.key = self.root / 'identity'
        self.pins = self.root / 'known_hosts'
        for path in (self.key, self.pins):
            path.write_text('fixture-protected-value')
            path.chmod(0o600)
        self.config = {'source': 'import-upstream', 'address': '198.18.0.20', 'user': 'health-reader',
            'port': 22, 'identity_file': str(self.key), 'known_hosts_file': str(self.pins),
            'expected_sources': ['upstream-generation'], 'expected_hostname': socket.gethostname()}

    def test_ssh_has_fixed_protocol_host_pins_and_no_ambient_authority(self):
        command = importer.ssh_command(self.config)
        self.assertEqual(command[:3], ['ssh', '-F', '/dev/null'])
        self.assertEqual(command[-2:], ['198.18.0.20', 'read-health-v1'])
        for required in ('BatchMode=yes', 'IdentitiesOnly=yes', 'StrictHostKeyChecking=yes',
                         'IdentityAgent=none', 'ControlPath=none', 'ClearAllForwardings=yes',
                         'ProxyCommand=none', 'ProxyJump=none', 'ForwardAgent=no'):
            self.assertIn(required, command)
        self.assertNotIn('fixture-protected-value', ' '.join(command))

    def test_untrusted_ssh_selections_and_path_permissions_are_rejected(self):
        for key, value in (('address', 'host.example.com'), ('user', 'root;id'),
                           ('port', True), ('expected_sources', ['../secret'])):
            selected = dict(self.config, **{key: value})
            with self.subTest(key=key), self.assertRaises(common.HealthError):
                importer.ssh_command(selected)
        self.key.chmod(0o644)
        with self.assertRaises(common.HealthError):
            importer.ssh_command(self.config)

    def test_import_preserves_remote_sample_time_and_strips_raw_body(self):
        document = json.loads(bundle())
        document['reports']['upstream-generation'].update(prompt=SECRET, token=SECRET, response_body=SECRET)
        directory = self.root / 'state'
        result = importer.run(self.config, directory, now=1400,
                              runner=lambda argv, **kwargs: json.dumps(document).encode())
        imported = common.read_json(directory / 'reports/upstream-generation.json')
        self.assertEqual(result['status'], 'HEALTHY')
        self.assertEqual(imported['observed_at'], 1000)
        self.assertNotIn(SECRET, json.dumps(imported))

    def test_invalid_bundle_never_overwrites_last_valid_result(self):
        directory = self.root / 'state'
        importer.run(self.config, directory, now=1400, runner=lambda *a, **k: bundle())
        previous = (directory / 'reports/upstream-generation.json').read_bytes()
        value = json.loads(bundle(now=1460))
        value['reports']['upstream-generation']['observed_at'] = 2000
        result = importer.run(self.config, directory, now=1460,
                              runner=lambda *a, **k: json.dumps(value).encode())
        self.assertEqual(result['status'], 'OBSERVER_ERROR')
        self.assertEqual((directory / 'reports/upstream-generation.json').read_bytes(), previous)

    def test_bundle_timestamp_source_size_and_numeric_validation(self):
        values = [b'x' * (common.MAX_JSON + 1), bundle(now=1)]
        wrong = json.loads(bundle())
        wrong['reports']['unexpected'] = wrong['reports'].pop('upstream-generation')
        values.append(json.dumps(wrong).encode())
        wrong = json.loads(bundle())
        wrong['reports']['upstream-generation']['latency_seconds'] = float('nan')
        values.append(json.dumps(wrong).encode())
        for value in values:
            with self.subTest(value_length=len(value)), self.assertRaises((common.HealthError, ValueError)):
                transport.inspect_bundle(value, ['upstream-generation'], 1400)

    def test_reader_only_uses_fixed_model_paths(self):
        paths = []
        def fetch(path):
            paths.append(str(path))
            return report('host-query' if 'host-query' in str(path) else 'local-generation')
        value = reader.collect({'role': 'model-host', 'expected_hostname': socket.gethostname()},
                               now=1400, reader=fetch)
        self.assertEqual(set(paths), {'/var/lib/pickle-llm-host-query/result.json',
                                    '/var/lib/pickle-llm-local-generation/result.json'})
        self.assertEqual(value['reports']['local-generation']['observed_at'], 1000)

    def test_reader_unavailability_is_observer_error_not_service_failure(self):
        def unavailable(path):
            raise OSError(SECRET)
        value = reader.collect({'role': 'gateway-container', 'expected_hostname': socket.gethostname()},
                               now=1400, reader=unavailable)
        self.assertEqual(value['reports']['upstream-generation']['status'], 'OBSERVER_ERROR')
        self.assertNotIn(SECRET, json.dumps(value))

    def test_gateway_host_exec_is_exact_container_and_command(self):
        commands = []
        def execute(argv, **kwargs):
            commands.append(argv)
            return bundle()
        reader.collect({'role': 'gateway-host', 'expected_hostname': socket.gethostname()},
                       now=1400, runner=execute)
        self.assertEqual(commands[0][:4], ['/usr/sbin/pct', 'exec', '203', '--'])
        self.assertEqual(commands[0][-1], '--local-child')
        self.assertNotIn('/bin/sh', commands[0])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process groups; Mac empty-group killpg returns EPERM')
    def test_gateway_absolute_sbin_fixture_runs_with_restricted_path(self):
        executable = self.root / 'sbin/pct'
        executable.parent.mkdir()
        executable.write_text('#!/bin/sh\n[ "$PATH" = /usr/bin:/bin ] || exit 72\nprintf %s ' +
                              "'" + bundle().decode().replace("'", "'\\''") + "'\n")
        executable.chmod(0o700)
        self.assertNotIn(str(executable.parent), '/usr/bin:/bin')
        with self.assertRaises(FileNotFoundError):
            transport.bounded_process(['pct'], timeout=1)
        def actual_process(argv, **kwargs):
            self.assertEqual(argv[0], '/usr/sbin/pct')
            return transport.bounded_process([str(executable), *argv[1:]], **kwargs)
        result = reader.collect({'role': 'gateway-host', 'expected_hostname': socket.gethostname()},
                                now=1400, runner=actual_process)
        self.assertEqual(result['reports']['upstream-generation']['observed_at'], 1000)

    def test_exited_leader_with_child_holding_stdout_is_killed_on_timeout(self):
        pid_file = self.root / 'child.pid'
        program = ('import os,time\n'
            'child=os.fork()\n'
            'if child:\n os._exit(0)\n'
            'with open(' + repr(str(pid_file)) + ',"w") as handle:handle.write(str(os.getpid()))\n'
            'time.sleep(10)\n')
        child = None
        try:
            with self.assertRaisesRegex(common.HealthError, 'TIMEOUT'):
                transport.bounded_process([sys.executable, '-c', program], timeout=0.2)
            child = int(pid_file.read_text())
            import subprocess
            status = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not status or status.startswith('Z'), 'owned descendant still runs')
        finally:
            if child is None and pid_file.exists():
                child = int(pid_file.read_text())
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_successful_leader_cannot_leave_child_with_closed_standard_streams(self):
        pid_file = self.root / 'closed-stream-child.pid'
        marker = self.root / 'unexpected-child-marker'
        program = ('import os,time\n'
            'child=os.fork()\n'
            'if child:\n'
            ' with open(' + repr(str(pid_file)) + ',"w") as handle:handle.write(str(child))\n'
            ' print("{}",flush=True);os._exit(0)\n'
            'os.close(1);os.close(2)\n'
            'time.sleep(0.3)\n'
            'with open(' + repr(str(marker)) + ',"w") as handle:handle.write("child survived")\n'
            'time.sleep(10)\n')
        child = None
        try:
            result = transport.bounded_process([sys.executable, '-c', program], timeout=2)
            self.assertEqual(result.strip(), b'{}')
            time.sleep(0.45)
            self.assertFalse(marker.exists(), 'owned child survived successful leader exit')
            if pid_file.exists():
                child = int(pid_file.read_text())
                import subprocess
                status = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not status or status.startswith('Z'), 'owned descendant still runs')
        finally:
            if child is None and pid_file.exists():
                child = int(pid_file.read_text())
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_public_classification_is_rejected_with_a_documentation_address(self):
        address = importer.ipaddress.ip_address('198.51.100.10')
        # Python classifies documentation networks as private. Override only
        # that property to cover the public branch without a live IP or DNS.
        with patch.object(type(address), 'is_private', new_callable=PropertyMock, return_value=False):
            with patch.object(importer, 'private_read', side_effect=AssertionError('credential read')):
                with self.assertRaisesRegex(common.HealthError, 'IMPORT_SSH_SELECTION_INVALID'):
                    importer.ssh_command(dict(self.config, address='198.51.100.10'))

    def test_shared_overlay_range_is_explicit_unicast_not_public_or_link_local(self):
        for address in ('100.64.0.10', '198.18.0.10'):
            with self.subTest(address=address):
                self.assertIn(address, importer.ssh_command(dict(self.config, address=address)))
        # MCAST-TEST-NET is reserved for documentation-only multicast cases.
        rejected = (socket.inet_ntoa(bytes((169, 254, 10, 10))),
                    socket.inet_ntoa(bytes((233, 252, 0, 1))), '0.0.0.0', '255.255.255.255')
        for address in rejected:
            with self.subTest(address=address), self.assertRaises(common.HealthError):
                importer.ssh_command(dict(self.config, address=address))

    def test_ssh_original_command_must_be_exact_protocol(self):
        config_path = self.root / 'reader.json'
        common.atomic_json(config_path, {'role': 'gateway-container', 'expected_hostname': socket.gethostname()})
        for original in ('', 'read-health-v1; id', 'read-health-v1 extra', '/bin/sh'):
            output = io.StringIO()
            with patch.object(sys, 'argv', ['reader', '--config', str(config_path)]), \
                 patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': original}), patch.object(sys, 'stdout', output), \
                 patch.object(reader, 'collect', side_effect=AssertionError('must not collect')):
                self.assertEqual(reader.main(), 2)
            self.assertNotIn(original or SECRET, output.getvalue())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process groups; Mac empty-group killpg returns EPERM')
    def test_bounded_child_rejects_size_timeout_and_discards_stderr(self):
        with self.assertRaisesRegex(common.HealthError, 'TOO_LARGE'):
            transport.bounded_process([sys.executable, '-c', 'print("x"*65537)'], timeout=2)
        started = time.monotonic()
        with self.assertRaisesRegex(common.HealthError, 'TIMEOUT'):
            transport.bounded_process([sys.executable, '-c', 'import time;time.sleep(10)'], timeout=0.1)
        self.assertLess(time.monotonic() - started, 0.7)
        value = transport.bounded_process([sys.executable, '-c',
            'import sys;sys.stderr.write(' + repr(SECRET) + ');print("{}")'], timeout=2)
        self.assertEqual(value.strip(), b'{}')

    def test_collector_heartbeat_preserves_epoch_and_service_fault_is_not_observer_fault(self):
        summary = {'version': 1, 'observed_at': 1000,
                   'sources': {'upstream-generation': {'status': 'FAILED', 'sample_at': 1000}}}
        result = reader.collect({'role': 'collector-host', 'expected_hostname': socket.gethostname()},
                                now=1400, reader=lambda path: summary)
        heartbeat = result['reports']['collector-heartbeat']
        self.assertEqual((heartbeat['status'], heartbeat['observed_at']), ('HEALTHY', 1000))
        source = {'name': 'collector-heartbeat', 'path': '/var/lib/fixture/heartbeat.json',
                  'freshness_seconds': 180, 'failure_count': 3}
        status, timestamp, reason = collector.sample(source, 1400, reader=lambda path: heartbeat)
        self.assertEqual(status, 'OBSERVER_ERROR')

    def test_collector_summary_unreadable_or_bad_structure_is_observer_error(self):
        bad = [None, {'version': 1, 'observed_at': 1000, 'sources': {}},
               {'version': 1, 'observed_at': 2000, 'sources': {'api': {'status': 'FAILED', 'sample_at': 1000}}},
               {'version': 1, 'observed_at': 1000, 'sources': {'../secret': {'status': 'FAILED', 'sample_at': 1000}}}]
        for summary in bad:
            with self.subTest(summary=summary):
                result = reader.collect({'role': 'collector-host', 'expected_hostname': socket.gethostname()},
                    now=1400, reader=lambda path: summary)
                self.assertEqual(result['reports']['collector-heartbeat']['status'], 'OBSERVER_ERROR')
        def unreadable(path):
            raise OSError(SECRET)
        result = reader.collect({'role': 'collector-host', 'expected_hostname': socket.gethostname()},
                                now=1400, reader=unreadable)
        self.assertEqual(result['reports']['collector-heartbeat']['status'], 'OBSERVER_ERROR')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_heartbeat_role_host_and_source_ids_are_fixed(self):
        summary = {'version': 1, 'observed_at': 1000,
                   'sources': {'api': {'status': 'HEALTHY', 'sample_at': 1000}}}
        paths = []
        def fetch(path):
            paths.append(str(path))
            return summary if path.name == 'summary.json' else report('public-' + path.parent.name.removeprefix('pickle-public-'))
        result = reader.collect({'role': 'public-observer', 'expected_hostname': socket.gethostname(),
            'include_collector_heartbeat': True}, now=1400, reader=fetch)
        self.assertEqual(set(result['reports']), {'public-console', 'public-api', 'public-development', 'collector-heartbeat'})
        self.assertIn('/var/lib/pickle-service-health-monitor/summary.json', paths)
        with self.assertRaises(common.HealthError):
            reader.collect({'role': 'collector-host', 'expected_hostname': 'wrong-host'}, now=1400)
        selected = dict(self.config, expected_sources=['collector-heartbeat'])
        document = json.dumps({'version': 1, 'generated_at': 1400,
            'reports': {'collector-heartbeat': result['reports']['collector-heartbeat']}}).encode()
        importer.run(selected, self.root / 'heartbeat-import', now=1400, runner=lambda *a, **k: document)
        imported = common.read_json(self.root / 'heartbeat-import/reports/collector-heartbeat.json')
        self.assertEqual(imported['observed_at'], 1000)

    def test_tls_export_paths_and_kind_survive_import(self):
        paths = []
        def fetch(path):
            paths.append(str(path))
            if path.parent.name == 'pickle-public-tls-production':
                return {'version': 1, 'source': 'public-tls-production', 'kind': 'tls',
                    'observed_at': 1000, 'status': 'FAILED', 'reason': 'TLS_CERTIFICATE_EXPIRING'}
            return report('public-' + path.parent.name.removeprefix('pickle-public-'))
        result = reader.collect({'role': 'public-observer', 'expected_hostname': socket.gethostname(),
            'tls_sources': ['public-tls-production']}, now=1400, reader=fetch)
        self.assertIn('/var/lib/pickle-public-tls-production/result.json', paths)
        tls = result['reports']['public-tls-production']
        self.assertEqual((tls['kind'], tls['status']), ('tls', 'FAILED'))
        selected = dict(self.config, expected_sources=list(result['reports']))
        importer.run(selected, self.root / 'tls-import', now=1400,
                     runner=lambda *a, **k: json.dumps(result).encode())
        imported = common.read_json(self.root / 'tls-import/reports/public-tls-production.json')
        self.assertEqual(imported['observed_at'], 1000)
        self.assertEqual(imported['reason'], 'TLS_CERTIFICATE_EXPIRING')


if __name__ == '__main__':
    unittest.main()
