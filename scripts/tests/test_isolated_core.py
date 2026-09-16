#!/usr/bin/env python3
"""Offline safety tests. No PVE, database, package installation or network calls."""
import base64
from dataclasses import asdict, replace
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True

MODULE = Path(__file__).resolve().parents[1] / 'lib' / 'isolated_core.py'
SPEC = importlib.util.spec_from_file_location('isolated_core', MODULE)
core = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = core
SPEC.loader.exec_module(core)


def config(root=Path('/nonexistent-inputs')):
    return core.Config(
        expected_node='compute-a', expected_cluster='example-cluster', bridge='infranet',
        subnet='100.65.0.0/16', gateway='100.65.0.1', app_ip='100.65.1.20',
        db_ip='100.65.1.21', proxy_ip='100.65.1.10', nameserver='192.0.2.53', mtu=1370,
        app_ctid=201, db_ctid=204, app_hostname='pickle-app', db_hostname='pickle-db',
        app_cores=4, db_cores=2, app_memory_mb=4096, db_memory_mb=4096,
        app_disk_gb=32, db_disk_gb=64, storage_reserve_gb=16, storage='local-lvm',
        template='local:vztmpl/debian-13-standard_REVIEWED_amd64.tar.zst', template_sha256='a' * 64,
        postgresql_version='18.6-1.pgdg13+2', jre_version='25.0.4.1+1-1~deb13u1',
        db_name='pickle_verify', db_role='pickle_verify',
        api_env_file=str(root / 'api.env'), db_password_file=str(root / 'db-password'),
        db_ca_file=str(root / 'db-ca.crt'), db_cert_file=str(root / 'db-server.crt'),
        db_key_file=str(root / 'db-server.key'), state_dir=str(root / 'new-run'))


def fresh_environment():
    return ('PICKLE_JWT_SECRET=' + 'j' * 48 + '\nPICKLE_CREDENTIALS_KEY='
            + base64.b64encode(b'c' * 32).decode()
            + '\nPICKLE_SEED_SYSADMIN_EMAIL=admin@example.test\n'
              'PICKLE_SEED_SYSADMIN_PASSWORD=fresh-admin-password-example\n'
              'PICKLE_SEED_ORGADMIN_EMAIL=orgadmin@example.test\n'
              'PICKLE_SEED_ORGADMIN_PASSWORD=fresh-orgadmin-password-example\n').encode()


class FakeRunner(core.Runner):
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.get(kwargs.get('label'), '')


