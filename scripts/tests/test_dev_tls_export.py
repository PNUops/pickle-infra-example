#!/usr/bin/env python3
"""Check that the exporter cannot select another Certbot lineage."""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('exporter', Path(__file__).parents[1] / 'dev-tls-export.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lineage = 'dev.example.com'
        for folder in ('archive/' + self.lineage, 'live/' + self.lineage, 'renewal'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        (self.root / 'renewal' / (self.lineage + '.conf')).write_text('account = existing-reference\n')
        for name in ('cert', 'chain', 'fullchain', 'privkey'):
            (self.root / 'archive' / self.lineage / (name + '1.pem')).write_text(name)
            (self.root / 'live' / self.lineage / (name + '.pem')).symlink_to('../../archive/' + self.lineage + '/' + name + '1.pem')
        (self.root / 'accounts').mkdir()
        (self.root / 'accounts' / 'never-export').write_text('account private data')

    def test_selected_files_and_links_only(self):
        data = module.archive(self.root, self.lineage)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            self.assertEqual(len(tar.getmembers()), 9)
            self.assertFalse(any('accounts' in name for name in tar.getnames()))
            self.assertTrue(tar.getmember('live/' + self.lineage + '/privkey.pem').issym())

    def test_cross_lineage_link_rejected(self):
        other = self.root / 'archive' / 'other.example.com'
        other.mkdir()
        (other / 'privkey1.pem').write_text('other private key')
        link = self.root / 'live' / self.lineage / 'privkey.pem'
        link.unlink()
        link.symlink_to('../../archive/other.example.com/privkey1.pem')
        with self.assertRaises(ValueError):
            module.archive(self.root, self.lineage)

    def test_path_injection_rejected(self):
        with self.assertRaises(ValueError):
            module.archive(self.root, '../other')

    def test_archive_file_symlink_rejected(self):
        target = self.root / 'archive' / self.lineage / 'privkey1.pem'
        target.unlink()
        target.symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            module.archive(self.root, self.lineage)


if __name__ == '__main__':
    unittest.main()
