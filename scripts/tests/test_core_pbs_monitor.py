"""Offline checks for the independent pve-node-3 core PBS archive monitor."""
import importlib.util
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import smtplib
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[1] / 'core-pbs-monitor.py'
spec = importlib.util.spec_from_file_location('core_pbs_monitor', SCRIPT)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
KST = ZoneInfo('Asia/Seoul')
DAY = datetime(2025, 1, 2, 4, 31, tzinfo=KST)


def rows(now=DAY, *, daily=True):
    result = []
    for ctid in (1200, 1201, 1202, 1203, 1204):
        base = {'content': 'backup', 'encrypted': monitor.FINGERPRINT,
                'format': 'pbs-ct', 'subtype': 'lxc', 'vmid': ctid, 'size': 1024}
        manual = monitor.MANUAL.get(ctid)
        if manual is not None:
            manual_stamp = manual.rsplit('/', 1)[1]
            manual_epoch = int(datetime.strptime(manual_stamp, '%Y-%m-%dT%H:%M:%SZ')
                               .replace(tzinfo=timezone.utc).timestamp())
            result.append(dict(base, protected=1, ctime=manual_epoch, volid=manual))
        if daily:
            stamp = datetime.combine(now.date(), datetime.min.time(), KST) + timedelta(hours=3, minutes=18)
            result.append(dict(base, protected=0, ctime=int(stamp.timestamp()),
                               volid=f'pbs-example-core-read:backup/ct/{ctid}/{stamp.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")}'))
    return result


