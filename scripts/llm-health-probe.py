#!/usr/bin/env python3
"""Publish bounded self-hosted host, model-query and known-answer generation checks."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from llm_health import (HealthError, MAX_JSON, NAME, atomic_json, epoch, locked,
                        private_read, read_json)

MAX_OUTPUT = 8
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}')
METRICS = {'vllm:generation_tokens_total': 'generation_tokens',
           'vllm:prompt_tokens_total': 'prompt_tokens',
           'vllm:num_requests_running': 'running', 'vllm:num_requests_waiting': 'waiting',
           'vllm:kv_cache_usage_perc': 'kv_cache_usage'}


def parse_metrics(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^\n]*\})?\s+([^\s]+)', line)
        if match and match[1] in METRICS:
            number = float(match[2])
            if not math.isfinite(number) or number < 0:
                raise HealthError('METRIC_VALUE_INVALID')
            name = METRICS[match[1]]
            result[name] = result.get(name, 0) + number
    return result


def counters(state: dict, metrics: dict, boot_id: str) -> dict:
    previous = state.get('metrics', {})
    old = previous.get('generation_tokens')
    current = metrics.get('generation_tokens')
    reset = (state.get('metrics_boot_id') != boot_id or old is None or current is None
             or current < old)
    state.update(metrics=metrics, metrics_boot_id=boot_id)
    return {'counter_reset': reset, 'generation_counter_delta':
            None if reset else current - old}


def validate(config: dict) -> None:
    if (config.get('kind') not in ('host-query', 'generation')
            or not NAME.fullmatch(str(config.get('source', '')))
            or not MODEL.fullmatch(str(config.get('model', '')))):
        raise HealthError('CONFIG_INVALID')
    endpoint = urlsplit(config.get('base_url', ''))
    try:
        address = ipaddress.ip_address(endpoint.hostname)
        port = endpoint.port
    except ValueError as error:
        raise HealthError('LITERAL_PRIVATE_UPSTREAM_REQUIRED') from error
    if (endpoint.scheme != 'http' or endpoint.path != '/v1'
            or not address.is_private or address.is_link_local or address.is_multicast
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or port is None or not 1 <= port <= 65535):
        raise HealthError('LITERAL_PRIVATE_UPSTREAM_REQUIRED')
    if config.get('profile') not in ('local', 'upstream'):
        raise HealthError('CONFIG_PROFILE_INVALID')
    if config.get('kind') == 'host-query' and config['profile'] != 'local':
        raise HealthError('HOST_PROBE_MUST_BE_LOCAL')


def checked_generation(document: dict) -> dict:
    try:
        choice = document['choices'][0]
        content = choice['message']['content']
        output = document['usage']['completion_tokens']
        prompt = document['usage']['prompt_tokens']
        valid_usage = (type(output) is int and 0 < output <= MAX_OUTPUT
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


def http_worker(request: dict) -> dict:
    """The caller kills this isolated process at the total wall-clock deadline."""
    config = request['config']
    validate(config)
    key = request['token']
    if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
        raise HealthError('CREDENTIAL_INVALID')
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    start = time.monotonic()
    endpoint = urlsplit(config['base_url'])

    def fetch(path: str, body: bytes | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        try:
            connection.connect()
            connection.sock.settimeout(20 if config['kind'] == 'generation' else 5)
            connection.request('POST' if body else 'GET', path, body, headers)
            response = connection.getresponse()
            data = response.read(MAX_JSON + 1)
            if len(data) > MAX_JSON:
                raise HealthError('RESPONSE_TOO_LARGE')
            return response.status, data
        finally:
            connection.close()

    operation = request.get('operation', 'probe')
    if operation == 'metrics':
        if config['kind'] != 'host-query':
            raise HealthError('METRICS_OPERATION_INVALID')
        status, data = fetch('/metrics')
        metrics = parse_metrics(data.decode()) if status == 200 else {}
        result = {'status': 'HEALTHY', 'metrics': metrics,
                  'metrics_status': 'SUPPORTED' if metrics else 'UNSUPPORTED'}
    elif operation != 'probe':
        raise HealthError('HTTP_OPERATION_INVALID')
    elif config['kind'] == 'generation':
        body = json.dumps({'model': config['model'], 'messages': [
            {'role': 'user', 'content': 'What is 2+2? Reply with the digit only.'}],
            'temperature': 0, 'max_completion_tokens': MAX_OUTPUT, 'stream': False,
            'chat_template_kwargs': {'enable_thinking': False}}).encode()
        status, data = fetch('/v1/chat/completions', body)
        result = ({'status': 'OBSERVER_ERROR', 'reason': 'CREDENTIAL_REJECTED'} if status in (401, 403) else
                  checked_generation(json.loads(data)) if status == 200 else
                  {'status': 'FAILED', 'reason': 'HTTP_ERROR'})
        result['http_status'] = status
    else:
        for suffix in ('/health', '/v1/models'):
            status, data = fetch(suffix)
            if status in (401, 403):
                return {'status': 'OBSERVER_ERROR', 'reason': 'CREDENTIAL_REJECTED', 'http_status': status}
            if status != 200:
                return {'status': 'FAILED', 'reason': 'HTTP_ERROR', 'http_status': status}
            if suffix.endswith('models'):
                models = json.loads(data)['data']
                if not any(isinstance(row, dict) and row.get('id') == config['model'] for row in models):
                    raise HealthError('EXPECTED_MODEL_MISSING')
        result = {'status': 'HEALTHY', 'reason': 'HEALTH_AND_MODEL_VERIFIED', 'http_status': 200}
    result['latency_seconds'] = round(time.monotonic() - start, 3)
    return result


def worker_transport(config: dict, token: str, *, timeout: float, operation: str) -> dict:
    started = time.monotonic()
    try:
        child = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--http-worker'],
            input=json.dumps({'config': config, 'token': token, 'operation': operation}), text=True,
            capture_output=True, timeout=timeout, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    except subprocess.TimeoutExpired:
        return {'status': 'FAILED', 'reason': 'TOTAL_TIMEOUT',
                'latency_seconds': round(time.monotonic() - started, 3)}
    if child.returncode or len(child.stdout) > MAX_JSON:
        return {'status': 'OBSERVER_ERROR', 'reason': 'HTTP_WORKER_FAILED'}
    try:
        value = json.loads(child.stdout)
        if not isinstance(value, dict) or value.get('status') not in ('HEALTHY', 'FAILED', 'OBSERVER_ERROR'):
            raise ValueError()
        return value
    except ValueError:
        return {'status': 'OBSERVER_ERROR', 'reason': 'HTTP_WORKER_INVALID'}


def transport(config: dict, token: str, *, timeout: float = 20) -> dict:
    result = worker_transport(config, token, timeout=timeout, operation='probe')
    if config['kind'] == 'host-query' and result['status'] == 'HEALTHY':
        # Optional telemetry has its own process and deadline. Its failure must
        # never erase an already completed authenticated health/model check.
        metrics = worker_transport(config, token, timeout=min(3, timeout), operation='metrics')
        if metrics['status'] == 'HEALTHY':
            result['metrics'] = metrics.get('metrics', {})
            result['metrics_status'] = metrics.get('metrics_status', 'UNSUPPORTED')
        else:
            result['metrics_status'] = 'OBSERVER_ERROR'
            result['metrics_reason'] = metrics['reason']
    return result


def command(argv: list[str], timeout: int = 5) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    if result.returncode or len(result.stdout) > MAX_JSON:
        raise HealthError('HOST_COMMAND_FAILED')
    return result.stdout.strip()


def host_report(now: float, *, runner=command, proc=Path('/proc')) -> dict:
    started = time.monotonic()
    try:
        boot_id = (proc / 'sys/kernel/random/boot_id').read_text().strip()
        uptime = float((proc / 'uptime').read_text().split()[0])
        service = dict(line.split('=', 1) for line in runner(['systemctl', 'show',
            'pickle-vllm.service', '-p', 'ActiveState', '-p', 'ActiveEnterTimestampMonotonic']).splitlines())
        since = int(service.get('ActiveEnterTimestampMonotonic', '0')) / 1000000
        loading = uptime < 600 or since > 0 and uptime - since < 600
        memory = dict(line.split(':', 1) for line in (proc / 'meminfo').read_text().splitlines())
        available = int(memory['MemAvailable'].split()[0]) * 1024
        total = int(memory['MemTotal'].split()[0]) * 1024
        status = ('LOADING' if loading else 'HEALTHY'
                  if service['ActiveState'] == 'active' else 'FAILED')
        result = {'status': status, 'reason': 'COLD_LOADING_GRACE' if loading else
                  'HOST_SERVICE_ACTIVE' if status == 'HEALTHY' else 'SERVICE_INACTIVE',
                  'boot_id': boot_id, 'host_available_bytes': available,
                  'host_total_bytes': total, 'gpu_memory': 'UNSUPPORTED'}
        try:
            values = runner(['nvidia-smi', '--query-gpu=temperature.gpu,utilization.gpu,memory.total',
                             '--format=csv,noheader,nounits']).split(',')
            temperature, utilization = int(values[0].strip()), int(values[1].strip())
            result['gpu_temperature_c'] = temperature
            result['gpu_utilization_percent'] = utilization
            if len(values) == 3 and values[2].strip().isdigit():
                result['gpu_memory'] = 'SUPPORTED'
                result['gpu_memory_total_mib'] = int(values[2].strip())
            if temperature >= 90:
                result.update(status='FAILED', reason='GPU_TEMPERATURE_HIGH')
        except (HealthError, OSError, ValueError, IndexError, subprocess.SubprocessError):
            result['gpu_query'] = 'OBSERVER_ERROR'
            if result['status'] != 'LOADING':
                result.update(status='OBSERVER_ERROR', reason='GPU_OBSERVATION_UNAVAILABLE')
        return result
    except (HealthError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {'status': 'OBSERVER_ERROR', 'reason': 'HOST_OBSERVATION_UNAVAILABLE'}
    finally:
        if time.monotonic() - started > 15:
            raise HealthError('HOST_DEADLINE_EXCEEDED')


def probe(config: dict, directory: Path, *, now: float | None = None,
          requester=transport, host_reader=host_report) -> dict:
    validate(config)
    now = epoch(time.time() if now is None else now)
    with locked(directory):
        state_path = directory / 'budget.json'
        state = read_json(state_path) if state_path.exists() else {}
        if now < state.get('last_check', 0):
            raise HealthError('CLOCK_MOVED_BACKWARDS')
        state['last_check'] = now
        report = {'version': 1, 'source': config['source'], 'kind': config['kind'], 'observed_at': now}
        if config['kind'] == 'generation':
            period, maximum = (900, 120) if config['profile'] == 'local' else (300, 312)
            day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
            if state.get('day') != day:
                state.update(day=day, attempts=0)
            if now - state.get('last_request', -period) < period or state['attempts'] >= maximum:
                report.update(status='BUDGET_LIMIT', reason='REQUEST_RATE_OR_DAILY_LIMIT')
                atomic_json(state_path, state)
                # A skipped duplicate must not erase the last actual generation result.
                return report
            token = private_read(Path(config['credential_file']), 4096).decode().strip()
            state['attempts'] += 1
            state['last_request'] = now
            atomic_json(state_path, state)
            report.update(requester(config, token))
            if config['profile'] == 'local' and report['status'] == 'FAILED':
                host = host_reader(now)
                report['host'] = host
                if host['status'] == 'LOADING':
                    report.update(status='LOADING', reason='COLD_LOADING_GRACE')
        else:
            host = host_reader(now)
            report['host'] = host
            token = private_read(Path(config['credential_file']), 4096).decode().strip()
            report.update(requester(config, token, timeout=10))
            if report.get('metrics'):
                report.update(counters(state, report['metrics'], host.get('boot_id', 'unknown')))
            if report['status'] == 'FAILED' and host['status'] == 'LOADING':
                report.update(status='LOADING', reason='COLD_LOADING_GRACE')
            atomic_json(state_path, state)
        atomic_json(directory / 'result.json', report)
        return report


def main() -> int:
    if sys.argv[1:] == ['--http-worker']:
        try:
            request = json.loads(sys.stdin.read(MAX_JSON))
            result = http_worker(request)
        except (TimeoutError, ConnectionError):
            result = {'status': 'FAILED', 'reason': 'UPSTREAM_TRANSPORT_FAILED'}
        except (HealthError, OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
            result = {'status': 'FAILED', 'reason': 'UPSTREAM_RESPONSE_INVALID'}
        print(json.dumps(result, allow_nan=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = probe(read_json(args.config), args.state_dir)
        print(json.dumps(report, allow_nan=False, sort_keys=True))
        return 0
    except (HealthError, OSError, ValueError, KeyError, TypeError):
        print('{"status":"OBSERVER_ERROR","reason":"PROBE_CONFIGURATION_OR_STATE_INVALID"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
