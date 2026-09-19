#!/usr/bin/env python3
"""Offline workflow tests using an in-memory PBS substitute; no real backups or mail."""
import copy
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
SPEC = importlib.util.spec_from_file_location('db_pbs_backup', LIB / 'db_pbs_backup.py')
backup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup
SPEC.loader.exec_module(backup)

EPOCH = 1_800_000_000


def config(root):
    return backup.Config('database-node', 'monitor-node', 'dbfa5efe-d8b4-4d72-ad0f-ed43849983dc',
                         'example_db', '123456789012345', 1, '100.65.1.21',
                         'backup@pbs!writer@pbs.example.test:platform', 'verification', 'application-db',
                         ':'.join(['ab'] * 32), str(root / 'password'), str(root / 'encryption-key'), None,
                         str(root / 'escrow.json'), str(root / 'state'), str(root / 'monitor-state'), None)


def index(c, epoch=EPOCH, proofs=False):
    return {'backup-type': 'host', 'backup-id': c.backup_id + ('-verified' if proofs else ''), 'backup-time': epoch,
            'signature': 'b' * 64, 'unprotected': {'key-fingerprint': ':'.join(['cc'] * 32)},
            'files': [{'filename': backup.PROOF_ARCHIVE + '.didx' if proofs else backup.ARCHIVE_INDEX, 'crypt-mode': 'encrypt',
                       'size': 100, 'csum': 'a' * 64}]}


class Runner:
    def __init__(self, c, fail=None):
        self.c, self.fail = c, fail
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        label = kwargs.get('label')
        if label == self.fail:
            raise backup.BackupError('simulated failure')
        if kwargs.get('output'):
            kwargs['output'].write_bytes(b'PGDMP-example' if 'consistent dump' in label else b'CREATE ROLE example;')
        if label == 'database source address':
            return json.dumps([{'addr_info': [{'local': self.c.source_address}]}]).encode()
        if label == 'database identity and schema':
            return json.dumps({'database': self.c.database, 'system_identifier': self.c.expected_system_identifier,
                               'recovery': False, 'major': 18, 'schema': 1, 'failed_migrations': 0,
                               'database_bytes': 1024}).encode()
        return b''


class Client:
    def __init__(self, c, *, corrupt=False, unavailable=False, prune_fail=False):
        self.c, self.corrupt, self.unavailable, self.prune_fail = c, corrupt, unavailable, prune_fail
        self.manifest = index(c)
        self.payload = None
        self.proof = None
        self.proof_manifest = None
        self.data_present = True
        self.proof_failure = False
        self.pruned = False
        self.index_calls = 0

    def backup(self, payload, epoch):
        self.payload = payload
        self.manifest = index(self.c, epoch)

    def index(self, snapshot):
        self.index_calls += 1
        if self.unavailable:
            raise backup.BackupError('remote access unavailable')
        return copy.deepcopy(self.proof_manifest if '-verified/' in snapshot else self.manifest)

    def backup_receipt(self, payload, epoch):
        if self.proof_failure:
            raise backup.BackupError('proof publication failed')
        self.proof = json.loads((payload / 'receipt.json').read_text())
        self.proof_manifest = index(self.c, epoch, proofs=True)

    def receipt(self, snapshot):
        if self.unavailable:
            raise backup.BackupError('remote access unavailable')
        return copy.deepcopy(self.proof)

    def snapshots(self, proofs=False):
        if self.unavailable:
            raise backup.BackupError('remote access unavailable')
        selected = self.proof_manifest if proofs else self.manifest
        if not proofs and not self.data_present:
            return []
        return [selected['backup-time']] if selected is not None else []

    def restore(self, snapshot, target):
        shutil.copytree(self.payload, target)
        if self.corrupt:
            (target / 'database.dump').write_bytes(b'wrong database bytes')

    def prune(self, verified_epoch):
        if self.prune_fail:
            raise backup.BackupError('prune failure')
        self.pruned = True


class BackupWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.c = config(self.root)
        self.host = patch.object(backup, 'require_local_host')
        self.host.start()
        self.addCleanup(self.host.stop)
        backup.initialize(self.c)
        backup.initialize(self.c, monitor=True)

    def successful_backup(self, client=None):
        client = client or Client(self.c)
        with patch.object(backup.time, 'time', return_value=EPOCH):
            record = backup.run_backup(self.c, Runner(self.c), lambda *_: client)
        return record, client

    def test_checkpoint_requires_pbs_readback_not_just_a_local_dump(self):
        client = Client(self.c, corrupt=True)
        with patch.object(backup.time, 'time', return_value=EPOCH):
            with self.assertRaisesRegex(backup.BackupError, 'checksum'):
                backup.run_backup(self.c, Runner(self.c), lambda *_: client)
        self.assertFalse((Path(self.c.state_dir) / 'checkpoint.json').exists())
        self.assertFalse(client.pruned)
        self.assertEqual(len(list((Path(self.c.state_dir) / 'spool').iterdir())), 1)

    def test_local_dump_failure_does_not_upload_or_advance_checkpoint(self):
        client = Client(self.c)
        with self.assertRaises(backup.BackupError):
            backup.run_backup(self.c, Runner(self.c, 'PostgreSQL consistent dump'), lambda *_: client)
        self.assertIsNone(client.payload)
        self.assertFalse((Path(self.c.state_dir) / 'checkpoint.json').exists())

    def test_success_records_the_conservative_data_point_and_prunes_only_after_readback(self):
        record, client = self.successful_backup()
        self.assertEqual(record['status'], 'VERIFIED')
        self.assertEqual(record['recovery_point_epoch'], EPOCH)
        self.assertEqual(set(record['payload_sha256']), {'database.dump', 'roles.sql', 'manifest.json'})
        self.assertTrue(client.pruned)
        self.assertEqual(list((Path(self.c.state_dir) / 'spool').iterdir()), [])
        self.assertEqual((Path(self.c.state_dir) / 'checkpoint.json').stat().st_mode & 0o777, 0o600)

    def test_readable_local_checkpoint_cannot_hide_a_deleted_or_inaccessible_remote_snapshot(self):
        _, client = self.successful_backup()
        client.unavailable = True
        result = backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH + 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['reason'], 'PBS_SNAPSHOT_UNAVAILABLE')

    def test_a_present_verification_receipt_cannot_hide_deleted_data(self):
        _, client = self.successful_backup()
        client.data_present = False
        result = backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH + 1)
        self.assertEqual(result['reason'], 'PBS_DATA_SNAPSHOT_MISSING')
        self.assertEqual(result['status'], 'FAILED')

    def test_unpublished_proof_never_advances_a_checkpoint(self):
        client = Client(self.c)
        client.proof_failure = True
        with self.assertRaises(backup.BackupError):
            backup.run_backup(self.c, Runner(self.c), lambda *_: client)
        self.assertFalse((Path(self.c.state_dir) / 'checkpoint.json').exists())
        self.assertFalse(client.pruned)

    def test_warning_and_failure_boundaries_are_inclusive(self):
        _, client = self.successful_backup()
        for seconds, expected in ((599, 'HEALTHY'), (600, 'WARNING'), (899, 'WARNING'), (900, 'FAILED')):
            with self.subTest(seconds=seconds):
                result = backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH + seconds)
                self.assertEqual(result['status'], expected)
        self.assertEqual(client.index_calls, 10)

    def test_probe_latency_counts_toward_the_reported_recovery_point_age(self):
        _, client = self.successful_backup()
        elapsed = [EPOCH + 595]
        original_index = client.index
        def slow_index(snapshot):
            elapsed[0] += 10
            return original_index(snapshot)
        client.index = slow_index
        report = backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: elapsed[0])
        self.assertEqual(report['age_seconds'], 615)
        self.assertEqual(report['status'], 'WARNING')

    def test_source_commands_ignore_inherited_libpq_routing_and_use_the_local_socket(self):
        runner = Runner(self.c)
        with patch.dict(os.environ, {'PGHOST': 'remote.invalid', 'PGPORT': '9999',
                                     'PGSERVICE': 'foreign', 'PGOPTIONS': '-c search_path=foreign'}):
            backup.run_backup(self.c, runner, lambda *_: Client(self.c))
        calls = [(args, kwargs) for args, kwargs in runner.calls
                 if kwargs.get('label') in ('database identity and schema', 'PostgreSQL consistent dump',
                                           'database roles without passwords')]
        self.assertEqual(len(calls), 3)
        for args, kwargs in calls:
            self.assertEqual(args[args.index('-h') + 1], backup.PG_SOCKET)
            self.assertEqual(args[args.index('-p') + 1], '5432')
            self.assertEqual(args[args.index('-U') + 1], 'postgres')
            self.assertIn('-w', args)
            self.assertFalse(any(key.startswith('PG') for key in kwargs['env']))

    def test_mutable_server_notes_do_not_change_the_backup_but_changed_signature_does(self):
        _, client = self.successful_backup()
        client.manifest['unprotected']['verify_state'] = {'state': 'ok', 'time': EPOCH + 10}
        self.assertEqual(backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH + 15)['status'], 'HEALTHY')
        client.manifest['signature'] = 'd' * 64
        self.assertEqual(backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH + 15)['reason'], 'REMOTE_MANIFEST_CHANGED')

    def test_failed_server_verification_is_not_reported_healthy(self):
        _, client = self.successful_backup()
        client.manifest['unprotected']['verify_state'] = {'state': 'failed'}
        self.assertEqual(backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH + 1)['status'], 'FAILED')

    def test_retention_failure_does_not_erase_the_verified_restore_point(self):
        record, client = self.successful_backup(Client(self.c, prune_fail=True))
        self.assertEqual(record['status'], 'VERIFIED')
        report = backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH + 1)
        self.assertEqual(report['status'], 'HEALTHY')
        self.assertEqual(json.loads((Path(self.c.state_dir) / 'maintenance.json').read_text())['error'], 'RETENTION_FAILED')

    def test_clock_regression_does_not_turn_an_old_checkpoint_green(self):
        _, client = self.successful_backup()
        self.assertEqual(backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH - 1)['status'], 'FAILED')

    def test_source_local_checkpoint_is_not_required_or_trusted_by_the_independent_monitor(self):
        _, client = self.successful_backup()
        shutil.rmtree(self.c.state_dir)
        runner = Runner(self.c)
        report = backup.freshness(self.c, runner, lambda *_: client, clock=lambda: EPOCH + 1)
        self.assertEqual(report['status'], 'HEALTHY')
        self.assertEqual(runner.calls, [])
        client.proof['snapshot'] = 'host/another-group/2026-01-01T00:00:00Z'
        report = backup.freshness(self.c, Runner(self.c), lambda *_: client, clock=lambda: EPOCH + 1)
        self.assertEqual(report['status'], 'FAILED')

    def test_monitor_unit_does_not_depend_on_the_database_or_source_backup_enable_marker(self):
        units = backup.unit_files()
        monitor = units['pickle-db-pbs-monitor.service']
        self.assertNotIn('postgresql', monitor)
        self.assertNotIn('enable-backup', monitor)
        self.assertIn('--notify-status', monitor)
        self.assertIn('/bin/bash ', monitor)
        self.assertIn('*:0/5:00 UTC', units['pickle-db-pbs-backup.timer'])
        self.assertIn('*:*:00 UTC', units['pickle-db-pbs-monitor.timer'])

    def test_manifest_requires_encryption_identity_and_exact_archive(self):
        cases = []
        for key, value in (('backup-id', 'other'), ('signature', None), ('files', ['bad'])):
            changed = index(self.c)
            changed[key] = value
            cases.append(changed)
        changed = index(self.c)
        changed['files'][0]['crypt-mode'] = 'none'
        cases.append(changed)
        for changed in cases:
            with self.assertRaises(backup.BackupError):
                backup.manifest_fingerprint(self.c, changed, EPOCH)

    def test_configuration_refuses_keys_inside_payload_state(self):
        bad = replace(self.c, encryption_key_file=str(Path(self.c.state_dir) / 'key.json'))
        with self.assertRaises(backup.BackupError):
            bad.validate()

    def test_retention_preview_cannot_remove_the_current_point_or_another_group(self):
        client = object.__new__(backup.Client)
        client.c = self.c
        good = {'backup-type': 'host', 'backup-id': self.c.backup_id + '-verified', 'backup-time': EPOCH, 'keep': True}
        for bad in [dict(good, keep=False), dict(good, **{'backup-id': 'another-database'})]:
            calls = []
            def call(words, **kwargs):
                calls.append(words)
                return json.dumps([bad]).encode()
            client.call = call
            with self.assertRaises(backup.BackupError):
                client.prune(EPOCH)
            self.assertEqual(len(calls), 1)
            self.assertIn('--dry-run', calls[0])

    def test_key_custody_is_outside_payload_and_inherited_password_commands_are_ignored(self):
        key = self.root / 'encryption-key'
        key.write_bytes(b'unit-test-key-material')
        key.chmod(0o600)
        password = self.root / 'password'
        password.write_text('unit-test-api-token')
        password.chmod(0o600)
        backup.atomic_json(self.root / 'escrow.json', {
            'key_file_sha256': backup.digest(key), 'outside_datastore': True,
            'custody_reference': 'separate test recovery store',
            'recovery_copy_verified_at': '2026-01-01T00:00:00+00:00'})
        with patch.dict(os.environ, {'PBS_PASSWORD_CMD': 'unexpected-command', 'PBS_REPOSITORY': 'wrong-repository'}):
            env = backup.client_environment(self.c)
        self.assertNotIn('PBS_PASSWORD_CMD', env)
        self.assertNotIn('PBS_REPOSITORY', env)
        self.assertEqual(env['PBS_PASSWORD_FILE'], str(password))
        self.assertNotIn('unit-test-api-token', env.values())

    def test_spool_cleanup_validates_every_owner_before_removing_any_copy(self):
        spool = Path(self.c.state_dir) / 'spool'
        paths = []
        for number in (1, 2):
            run_id = str(uuid.uuid4())
            directory = spool / run_id
            directory.mkdir()
            backup.atomic_json(directory / 'attempt.json', {'instance_id': self.c.instance_id, 'run_id': run_id,
                               'status': 'FAILED_OR_UNCONFIRMED', 'recovery_point_epoch': number})
            paths.append(directory)
        foreign = spool / 'not-owned'
        foreign.mkdir()
        with self.assertRaises(backup.BackupError):
            backup.clean_owned_failed_spool(self.c, Path(self.c.state_dir))
        self.assertTrue(all(path.exists() for path in paths))
        foreign.rmdir()
        backup.clean_owned_failed_spool(self.c, Path(self.c.state_dir))
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[1].exists())

    def test_notification_changes_are_deduplicated_and_an_uncertain_test_is_not_resent(self):
        mail = self.root / 'mail.json'
        backup.atomic_json(mail, {'enabled': True})
        c = replace(self.c, mail_config_file=str(mail))
        sent = []
        def sender(_config, report, **kwargs):
            sent.append(report['status'])
            return True
        report = {'status': 'WARNING', 'reason': 'RECOVERY_POINT_AGING'}
        backup.notify(c, report, sender=sender)
        backup.notify(c, report, sender=sender)
        backup.notify(c, {'status': 'FAILED', 'reason': 'RECOVERY_POINT_TOO_OLD'}, sender=sender)
        self.assertEqual(sent, ['WARNING', 'FAILED'])
        def uncertain(*args, **kwargs):
            sent.append('TEST')
            raise OSError('network result uncertain')
        with self.assertRaises(backup.BackupError):
            backup.notify(c, {'status': 'TEST', 'reason': 'test'}, test=True, sender=uncertain)
        with self.assertRaises(backup.BackupError):
            backup.notify(c, {'status': 'TEST', 'reason': 'test'}, test=True, sender=uncertain)
        self.assertEqual(sent.count('TEST'), 1)

    def test_initial_healthy_notification_records_a_baseline_without_sending(self):
        mail = self.root / 'mail.json'
        backup.atomic_json(mail, {'enabled': True})
        c = replace(self.c, mail_config_file=str(mail))
        sent = []
        def sender(_config, report, **kwargs):
            sent.append(report['status'])
            return True
        healthy = {'status': 'HEALTHY', 'reason': 'VERIFIED_AND_REMOTE_AVAILABLE'}
        self.assertEqual(backup.notify(c, healthy, sender=sender)['delivery'], 'BASELINE')
        self.assertEqual(backup.notify(c, healthy, sender=sender)['delivery'], 'BASELINE')
        self.assertEqual(backup.notify(c, {'status': 'HEALTHY', 'reason': 'REMOTE_PROBE_COMPLETE'}, sender=sender)['delivery'], 'BASELINE')
        self.assertEqual(sent, [])
        self.assertEqual(backup.notify(c, {'status': 'WARNING', 'reason': 'RECOVERY_POINT_AGING'}, sender=sender)['delivery'], 'SENT')
        self.assertEqual(backup.notify(c, healthy, sender=sender)['delivery'], 'SENT')
        self.assertEqual(sent, ['WARNING', 'HEALTHY'])

    def test_default_plan_never_reads_credentials_or_runs_a_backup(self):
        path = self.root / 'config.json'
        from dataclasses import asdict
        path.write_text(json.dumps(asdict(self.c)))
        stream = io.StringIO()
        with patch.object(sys, 'argv', ['backup', '--config', str(path)]), \
                patch.object(backup, 'run_backup', side_effect=AssertionError('unexpected backup')), \
                patch.object(backup, 'client_environment', side_effect=AssertionError('unexpected credentials')), \
                patch.object(sys, 'stdout', stream):
            self.assertEqual(backup.main(), 0)
        self.assertEqual(json.loads(stream.getvalue())['mode'], 'dry-run')

    def test_operational_notification_retries_only_known_non_delivery_with_a_bound(self):
        mail = self.root / 'mail.json'
        backup.atomic_json(mail, {'enabled': True})
        c = replace(self.c, mail_config_file=str(mail))
        attempts = []
        def not_sent(*args, **kwargs):
            attempts.append(1)
            raise backup.MailNotSent('connection rejected before DATA')
        report = {'status': 'FAILED', 'reason': 'PBS_SNAPSHOT_UNAVAILABLE'}
        for moment, expected_attempts in ((EPOCH, 1), (EPOCH + 59, 1), (EPOCH + 60, 2),
                                          (EPOCH + 359, 2), (EPOCH + 360, 3), (EPOCH + 999, 3)):
            with patch.object(backup.time, 'time', return_value=moment):
                with self.assertRaises(backup.BackupError):
                    backup.notify(c, report, sender=not_sent)
            self.assertEqual(len(attempts), expected_attempts)
        receipt = json.loads((Path(c.monitor_state_dir) / 'notification.json').read_text())
        self.assertEqual(receipt['delivery'], 'RETRIES_EXHAUSTED')

    def test_notification_retry_can_recover_without_repeating_a_successful_delivery(self):
        mail = self.root / 'mail.json'
        backup.atomic_json(mail, {'enabled': True})
        c = replace(self.c, mail_config_file=str(mail))
        calls = []
        def sender(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise backup.MailNotSent('authentication failed before DATA')
            return True
        report = {'status': 'FAILED', 'reason': 'PBS_SNAPSHOT_UNAVAILABLE'}
        with patch.object(backup.time, 'time', return_value=EPOCH):
            with self.assertRaises(backup.BackupError):
                backup.notify(c, report, sender=sender)
        with patch.object(backup.time, 'time', return_value=EPOCH + 60):
            self.assertEqual(backup.notify(c, report, sender=sender)['delivery'], 'SENT')
            backup.notify(c, report, sender=sender)
        self.assertEqual(len(calls), 2)

    def test_smtp_connection_failure_is_classified_before_message_submission(self):
        mail = self.root / 'mail.json'
        password = self.root / 'smtp-password'
        password.write_text('test-placeholder')
        password.chmod(0o600)
        backup.atomic_json(mail, {'enabled': True, 'host': 'smtp.example.test', 'port': 587,
                                 'username': 'notifier', 'sender': 'sender@example.test',
                                 'recipient': 'recipient@example.test', 'tls_mode': 'starttls',
                                 'password_file': str(password)})
        c = replace(self.c, mail_config_file=str(mail))
        with patch.object(backup.smtplib, 'SMTP', side_effect=ConnectionRefusedError):
            with self.assertRaises(backup.MailNotSent):
                backup.send_mail(c, {'status': 'FAILED', 'reason': 'PBS_SNAPSHOT_UNAVAILABLE'})


if __name__ == '__main__':
    unittest.main()
