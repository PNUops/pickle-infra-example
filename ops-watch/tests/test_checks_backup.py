#!/usr/bin/env python3
"""Backup, PBS capacity, GPU checks and the pve-node-3/gpu-node catalog items."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opswatch import catalog as catalog_mod  # noqa: E402
from opswatch.agent import Context, run_check  # noqa: E402
from opswatch.checks import REGISTRY, CheckError, reasons  # noqa: E402
from opswatch.checks import core_backup, db_backup, gpu, pbs_capacity  # noqa: E402
from opswatch.result import clean  # noqa: E402

KST = timezone(timedelta(hours=9))
FINGERPRINT = ':'.join(['aa'] * 32)  # synthetic; real fingerprints stay in the catalog
STORAGE = 'pbs-example-core-read'
MANUAL = {
    '200': f'{STORAGE}:backup/ct/200/2026-09-27T12:43:02Z',
    '201': f'{STORAGE}:backup/ct/201/2026-09-27T11:55:44Z',
    '202': f'{STORAGE}:backup/ct/202/2026-09-27T12:46:30Z',
    '204': f'{STORAGE}:backup/ct/204/2026-09-27T12:50:42Z',
}
CORE_ARGS = {'node': 'pve-node-3', 'storage': STORAGE, 'ctids': [200, 201, 202, 203, 204],
             'fingerprint': FINGERPRINT, 'protected': MANUAL, 'window': '03:17', 'deadline': '04:30'}


def kst(*parts):
    return datetime(*parts, tzinfo=KST).timestamp()


def archive(ctid, epoch, **extra):
    stamp = datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    row = {'content': 'backup', 'encrypted': FINGERPRINT, 'format': 'pbs-ct', 'subtype': 'lxc',
           'vmid': ctid, 'size': 1024, 'protected': 0, 'ctime': int(epoch),
           'volid': f'{STORAGE}:backup/ct/{ctid}/{stamp}'}
    row.update(extra)
    return row


def listing(day, *, skip=(), at=(3, 18)):
    """Storage content with the protected baselines plus one daily archive per CT on a KST day."""
    rows = []
    for ctid, volid in MANUAL.items():
        epoch = datetime.strptime(volid.rsplit('/', 1)[1], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        rows.append(archive(int(ctid), epoch.timestamp(), protected=1))
    if day is not None:
        for ctid in CORE_ARGS['ctids']:
            if ctid not in skip:
                rows.append(archive(ctid, kst(*day, *at)))
    return rows


class FakeRunner:
    """Stands in for subprocess.run behind Context.run."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, int):
            return subprocess.CompletedProcess(argv, value, '', 'boom')
        return subprocess.CompletedProcess(argv, 0, value, '')


def context(now, *outputs):
    runner = FakeRunner(outputs)
    return Context(now, {}, runner), runner


