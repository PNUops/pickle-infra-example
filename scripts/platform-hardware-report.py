#!/usr/bin/env python3
"""Deliver one complete automatic hardware snapshot from fixed local/imported reads."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from hardware_observation import HardwareError, MAX_BYTES, observe, validate_observation
from health_result_transport import bounded_process
from llm_health import atomic_json, private_directory, private_read, read_json

API_PATH = '/internal/platform-hardware-observations'


def endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise HardwareError('HARDWARE_ENDPOINT_INVALID')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.path != API_PATH
                or parsed.query or parsed.fragment or parsed.username is not None
                or parsed.password is not None or not 1 <= (parsed.port or 443) <= 65535):
            raise ValueError()
        return parsed
    except ValueError:
        raise HardwareError('HARDWARE_ENDPOINT_INVALID') from None


def protected_path(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts:
        raise HardwareError('HARDWARE_PATH_INVALID')
    return Path(value)


def ssh_command(value):
    required = {'address', 'port', 'user', 'identity_file', 'known_hosts_file'}
    if not isinstance(value, dict) or set(value) != required:
        raise HardwareError('HARDWARE_SSH_INVALID')
    try:
        address = ipaddress.ip_address(value['address'])
    except (ValueError, TypeError):
        raise HardwareError('HARDWARE_SSH_INVALID') from None
    shared = address.version == 4 and address in ipaddress.ip_network('100.64.0.0/10')
    if (not (address.is_private or shared) or address.is_unspecified or address.is_multicast
            or address.is_link_local or address.is_reserved or type(value['port']) is not int
            or not 1 <= value['port'] <= 65535 or not isinstance(value['user'], str)
            or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value['user'])):
        raise HardwareError('HARDWARE_SSH_INVALID')
    for key in ('identity_file', 'known_hosts_file'):
        private_read(protected_path(value[key]))
    options = ['BatchMode=yes', 'IdentitiesOnly=yes', 'StrictHostKeyChecking=yes',
        'UserKnownHostsFile=' + value['known_hosts_file'], 'GlobalKnownHostsFile=/dev/null',
        'IdentityAgent=none', 'ForwardAgent=no', 'ClearAllForwardings=yes',
        'ExitOnForwardFailure=yes', 'ControlMaster=no', 'ControlPath=none',
        'PermitLocalCommand=no', 'ProxyCommand=none', 'ProxyJump=none', 'RequestTTY=no',
        'ConnectTimeout=3', 'ConnectionAttempts=1', 'ServerAliveInterval=3', 'ServerAliveCountMax=1']
    argv = ['ssh', '-F', '/dev/null']
    for option in options:
        argv.extend(['-o', option])
    return [*argv, '-i', value['identity_file'], '-p', str(value['port']), '-l', value['user'],
            '--', str(address), 'read-hardware-v1']


def configuration(value):
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'enabled', 'source_id',
            'expected_hostname', 'api_url', 'credential_file', 'assets'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or type(value['enabled']) is not bool):
        raise HardwareError('HARDWARE_CONFIGURATION_INVALID')
    if not value['enabled']:
        return value
    if value['expected_hostname'] != socket.gethostname():
        raise HardwareError('HARDWARE_HOST_MISMATCH')
    try:
        if str(UUID(value['source_id'])) != value['source_id']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HardwareError('HARDWARE_SOURCE_INVALID') from None
    endpoint(value['api_url'])
    protected_path(value['credential_file'])
    assets = value['assets']
    if not isinstance(assets, list) or not 1 <= len(assets) <= 32:
        raise HardwareError('HARDWARE_ROSTER_INVALID')
    ids, local = set(), 0
    for asset in assets:
        if (not isinstance(asset, dict) or not isinstance(asset.get('id'), str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', asset['id'])
                or asset['id'] in ids):
            raise HardwareError('HARDWARE_ROSTER_INVALID')
        ids.add(asset['id'])
        if set(asset) == {'id', 'local'} and asset['local'] is True:
            local += 1
        elif set(asset) == {'id', 'ssh'}:
            ssh_command(asset['ssh'])
        else:
            raise HardwareError('HARDWARE_ROSTER_INVALID')
    if local != 1:
        raise HardwareError('HARDWARE_ROSTER_INVALID')
    return value


def payload(config, now, *, local_observer=observe, ssh_runner=bounded_process):
    assets, stamps = [], []
    def remote(selected):
        document = ssh_runner(ssh_command(selected['ssh']), timeout=12, maximum=MAX_BYTES)
        if len(document) > MAX_BYTES:
            raise HardwareError('HARDWARE_BODY_INVALID')
        return json.loads(document)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {selected['id']: pool.submit(remote, selected) for selected in config['assets']
                   if selected.get('local') is not True}
        local = local_observer(now=now)
        for selected in config['assets']:
            raw = local if selected.get('local') is True else pending[selected['id']].result()
            observed = validate_observation(raw, now)
            if observed['status'] != 'OBSERVED':
                raise HardwareError('HARDWARE_SNAPSHOT_INCOMPLETE')
            assets.append({'id': selected['id'], **observed['asset']})
            stamps.append(observed['observed_at'])
    return {'schemaVersion': 1, 'sourceId': config['source_id'],
            'observedAt': datetime.fromtimestamp(min(stamps), timezone.utc)
                                  .isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'assets': assets}


def http_worker(config, body, *, connection_factory=None):
    if not 0 < len(body) <= MAX_BYTES:
        raise HardwareError('HARDWARE_BODY_INVALID')
    url = endpoint(config['api_url'])
    token = private_read(protected_path(config['credential_file']), 4096).decode('ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,4096}', token):
        raise HardwareError('HARDWARE_CREDENTIAL_INVALID')
    factory = connection_factory or http.client.HTTPSConnection
    connection = factory(url.hostname, url.port or 443, timeout=3,
                         context=ssl.create_default_context())
    try:
        connection.connect()
        connection.sock.settimeout(5)
        connection.request('POST', url.path, body=body,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            return {'status': response.status}
        reply = response.read(16385)
        if len(reply) > 16384:
            raise HardwareError('HARDWARE_ACK_INVALID')
        ack = json.loads(reply)
        if not isinstance(ack, dict) or set(ack) != {'sourceId', 'observedAt'}:
            raise HardwareError('HARDWARE_ACK_INVALID')
        return {'status': 200, 'ack': ack}
    finally:
        connection.close()


def send(config_path, body):
    try:
        result = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
            'http-worker', '--config', str(config_path)], input=body,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise HardwareError('HARDWARE_TRANSPORT_FAILED') from None
    if result.returncode or len(result.stdout) > 16384:
        raise HardwareError('HARDWARE_TRANSPORT_FAILED')
    return json.loads(result.stdout)


def instant(value):
    if not isinstance(value, str) or len(value) > 64:
        raise HardwareError('HARDWARE_ACK_INVALID')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise HardwareError('HARDWARE_ACK_INVALID')
    return parsed


def report(config, config_path, directory, *, now=None, local_observer=observe,
           ssh_runner=bounded_process, sender=send):
    config = configuration(config)
    private_directory(directory)
    result = {'status': 'DISABLED'}
    if config['enabled']:
        try:
            value = payload(config, time.time() if now is None else now,
                            local_observer=local_observer, ssh_runner=ssh_runner)
            body = json.dumps(value, allow_nan=False, sort_keys=True).encode('utf-8')
            if len(body) > MAX_BYTES:
                raise HardwareError('HARDWARE_BODY_INVALID')
        except Exception:
            result = {'status': 'INCOMPLETE', 'reason': 'HARDWARE_SNAPSHOT_UNAVAILABLE'}
        else:
            try:
                response = sender(config_path, body)
                if response.get('status') == 409:
                    result = {'status': 'CURRENT', 'reason': 'HARDWARE_SNAPSHOT_ALREADY_CURRENT'}
                elif (response.get('status') == 200 and isinstance(response.get('ack'), dict)
                        and response['ack'].get('sourceId') == config['source_id']
                        and instant(response['ack'].get('observedAt')) == instant(value['observedAt'])):
                    result = {'status': 'PUBLISHED', 'observedAt': value['observedAt']}
                else:
                    result = {'status': 'ERROR', 'reason': 'HARDWARE_DELIVERY_NOT_ACKNOWLEDGED'}
            except Exception:
                result = {'status': 'ERROR', 'reason': 'HARDWARE_DELIVERY_UNAVAILABLE'}
    atomic_json(directory / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('report', 'http-worker'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path)
    args = parser.parse_args()
    try:
        config = read_json(args.config)
        if args.operation == 'http-worker':
            value = http_worker(config, sys.stdin.buffer.read(MAX_BYTES + 1))
        else:
            if args.state_dir is None:
                raise HardwareError('HARDWARE_STATE_DIRECTORY_REQUIRED')
            value = report(config, args.config, args.state_dir)
        print(json.dumps(value, allow_nan=False, sort_keys=True))
        return 0 if value.get('status') in (200, 409, 'PUBLISHED', 'CURRENT', 'DISABLED') else 1
    except Exception:
        print('{"status":"ERROR","reason":"HARDWARE_CONFIGURATION_OR_TRANSPORT_INVALID"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
