#!/usr/bin/env python3
"""Offline durability and safety cases for the dedicated observation outbox."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from observation_spool import ObservationStore, StoreError


EPOCH = '00000000-0000-4000-8000-000000000001'
OTHER_EPOCH = '00000000-0000-4000-8000-000000000002'


class ObservationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'outbox'

    def store(self, **limits):
        return ObservationStore(self.path, **limits)

    def test_duplicate_key_and_content_is_idempotent_without_rewriting(self):
        with self.store().locked() as box:
            self.assertTrue(box.put(EPOCH, 1, {'observedAt': 100, 'value': 7}))
            first = box.pending(10)[0]
            before = (self.path / first.name).stat().st_mtime_ns
            self.assertFalse(box.put(EPOCH, 1, {'value': 7, 'observedAt': 100}))
            self.assertEqual((self.path / first.name).stat().st_mtime_ns, before)
            self.assertEqual(len(box.pending(10)), 1)

    def test_conflicting_duplicate_preserves_the_original(self):
        with self.store().locked() as box:
            box.put(EPOCH, 1, {'value': 7})
            with self.assertRaisesRegex(StoreError, '^CONTENT_CONFLICT$'):
                box.put(EPOCH, 1, {'value': 8})
            self.assertEqual(box.pending(10)[0].document, {'value': 7})

    def test_capacity_refuses_new_data_without_deleting_pending_records(self):
        with self.store(max_samples=1).locked() as box:
            box.put(EPOCH, 1, {'value': 7})
            with self.assertRaisesRegex(StoreError, '^STORE_FULL$'):
                box.put(EPOCH, 2, {'value': 8})
            self.assertFalse(box.put(EPOCH, 1, {'value': 7}))
            self.assertEqual([r.sequence for r in box.pending(10)], [1])

    def test_byte_capacity_and_single_record_limit_are_independent(self):
        with self.store(max_bytes=20, max_sample_bytes=16).locked() as box:
            box.put(EPOCH, 1, {'v': '1234'})
            with self.assertRaisesRegex(StoreError, '^STORE_FULL$'):
                box.put(EPOCH, 2, {'v': '5678'})
            with self.assertRaisesRegex(StoreError, '^SAMPLE_TOO_LARGE$'):
                box.put(EPOCH, 2, {'v': 'x' * 30})
            self.assertEqual(len(box.pending(10)), 1)

    def test_ack_removes_only_the_unchanged_record_sent(self):
        with self.store().locked() as box:
            box.put(EPOCH, 1, {'value': 7})
            box.put(OTHER_EPOCH, 1, {'value': 8})
            sent = next(r for r in box.pending(10) if r.epoch == EPOCH)
            box.ack(sent)
            self.assertEqual([r.epoch for r in box.pending(10)], [OTHER_EPOCH])

    def test_mutation_between_send_and_ack_does_not_delete_the_file(self):
        with self.store().locked() as box:
            box.put(EPOCH, 1, {'value': 7})
            sent = box.pending(1)[0]
            (self.path / sent.name).write_text('{"value":8}\n')
            with self.assertRaisesRegex(StoreError, '^CONTENT_CHANGED$'):
                box.ack(sent)
            self.assertTrue((self.path / sent.name).exists())

    def test_symlinks_and_nonprivate_files_are_refused_without_body_disclosure(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.write_text('SYNTHETIC_PRIVATE_CANARY')
        with self.store().locked() as box:
            box.put(EPOCH, 1, {'value': 7})
            record = box.pending(1)[0]
            target = self.path / record.name
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaises(StoreError) as error:
                box.pending(1)
            self.assertNotIn('CANARY', str(error.exception))
            target.unlink()
            target.write_text('{}\n')
            target.chmod(0o644)
            with self.assertRaisesRegex(StoreError, '^FILE_NOT_PRIVATE$'):
                box.pending(1)

    def test_unknown_paths_and_unbounded_keys_are_not_opened(self):
        with self.store().locked() as box:
            for epoch, sequence in [('../outside', 1), (EPOCH, -1), (EPOCH, True), (EPOCH, 2**53)]:
                with self.subTest(epoch=epoch, sequence=sequence):
                    with self.assertRaisesRegex(StoreError, '^RECORD_KEY_INVALID$'):
                        box.put(epoch, sequence, {})
            (self.path / 'unexpected').write_text('SYNTHETIC_PRIVATE_CANARY')
            with self.assertRaisesRegex(StoreError, '^STORE_ENTRY_INVALID$'):
                box.pending(1)

    def test_private_directory_and_record_permissions_are_retained(self):
        with self.store().locked() as box:
            box.put(EPOCH, 1, {'value': 7})
            record = box.pending(1)[0]
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.path / record.name).stat().st_mode), 0o600)
        self.path.chmod(0o755)
        with self.assertRaisesRegex(StoreError, '^DIRECTORY_NOT_PRIVATE$'):
            with self.store().locked():
                pass

    def test_lock_contention_never_overlaps_a_writer(self):
        with self.store().locked():
            with self.assertRaisesRegex(StoreError, '^ALREADY_RUNNING$'):
                with self.store().locked():
                    pass

    def test_nonfinite_and_invalid_documents_are_never_persisted(self):
        with self.store().locked() as box:
            for value in [[], {'value': float('nan')}, {'value': float('inf')}]:
                with self.subTest(value=type(value).__name__):
                    with self.assertRaisesRegex(StoreError, '^DOCUMENT_INVALID$'):
                        box.put(EPOCH, 1, value)
            self.assertEqual(box.pending(1), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