class CoreBackupTests(unittest.TestCase):
    def check(self, now, content):
        ctx, runner = context(now, json.dumps(content))
        result = core_backup.run(CORE_ARGS, ctx)
        self.assertEqual(runner.calls[0][:3], ['pvesh', 'get', f'/nodes/pve-node-3/storage/{STORAGE}/content'])
        clean(result, 'core_backup', item_id='core-backup', sample_at=now)
        return result

    def test_all_archives_present_after_deadline_is_healthy(self):
        result = self.check(kst(2026, 11, 12, 4, 31), listing((2026, 11, 12)))
        self.assertEqual((result['status'], result['reason']), ('OK', 'HEALTHY'))
        self.assertNotIn('pending', result['detail'])

    def test_missing_archive_before_deadline_is_pending_not_failure(self):
        rows = listing((2026, 11, 11)) + listing((2026, 11, 12), skip=(203,))
        result = self.check(kst(2026, 11, 12, 4, 29), rows)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['detail']['pending'], ['CT203'])

    def test_two_missed_days_fail_before_the_deadline(self):
        # Yesterday's run is missing too, so midnight must not read as a recovery.
        rows = listing((2026, 11, 10)) + listing((2026, 11, 12), skip=(203,))
        for hour, minute in ((0, 1), (4, 29)):
            result = self.check(kst(2026, 11, 12, hour, minute), rows)
            self.assertEqual((result['status'], result['reason']), ('FAIL', 'BACKUP_MISSING'))
            self.assertEqual(result['detail']['missing'], ['CT203'])

    def test_missing_archive_after_deadline_fails_with_ct_list(self):
        rows = listing((2026, 11, 11)) + listing((2026, 11, 12), skip=(202, 203))[4:]
        result = self.check(kst(2026, 11, 12, 4, 30), rows)
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'BACKUP_MISSING'))
        self.assertEqual(result['detail']['missing'], ['CT202', 'CT203'])
        self.assertEqual(result['detail']['missing_count'], 2)
        self.assertEqual(result['detail']['last_backup_kst'], '11-11 03:18')

    def test_ct_without_any_archive_is_missing(self):
        result = self.check(kst(2026, 11, 12, 5, 0), listing((2026, 11, 12), skip=(203,)))
        self.assertEqual(result['detail']['missing'], ['CT203'])

    def test_kst_date_boundary_uses_korean_day_not_utc_day(self):
        yesterday = listing((2026, 11, 11))
        # 23:59 KST on the 11th: the 11th's archives are today's.
        self.assertEqual(self.check(kst(2026, 11, 11, 23, 59), yesterday)['detail'].get('pending'), None)
        # 00:01 KST on the 12th (still the 11th in UTC): a new day, archives pending.
        self.assertEqual(self.check(kst(2026, 11, 12, 0, 1), yesterday)['detail']['pending'],
                         ['CT200', 'CT201', 'CT202', 'CT203', 'CT204'])
        # 04:31 KST on the 12th is 19:31 UTC on the 11th; the deadline has passed in KST.
        result = self.check(kst(2026, 11, 12, 4, 31), yesterday)
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'BACKUP_MISSING'))

    def test_archive_before_window_does_not_count_for_today(self):
        rows = listing((2026, 11, 12), at=(3, 10))
        result = self.check(kst(2026, 11, 12, 4, 31), rows)
        self.assertEqual(result['reason'], 'BACKUP_MISSING')

    def test_invalid_newest_metadata_fails_after_deadline(self):
        rows = listing((2026, 11, 12))
        newest = next(row for row in rows if row['vmid'] == 203)
        newest['encrypted'] = None
        before = self.check(kst(2026, 11, 12, 4, 0), rows)
        self.assertEqual(before['detail']['pending'], ['CT203'])
        result = self.check(kst(2026, 11, 12, 4, 31), rows)
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'BACKUP_INVALID_METADATA'))
        self.assertEqual(result['detail']['invalid'], ['CT203'])

    def test_ctime_not_matching_volid_is_invalid(self):
        rows = listing((2026, 11, 12))
        bad_row = dict(next(row for row in rows if row['vmid'] == 201 and not row['protected']))
        bad_row['ctime'] += 1
        rows.append(bad_row)
        result = self.check(kst(2026, 11, 12, 4, 31), rows)
        self.assertEqual(result['detail']['invalid'], ['CT201'])

    def test_missing_protected_baseline_fails_even_before_deadline(self):
        rows = [row for row in listing((2026, 11, 12)) if row['volid'] != MANUAL['202']]
        result = self.check(kst(2026, 11, 12, 1, 0), rows)
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'PROTECTED_BASELINE_MISSING'))
        self.assertEqual(result['detail']['missing'], ['CT202'])
        unprotected = listing((2026, 11, 12))
        next(row for row in unprotected if row['volid'] == MANUAL['200'])['protected'] = 0
        self.assertEqual(self.check(kst(2026, 11, 12, 5, 0), unprotected)['detail']['missing'], ['CT200'])

    def test_query_failure_is_a_backup_failure(self):
        for output in (2, subprocess.TimeoutExpired('pvesh', 40), 'not json', '{}'):
            ctx, _ = context(kst(2026, 11, 12, 10, 0), output)
            result = core_backup.run(CORE_ARGS, ctx)
            self.assertEqual((result['status'], result['reason']), ('FAIL', 'PBS_UNAVAILABLE'))

    def test_validate_rejects_bad_args(self):
        core_backup.validate(CORE_ARGS)
        for change in ({'ctids': []}, {'deadline': '03:00'}, {'window': '3:17'}, {'fingerprint': 'x'},
                       {'protected': {'205': MANUAL['200']}}, {'protected': {'200': 'other:backup/ct/200/x'}},
                       {'node': 'PVE3'}, {'timeout': 90}):
            with self.assertRaises(ValueError):
                core_backup.validate(dict(CORE_ARGS, **change))


