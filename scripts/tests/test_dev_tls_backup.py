#!/usr/bin/env python3
"""Validate restored archive rejection without contacting a host or PBS."""
import importlib.util
import io
from pathlib import Path
import tarfile
import json
import tempfile
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('export_tests', HERE / 'test_dev_tls_export.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
spec = importlib.util.spec_from_file_location('backup', HERE.parent / 'dev-tls-backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class RestoreTests(fixtures.ExportTests):
    def test_valid_restore_checks_certificate_and_key(self):
        raw = fixtures.module.archive(self.root, self.lineage)
        with patch.object(backup, 'run', return_value=b'public-key') as calls:
            self.assertEqual(len(backup.inspect_archive(raw, self.lineage)['archive_sha256']), 64)
            self.assertEqual(calls.call_count, 4)

    def test_mismatched_key_rejected(self):
        raw = fixtures.module.archive(self.root, self.lineage)
        with patch.object(backup, 'run', side_effect=[b'certificate-key', b'other-key']):
            with self.assertRaises(ValueError):
                backup.inspect_archive(raw, self.lineage)

    def test_unexpected_account_member_rejected(self):
        output = io.BytesIO(fixtures.module.archive(self.root, self.lineage))
        with tarfile.open(fileobj=output, mode='a') as tar:
            item = tarfile.TarInfo('accounts/private-key')
            item.size = 1
            tar.addfile(item, io.BytesIO(b'x'))
        with self.assertRaises(ValueError):
            backup.inspect_archive(output.getvalue(), self.lineage)

    def worker(self, failure=None, previous=None):
        raw = fixtures.module.archive(self.root, self.lineage)
        state = None
        if previous:
            with patch.object(backup, 'run', return_value=b'public-key'):
                state = {'version': 1, 'point': 123, **backup.inspect_archive(raw, self.lineage), 'ciphertext_sha256': 'a' * 64}
            if previous == 'changed':
                (self.root / 'renewal' / (self.lineage + '.conf')).write_text('account = new-reference\n')
                raw = fixtures.module.archive(self.root, self.lineage)
        config = {'hostname': 'test', 'namespace': 'dev-tls', 'backup_id': 'node-dev-dev-tls',
                  'ssh_port': 22, 'ssh_destination': 'root@dev.example.com',
                  'known_hosts': '/safe/pin', 'ssh_key': '/safe/read-key',
                  'recipient_certificate': '/safe/recipient', 'recipient_key': '/safe/key',
                  'lineage': self.lineage, 'writer_repository': 'writer@example:store',
                  'reader_repository': 'reader@example:store', 'pbs_key': '/safe/pbs-key',
                  'writer_token': '/safe/writer', 'reader_token': '/safe/reader',
                  'pbs_fingerprint': 'pin'}
        calls = []
        def native(argv, payload=None, env=None):
            calls.append((argv, env))
            if argv[0] == 'ssh': return b'encrypted-ciphertext'
            if argv[:3] == ['openssl', 'cms', '-decrypt']: return raw
            if argv[:3] == ['proxmox-backup-client', 'snapshot', 'list']:
                if failure == 'inventory': raise subprocess.CalledProcessError(1, argv)
                return json.dumps([] if previous == 'missing' else [{'backup-type': 'host', 'backup-id': config['backup_id'], 'backup-time': 123, 'files': ['tls.pxar.didx']}]).encode()
            if argv[:2] == ['proxmox-backup-client', 'backup']:
                if failure == 'backup': raise subprocess.CalledProcessError(1, argv)
                self.assertIn('encrypt', argv)
                self.assertEqual(env['PBS_PASSWORD_FILE'], '/safe/writer')
                self.assertNotIn('PBS_PASSWORD', env)
            if argv[:2] == ['proxmox-backup-client', 'restore']:
                self.assertEqual(env['PBS_PASSWORD_FILE'], '/safe/reader')
                folder = Path(argv[4]); folder.mkdir()
                (folder / 'lineage.cms').write_bytes(b'wrong' if failure == 'restore' else b'encrypted-ciphertext')
            return b'public-key'
        real_temp = tempfile.TemporaryDirectory
        def stage(**kwargs): return real_temp(dir=self.temp.name)
        with patch.object(Path, 'read_text', return_value=json.dumps(config)), \
             patch.object(backup.socket, 'gethostname', return_value='test'), \
             patch.object(backup.os, 'geteuid', return_value=0), \
             patch.object(backup.tempfile, 'TemporaryDirectory', side_effect=stage), \
             patch.object(backup, 'read_state', return_value=state), \
             patch.object(backup, 'write_state') as save, \
             patch.object(backup, 'run', side_effect=native), \
             patch('builtins.print'):
            if failure:
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    backup.main()
            else:
                backup.main()
            self.assertEqual(save.call_count, 0 if failure or previous == 'unchanged' else 1)
        return calls

    def test_native_backup_and_restore_use_separate_tokens(self):
        calls = self.worker()
        self.assertEqual(sum(argv[:2] == ['proxmox-backup-client', 'restore'] for argv, env in calls), 1)

    def test_backup_failure_does_not_restore_or_report_success(self):
        calls = self.worker('backup')
        self.assertFalse(any(argv[:2] == ['proxmox-backup-client', 'restore'] for argv, env in calls))

    def test_native_restore_mismatch_is_failure(self):
        self.worker('restore')

    def test_unchanged_existing_point_does_not_write_or_restore(self):
        calls = self.worker(previous='unchanged')
        self.assertFalse(any(argv[:2] in [['proxmox-backup-client', 'backup'], ['proxmox-backup-client', 'restore']] for argv, env in calls))

    def test_renewal_config_change_requires_new_backup_and_restore(self):
        calls = self.worker(previous='changed')
        self.assertTrue(any(argv[:2] == ['proxmox-backup-client', 'restore'] for argv, env in calls))

    def test_missing_point_requires_new_backup_and_restore(self):
        calls = self.worker(previous='missing')
        self.assertTrue(any(argv[:2] == ['proxmox-backup-client', 'restore'] for argv, env in calls))

    def test_inventory_failure_does_not_write_or_advance_state(self):
        calls = self.worker('inventory', previous='unchanged')
        self.assertFalse(any(argv[:2] == ['proxmox-backup-client', 'backup'] for argv, env in calls))

    def test_symlink_target_changes_digest(self):
        raw = fixtures.module.archive(self.root, self.lineage)
        with patch.object(backup, 'run', return_value=b'public-key'):
            old = backup.inspect_archive(raw, self.lineage)
            for stem in ('cert', 'chain', 'fullchain', 'privkey'):
                (self.root / 'archive' / self.lineage / (stem + '2.pem')).write_text(stem)
                link = self.root / 'live' / self.lineage / (stem + '.pem')
                link.unlink()
                link.symlink_to('../../archive/' + self.lineage + '/' + stem + '2.pem')
            new = backup.inspect_archive(fixtures.module.archive(self.root, self.lineage), self.lineage)
        self.assertEqual(old['certificate_sha256'], new['certificate_sha256'])
        self.assertNotEqual(old['archive_sha256'], new['archive_sha256'])

    def state_fixture(self, contents):
        path = self.root / 'state.json'
        path.write_text(contents)
        path.chmod(0o600)
        return path

    def test_malformed_state_rejected(self):
        path = self.state_fixture('{"version":1}')
        actual = backup.os.fstat
        def owned(fd):
            info = actual(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink, st_size=info.st_size)
        with patch.object(backup, 'state_directory'), patch.object(backup.os, 'fstat', side_effect=owned):
            with self.assertRaises(ValueError): backup.read_state(path)

    def test_unknown_state_owner_rejected(self):
        path = self.state_fixture('{}')
        with patch.object(backup, 'state_directory'), patch.object(backup.os, 'fstat', return_value=SimpleNamespace(st_mode=0o100600, st_uid=1000, st_nlink=1, st_size=2)):
            with self.assertRaises(ValueError): backup.read_state(path)

    def test_state_symlink_rejected(self):
        target = self.state_fixture('{}')
        link = self.root / 'linked-state.json'; link.symlink_to(target)
        with patch.object(backup, 'state_directory'):
            with self.assertRaises(OSError): backup.read_state(link)

    def test_unknown_state_directory_owner_rejected(self):
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o40700, st_uid=1000)):
            with self.assertRaises(ValueError): backup.state_directory(self.root / 'state.json')

    def test_state_directory_unsafe_mode_rejected(self):
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o40755, st_uid=0)):
            with self.assertRaises(ValueError): backup.state_directory(self.root / 'state.json')


if __name__ == '__main__':
    unittest.main()
