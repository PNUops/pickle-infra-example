#!/usr/bin/env python3
"""Offline tests for the candidate service-container bootstrap."""

from dataclasses import asdict, replace
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
MODULE = LIB / 'isolated_services.py'
SPEC = importlib.util.spec_from_file_location('isolated_services', MODULE)
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)


def config(root=Path('/root/fixture')):
    digest = 'a' * 64
    return service.Config(
        expected_node='compute-a', expected_cluster='example-cluster', bridge='infranet',
        subnet='198.18.0.0/16', gateway='198.18.0.1', mtu=1370,
        nameserver='192.0.2.53', storage='local-lvm', storage_reserve_gb=16,
        template='local:vztmpl/debian-13-standard_REVIEWED_amd64.tar.zst',
        template_sha256=digest, nginx_version='1.30.5-1~trixie',
        proxy_ctid=200, proxy_hostname='candidate-proxy', proxy_ip='198.18.1.10',
        proxy_cores=2, proxy_memory_mb=2048, proxy_disk_gb=16,
        sshgw_ctid=202, sshgw_hostname='candidate-sshgw', sshgw_ip='198.18.1.30',
        sshgw_cores=2, sshgw_memory_mb=1024, sshgw_disk_gb=8,
        api_ctid=201, api_ip='198.18.1.20', console_origin='https://console.example.ac.kr',
        candidate_id='11111111-2222-4333-8444-555555555555',
        proxy_env_file=str(root / 'proxy.env'), sshgw_env_file=str(root / 'sshgw.env'),
        forbidden_token_hashes_file=str(root / 'legacy-token-sha256.txt'),
        proxy_agent_file=str(root / 'proxy-agent'), proxy_agent_sha256=digest,
        proxy_unit_file=str(root / 'proxy.service'), proxy_unit_sha256=digest,
        proxy_nginx_file=str(root / 'proxy-nginx.conf'), proxy_nginx_sha256=digest,
        sshpiperd_archive_file=str(root / 'sshpiperd.tar.gz'),
        sshpiperd_archive_sha256=service.SSHPIPERD_ASSET_SHA256,
        sshgw_route_plugin_file=str(root / 'route-plugin'), sshgw_route_plugin_sha256=digest,
        sshgw_terminal_bridge_file=str(root / 'terminal-bridge'),
        sshgw_terminal_bridge_sha256=digest,
        sshpiperd_unit_file=str(root / 'sshpiperd.service'), sshpiperd_unit_sha256=digest,
        terminal_unit_file=str(root / 'terminal.service'), terminal_unit_sha256=digest,
        state_dir=str(root / 'state'))


class FakeRunner(service.Runner):
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.get(kwargs.get('label'), '')