def private_file(directory, name, content):
    path = Path(directory) / name
    path.write_text(content)
    os.chmod(path, 0o600)
    return str(path)


class PbsCapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        token = private_file(self.tmp.name, 'token', 'a' * 32 + '\n')
        self.config = private_file(self.tmp.name, 'config.json', json.dumps({
            'server': 'pbs-vm.tailnet.example', 'port': 8007, 'datastore': 'pickle',
            'auth_id': 'capacity@pbs!probe', 'token_file': token, 'tls_fingerprint': FINGERPRINT,
            'warning_avail_bytes': 100, 'critical_avail_bytes': 50}))
        self.args = {'config': self.config, 'warn_avail_gb': 100, 'fail_avail_gb': 50}

    def run_with(self, response):
        with mock.patch.object(pbs_capacity, 'fetch', side_effect=response if isinstance(response, type)
                               and issubclass(response, Exception) else None,
                               return_value=response) as fetch:
            result = pbs_capacity.run(self.args, Context(0, {}))
        if fetch.called:
            self.assertEqual(fetch.call_args[0][0]['server'], 'pbs-vm.tailnet.example')
        clean(result, 'pbs_capacity', item_id='pbs-capacity', sample_at=1)
        return result

    @staticmethod
    def status(avail_gb, total_gb=1000):
        avail = int(avail_gb * 10**9)
        total = total_gb * 10**9
        return 200, json.dumps({'data': {'total': total, 'used': total - avail, 'avail': avail}}).encode()

    def test_thresholds(self):
        self.assertEqual(self.run_with(self.status(400))['status'], 'OK')
        self.assertEqual(self.run_with(self.status(400))['detail'], {'used_pct': 60, 'avail_gb': 400})
        warn = self.run_with(self.status(99.5))
        self.assertEqual((warn['status'], warn['reason'], warn['detail']['avail_gb']), ('WARN', 'PBS_SPACE_LOW', 99))
        self.assertEqual(self.run_with(self.status(100))['status'], 'OK')
        self.assertEqual(self.run_with(self.status(49))['status'], 'FAIL')

    def test_server_address_never_reaches_the_result(self):
        result = self.run_with(self.status(400))
        self.assertNotIn('tailnet', json.dumps(result))

    def test_pbs_errors_are_failures(self):
        for response in ((500, b'{}'), (200, b'not json'), (200, b'{"data": {"total": 0, "used": 0, "avail": 0}}'),
                         (200, b'{"data": {"total": 10, "used": 5, "avail": 5, "store": "other"}}'),
                         pbs_capacity.Unavailable):
            result = self.run_with(response)
            self.assertEqual((result['status'], result['reason']), ('FAIL', 'PBS_UNAVAILABLE'), response)
        self.assertEqual(self.run_with(pbs_capacity.IdentityMismatch)['reason'], 'PBS_IDENTITY_MISMATCH')

    def test_rejected_token_and_bad_config_are_checker_errors(self):
        with self.assertRaises(CheckError) as caught:
            self.run_with((401, b''))
        self.assertEqual(caught.exception.reason, 'PBS_AUTH_REJECTED')
        os.chmod(self.config, 0o644)
        with self.assertRaises(CheckError) as caught:
            self.run_with(self.status(400))
        self.assertEqual(caught.exception.reason, 'BACKUP_CONFIG_INVALID')

    def test_validate(self):
        pbs_capacity.validate(self.args)
        for change in ({'warn_avail_gb': 40}, {'config': 'relative.json'}, {'fail_avail_gb': 1.5}):
            with self.assertRaises(ValueError):
                pbs_capacity.validate(dict(self.args, **change))


