"""Keep the manual CI preflight limited to authentication without remote sessions."""
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'check-ci-ssh.py'
SPEC = importlib.util.spec_from_file_location('check_ci_ssh', SCRIPT)
ssh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ssh)


class ReadOnlySshTest(unittest.TestCase):
    def env(self, folder):
        return {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
                'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64', 'GITHUB_RUN_ID': '123',
                'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'check-production-ssh',
                'RUNNER_TEMP': folder, 'EXAMPLE_DEV_VPN_HOST': '100.64.0.10',
                'EXAMPLE_PROD_VPN_HOST': '100.64.0.20', 'EXAMPLE_DEV_HOST': 'hop.example.invalid',
                'EXAMPLE_DEV_PORT': '22022', 'EXAMPLE_PROD_HOST': 'production.example.invalid', 'EXAMPLE_PROD_PORT': '22',
                'EXAMPLE_HOP_SSH_KEY': 'fixture-hop-material', 'EXAMPLE_PROD_SSH_KEY': 'fixture-production-material',
                'EXAMPLE_DEV_KNOWN_HOSTS': 'fixture-hop-public-pin',
                'EXAMPLE_PROD_KNOWN_HOSTS': 'fixture-production-public-pin'}

    def test_only_authentication_masters_run_and_client_material_is_removed(self):
        configs = []
        def execute(argv, **kwargs):
            self.assertEqual(argv[0], 'ssh')
            self.assertIn(argv[-1], ('ci-hop', 'ci-production'))
            config = Path(argv[2])
            configs.append(config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            if '-O' in argv:
                self.assertEqual(kwargs['stdout'], subprocess.PIPE)
                self.assertEqual(kwargs['stderr'], subprocess.PIPE)
                self.assertIn(argv[argv.index('-O') + 1], ('check', 'exit'))
            else:
                self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
                self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
                self.assertIn('-M', argv)
                self.assertIn('-fNT', argv)
                self.assertEqual(kwargs['timeout'], 35)
            return SimpleNamespace(returncode=0, stdout=b'')
        with tempfile.TemporaryDirectory() as folder, patch.object(ssh.subprocess, 'run', side_effect=execute) as run, \
             patch('builtins.print') as output:
            env = self.env(folder)
            ssh.check(env)
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual(run.call_count, 6)
        self.assertEqual([call.args[0][-1] for call in run.call_args_list],
                         ['ci-hop'] * 3 + ['ci-production'] * 3)
        self.assertIn('HostKeyAlias [hop.example.invalid]:22022', configs[0])
        self.assertIn('HostKeyAlias production.example.invalid', configs[0])
        self.assertIn('ProxyJump ci-hop', configs[0])
        self.assertIn('StrictHostKeyChecking yes', configs[0])
        rendered = str(output.call_args_list)
        for name in ('EXAMPLE_HOP_SSH_KEY', 'EXAMPLE_PROD_SSH_KEY', 'EXAMPLE_DEV_VPN_HOST', 'EXAMPLE_PROD_VPN_HOST'):
            self.assertNotIn(env[name], rendered)

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires a local fork fixture')
    def test_background_child_cannot_hold_the_initial_capture_open(self):
        fake_source = '''import os,sys,time
from pathlib import Path
args=sys.argv[1:]
control=Path(args[args.index('-S')+1])
stop=Path(str(control)+'.stop');done=Path(str(control)+'.done')
if '-O' in args:
    operation=args[args.index('-O')+1]
    if operation=='check':sys.exit(0 if control.exists() else 1)
    stop.write_text('stop')
    deadline=time.monotonic()+0.6
    while not done.exists() and time.monotonic()<deadline:time.sleep(0.01)
    if not done.exists():sys.exit(1)
    for path in (control,stop,done):path.unlink(missing_ok=True)
    sys.exit(0)
control.write_text('owned')
if os.fork()==0:
    deadline=time.monotonic()+8
    while not stop.exists() and time.monotonic()<deadline:time.sleep(0.01)
    done.write_text('closed')
    os._exit(0)
os._exit(0)
'''
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = root / 'fake_ssh.py'
            fake.write_text(fake_source)
            config = root / 'config'
            config.write_text('fixture configuration')
            control = root / 'owned.sock'
            def execute(argv, **kwargs):
                kwargs['timeout'] = min(kwargs['timeout'], 1)
                return real_run([sys.executable, '-I', '-B', str(fake), *argv[1:]], **kwargs)
            with patch.object(ssh.subprocess, 'run', side_effect=execute):
                ssh.authenticate(config, 'ci-hop', control)
            self.assertFalse(control.exists())

    def test_missing_authentication_data_never_spawns_ssh(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ssh.subprocess, 'run') as run:
            env = self.env(folder)
            env['EXAMPLE_PROD_SSH_KEY'] = ''
            with self.assertRaises(ssh.network.Refusal):
                ssh.check(env)
            self.assertEqual(list(Path(folder).iterdir()), [])
        run.assert_not_called()

    def test_failed_authentication_requests_cleanup_without_a_second_target(self):
        for failure in ('start', 'check', 'exit'):
            with self.subTest(failure=failure):
                def execute(argv, **kwargs):
                    action = argv[argv.index('-O') + 1] if '-O' in argv else 'start'
                    return SimpleNamespace(returncode=255 if action == failure else 0, stdout=b'')
                with tempfile.TemporaryDirectory() as folder, patch.object(ssh.subprocess, 'run', side_effect=execute) as run:
                    with self.assertRaises(ssh.network.Refusal):
                        ssh.check(self.env(folder))
                    self.assertEqual(list(Path(folder).iterdir()), [])
                self.assertEqual(run.call_count, 2 if failure == 'start' else 3)
                self.assertEqual(run.call_args_list[-1].args[0][-3:], ['-O', 'exit', 'ci-hop'])

    def test_workflow_is_manual_and_has_only_bounded_read_only_steps(self):
        workflow = SCRIPT.parents[1] / '.github/check-ssh.example.yml'
        text = workflow.read_text()
        self.assertEqual(text.split('on:\n', 1)[1].split('\npermissions:', 1)[0],
                         '  workflow_dispatch:\n')
        self.assertIn('    environment: prod\n', text)
        self.assertEqual(text.count('    timeout-minutes: 3\n'), 1)
        self.assertIn('          DEPLOY_TRANSPORT: netbird\n', text)
        self.assertIn('        if: always()\n', text)
        self.assertEqual(re.findall(r'^        uses: (.*)$', text, re.M), ['actions/checkout@v7'])
        self.assertEqual(re.findall(r'^        run: (.*)$', text, re.M), [
            'python3 -I -B infra-example/scripts/ci-netbird.py start',
            'python3 -I -B infra-example/scripts/check-ci-ssh.py',
            'python3 -I -B infra-example/scripts/ci-netbird.py stop'])
        for forbidden in ('pull_request:', '  push:', 'scp ', 'upload-artifact', 'download-artifact',
                          'deploy-environment.py', 'pickle-dev-deploy-v1', 'needs: build'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
