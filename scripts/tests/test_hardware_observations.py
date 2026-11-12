#!/usr/bin/env python3
"""Automatic hardware facts, optional health isolation and complete delivery."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import hardware_observation as hardware
import llm_health as common


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reader = load('hardware_health_reader', 'health-result-reader.py')
poster = load('hardware_poster', 'platform-hardware-report.py')


def asset():
    return {'displayModel': 'Example compute server', 'cpuModel': 'Example CPU',
            'cores': 4, 'threads': 8, 'memoryMiB': 16384, 'gpus': []}


def observation(stamp=1000):
    return {'version': 1, 'observed_at': stamp, 'status': 'OBSERVED', 'asset': asset()}


def health_bundle(aux=None):
    value = {'version': 1, 'generated_at': 1000, 'reports': {'upstream-generation': {
        'version': 1, 'source': 'upstream-generation', 'kind': 'generation',
        'observed_at': 999, 'status': 'HEALTHY', 'reason': 'ANSWER_VERIFIED'}}}
    if aux is not None:
        value['hardware'] = aux
    return json.dumps(value).encode()


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sys = self.root / 'sys'
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.write(self.proc / 'meminfo', 'MemTotal:       16777216 kB\n')
        self.write(self.sys / 'class/dmi/id/product_name', 'Example server\n')
        self.write(self.sys / 'class/dmi/id/sys_vendor', 'Example vendor\n')
        self.write(self.sys / 'devices/system/cpu/present', '0-3\n')
        for cpu in range(4):
            self.write(self.sys / f'devices/system/cpu/cpu{cpu}/topology/thread_siblings_list', str(cpu))
        (self.sys / 'bus/pci/devices').mkdir(parents=True)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o600)

    def run_probe(self, runner=None):
        return hardware.probe(sys_root=self.sys, proc_root=self.proc, runner=runner or self.runner)

    def runner(self, argv, **_):
        self.calls.append(argv)
        if argv == ['lscpu', '-J']:
            return json.dumps({'lscpu': [{'field': 'Model name:', 'data': 'Example fast cores'},
                {'field': 'Model name:', 'data': 'Example efficient cores'}]}).encode()
        if argv == ['lspci', '-Dmm']:
            return b'0000:7f:00.0 "VGA compatible controller" "Example vendor" "Example accelerator"\n'
        raise FileNotFoundError('fixture-only missing NVML')

    def gpu(self, vendor='0x10de', klass='0x030000'):
        self.write(self.sys / 'bus/pci/devices/0000:7f:00.0/vendor', vendor)
        self.write(self.sys / 'bus/pci/devices/0000:7f:00.0/class', klass)

    def test_hybrid_cpu_uses_thread_groups_without_model_capacity_constants(self):
        measured = self.run_probe()
        self.assertEqual((measured['cores'], measured['threads'], measured['memoryMiB']), (4, 4, 16384))
        self.assertEqual(measured['cpuModel'], 'Example fast cores / Example efficient cores')
        self.assertEqual(measured['gpus'], [])
        self.assertEqual(self.calls, [['lscpu', '-J']])

    def test_smt_thread_pairs_count_once_and_changed_memory_is_observed(self):
        for cpu in range(4):
            self.write(self.sys / f'devices/system/cpu/cpu{cpu}/topology/thread_siblings_list',
                       '0,2' if cpu % 2 == 0 else '1,3')
        self.write(self.proc / 'meminfo', 'MemTotal:       33554432 kB\n')
        measured = self.run_probe()
        self.assertEqual((measured['cores'], measured['threads'], measured['memoryMiB']), (2, 4, 32768))

    def test_bmc_display_is_not_an_accelerator(self):
        for vendor in ('0x102b', '0x1a03'):
            with self.subTest(vendor=vendor):
                self.gpu(vendor)
                self.assertEqual(self.run_probe()['gpus'], [])

    def test_vfio_gpu_remains_present_with_unknown_memory(self):
        self.gpu()
        measured = self.run_probe()
        self.assertEqual(measured['gpus'], [{'model': 'Example accelerator', 'count': 1,
            'dedicatedMemoryMiB': None, 'unifiedMemoryMiB': None}])

    def test_nvml_uses_observed_memory_and_normalizes_pci_domain(self):
        self.gpu()
        def run(argv, **kwargs):
            if argv[0] == 'nvidia-smi':
                return b'00000000:7f:00.0, Example observed GPU, 24576\n'
            return self.runner(argv, **kwargs)
        gpu = self.run_probe(run)['gpus'][0]
        self.assertEqual((gpu['model'], gpu['dedicatedMemoryMiB']), ('Example observed GPU', 24576))
        self.assertIsNone(gpu['unifiedMemoryMiB'])

    def test_missing_pci_or_cpu_facts_do_not_become_zero_capacity(self):
        self.gpu()
        (self.sys / 'bus/pci/devices/0000:7f:00.0/class').unlink()
        value = hardware.observe(now=1000, observer=self.run_probe)
        self.assertEqual(value, hardware.unavailable(1000))
        self.write(self.sys / 'devices/system/cpu/present', '0-999999999')
        self.assertEqual(hardware.observe(now=1000, observer=self.run_probe), hardware.unavailable(1000))

    def test_typed_observation_rejects_stale_future_and_raw_metadata(self):
        for value in (observation(1), observation(1100), dict(observation(), raw_secret='fixture-private')):
            with self.subTest(value=value), self.assertRaises(hardware.HardwareError):
                hardware.validate_observation(value, 1000)

    def ssh_config(self):
        self.write(self.root / 'identity', 'fixture-key-not-real')
        self.write(self.root / 'known-hosts', 'fixture-pin-not-real')
        return {'address': '198.18.0.20', 'port': 22, 'user': 'observer',
            'identity_file': str(self.root / 'identity'), 'known_hosts_file': str(self.root / 'known-hosts')}

    def test_disabled_reader_is_legacy_byte_identical_and_does_not_probe(self):
        config = {'role': 'gateway-host', 'expected_hostname': socket.gethostname()}
        legacy = reader.collect(config, now=1000, runner=lambda *a, **k: health_bundle())
        disabled = reader.collect(dict(config, allow_hardware=False), now=1000,
            runner=lambda *a, **k: health_bundle())
        self.assertEqual(json.dumps(legacy, sort_keys=True), json.dumps(disabled, sort_keys=True))

    def test_health_read_never_executes_optional_hardware_probe(self):
        with patch.object(hardware, 'observe') as probe:
            isolated = load('hardware_timing_reader', 'health-result-reader.py')
            value = isolated.collect({'role': 'gateway-host', 'expected_hostname': socket.gethostname(),
                'include_hardware': True, 'allow_hardware': True}, now=1000,
                runner=lambda *a, **k: health_bundle())
        probe.assert_not_called()
        self.assertNotIn('hardware', value)

    def test_hardware_read_failure_is_separate_and_redacts_raw_error(self):
        config = {'role': 'gateway-host', 'expected_hostname': socket.gethostname(), 'allow_hardware': True}
        def failed(**_):
            raise OSError('fixture-private-value')
        value = reader.collect_hardware(config, now=1000, observer=failed)
        self.assertEqual(value, hardware.unavailable(1000))
        self.assertNotIn('fixture-private-value', json.dumps(value))

    def test_hardware_reader_is_opt_in_and_does_not_run_health_query(self):
        config = {'role': 'hardware-host', 'expected_hostname': socket.gethostname(), 'allow_hardware': True}
        self.assertEqual(reader.collect_hardware(config, now=1000, observer=lambda **_: observation()), observation())
        for changed in (dict(config, allow_hardware=False), dict(config, role='public-observer'),
                        dict(config, expected_hostname='wrong-host')):
            with self.subTest(changed=changed), self.assertRaises(common.HealthError):
                reader.collect_hardware(changed, now=1000)
        with self.assertRaises(common.HealthError):
            reader.collect(config, now=1000)

    def test_reader_dispatch_accepts_only_two_fixed_protocols(self):
        config = {'role': 'hardware-host', 'expected_hostname': socket.gethostname(), 'allow_hardware': True}
        for command, expected in (('read-hardware-v1', 0), ('read-hardware-v1 extra', 2), ('id', 2)):
            with self.subTest(command=command), patch.object(sys, 'argv', ['reader', '--config', 'fixture']), \
                    patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command}), \
                    patch.object(reader, 'read_json', return_value=config), \
                    patch.object(reader, 'collect_hardware', return_value=observation()) as selected, \
                    patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(reader.main(), expected)
                self.assertEqual(selected.call_count, int(expected == 0))

    def poster_config(self):
        return {'schemaVersion': 1, 'enabled': True,
            'source_id': '00000000-0000-4000-8000-000000000201',
            'expected_hostname': socket.gethostname(),
            'api_url': 'https://api.example.invalid/internal/platform-hardware-observations',
            'credential_file': str(self.root / 'not-read-by-parent'),
            'assets': [{'id': 'local', 'local': True},
                       {'id': 'remote', 'ssh': self.ssh_config()}]}

    def test_complete_batch_uses_oldest_actual_timestamp_and_fixed_roster(self):
        sent = []
        def send(_, body):
            value = json.loads(body); sent.append(value)
            return {'status': 200, 'ack': {'sourceId': value['sourceId'], 'observedAt': value['observedAt']}}
        result = poster.report(self.poster_config(), self.root / 'config', self.root / 'poster', now=1000,
            local_observer=lambda **_: observation(1000),
            ssh_runner=lambda *a, **k: json.dumps(observation(900)).encode(), sender=send)
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(sent[0]['observedAt'], '1970-01-01T00:15:00.000Z')
        self.assertEqual([a['id'] for a in sent[0]['assets']], ['local', 'remote'])

    def test_incomplete_or_stale_batch_never_posts_or_falls_back(self):
        for index, value in enumerate((hardware.unavailable(1000), observation(1), {'raw_secret': 'fixture-private'})):
            result = poster.report(self.poster_config(), self.root / 'config', self.root / ('p' + str(index)),
                now=1000, local_observer=lambda **_: observation(),
                ssh_runner=lambda *a, **k: json.dumps(value).encode(),
                sender=lambda *args: self.fail('Incomplete snapshots must never be sent'))
            self.assertEqual(result['status'], 'INCOMPLETE')

    def test_four_remote_reads_are_parallel_and_use_separate_fixed_command(self):
        config = self.poster_config()
        config['assets'] = [{'id': 'local', 'local': True}] + [
            {'id': 'remote' + str(index), 'ssh': self.ssh_config()} for index in range(4)]
        together = threading.Barrier(4)
        def read_remote(argv, **limits):
            self.assertEqual(argv[-1], 'read-hardware-v1')
            self.assertEqual(limits, {'timeout': 12, 'maximum': 65536})
            together.wait(timeout=2)
            return json.dumps(observation()).encode()
        result = poster.report(config, self.root / 'config', self.root / 'parallel', now=1000,
            local_observer=lambda **_: observation(), ssh_runner=read_remote,
            sender=lambda *a: {'status': 409})
        self.assertEqual(result['status'], 'CURRENT')

    def test_hardware_ssh_retains_pins_and_no_ambient_authority(self):
        config = self.ssh_config()
        argv = poster.ssh_command(config)
        self.assertEqual(argv[-1], 'read-hardware-v1')
        for option in ('StrictHostKeyChecking=yes', 'IdentityAgent=none', 'ForwardAgent=no',
                       'ClearAllForwardings=yes', 'ProxyCommand=none', 'ProxyJump=none',
                       'RequestTTY=no', 'UserKnownHostsFile=' + config['known_hosts_file']):
            self.assertIn(option, argv)
        for changed in (dict(config, address='198.18.0.20;id'), dict(config, user='observer;id'),
                        dict(config, port=True), dict(config, identity_file='relative-key')):
            with self.subTest(changed=changed), self.assertRaises((hardware.HardwareError, common.HealthError)):
                poster.ssh_command(changed)

    def test_duplicate_is_benign_and_wrong_ack_is_not_success(self):
        for index, (reply, expected) in enumerate((({'status': 409}, 'CURRENT'),
                ({'status': 200, 'ack': {'sourceId': 'wrong', 'observedAt': 'bad'}}, 'ERROR'),
                ({'status': 302}, 'ERROR'))):
            result = poster.report(self.poster_config(), self.root / 'config', self.root / ('r' + str(index)),
                now=1000, local_observer=lambda **_: observation(),
                ssh_runner=lambda *a, **k: json.dumps(observation()).encode(), sender=lambda *a: reply)
            self.assertEqual(result['status'], expected)

    def test_disabled_reporter_does_not_read_credentials_or_observe(self):
        config = dict(self.poster_config(), enabled=False)
        result = poster.report(config, self.root / 'config', self.root / 'disabled',
            local_observer=lambda **_: self.fail('disabled probe'), sender=lambda *a: self.fail('disabled sender'))
        self.assertEqual(result, {'status': 'DISABLED'})

    def test_endpoint_is_https_exact_path_without_redirect_or_userinfo(self):
        for value in ('http://api.example.invalid/internal/platform-hardware-observations',
                      'https://api.example.invalid/api/v1/internal/platform-hardware-observations',
                      'https://token@api.example.invalid/internal/platform-hardware-observations',
                      'https://api.example.invalid/internal/platform-hardware-observations?q=secret'):
            with self.subTest(value=value), self.assertRaises(hardware.HardwareError):
                poster.endpoint(value)

    def test_http_worker_uses_private_credential_and_never_follows_redirect(self):
        token = 'fixture-hardware-token-not-real'
        self.write(self.root / 'api-token', token)
        config = dict(self.poster_config(), credential_file=str(self.root / 'api-token'))
        calls = []
        class Connection:
            def __init__(self):
                self.sock = self
            def connect(self):
                pass
            def settimeout(self, _):
                pass
            def request(self, method, path, **kwargs):
                calls.append((method, path, kwargs))
            def getresponse(self):
                return type('Response', (), {'status': 302})()
            def close(self):
                pass
        value = poster.http_worker(config, b'{}', connection_factory=lambda *a, **k: Connection())
        self.assertEqual(value, {'status': 302})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0:2], ('POST', '/internal/platform-hardware-observations'))
        self.assertEqual(calls[0][2]['headers']['Authorization'], 'Bearer ' + token)
        self.assertNotIn(token, json.dumps(value))

    def test_http_ack_cannot_export_unselected_response_fields(self):
        self.write(self.root / 'api-token', 'fixture-token')
        config = dict(self.poster_config(), credential_file=str(self.root / 'api-token'))
        class Connection:
            def __init__(self):
                self.sock = self
            def connect(self):
                pass
            def settimeout(self, _):
                pass
            def request(self, *a, **k):
                pass
            def getresponse(self):
                return type('Response', (), {'status': 200, 'read': lambda _, size: b'{"raw_secret":"fixture-private"}'})()
            def close(self):
                pass
        with self.assertRaises(hardware.HardwareError):
            poster.http_worker(config, b'{}', connection_factory=lambda *a, **k: Connection())

    def test_http_subprocess_has_deadline_and_no_bearer_in_arguments(self):
        result = subprocess.CompletedProcess([], 0, b'{"status":409}')
        with patch.object(poster.subprocess, 'run', return_value=result) as run:
            self.assertEqual(poster.send(self.root / 'config.json', b'{}'), {'status': 409})
            self.assertEqual(run.call_args.kwargs['timeout'], 10)
            self.assertEqual(run.call_args.kwargs['input'], b'{}')
            self.assertNotIn('Authorization', ' '.join(run.call_args.args[0]))
        with patch.object(poster.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 10)):
            with self.assertRaises(hardware.HardwareError):
                poster.send(self.root / 'config.json', b'{}')

    def test_hardware_unit_has_its_own_deadline_without_health_ordering(self):
        units = ROOT.parent / 'hosts/llm-monitor/systemd'
        dropin = (units / 'pickle-service-health-monitor.service.d/40-platform-hardware.conf').read_text()
        self.assertEqual(dropin, '[Unit]\nWants=pickle-platform-hardware.service\n')
        unit = (units / 'pickle-platform-hardware.service').read_text()
        self.assertIn('Type=oneshot\n', unit)
        self.assertIn('TimeoutStartSec=25\n', unit)
        self.assertNotIn('RemainAfterExit', unit)
        self.assertIn('TimeoutStartSec=120\n', (units / 'pickle-service-health-monitor.service').read_text())


if __name__ == '__main__':
    unittest.main()
