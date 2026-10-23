#!/usr/bin/env python3
"""Bounded external HTTPS shape checks and an opt-in public generation canary."""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import http.client
import ipaddress
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


HOST = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
TLS_SOURCES = {'public-tls-production', 'public-tls-development', 'public-tls-gateway', 'public-tls-staging'}
TLS_ONLY = {'tls', 'tls-expiry-warning'}
OBSERVATION_ONLY = TLS_ONLY | {'access-policy'}
# These are the three standard RFC 1918 network definitions, not host inventory.
RFC1918_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    (0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16)))
# The canary's own bounds. They hold whatever the gateway routes the model to:
# a self-hosted upstream or a paid one (operator decision 2026-10-23, the model
# was routed to a paid provider and the canary is meant to keep running). The
# output allowance is sized for a reasoning model, which spends part of the
# budget on reasoning tokens before it answers: measured 2026-10-24 against the
# paid route, 8 tokens ended in finish_reason=length with no content, while 64
# and 256 both answered in 17 to 20 completion tokens. Worst case at that
# route's price is 312 * (19 input + 128 output) tokens, about two US cents a
# day; the key's own daily token cap stays the server-side bound.
MAXIMUM_OUTPUT_TOKENS = 128
MAXIMUM_DAILY_REQUESTS = 312
MINIMUM_INTERVAL_SECONDS = 300


def validate(config: dict) -> None:
    if config.get('kind') not in ('public-http', 'public-generation', 'tls', 'tls-expiry-warning', 'access-policy') or not NAME.fullmatch(config.get('source', '')):
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
    if 'required_dns_san' in config and config['required_dns_san'] != url.hostname:
        raise HealthError('EXACT_DNS_SAN_REQUIRED')
    if (config['source'] in {'public-staging', 'public-tls-staging', 'public-staging-expiry-warning', 'staging-access-policy'}
            and config.get('required_dns_san') != url.hostname):
        raise HealthError('EXACT_DNS_SAN_REQUIRED')
    staging_kinds = {'public-staging': 'public-http', 'public-tls-staging': 'tls',
                     'public-staging-expiry-warning': 'tls-expiry-warning', 'staging-access-policy': 'access-policy'}
    if config['source'] in staging_kinds and config['kind'] != staging_kinds[config['source']]:
        raise HealthError('STAGING_SOURCE_KIND_INVALID')
    if ('severity' in config and (config['kind'] != 'tls-expiry-warning' or config['severity'] != 'warning')):
        raise HealthError('WARNING_SCOPE_INVALID')
    if 'connect_address' in config and config['kind'] != 'access-policy':
        raise HealthError('CONNECT_OVERRIDE_NOT_ALLOWED')
    if config['kind'] == 'public-http':
        response = config.get('response', {})
        if (response.get('kind') not in ('json', 'html', 'text') or config.get('expected_status') != 200
                or type(config.get('minimum_certificate_seconds', 604800)) is not int
                or not 86400 <= config.get('minimum_certificate_seconds', 604800) <= 2592000):
            raise HealthError('PUBLIC_EXPECTATION_INVALID')
        if response['kind'] == 'json':
            required = response.get('required')
            if (not isinstance(required, dict) or not required or len(required) > 16
                    or any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', key)
                           or type(value) not in (str, int, bool) for key, value in required.items())):
                raise HealthError('JSON_FIELDS_REQUIRED')
        elif response['kind'] == 'text':
            if (not isinstance(response.get('exact'), str) or not 4 <= len(response['exact'].encode('utf-8')) <= 128
                    or set(response) != {'kind', 'exact'}):
                raise HealthError('EXACT_TEXT_REQUIRED')
        elif not isinstance(response.get('marker'), str) or not 4 <= len(response['marker']) <= 128:
            raise HealthError('HTML_MARKER_REQUIRED')
        if config['source'] == 'public-staging' and (url.path != '/__ingress_probe'
                or response != {'kind': 'text', 'exact': 'candidate-ready'}):
            raise HealthError('STAGING_RESPONSE_INVALID')
    elif config['kind'] == 'tls':
        if (url.path != '/' or config['source'] not in TLS_SOURCES
                or config.get('minimum_certificate_seconds', 604800) != 604800):
            raise HealthError('CRITICAL_TLS_CONFIG_INVALID')
    elif config['kind'] == 'tls-expiry-warning':
        if (url.path != '/' or config['source'] != 'public-staging-expiry-warning'
                or config.get('minimum_certificate_seconds') != 2592000
                or config.get('severity') != 'warning'):
            raise HealthError('TLS_WARNING_CONFIG_INVALID')
    elif config['kind'] == 'access-policy':
        try:
            if not isinstance(config.get('connect_address'), str):
                raise ipaddress.AddressValueError('not a literal address')
            address = ipaddress.IPv4Address(config.get('connect_address', ''))
        except ipaddress.AddressValueError as error:
            raise HealthError('PRIVATE_POLICY_ADDRESS_REQUIRED') from error
        if (not any(address in network for network in RFC1918_NETWORKS)
                or config['source'] != 'staging-access-policy' or url.path != '/__ingress_probe'
                or config.get('expected_status') != 403 or config.get('response') != {'kind': 'status'}
                or config.get('minimum_certificate_seconds', 604800) != 604800):
            raise HealthError('PRIVATE_POLICY_CONFIG_INVALID')
    elif (url.path != '/v1/chat/completions' or type(config.get('canary_enabled', False)) is not bool
          or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', config.get('model', ''))
          or 'policy_attestation' in config):
        # policy_attestation is refused rather than ignored: a config that still
        # names a receipt reads as if the receipt gated the canary, and it no
        # longer does.
        raise HealthError('PUBLIC_CANARY_INVALID')


