#!/usr/bin/env python3
"""Emit fixed, sanitized local health results for one forced SSH command."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from llm_health import HealthError, MAX_JSON, NAME, STATES, epoch, read_json
from health_result_transport import bounded_process, inspect_bundle, invalid_policy, sanitize_policy, sanitize_report

PROTOCOL = 'read-health-v1'
PATHS = {
    'model-host': {'host-query': '/var/lib/pickle-llm-host-query/result.json',
                   'local-generation': '/var/lib/pickle-llm-local-generation/result.json'},
    'gateway-container': {'upstream-generation': '/var/lib/pickle-llm-upstream-generation/result.json'},
    'public-observer': {'public-console': '/var/lib/pickle-public-console/result.json',
                        'public-api': '/var/lib/pickle-public-api/result.json',
                        'public-development': '/var/lib/pickle-public-development/result.json'},
    'collector-host': {'collector-heartbeat': '/var/lib/pickle-service-health-monitor/summary.json'},
}
POLICY_PATHS = {'gateway-host': '/var/lib/pickle-public-policy/attestation.json',
                'collector-host': '/var/lib/pickle-health-import-gateway/policy-attestation.json'}


def heartbeat(value: dict, now: float) -> dict:
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or not isinstance(value.get('sources'), dict) or not 1 <= len(value['sources']) <= 32):
        raise HealthError('COLLECTOR_SUMMARY_INVALID')
    timestamp = epoch(value.get('observed_at'))
    if timestamp > now + 30:
        raise HealthError('COLLECTOR_SUMMARY_FUTURE')
    for source, observation in value['sources'].items():
        if (not NAME.fullmatch(source) or not isinstance(observation, dict)
                or observation.get('status') not in STATES
                or epoch(observation.get('sample_at')) > timestamp + 30):
            raise HealthError('COLLECTOR_SUMMARY_SOURCE_INVALID')
    return {'version': 1, 'source': 'collector-heartbeat', 'kind': 'observer-report',
            'observed_at': timestamp, 'status': 'HEALTHY', 'reason': 'COLLECTOR_HEARTBEAT_VERIFIED'}


def collect(config: dict, *, now: float | None = None, reader=read_json,
            runner=bounded_process) -> dict:
    now = epoch(time.time() if now is None else now)
    role = config.get('role')
    if (role not in (*PATHS, 'gateway-host') or config.get('expected_hostname') != socket.gethostname()
            or type(config.get('include_public_generation', False)) is not bool
            or type(config.get('include_collector_heartbeat', False)) is not bool
            or type(config.get('include_policy_attestation', False)) is not bool
            or type(config.get('include_staging', False)) is not bool
            or config.get('include_staging') and role != 'public-observer'
            or config.get('include_policy_attestation') and role not in POLICY_PATHS
            or not isinstance(config.get('tls_sources', []), list)
            or len(config.get('tls_sources', [])) != len(set(config.get('tls_sources', [])))
            or not set(config.get('tls_sources', [])) <= {
                'public-tls-production', 'public-tls-development', 'public-tls-gateway', 'public-tls-staging'}
            or config.get('tls_sources') and role != 'public-observer'):
        raise HealthError('READER_DEPLOYMENT_INVALID')
    if role == 'gateway-host':
        document = runner(['/usr/sbin/pct', 'exec', '203', '--', '/usr/bin/python3', '-I', '-B',
            '/usr/local/libexec/pickle/health-result-reader.py', '--config',
            '/etc/pickle-llm-health/reader.json', '--local-child'], timeout=10)
        reports = inspect_bundle(document, ['upstream-generation'], now)
    else:
        paths = dict(PATHS[role])
        if role == 'public-observer' and config.get('include_public_generation'):
            paths['public-generation'] = '/var/lib/pickle-public-generation/result.json'
        if role == 'public-observer' and config.get('include_collector_heartbeat'):
            paths['collector-heartbeat'] = '/var/lib/pickle-service-health-monitor/summary.json'
        if role == 'public-observer' and config.get('include_staging'):
            paths['public-staging'] = '/var/lib/pickle-public-staging/result.json'
            paths['public-staging-expiry-warning'] = '/var/lib/pickle-public-staging-expiry-warning/result.json'
        if role == 'public-observer':
            for source in config.get('tls_sources', []):
                paths[source] = '/var/lib/pickle-public-' + source.removeprefix('public-') + '/result.json'
        reports = {}
        for source, path in paths.items():
            try:
                document = reader(Path(path))
                reports[source] = (heartbeat(document, now) if source == 'collector-heartbeat' else
                                   sanitize_report(document, source, now))
            except (HealthError, OSError, ValueError, TypeError, KeyError):
                reports[source] = {'version': 1, 'source': source, 'kind': 'observer-report',
                    'observed_at': now, 'status': 'OBSERVER_ERROR', 'reason': 'REMOTE_RESULT_UNAVAILABLE'}
    result = {'version': 1, 'generated_at': now, 'reports': reports}
    if config.get('include_policy_attestation'):
        try:
            result['policy_attestation'] = sanitize_policy(reader(Path(POLICY_PATHS[role])), now)
        except (HealthError, OSError, ValueError, TypeError, KeyError):
            result['policy_attestation'] = invalid_policy(now)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--local-child', action='store_true')
    args = parser.parse_args()
    try:
        config = read_json(args.config)
        if args.local_child:
            if os.geteuid() != 0 or config.get('role') != 'gateway-container':
                raise HealthError('READER_CHILD_DENIED')
        elif os.environ.get('SSH_ORIGINAL_COMMAND') != PROTOCOL:
            raise HealthError('READER_COMMAND_DENIED')
        result = json.dumps(collect(config), allow_nan=False, sort_keys=True)
        if len(result.encode()) > MAX_JSON:
            raise HealthError('READER_OUTPUT_TOO_LARGE')
        print(result)
        return 0
    except (HealthError, OSError, ValueError, TypeError, KeyError):
        print('{"status":"OBSERVER_ERROR","reason":"RESULT_READER_DENIED_OR_UNAVAILABLE"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
