"""Deployment network refusal and pinned-host behavior without live enrollment."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'ci-netbird.py'
SPEC = importlib.util.spec_from_file_location('ci_netbird', SCRIPT)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


class DeploymentNetworkTest(unittest.TestCase):
    def env(self, folder):
        return {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
                'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64', 'GITHUB_RUN_ID': '123',
                'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'deploy-development',
                'RUNNER_TEMP': folder, 'NETBIRD_SETUP_KEY': '00000000-0000-4000-8000-000000000001',
                'EXAMPLE_DEV_VPN_HOST': '100.64.0.10', 'EXAMPLE_DEV_SSH_PORT': '22'}

    def test_legacy_mode_does_not_run_a_network_command(self):
        with patch.object(ci, 'command') as command, patch.object(ci, 'emit_mode') as emit:
            ci.start({})
        command.assert_not_called()
        emit.assert_called_once_with({}, 'native')

    def test_selected_vpn_cannot_fall_back_when_key_is_missing(self):
        with patch.object(ci, 'command') as command, patch.object(ci, 'emit_mode') as emit:
            with self.assertRaises(ci.Refusal):
                ci.start({'DEPLOY_TRANSPORT': 'netbird'})
        command.assert_not_called()
        emit.assert_not_called()

    def test_nonempty_whitespace_key_is_not_native_mode(self):
        with patch.object(ci, 'command') as command, patch.object(ci, 'emit_mode') as emit:
            with self.assertRaises(ci.Refusal):
                ci.start({'NETBIRD_SETUP_KEY': ' \n '})
        command.assert_not_called()
        emit.assert_not_called()

    def test_self_hosted_runner_is_not_modified(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ci, 'command') as command:
            env = self.env(folder)
            env['RUNNER_ENVIRONMENT'] = 'self-hosted'
            with self.assertRaises(ci.Refusal):
                ci.start(env)
            self.assertEqual(list(Path(folder).iterdir()), [])
        command.assert_not_called()

    def test_existing_client_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ci.shutil, 'which', return_value='/usr/bin/netbird'), \
             patch.object(ci, 'command') as command:
            with self.assertRaises(ci.Refusal):
                ci.start(self.env(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])
        command.assert_not_called()

    def test_package_mismatch_fails_before_privileged_install_and_removes_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ci.shutil, 'which', return_value=None), \
             patch.object(ci, 'existing_state', return_value=False):
            calls = []
            def download(argv, **kwargs):
                calls.append(argv)
                Path(argv[argv.index('--output') + 1]).write_bytes(b'not the pinned package')
            with patch.object(ci, 'command', side_effect=download), patch.object(ci, 'emit_mode') as emit:
                with self.assertRaises(ci.Refusal):
                    ci.start(self.env(folder))
            self.assertFalse((Path(folder) / 'pickle-ci-netbird/setup.key').exists())
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(argv[0] == 'curl' for argv in calls))
            self.assertNotIn('--location', calls[0])
            emit.assert_not_called()

    def test_cleanup_does_not_stop_an_unowned_client(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ci, 'command') as command:
            ci.stop(self.env(folder))
            state = Path(folder) / 'pickle-ci-netbird'
            state.mkdir()
            (state / 'owner.json').write_text(json.dumps({'run': 'another run'}))
            with self.assertRaises(ci.Refusal):
                ci.stop(self.env(folder))
        command.assert_not_called()

    def test_peer_scope_rejects_an_operators_or_customer_key(self):
        status = {'peers': {'details': [{'netbirdIp': '100.64.0.10'}, {'netbirdIp': '100.64.0.20'}]}}
        with self.assertRaises(ci.Refusal):
            ci.ready(status, '100.64.0.10', 22, 2)

    def test_ready_requires_control_plane_and_disabled_builtin_ssh(self):
        status = {'management': {'connected': True}, 'signal': {'connected': True},
                  'daemonVersion': ci.VERSION, 'sshServer': {'enabled': False},
                  'peers': {'details': [{'netbirdIp': '100.64.0.10'}]}}
        with patch.object(ci, 'tcp_ready', return_value=True) as probe:
            self.assertTrue(ci.ready(status, '100.64.0.10', 22, 2))
            probe.assert_called_once_with('100.64.0.10', 22, 2)
            status['sshServer']['enabled'] = True
            self.assertFalse(ci.ready(status, '100.64.0.10', 22, 2))

    def test_lazy_peer_needs_a_successful_bounded_tcp_probe(self):
        status = {'management': {'connected': True}, 'signal': {'connected': True},
                  'daemonVersion': ci.VERSION, 'sshServer': {'enabled': False},
                  'peers': {'details': [{'netbirdIp': '100.64.0.10', 'status': 'Idle'}]}}
        with patch.object(ci, 'tcp_ready', return_value=False):
            self.assertFalse(ci.ready(status, '100.64.0.10', 22, 2))
        with patch.object(ci, 'tcp_ready', return_value=True):
            self.assertTrue(ci.ready(status, '100.64.0.10', 22, 2))

    def test_daemon_stop_is_attempted_even_if_down_times_out(self):
        def execute(argv, **kwargs):
            if argv[-1] == 'down':
                raise subprocess.TimeoutExpired(argv, 20)
        with patch.object(ci, 'command', side_effect=execute) as command:
            with self.assertRaises(subprocess.TimeoutExpired):
                ci.stop_client()
        self.assertEqual([call.args[0][-1] for call in command.call_args_list], ['down', 'stop'])

    def test_nonstandard_port_retains_the_original_known_host_label(self):
        self.assertEqual(ci.ssh_alias('deploy.example.invalid', '22022'), '[deploy.example.invalid]:22022')
        self.assertEqual(ci.ssh_alias('operating.example.invalid', '22'), 'operating.example.invalid')
        for host, port in [('host\nProxyCommand unsafe', '22'), ('host', '0')]:
            with self.assertRaises(ci.Refusal):
                ci.ssh_alias(host, port)
        with self.assertRaises(ci.Refusal):
            ci.vpn_host('192.0.2.10')


if __name__ == '__main__':
    unittest.main()