def checked_public_generation(document: dict) -> dict:
    """The known answer, judged the way the direct generation probe judges it.

    Only the output allowance differs from llm-health-probe.py: completion
    tokens include reasoning tokens, so the bound is the canary's own request
    cap rather than the direct probe's eight. finish_reason must still be stop,
    which is what tells an exhausted reasoning budget (length, no content) from
    an answer.
    """
    try:
        choice = document['choices'][0]
        content = choice['message']['content']
        output = document['usage']['completion_tokens']
        prompt = document['usage']['prompt_tokens']
        valid_usage = (type(output) is int and 0 < output <= MAXIMUM_OUTPUT_TOKENS
                       and type(prompt) is int and 0 < prompt <= 4096)
        correct = isinstance(content, str) and content.strip() == '4'
        good = correct and choice['finish_reason'] == 'stop' and valid_usage
        return {'status': 'HEALTHY' if good else 'FAILED',
                'reason': 'ANSWER_VERIFIED' if good else 'GENERATION_VALIDATION_FAILED',
                'answer_correct': correct, 'completion_tokens': output if valid_usage else None,
                'prompt_tokens': prompt if valid_usage else None}
    except (KeyError, IndexError, TypeError):
        return {'status': 'FAILED', 'reason': 'GENERATION_RESPONSE_INVALID',
                'answer_correct': False, 'completion_tokens': None, 'prompt_tokens': None}


def inspect_response(config: dict, status: int, content_type: str, body: bytes) -> dict:
    if config['kind'] == 'access-policy':
        return {'status': 'HEALTHY' if status == 403 else 'FAILED',
                'reason': 'ACCESS_POLICY_VERIFIED' if status == 403 else 'ACCESS_POLICY_VIOLATION',
                'http_status': status}
    if config['kind'] == 'public-generation' and status in (401, 403):
        return {'status': 'OBSERVER_ERROR', 'reason': 'CREDENTIAL_REJECTED', 'http_status': status}
    if status != 200:
        return {'status': 'FAILED', 'reason': 'HTTP_STATUS_FAILED', 'http_status': status}
    if config['kind'] == 'public-generation':
        if content_type != 'application/json':
            raise HealthError('GENERATION_NOT_JSON')
        result = checked_public_generation(json.loads(body))
    else:
        expected = config['response']
        if expected['kind'] == 'json':
            if content_type != 'application/json':
                raise HealthError('EXPECTED_JSON_RECEIVED_OTHER_CONTENT')
            document = json.loads(body)
            if (not isinstance(document, dict) or any(type(document.get(key)) is not type(value)
                    or document.get(key) != value for key, value in expected['required'].items())):
                raise HealthError('JSON_SHAPE_MISMATCH')
        elif expected['kind'] == 'text':
            if content_type != 'text/plain' or body != expected['exact'].encode('utf-8'):
                raise HealthError('TEXT_SHAPE_MISMATCH')
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
        addresses = socket.getaddrinfo(config.get('connect_address', url.hostname), 443, type=socket.SOCK_STREAM)
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
        if ('required_dns_san' in config and config['required_dns_san'] not in
                [name for kind, name in certificate.get('subjectAltName', ()) if kind == 'DNS']):
            return {'status': 'OBSERVER_ERROR' if config['kind'] == 'tls-expiry-warning' else 'FAILED',
                    'reason': 'TLS_OBSERVATION_UNAVAILABLE' if config['kind'] == 'tls-expiry-warning' else 'TLS_CERTIFICATE_VALIDATION_FAILED',
                    'phase': 'TLS'}
        remaining = ssl.cert_time_to_seconds(certificate['notAfter']) - time.time()
        if config['kind'] == 'tls-expiry-warning':
            return {'status': 'FAILED' if remaining < 2592000 else 'HEALTHY',
                    'severity': 'warning', 'phase': 'TLS',
                    'reason': 'TLS_CERTIFICATE_EXPIRY_WARNING' if remaining < 2592000 else 'TLS_EXPIRY_WARNING_CLEAR',
                    'certificate_remaining_seconds': max(0, int(remaining)),
                    'latency_seconds': round(time.monotonic() - started, 3)}
        if remaining < config.get('minimum_certificate_seconds', 604800):
            return {'status': 'FAILED', 'reason': 'TLS_CERTIFICATE_EXPIRING', 'phase': 'TLS'}
        if config['kind'] == 'tls':
            return {'status': 'HEALTHY', 'reason': 'TLS_CERTIFICATE_VERIFIED', 'phase': 'TLS',
                    'certificate_remaining_seconds': int(remaining),
                    'latency_seconds': round(time.monotonic() - started, 3)}
        connection.sock.settimeout(20 if config['kind'] == 'public-generation' else 10)
        phase('HTTP')
        headers = {'Accept': 'application/json' if config['kind'] == 'public-generation'
                   or config['response']['kind'] == 'json' else 'text/plain'
                   if config['response']['kind'] in ('text', 'status') else 'text/html'}
        body = None
        if config['kind'] == 'public-generation':
            token = request['token']
            if not token or '\r' in token or '\n' in token:
                raise HealthError('PUBLIC_CREDENTIAL_INVALID')
            headers.update(Authorization='Bearer ' + token, **{'Content-Type': 'application/json'})
            body = json.dumps({'model': config['model'], 'messages': [
                {'role': 'user', 'content': 'What is 2+2? Reply with the digit only.'}],
                'temperature': 0, 'top_p': 1, 'max_completion_tokens': MAXIMUM_OUTPUT_TOKENS,
                'stream': False}).encode()
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
        return {'status': 'OBSERVER_ERROR' if config['kind'] == 'tls-expiry-warning' else 'FAILED',
                'reason': 'TLS_OBSERVATION_UNAVAILABLE' if config['kind'] == 'tls-expiry-warning' else 'TLS_CERTIFICATE_VALIDATION_FAILED', 'phase': 'TLS'}
    except (ssl.SSLError, TimeoutError, ConnectionError, OSError):
        if config['kind'] in OBSERVATION_ONLY:
            return {'status': 'OBSERVER_ERROR', 'reason': 'ACCESS_POLICY_OBSERVATION_UNAVAILABLE'
                    if config['kind'] == 'access-policy' else 'TLS_OBSERVATION_UNAVAILABLE', 'phase': 'TLS'}
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
        if config['kind'] in OBSERVATION_ONLY:
            return {'status': 'OBSERVER_ERROR', 'reason': 'ACCESS_POLICY_OBSERVATION_UNAVAILABLE'
                    if config['kind'] == 'access-policy' else 'TLS_OBSERVATION_UNAVAILABLE',
                    'phase': phase, 'target': config['source']}
        return {'status': 'FAILED', 'reason': 'DNS_TLS_HTTP_TOTAL_TIMEOUT',
                'phase': phase if phase in ('TLS', 'HTTP') else 'UNKNOWN',
                'latency_seconds': round(time.monotonic() - started, 3)}
    except (HealthError, OSError, ValueError):
        return {'status': 'OBSERVER_ERROR', 'reason': 'PUBLIC_WORKER_UNAVAILABLE'}


