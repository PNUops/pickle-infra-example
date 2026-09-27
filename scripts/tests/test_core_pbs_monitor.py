"""Offline checks for the independent pve-node-3 core PBS archive monitor."""
import importlib.util
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
    for ctid in monitor.CTIDS:
        base = {'content': 'backup', 'encrypted': monitor.FINGERPRINT,
                'format': 'pbs-ct', 'subtype': 'lxc', 'vmid': ctid, 'size': 1024}
        manual_stamp = monitor.MANUAL[ctid].rsplit('/', 1)[1]
        manual_epoch = int(datetime.strptime(manual_stamp, '%Y-%m-%dT%H:%M:%SZ')
                           .replace(tzinfo=timezone.utc).timestamp())
        result.append(dict(base, protected=1, ctime=manual_epoch,
                           volid=monitor.MANUAL[ctid]))
        if daily:
            stamp = datetime.combine(now.date(), datetime.min.time(), KST) + timedelta(hours=3, minutes=18)
            result.append(dict(base, protected=0, ctime=int(stamp.timestamp()),
                               volid=f'pbs-example-core-read:backup/ct/{ctid}/{stamp.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")}'))
    return result


class CorePbsMonitorTest(unittest.TestCase):
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
