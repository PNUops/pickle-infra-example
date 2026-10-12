"""Host-only key custody and append-only authorization failure checks."""

import base64
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'prepare-dev-deploy-key.py'
SPEC=importlib.util.spec_from_file_location('prepare_dev_key',SCRIPT)
key=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(key)


class KeyPreparationTest(unittest.TestCase):
    def test_existing_key_directory_never_generates_or_overwrites(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(key.subprocess,'run') as command:
            with self.assertRaisesRegex(key.Refusal,'already exists'):
                key.create_key(Path(folder))
            command.assert_not_called()

    def test_missing_runtime_does_not_return_private_data(self):
        with patch.object(key.ctypes.util,'find_library',return_value=None):
            with self.assertRaisesRegex(key.Refusal,'no private key leaves'):
                key.sealed_box(b'fixture-not-a-private-key',b'x'*32)

    def test_authorized_rows_append_preserves_original_bytes_and_detects_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);receiver=root/'receiver.py';receiver.write_text('frozen receiver')
            authorized=root/'authorized_keys';before=b'original authorized row\n';authorized.write_bytes(before)
            public=b'ssh-ed25519 QUJD example-only\n'
            with patch.object(key,'RECEIVER',receiver),patch.object(key,'BACKUP_ROOT',root/'backup'), \
                 patch.object(key.Path,'lstat',return_value=SimpleNamespace(st_uid=0,st_mode=0o100600)), \
                 patch.object(key.Path,'stat',return_value=SimpleNamespace(st_uid=0,st_mode=0o100600)):
                with self.assertRaisesRegex(key.Refusal,'changed'):
                    key.append_restricted_row(public,authorized,'0'*64,hashlib.sha256(receiver.read_bytes()).hexdigest())
                self.assertEqual(authorized.read_bytes(),before)
                key.append_restricted_row(public,authorized,hashlib.sha256(before).hexdigest(),hashlib.sha256(receiver.read_bytes()).hexdigest())
            after=authorized.read_bytes()
            self.assertTrue(after.startswith(before))
            self.assertIn(b'restrict,command="/usr/bin/python3 -I /opt/pickle/dev-deploy/receiver.py"',after)
            backups=list((root/'backup').rglob('authorized_keys.before'))
            self.assertEqual(len(backups),1)
            self.assertEqual(backups[0].read_bytes(),before)


if __name__=='__main__':unittest.main()
