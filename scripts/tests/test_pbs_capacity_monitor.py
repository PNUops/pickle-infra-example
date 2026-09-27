#!/usr/bin/env python3
"""Check transition rules using synthetic datastore reports."""
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

MODULE = Path(__file__).parents[1] / 'pbs-capacity-monitor.py'
SPEC = importlib.util.spec_from_file_location('example_capacity_monitor', MODULE)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class CapacityMonitorTests(unittest.TestCase):
    def test_corrupt_state_receipt_fails_closed(self):
        corrupt = {'status': 'UNEXPECTED', 'sequence': 1,
                   'delivery': 'SENT', 'attempts': 0}
        with self.assertRaises(monitor.MonitorFailure):
            monitor.validate_state(corrupt)
        for field, value in (('sequence', True), ('attempts', True),
                             ('delivery', 'UNKNOWN')):
            receipt = {'status': 'WARNING', 'sequence': 1,
                       'delivery': 'SENT', 'attempts': 0}
            receipt[field] = value
            with self.subTest(field=field), self.assertRaises(monitor.MonitorFailure):
                monitor.validate_state(receipt)

    def test_retry_schedule_is_bounded_and_clock_reversal_fails_closed(self):
        calls = []

        def rejected(event, report, sequence):
            calls.append((event, sequence))
            raise monitor.SubmissionRejected('synthetic explicit rejection')

        def read_warning(now):
            return {'status': 'WARNING', 'checked_at': now.isoformat(), 'avail_bytes': 10}

        def parse_receipt(path):
            return json.loads(path.read_text(encoding='utf-8'))

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'state'
            def prepare(path):
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.chmod(0o700)

            with patch.object(monitor.os, 'geteuid', return_value=0), \
                    patch.object(monitor.socket, 'gethostname', return_value='pve-node-3'), \
                    patch.object(monitor, 'require_state_dir', side_effect=prepare), \
                    patch.object(monitor, 'private_json', side_effect=parse_receipt):
                with self.assertRaises(monitor.SubmissionRejected):
                    monitor.run(start, state_dir=directory, status_reader=read_warning,
                                mail_sender=rejected)
                state_path = directory / 'state.json'
                state = json.loads(state_path.read_text())
                self.assertEqual((state['attempts'], state['delivery']), (1, 'NOT_SENT'))
                self.assertEqual(state['last_check_epoch'], start.timestamp())

                with self.assertRaises(monitor.MonitorFailure):
                    monitor.run(start - timedelta(seconds=10), state_dir=directory,
                                status_reader=read_warning, mail_sender=rejected)
                self.assertEqual(len(calls), 1)

                monitor.run(start + timedelta(seconds=59), state_dir=directory,
                            status_reader=read_warning, mail_sender=rejected)
                self.assertEqual(len(calls), 1)
                with self.assertRaises(monitor.SubmissionRejected):
                    monitor.run(start + timedelta(seconds=60), state_dir=directory,
                                status_reader=read_warning, mail_sender=rejected)
                state = json.loads(state_path.read_text())
                self.assertEqual((state['attempts'], state['delivery']), (2, 'NOT_SENT'))

                monitor.run(start + timedelta(seconds=359), state_dir=directory,
                            status_reader=read_warning, mail_sender=rejected)
                self.assertEqual(len(calls), 2)
                with self.assertRaises(monitor.SubmissionRejected):
                    monitor.run(start + timedelta(seconds=360), state_dir=directory,
                                status_reader=read_warning, mail_sender=rejected)
                state = json.loads(state_path.read_text())
                self.assertEqual((state['attempts'], state['delivery']), (3, 'RETRIES_EXHAUSTED'))
                monitor.run(start + timedelta(hours=1), state_dir=directory,
                            status_reader=read_warning, mail_sender=rejected)
                self.assertEqual(len(calls), 3)

    def test_state_loader_rejects_dangling_and_live_symlinks_and_directories(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            dangling = root / 'dangling.json'
            dangling.symlink_to(root / 'missing.json')
            with self.subTest(kind='dangling symlink'), self.assertRaises(monitor.MonitorFailure):
                monitor.load_state(dangling)

            target = root / 'target.json'
            target.write_text(json.dumps({'status': 'OK', 'sequence': 1,
                                          'delivery': 'BASELINE', 'attempts': 0}))
            target.chmod(0o600)
            linked = root / 'linked.json'
            linked.symlink_to(target)
            with self.subTest(kind='live symlink'), self.assertRaises(monitor.MonitorFailure):
                monitor.load_state(linked)

            directory = root / 'directory.json'
            directory.mkdir()
            with self.subTest(kind='directory receipt'), self.assertRaises(monitor.MonitorFailure):
                monitor.load_state(directory)

    def test_first_healthy_sample_is_a_silent_baseline(self):
        state, event = monitor.transition({}, {'status': 'OK'})
        self.assertIsNone(event)
        self.assertEqual((state['delivery'], state['sequence']), ('BASELINE', 1))

    def test_warning_escalates_and_recovery_notifies(self):
        state, event = monitor.transition({'status': 'OK', 'sequence': 1}, {'status': 'WARNING'})
        self.assertEqual((event, state['delivery']), ('WARNING', 'ATTEMPTED'))
        state, event = monitor.transition(state, {'status': 'CRITICAL'})
        self.assertEqual(event, 'CRITICAL')
        state, event = monitor.transition(state, {'status': 'OK'})
        self.assertEqual(event, 'RECOVERED')

    def test_repeated_status_does_not_create_a_new_transition(self):
        previous = {'status': 'CRITICAL', 'sequence': 4, 'delivery': 'UNCERTAIN'}
        state, event = monitor.transition(previous, {'status': 'CRITICAL'})
        self.assertIsNone(event)
        self.assertEqual(state['sequence'], 4)
        self.assertEqual(state['delivery'], 'UNCERTAIN')


if __name__ == '__main__':
    unittest.main()
