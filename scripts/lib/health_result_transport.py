"""Fixed health-result schema and bounded subprocess transport."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
from urllib.parse import urlsplit

from llm_health import HealthError, MAX_JSON, STATES, epoch

SOURCES = {'host-query', 'local-generation', 'upstream-generation', 'public-console',
           'public-api', 'public-development', 'public-generation', 'collector-heartbeat',
           'public-tls-production', 'public-tls-development', 'public-tls-gateway'}
KINDS = {'host-query', 'generation', 'public-http', 'public-generation', 'observer-report', 'tls'}
SAFE_REASON = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
NUMBERS = {'latency_seconds': 3600, 'certificate_remaining_seconds': 366 * 86400,
           'completion_tokens': 8, 'prompt_tokens': 4096, 'generation_counter_delta': 10**15}
HOST_NUMBERS = {'host_available_bytes': 2**60, 'host_total_bytes': 2**60,
                'gpu_temperature_c': 200, 'gpu_utilization_percent': 100,
                'gpu_memory_total_mib': 2**40}
METRICS = {'generation_tokens', 'prompt_tokens', 'running', 'waiting', 'kv_cache_usage'}
POLICY_KIND = 'selfhost-only-diagnostic-identity-v1'
POLICY_REASONS = {'POLICY_OBSERVATION_FAILED', 'POLICY_MISMATCH', 'QUOTA_EXHAUSTED',
                  'GATEWAY_NOT_READY', 'POLICY_TRANSPORT_UNAVAILABLE'}
POLICY_PROBE = {'maximum_output_tokens': 8, 'maximum_daily_requests': 312,
                'minimum_interval_seconds': 300, 'total_timeout_seconds': 20}


def invalid_policy(now: float) -> dict:
    return {'kind': POLICY_KIND, 'valid': False, 'reason': 'POLICY_TRANSPORT_UNAVAILABLE',
            'issued_at': epoch(now), 'expires_at': epoch(now)}


def sanitize_policy(value: dict, now: float) -> dict:
    if (not isinstance(value, dict) or value.get('kind') != POLICY_KIND
            or type(value.get('valid')) is not bool):
        raise HealthError('POLICY_SCHEMA_INVALID')
    issued, expires = epoch(value.get('issued_at')), epoch(value.get('expires_at'))
    if issued > now + 30 or issued > 4102444800 or expires > 4102444800:
        raise HealthError('POLICY_TIME_INVALID')
    result = {'kind': POLICY_KIND, 'valid': value['valid'], 'issued_at': issued, 'expires_at': expires}
    if not value['valid']:
        if value.get('reason') not in POLICY_REASONS or expires != issued:
            raise HealthError('POLICY_INVALID_REASON_OR_TIME')
        result['reason'] = value['reason']
        return result
    if not issued <= now <= expires or not 0 < expires - issued <= 180:
        raise HealthError('POLICY_EXPIRED')
    origin = value.get('gateway_origin')
    if not isinstance(origin, str) or not 9 <= len(origin) <= 261:
        raise HealthError('POLICY_ORIGIN_INVALID')
    url = urlsplit(origin)
    if (url.scheme != 'https' or not re.fullmatch(
            r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', url.hostname or '')
            or origin != 'https://' + url.hostname or url.port is not None
            or url.username or url.password or url.path or url.query or url.fragment):
        raise HealthError('POLICY_ORIGIN_INVALID')
    model = value.get('model')
    if not isinstance(model, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', model):
        raise HealthError('POLICY_MODEL_INVALID')
    for key in ('key_sha256', 'server_policy_sha256'):
        if not isinstance(value.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', value[key]):
            raise HealthError('POLICY_HASH_INVALID')
        result[key] = value[key]
    if value.get('student_account') is not False:
        raise HealthError('POLICY_IDENTITY_INVALID')
    server = value.get('server_enforced')
    probe = value.get('probe_enforced')
    if (not isinstance(server, dict) or not isinstance(probe, dict)
            or server.get('operations') != ['chat'] or server.get('effective_models') != [model]
            or 'fallback' not in server or server['fallback'] is not None
            or server.get('route') != 'SELF_HOSTED'):
        raise HealthError('POLICY_ENFORCEMENT_INVALID')
    safe_server = {'operations': ['chat'], 'effective_models': [model], 'fallback': None, 'route': 'SELF_HOSTED'}
    for key, expected in (('maximum_paid_credits', 0), ('maximum_daily_tokens', 64000),
                          ('requests_per_minute', 1), ('maximum_concurrency', 1)):
        if type(server.get(key)) is not int or server[key] != expected:
            raise HealthError('POLICY_SERVER_LIMIT_INVALID')
        safe_server[key] = server[key]
    key_expiry = epoch(server.get('expires_at'))
    if not expires <= key_expiry <= 4102444800:
        raise HealthError('POLICY_KEY_EXPIRY_INVALID')
    safe_server['expires_at'] = key_expiry
    for key, expected in POLICY_PROBE.items():
        if type(probe.get(key)) is not int or probe[key] != expected:
            raise HealthError('POLICY_PROBE_LIMIT_INVALID')
    result.update(gateway_origin=origin, model=model, student_account=False,
                  server_enforced=safe_server, probe_enforced=dict(POLICY_PROBE))
    return result


def number(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise HealthError('REPORT_NUMBER_INVALID')
    return value


def sanitize_report(value: dict, source: str, now: float) -> dict:
    if not isinstance(value, dict) or source not in SOURCES:
        raise HealthError('REPORT_SOURCE_INVALID')
    timestamp = epoch(value.get('observed_at'))
    if (type(value.get('version')) is not int or value['version'] != 1 or value.get('source') != source
            or value.get('kind') not in KINDS or value.get('status') not in STATES
            or timestamp > now + 30):
        raise HealthError('REPORT_SCHEMA_INVALID')
    result = {key: value[key] for key in ('version', 'source', 'kind', 'observed_at', 'status')}
    if 'phase' in value:
        if value['phase'] not in ('DNS', 'TLS', 'HTTP', 'UNKNOWN'):
            raise HealthError('REPORT_PHASE_INVALID')
        result['phase'] = value['phase']
    if 'target' in value:
        if value['target'] != source:
            raise HealthError('REPORT_TARGET_INVALID')
        result['target'] = value['target']
    for key in ('reason', 'metrics_reason'):
        if key in value:
            if not isinstance(value[key], str) or not SAFE_REASON.fullmatch(value[key]):
                raise HealthError('REPORT_REASON_INVALID')
            result[key] = value[key]
    for key, maximum in NUMBERS.items():
        if key in value:
            result[key] = None if value[key] is None else number(value[key], maximum)
    if 'http_status' in value:
        if type(value['http_status']) is not int or not 100 <= value['http_status'] <= 599:
            raise HealthError('REPORT_HTTP_STATUS_INVALID')
        result['http_status'] = value['http_status']
    for key in ('answer_correct', 'counter_reset'):
        if key in value:
            if type(value[key]) is not bool:
                raise HealthError('REPORT_BOOLEAN_INVALID')
            result[key] = value[key]
    if 'metrics_status' in value:
        if value['metrics_status'] not in ('SUPPORTED', 'UNSUPPORTED', 'OBSERVER_ERROR'):
            raise HealthError('REPORT_METRICS_STATUS_INVALID')
        result['metrics_status'] = value['metrics_status']
    if 'metrics' in value:
        if not isinstance(value['metrics'], dict) or not set(value['metrics']) <= METRICS:
            raise HealthError('REPORT_METRICS_INVALID')
        result['metrics'] = {key: number(item, 10**15) for key, item in value['metrics'].items()}
    if 'host' in value:
        host = value['host']
        if not isinstance(host, dict) or host.get('status') not in STATES:
            raise HealthError('REPORT_HOST_INVALID')
        safe_host = {'status': host['status']}
        if 'reason' in host:
            if not isinstance(host['reason'], str) or not SAFE_REASON.fullmatch(host['reason']):
                raise HealthError('REPORT_HOST_REASON_INVALID')
            safe_host['reason'] = host['reason']
        if 'boot_id' in host:
            if not re.fullmatch(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}', host['boot_id']):
                raise HealthError('REPORT_BOOT_ID_INVALID')
            safe_host['boot_id'] = host['boot_id']
        for key, maximum in HOST_NUMBERS.items():
            if key in host:
                safe_host[key] = number(host[key], maximum)
        for key, allowed in (('gpu_memory', ('SUPPORTED', 'UNSUPPORTED')),
                             ('gpu_query', ('OBSERVER_ERROR',))):
            if key in host:
                if host[key] not in allowed:
                    raise HealthError('REPORT_GPU_STATUS_INVALID')
                safe_host[key] = host[key]
        result['host'] = safe_host
    return result


def bounded_process(argv: list[str], *, timeout: float, maximum: int = MAX_JSON) -> bytes:
    if not hasattr(os, 'WNOWAIT') or not hasattr(os, 'waitid'):
        raise HealthError('RESULT_TRANSPORT_WAIT_UNSUPPORTED')
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
        start_new_session=True)
    selector = selectors.DefaultSelector()
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HealthError('RESULT_TRANSPORT_TIMEOUT')
            ready = selector.select(remaining)
            if not ready:
                raise HealthError('RESULT_TRANSPORT_TIMEOUT')
            block = os.read(process.stdout.fileno(), min(8192, maximum + 1 - len(output)))
            if not block:
                selector.unregister(process.stdout)
                break
            output.extend(block)
            if len(output) > maximum:
                raise HealthError('RESULT_TRANSPORT_TOO_LARGE')
        # Leave the leader unreaped until its owned group is terminated. Even
        # an exited leader retains its PID, preventing process-group ID reuse.
        info = None
        while info is None and time.monotonic() < deadline:
            info = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if info is None:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        if info is None:
            raise HealthError('RESULT_TRANSPORT_TIMEOUT')
        if info.si_code != os.CLD_EXITED or info.si_status != 0:
            raise HealthError('RESULT_TRANSPORT_FAILED')
        return bytes(output)
    finally:
        selector.close()
        process.stdout.close()
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            process.wait()


def inspect_bundle(document: bytes, expected: list[str], now: float) -> dict:
    reports, _ = inspect_envelope(document, expected, now)
    return reports


def inspect_envelope(document: bytes, expected: list[str], now: float, *,
                     include_policy_attestation: bool = False) -> tuple[dict, dict | None]:
    if len(document) > MAX_JSON:
        raise HealthError('RESULT_BUNDLE_TOO_LARGE')
    value = json.loads(document)
    expected_keys = {'version', 'generated_at', 'reports'}
    if include_policy_attestation:
        expected_keys.add('policy_attestation')
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or set(value) != expected_keys
            or not isinstance(value.get('reports'), dict) or set(value['reports']) != set(expected)
            or not now - 120 <= epoch(value.get('generated_at')) <= now + 30):
        raise HealthError('RESULT_BUNDLE_INVALID')
    reports = {name: sanitize_report(value['reports'][name], name, now) for name in expected}
    policy = sanitize_policy(value['policy_attestation'], now) if include_policy_attestation else None
    return reports, policy