def probe(config: dict, directory: Path, *, now: float | None = None,
          requester=transport) -> dict:
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
        if config['kind'] == 'tls-expiry-warning':
            result['severity'] = 'warning'
        token = None
        if config['kind'] == 'public-generation':
            if config.get('canary_enabled') is not True:
                result.update(status='OBSERVER_ERROR', reason='PUBLIC_CANARY_DISABLED')
                return result
            token = private_read(Path(config['credential_file']), 4096).decode().strip()
            if not token or '\r' in token or '\n' in token:
                raise HealthError('PUBLIC_CREDENTIAL_INVALID')
            day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
            if state.get('day') != day:
                state.update(day=day, submissions=0)
            if (now - state.get('last_submission', -MINIMUM_INTERVAL_SECONDS) < MINIMUM_INTERVAL_SECONDS
                    or state['submissions'] >= MAXIMUM_DAILY_REQUESTS):
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
        result = ({'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE'}
                  if request.get('config', {}).get('kind') == 'tls-expiry-warning' else
                  {'status': 'FAILED', 'reason': 'TLS_CERTIFICATE_VALIDATION_FAILED'})
    except ssl.SSLError:
        result = ({'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE', 'phase': 'TLS'}
                  if request.get('config', {}).get('kind') in OBSERVATION_ONLY else
                  {'status': 'FAILED', 'reason': 'TLS_HANDSHAKE_FAILED'})
    except socket.gaierror:
        result = {'status': 'OBSERVER_ERROR', 'reason': 'DNS_OBSERVATION_UNAVAILABLE', 'phase': 'DNS'}
    except (TimeoutError, ConnectionError):
        result = ({'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE', 'phase': 'TLS'}
                  if request.get('config', {}).get('kind') in OBSERVATION_ONLY else
                  {'status': 'FAILED', 'reason': 'PUBLIC_TRANSPORT_FAILED'})
    except (HealthError, OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        result = ({'status': 'OBSERVER_ERROR', 'reason': 'TLS_OBSERVATION_UNAVAILABLE', 'phase': 'TLS'}
                  if request.get('config', {}).get('kind') in OBSERVATION_ONLY else
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
