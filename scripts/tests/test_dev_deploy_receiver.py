"""Negative protocol and scoped artifact receiver checks without live SSH."""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'dev-deploy-receiver.py'
SPEC = importlib.util.spec_from_file_location('dev_receiver', SCRIPT)
receiver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receiver)


class ReceiverTest(unittest.TestCase):
    def config(self, folder):
        return {'host': 'example-dev-host', 'manifest': str(Path(folder)/'manifest.json'),
                'forbidden_ctids': [904, 905], 'target_ctids': {name: 901 for name in receiver.TARGETS},
                'database_ctid': 902, 'staging_root': folder, 'maximum_bytes': {name: 1024 for name in receiver.TARGETS},
                'maximum_expanded_bytes': {'console': 1024, 'llm-gateway': 1024}, 'maximum_archive_entries': 3,'maximum_staging_bytes':1048576,'minimum_free_bytes':0}

    def request(self, payload=b'example artifact'):
        return {'version': 1, 'action': 'check', 'target': 'api', 'commit': 'a'*40,
                'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload)}

    def test_original_ssh_commands_cannot_enter_header_or_deployer(self):
        for command in ('sh', 'scp -t /root', 'internal-sftp', 'pickle-dev-deploy-v1; id', 'pickle-dev-deploy-v1\n'):
            with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command}), patch.object(receiver.os, 'geteuid', return_value=0), patch.object(receiver, 'header') as reader:
                with self.assertRaisesRegex(receiver.Refusal, 'fixed development'):
                    receiver.main()
                reader.assert_not_called()

    def test_header_rejects_unknown_duplicate_production_scope_and_oversize(self):
        config = {'maximum_bytes': {name: 1024 for name in receiver.TARGETS}}
        manifest = {'allowed_commits': {'api': 'a'*40}, 'approved_artifacts': {'api': {'a'*40: {'sha256': self.request()['sha256'], 'size': self.request()['size']}}}}
        good = self.request()
        self.assertEqual(receiver.header(io.BytesIO((json.dumps(good)+'\n').encode()), config, manifest), good)
        for wrong in (dict(good, environment='prod'), dict(good, tool='/uploaded.py'),
                      dict(good, action='shell'), dict(good, target='source'), dict(good, commit='b'*40),
                      dict(good, size=1025), dict(good, size=True), dict(good, sha256='0'*64), dict(good, size=1)):
            with self.assertRaises(receiver.Refusal):
                receiver.header(io.BytesIO((json.dumps(wrong)+'\n').encode()), config, manifest)
        with self.assertRaisesRegex(receiver.Refusal, 'duplicate'):
            receiver.header(io.BytesIO(b'{"version":1,"version":1}\n'), config, manifest)
        with self.assertRaisesRegex(receiver.Refusal, 'bounded'):
            receiver.header(io.BytesIO(b'a'*4097+b'\n'), config, manifest)

    def test_missing_reviewed_artifact_pin_refuses_matching_commit_and_digest(self):
        request = self.request()
        config = {'maximum_bytes': {'api': 1024}}
        manifest = {'allowed_commits': {'api': request['commit']}, 'approved_artifacts': {'api': {}}}
        with self.assertRaisesRegex(receiver.Refusal, 'protected development digest'):
            receiver.header(io.BytesIO((json.dumps(request)+'\n').encode()), config, manifest)

    def test_manifest_cannot_select_source_or_other_ct(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(receiver, 'protected', side_effect=lambda p, **kwargs: Path(p)):
            config = self.config(folder)
            manifest = {'host': 'example-dev-host', 'environment': 'dev', 'forbidden_ctids': [904,905],
                        'chat_db': {'ctid': 902}, 'targets': {name: {'ctid': 901} for name in receiver.TARGETS}}
            path = Path(config['manifest'])
            path.write_text(json.dumps(manifest))
            receiver.validate_manifest(config)
            for ctid in (904, 905, 911):
                manifest['targets']['api']['ctid'] = ctid
                path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(receiver.Refusal, 'source or other'):
                    receiver.validate_manifest(config)

    def test_receive_exact_bytes_reuse_and_owned_failed_temporary_cleanup(self):
        payload = b'example artifact'
        request = self.request(payload)
        with tempfile.TemporaryDirectory() as folder, patch.object(receiver, 'protected', side_effect=lambda p, **kwargs: Path(p)):
            config = self.config(folder)
            path = receiver.receive(io.BytesIO(payload), config, request)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(receiver.receive(io.BytesIO(payload), config, request), path)
            for wrong in (payload[:-1], payload+b'extra', b'x'*len(payload)):
                with self.assertRaises(receiver.Refusal):
                    receiver.receive(io.BytesIO(wrong), config, request)
                self.assertEqual(list(Path(folder).rglob('.incoming-*')), [])
                self.assertEqual(path.read_bytes(), payload)

    def test_archive_expansion_and_member_limits_are_real_failure_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.config(folder)
            path = Path(folder)/'bundle.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                info = tarfile.TarInfo('large.txt');info.size=2048
                archive.addfile(info, io.BytesIO(b'x'*2048))
            with self.assertRaises(receiver.Refusal):
                receiver.bounded_archive(path, config, 'console')
            config['maximum_expanded_bytes']['console'] = 65536
            with tarfile.open(path, 'w:gz') as archive:
                for n in range(4):archive.addfile(tarfile.TarInfo(f'file-{n}'))
            with self.assertRaisesRegex(receiver.Refusal, 'inventory'):
                receiver.bounded_archive(path, config, 'console')

    def test_receiver_invokes_only_frozen_tool_with_dev_fixed_argv(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(receiver, 'protected', side_effect=lambda p, **kwargs: Path(p)):
            tool = Path(folder)/'tool.py';tool.write_text('installed code')
            config = {'trusted_tool':str(tool),'trusted_tool_sha256':receiver.digest(tool),'deployment_timeout_seconds':2}
            process = unittest.mock.Mock();process.returncode=0
            request=self.request()
            with patch.object(receiver.subprocess,'Popen',return_value=process) as execute:
                receiver.execute(config,request,Path('/protected/artifact.jar'))
            self.assertEqual(execute.call_args.args[0][3:6],['check','dev','api'])
            self.assertEqual(execute.call_args.kwargs['env']['PATH'],'/usr/sbin:/usr/bin:/sbin:/bin')
            config['trusted_tool_sha256']='0'*64
            with self.assertRaisesRegex(receiver.Refusal,'frozen SHA'):
                receiver.execute(config,request,Path('/protected/artifact.jar'))

    def test_dev_workflow_cannot_receive_operating_key_or_upload_a_tool(self):
        path=SCRIPT.parents[1]/'.github/deploy.example.yml'
        self.assertTrue(path.is_file(), 'development deployment workflow must exist')
        workflow=path.read_text()
        dev=workflow.split('  deploy-development:\n',1)[1].split('  deploy-production:\n',1)[0]
        self.assertIn('secrets.DEV_DEPLOY_SSH_KEY',dev)
        self.assertNotIn('secrets.EXAMPLE_HOP_SSH_KEY',dev)
        self.assertNotIn('secrets.EXAMPLE_PROD_',dev)
        self.assertNotIn('scp ',dev)
        self.assertNotIn('deploy-environment.py',dev)
        self.assertIn('pickle-dev-deploy-v1',dev)
        production=workflow.split('  deploy-production:\n',1)[1]
        self.assertIn('secrets.EXAMPLE_HOP_SSH_KEY',production)
        self.assertIn('secrets.EXAMPLE_PROD_SSH_KEY',production)
        build=workflow.split('  deploy-development:\n',1)[0]
        self.assertNotIn('secrets.',build)

    def test_deleted_workflow_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder)/'scripts/dev-deploy-receiver.py'
            script.parent.mkdir()
            script.write_text('receiver exists but its workflow is missing')
            with patch.dict(globals(), {'SCRIPT': script}):
                with self.assertRaisesRegex(AssertionError, 'workflow must exist'):
                    self.test_dev_workflow_cannot_receive_operating_key_or_upload_a_tool()

    def test_receive_alarm_and_deployment_deadline_are_fail_closed(self):
        with self.assertRaisesRegex(receiver.Refusal,'deadline'):
            receiver.timeout(None,None)
        with tempfile.TemporaryDirectory() as folder, patch.object(receiver,'protected',side_effect=lambda p,**kwargs:Path(p)):
            tool=Path(folder)/'tool.py';tool.write_text('installed code')
            config={'trusted_tool':str(tool),'trusted_tool_sha256':receiver.digest(tool),'deployment_timeout_seconds':1}
            process=unittest.mock.Mock();process.pid=123;process.poll.return_value=None;process.wait.side_effect=[subprocess.TimeoutExpired('tool',1),None]
            with patch.object(receiver.subprocess,'Popen',return_value=process),patch.object(receiver.os,'killpg') as terminate:
                with self.assertRaisesRegex(receiver.Refusal,'execution deadline'):
                    receiver.execute(config,self.request(),Path('/protected/artifact.jar'))
                terminate.assert_called_once_with(123,receiver.signal.SIGKILL)


if __name__=='__main__':
    unittest.main()
