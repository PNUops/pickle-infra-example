#!/usr/bin/env python3
"""Import one fixed health-result protocol with strict SSH identity and host pins."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from llm_health import HealthError, NAME, atomic_json, epoch, locked, private_directory, private_read, read_json
from health_result_transport import SOURCES, bounded_process, inspect_envelope, invalid_policy


def ssh_command(config: dict) -> list[str]:
    if type(config.get('include_policy_attestation', False)) is not bool:
        raise HealthError('IMPORT_POLICY_SETTING_INVALID')
    try:
        address = ipaddress.ip_address(config['address'])
    except ValueError as error:
        raise HealthError('IMPORT_ADDRESS_INVALID') from error
    shared = address.version == 4 and address in ipaddress.ip_network('100.64.0.0/10')
    if (not (address.is_private or shared) or address.is_link_local or address.is_multicast
            or address.is_unspecified or address.is_reserved
            or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', config.get('user', ''))
            or type(config.get('port')) is not int or not 1 <= config['port'] <= 65535
            or not NAME.fullmatch(config.get('source', ''))):
        raise HealthError('IMPORT_SSH_SELECTION_INVALID')
    expected = config.get('expected_sources')
    if (not isinstance(expected, list) or not expected or len(expected) > 9
            or len(set(expected)) != len(expected) or not set(expected) <= SOURCES):
        raise HealthError('IMPORT_SOURCES_INVALID')
    for key in ('identity_file', 'known_hosts_file'):
        path = Path(config[key])
        if not path.is_absolute() or '..' in path.parts:
            raise HealthError('IMPORT_PROTECTED_PATH_INVALID')
        private_read(path)
    options = ['BatchMode=yes', 'IdentitiesOnly=yes', 'StrictHostKeyChecking=yes',
        'UserKnownHostsFile=' + config['known_hosts_file'], 'GlobalKnownHostsFile=/dev/null',
        'IdentityAgent=none', 'ForwardAgent=no', 'ClearAllForwardings=yes',
        'ExitOnForwardFailure=yes', 'ControlMaster=no', 'ControlPath=none',
        'PermitLocalCommand=no', 'ProxyCommand=none', 'ProxyJump=none', 'RequestTTY=no',
        'ConnectTimeout=3', 'ConnectionAttempts=1', 'ServerAliveInterval=3', 'ServerAliveCountMax=1']
    argv = ['ssh', '-F', '/dev/null']
    for option in options:
        argv.extend(['-o', option])
    return [*argv, '-i', config['identity_file'], '-p', str(config['port']), '-l', config['user'],
            '--', str(address), 'read-health-v1']


def run(config: dict, directory: Path, *, now: float | None = None,
        runner=bounded_process) -> dict:
    now = epoch(time.time() if now is None else now)
    if config.get('expected_hostname') != socket.gethostname():
        raise HealthError('IMPORT_OBSERVER_HOST_MISMATCH')
    with locked(directory):
        include_policy = config.get('include_policy_attestation') is True
        policy_path = directory / 'policy-attestation.json'
        if include_policy:
            # Revoke the previous permission before validation or remote I/O.
            atomic_json(policy_path, invalid_policy(now))
        state_path = directory / 'state.json'
        state = read_json(state_path) if state_path.exists() else {}
        if now < state.get('last_check', 0):
            raise HealthError('CLOCK_MOVED_BACKWARDS')
        state['last_check'] = now
        atomic_json(state_path, state)
        result = {'version': 1, 'source': config['source'], 'kind': 'observer-report', 'observed_at': now}
        try:
            argv = ssh_command(config)
            reports, policy = inspect_envelope(runner(argv, timeout=15), config['expected_sources'], now,
                                               include_policy_attestation=include_policy)
            output = directory / 'reports'
            private_directory(output)
            # The complete bundle is validated before any selected result changes.
            for name, report in reports.items():
                atomic_json(output / (name + '.json'), report)
            if include_policy:
                atomic_json(policy_path, policy)
            result.update(status='HEALTHY', reason='RESULT_IMPORT_VERIFIED')
        except (HealthError, OSError, ValueError, TypeError, KeyError):
            if include_policy:
                atomic_json(policy_path, invalid_policy(now))
            result.update(status='OBSERVER_ERROR', reason='RESULT_IMPORT_UNAVAILABLE')
        try:
            atomic_json(directory / 'result.json', result)
        except (HealthError, OSError, ValueError, TypeError):
            if include_policy:
                atomic_json(policy_path, invalid_policy(now))
            raise
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(read_json(args.config), args.state_dir), sort_keys=True))
        return 0
    except (HealthError, OSError, ValueError, TypeError, KeyError):
        print('{"status":"OBSERVER_ERROR","reason":"RESULT_IMPORT_CONFIGURATION_INVALID"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
