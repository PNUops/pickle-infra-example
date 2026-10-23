"""Pure, bounded Prometheus snapshots and observed-window inference deltas.

No acquisition, persistence or policy lives here. A caller supplies the source,
boot, process and configuration identities. Missing evidence is never zero.
The metric vocabulary follows the vLLM 0.27.1 inference metrics; unsupported
families retain names only, not arbitrary labels or values.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import math
import re

MAX_PAYLOAD_BYTES = 512 * 1024
MAX_ENGINE_GROUPS = 32
MAX_SERIES = 2048
MAX_BUCKETS = 128
MAX_UNKNOWN_METRICS = 128
MAX_LINES = 16384
MAX_LINE_BYTES = 16384
MAX_LABEL_BYTES = 256
MAX_SAFE_COUNT = 2**53 - 1
SOURCE_STATES = {'OBSERVED', 'UNKNOWN', 'UNSUPPORTED', 'NOT_REPORTED', 'SOURCE_FAILED'}
IDENTITY_KEYS = ('sourceId', 'bootId', 'processId', 'configId')
NAME = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*')
LABEL = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*')
NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')

# Definitions are kind, unit, and the permitted dimensions besides model/engine.
DEFINITIONS = {
    'vllm:num_requests_running': ('gauges', 'requests', frozenset()),
    'vllm:num_requests_waiting': ('gauges', 'requests', frozenset()),
    'vllm:num_requests_waiting_by_reason': ('gauges', 'requests', frozenset({'reason'})),
    'vllm:engine_sleep_state': ('gauges', 'state', frozenset({'sleep_state'})),
    'vllm:kv_cache_usage_perc': ('gauges', 'ratio', frozenset()),
}
for _name in ('prompt_tokens_total', 'generation_tokens_total', 'prompt_tokens_cached_total',
              'prefix_cache_queries_total', 'prefix_cache_hits_total',
              'external_prefix_cache_queries_total', 'external_prefix_cache_hits_total'):
    DEFINITIONS['vllm:' + _name] = ('counters', 'tokens', frozenset())
DEFINITIONS.update({
    'vllm:num_preemptions_total': ('counters', 'preemptions', frozenset()),
    'vllm:mm_cache_queries_total': ('counters', 'items', frozenset()),
    'vllm:mm_cache_hits_total': ('counters', 'items', frozenset()),
    'vllm:request_success_total': ('counters', 'requests', frozenset({'finished_reason'})),
    'vllm:prompt_tokens_by_source_total': ('counters', 'tokens', frozenset({'source'})),
})
for _name in ('time_to_first_token_seconds', 'inter_token_latency_seconds',
              'request_time_per_output_token_seconds', 'e2e_request_latency_seconds',
              'request_queue_time_seconds', 'request_inference_time_seconds',
              'request_prefill_time_seconds', 'request_decode_time_seconds'):
    DEFINITIONS['vllm:' + _name] = ('histograms', 'seconds', frozenset())
for _name in ('request_prompt_tokens', 'request_generation_tokens', 'iteration_tokens_total',
              'request_max_num_generation_tokens', 'request_params_max_tokens',
              'request_prefill_kv_computed_tokens'):
    DEFINITIONS['vllm:' + _name] = ('histograms', 'tokens', frozenset())
DEFINITIONS['vllm:request_params_n'] = ('histograms', 'sequences', frozenset())

SAMPLES = {}
for _name, (_kind, _, _) in DEFINITIONS.items():
    if _kind == 'histograms':
        for _part in ('bucket', 'count', 'sum', 'created'):
            SAMPLES[_name + '_' + _part] = (_name, _part)
    else:
        SAMPLES[_name] = (_name, 'value')
        if _kind == 'counters':
            SAMPLES[_name.removesuffix('_total') + '_created'] = (_name, 'created')


class MetricsError(ValueError):
    """A bounded machine reason, without untrusted response text or values."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _fail(reason):
    raise MetricsError(reason)


