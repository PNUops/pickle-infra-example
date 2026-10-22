#!/usr/bin/env python3
"""Collect allowlisted local health reports without remote shell access."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from llm_health import (HealthError, NAME, STATES, atomic_json, deliver, epoch, locked,
                        observe, read_json, reserve_event, smtp_sender)

CRITICAL_TLS_FAILURES = {'TLS_CERTIFICATE_VALIDATION_FAILED', 'TLS_CERTIFICATE_EXPIRING'}
WARNING_REASONS = {'FAILED': 'TLS_CERTIFICATE_EXPIRY_WARNING', 'HEALTHY': 'TLS_EXPIRY_WARNING_CLEAR'}
POLICY_FAILURES = CRITICAL_TLS_FAILURES | {'ACCESS_POLICY_VIOLATION'}


def validate(config: dict) -> None:
    sources = config.get('sources')
    if not isinstance(sources, list) or not sources or len(sources) > 32:
        raise HealthError('SOURCES_INVALID')
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            raise HealthError('SOURCE_INVALID')
        name = source.get('name', '')
        path = Path(source.get('path', ''))
        critical_tls = source.get('component') == 'critical-tls' and source.get('expected_kind') == 'tls'
        policy = source.get('component') == 'access-policy' and source.get('expected_kind') == 'access-policy'
        warning = source.get('severity') == 'warning' and source.get('expected_kind') == 'tls-expiry-warning'
        if (not NAME.fullmatch(name) or name in seen or not path.is_absolute()
                or '..' in path.parts or source.get('component', 'query') not in ('query', 'host', 'critical-tls', 'access-policy')
                or type(source.get('freshness_seconds')) is not int
                or not 60 <= source['freshness_seconds'] <= 3600
                or type(source.get('failure_count')) is not int
                or (source['failure_count'] != 1 if critical_tls or policy or warning else not 2 <= source['failure_count'] <= 5)
                or source.get('component') == 'critical-tls' and not critical_tls
                or source.get('component') == 'access-policy' and not policy
                or 'severity' in source and not warning
                or warning and source.get('component', 'query') != 'query'
                or source.get('expected_kind') == 'tls-expiry-warning' and not warning):
            raise HealthError('SOURCE_INVALID')
        seen.add(name)
    indexed = {source['name']: source for source in sources}
    for source in sources:
        if 'observer_parent' not in source:
            continue
        parent = source['observer_parent']
        if (not isinstance(parent, str) or parent not in indexed or parent == source['name']
                or 'observer_parent' in indexed[parent]
                or indexed[parent].get('expected_kind') != 'observer-report'
                or indexed[parent].get('component', 'query') != 'query'):
            raise HealthError('OBSERVER_PARENT_INVALID')
    if type(config.get('notifications_enabled', False)) is not bool:
        raise HealthError('NOTIFICATION_SETTING_INVALID')


def result_snapshot(sources: list[dict], reader) -> dict:
    """Read each path once before any source or dependency classification."""
    snapshot = {}
    for source in sources:
        path = Path(source['path'])
        if path in snapshot:
            continue
        try:
            snapshot[path] = (deepcopy(reader(path)), None)
        except (HealthError, OSError, ValueError, TypeError, KeyError) as error:
            snapshot[path] = (None, error)
    return snapshot


def sampled(source: dict, now: float, snapshot: dict) -> dict:
    result = {'status': 'OBSERVER_ERROR', 'sample_at': now, 'reason': 'RESULT_UNAVAILABLE',
        'raw_status': 'UNKNOWN', 'source_sample_at': None, 'source_reason': None,
        'unknown_reason': 'RESULT_INVALID'}
    if source.get('severity') == 'warning':
        result['severity'] = 'warning'
    value, error = snapshot[Path(source['path'])]
    if error is not None:
        result['unknown_reason'] = 'RESULT_MISSING' if isinstance(error, FileNotFoundError) else 'RESULT_READ_FAILED'
        return result
    try:
        timestamp = epoch(value['observed_at'])
        result['source_sample_at'] = timestamp
        original_reason = value.get('reason')
        if isinstance(original_reason, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', original_reason):
            result['source_reason'] = original_reason
        if value.get('version') != 1 or value.get('source') != source.get('report_source', source['name']):
            raise HealthError('RESULT_IDENTITY_INVALID')
        value = value['host'] if source.get('component') == 'host' else value
        status = value['status']
        if ((value.get('kind') == 'tls-expiry-warning' or 'severity' in value)
                and source.get('severity') != 'warning'):
            raise HealthError('WARNING_SOURCE_SELECTION_REQUIRED')
        if source.get('severity') == 'warning':
            if (value.get('kind') != 'tls-expiry-warning' or value.get('severity') != 'warning'
                    or status not in ('HEALTHY', 'FAILED', 'OBSERVER_ERROR')
                    or status in WARNING_REASONS and value.get('reason') != WARNING_REASONS[status]):
                raise HealthError('TLS_WARNING_EVIDENCE_INVALID')
            result['severity'] = 'warning'
        if source.get('component') == 'access-policy':
            if (value.get('kind') != 'access-policy' or status not in ('HEALTHY', 'FAILED', 'OBSERVER_ERROR')
                    or status == 'HEALTHY' and (value.get('reason') != 'ACCESS_POLICY_VERIFIED' or value.get('http_status') != 403)
                    or status == 'FAILED' and value.get('reason') not in POLICY_FAILURES):
                raise HealthError('ACCESS_POLICY_EVIDENCE_INVALID')
        if source.get('component') == 'critical-tls':
            if (value.get('kind') != 'tls'
                    or status == 'FAILED' and value.get('reason') not in CRITICAL_TLS_FAILURES
                    or status == 'HEALTHY' and value.get('reason') != 'TLS_CERTIFICATE_VERIFIED'
                    or status not in ('HEALTHY', 'FAILED', 'OBSERVER_ERROR')):
                raise HealthError('TLS_SECURITY_EVIDENCE_INVALID')
        if source.get('expected_kind') == 'observer-report':
            if (value.get('kind') != 'observer-report'
                    or (status, value.get('reason')) not in (
                        ('HEALTHY', 'RESULT_IMPORT_VERIFIED'), ('OBSERVER_ERROR', 'RESULT_IMPORT_UNAVAILABLE'))):
                raise HealthError('IMPORT_OBSERVER_EVIDENCE_INVALID')
        if status not in STATES:
            raise HealthError('RESULT_STATUS_INVALID')
        # Staleness correlates transport loss only after the selected component
        # has valid evidence; an old malformed report remains its own error.
        if timestamp > now + 30:
            result['unknown_reason'] = 'RESULT_FUTURE'
            return result
        if now - timestamp > source['freshness_seconds']:
            result['unknown_reason'] = 'RESULT_STALE'
            return result
        result.update(raw_status=status, sample_at=timestamp, unknown_reason=None)
        if status in ('OBSERVER_ERROR', 'BUDGET_LIMIT'):
            result.update(status='OBSERVER_ERROR', reason='PROBE_OBSERVER_ERROR')
        else:
            result.update(status=status, reason='SOURCE_OBSERVED')
    except (HealthError, OSError, ValueError, TypeError, KeyError):
        pass
    return result


def sample(source: dict, now: float, *, reader=read_json) -> tuple[str, float, str]:
    result = sampled(source, now, result_snapshot([source], reader))
    return result['status'], result['sample_at'], result['reason']


def collect(config: dict, state: dict, now: float, *, reader=read_json,
            notifications: bool = False) -> dict:
    validate(config)
    now = epoch(now)
    if now < state.get('last_collection', 0):
        raise HealthError('CLOCK_MOVED_BACKWARDS')
    state['last_collection'] = now
    summary = {'version': 1, 'observed_at': now, 'sources': {}}
    snapshot = result_snapshot(config['sources'], reader)
    samples = {source['name']: sampled(source, now, snapshot) for source in config['sources']}
    for source in config['sources']:
        name = source['name']
        observation = samples[name]
        status, timestamp = observation['status'], observation['sample_at']
        parent = source.get('observer_parent')
        parent_status = samples[parent]['status'] if parent is not None else None
        suppressed_by = (parent if observation['unknown_reason'] in ('RESULT_MISSING', 'RESULT_STALE')
                         and parent_status == 'OBSERVER_ERROR' else None)
        entry = state.setdefault('sources', {}).setdefault(name, {})
        event = observe(entry, status, timestamp, failure_count=source['failure_count'],
                        freshness=source['freshness_seconds'])
        # Unknown evidence interrupts the stable window even if its old timestamp
        # has already been consumed as an otherwise healthy source sample.
        if status == 'OBSERVER_ERROR':
            entry.update(healthy_since=None, healthy_samples=0, failures=0)
        observer = entry.setdefault('observer', {})
        observer_status = ('OBSERVER_ERROR' if suppressed_by else
                           'FAILED' if status == 'OBSERVER_ERROR' else 'HEALTHY')
        observer_event = observe(observer, observer_status, now,
                                 failure_count=3, freshness=180)
        if suppressed_by:
            # A dependency loss also interrupts a sample already consumed at
            # this collection epoch; it never confirms observer recovery.
            observer.update(healthy_since=None, healthy_samples=0, failures=0)
        for target_state, current_status, transition in (
                (entry, status, event), (observer, observer_status, observer_event)):
            if current_status != 'HEALTHY':
                previous_recovery = target_state.get('deliveries', {}).get('RECOVERED', {})
                if previous_recovery.get('status') in ('NEW', 'NOT_SENT'):
                    previous_recovery['status'] = 'SUPERSEDED'
            if transition == 'RECOVERED':
                previous_failure = target_state.get('deliveries', {}).get('FAILED', {})
                if previous_failure.get('status') in ('NEW', 'NOT_SENT'):
                    previous_failure['status'] = 'SUPERSEDED'
        if notifications:
            for target_state, target, transition in (
                    (entry, name, event), (observer, 'observer-' + name, observer_event)):
                if target_state is observer and suppressed_by:
                    continue
                if transition in ('FAILED', 'RECOVERED'):
                    delivery = reserve_event(target_state, target, transition, now)
                    if target_state is entry and source.get('severity') == 'warning':
                        delivery['severity'] = 'warning'
                elif (target_state.get('incident')
                        and target_state.get('deliveries', {}).get('FAILED', {}).get('incident')
                        != target_state['incident']):
                    delivery = reserve_event(target_state, target, 'FAILED', now)
                    if target_state is entry and source.get('severity') == 'warning':
                        delivery['severity'] = 'warning'
        summary['sources'][name] = {**observation, 'incident': entry.get('incident'),
            'observer_incident': observer.get('incident'), 'event': event,
            'observer_status': observer_status, 'observer_event': observer_event,
            'suppressed_by': suppressed_by, 'parent_status': parent_status,
            'recovery_candidate_since': entry.get('healthy_since')}
    return summary


def run(config: dict, directory: Path, *, dry_run: bool = False,
        now: float | None = None, reader=read_json, sender=None) -> dict:
    validate(config)
    now = time.time() if now is None else now
    if dry_run:
        # No lock, state mutation, event reservation or SMTP configuration read.
        state_path = directory / 'state.json'
        state = read_json(state_path) if state_path.exists() else {}
        return collect(config, deepcopy(state), now, reader=reader)
    with locked(directory):
        path = directory / 'state.json'
        state = read_json(path) if path.exists() else {}
        enabled = config.get('notifications_enabled', False)
        summary = collect(config, state, now, reader=reader, notifications=enabled)
        persist = lambda: atomic_json(path, state)
        persist()
        if enabled:
            if sender is None:
                sender = smtp_sender(read_json(Path(config['mail_config'])))
            for source in config['sources']:
                source_state = state['sources'][source['name']]
                entries = [source_state]
                if not summary['sources'][source['name']]['suppressed_by']:
                    entries.append(source_state.get('observer', {}))
                for entry in entries:
                    for delivery in entry.get('deliveries', {}).values():
                        deliver(delivery, now, sender, persist)
        atomic_json(directory / 'summary.json', summary)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(run(read_json(args.config), args.state_dir, dry_run=args.dry_run), sort_keys=True))
        return 0
    except (HealthError, OSError, ValueError, TypeError, KeyError):
        print('{"status":"OBSERVER_ERROR","reason":"COLLECTOR_CONFIGURATION_OR_STATE_INVALID"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
