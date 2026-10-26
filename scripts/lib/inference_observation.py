"""Typed observation samples, independent of serving and incident state."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from uuid import UUID

from inference_metrics import MetricsError, interval, parse_snapshot


MAX_SAMPLE_BYTES = 512 * 1024
MAX_SEQUENCE = 2**53 - 1
HOST_STATES = frozenset(('OBSERVED', 'NOT_REPORTED', 'UNSUPPORTED', 'SOURCE_FAILED'))
HOST_UNITS = {
    'cpu_load1': 'load',
    'memory_total_bytes': 'bytes',
    'memory_available_bytes': 'bytes',
    'swap_used_bytes': 'bytes',
    'gpu_utilization_percent': 'percent',
    'gpu_temperature_celsius': 'celsius',
    'gpu_memory_total_bytes': 'bytes',
    'gpu_power_watts': 'watts',
    'gpu_power_limit_watts': 'watts',
}
CORE_FIELDS = frozenset(('version', 'sourceId', 'epoch', 'sequence', 'observedAt',
                         'host', 'inference'))


class ObservationError(Exception):
    """A fixed machine reason without source payload or credential values."""


def canonical_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ObservationError('DOCUMENT_INVALID') from None


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ObservationError('IDENTITY_INVALID') from None
    return value


def instant(milliseconds):
    if type(milliseconds) is not int or milliseconds < 0:
        raise ObservationError('OBSERVATION_TIME_INVALID')
    try:
        value = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=milliseconds)
        return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        raise ObservationError('OBSERVATION_TIME_INVALID') from None


def instant_millis(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ObservationError('OBSERVATION_TIME_INVALID')
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00')
        delta = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
        result = delta.days * 86400_000 + delta.seconds * 1000 + delta.microseconds // 1000
        if parsed.microsecond % 1000 or result < 0 or instant(result) != value:
            raise ValueError()
        return result
    except (ValueError, OverflowError, TypeError):
        raise ObservationError('OBSERVATION_TIME_INVALID') from None


def normalize_host(value):
    if (not isinstance(value, dict) or set(value) != {'state', 'metrics'}
            or not isinstance(value['state'], str) or value['state'] not in HOST_STATES or not isinstance(value['metrics'], dict)
            or set(value['metrics']) - set(HOST_UNITS)):
        raise ObservationError('HOST_INVALID')
    metrics = {}
    for name in HOST_UNITS:
        item = value['metrics'].get(name, {'state': 'NOT_REPORTED', 'value': None})
        if (not isinstance(item, dict) or set(item) != {'state', 'value'}
                or not isinstance(item['state'], str) or item['state'] not in HOST_STATES):
            raise ObservationError('HOST_MEASUREMENT_INVALID')
        number = item['value']
        if item['state'] == 'OBSERVED':
            if type(number) not in (int, float) or not 0 <= number <= MAX_SEQUENCE or not math.isfinite(number):
                raise ObservationError('HOST_MEASUREMENT_INVALID')
            if name == 'gpu_utilization_percent' and number > 100:
                raise ObservationError('HOST_MEASUREMENT_INVALID')
        elif number is not None:
            raise ObservationError('HOST_MEASUREMENT_INVALID')
        metrics[name] = {'state': item['state'], 'value': number}
    return {'state': value['state'], 'metrics': metrics}


def validate_sample(value, *, reporter=False):
    expected = CORE_FIELDS | ({'reporterReceivedAt'} if reporter else set())
    if not isinstance(value, dict) or set(value) != expected or type(value.get('version')) is not int or value['version'] != 1:
        raise ObservationError('SAMPLE_INVALID')
    canonical_uuid(value['sourceId'])
    canonical_uuid(value['epoch'])
    if type(value['sequence']) is not int or not 0 <= value['sequence'] <= MAX_SEQUENCE:
        raise ObservationError('SEQUENCE_INVALID')
    milliseconds = instant_millis(value['observedAt'])
    normalize_host(value['host'])
    try:
        interval(None, value['inference'], max_gap_seconds=180)
    except (MetricsError, TypeError, ValueError, KeyError):
        raise ObservationError('INFERENCE_INVALID') from None
    inference = value['inference']
    for engine in inference['engines']:
        for family in ('gauges', 'counters', 'histograms'):
            for item in engine[family]:
                if any('\x00' in label for label in item['labels'].values()):
                    raise ObservationError('INFERENCE_LABEL_UNSUPPORTED')
    if (inference['identity']['sourceId'] != value['sourceId']
            or inference['observedAt'] != milliseconds / 1000):
        raise ObservationError('SAMPLE_IDENTITY_OR_TIME_MISMATCH')
    if reporter:
        instant_millis(value['reporterReceivedAt'])
    if len(canonical_bytes(value)) > MAX_SAMPLE_BYTES:
        raise ObservationError('SAMPLE_TOO_LARGE')
    return value


def make_sample(payload, *, source_id, epoch, sequence, observed_at_millis, host,
                identity, source_state='OBSERVED'):
    canonical_uuid(source_id)
    canonical_uuid(epoch)
    if not isinstance(identity, dict) or set(identity) - {'bootId', 'processId', 'configId'}:
        raise ObservationError('IDENTITY_INVALID')
    observed = instant(observed_at_millis)
    try:
        snapshot = parse_snapshot(payload, observed_at=observed_at_millis / 1000,
                                  identity={'sourceId': source_id, **identity}, source_state=source_state)
    except (MetricsError, TypeError, ValueError, KeyError, ZeroDivisionError):
        raise ObservationError('INFERENCE_INVALID') from None
    value = {'version': 1, 'sourceId': source_id, 'epoch': epoch, 'sequence': sequence,
             'observedAt': observed, 'host': normalize_host(host), 'inference': snapshot}
    validate_sample(value)
    return value


def for_reporter(sample, *, received_at_millis):
    validate_sample(sample)
    value = deepcopy(sample)
    value['reporterReceivedAt'] = instant(received_at_millis)
    return validate_sample(value, reporter=True)
