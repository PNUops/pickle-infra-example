"""Offline proof that historical backup evidence never implies current success."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('backup_summary', Path(__file__).resolve().parents[1] / 'backup-health-summary.py')
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)
NOW = 1700001000
MONO = 1000


def config():
    return {'expected_hostname': socket.gethostname(), 'core_state_file': summary.CORE_PATH,
            'database_cache_file': summary.CACHE_PATH, 'database_unit': summary.DB_UNIT}


def core(now=NOW, *, age=60):
    stamp = now - age
    return {'observation_schema_version': 1, 'last_observation': {
        'observed_at_epoch': stamp, 'status': 'HEALTHY', 'query_status': 'OK',
        'inventory_observed_at_epoch': stamp, 'point_evidence': 'OBSERVED',
        'last_valid_points': {ct: {'ctime': stamp - 120, 'age_seconds': 120}
                              for ct in ('1200', '1201', '1202', '1203', '1204')}}}


def cache(now=NOW, *, evidence_age=3):
    point = now - 60
    return {'recovery_point_epoch': point, 'last_readback_epoch': point + 10,
        'age_seconds': now - evidence_age - point,
        'snapshot': 'host/example/' + datetime.fromtimestamp(point, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'probe': summary.PROBE}


def unit(**updates):
    values = {'ActiveState': 'inactive', 'SubState': 'dead', 'Result': 'success', 'ExecMainStatus': '0',
        'ExecMainStartTimestampMonotonic': '995000000', 'ExecMainExitTimestampMonotonic': '998000000',
        'InvocationID': '1' * 32}
    values.update(updates)
    return values


class BackupSummaryTests(unittest.TestCase):
    def test_current_positive_requires_matching_native_invocation_and_no_top_status(self):
        value = summary.build(config(), core(), cache(), unit(), NOW, MONO)
        self.assertEqual(value['core']['current_verdict'], 'HEALTHY')
        self.assertEqual(value['database']['current_verdict'], 'HEALTHY')
        self.assertEqual(value['kind'], 'backup-freshness-summary')
        self.assertEqual(value['schema_version'], 1)
        self.assertIs(value['notifications_enabled'], False)
        self.assertNotIn('status', value)
        self.assertEqual(value['database']['last_healthy_evidence']['evidence_as_of_epoch'], NOW - 3)

    def test_cache_presence_does_not_hide_later_warning_or_error(self):
        for code in ('1', '2', '3'):
            value = summary.database_summary(cache(), unit(ActiveState='failed', SubState='failed',
                Result='exit-code', ExecMainStatus=code), NOW, MONO)
            self.assertEqual(value['current_verdict'], 'UNKNOWN')
            self.assertEqual(value['native_invocation']['exit_status'], int(code))
            self.assertEqual(value['last_healthy_evidence']['evidence_as_of_epoch'], NOW - 3)

    def test_successful_later_invocation_cannot_reuse_old_cache(self):
        for age in (30, 60, 179):
            data = cache()
            data['recovery_point_epoch'] = NOW - age - 60
            data['last_readback_epoch'] = NOW - age - 50
            data['age_seconds'] = 60
            data['snapshot'] = 'host/example/' + datetime.fromtimestamp(data['recovery_point_epoch'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            value = summary.database_summary(data, unit(), NOW, MONO)
            self.assertEqual(value['current_verdict'], 'UNKNOWN')
            self.assertEqual(value['reason'], 'DB_CACHE_NOT_CURRENT_INVOCATION')

    def test_running_or_activating_unit_is_not_current_success(self):
        for active, sub in (('active', 'running'), ('activating', 'start'), ('deactivating', 'stop')):
            value = summary.database_summary(cache(), unit(ActiveState=active, SubState=sub), NOW, MONO)
            self.assertEqual(value['current_verdict'], 'UNKNOWN')

    def test_old_native_or_cache_evidence_is_unknown_with_raw_epochs_preserved(self):
        value = summary.database_summary(cache(), unit(ExecMainStartTimestampMonotonic='700000000',
            ExecMainExitTimestampMonotonic='710000000'), NOW, MONO)
        self.assertEqual(value['reason'], 'DB_EVIDENCE_STALE')
        self.assertEqual(value['last_healthy_evidence']['recovery_point_epoch'], NOW - 60)
        self.assertEqual(value['native_invocation']['finish_monotonic_us'], 710000000)

    def test_core_staleness_uses_observation_time_and_recalculates_point_age(self):
        data = core(age=721)
        value = summary.core_summary(data, NOW)
        self.assertEqual(value['current_verdict'], 'UNKNOWN')
        self.assertEqual(value['reason'], 'CORE_OBSERVATION_STALE')
        self.assertEqual(value['observed_at_epoch'], NOW - 721)
        self.assertEqual(value['last_valid_points']['1203']['current_age_seconds'], 841)

    def test_core_query_failure_keeps_old_inventory_asof_and_cached_point_age(self):
        data = core()
        observation = data['last_observation']
        observation.update(status='FAILED', query_status='UNAVAILABLE', point_evidence='CACHED',
                           inventory_observed_at_epoch=NOW - 600)
        observation['last_valid_points'] = {ct: {'ctime': NOW - 1000, 'age_seconds': 940}
                                          for ct in ('1200', '1201', '1202', '1203', '1204')}
        value = summary.core_summary(data, NOW)
        self.assertEqual(value['current_verdict'], 'UNKNOWN')
        self.assertEqual(value['native_verdict'], 'FAILED')
        self.assertEqual(value['inventory_observed_at_epoch'], NOW - 600)
        self.assertEqual(value['last_valid_points']['1203']['current_age_seconds'], 1000)

    def test_core_missing_member_and_legacy_report_cannot_establish_current_health(self):
        data = core()
        del data['last_observation']['last_valid_points']['1203']
        self.assertEqual(summary.core_summary(data, NOW)['current_verdict'], 'UNKNOWN')
        legacy = {'status': 'HEALTHY', 'last_check_epoch': NOW, 'report': {'status': 'HEALTHY'}}
        self.assertEqual(summary.core_summary(legacy, NOW)['current_verdict'], 'UNKNOWN')
        data = core()
        data['last_observation'].update(status='FAILED')
        data['last_observation']['last_valid_points']['1203'] = {'ctime': None, 'age_seconds': None}
        self.assertEqual(summary.core_summary(data, NOW)['current_verdict'], 'FAILED')

    def test_nonfinite_future_or_malformed_inputs_are_unknown(self):
        for key, value in (('observed_at_epoch', float('nan')), ('observed_at_epoch', NOW + 1),
                           ('inventory_observed_at_epoch', float('inf'))):
            data = core()
            data['last_observation'][key] = value
            self.assertEqual(summary.core_summary(data, NOW)['current_verdict'], 'UNKNOWN')
        for field, value in (('age_seconds', float('nan')), ('last_readback_epoch', NOW + 1),
                             ('probe', 'untrusted-body-value'), ('snapshot', 'wrong')):
            data = cache()
            data[field] = value
            self.assertEqual(summary.database_summary(data, unit(), NOW, MONO)['current_verdict'], 'UNKNOWN')
        for field, value in (('ExecMainExitTimestampMonotonic', '1001000000'), ('InvocationID', ''),
                             ('ExecMainStatus', 'NaN')):
            self.assertEqual(summary.database_summary(cache(), unit(**{field: value}), NOW, MONO)['current_verdict'], 'UNKNOWN')

    def test_fixed_config_rejects_other_files_or_commands(self):
        for field, value in (('core_state_file', '/tmp/other'), ('database_cache_file', '/tmp/cache'),
                             ('database_unit', 'arbitrary.service; id'), ('expected_hostname', 'wrong-host')):
            selected = config()
            selected[field] = value
            with self.assertRaises(summary.HealthError):
                summary.validate(selected)

    def test_native_unit_query_is_read_only_exact_and_bounded(self):
        calls = []
        def command(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(stdout='\n'.join(key + '=' + value for key, value in unit().items()))
        self.assertEqual(summary.unit_observation(runner=command), unit())
        argv, kwargs = calls[0]
        self.assertEqual(argv[:3], ['systemctl', 'show', summary.DB_UNIT])
        self.assertIn('--property=' + ','.join(summary.UNIT_FIELDS), argv)
        self.assertEqual(kwargs['timeout'], 3)
        self.assertNotIn('shell', kwargs)
        with self.assertRaises(summary.HealthError):
            summary.unit_observation('other.service', runner=lambda *args, **kwargs: self.fail())

    def test_runtime_only_writes_own_result_and_preserves_historical_cache_after_missing_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            reads = []
            healthy_cache = [cache()]
            def fetch(path):
                reads.append(str(path))
                if str(path) == summary.CORE_PATH:
                    return core()
                if str(path) == summary.CACHE_PATH:
                    if healthy_cache[0] is None:
                        raise FileNotFoundError()
                    return deepcopy(healthy_cache[0])
                if path == directory / 'result.json':
                    return summary.read_json(path)
                self.fail('unexpected native file')
            with patch.object(summary, 'atomic_json', wraps=summary.atomic_json) as writer:
                first = summary.run(config(), directory, reader=fetch, unit_reader=lambda unit: globals()['unit'](),
                                    clock=lambda: NOW, monotonic=lambda: MONO)
                self.assertEqual(writer.call_args[0][0], directory / 'result.json')
            healthy_cache[0] = None
            second = summary.run(config(), directory, reader=fetch, unit_reader=lambda unit: globals()['unit'](),
                                 clock=lambda: NOW + 60, monotonic=lambda: MONO + 60)
            self.assertEqual(first['database']['current_verdict'], 'HEALTHY')
            self.assertEqual(second['database']['current_verdict'], 'UNKNOWN')
            self.assertEqual(second['database']['last_healthy_evidence']['evidence_as_of_epoch'], NOW - 3)
            self.assertEqual(second['database']['last_healthy_evidence']['provenance'], 'PREVIOUS_SUMMARY_HEALTHY_CACHE')
            self.assertEqual((directory / 'result.json').stat().st_mode & 0o777, 0o600)
            self.assertFalse(any('notification.json' in path for path in reads))

    def test_clock_step_during_runtime_cannot_claim_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            times = iter((NOW + 2, NOW))
            monos = iter((MONO - 1, MONO))
            def fetch(path):
                if str(path) == summary.CORE_PATH:
                    return core()
                if str(path) == summary.CACHE_PATH:
                    return cache()
                raise FileNotFoundError()
            value = summary.run(config(), directory, reader=fetch, unit_reader=lambda unit: globals()['unit'](),
                                clock=lambda: next(times), monotonic=lambda: next(monos))
            self.assertEqual(value['clock_status'], 'UNSAFE')
            self.assertEqual(value['core']['current_verdict'], 'UNKNOWN')
            self.assertEqual(value['database']['current_verdict'], 'UNKNOWN')

    def test_invocation_binding_tolerance_is_bounded(self):
        for asof_delta, expected in ((7, 'HEALTHY'), (8, 'UNKNOWN'), (0, 'HEALTHY')):
            data = cache(evidence_age=asof_delta)
            value = summary.database_summary(data, unit(), NOW, MONO)
            self.assertEqual(value['current_verdict'], expected)

    def test_unit_timeout_or_missing_inputs_never_fabricate_current_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            def missing(path):
                raise FileNotFoundError()
            def timeout(name):
                raise subprocess.TimeoutExpired(['systemctl'], 3)
            value = summary.run(config(), directory, reader=missing, unit_reader=timeout,
                                clock=lambda: NOW, monotonic=lambda: MONO)
            self.assertEqual(value['core']['current_verdict'], 'UNKNOWN')
            self.assertEqual(value['database']['current_verdict'], 'UNKNOWN')
            self.assertIsNone(value['database']['last_healthy_evidence'])

    def test_cli_rejects_nonroot_before_reading_config_or_sources(self):
        with patch.object(summary.os, 'geteuid', return_value=1), \
             patch.object(sys, 'argv', ['summary', '--config', '/protected/config.json']), \
             patch.object(summary, 'read_json', side_effect=AssertionError('config read')), \
             patch.object(sys, 'stdout', io.StringIO()):
            self.assertEqual(summary.main(), 2)


if __name__ == '__main__':
    unittest.main()