def _number(value, *, count=False, maximum=1e300):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        _fail('NUMBER_INVALID')
    text = str(value)
    if len(text) > 128:
        _fail('NUMBER_TOO_LONG')
    if NUMBER.fullmatch(text) is None:
        _fail('NUMBER_INVALID')
    try:
        number = Decimal(text)
    except InvalidOperation:
        _fail('NUMBER_INVALID')
    if not number.is_finite():
        _fail('NUMBER_NOT_FINITE')
    if number < 0 or number > Decimal(str(maximum)):
        _fail('NUMBER_OUT_OF_RANGE')
    if number == number.to_integral_value() and number > MAX_SAFE_COUNT:
        _fail('NUMBER_UNSAFE_INTEGER')
    if count:
        if number != number.to_integral_value() or number > MAX_SAFE_COUNT:
            _fail('COUNT_NOT_SAFE_INTEGER')
        return int(number)
    result = float(number)
    if not math.isfinite(result) or number != 0 and result == 0:
        _fail('NUMBER_NOT_REPRESENTABLE')
    return int(number) if number == number.to_integral_value() and number <= MAX_SAFE_COUNT else result


def _text(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        _fail('IDENTITY_INVALID')
    try:
        length = len(value.encode('utf-8'))
    except UnicodeError:
        _fail('TEXT_ENCODING_INVALID')
    if not value or length > MAX_LABEL_BYTES or any(ord(char) < 32 for char in value):
        _fail('IDENTITY_INVALID')
    return value


def _identity(value):
    if not isinstance(value, dict) or set(value) - set(IDENTITY_KEYS):
        _fail('IDENTITY_INVALID')
    return {key: _text(value.get(key), optional=key != 'sourceId') for key in IDENTITY_KEYS}


def _labels(text):
    result = {}
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos == len(text):
            break
        match = LABEL.match(text, pos)
        if match is None:
            _fail('LABEL_SYNTAX_INVALID')
        name = match[0]
        pos = match.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos == len(text) or text[pos] != '=':
            _fail('LABEL_SYNTAX_INVALID')
        pos += 1
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos == len(text) or text[pos] != '"':
            _fail('LABEL_SYNTAX_INVALID')
        pos += 1
        chars = []
        while pos < len(text) and text[pos] != '"':
            char = text[pos]
            pos += 1
            if char == '\\':
                if pos == len(text) or text[pos] not in ('\\', '"', 'n'):
                    _fail('LABEL_ESCAPE_INVALID')
                char = {'\\': '\\', '"': '"', 'n': '\n'}[text[pos]]
                pos += 1
            chars.append(char)
        if pos == len(text):
            _fail('LABEL_SYNTAX_INVALID')
        value = ''.join(chars)
        if name in result:
            _fail('DUPLICATE_LABEL')
        if len(value.encode('utf-8')) > MAX_LABEL_BYTES or len(result) >= 8:
            _fail('LABEL_LIMIT_EXCEEDED')
        result[name] = value
        pos += 1
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos < len(text):
            if text[pos] != ',':
                _fail('LABEL_SYNTAX_INVALID')
            pos += 1
    return result


def _sample(line):
    match = NAME.match(line)
    if match is None or len(match[0]) > 128:
        _fail('SAMPLE_SYNTAX_INVALID')
    name = match[0]
    pos = match.end()
    labels = {}
    if pos < len(line) and line[pos] == '{':
        begin = pos + 1
        pos += 1
        quoted = escaped = False
        while pos < len(line):
            char = line[pos]
            if escaped:
                escaped = False
            elif char == '\\' and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif char == '}' and not quoted:
                break
            pos += 1
        if pos == len(line):
            _fail('SAMPLE_SYNTAX_INVALID')
        labels = _labels(line[begin:pos])
        pos += 1
    if pos == len(line) or not line[pos].isspace():
        _fail('SAMPLE_SYNTAX_INVALID')
    parts = line[pos:].split()
    if len(parts) not in (1, 2):
        _fail('SAMPLE_SYNTAX_INVALID')
    sample_at = _number(parts[1], maximum=1e15) / 1000 if len(parts) == 2 else None
    return name, labels, parts[0], sample_at


def _histogram(bounds, counts, count, total):
    if (not isinstance(bounds, list) or not isinstance(counts, list)
            or not bounds or len(bounds) > MAX_BUCKETS or len(bounds) != len(counts)
            or bounds[-1] != '+Inf' or count is None or total is None):
        _fail('HISTOGRAM_INCOMPLETE')
    last_bound = -1
    last_count = 0
    for boundary, cumulative in zip(bounds, counts):
        if boundary != '+Inf':
            boundary = _number(boundary)
            if boundary <= last_bound:
                _fail('HISTOGRAM_BOUNDARY_INVALID')
            last_bound = boundary
        elif boundary != bounds[-1] or bounds.count('+Inf') != 1:
            _fail('HISTOGRAM_BOUNDARY_INVALID')
        cumulative = _number(cumulative, count=True)
        if cumulative < last_count:
            _fail('HISTOGRAM_BUCKETS_NOT_MONOTONE')
        last_count = cumulative
    count = _number(count, count=True)
    total = _number(total)
    if counts[-1] != count or count == 0 and total != 0:
        _fail('HISTOGRAM_COUNT_SUM_MISMATCH')
    lower = upper = 0
    previous_bound = previous_count = 0
    unbounded = False
    for boundary, cumulative in zip(bounds, counts):
        observations = cumulative - previous_count
        lower += previous_bound * observations
        if boundary == '+Inf':
            unbounded = observations > 0
        else:
            upper += boundary * observations
            previous_bound = boundary
        previous_count = cumulative
    # Floating sums can differ by rounding; these are necessary feasibility
    # bounds, not a reconstruction of observations within a bucket.
    tolerance = 1e-9 * max(1, lower, upper, total)
    if total < lower - tolerance or not unbounded and total > upper + tolerance:
        _fail('HISTOGRAM_SUM_OUTSIDE_BUCKET_BOUNDS')


def parse_snapshot(payload, *, observed_at, identity, source_state='OBSERVED'):
    """Return JSON-compatible cumulative data; a missing series is not zero.

    Model/engine labels define groups. Other allowlisted labels remain separate
    series, and histogram boundaries use '+Inf' instead of a non-JSON float.
    Reported sample timestamps are milliseconds in the text format and seconds
    in this snapshot. Unknown families expose only their bounded names.
    """
    if not isinstance(source_state, str) or source_state not in SOURCE_STATES:
        _fail('SOURCE_STATE_INVALID')
    if type(observed_at) not in (int, float):
        _fail('OBSERVATION_TIME_INVALID')
    result = {'version': 1, 'observedAt': _number(observed_at, maximum=1e12),
              'sourceState': source_state, 'identity': _identity(identity),
              'engines': [], 'unsupportedMetricNames': [], 'notReportedMetricNames': sorted(DEFINITIONS)}
    if source_state != 'OBSERVED':
        if payload is not None:
            _fail('SOURCE_STATE_VALUE_CONFLICT')
        return result
    if not isinstance(payload, (str, bytes)):
        _fail('PAYLOAD_INVALID')
    try:
        raw = payload.encode('utf-8') if isinstance(payload, str) else payload
        if len(raw) > MAX_PAYLOAD_BYTES:
            _fail('PAYLOAD_TOO_LARGE')
        text = raw.decode('utf-8')
    except UnicodeError:
        _fail('PAYLOAD_ENCODING_INVALID')
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        _fail('LINE_LIMIT_EXCEEDED')
    groups, types, created, unknown, seen = {}, {}, {}, set(), set()
    for line in lines:
        if len(line.encode('utf-8')) > MAX_LINE_BYTES:
            _fail('LINE_LIMIT_EXCEEDED')
        line = line.strip()
        if not line:
            continue
        if line.startswith('#'):
            if re.match(r'#\s*TYPE(?:\s|$)', line):
                declaration = re.fullmatch(r'#\s*TYPE\s+(\S+)\s+(\S+)', line)
                if declaration is None:
                    _fail('TYPE_SYNTAX_INVALID')
                family, declared_type = declaration.groups()
                if NAME.fullmatch(family) is None or len(family) > 128:
                    _fail('TYPE_SYNTAX_INVALID')
                if family in types and types[family] != declared_type:
                    _fail('TYPE_MISMATCH')
                types[family] = declared_type
            continue
        match = NAME.match(line)
        if match is None or len(match[0]) > 128:
            _fail('SAMPLE_SYNTAX_INVALID')
        name = match[0]
        if name not in SAMPLES:
            unknown.add(name)
            if len(unknown) > MAX_UNKNOWN_METRICS:
                _fail('UNKNOWN_METRIC_LIMIT_EXCEEDED')
            continue
        name, labels, raw_value, sample_at = _sample(line)
        family, part = SAMPLES[name]
        kind, unit, extras = DEFINITIONS[family]
        permitted = {'model_name', 'engine'} | extras | ({'le'} if part == 'bucket' else set())
        if (set(labels) - permitted or not labels.get('model_name') or not labels.get('engine')
                or part == 'bucket' and 'le' not in labels):
            _fail('LABELS_UNSUPPORTED')
        _text(labels['model_name'])
        _text(labels['engine'])
        key = (name, tuple(sorted(labels.items())))
        if key in seen:
            _fail('DUPLICATE_SERIES')
        seen.add(key)
        if len(seen) > MAX_SERIES:
            _fail('SERIES_LIMIT_EXCEEDED')
        group_key = (labels['model_name'], labels['engine'])
        if group_key not in groups:
            if len(groups) >= MAX_ENGINE_GROUPS:
                _fail('ENGINE_LIMIT_EXCEEDED')
            groups[group_key] = {'gauges': {}, 'counters': {}, 'histograms': {}}
        other_labels = {key: labels[key] for key in sorted(extras) if key in labels}
        series_key = (family, tuple(other_labels.items()))
        if part == 'created':
            created[(group_key, series_key)] = _number(raw_value, maximum=1e12)
            continue
        collection = groups[group_key][kind]
        if kind != 'histograms':
            value = _number(raw_value, count=kind == 'counters' or unit == 'requests')
            if unit == 'ratio' and value > 1:
                _fail('RATIO_OUT_OF_RANGE')
            if unit == 'state' and value not in (0, 1):
                _fail('STATE_OUT_OF_RANGE')
            collection[series_key] = {'name': family, 'labels': other_labels,
                                      'value': value, 'unit': unit, 'sampleAt': sample_at}
        else:
            item = collection.setdefault(series_key, {'name': family, 'labels': other_labels,
                'unit': unit, '_buckets': {}, '_timestamps': set(), 'count': None, 'sum': None})
            item['_timestamps'].add(sample_at)
            if part == 'bucket':
                bound = '+Inf' if labels['le'] == '+Inf' else _number(labels['le'])
                if bound in item['_buckets']:
                    _fail('DUPLICATE_HISTOGRAM_BOUNDARY')
                item['_buckets'][bound] = _number(raw_value, count=True)
                if len(item['_buckets']) > MAX_BUCKETS:
                    _fail('BUCKET_LIMIT_EXCEEDED')
            else:
                item[part] = _number(raw_value, count=part == 'count')
    for name, _ in seen:
        if SAMPLES[name][1] == 'created' and name in types and types[name] != 'gauge':
            _fail('TYPE_MISMATCH')
    reported = set()
    for group_key, collections in sorted(groups.items()):
        group = {'modelName': group_key[0], 'engineId': group_key[1]}
        present = set()
        for kind, values in collections.items():
            group[kind] = []
            for series_key, item in sorted(values.items()):
                family = series_key[0]
                expected_type = {'gauges': 'gauge', 'counters': 'counter', 'histograms': 'histogram'}[kind]
                if family in types and types[family] != expected_type:
                    _fail('TYPE_MISMATCH')
                item['typeDeclared'] = family in types
                if kind != 'gauges':
                    item['createdAt'] = created.get((group_key, series_key))
                if kind == 'histograms':
                    if len(item['_timestamps']) != 1:
                        _fail('HISTOGRAM_TIMESTAMP_MISMATCH')
                    item['sampleAt'] = item.pop('_timestamps').pop()
                    bucket_map = item.pop('_buckets')
                    item['bounds'] = sorted(b for b in bucket_map if b != '+Inf')
                    if '+Inf' in bucket_map:
                        item['bounds'].append('+Inf')
                    item['counts'] = [bucket_map[b] for b in item['bounds']]
                    _histogram(item['bounds'], item['counts'], item['count'], item['sum'])
                group[kind].append(item)
                present.add(family)
        if present:
            group['notReportedMetricNames'] = sorted(set(DEFINITIONS) - present)
            result['engines'].append(group)
            reported.update(present)
    result['unsupportedMetricNames'] = sorted(unknown)
    result['notReportedMetricNames'] = sorted(set(DEFINITIONS) - reported)
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')) > MAX_PAYLOAD_BYTES:
        _fail('SNAPSHOT_TOO_LARGE')
    return result


def measurement(value=None, *, state='REPORTED'):
    """Normalize a supplied observation without acquiring host or engine data."""
    if not isinstance(state, str) or state not in (SOURCE_STATES - {'OBSERVED'}) | {'REPORTED'}:
        _fail('MEASUREMENT_STATE_INVALID')
    if state == 'REPORTED':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail('NUMBER_INVALID')
        return {'state': state, 'value': _number(value)}
    if value is not None and not (state == 'UNSUPPORTED' and value in ('N/A', '[N/A]')):
        _fail('MEASUREMENT_STATE_VALUE_CONFLICT')
    return {'state': state, 'value': None}


def _validate_snapshot(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value.get('version') != 1
            or not isinstance(value.get('sourceState'), str) or value.get('sourceState') not in SOURCE_STATES
            or set(value) != {'version', 'observedAt', 'sourceState', 'identity', 'engines',
                              'unsupportedMetricNames', 'notReportedMetricNames'}):
        _fail('SNAPSHOT_INVALID')
    _identity(value.get('identity'))
    if type(value.get('observedAt')) not in (int, float):
        _fail('SNAPSHOT_INVALID')
    _number(value.get('observedAt'), maximum=1e12)
    for key, allowed in (('unsupportedMetricNames', None), ('notReportedMetricNames', DEFINITIONS)):
        names = value.get(key)
        if (not isinstance(names, list) or len(names) > (MAX_UNKNOWN_METRICS if allowed is None else len(DEFINITIONS))
                or any(not isinstance(name, str) or len(name) > 128 or NAME.fullmatch(name) is None
                       or allowed is not None and name not in allowed for name in names)
                or len(set(names)) != len(names)):
            _fail('SNAPSHOT_INVALID')
    engines = value.get('engines')
    if not isinstance(engines, list) or len(engines) > MAX_ENGINE_GROUPS:
        _fail('SNAPSHOT_INVALID')
    if value['sourceState'] != 'OBSERVED' and engines:
        _fail('SNAPSHOT_INVALID')
    seen_engines, seen_series = set(), set()
    for group in engines:
        if (not isinstance(group, dict) or set(group) != {'modelName', 'engineId', 'gauges',
                                                        'counters', 'histograms', 'notReportedMetricNames'}):
            _fail('SNAPSHOT_INVALID')
        key = (_text(group.get('modelName')), _text(group.get('engineId')))
        if key in seen_engines:
            _fail('SNAPSHOT_DUPLICATE_ENGINE')
        seen_engines.add(key)
        missing = group.get('notReportedMetricNames')
        if (not isinstance(missing, list) or len(missing) > len(DEFINITIONS)
                or any(not isinstance(name, str) or name not in DEFINITIONS for name in missing)
                or len(set(missing)) != len(missing)):
            _fail('SNAPSHOT_INVALID')
        for kind in ('gauges', 'counters', 'histograms'):
            if not isinstance(group.get(kind), list):
                _fail('SNAPSHOT_INVALID')
            for item in group[kind]:
                keys = {'name', 'labels', 'unit', 'sampleAt', 'typeDeclared'}
                keys |= {'bounds', 'counts', 'count', 'sum', 'createdAt'} if kind == 'histograms' else {'value'}
                if kind == 'counters':
                    keys.add('createdAt')
                if not isinstance(item, dict) or set(item) != keys or type(item.get('typeDeclared')) is not bool:
                    _fail('SNAPSHOT_INVALID')
                name = item.get('name')
                if not isinstance(name, str) or name not in DEFINITIONS or DEFINITIONS[name][0] != kind:
                    _fail('SNAPSHOT_INVALID')
                labels = item.get('labels')
                if not isinstance(labels, dict) or set(labels) - DEFINITIONS[name][2]:
                    _fail('SNAPSHOT_INVALID')
                try:
                    if any(not isinstance(v, str) or len(v.encode('utf-8')) > MAX_LABEL_BYTES for v in labels.values()):
                        _fail('SNAPSHOT_INVALID')
                except UnicodeError:
                    _fail('TEXT_ENCODING_INVALID')
                series_key = (key, name, tuple(sorted(labels.items())))
                if series_key in seen_series or len(seen_series) >= MAX_SERIES:
                    _fail('SNAPSHOT_INVALID')
                seen_series.add(series_key)
                if item.get('unit') != DEFINITIONS[name][1]:
                    _fail('SNAPSHOT_INVALID')
                for stamp in ('sampleAt', 'createdAt'):
                    if item.get(stamp) is not None:
                        if type(item[stamp]) not in (int, float):
                            _fail('SNAPSHOT_INVALID')
                        _number(item[stamp], maximum=1e12)
                if kind == 'histograms':
                    if (not isinstance(item.get('bounds'), list) or not isinstance(item.get('counts'), list)
                            or any(bound != '+Inf' and type(bound) not in (int, float) for bound in item['bounds'])
                            or any(type(count) is not int for count in item['counts'])
                            or type(item.get('count')) is not int or type(item.get('sum')) not in (int, float)):
                        _fail('SNAPSHOT_INVALID')
                    _histogram(item.get('bounds'), item.get('counts'), item.get('count'), item.get('sum'))
                else:
                    if type(item.get('value')) not in (int, float):
                        _fail('SNAPSHOT_INVALID')
                    _number(item.get('value'), count=kind == 'counters' or item['unit'] == 'requests')
                    if item['unit'] == 'ratio' and item['value'] > 1:
                        _fail('RATIO_OUT_OF_RANGE')
                    if item['unit'] == 'state' and item['value'] not in (0, 1):
                        _fail('STATE_OUT_OF_RANGE')


def _index(group, kind):
    return {(item['name'], tuple(sorted(item['labels'].items()))): item for item in group.get(kind, [])}


def _series_elapsed(previous, current, elapsed, maximum):
    old, new = previous.get('sampleAt'), current.get('sampleAt')
    if (old is None) != (new is None):
        return None, 'SAMPLE_TIMESTAMP_CHANGED'
    duration = elapsed if old is None else new - old
    if duration <= 0:
        return None, 'DUPLICATE_TIMESTAMP' if duration == 0 else 'CLOCK_MOVED_BACKWARDS'
    if duration > maximum:
        return None, 'SAMPLE_GAP'
    return duration, None


def _delta(previous, current, kind, elapsed, maximum, reason, window_from, window_to):
    result = {key: deepcopy(current.get(key)) for key in ('name', 'labels', 'unit', 'createdAt')}
    result.update(state='BASELINE', reason=reason, elapsedSeconds=None,
                  fromSampleAt=(previous.get('sampleAt') if previous and previous.get('sampleAt') is not None
                                else window_from),
                  toSampleAt=current.get('sampleAt') if current.get('sampleAt') is not None else window_to)
    if kind == 'counters':
        result.update(delta=None, ratePerSecond=None)
    else:
        result.update(bounds=deepcopy(current.get('bounds')), counts=None, count=None, sum=None, mean=None)
    if reason is None and previous is None:
        reason = 'NEW_SERIES'
    if reason is None and previous.get('createdAt') != current.get('createdAt'):
        reason = 'CREATION_EPOCH_CHANGED'
    if reason is None:
        duration, reason = _series_elapsed(previous, current, elapsed, maximum)
    if reason is None and kind == 'counters':
        difference = current['value'] - previous['value']
        if difference < 0:
            reason = 'COUNTER_DECREASED'
        else:
            rate = difference / duration
            if not math.isfinite(rate):
                reason = 'RATE_NOT_FINITE'
            else:
                result.update(delta=difference, ratePerSecond=rate)
    elif reason is None:
        if current['bounds'] != previous['bounds']:
            reason = 'BUCKET_SCHEMA_CHANGED'
        else:
            counts = [new - old for old, new in zip(previous['counts'], current['counts'])]
            count = current['count'] - previous['count']
            total = current['sum'] - previous['sum']
            try:
                _histogram(current['bounds'], counts, count, total)
            except MetricsError:
                reason = 'HISTOGRAM_DELTA_INVALID'
            else:
                # This is the observation-weighted distribution mean. For TPOT
                # it averages finished-request TPOT, not token-weighted time.
                result.update(counts=counts, count=count, sum=total,
                              mean=total / count if count else None)
    result['reason'] = reason
    if reason is None:
        result.update(state='VALID', elapsedSeconds=duration)
    return result


def interval(previous, current, *, max_gap_seconds):
    """Compare cumulative snapshots only across an observed, matching epoch.

    Gauges remain the current instantaneous observations. Counter rates use
    the actual sample elapsed (or scrape elapsed when no explicit sample time
    exists). Histograms retain interval buckets and sum/count; their mean is
    not a percentile, throughput, capacity or a cross-layer latency measure.
    Missing engine groups downgrade aggregate quality. A baseline also marks
    an observed scrape without counter or histogram interval evidence.
    Returned data never mutates either supplied snapshot.
    """
    _validate_snapshot(current)
    if type(max_gap_seconds) not in (int, float):
        _fail('GAP_LIMIT_INVALID')
    maximum = _number(max_gap_seconds)
    if maximum == 0:
        _fail('GAP_LIMIT_INVALID')
    if previous is not None:
        _validate_snapshot(previous)
    elapsed = None if previous is None else current['observedAt'] - previous['observedAt']
    reason = None
    if current['sourceState'] != 'OBSERVED':
        reason = current['sourceState']
    elif any(current['identity'].get(key) is None for key in IDENTITY_KEYS):
        reason = 'IDENTITY_NOT_REPORTED'
    elif previous is None:
        reason = 'FIRST_SAMPLE'
    elif previous['sourceState'] != 'OBSERVED':
        reason = 'PREVIOUS_NOT_OBSERVED'
    elif any(previous['identity'].get(key) is None for key in IDENTITY_KEYS):
        reason = 'IDENTITY_NOT_REPORTED'
    elif previous['identity'] != current['identity']:
        reason = 'IDENTITY_CHANGED'
    elif elapsed <= 0:
        reason = 'DUPLICATE_TIMESTAMP' if elapsed == 0 else 'CLOCK_MOVED_BACKWARDS'
    elif elapsed > maximum:
        reason = 'SAMPLE_GAP'
    result = {'version': 1, 'identity': deepcopy(current['identity']),
        'fromObservedAt': previous['observedAt'] if previous else None,
        'toObservedAt': current['observedAt'], 'elapsedSeconds': elapsed if elapsed and elapsed > 0 else None,
        'state': 'BASELINE' if reason else 'VALID', 'reason': reason, 'engines': []}
    old_groups = {(g['modelName'], g['engineId']): g for g in previous['engines']} if previous else {}
    states = []
    for group in current['engines']:
        key = (group['modelName'], group['engineId'])
        old_group = old_groups.get(key, {})
        output = {key: deepcopy(group[key]) for key in ('modelName', 'engineId', 'gauges', 'notReportedMetricNames')}
        for kind in ('counters', 'histograms'):
            old_items, new_items = _index(old_group, kind), _index(group, kind)
            output[kind] = []
            for series_key, item in new_items.items():
                item_reason = reason or ('NEW_MODEL_OR_ENGINE' if not old_group else None)
                delta = _delta(old_items.get(series_key), item, kind, elapsed, maximum, item_reason,
                               result['fromObservedAt'], result['toObservedAt'])
                output[kind].append(delta)
                states.append(delta['state'])
            for missing_key in old_items.keys() - new_items.keys():
                missing = _delta(None, old_items[missing_key], kind, elapsed, maximum, 'SERIES_NOT_REPORTED',
                                 None, None)
                missing.update(state='NOT_REPORTED', fromSampleAt=None, toSampleAt=None)
                if kind == 'histograms':
                    missing['bounds'] = None
                output[kind].append(missing)
                states.append('NOT_REPORTED')
        result['engines'].append(output)
    result['missingEngineKeys'] = [{'modelName': key[0], 'engineId': key[1]}
                                   for key in sorted(old_groups.keys() - {(g['modelName'], g['engineId']) for g in current['engines']})]
    if current['sourceState'] != 'OBSERVED':
        result['state'] = 'UNAVAILABLE'
    elif reason is None:
        if not states or result['missingEngineKeys'] or any(state != 'VALID' for state in states):
            result['state'] = 'PARTIAL' if 'VALID' in states else 'BASELINE'
            if result['missingEngineKeys']:
                result['reason'] = 'ENGINE_NOT_REPORTED'
            else:
                result['reason'] = 'SERIES_BASELINE' if states else 'NO_INTERVAL_SERIES'
    return result
