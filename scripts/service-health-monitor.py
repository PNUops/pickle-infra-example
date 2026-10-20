#!/usr/bin/env python3
"""Collect allowlisted local health reports without remote shell access."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from llm_health import (HealthError, NAME, STATES, atomic_json, deliver, epoch, locked,
                        observe, read_json, reserve_event, smtp_sender)

CRITICAL_TLS_FAILURES = {'TLS_CERTIFICATE_VALIDATION_FAILED', 'TLS_CERTIFICATE_EXPIRING'}


def validate(config: dict) -> None:
    sources = config.get('sources')
    if not isinstance(sources, list) or not sources or len(sources) > 32:
        raise HealthError('SOURCES_INVALID')
    seen = set()
    for source in sources:
        name = source.get('name', '')
        path = Path(source.get('path', ''))
        critical_tls = source.get('component') == 'critical-tls' and source.get('expected_kind') == 'tls'
        if (not NAME.fullmatch(name) or name in seen or not path.is_absolute()
                or '..' in path.parts or source.get('component', 'query') not in ('query', 'host', 'critical-tls')
                or type(source.get('freshness_seconds')) is not int
                or not 60 <= source['freshness_seconds'] <= 3600
                or type(source.get('failure_count')) is not int
                or (source['failure_count'] != 1 if critical_tls else not 2 <= source['failure_count'] <= 5)
                or source.get('component') == 'critical-tls' and not critical_tls):
            raise HealthError('SOURCE_INVALID')
        seen.add(name)
    if type(config.get('notifications_enabled', False)) is not bool:
        raise HealthError('NOTIFICATION_SETTING_INVALID')


def sample(source: dict, now: float, *, reader=read_json) -> tuple[str, float, str]:
    try:
        value = reader(Path(source['path']))
        timestamp = epoch(value['observed_at'])
        if (timestamp > now + 30 or now - timestamp > source['freshness_seconds']
                or value.get('version') != 1 or value.get('source') != source.get('report_source', source['name'])):
            raise HealthError('RESULT_STALE_OR_INVALID')
        value = value['host'] if source.get('component') == 'host' else value
        status = value['status']
        if source.get('component') == 'critical-tls':
            if (value.get('kind') != 'tls'
                    or status == 'FAILED' and value.get('reason') not in CRITICAL_TLS_FAILURES
                    or status == 'HEALTHY' and value.get('reason') != 'TLS_CERTIFICATE_VERIFIED'
                    or status not in ('HEALTHY', 'FAILED', 'OBSERVER_ERROR')):
                raise HealthError('TLS_SECURITY_EVIDENCE_INVALID')
        if status not in STATES:
            raise HealthError('RESULT_STATUS_INVALID')
        if status in ('OBSERVER_ERROR', 'BUDGET_LIMIT'):
            return 'OBSERVER_ERROR', timestamp, 'PROBE_OBSERVER_ERROR'
        return status, timestamp, 'SOURCE_OBSERVED'
    except (HealthError, OSError, ValueError, TypeError, KeyError):
        return 'OBSERVER_ERROR', now, 'RESULT_UNAVAILABLE'


def collect(config: dict, state: dict, now: float, *, reader=read_json,
            notifications: bool = False) -> dict:
    validate(config)
    now = epoch(now)
    if now < state.get('last_collection', 0):
        raise HealthError('CLOCK_MOVED_BACKWARDS')
    state['last_collection'] = now
    summary = {'version': 1, 'observed_at': now, 'sources': {}}
    for source in config['sources']:
        name = source['name']
        status, timestamp, reason = sample(source, now, reader=reader)
        entry = state.setdefault('sources', {}).setdefault(name, {})
        event = observe(entry, status, timestamp, failure_count=source['failure_count'],
                        freshness=source['freshness_seconds'])
        # Unknown evidence interrupts the stable window even if its old timestamp
        # has already been consumed as an otherwise healthy source sample.
        if status == 'OBSERVER_ERROR':
            entry.update(healthy_since=None, healthy_samples=0, failures=0)
        observer = entry.setdefault('observer', {})
        observer_status = 'FAILED' if status == 'OBSERVER_ERROR' else 'HEALTHY'
        observer_event = observe(observer, observer_status, now,
                                 failure_count=3, freshness=180)
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
                if transition in ('FAILED', 'RECOVERED'):
                    reserve_event(target_state, target, transition, now)
                elif (target_state.get('incident')
                        and target_state.get('deliveries', {}).get('FAILED', {}).get('incident')
                        != target_state['incident']):
                    reserve_event(target_state, target, 'FAILED', now)
        summary['sources'][name] = {'status': status, 'reason': reason,
            'sample_at': timestamp, 'incident': entry.get('incident'),
            'observer_incident': observer.get('incident'), 'event': event,
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
                for entry in (source_state, source_state.get('observer', {})):
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