class IsolatedCoreSafetyTest(unittest.TestCase):
    def test_default_mode_does_not_read_credentials_or_execute_host_commands(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'config.json'
            source.write_text(json.dumps(asdict(config())))
            output = io.StringIO()
            with patch.object(sys, 'argv', ['bootstrap', '--config', str(source)]), \
                    patch.object(core.Bootstrap, 'apply', side_effect=AssertionError('unexpected mutation')), \
                    patch.object(core, 'protected_file', side_effect=AssertionError('secret read')), \
                    patch.object(sys, 'stdout', output):
                self.assertEqual(core.main(), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result['mode'], 'dry-run')
            self.assertFalse(result['api_started'])
            self.assertFalse(result['schema_or_inventory_registered'])

    def test_an_unexpected_host_is_rejected_before_any_pve_or_secret_access(self):
        runner = FakeRunner()
        with patch.object(core.os, 'geteuid', return_value=0), \
                patch.object(core.socket, 'gethostname', return_value='other-node'):
            with self.assertRaisesRegex(core.BootstrapError, 'expected node'):
                core.preflight(config(), runner)
        self.assertEqual(runner.calls, [])

    def test_nonroot_is_rejected_before_any_operation(self):
        with patch.object(core.os, 'geteuid', return_value=1000):
            with self.assertRaises(core.BootstrapError):
                core.preflight(config(), FakeRunner())

    def test_missing_quorum_and_wrong_cluster_are_rejected(self):
        for cluster in [{'type': 'cluster', 'name': 'example-cluster', 'quorate': 0},
                        {'type': 'cluster', 'name': 'other-cluster', 'quorate': 1}]:
            runner = FakeRunner({'cluster status': json.dumps([cluster])})
            with patch.object(core.os, 'geteuid', return_value=0), \
                    patch.object(core.socket, 'gethostname', return_value='compute-a'):
                with self.assertRaisesRegex(core.BootstrapError, 'quorum'):
                    core.preflight(config(), runner)
            self.assertEqual(len(runner.calls), 1)

    def test_existing_qemu_or_lxc_id_anywhere_in_cluster_blocks_creation(self):
        for kind in ('qemu', 'lxc'):
            runner = FakeRunner({'cluster status': json.dumps([{'type': 'cluster', 'name': 'example-cluster', 'quorate': 1}]),
                                 'guest inventory': json.dumps([{'type': kind, 'vmid': 201, 'node': 'other-node'}])})
            with patch.object(core.os, 'geteuid', return_value=0), \
                    patch.object(core.socket, 'gethostname', return_value='compute-a'):
                with self.assertRaisesRegex(core.BootstrapError, 'already used'):
                    core.preflight(config(), runner)
            self.assertFalse(any(args[:2] == ['pct', 'create'] for args, _ in runner.calls))

    def test_invalid_network_id_or_injection_is_rejected(self):
        variants = [replace(config(), app_ctid=204), replace(config(), app_ctid=100000),
                    replace(config(), app_ip='100.65.1.21'), replace(config(), db_ip='203.0.113.1'),
                    replace(config(), bridge='net;reboot'), replace(config(), db_role="x';DROP ROLE postgres;--"),
                    replace(config(), template='local:vztmpl/../../archive.tar.zst'),
                    replace(config(), postgresql_version='19beta3-1')]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                with self.assertRaises((core.BootstrapError, ValueError)):
                    candidate.validate()

    def test_secret_symlink_or_broad_permissions_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / 'secret'
            target.write_text('not-a-real-credential')
            target.chmod(0o644)
            with self.assertRaises(core.BootstrapError):
                core.protected_file(str(target), private=True)
            target.chmod(0o600)
            link = Path(td) / 'link'
            link.symlink_to(target)
            with self.assertRaises(core.BootstrapError):
                core.protected_file(str(link), private=True)
            self.assertEqual(core.protected_file(str(target), private=True), target.read_bytes())

    def test_ordinary_production_environment_cannot_be_copied_as_bootstrap_input(self):
        core.validate_fresh_api_env(fresh_environment())
        for extra in (b'PICKLE_DB_PASSWORD=existing\n', b'SPRING_PROFILES_ACTIVE=prod\n',
                      b'PICKLE_SMTP_HOST=mail.example.test\n', b'PICKLE_PROXMOX_TOKEN_SECRET=existing\n',
                      b'PICKLE_JWT_SECRET=duplicate\n'):
            with self.assertRaises(core.BootstrapError):
                core.validate_fresh_api_env(fresh_environment() + extra)

    def test_database_hba_requires_tls_scram_and_only_the_application_host(self):
        postgres, hba = core.db_config(config())
        self.assertIn("listen_addresses = '127.0.0.1,100.65.1.21'", postgres)
        self.assertIn('hostssl pickle_verify pickle_verify 100.65.1.20/32 scram-sha-256', hba)
        self.assertIn('hostnossl all all 0.0.0.0/0 reject', hba)
        self.assertNotIn('trust', hba)
        self.assertEqual(sum('hostssl ' in line for line in hba.splitlines()), 1)

    def test_firewall_replaces_only_its_own_table_and_preserves_established_flows(self):
        database = core.firewall(config(), 'database')
        application = core.firewall(config(), 'application')
        self.assertIn('flush table inet isolated_core', database)
        self.assertNotIn('flush ruleset', database)
        self.assertIn('ct state established,related accept', database)
        self.assertIn('ip saddr 100.65.1.20 tcp dport 5432 accept', database)
        self.assertIn('ip saddr 100.65.1.10 tcp dport 80 accept', application)
        self.assertNotIn('tcp dport 8080 accept', application)

    def test_api_cannot_start_or_process_jobs_until_a_later_explicit_step(self):
        self.assertIn('ConditionPathExists=/etc/pickle/allow-api-start', core.API_UNIT)
        self.assertIn('Requires=isolated-core-firewall.service', core.API_UNIT)
        self.assertIn('--jobrunr.background-job-server.enabled=false', core.API_UNIT)
        self.assertNotIn('postgresql.service', core.API_UNIT)
        content = core.api_environment(config(), b'x' * 48).decode()
        self.assertIn('sslmode=verify-full', content)
        self.assertIn('JOBRUNR_BACKGROUND_JOB_SERVER_ENABLED=false', content)
        self.assertIn('SPRING_PROFILES_ACTIVE=dev', content)
        self.assertIn('SERVER_ADDRESS=127.0.0.1', content)

    def test_proxy_header_trust_is_restricted_even_if_the_kernel_policy_is_missing(self):
        content = core.nginx(config())
        self.assertIn('allow 100.65.1.10;', content)
        self.assertIn('deny all;', content)
        self.assertNotIn('real_ip_header', content)

    def test_changed_container_ownership_blocks_guest_file_writes(self):
        runner = FakeRunner({'owned container identity': 'hostname: other\ndescription: not-owned\n'})
        bootstrap = core.Bootstrap(config(), runner)
        with self.assertRaisesRegex(core.BootstrapError, 'ownership changed'):
            bootstrap.put(201, '/etc/example', 'value')
        self.assertEqual(len(runner.calls), 1)

    def test_errors_do_not_echo_sensitive_stdin_or_stderr(self):
        completed = subprocess.CompletedProcess(['psql'], 1, b'', b'error: password=SECRET-SENTINEL')
        with patch.object(core.subprocess, 'run', return_value=completed):
            with self.assertRaises(core.BootstrapError) as failure:
                core.Runner().run(['psql'], data=b'SECRET-SENTINEL', label='role creation')
        self.assertNotIn('SECRET-SENTINEL', str(failure.exception))

    def test_partial_creation_is_recorded_without_automatic_destroy(self):
        with tempfile.TemporaryDirectory() as td:
            c = replace(config(Path(td)), state_dir=str(Path(td) / 'run'))
            Path(c.state_dir).mkdir()
            runner = FakeRunner()
            bootstrap = core.Bootstrap(c, runner)
            with patch.object(runner, 'run', side_effect=core.BootstrapError('creation failed')):
                with self.assertRaises(core.BootstrapError):
                    bootstrap.create('database')
            manifest = json.loads((Path(c.state_dir) / 'manifest.json').read_text())
            self.assertEqual(manifest['attempted'][0]['id'], c.db_ctid)
            self.assertEqual(manifest['created'], [])


if __name__ == '__main__':
    unittest.main()