class IsolatedServicesTest(unittest.TestCase):
    def test_owned_accepts_once_encoded_description_and_rejects_role_drift(self):
        bootstrap = service.Bootstrap(config(), FakeRunner())
        encoded = (f'hostname: candidate-proxy\n'
                   f'description: isolated-services%3A{bootstrap.run_id}%3Aproxy%0A\n')
        bootstrap.r.responses['proxy container identity'] = encoded
        bootstrap.owned('proxy')
        bootstrap.r.responses['proxy container identity'] = encoded.replace('proxy%0A', 'sshgw%0A')
        with self.assertRaisesRegex(service.BootstrapError, 'ownership changed'):
            bootstrap.owned('proxy')
        bootstrap.r.responses['proxy container identity'] = encoded
        bootstrap.manifest['created'] = [{'role': 'proxy', 'machine_id': 'a' * 32}]
        bootstrap.r.responses['proxy machine identity'] = 'b' * 32 + '\n'
        with self.assertRaisesRegex(service.BootstrapError, 'machine identity changed'):
            bootstrap.owned('proxy')

    def test_default_mode_is_plan_only_and_never_reads_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'config.json'
            path.write_text(json.dumps(asdict(config())))
            output = io.StringIO()
            with patch.object(sys, 'argv', ['bootstrap', '--config', str(path)]), \
                    patch.object(service, 'preflight', side_effect=AssertionError('host access')), \
                    patch.object(service.core, 'protected_file', side_effect=AssertionError('secret read')), \
                    patch.object(sys, 'stdout', output):
                self.assertEqual(service.main(), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result['mode'], 'dry-run')
            self.assertFalse(result['services']['enabled'])
            self.assertFalse(result['api_changed'])

    def test_config_rejects_wrong_mtu_overlap_and_old_sshpiperd(self):
        for bad in (replace(config(), mtu=1500),
                    replace(config(), sshgw_ip='198.18.1.10'),
                    replace(config(), sshpiperd_archive_sha256='b' * 64)):
            with self.subTest(bad=bad):
                with self.assertRaises(service.BootstrapError):
                    bad.validate()

    def test_input_parent_must_be_root_owned_mode_0700(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            path.chmod(0o755)
            with patch.object(service.os, 'geteuid', return_value=0):
                with self.assertRaisesRegex(service.BootstrapError, '0700'):
                    service.require_protected_parent(str(path / 'candidate.env'))

    def test_preflight_requires_existing_api_lxc_on_target_node(self):
        runner = FakeRunner({
            'cluster status': json.dumps([
                {'type': 'cluster', 'name': 'example-cluster', 'quorate': 1}]),
            'guest inventory': json.dumps([
                {'type': 'lxc', 'vmid': 201, 'node': 'compute-b'}]),
        })
        with patch.object(service.os, 'geteuid', return_value=0), \
                patch.object(service.socket, 'gethostname', return_value='compute-a'):
            with self.assertRaisesRegex(service.BootstrapError, 'API LXC'):
                service.preflight(config(), runner)

    def test_api_net0_requires_one_static_address_and_bridge(self):
        self.assertEqual(service.api_net0_identity(
            'hostname: api\nnet0: name=eth0,bridge=infranet,ip=198.18.1.20/16,mtu=1370\n'),
            ('infranet', '198.18.1.20'))
        for value in (
                'net0: name=eth0,bridge=infranet,bridge=other,ip=198.18.1.20/16\n',
                'net0: name=eth0,bridge=infranet,ip=198.18.1.20/16\nnet0: name=eth1,bridge=other,ip=198.18.2.20/16\n',
                'net0: name=eth0,bridge=infranet,ip=dhcp\n',
                'net0: name=eth0,bridge=infranet,ip=2001:db8::20/64\n'):
            with self.subTest(value=value), self.assertRaises(service.BootstrapError):
                service.api_net0_identity(value)

    def test_preflight_rejects_api_static_address_mismatch_before_inputs(self):
        runner = FakeRunner({
            'cluster status': json.dumps([
                {'type': 'cluster', 'name': 'example-cluster', 'quorate': 1}]),
            'guest inventory': json.dumps([
                {'type': 'lxc', 'vmid': 201, 'node': 'compute-a'}]),
            'API container network identity':
                'net0: name=eth0,bridge=infranet,ip=198.18.1.99/16,mtu=1370\n',
        })
        with patch.object(service.os, 'geteuid', return_value=0), \
                patch.object(service.socket, 'gethostname', return_value='compute-a'), \
                patch.object(service, 'read_inputs', side_effect=AssertionError('secret read')):
            with self.assertRaisesRegex(service.BootstrapError, 'does not match'):
                service.preflight(config(), runner)

    def test_candidate_tokens_are_separate_and_bound_to_plan(self):
        proxy = ('PICKLE_CANDIDATE_ID=11111111-2222-4333-8444-555555555555\n'
                 'PICKLE_PROXY_AGENT_TOKEN=' + 'p' * 32 + '\n').encode()
        sshgw = ('PICKLE_CANDIDATE_ID=11111111-2222-4333-8444-555555555555\n'
                 'PICKLE_SSHGW_TOKEN=' + 's' * 32 + '\n'
                 'PICKLE_TERMINAL_CONTROL_TOKEN=' + 't' * 32 + '\n').encode()
        proxy_values = service.parse_env(proxy, {'PICKLE_CANDIDATE_ID',
                                                  'PICKLE_PROXY_AGENT_TOKEN'})
        sshgw_values = service.parse_env(sshgw, {'PICKLE_CANDIDATE_ID', 'PICKLE_SSHGW_TOKEN',
                                                 'PICKLE_TERMINAL_CONTROL_TOKEN'})
        forbidden = {'f' * 64}
        service.validate_candidate_envs(config(), proxy_values, sshgw_values, forbidden)
        reused = sshgw.replace(b't' * 32, b's' * 32)
        reused_values = service.parse_env(reused, {'PICKLE_CANDIDATE_ID', 'PICKLE_SSHGW_TOKEN',
                                                   'PICKLE_TERMINAL_CONTROL_TOKEN'})
        with self.assertRaisesRegex(service.BootstrapError, 'independent fresh token'):
            service.validate_candidate_envs(config(), proxy_values, reused_values, forbidden)
        forbidden = {__import__('hashlib').sha256(('p' * 32).encode()).hexdigest()}
        with self.assertRaisesRegex(service.BootstrapError, 'legacy token'):
            service.validate_candidate_envs(config(), proxy_values, sshgw_values, forbidden)

    def test_artifact_architecture_rejects_host_native_or_text_files(self):
        elf = bytearray(64)
        elf[:6] = b'\x7fELF\x02\x01'
        elf[18:20] = (62).to_bytes(2, 'little')
        service.require_linux_amd64_elf(bytes(elf), 'fixture')
        with self.assertRaisesRegex(service.BootstrapError, 'Linux amd64'):
            service.require_linux_amd64_elf(b'#!/bin/sh\n', 'fixture')
        elf[18:20] = (183).to_bytes(2, 'little')
        with self.assertRaisesRegex(service.BootstrapError, 'Linux amd64'):
            service.require_linux_amd64_elf(bytes(elf), 'fixture')

    def test_apt_source_endpoint_parser_is_strict_and_deduplicates(self):
        plan = """Types: deb
URIs: http://deb.example.org/debian https://security.example.org/debian-security
Suites: stable stable-security

deb [signed-by=/keyring] http://deb.example.org/debian stable main
"""
        self.assertEqual(service.apt_source_endpoints(plan), [
            ('deb.example.org', 80), ('security.example.org', 443)])
        for invalid in ('', 'URIs: ftp://mirror.example.org/debian\n',
                        'deb http://user@example.org/debian stable main\n'):
            with self.subTest(invalid=invalid), self.assertRaises(service.BootstrapError):
                service.apt_source_endpoints(invalid)

    def test_apt_deb822_continued_uris_are_all_preflighted(self):
        source = """Types: deb deb-src
URIs: http://deb.example.org/debian
 https://security.example.org/debian-security
Suites: stable
Components: main
"""
        self.assertEqual(service.apt_source_endpoints(source), [
            ('deb.example.org', 80), ('security.example.org', 443)])

    def test_apt_disabled_deb822_stanza_is_not_preflighted(self):
        source = """Types: deb
URIs: http://disabled.example.org/debian
Suites: stable
Enabled: no

Types: deb
URIs: https://enabled.example.org/debian
Suites: stable
Enabled: yes
"""
        self.assertEqual(service.apt_source_endpoints(source), [
            ('enabled.example.org', 443)])

    def test_apt_update_promotes_every_index_error(self):
        self.assertEqual(service.apt_update_command()[-2:], ['update', '--error-on=any'])

    def test_apt_network_preflight_resolves_then_connects_by_ipv4(self):
        runner = FakeRunner({
            'APT source definitions':
                'Types: deb\nURIs: http://deb.example.org/debian\nSuites: stable\n',
            'APT endpoint deb.example.org:80 DNS resolution':
                '192.0.2.40 STREAM deb.example.org\n192.0.2.40 DGRAM\n',
        })
        bootstrap = service.Bootstrap(config(), runner)
        bootstrap.apt_network_preflight('proxy')
        connect = next(args for args, kwargs in runner.calls
                       if kwargs.get('label') == 'APT endpoint deb.example.org:80 TCP connect')
        self.assertEqual(connect[-2:], ['192.0.2.40', '80'])

    def test_failed_dns_preflight_stops_before_any_apt_mutation(self):
        runner = FakeRunner({'APT source definitions':
                             'Types: deb\nURIs: http://deb.example.org/debian\nSuites: stable\n'})
        bootstrap = service.Bootstrap(config(), runner)
        with self.assertRaises(service.BootstrapError):
            bootstrap.packages_and_firewall('proxy')
        self.assertFalse(any('systemd-run' in args for args, _ in runner.calls))
        self.assertFalse(any(kwargs.get('label') == 'new owned guest file'
                             for _, kwargs in runner.calls))

    def test_transient_package_unit_owns_timeout_and_whole_process_group(self):
        runner = FakeRunner({'fixture unit collision': 'not-found\n'})
        bootstrap = service.Bootstrap(config(), runner)
        with patch.object(bootstrap, 'owned'):
            bootstrap.transient_package_command(
                'proxy', 'fixture', 120, ['/usr/bin/true'], 'fixture')
        command, kwargs = next((args, kwargs) for args, kwargs in runner.calls
                               if 'systemd-run' in args)
        unit = service.transient_unit_name(bootstrap.run_id, 'proxy', 'fixture')
        self.assertIn('--unit=' + unit, command)
        self.assertIn('--property=Type=exec', command)
        self.assertIn('--property=RuntimeMaxSec=120s', command)
        self.assertIn('--property=TimeoutStopSec=20s', command)
        self.assertIn('--property=KillMode=control-group', command)
        self.assertIn('--property=SendSIGKILL=yes', command)
        self.assertIn('--property=UMask=0022', command)
        self.assertNotIn('--pipe', command)
        self.assertFalse(any('MemoryMax' in value for value in command))
        self.assertEqual(kwargs['timeout'], 180)

    def test_transient_unit_collision_never_stops_or_reuses_existing_unit(self):
        runner = FakeRunner({'fixture unit collision': 'loaded\n'})
        bootstrap = service.Bootstrap(config(), runner)
        with patch.object(bootstrap, 'owned'):
            with self.assertRaisesRegex(service.BootstrapError, 'already exists'):
                bootstrap.transient_package_command(
                    'proxy', 'fixture', 120, ['/usr/bin/true'], 'fixture')
        self.assertFalse(any('systemd-run' in args for args, _ in runner.calls))
        self.assertFalse(any('systemctl' in args and 'stop' in args
                             for args, _ in runner.calls))

    def test_package_flow_preflights_and_wraps_every_apt_mutation(self):
        runner = FakeRunner({
            'nginx signing key fingerprint':
                f'fpr:::::::::{service.core.NGINX_SIGNING_FINGERPRINT}:\n',
            'signed nginx candidate':
                'Candidate: 1.30.5-1~trixie\n https://nginx.org/packages/debian\n',
            'installed package receipt': 'nginx\t1.30.5-1~trixie\n',
        })
        bootstrap = service.Bootstrap(config(), runner)
        events = []
        with patch.object(bootstrap, 'apt_network_preflight',
                          side_effect=lambda role: events.append(('preflight', role))), \
                patch.object(bootstrap, 'endpoint_preflight',
                             side_effect=lambda role, host, port, label:
                             events.append(('endpoint', host, port))), \
                patch.object(bootstrap, 'transient_package_command',
                             side_effect=lambda role, phase, runtime, command, label:
                             events.append(('apt', phase, command))), \
                patch.object(bootstrap, 'put'), patch.object(bootstrap, 'save'):
            bootstrap.packages_and_firewall('proxy')
        phases = [event[1] for event in events if event[0] == 'apt']
        self.assertEqual(phases, ['debian-index', 'debian-upgrade', 'debian-packages',
                                  'nginx-index', 'nginx-package'])
        self.assertEqual(events[0], ('preflight', 'proxy'))
        self.assertLess(events.index(('endpoint', 'nginx.org', 443)),
                        next(index for index, event in enumerate(events)
                             if event[:2] == ('apt', 'nginx-index')))
        for event in events:
            if event[:2] in (('apt', 'debian-index'), ('apt', 'nginx-index')):
                self.assertIn('--error-on=any', event[2])

    def test_firewalls_expose_only_candidate_control_ports(self):
        proxy = service.firewall(config(), 'proxy')
        sshgw = service.firewall(config(), 'sshgw')
        self.assertIn('198.18.1.20 tcp dport 9443 accept', proxy)
        self.assertIn('198.18.1.10 tcp dport 8082 accept', sshgw)
        self.assertIn('198.18.1.20 tcp dport 8083 accept', sshgw)
        for text in (proxy, sshgw):
            self.assertNotIn('dport 22 accept', text)
            self.assertNotIn('WireGuard', text)

    def test_create_is_unprivileged_onboot_zero_and_does_not_touch_core_ids(self):
        runner = FakeRunner({'guest OS identity': 'VERSION_ID="13"\n',
                             'guest machine identity': 'a' * 32 + '\n'})
        bootstrap = service.Bootstrap(config(), runner)
        with patch.object(bootstrap, 'save'), patch.object(bootstrap, 'owned'), \
                patch.object(bootstrap, 'ensure_mtu'):
            bootstrap.create('proxy')
        command = next(args for args, kwargs in runner.calls
                       if kwargs.get('label') == 'new container creation')
        self.assertEqual(command[command.index('--onboot') + 1], '0')
        self.assertEqual(command[command.index('--start') + 1], '0')
        self.assertEqual(command[command.index('--unprivileged') + 1], '1')
        self.assertIn('mtu=1370', command[command.index('--net0') + 1])
        self.assertNotIn('204', command)

    def test_mtu_refuses_ifupdown2_without_addon_scripts(self):
        runner = FakeRunner({
            'guest network manager': 'ifupdown2',
            'ifupdown2 addon support': 'addon_scripts_support=0\n',
        })
        bootstrap = service.Bootstrap(config(), runner)
        with self.assertRaisesRegex(service.BootstrapError, 'addon script'):
            bootstrap.ensure_mtu('proxy')

    def test_put_uses_pct_push_mode_and_requires_checksum_readback(self):
        body = b'fixture body\n'
        digest = __import__('hashlib').sha256(body).hexdigest()
        runner = FakeRunner({'guest file checksum': digest + '  /fixture\n'})
        bootstrap = service.Bootstrap(config(), runner)
        with tempfile.TemporaryDirectory() as td, patch.object(bootstrap, 'owned'):
            object.__setattr__(bootstrap.c, 'state_dir', td)
            bootstrap.put('proxy', '/fixture', body, '0600')
        push = next(args for args, kwargs in runner.calls
                    if kwargs.get('label') == 'new owned guest file')
        self.assertEqual(push[-2:], ['--perms', '0600'])
        self.assertNotIn('fixture body', ' '.join(push))
        runner = FakeRunner({'guest file checksum': 'b' * 64 + '  /fixture\n'})
        bootstrap = service.Bootstrap(config(), runner)
        with tempfile.TemporaryDirectory() as td, patch.object(bootstrap, 'owned'):
            object.__setattr__(bootstrap.c, 'state_dir', td)
            with self.assertRaisesRegex(service.BootstrapError, 'checksum readback'):
                bootstrap.put('proxy', '/fixture', body)

    def test_dependency_order_and_env_addresses_are_explicit(self):
        self.assertIn('Requires=isolated-services-firewall.service', service.NETWORKING_DROPIN)
        self.assertIn('After=isolated-services-firewall.service networking.service',
                      service.SERVICE_DROPIN)
        proxy = service.proxy_environment(config(), {'PICKLE_PROXY_AGENT_TOKEN': 'p' * 32})
        sshgw = service.sshgw_environment(config(), {
            'PICKLE_SSHGW_TOKEN': 's' * 32,
            'PICKLE_TERMINAL_CONTROL_TOKEN': 't' * 32})
        self.assertIn('PICKLE_PROXY_AGENT_ALLOWED_SRC=198.18.1.20', proxy)
        self.assertIn('PICKLE_SSHGW_API_BASE=http://198.18.1.20:8080', sshgw)
        self.assertNotIn('proxyfront', sshgw.lower())

    def test_failed_optional_health_still_parks_all_services(self):
        class FailingRunner(FakeRunner):
            def run(self, args, **kwargs):
                self.calls.append((args, kwargs))
                if kwargs.get('label') == 'temporary sshgw health start':
                    raise service.BootstrapError('fixture start failure')
                return ''

        runner = FailingRunner()
        bootstrap = service.Bootstrap(config(), runner)
        with self.assertRaisesRegex(service.BootstrapError, 'fixture start failure'):
            bootstrap.validate_services()
        labels = [kwargs.get('label') for _, kwargs in runner.calls]
        self.assertIn('proxy health cleanup', labels)
        self.assertIn('sshgw health cleanup', labels)

    def test_proxy_cleanup_failure_does_not_skip_sshgw_cleanup(self):
        class CleanupFailingRunner(FakeRunner):
            def run(self, args, **kwargs):
                self.calls.append((args, kwargs))
                if kwargs.get('label') == 'proxy health cleanup':
                    raise service.BootstrapError('proxy cleanup failed')
                return ''

        runner = CleanupFailingRunner()
        bootstrap = service.Bootstrap(config(), runner)
        with self.assertRaisesRegex(service.BootstrapError, 'proxy cleanup failed'):
            bootstrap.validate_services()
        labels = [kwargs.get('label') for _, kwargs in runner.calls]
        self.assertIn('sshgw health cleanup', labels)

    def test_health_and_both_cleanup_failures_are_preserved(self):
        class MultiFailingRunner(FakeRunner):
            def run(self, args, **kwargs):
                self.calls.append((args, kwargs))
                label = kwargs.get('label')
                if label in ('temporary sshgw health start', 'proxy health cleanup',
                             'sshgw health cleanup'):
                    raise service.BootstrapError(label)
                return ''

        runner = MultiFailingRunner()
        bootstrap = service.Bootstrap(config(), runner)
        with self.assertRaises(BaseExceptionGroup) as raised:
            bootstrap.validate_services()
        self.assertEqual([str(error) for error in raised.exception.exceptions], [
            'temporary sshgw health start', 'proxy health cleanup', 'sshgw health cleanup'])

    def test_manifest_save_fsyncs_file_and_directory(self):
        with tempfile.TemporaryDirectory() as td:
            c = replace(config(Path(td)), state_dir=str(Path(td) / 'state'))
            Path(c.state_dir).mkdir()
            bootstrap = service.Bootstrap(c, FakeRunner())
            with patch.object(service.os, 'fsync', wraps=os.fsync) as sync:
                bootstrap.save()
            self.assertEqual(sync.call_count, 2)
            self.assertFalse((Path(c.state_dir) / '.manifest.tmp').exists())
            manifest = Path(c.state_dir) / 'manifest.json'
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)

    def test_sshgw_key_directory_is_writable_only_by_service_owner(self):
        runner = FakeRunner({'guest file checksum': ''})
        bootstrap = service.Bootstrap(config(), runner)
        artifacts = {name: b'fixture' for name, _ in service.ARTIFACTS}
        # Stop after directory creation; the assertion concerns key-generation custody.
        with patch.object(bootstrap, 'put', side_effect=service.BootstrapError('fixture stop')):
            with self.assertRaisesRegex(service.BootstrapError, 'fixture stop'):
                bootstrap.sshgw({'PICKLE_SSHGW_TOKEN': 's' * 32,
                                 'PICKLE_TERMINAL_CONTROL_TOKEN': 't' * 32}, artifacts)
        directory = next(args for args, kwargs in runner.calls
                         if kwargs.get('label') == 'sshgw key directory')
        self.assertEqual(directory[directory.index('-o') + 1], 'pickle')
        self.assertEqual(directory[directory.index('-m') + 1], '0750')


if __name__ == '__main__':
    unittest.main()