class DbBackupTests(unittest.TestCase):
    POINT = int(kst(2026, 11, 12, 10, 0))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = {'config': private_file(self.tmp.name, 'production.json', json.dumps({
            'repository': 'backup@pbs!db@pbs-vm.tailnet.example:pickle', 'namespace': 'production/db',
            'backup_id': 'pickle-db', 'server_fingerprint': FINGERPRINT,
            'password_file': '/etc/pickle-db-backup/token', 'instance_id': 'ignored'})),
            'warn_age': 600, 'fail_age': 900}

    @staticmethod
    def group(backup_id, epochs, archive):
        return json.dumps([{'backup-type': 'host', 'backup-id': backup_id, 'backup-time': epoch,
                            'files': [{'filename': archive + '.didx', 'crypt-mode': 'encrypt'}, 'index.json.blob']}
                           for epoch in epochs])

    def check(self, age, proofs=None, data=None):
        proofs = [self.POINT] if proofs is None else proofs
        data = proofs if data is None else data
        ctx, runner = context(self.POINT + age, self.group('pickle-db-verified', proofs, db_backup.PROOF_ARCHIVE),
                              self.group('pickle-db', data, db_backup.ARCHIVE))
        result = db_backup.run(self.args, ctx)
        clean(result, 'db_backup', item_id='db-backup', sample_at=1)
        return result, runner

    def test_fresh_point_is_healthy_and_uses_the_monitor_invocation(self):
        result, runner = self.check(120)
        self.assertEqual(result, {'status': 'OK', 'reason': 'HEALTHY',
                                  'detail': {'age_min': 2, 'recovery_point_kst': '10:00'}})
        argv = runner.calls[0]
        self.assertEqual(argv[:3], ['env', 'PBS_PASSWORD_FILE=/etc/pickle-db-backup/token',
                                    'PBS_FINGERPRINT=' + FINGERPRINT])
        self.assertEqual(argv[3:7], ['proxmox-backup-client', 'snapshot', 'list', 'host/pickle-db-verified'])
        self.assertEqual(argv[-4:], ['--repository', 'backup@pbs!db@pbs-vm.tailnet.example:pickle',
                                     '--ns', 'production/db'])
        self.assertEqual(runner.calls[1][6], 'host/pickle-db')

    def test_age_thresholds(self):
        self.assertEqual(self.check(599)[0]['status'], 'OK')
        aging = self.check(600)[0]
        self.assertEqual((aging['status'], aging['reason'], aging['detail']['age_min']),
                         ('WARN', 'RECOVERY_POINT_AGING', 10))
        old = self.check(900)[0]
        self.assertEqual((old['status'], old['reason']), ('FAIL', 'RECOVERY_POINT_TOO_OLD'))

    def test_newest_proof_wins_and_needs_its_data_snapshot(self):
        older = self.POINT - 300
        result = self.check(60, proofs=[older, self.POINT], data=[older])[0]
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'PBS_DATA_SNAPSHOT_MISSING'))
        self.assertEqual(self.check(60, proofs=[older, self.POINT])[0]['detail']['age_min'], 1)

    def test_no_proof_and_future_point(self):
        self.assertEqual(self.check(60, proofs=[], data=[self.POINT])[0]['reason'], 'NO_VERIFIED_CHECKPOINT')
        self.assertEqual(self.check(-120)[0]['reason'], 'CLOCK_AHEAD')

    def test_snapshot_without_archive_is_not_a_point(self):
        ctx, _ = context(self.POINT + 60, self.group('pickle-db-verified', [self.POINT], 'other.pxar'),
                         self.group('pickle-db', [self.POINT], db_backup.ARCHIVE))
        self.assertEqual(db_backup.run(self.args, ctx)['reason'], 'NO_VERIFIED_CHECKPOINT')

    def test_client_failure_or_foreign_rows_are_pbs_failures(self):
        for outputs in ((1,), ('[]', subprocess.TimeoutExpired('x', 20)), ('not json',),
                        (self.group('other-group', [self.POINT], db_backup.PROOF_ARCHIVE),)):
            ctx, _ = context(self.POINT, *outputs)
            result = db_backup.run(self.args, ctx)
            self.assertEqual((result['status'], result['reason']), ('FAIL', 'PBS_UNAVAILABLE'), outputs)

    def test_unreadable_profile_is_a_checker_error(self):
        os.chmod(self.args['config'], 0o640)
        item = {'id': 'db-backup', 'check': 'db_backup', 'args': self.args}
        result = run_check(item, Context(self.POINT, {}, FakeRunner([])), timeout=5)
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'BACKUP_CONFIG_INVALID'))


