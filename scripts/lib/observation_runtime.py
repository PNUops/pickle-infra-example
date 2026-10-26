"""Independent observation sampling, fixed reading and acknowledged delivery."""

import base64
from copy import deepcopy
import hashlib
import http.client
import json
import math
from pathlib import Path
import re
import socket
import sys
import time
from uuid import uuid4

from inference_observation import (HOST_UNITS, MAX_SAMPLE_BYTES, MAX_SEQUENCE,
                                   ObservationError, canonical_bytes, canonical_uuid,
                                   for_reporter, instant, instant_millis, make_sample,
                                   normalize_host, validate_sample)
from observation_io import (MAX_ACK_BYTES, PROTOCOL, IoError, bounded_process,
                            credential, endpoint, private_bytes, ssh_command)
from observation_spool import ObservationStore, StoreError, decoded


COMMON = frozenset(('version', 'source_id', 'expected_hostname'))
SAMPLER = COMMON | {'metrics_url', 'metrics_credential_file', 'service', 'configuration_id', 'gpu_index'}
REPORTER = COMMON | {'ssh', 'api_url', 'credential_file', 'max_pending_samples',
                     'max_pending_bytes', 'max_exports_per_run'}
MAX_CHECKPOINTS = 64


def clock_millis():
    return time.time_ns() // 1_000_000


