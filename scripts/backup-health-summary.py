#!/usr/bin/env python3
"""Publish local backup evidence without initiating backups, queries or alerts."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from llm_health import HealthError, atomic_json, locked, read_json

KIND = 'backup-freshness-summary'
CORE_PATH = '/var/lib/pickle-core-pbs-monitor/state.json'
CACHE_PATH = '/var/lib/pickle-db-monitor-production/last-remote-check.json'
DB_UNIT = 'pickle-db-pbs-production-monitor.service'
STATE_DIR = Path('/var/lib/pickle-backup-health-summary')
CTIDS = ('1200', '1201', '1202', '1203', '1204')
CORE_FRESHNESS = 720
DB_FRESHNESS = 180
TOLERANCE = 2
PROBE = 'current PBS groups, encrypted verification receipt and data manifest'
UNIT_FIELDS = ('ActiveState', 'SubState', 'Result', 'ExecMainStatus',
               'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic', 'InvocationID')


def numeric(value, *, maximum=4102444800):
    if (type(value) not in (int, float) or not 0 <= value <= maximum
            or not math.isfinite(value)):
        raise HealthError('TIMESTAMP_INVALID')
    return float(value)


def validate(config):
    if (not isinstance(config, dict) or set(config) != {
            'expected_hostname', 'core_state_file', 'database_cache_file', 'database_unit'}
            or not isinstance(config['expected_hostname'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,252}', config['expected_hostname'])
            or config['expected_hostname'] != socket.gethostname()
            or config['core_state_file'] != CORE_PATH or config['database_cache_file'] != CACHE_PATH
            or config['database_unit'] != DB_UNIT):
        raise HealthError('SUMMARY_CONFIG_INVALID')


def unit_observation(unit=DB_UNIT, *, runner=subprocess.run):
    if unit != DB_UNIT:
        raise HealthError('SUMMARY_UNIT_INVALID')
    result = runner(['systemctl', 'show', unit, '--no-pager', '--property=' + ','.join(UNIT_FIELDS)],
        capture_output=True, text=True, check=True, timeout=3,
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    if len(result.stdout.encode()) > 16384:
        raise HealthError('UNIT_RESPONSE_TOO_LARGE')
    lines = result.stdout.splitlines()
    if len(lines) != len(UNIT_FIELDS) or any('=' not in line for line in lines):
        raise HealthError('UNIT_RESPONSE_INVALID')
    values = dict(line.split('=', 1) for line in lines)
    if set(values) != set(UNIT_FIELDS):
        raise HealthError('UNIT_FIELDS_INVALID')
    return values


def core_summary(state, now, *, clock_safe=True):
    output = {'current_verdict': 'UNKNOWN', 'reason': 'CORE_OBSERVATION_UNAVAILABLE',
              'observed_at_epoch': None, 'inventory_observed_at_epoch': None,
              'observation_age_seconds': None, 'last_valid_points': {}}
    try:
        if (not isinstance(state, dict) or type(state.get('observation_schema_version')) is not int
                or state['observation_schema_version'] != 1):
            raise HealthError('CORE_SCHEMA_UNSUPPORTED')
        observation = state['last_observation']
        if not isinstance(observation, dict):
            raise HealthError('CORE_OBSERVATION_INVALID')
        stamp = numeric(observation['observed_at_epoch'])
        inventory = observation.get('inventory_observed_at_epoch')
        inventory = None if inventory is None else numeric(inventory)
        if stamp > now or inventory is not None and inventory > stamp:
            raise HealthError('CORE_FUTURE_EVIDENCE')
        status, query, evidence = (observation['status'], observation['query_status'],
                                   observation['point_evidence'])
        if (status not in ('HEALTHY', 'FAILED', 'PENDING') or query not in ('OK', 'UNAVAILABLE')
                or evidence not in ('OBSERVED', 'CACHED', 'UNAVAILABLE')):
            raise HealthError('CORE_CLASSIFICATION_INVALID')
        if query == 'OK' and (evidence != 'OBSERVED' or inventory != stamp):
            raise HealthError('CORE_INVENTORY_TIME_INVALID')
        if query == 'UNAVAILABLE' and (evidence == 'OBSERVED'
                or evidence == 'CACHED' and inventory is None
                or evidence == 'UNAVAILABLE' and inventory is not None):
            raise HealthError('CORE_CACHED_PROVENANCE_INVALID')
        points = observation['last_valid_points']
        if not isinstance(points, dict) or set(points) != set(CTIDS):
            raise HealthError('CORE_MEMBERS_INVALID')
        projected = {}
        for ctid in CTIDS:
            point = points[ctid]
            if not isinstance(point, dict):
                raise HealthError('CORE_POINT_INVALID')
            ctime, cached_age = point.get('ctime'), point.get('age_seconds')
            if ctime is None:
                if cached_age is not None:
                    raise HealthError('CORE_POINT_AGE_INVALID')
                projected[ctid] = {'ctime': None, 'current_age_seconds': None}
                continue
            if type(ctime) is not int:
                raise HealthError('CORE_POINT_TIMESTAMP_TYPE_INVALID')
            ctime = numeric(ctime)
            cached_age = numeric(cached_age)
            if (inventory is None or ctime > inventory
                    or abs(stamp - ctime - cached_age) > TOLERANCE):
                raise HealthError('CORE_POINT_TIME_INVALID')
            projected[ctid] = {'ctime': ctime, 'current_age_seconds': now - ctime}
        if status == 'HEALTHY' and any(point['ctime'] is None for point in projected.values()):
            raise HealthError('CORE_HEALTHY_POINT_MISSING')
        output.update(observed_at_epoch=stamp, inventory_observed_at_epoch=inventory,
            observation_age_seconds=now - stamp, query_status=query, point_evidence=evidence,
            native_verdict=status, last_valid_points=projected)
        if not clock_safe:
            output['reason'] = 'CLOCK_UNSAFE'
        elif now - stamp > CORE_FRESHNESS:
            output['reason'] = 'CORE_OBSERVATION_STALE'
        elif query != 'OK':
            output['reason'] = 'CORE_QUERY_UNAVAILABLE'
        else:
            output.update(current_verdict=status, reason='CORE_CURRENT_OBSERVATION')
    except (HealthError, OSError, ValueError, TypeError, KeyError, OverflowError):
        pass
    return output


def cache_evidence(cache, now):
    if not isinstance(cache, dict) or cache.get('probe') != PROBE:
        raise HealthError('DB_CACHE_UNAVAILABLE')
    point, readback, age = (numeric(cache[key]) for key in
                            ('recovery_point_epoch', 'last_readback_epoch', 'age_seconds'))
    if any(type(cache[key]) is not int for key in ('recovery_point_epoch', 'last_readback_epoch', 'age_seconds')):
        raise HealthError('DB_CACHE_TIMESTAMP_TYPE_INVALID')
    asof = point + age
    if not 0 < point <= readback <= asof <= now:
        raise HealthError('DB_CACHE_TIME_INVALID')
    snapshot = cache.get('snapshot')
    if not isinstance(snapshot, str) or not re.fullmatch(
            r'host/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', snapshot):
        raise HealthError('DB_CACHE_SNAPSHOT_INVALID')
    parsed = datetime.strptime(snapshot.rsplit('/', 1)[1], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if parsed.timestamp() != point:
        raise HealthError('DB_CACHE_SNAPSHOT_TIME_INVALID')
    return {'recovery_point_epoch': cache['recovery_point_epoch'], 'last_readback_epoch': cache['last_readback_epoch'],
            'evidence_as_of_epoch': cache['recovery_point_epoch'] + cache['age_seconds'],
            'cached_age_seconds': cache['age_seconds'],
            'current_point_age_seconds': now - point, 'evidence_age_seconds': now - asof,
            'snapshot': snapshot, 'provenance': 'NATIVE_HEALTHY_ONLY_CACHE'}


def parse_unit(values):
    if not isinstance(values, dict) or set(values) != set(UNIT_FIELDS):
        raise HealthError('DB_UNIT_UNAVAILABLE')
    if (values['ActiveState'] not in ('active', 'inactive', 'activating', 'deactivating', 'failed', 'reloading')
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', values['SubState'])
            or values['Result'] not in ('success', 'exit-code', 'signal', 'timeout', 'watchdog',
                'start-limit-hit', 'resources', 'protocol', 'oom-kill')
            or not re.fullmatch(r'[a-f0-9]{32}', values['InvocationID'])):
        raise HealthError('DB_UNIT_FIELDS_INVALID')
    for key in ('ExecMainStatus', 'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic'):
        if not re.fullmatch(r'[0-9]{1,20}', values[key]):
            raise HealthError('DB_UNIT_NUMBER_INVALID')
    code = int(values['ExecMainStatus'])
    start = int(values['ExecMainStartTimestampMonotonic'])
    finish = int(values['ExecMainExitTimestampMonotonic'])
    if code > 255 or start > 2**63 - 1 or finish > 2**63 - 1:
        raise HealthError('DB_UNIT_NUMBER_INVALID')
    return {'active_state': values['ActiveState'], 'sub_state': values['SubState'],
            'result': values['Result'], 'exit_status': code, 'invocation_id': values['InvocationID'],
            'start_monotonic_us': start, 'finish_monotonic_us': finish}


def database_summary(cache, unit, now, mono, *, clock_safe=True, previous_evidence=None):
    output = {'current_verdict': 'UNKNOWN', 'reason': 'DB_EVIDENCE_UNAVAILABLE',
              'last_healthy_evidence': None, 'native_invocation': None}
    try:
        native = parse_unit(unit)
        output['native_invocation'] = native
    except (HealthError, ValueError, TypeError, KeyError, OverflowError):
        native = None
    try:
        evidence = cache_evidence(cache, now)
        output['last_healthy_evidence'] = evidence
    except (HealthError, ValueError, TypeError, KeyError, OverflowError):
        # A previously validated local projection remains historical, never current.
        try:
            previous = previous_evidence or {}
            historical = cache_evidence({'recovery_point_epoch': previous['recovery_point_epoch'],
                'last_readback_epoch': previous['last_readback_epoch'],
                'age_seconds': previous['cached_age_seconds'], 'snapshot': previous['snapshot'], 'probe': PROBE}, now)
            historical['provenance'] = 'PREVIOUS_SUMMARY_HEALTHY_CACHE'
            output['last_healthy_evidence'] = historical
        except (HealthError, ValueError, TypeError, KeyError, OverflowError):
            pass
        return output
    try:
        if native is None:
            raise HealthError('DB_UNIT_UNAVAILABLE')
        start = native['start_monotonic_us'] / 1000000
        finish = native['finish_monotonic_us'] / 1000000
        if start <= 0 or start > mono or finish > mono or finish < 0:
            raise HealthError('DB_UNIT_CLOCK_INVALID')
        native.update(start_epoch=now - (mono - start), finish_epoch=None if finish == 0 else now - (mono - finish))
        if native['start_epoch'] < 0 or native['finish_epoch'] is not None and native['finish_epoch'] < 0:
            raise HealthError('DB_UNIT_CLOCK_INVALID')
        if not clock_safe:
            output['reason'] = 'CLOCK_UNSAFE'
        elif native['active_state'] != 'inactive' or native['sub_state'] != 'dead':
            output['reason'] = 'DB_NATIVE_NOT_COMPLETED'
        elif native['result'] != 'success' or native['exit_status'] != 0:
            output['reason'] = 'DB_NATIVE_NOT_SUCCESSFUL'
        elif not 0 < start <= finish:
            output['reason'] = 'DB_NATIVE_TIMESTAMPS_INVALID'
        elif mono - finish > DB_FRESHNESS or evidence['evidence_age_seconds'] > DB_FRESHNESS:
            output['reason'] = 'DB_EVIDENCE_STALE'
        elif not native['start_epoch'] - TOLERANCE <= evidence['evidence_as_of_epoch'] <= native['finish_epoch'] + TOLERANCE:
            output['reason'] = 'DB_CACHE_NOT_CURRENT_INVOCATION'
        else:
            output.update(current_verdict='HEALTHY', reason='DB_CURRENT_NATIVE_AND_CACHE_MATCH')
    except (HealthError, ValueError, TypeError, KeyError, OverflowError):
        output['reason'] = 'DB_NATIVE_UNAVAILABLE_OR_UNSAFE'
    return output


def build(config, core, cache, unit, now, mono, *, clock_safe=True, previous=None):
    validate(config)
    now, mono = numeric(now), numeric(mono, maximum=2**53)
    old_database = previous.get('database', {}) if isinstance(previous, dict) else {}
    old_evidence = old_database.get('last_healthy_evidence') if isinstance(old_database, dict) else None
    return {'schema_version': 1, 'kind': KIND, 'node': config['expected_hostname'],
            'observed_at_epoch': now, 'clock_status': 'OK' if clock_safe else 'UNSAFE',
            'notifications_enabled': False,
            'core': core_summary(core, now, clock_safe=clock_safe),
            'database': database_summary(cache, unit, now, mono, clock_safe=clock_safe,
                previous_evidence=old_evidence)}


def run(config, directory=STATE_DIR, *, reader=read_json, unit_reader=unit_observation,
        clock=time.time, monotonic=time.monotonic):
    validate(config)
    with locked(directory):
        path = directory / 'result.json'
        previous = None
        try:
            value = reader(path)
            if (type(value.get('schema_version')) is int and value['schema_version'] == 1
                    and value.get('kind') == KIND and value.get('node') == config['expected_hostname']):
                numeric(value['observed_at_epoch'])
                previous = value
        except (HealthError, OSError, ValueError, TypeError, KeyError, OverflowError):
            pass
        before, before_mono = numeric(clock()), numeric(monotonic(), maximum=2**53)
        inputs = []
        for filename in (config['core_state_file'], config['database_cache_file']):
            try:
                inputs.append(reader(Path(filename)))
            except (HealthError, OSError, ValueError, TypeError):
                inputs.append(None)
        try:
            unit = unit_reader(config['database_unit'])
        except (HealthError, OSError, ValueError, TypeError, subprocess.SubprocessError):
            unit = None
        now, mono = numeric(clock()), numeric(monotonic(), maximum=2**53)
        safe = (now >= before and mono >= before_mono
                and abs((now - before) - (mono - before_mono)) <= TOLERANCE
                and (previous is None or now >= numeric(previous['observed_at_epoch'])))
        report = build(config, inputs[0], inputs[1], unit, now, mono, clock_safe=safe, previous=previous)
        atomic_json(path, report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise HealthError('SUMMARY_REQUIRES_ROOT')
        result = run(read_json(args.config))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (HealthError, OSError, ValueError, TypeError, KeyError, OverflowError):
        print('{"kind":"backup-freshness-summary","reason":"SUMMARY_UNAVAILABLE","notifications_enabled":false}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