class GpuTests(unittest.TestCase):
    def check(self, output, args=None):
        ctx, runner = context(1, output)
        result = gpu.run(args or {}, ctx)
        self.assertEqual(runner.calls[0], gpu.QUERY)
        clean(result, 'gpu', item_id='gpu-node-gpu', sample_at=1)
        return result

    def test_reading(self):
        self.assertEqual(self.check('47, 12\n'), {'status': 'OK', 'reason': 'HEALTHY',
                                                  'detail': {'temperature_c': 47, 'utilization_pct': 12, 'gpus': 1}})
        self.assertNotIn('utilization_pct', self.check('47, [N/A]')['detail'])

    def test_too_hot(self):
        self.assertEqual(self.check('89, 99')['status'], 'OK')
        result = self.check('90, 99')
        self.assertEqual((result['status'], result['reason'], result['detail']['temperature_c']),
                         ('FAIL', 'GPU_TOO_HOT', 90))
        self.assertEqual(self.check('70, 1', {'max_temp_c': 70})['status'], 'FAIL')

    def test_lost_device(self):
        self.assertEqual(self.check(9)['reason'], 'GPU_UNAVAILABLE')
        self.assertEqual(self.check('')['reason'], 'GPU_UNAVAILABLE')
        with self.assertRaises(CheckError):
            self.check('[N/A], 3')


class ReasonTests(unittest.TestCase):
    def test_reason_texts_are_consistent_and_formattable(self):
        table = reasons()
        samples = {'missing_count': 2, 'avail_gb': 42, 'age_min': 12, 'temperature_c': 91}
        for module in (core_backup, pbs_capacity, db_backup, gpu):
            for code, (text, category) in module.REASONS.items():
                self.assertEqual(table[code], (text, category))
                text.format(**samples)
                self.assertTrue(text.endswith('요.'), code)
                self.assertLessEqual(len(category), 12)
        self.assertEqual(core_backup.REASONS['PBS_UNAVAILABLE'], db_backup.REASONS['PBS_UNAVAILABLE'])


class CatalogTests(unittest.TestCase):
    def test_own_items_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'items').mkdir()
            shutil.copy(ROOT / 'catalog' / 'hosts.json', root / 'hosts.json')
            loaded = 0
            for name in ('pve-node-3.json', 'gpu-node.json'):
                items = json.loads((ROOT / 'catalog' / 'items' / name).read_text(encoding='utf-8'))
                kept = [item for item in items if item['check'] in REGISTRY]
                loaded += len(kept)
                (root / 'items' / name).write_text(json.dumps(kept, ensure_ascii=False), encoding='utf-8')
            catalog = catalog_mod.load(root)
        self.assertEqual(len(catalog['items']), loaded)
        for kind in ('core_backup', 'pbs_capacity', 'db_backup', 'gpu', 'http'):
            self.assertIn(kind, {item['check'] for item in catalog['items'].values()})
        for item in catalog['items'].values():
            self.assertNotRegex(item['page_label'], r'CT\d|pve\d|dgx|\d+\.\d+\.\d+')
            if item['host'] == 'gpu-node':
                self.assertRegex(item['target'], r'^gpu-node 모델 서버 .+ \(운영 미사용\)$')
                self.assertEqual((item['group'], item['user_facing']), ('nonprod', False))
            if item['check'] in ('core_backup', 'pbs_capacity', 'db_backup'):
                self.assertEqual(item['group'], 'backup')

    def test_referenced_kinds_are_known(self):
        kinds = set()
        for name in ('pve-node-3.json', 'gpu-node.json'):
            kinds |= {item['check'] for item in json.loads((ROOT / 'catalog' / 'items' / name).read_text())}
        self.assertLessEqual(kinds, {'core_backup', 'pbs_capacity', 'db_backup', 'gpu', 'http',
                                     'host', 'systemd_unit', 'llm_generation'})


if __name__ == '__main__':
    unittest.main(verbosity=1)
