#!/usr/bin/env python3
"""Bounded external HTTPS shape checks and opt-in dedicated public generation."""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
from llm_health import HealthError, MAX_JSON, NAME, atomic_json, epoch, locked, private_read, read_json

spec = importlib.util.spec_from_file_location('known_answer_probe', HERE / 'llm-health-probe.py')
known_answer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(known_answer)

HOST = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
TLS_SOURCES = {'public-tls-production', 'public-tls-development', 'public-tls-gateway'}
PROBE_LIMITS = {'maximum_output_tokens': 8, 'maximum_daily_requests': 312,
                'minimum_interval_seconds': 300, 'total_timeout_seconds': 20}


def validate(config: dict) -> None:
    if config.get('kind') not in ('public-http', 'public-generation', 'tls') or not NAME.fullmatch(config.get('source', '')):
        raise HealthError('PUBLIC_CONFIG_INVALID')
    url = urlsplit(config.get('url', ''))
    allowed = config.get('allowed_hosts')
    if (url.scheme != 'https' or not HOST.fullmatch(url.hostname or '')
            or not isinstance(allowed, list) or not allowed or len(allowed) > 8
            or any(not isinstance(host, str) or not HOST.fullmatch(host) for host in allowed)
            or url.hostname not in allowed or url.port not in (None, 443)
            or url.username or url.password or url.query or url.fragment
            or not url.path.startswith('/') or len(url.path) > 256):
        raise HealthError('EXPLICIT_HTTPS_TARGET_REQUIRED')
    if config['kind'] == 'public-http':
        response = config.get('response', {})
        if (response.get('kind') not in ('json', 'html') or config.get('expected_status') != 200
                or type(config.get('minimum_certificate_seconds', 604800)) is not int
                or not 86400 <= config.get('minimum_certificate_seconds', 604800) <= 2592000):
            raise HealthError('PUBLIC_EXPECTATION_INVALID')
        if response['kind'] == 'json':
            required = response.get('required')
            if (not isinstance(required, dict) or not required or len(required) > 16
                    or any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', key)
                           or type(value) not in (str, int, bool) for key, value in required.items())):
                raise HealthError('JSON_FIELDS_REQUIRED')
        elif not isinstance(response.get('marker'), str) or not 4 <= len(response['marker']) <= 128:
            raise HealthError('HTML_MARKER_REQUIRED')
    elif config['kind'] == 'tls':
        if (url.path != '/' or config['source'] not in TLS_SOURCES
                or config.get('minimum_certificate_seconds', 604800) != 604800):
            raise HealthError('CRITICAL_TLS_CONFIG_INVALID')
    elif (url.path != '/v1/chat/completions' or type(config.get('canary_enabled', False)) is not bool
          or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', config.get('model', ''))):
        raise HealthError('PUBLIC_CANARY_INVALID')


def attest(config: dict, token: str, now: float, *, reader=read_json) -> None:
    """Require a bounded operator receipt; this does not create server policy."""
    value = reader(Path(config['policy_attestation']))
    url = urlsplit(config['url'])
    issued, expires = epoch(value.get('issued_at')), epoch(value.get('expires_at'))
    server = value.get('server_enforced')
    probe_limits = value.get('probe_enforced')
    required_server = {'operations', 'maximum_paid_credits', 'maximum_daily_tokens',
        'requests_per_minute', 'maximum_concurrency', 'expires_at', 'effective_models', 'fallback', 'route'}
    if (not isinstance(server, dict) or set(server) != required_server
            or server['operations'] != ['chat'] or server['effective_models'] != [config['model']]
            or server['fallback'] is not None or server['route'] != 'SELF_HOSTED'
            or any(type(server.get(key)) is not int or server[key] != expected for key, expected in (
                ('maximum_paid_credits', 0), ('maximum_daily_tokens', 64000),
                ('requests_per_minute', 1), ('maximum_concurrency', 1)))
            or epoch(server['expires_at']) < expires
            or not isinstance(probe_limits, dict) or probe_limits != PROBE_LIMITS
            or any(type(probe_limits.get(key)) is not int for key in PROBE_LIMITS)):
        raise HealthError('POLICY_ENFORCEMENT_BOUNDARY_INVALID')
    if (value.get('kind') != 'selfhost-only-diagnostic-identity-v1' or value.get('valid') is not True
            or value.get('gateway_origin') != f'https://{url.hostname}'
            or value.get('model') != config['model']
            or value.get('key_sha256') != hashlib.sha256(token.encode()).hexdigest()
            or not re.fullmatch(r'[a-f0-9]{64}', value.get('server_policy_sha256', ''))
            or value.get('student_account') is not False
            or not issued <= now <= expires or expires - issued > 180):
        raise HealthError('SERVER_POLICY_ATTESTATION_INVALID')


def inspect_response(config: dict, status: int, content_type: str, body: bytes) -> dict:
    if config['kind'] == 'public-generation' and status in (401, 403):
        return {'status': 'OBSERVER_ERROR', 'reason': 'CREDENTIAL_REJECTED', 'http_status': status}
    if status != 200:
        return {'status': 'FAILED', 'reason': 'HTTP_STATUS_FAILED', 'http_status': status}
    if config['kind'] == 'public-generation':
        if content_type != 'application/json':
            raise HealthError('GENERATION_NOT_JSON')
        result = known_answer.checked_generation(json.loads(body))
    else:
        expected = config['response']
        if expected['kind'] == 'json':
            if content_type != 'application/json':
                raise HealthError('EXPECTED_JSON_RECEIVED_OTHER_CONTENT')
            document = json.loads(body)
            if (not isinstance(document, dict) or any(type(document.get(key)) is not type(value)
                    or document.get(key) != value for key, value in expected['required'].items())):
                raise HealthError('JSON_SHAPE_MISMATCH')
        elif content_type != 'text/html' or expected['marker'] not in body.decode('utf-8'):
            raise HealthError('HTML_SHAPE_MISMATCH')
        result = {'status': 'HEALTHY', 'reason': 'PUBLIC_SHAPE_VERIFIED'}
    result['http_status'] = status
    return result


def http_worker(request: dict) -> dict:
    config = request['config']
    validate(config)
    url = urlsplit(config['url'])
    started = time.monotonic()

    def phase(name):
        if request.get('trace_phases') is True:
            print(json.dumps({'phase': name}), flush=True)

    phase('DNS')
    try:
        addresses = socket.getaddrinfo(url.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise socket.gaierror()
    except OSError:
        return {'status': 'OBSERVER_ERROR', 'reason': 'DNS_OBSERVATION_UNAVAILABLE',
                'phase': 'DNS', 'target': config['source']}

    def connect_resolved(address, timeout, source_address=None):
        # Preserve the configured hostname for TLS SNI/verification while using
        # only the completed lookup, so a second resolver call cannot stall.
        deadline = time.monotonic() + 3
        for family, socktype, protocol, canonical, sockaddr in addresses[:8]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            selected = socket.socket(family, socktype, protocol)
            try:
                selected.settimeout(remaining)
                selected.connect(sockaddr)
                return selected
            except OSError:
                selected.close()
        raise ConnectionError()

    phase('TLS')
    connection = http.client.HTTPSConnection(url.hostname, 443, timeout=3,
                                             context=ssl.create_default_context())
    connection._create_connection = connect_resolved
    try:
        connection.connect()
        certificate = connection.sock.getpeercert()
        remaining = ssl.cert_time_to_seconds(certificate['notAfter']) - time.time()
        if remaining < config.get('minimum_certificate_seconds', 604800):
            return {'status': 'FAILED', 'reason': 'TLS_CERTIFICATE_EXPIRING', 'phase': 'TLS'}
        if config['kind'] == 'tls':
            return {'status': 'HEALTHY', 'reason': 'TLS_CERTIFICATE_VERIFIED', 'phase': 'TLS',
                    'certificate_remaining_seconds': int(remaining),
                    'latency_seconds': round(time.monotonic() - started, 3)}
        connection.sock.settimeout(20 if config['kind'] == 'public-generation' else 10)
        phase('HTTP')
        headers = {'Accept': 'application/json' if config['kind'] == 'public-generation'
                   or config['response']['kind'] == 'json' else 'text/html'}
        body = None
        if config['kind'] == 'public-generation':
            token = request['token']
            if not token or '\r' in token or '\n' in token:
                raise HealthError('PUBLIC_CREDENTIAL_INVALID')
            headers.update(Authorization='Bearer ' + token, **{'Content-Type': 'application/json'})
            body = json.dumps({'model': config['model'], 'messages': [
                {'role': 'user', 'content': 'What is 2+2? Reply with the digit only.'}],
                'temperature': 0, 'top_p': 1, 'max_completion_tokens': 8, 'stream': False}).encode()
        connection.request('POST' if body else 'GET', url.path, body, headers)
        response = connection.getresponse()
        document = response.read(MAX_JSON + 1)
        if len(document) > MAX_JSON:
            raise HealthError('PUBLIC_RESPONSE_TOO_LARGE')
        media_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
        result = inspect_response(config, response.status, media_type, document)
        result['latency_seconds'] = round(time.monotonic() - started, 3)
        result['certificate_remaining_seconds'] = int(remaining)
        return result
    except ssl.SSLCertVerificationError:
        return {'status': 'FAILED', 'reason': 'TLS_CERTIFICATE_VALIDATION_FAILED', 'phase': 'TLS'}
    except (ssl.SSLError, TimeoutError, ConnectionError, OSError):
        if config['kind'] == 'tls':
            return {'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE', 'phase': 'TLS'}
        raise
    finally:
        connection.close()


def transport(config: dict, token: str | None = None, *, timeout: float | None = None) -> dict:
    budget = timeout if timeout is not None else 20 if config['kind'] == 'public-generation' else 10
    started = time.monotonic()
    try:
        child = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--http-worker'],
            input=json.dumps({'config': config, 'token': token, 'trace_phases': True}), capture_output=True, text=True,
            timeout=budget, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        if child.returncode or len(child.stdout) > MAX_JSON:
            raise HealthError('PUBLIC_WORKER_INVALID')
        lines = child.stdout.splitlines()
        if not lines or len(lines) > 4:
            raise HealthError('PUBLIC_WORKER_INVALID')
        for line in lines[:-1]:
            if json.loads(line).get('phase') not in ('DNS', 'TLS', 'HTTP'):
                raise HealthError('PUBLIC_WORKER_INVALID')
        result = json.loads(lines[-1])
        if not isinstance(result, dict) or result.get('status') not in ('HEALTHY', 'FAILED', 'OBSERVER_ERROR'):
            raise HealthError('PUBLIC_WORKER_INVALID')
        return result
    except subprocess.TimeoutExpired as error:
        captured = error.stdout or b''
        if isinstance(captured, bytes):
            captured = captured.decode('utf-8', errors='replace')
        phase = None
        for line in captured[:MAX_JSON].splitlines():
            try:
                phase = json.loads(line).get('phase')
            except (ValueError, AttributeError):
                pass
        if phase == 'DNS':
            return {'status': 'OBSERVER_ERROR', 'reason': 'DNS_OBSERVATION_UNAVAILABLE',
                    'phase': 'DNS', 'target': config['source'],
                    'latency_seconds': round(time.monotonic() - started, 3)}
        if phase not in ('TLS', 'HTTP'):
            return {'status': 'OBSERVER_ERROR', 'reason': 'PUBLIC_WORKER_STARTUP_TIMEOUT',
                    'phase': 'UNKNOWN', 'target': config['source']}
        if config['kind'] == 'tls':
            return {'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE',
                    'phase': 'TLS', 'target': config['source']}
        return {'status': 'FAILED', 'reason': 'DNS_TLS_HTTP_TOTAL_TIMEOUT',
                'phase': phase if phase in ('TLS', 'HTTP') else 'UNKNOWN',
                'latency_seconds': round(time.monotonic() - started, 3)}
    except (HealthError, OSError, ValueError):
        return {'status': 'OBSERVER_ERROR', 'reason': 'PUBLIC_WORKER_UNAVAILABLE'}


def probe(config: dict, directory: Path, *, now: float | None = None,
          requester=transport, attester=attest) -> dict:
    validate(config)
    now = epoch(time.time() if now is None else now)
    expected_host = config.get('expected_hostname')
    if not isinstance(expected_host, str) or expected_host != socket.gethostname():
        raise HealthError('PUBLIC_OBSERVER_HOST_MISMATCH')
    with locked(directory):
        path = directory / 'budget.json'
        state = read_json(path) if path.exists() else {}
        if now < state.get('last_check', 0):
            raise HealthError('CLOCK_MOVED_BACKWARDS')
        state['last_check'] = now
        result = {'version': 1, 'source': config['source'], 'kind': config['kind'], 'observed_at': now}
        token = None
        if config['kind'] == 'public-generation':
            if config.get('canary_enabled') is not True:
                result.update(status='OBSERVER_ERROR', reason='PUBLIC_CANARY_DISABLED')
                return result
            token = private_read(Path(config['credential_file']), 4096).decode().strip()
            if not token or '\r' in token or '\n' in token:
                raise HealthError('PUBLIC_CREDENTIAL_INVALID')
            try:
                attester(config, token, now)
            except (HealthError, OSError, ValueError, TypeError, KeyError):
                result.update(status='OBSERVER_ERROR', reason='POLICY_OBSERVATION_UNAVAILABLE')
                atomic_json(path, state)
                atomic_json(directory / 'result.json', result)
                return result
            day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
            if state.get('day') != day:
                state.update(day=day, submissions=0)
            if now - state.get('last_submission', -300) < 300 or state['submissions'] >= 312:
                result.update(status='BUDGET_LIMIT', reason='PUBLIC_CANARY_REQUEST_LIMIT')
                atomic_json(path, state)
                return result
            state['submissions'] += 1
            state['last_submission'] = now
        atomic_json(path, state)
        result.update(requester(config, token))
        atomic_json(directory / 'result.json', result)
        return result


def worker_main() -> int:
    request = {}
    try:
        request = json.loads(sys.stdin.read(MAX_JSON))
        result = http_worker(request)
    except ssl.SSLCertVerificationError:
        result = {'status': 'FAILED', 'reason': 'TLS_CERTIFICATE_VALIDATION_FAILED'}
    except ssl.SSLError:
        result = ({'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE', 'phase': 'TLS'}
                  if request.get('config', {}).get('kind') == 'tls' else
                  {'status': 'FAILED', 'reason': 'TLS_HANDSHAKE_FAILED'})
    except socket.gaierror:
        result = {'status': 'OBSERVER_ERROR', 'reason': 'DNS_OBSERVATION_UNAVAILABLE', 'phase': 'DNS'}
    except (TimeoutError, ConnectionError):
        result = ({'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE', 'phase': 'TLS'}
                  if request.get('config', {}).get('kind') == 'tls' else
                  {'status': 'FAILED', 'reason': 'PUBLIC_TRANSPORT_FAILED'})
    except (HealthError, OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        result = ({'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE', 'phase': 'TLS'}
                  if request.get('config', {}).get('kind') == 'tls' else
                  {'status': 'FAILED', 'reason': 'PUBLIC_RESPONSE_INVALID'})
    print(json.dumps(result, allow_nan=False, sort_keys=True))
    return 0


def main() -> int:
    if sys.argv[1:] == ['--http-worker']:
        return worker_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(probe(read_json(args.config), args.state_dir), sort_keys=True))
        return 0
    except (HealthError, OSError, ValueError, TypeError, KeyError):
        print('{"status":"OBSERVER_ERROR","reason":"PUBLIC_CONFIGURATION_OR_STATE_INVALID"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