class CorePbsMonitorTest(unittest.TestCase):
    def test_five_daily_members_preserve_exact_four_protected_baselines(self):
        report = monitor.inspect(rows(), DAY)
        self.assertEqual(set(report['latest']), {'1200', '1201', '1202', '1203', '1204'})
        self.assertEqual(set(monitor.MANUAL), {1200, 1201, 1202, 1204})
        missing = [row for row in rows() if row['vmid'] != 1203]
        self.assertEqual(monitor.inspect(missing, DAY)['reason'], 'CT1203:MISSING')
        self.assertEqual(monitor.inspect(missing, DAY - timedelta(minutes=2))['status'], 'PENDING')

    def test_ct1203_metadata_validation_and_last_valid_point_are_independent(self):
        data = rows()
        invalid = deepcopy(next(row for row in data if row['vmid'] == 1203))
        invalid['ctime'] += 1
        data.append(invalid)
        report = monitor.inspect(data, DAY)
        self.assertIn('CT1203:INVALID_METADATA', report['reason'])
        diagnostic = report['last_valid_points']['1203']
        self.assertEqual(diagnostic['ctime'], invalid['ctime'] - 1)
        self.assertGreater(diagnostic['age_seconds'], 0)
        self.assertNotIn('1203', report['latest'])

    def test_stale_valid_points_keep_real_ages_separate_from_fresh_daily_verdict(self):
        current = DAY.replace(month=10, day=20)
        report = monitor.inspect(rows(current - timedelta(days=1)), current)
        self.assertEqual(report['status'], 'FAILED')
        self.assertEqual(report['latest'], {})
        self.assertEqual(set(report['last_valid_points']), {'1200', '1201', '1202', '1203', '1204'})
        self.assertTrue(all(point['age_seconds'] > 86400 for point in report['last_valid_points'].values()))

    def test_legacy_delivery_matrix_preserves_provenance_without_replaying_mail(self):
        for delivery in ('SENT', 'UNCERTAIN', 'ATTEMPTED', 'RETRIES_EXHAUSTED', 'NOT_SENT'):
            with self.subTest(delivery=delivery), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(monitor, 'require_root'):
                path = Path(temporary)
                path.chmod(0o700)
                legacy = {'status': 'FAILED', 'sequence': 8, 'event': 'FAILED', 'delivery': delivery,
                          'attempts': 2, 'retry_after': DAY.timestamp() + 10000,
                          'last_check_epoch': DAY.timestamp() - 300,
                          'report': {'status': 'FAILED', 'date': '2025-01-01', 'reason': 'OLD', 'latest': {}}}
                monitor.atomic_json(path / 'state.json', legacy)
                monitor.run(DAY, fetcher=lambda: rows(daily=False), state_dir=path,
                            sender=lambda *args: self.fail('replayed legacy delivery'))
                updated = monitor.json.loads((path / 'state.json').read_text())
                for key in ('status', 'sequence', 'event', 'delivery', 'attempts', 'retry_after', 'report'):
                    self.assertEqual(updated[key], legacy[key])
                self.assertEqual(updated['legacy_available_report']['report'], legacy['report'])
                self.assertEqual(updated['legacy_available_report']['provenance'], 'LEGACY_AVAILABLE_REPORT')
                self.assertNotIn('first_report', updated['incident'])
                self.assertEqual(updated['last_observation']['observed_at_epoch'], DAY.timestamp())
                self.assertEqual(updated['last_observation']['query_status'], 'OK')

    def test_new_incident_first_report_is_immutable_while_latest_observation_advances(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            sent = []
            monitor.run(DAY, fetcher=lambda: [row for row in rows() if row['vmid'] != 1203],
                        state_dir=path, sender=lambda report, event: sent.append(event))
            first = monitor.json.loads((path / 'state.json').read_text())['incident']['first_report']
            monitor.run(DAY + timedelta(minutes=5), fetcher=lambda: rows(daily=False),
                        state_dir=path, sender=lambda report, event: sent.append(event))
            latest = monitor.json.loads((path / 'state.json').read_text())
            self.assertEqual(latest['incident']['first_report'], first)
            self.assertEqual(first['reason'], 'CT1203:MISSING')
            self.assertNotEqual(latest['last_observation']['reason'], first['reason'])
            self.assertEqual(sent, ['FAILED'])

    def test_query_failure_preserves_inventory_time_and_increases_cached_point_ages(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            monitor.run(DAY, fetcher=rows, state_dir=path, sender=lambda *args: None)
            def unavailable():
                raise monitor.MonitorError('query unavailable')
            later = DAY + timedelta(minutes=5)
            report, event = monitor.run(later, fetcher=unavailable, state_dir=path, sender=lambda *args: None)
            state = monitor.json.loads((path / 'state.json').read_text())
            observation = state['last_observation']
            self.assertEqual((report['status'], event), ('FAILED', 'FAILED'))
            self.assertEqual(observation['query_status'], 'UNAVAILABLE')
            self.assertEqual(observation['inventory_observed_at_epoch'], DAY.timestamp())
            self.assertGreater(observation['last_valid_points']['1203']['age_seconds'], 300)
            self.assertEqual(observation['point_evidence'], 'CACHED')
            monitor.run(later + timedelta(minutes=5), fetcher=unavailable, state_dir=path,
                        sender=lambda *args: self.fail('repeated failed query resent mail'))
            repeated = monitor.json.loads((path / 'state.json').read_text())['last_observation']
            self.assertEqual(repeated['inventory_observed_at_epoch'], DAY.timestamp())
            self.assertEqual(repeated['last_valid_points']['1203']['ctime'], observation['last_valid_points']['1203']['ctime'])
            self.assertEqual(repeated['last_valid_points']['1203']['age_seconds'], observation['last_valid_points']['1203']['age_seconds'] + 300)

    def test_first_report_is_detached_and_new_episode_gets_its_own_first_report(self):
        failed = monitor.inspect(rows(daily=False), DAY)
        state, _ = monitor.transition({'status': 'HEALTHY', 'sequence': 1}, failed)
        first = deepcopy(state['incident']['first_report'])
        failed['reason'] = 'CALLER_MUTATED'
        self.assertEqual(state['incident']['first_report'], first)
        state['delivery'] = 'UNCERTAIN'
        recovered, event = monitor.transition(state, monitor.inspect(rows(), DAY))
        self.assertEqual(event, 'RECOVERED')
        self.assertEqual(recovered['incident']['first_report'], first)
        self.assertEqual(recovered['previous_notification']['delivery'], 'UNCERTAIN')
        next_report = monitor.inspect(rows(daily=False), DAY + timedelta(days=1))
        new, event = monitor.transition(recovered, next_report)
        self.assertEqual(event, 'FAILED')
        self.assertNotEqual(new['incident']['sequence'], state['incident']['sequence'])
        self.assertEqual(new['incident']['first_report'], next_report)
        self.assertEqual(state['incident']['first_report'], first)

    def test_latest_observation_is_durable_before_rejected_mail_submission(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            def reject(report, event):
                stored = monitor.json.loads((path / 'state.json').read_text())
                self.assertEqual(stored['last_observation'], report)
                self.assertEqual(stored['incident']['first_report'], report)
                self.assertEqual(stored['delivery'], 'ATTEMPTED')
                raise monitor.MailNotSent('fixture rejection')
            with self.assertRaises(monitor.MailNotSent):
                monitor.run(DAY, fetcher=lambda: rows(daily=False), state_dir=path, sender=reject)
            stored = monitor.json.loads((path / 'state.json').read_text())
            self.assertEqual(stored['last_observation']['observed_at_epoch'], DAY.timestamp())
            self.assertEqual((stored['delivery'], stored['attempts']), ('NOT_SENT', 1))

    def test_unknown_observation_schema_is_not_rewritten_or_submitted(self):
        for version in (True, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(monitor, 'require_root'):
                path = Path(temporary)
                path.chmod(0o700)
                monitor.atomic_json(path / 'state.json', {'observation_schema_version': version})
                original = (path / 'state.json').read_bytes()
                with self.assertRaises(monitor.MonitorError):
                    monitor.run(DAY, fetcher=rows, state_dir=path, sender=lambda *args: self.fail())
                self.assertEqual((path / 'state.json').read_bytes(), original)

    def test_first_query_failure_has_unknown_point_ages_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            def unavailable():
                raise monitor.MonitorError('query unavailable')
            monitor.run(DAY, fetcher=unavailable, state_dir=path, sender=lambda *args: None)
            observation = monitor.json.loads((path / 'state.json').read_text())['last_observation']
            self.assertIsNone(observation['inventory_observed_at_epoch'])
            self.assertTrue(all(point['ctime'] is None and point['age_seconds'] is None
                                for point in observation['last_valid_points'].values()))

    def test_pending_observation_does_not_close_failed_incident_or_change_delivery(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            sent = []
            monitor.run(DAY, fetcher=lambda: rows(daily=False), state_dir=path,
                        sender=lambda report, event: sent.append(event))
            original = monitor.json.loads((path / 'state.json').read_text())
            later = (DAY + timedelta(days=1)).replace(hour=3, minute=0)
            report, event = monitor.run(later, fetcher=lambda: rows(daily=False), state_dir=path,
                                       sender=lambda report, event: sent.append(event))
            state = monitor.json.loads((path / 'state.json').read_text())
            self.assertEqual((report['status'], event), ('PENDING', 'PENDING'))
            self.assertEqual(state['last_observation']['status'], 'PENDING')
            self.assertEqual(state['incident']['first_report'], original['incident']['first_report'])
            for key in ('status', 'sequence', 'event', 'delivery', 'attempts', 'report'):
                self.assertEqual(state[key], original[key])
            self.assertEqual(sent, ['FAILED'])

    def test_baseline_and_fresh_daily_set(self):
        report = monitor.inspect(rows(), DAY)
        self.assertEqual(report['status'], 'HEALTHY')
        state, event = monitor.transition({}, report)
        self.assertEqual((event, state['delivery']), ('BASELINE', 'BASELINE'))

    def test_missing_daily_set_fails_after_grace(self):
        self.assertEqual(monitor.inspect(rows(daily=False), DAY - timedelta(minutes=2))['status'], 'PENDING')
        self.assertEqual(monitor.inspect(rows(daily=False), DAY)['status'], 'FAILED')

    def test_first_due_date_baselines_manual_points_then_requires_daily_set(self):
        installation = datetime(2025, 1, 1, 22, 30, tzinfo=KST)
        self.assertEqual(monitor.inspect(rows(daily=False), installation)['status'], 'HEALTHY')
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            calls = []
            sender = lambda report, event: calls.append(event)
            _, event = monitor.run(installation, fetcher=lambda: rows(daily=False),
                                   sender=sender, state_dir=path)
            self.assertEqual(event, 'BASELINE')
            monitor.run(DAY - timedelta(minutes=2), fetcher=lambda: rows(daily=False),
                        sender=sender, state_dir=path)
            self.assertEqual(calls, [])
            _, event = monitor.run(DAY, fetcher=lambda: rows(daily=False),
                                   sender=sender, state_dir=path)
            self.assertEqual((event, calls), ('FAILED', ['FAILED']))
        self.assertEqual(monitor.inspect(rows(daily=False), DAY,
                                         first_due_date=date(2025, 1, 2))['status'], 'FAILED')

    def test_first_run_after_due_date_alerts_without_baseline_suppression(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            calls = []
            report, event = monitor.run(DAY, fetcher=lambda: rows(daily=False),
                                        sender=lambda report, event: calls.append(event),
                                        state_dir=path)
            self.assertEqual((report['status'], event, calls), ('FAILED', 'FAILED', ['FAILED']))

    def test_bad_metadata_and_manual_point_loss(self):
        data = rows()
        data[-1]['encrypted'] = 'wrong'
        self.assertIn('CT1204:INVALID_METADATA', monitor.inspect(data, DAY)['reason'])
        data = rows()
        data[0]['protected'] = 0
        self.assertIn('CT1200:PROTECTED_BASELINE_MISSING', monitor.inspect(data, DAY)['reason'])
        data = rows()
        data[-1]['volid'] = 'pbs-example-core-read:backup/ct/1200/wrong'
        self.assertIn('CT1204:INVALID_METADATA', monitor.inspect(data, DAY)['reason'])
        data = rows()
        data[-1]['ctime'] += 1
        self.assertIn('CT1204:INVALID_METADATA', monitor.inspect(data, DAY)['reason'])
        data = rows()
        data[-1]['volid'] = 'pbs-example-core-read:backup/ct/1204/2025-01-02T03:18:00+00:00'
        self.assertIn('CT1204:INVALID_METADATA', monitor.inspect(data, DAY)['reason'])

    def test_transition_failure_recovery_and_duplicate(self):
        healthy = monitor.inspect(rows(), DAY)
        failed = monitor.inspect(rows(daily=False), DAY)
        state, self_event = monitor.transition({}, healthy)
        self.assertEqual(self_event, 'BASELINE')
        state, event = monitor.transition(state, failed)
        self.assertEqual(event, 'FAILED')
        duplicate, event = monitor.transition(state, failed)
        self.assertEqual((duplicate, event), (state, 'UNCHANGED'))
        state, event = monitor.transition(state, healthy)
        self.assertEqual(event, 'RECOVERED')

    def test_uncertain_smtp_is_not_resent(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            calls = []
            def uncertain(report, event):
                calls.append(event)
                raise monitor.MailUncertain('unknown')
            with self.assertRaises(monitor.MailUncertain):
                monitor.run(DAY, fetcher=lambda: rows(daily=False), sender=uncertain, state_dir=path)
            self.assertEqual(monitor.json.loads((path / 'state.json').read_text())['delivery'], 'UNCERTAIN')
            monitor.run(DAY, fetcher=lambda: rows(daily=False), sender=uncertain, state_dir=path)
            self.assertEqual(calls, ['FAILED'])

    def test_definitive_not_sent_retries_after_one_and_five_minutes(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            calls = []
            def rejected(report, event):
                calls.append(event)
                raise monitor.MailNotSent('before DATA')
            def check(expected_delivery, expected_attempts):
                state = monitor.json.loads((path / 'state.json').read_text())
                self.assertEqual((state['delivery'], state['attempts']),
                                 (expected_delivery, expected_attempts))
            with self.assertRaises(monitor.MailNotSent):
                monitor.run(DAY, fetcher=lambda: rows(daily=False), sender=rejected, state_dir=path)
            check('NOT_SENT', 1)
            monitor.run(DAY + timedelta(seconds=59), fetcher=lambda: rows(daily=False),
                        sender=rejected, state_dir=path)
            self.assertEqual(len(calls), 1)
            with self.assertRaises(monitor.MailNotSent):
                monitor.run(DAY + timedelta(seconds=60), fetcher=lambda: rows(daily=False),
                            sender=rejected, state_dir=path)
            check('NOT_SENT', 2)
            monitor.run(DAY + timedelta(seconds=359), fetcher=lambda: rows(daily=False),
                        sender=rejected, state_dir=path)
            self.assertEqual(len(calls), 2)
            with self.assertRaises(monitor.MailNotSent):
                monitor.run(DAY + timedelta(seconds=360), fetcher=lambda: rows(daily=False),
                            sender=rejected, state_dir=path)
            check('RETRIES_EXHAUSTED', 3)
            monitor.run(DAY + timedelta(seconds=700), fetcher=lambda: rows(daily=False),
                        sender=rejected, state_dir=path)
            self.assertEqual(calls, ['FAILED'] * 3)

    def test_missing_mail_settings_are_recorded_as_not_sent(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'), \
                patch.object(monitor, 'settings', side_effect=monitor.MonitorError('missing')):
            path = Path(temporary)
            path.chmod(0o700)
            with self.assertRaises(monitor.MailNotSent):
                monitor.run(DAY, fetcher=lambda: rows(daily=False), state_dir=path)
            state = monitor.json.loads((path / 'state.json').read_text())
            self.assertEqual((state['delivery'], state['attempts']), ('NOT_SENT', 1))

    def test_mail_settings_must_be_an_object(self):
        with patch.object(monitor, 'private_bytes', return_value=b'[]'):
            with self.assertRaises(monitor.MonitorError):
                monitor.settings()

    def test_definitive_failure_then_success_is_sent_once(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            calls = []
            def sender(report, event):
                calls.append(event)
                if len(calls) == 1:
                    raise monitor.MailNotSent('before DATA')
            with self.assertRaises(monitor.MailNotSent):
                monitor.run(DAY, fetcher=lambda: rows(daily=False), sender=sender, state_dir=path)
            monitor.run(DAY + timedelta(seconds=60), fetcher=lambda: rows(daily=False),
                        sender=sender, state_dir=path)
            monitor.run(DAY + timedelta(seconds=120), fetcher=lambda: rows(daily=False),
                        sender=sender, state_dir=path)
            self.assertEqual(calls, ['FAILED', 'FAILED'])
            state = monitor.json.loads((path / 'state.json').read_text())
            self.assertEqual((state['delivery'], state['attempts']), ('SENT', 2))

    def test_sender_classifies_pre_data_and_ambiguous_acceptance(self):
        report = monitor.inspect(rows(daily=False), DAY)
        config = {'host': 'mail.example.test', 'port': 587, 'tls_mode': 'starttls',
                  'username': 'sender', 'password': 'secret',
                  'sender': 'sender@example.test', 'recipient': 'operator@example.test'}
        with patch.object(monitor, 'settings', side_effect=monitor.MonitorError('missing')):
            with self.assertRaises(monitor.MailNotSent):
                monitor.send_mail(report, 'FAILED')
        with patch.object(monitor, 'settings', return_value=config), \
                patch.object(monitor.smtplib, 'SMTP', side_effect=ConnectionRefusedError):
            with self.assertRaises(monitor.MailNotSent):
                monitor.send_mail(report, 'FAILED')
        class FakeSMTP:
            def starttls(self, **kwargs):
                pass
            def login(self, *args):
                pass
            def close(self):
                pass
            def send_message(self, message):
                raise smtplib.SMTPDataError(554, b'rejected')
        with patch.object(monitor, 'settings', return_value=config), \
                patch.object(monitor.smtplib, 'SMTP', return_value=FakeSMTP()):
            with self.assertRaises(monitor.MailNotSent):
                monitor.send_mail(report, 'FAILED')
        class AmbiguousSMTP(FakeSMTP):
            def send_message(self, message):
                raise OSError('connection dropped')
        with patch.object(monitor, 'settings', return_value=config), \
                patch.object(monitor.smtplib, 'SMTP', return_value=AmbiguousSMTP()):
            with self.assertRaises(monitor.MailUncertain):
                monitor.send_mail(report, 'FAILED')

    def test_runtime_baseline_failure_recovery_has_no_success_spam(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(monitor, 'require_root'):
            path = Path(temporary)
            path.chmod(0o700)
            calls = []
            sender = lambda report, event: calls.append(event)
            _, event = monitor.run(DAY, fetcher=lambda: rows(), sender=sender, state_dir=path)
            self.assertEqual(event, 'BASELINE')
            monitor.run(DAY, fetcher=lambda: rows(daily=False), sender=sender, state_dir=path)
            monitor.run(DAY, fetcher=lambda: rows(daily=False), sender=sender, state_dir=path)
            monitor.run(DAY, fetcher=lambda: rows(), sender=sender, state_dir=path)
            monitor.run(DAY, fetcher=lambda: rows(), sender=sender, state_dir=path)
            self.assertEqual(calls, ['FAILED', 'RECOVERED'])


if __name__ == '__main__':
    unittest.main()