def validate_config(value, role, hostname=None):
    fields = {'sample': SAMPLER, 'read': COMMON, 'report': REPORTER}[role]
    if (not isinstance(value, dict) or set(value) != fields or type(value.get('version')) is not int
            or value['version'] != 1 or not isinstance(value['expected_hostname'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,127}', value['expected_hostname'])):
        raise ObservationError('CONFIGURATION_INVALID')
    canonical_uuid(value['source_id'])
    if value['expected_hostname'] != (socket.gethostname() if hostname is None else hostname):
        raise ObservationError('SOURCE_HOST_MISMATCH')
    if role == 'sample':
        endpoint(value['metrics_url'], metrics=True)
        if (not isinstance(value['service'], str)
                or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}\.service', value['service'])
                or not isinstance(value['configuration_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value['configuration_id'])
                or type(value['gpu_index']) is not int or not 0 <= value['gpu_index'] <= 31):
            raise ObservationError('CONFIGURATION_INVALID')
        if value['metrics_credential_file'] is not None:
            protected_path(value['metrics_credential_file'])
    elif role == 'report':
        endpoint(value['api_url'])
        protected_path(value['credential_file'])
        if (type(value['max_pending_samples']) is not int or not 1 <= value['max_pending_samples'] <= 200000
                or type(value['max_pending_bytes']) is not int or not 1 <= value['max_pending_bytes'] <= 16 * 1024**3
                or type(value['max_exports_per_run']) is not int or not 1 <= value['max_exports_per_run'] <= 4):
            raise ObservationError('CONFIGURATION_INVALID')
    return value


def protected_path(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts:
        raise ObservationError('CONFIGURATION_INVALID')


def unavailable_host():
    return {'state': 'SOURCE_FAILED', 'metrics': {
        name: {'state': 'SOURCE_FAILED', 'value': None} for name in HOST_UNITS}}


def read_host(config, now, *, runner=bounded_process, proc=Path('/proc')):
    metrics = {name: {'state': 'NOT_REPORTED', 'value': None} for name in HOST_UNITS}
    identity = {'bootId': None, 'processId': None, 'configId': config['configuration_id']}
    try:
        value = (proc / 'loadavg').read_bytes()
        if len(value) > 1024:
            raise ValueError()
        load = float(value.decode('ascii').split()[0])
        if not math.isfinite(load) or not 0 <= load <= MAX_SEQUENCE:
            raise ValueError()
        metrics['cpu_load1'] = {'state': 'OBSERVED', 'value': load}
    except (OSError, ValueError, UnicodeError, IndexError):
        pass
    try:
        value = (proc / 'sys/kernel/random/boot_id').read_text().strip()
        identity['bootId'] = canonical_uuid(value)
    except (OSError, ObservationError):
        pass
    try:
        data = (proc / 'meminfo').read_bytes()
        if len(data) > 65536:
            raise ValueError()
        fields = {}
        for line in data.decode('ascii').splitlines():
            match = re.fullmatch(r'(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+) kB', line)
            if match:
                fields[match[1]] = int(match[2]) * 1024
        for source, target in [('MemTotal', 'memory_total_bytes'), ('MemAvailable', 'memory_available_bytes')]:
            if source in fields and fields[source] <= MAX_SEQUENCE:
                metrics[target] = {'state': 'OBSERVED', 'value': fields[source]}
        if 'SwapTotal' in fields and 'SwapFree' in fields and 0 <= fields['SwapTotal'] - fields['SwapFree'] <= MAX_SEQUENCE:
            metrics['swap_used_bytes'] = {'state': 'OBSERVED', 'value': fields['SwapTotal'] - fields['SwapFree']}
    except (OSError, ValueError, UnicodeError):
        for name in ('memory_total_bytes', 'memory_available_bytes', 'swap_used_bytes'):
            metrics[name] = {'state': 'SOURCE_FAILED', 'value': None}
    try:
        value = runner(['systemctl', 'show', config['service'], '-p', 'InvocationID',
                        '-p', 'ActiveState', '-p', 'MainPID'], timeout=3, maximum=1024).decode('ascii').strip()
        properties = dict(line.split('=', 1) for line in value.splitlines())
        invocation = properties.get('InvocationID', '')
        pid = properties.get('MainPID', '')
        if (properties.get('ActiveState') == 'active' and re.fullmatch(r'[a-f0-9]{32}', invocation)
                and invocation != '0' * 32 and re.fullmatch(r'[1-9][0-9]{0,9}', pid)):
            with (proc / pid / 'stat').open('rb') as stream:
                process = stream.read(8193)
            if len(process) <= 8192:
                text = process.decode('ascii')
                fields = text[text.rfind(')') + 1:].split()
                if text.startswith(pid + ' (') and len(fields) > 19 and fields[19].isdigit():
                    identity['processId'] = invocation + ':' + pid + ':' + fields[19]
    except (IoError, OSError, UnicodeError, ValueError):
        pass
    gpu_names = ('gpu_utilization_percent', 'gpu_temperature_celsius', 'gpu_memory_total_bytes',
                 'gpu_power_watts', 'gpu_power_limit_watts')
    try:
        value = runner(['nvidia-smi', '--id=' + str(config['gpu_index']),
                        '--query-gpu=utilization.gpu,temperature.gpu,memory.total,power.draw,power.limit',
                        '--format=csv,noheader,nounits'], timeout=3, maximum=4096).decode('ascii').strip()
        if '\n' in value or len(value.split(',')) != 5:
            raise ValueError()
        for name, raw in zip(gpu_names, value.split(',')):
            text = raw.strip()
            if text in ('N/A', '[N/A]', 'Not Supported', '[Not Supported]'):
                metrics[name] = {'state': 'UNSUPPORTED', 'value': None}
                continue
            number = float(text)
            if name == 'gpu_memory_total_bytes':
                number *= 1024**2
            if not math.isfinite(number) or not 0 <= number <= MAX_SEQUENCE:
                raise ValueError()
            metrics[name] = {'state': 'OBSERVED', 'value': int(number) if number.is_integer() else number}
    except (IoError, OSError, ValueError, UnicodeError):
        for name in gpu_names:
            metrics[name] = {'state': 'SOURCE_FAILED', 'value': None}
    host = {'state': 'OBSERVED' if any(x['state'] == 'OBSERVED' for x in metrics.values()) else 'SOURCE_FAILED',
            'metrics': metrics}
    return normalize_host(host), identity


def fetch_metrics_worker(request, *, connection_factory=None):
    if not isinstance(request, dict) or set(request) != {'url', 'token'}:
        raise IoError('METRICS_WORKER_INVALID')
    url = endpoint(request['url'], metrics=True)
    token = request['token']
    if token is not None and (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,4096}', token)):
        raise IoError('CREDENTIAL_INVALID')
    factory = connection_factory or http.client.HTTPConnection
    connection = factory(url.hostname, url.port or 80, timeout=3)
    try:
        connection.connect()
        connection.sock.settimeout(5)
        headers = {} if token is None else {'Authorization': 'Bearer ' + token}
        connection.request('GET', '/metrics', headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise IoError('METRICS_UNAVAILABLE')
        payload = response.read(MAX_SAMPLE_BYTES + 1)
        if len(payload) > MAX_SAMPLE_BYTES:
            raise IoError('METRICS_TOO_LARGE')
        return {'payload': base64.b64encode(payload).decode('ascii')}
    finally:
        connection.close()


def fetch_metrics(config):
    token = credential(config['metrics_credential_file']) if config['metrics_credential_file'] else None
    worker = Path(__file__).resolve().parent.parent / 'inference-observation.py'
    result = decoded(bounded_process([sys.executable, '-I', '-B', str(worker), 'metrics-worker'],
                                    timeout=8, maximum=768 * 1024,
                                    input_bytes=encoded({'url': config['metrics_url'], 'token': token})))
    if set(result) != {'payload'}:
        raise IoError('METRICS_WORKER_INVALID')
    try:
        payload = base64.b64decode(result['payload'], validate=True)
    except (ValueError, TypeError):
        raise IoError('METRICS_WORKER_INVALID') from None
    if len(payload) > MAX_SAMPLE_BYTES:
        raise IoError('METRICS_TOO_LARGE')
    return payload


def producer_state(value, source_id):
    fields = {'version', 'kind', 'sourceId', 'epoch', 'lastSequence', 'lastPublishedUtcMinute'}
    if (not isinstance(value, dict) or set(value) != fields or type(value.get('version')) is not int or value.get('version') != 1
            or value.get('kind') != 'producer' or value.get('sourceId') != source_id
            or type(value.get('lastSequence')) is not int or not 0 <= value['lastSequence'] <= MAX_SEQUENCE
            or type(value.get('lastPublishedUtcMinute')) is not int or value['lastPublishedUtcMinute'] < 0):
        raise ObservationError('PRODUCER_STATE_INVALID')
    canonical_uuid(value['epoch'])
    return value


def sample(config, directory, *, now=None, hostname=None, metrics_reader=fetch_metrics, host_reader=read_host):
    validate_config(config, 'sample', hostname)
    now = clock_millis() if now is None else now
    instant(now)
    minute = now // 60_000
    with ObservationStore(directory).locked() as box:
        state = box.load_control('state.json')
        latest = box.load_control('latest.json')
        if state is None:
            if latest is not None or box.stats()['samples']:
                raise ObservationError('PRODUCER_STATE_LOST')
            state = {'version': 1, 'kind': 'producer', 'sourceId': config['source_id'],
                     'epoch': str(uuid4()), 'lastSequence': 0, 'lastPublishedUtcMinute': minute}
            box.save_control('state.json', state)
            return {'status': 'WAITING', 'reason': 'FIRST_MINUTE_RESERVED'}
        producer_state(state, config['source_id'])
        if latest is not None:
            validate_sample(latest)
            if latest['sourceId'] != config['source_id'] or latest['epoch'] != state['epoch']:
                raise ObservationError('PRODUCER_STATE_INVALID')
        if minute < state['lastPublishedUtcMinute']:
            raise ObservationError('CLOCK_MOVED_BACKWARDS')
        if minute == state['lastPublishedUtcMinute']:
            return {'status': 'WAITING', 'reason': 'MINUTE_ALREADY_RESERVED'}
        if state['lastSequence'] == MAX_SEQUENCE:
            raise ObservationError('SEQUENCE_EXHAUSTED')
        state['lastSequence'] += 1
        state['lastPublishedUtcMinute'] = minute
        box.save_control('state.json', state)
        try:
            host, identity = host_reader(config, now)
        except (IoError, ObservationError, OSError, ValueError, TypeError):
            host = unavailable_host()
            identity = {'bootId': None, 'processId': None, 'configId': config['configuration_id']}
        try:
            payload = metrics_reader(config)
            value = make_sample(payload, source_id=config['source_id'], epoch=state['epoch'],
                                sequence=state['lastSequence'], observed_at_millis=now,
                                host=host, identity=identity)
            for_reporter(value, received_at_millis=now)
            reason = 'SAMPLE_OBSERVED'
        except (IoError, ObservationError, OSError, ValueError, TypeError):
            value = make_sample(None, source_id=config['source_id'], epoch=state['epoch'],
                                sequence=state['lastSequence'], observed_at_millis=now,
                                host=host, identity=identity, source_state='SOURCE_FAILED')
            reason = 'INFERENCE_SOURCE_FAILED'
        box.save_control('latest.json', value)
        result = {'status': 'PERSISTED', 'reason': reason, 'observedAt': value['observedAt'],
                  'epoch': value['epoch'], 'sequence': value['sequence']}
        box.save_control('status.json', result)
        return result


def read_latest(config, directory, *, hostname=None, original_command=None):
    validate_config(config, 'read', hostname)
    if original_command != PROTOCOL:
        raise ObservationError('READER_COMMAND_DENIED')
    with ObservationStore(directory).locked(read_only=True) as box:
        state = box.load_control('state.json')
        latest = box.load_control('latest.json')
        if state is None or latest is None:
            raise ObservationError('SAMPLE_NOT_READY')
        producer_state(state, config['source_id'])
        validate_sample(latest)
        if (latest['sourceId'] != config['source_id'] or latest['epoch'] != state['epoch']
                or latest['sequence'] > state['lastSequence']):
            raise ObservationError('PRODUCER_STATE_INVALID')
        return canonical_bytes(latest)


def reporter_state(value, source_id):
    if (not isinstance(value, dict) or set(value) != {'version', 'kind', 'sourceId', 'checkpoints', 'blocked'}
            or type(value.get('version')) is not int or value.get('version') != 1 or value.get('kind') != 'reporter'
            or value.get('sourceId') != source_id or not isinstance(value['checkpoints'], dict)
            or len(value['checkpoints']) > MAX_CHECKPOINTS or not isinstance(value['blocked'], dict)
            or len(value['blocked']) > 64):
        raise ObservationError('REPORTER_STATE_INVALID')
    for epoch, entry in value['checkpoints'].items():
        canonical_uuid(epoch)
        if (not isinstance(entry, dict) or set(entry) != {'sequence', 'bodySha256', 'producerSha256', 'receivedAt', 'observedAt'}
                or type(entry['sequence']) is not int or not 0 <= entry['sequence'] <= MAX_SEQUENCE
                or not isinstance(entry['bodySha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', entry['bodySha256'])
                or not isinstance(entry['producerSha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', entry['producerSha256'])):
            raise ObservationError('REPORTER_STATE_INVALID')
        ack_instant(entry['receivedAt'])
        instant_millis(entry['observedAt'])
    for key, entry in value['blocked'].items():
        if (not isinstance(key, str) or not re.fullmatch(r'[a-f0-9-]{36}:[0-9]{1,16}', key)
                or not isinstance(entry, dict) or set(entry) != {'bodySha256', 'reason'}
                or not isinstance(entry['bodySha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', entry['bodySha256'])
                or entry['reason'] not in ('CONTENT_CONFLICT', 'REPLAY_REJECTED')):
            raise ObservationError('REPORTER_STATE_INVALID')
        epoch, sequence = key.split(':')
        canonical_uuid(epoch)
        if int(sequence) > MAX_SEQUENCE:
            raise ObservationError('REPORTER_STATE_INVALID')
    return value


def ack_instant(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z', value):
        raise ObservationError('ACK_INVALID')
    from datetime import datetime
    try:
        datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:
        raise ObservationError('ACK_INVALID') from None


def ack_millis(value):
    ack_instant(value)
    from datetime import datetime, timezone
    delta = datetime.fromisoformat(value[:-1] + '+00:00') - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def producer_part(document):
    return {key: value for key, value in document.items() if key != 'reporterReceivedAt'}


def verify_ack(value, record, source_id):
    if not isinstance(value, dict) or set(value) != {'sourceId', 'epoch', 'sequence', 'status', 'contentSha256', 'receivedAt'}:
        raise ObservationError('ACK_INVALID')
    if (value['sourceId'] != source_id or value['epoch'] != record.epoch
            or type(value['sequence']) is not int or value['sequence'] != record.sequence
            or value['status'] not in ('ACCEPTED', 'DUPLICATE')
            or value['contentSha256'] != record.sha256):
        raise ObservationError('ACK_MISMATCH')
    ack_instant(value['receivedAt'])
    return value


def send_api(config, token, body):
    worker = Path(__file__).resolve().parent.parent / 'inference-observation.py'
    value = {'url': config['api_url'], 'token': token, 'body': base64.b64encode(body).decode('ascii')}
    return decoded(bounded_process([sys.executable, '-I', '-B', str(worker), 'http-worker'],
                                  timeout=10, maximum=MAX_ACK_BYTES, input_bytes=encoded(value)))


def report(config, directory, *, now=None, hostname=None, source_reader=None,
           sender=send_api, token_reader=credential, boundary=lambda _: None):
    validate_config(config, 'report', hostname)
    now = clock_millis() if now is None else now
    instant(now)
    token = token_reader(config['credential_file'])
    source_reader = source_reader or (lambda: bounded_process(ssh_command(config['ssh']), timeout=15, maximum=MAX_SAMPLE_BYTES))
    with ObservationStore(directory, max_samples=config['max_pending_samples'],
                          max_bytes=config['max_pending_bytes']).locked() as box:
        state = box.load_control('state.json')
        if state is None:
            if box.stats()['samples']:
                raise ObservationError('REPORTER_STATE_LOST')
            state = {'version': 1, 'kind': 'reporter', 'sourceId': config['source_id'], 'checkpoints': {}, 'blocked': {}}
            box.save_control('state.json', state)
        reporter_state(state, config['source_id'])
        delivered = imported = attempts = 0
        reason = None

        def drain():
            nonlocal delivered, attempts, reason
            excluded = {(key.split(':')[0], int(key.split(':')[1])) for key in state['blocked']}
            for record in box.pending(config['max_exports_per_run'], exclude_keys=excluded):
                if attempts >= config['max_exports_per_run'] or reason is not None:
                    return
                validate_sample(record.document, reporter=True)
                if (record.document['sourceId'] != config['source_id'] or record.document['epoch'] != record.epoch
                        or record.document['sequence'] != record.sequence or canonical_bytes(record.document) != record.payload):
                    raise ObservationError('OUTBOX_RECORD_INVALID')
                key = record.epoch + ':' + str(record.sequence)
                if key in state['blocked']:
                    if state['blocked'][key]['bodySha256'] != record.sha256:
                        raise ObservationError('QUARANTINED_CONTENT_CHANGED')
                    continue
                attempts += 1
                try:
                    response = sender(config, token, record.payload)
                    if not isinstance(response, dict) or response.get('status') != 200 or set(response) != {'status', 'ack'}:
                        code = response.get('status') if isinstance(response, dict) else None
                        if code in (409, 422):
                            if len(state['blocked']) >= 64:
                                raise ObservationError('QUARANTINE_LIMIT')
                            state['blocked'][key] = {'bodySha256': record.sha256,
                                                     'reason': 'CONTENT_CONFLICT' if code == 409 else 'REPLAY_REJECTED'}
                            box.save_control('state.json', state)
                        raise ObservationError('CONTENT_CONFLICT' if code == 409 else
                                               'REPLAY_REJECTED' if code == 422 else 'DELIVERY_FAILED')
                    ack = verify_ack(response['ack'], record, config['source_id'])
                    boundary('after_ack')
                    previous = state['checkpoints'].get(record.epoch)
                    if previous is None and len(state['checkpoints']) >= MAX_CHECKPOINTS:
                        raise ObservationError('CHECKPOINT_LIMIT')
                    if previous is None or record.sequence >= previous['sequence']:
                        if previous and record.sequence == previous['sequence'] and record.sha256 != previous['bodySha256']:
                            raise ObservationError('CHECKPOINT_CONFLICT')
                        state['checkpoints'][record.epoch] = {
                            'sequence': record.sequence, 'bodySha256': record.sha256,
                            'producerSha256': hashlib.sha256(canonical_bytes(producer_part(record.document))).hexdigest(),
                            'receivedAt': ack['receivedAt'], 'observedAt': record.document['observedAt']}
                    box.save_control('state.json', state)
                    boundary('after_checkpoint')
                    box.ack(record)
                    delivered += 1
                except (ObservationError, IoError, StoreError, OSError) as error:
                    reason = str(error) if isinstance(error, (ObservationError, IoError, StoreError)) else 'DELIVERY_FAILED'
                    return

        drain()
        latest = None
        try:
            payload = source_reader()
            if not isinstance(payload, bytes) or len(payload) > MAX_SAMPLE_BYTES:
                raise ObservationError('REMOTE_SAMPLE_INVALID')
            candidate = decoded(payload)
            validate_sample(candidate)
            if candidate['sourceId'] != config['source_id']:
                raise ObservationError('REMOTE_SOURCE_MISMATCH')
            latest = candidate
            core_hash = hashlib.sha256(canonical_bytes(latest)).hexdigest()
            previous = state['checkpoints'].get(latest['epoch'])
            if previous and latest['sequence'] <= previous['sequence']:
                if latest['sequence'] != previous['sequence'] or core_hash != previous['producerSha256']:
                    raise ObservationError('SOURCE_SEQUENCE_ROLLBACK_OR_CONFLICT')
            else:
                existing = box.get(latest['epoch'], latest['sequence'])
                if existing:
                    if producer_part(existing.document) != latest:
                        raise ObservationError('CONTENT_CONFLICT')
                else:
                    request = for_reporter(latest, received_at_millis=now)
                    box.put(latest['epoch'], latest['sequence'], request)
                    imported += 1
        except (ObservationError, StoreError, IoError, OSError, ValueError, TypeError) as error:
            if reason is None:
                reason = str(error) if isinstance(error, (ObservationError, StoreError, IoError)) else 'ACQUISITION_FAILED'
        drain()
        pending = box.stats()
        if reason is None and state['blocked']:
            reason = 'SAMPLES_QUARANTINED'
        observed = latest['observedAt'] if latest else None
        acknowledged = max(state['checkpoints'].values(), key=lambda item: ack_millis(item['receivedAt']), default=None)
        acknowledged_observed = acknowledged['observedAt'] if acknowledged else None
        lag = ((ack_millis(acknowledged['receivedAt']) - instant_millis(acknowledged_observed)) / 1000
               if acknowledged and ack_millis(acknowledged['receivedAt']) >= instant_millis(acknowledged_observed) else None)
        result = {'status': 'OK' if reason is None else 'OBSERVATION_ERROR', 'reason': reason,
                  'imported': imported, 'delivered': delivered, 'pending': pending['samples'],
                  'pendingBytes': pending['bytes'], 'latestSourceObservedAt': observed,
                  'quarantined': len(state['blocked']),
                  'latestAcknowledgedObservedAt': acknowledged_observed,
                  'acknowledgedAgeSeconds': ((now - instant_millis(acknowledged_observed)) / 1000
                                            if acknowledged and now >= instant_millis(acknowledged_observed) else None),
                  'deliveryLagSeconds': lag}
        box.save_control('status.json', result)
        return result
