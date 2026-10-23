#!/usr/bin/env python3
"""Offline contracts for inference identity, cumulative metrics and missing evidence."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import inference_metrics as metrics

IDENTITY = {'sourceId': '00000000-0000-0000-0000-000000000001',
            'bootId': '00000000-0000-0000-0000-000000000002',
            'processId': 'process-example', 'configId': 'a' * 64}
RUNNING = 'vllm:num_requests_running'
TOKENS = 'vllm:generation_tokens_total'
HIST = 'vllm:time_to_first_token_seconds'


def exposition(*, engine='0', model='example-model', tokens=100, created=900,
               buckets=(1, 2, 3), count=3, total=1.5, running=0, bounds=('0.1', '1', '+Inf')):
    labels = f'engine="{engine}",model_name="{model}"'
    lines = [f'# TYPE {RUNNING} gauge', f'{RUNNING}{{{labels}}} {running}',
             f'# TYPE {TOKENS} counter', f'{TOKENS}{{{labels}}} {tokens}',
             '# TYPE vllm:generation_tokens_created gauge',
             f'vllm:generation_tokens_created{{{labels}}} {created}',
             f'# TYPE {HIST} histogram']
    lines += [f'{HIST}_bucket{{{labels},le="{bound}"}} {value}'
              for bound, value in zip(bounds, buckets)]
    lines += [f'{HIST}_count{{{labels}}} {count}', f'{HIST}_sum{{{labels}}} {total}',
              f'{HIST}_created{{{labels}}} {created}']
    return '\n'.join(lines) + '\n'


def snapshot(text=None, *, at=1000, identity=None, **kwargs):
    return metrics.parse_snapshot(exposition() if text is None else text,
                                  observed_at=at, identity=identity or IDENTITY, **kwargs)


def series(document, collection, name, *, engine='0'):
    groups = [g for g in document['engines'] if g['engineId'] == engine]
    assert len(groups) == 1, 'The fixture must preserve its engine group.'
    return next(v for v in groups[0][collection] if v['name'] == name)


class ParseTests(unittest.TestCase):
    def test_zero_and_creation_epochs_are_reported_without_model_engine_merging(self):
        text = exposition() + exposition(engine='1', tokens=200, running=2)
        value = snapshot(text)
        self.assertEqual(len(value['engines']), 2)
        self.assertEqual(series(value, 'gauges', RUNNING)['value'], 0)
        self.assertEqual(series(value, 'counters', TOKENS)['value'], 100)
        self.assertEqual(series(value, 'counters', TOKENS, engine='1')['value'], 200)
        self.assertEqual(series(value, 'counters', TOKENS)['createdAt'], 900)
        histogram = series(value, 'histograms', HIST)
        self.assertEqual(histogram['bounds'], [0.1, 1, '+Inf'])
        self.assertEqual(histogram['counts'], [1, 2, 3])
        self.assertEqual(histogram['createdAt'], 900)
        json.dumps(value, allow_nan=False)

    def test_unknown_metrics_keep_names_without_publishing_their_values_or_labels(self):
        text = exposition() + 'vllm:future_counter_total{private_label="hidden-example"} NaN\n'
        value = snapshot(text)
        self.assertIn('vllm:future_counter_total', value['unsupportedMetricNames'])
        self.assertNotIn('hidden-example', json.dumps(value))
        self.assertNotIn('private_label', json.dumps(value))

    def test_allowed_extra_dimensions_and_prometheus_escapes_remain_distinct(self):
        name = 'vllm:num_requests_waiting_by_reason'
        text = (f'# TYPE {name} gauge\n'
                + name + r'{engine="0",model_name="example-\"model\\path",reason="capacity"} 0' + '\n'
                + name + r'{engine="0",model_name="example-\"model\\path",reason="scheduler"} 2' + '\n')
        value = snapshot(text)
        self.assertEqual(len(value['engines']), 1)
        group = value['engines'][0]
        self.assertEqual(group['modelName'], 'example-"model\\path')
        self.assertEqual({v['labels']['reason'] for v in group['gauges']}, {'capacity', 'scheduler'})

    def test_selected_series_reject_duplicates_even_when_label_order_changes(self):
        duplicate = f'{RUNNING}{{model_name="example-model",engine="0"}} 0\n'
        with self.assertRaises(metrics.MetricsError):
            snapshot(exposition() + duplicate)

    def test_known_values_reject_negative_nonfinite_and_unsafe_counts(self):
        for value in ('-1', 'NaN', '+Inf', '1e1000', '9007199254740993', '1e-1000'):
            with self.subTest(value=value), self.assertRaises(metrics.MetricsError):
                snapshot(exposition(tokens=value))

    def test_histograms_require_consistent_finite_monotone_cumulative_counts(self):
        cases = [dict(buckets=(2, 1, 3)), dict(buckets=(1, 2, 4)), dict(count=3.5),
                 dict(total=-1), dict(count=0, total=1, buckets=(0, 0, 0)),
                 dict(bounds=('0.1', '0.10', '+Inf')), dict(bounds=('0.1', '1', 'NaN'))]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(metrics.MetricsError):
                snapshot(exposition(**changes))
        with self.assertRaises(metrics.MetricsError):
            snapshot(exposition().replace(f'{HIST}_count', 'unknown_count'))

    def test_declared_type_and_unknown_label_cannot_change_known_series_semantics(self):
        for text in [exposition().replace(f'# TYPE {TOKENS} counter', f'# TYPE {TOKENS} gauge'),
                     exposition().replace('engine="0"', 'untrusted="0"')]:
            with self.subTest(text=text[:30]), self.assertRaises(metrics.MetricsError):
                snapshot(text)

    def test_bounded_payload_and_engine_cardinality_are_enforced(self):
        with self.assertRaises(metrics.MetricsError):
            snapshot('#' + 'x' * metrics.MAX_PAYLOAD_BYTES)
        lines = [f'{RUNNING}{{engine="{i}",model_name="example-model"}} 0'
                 for i in range(metrics.MAX_ENGINE_GROUPS + 1)]
        with self.assertRaises(metrics.MetricsError):
            snapshot('\n'.join(lines))

    def test_sample_timestamps_are_preserved_in_seconds(self):
        text = f'{TOKENS}{{engine="0",model_name="example-model"}} 100 1000000\n'
        value = snapshot(text)
        self.assertEqual(len(value['engines']), 1)
        self.assertEqual(series(value, 'counters', TOKENS)['sampleAt'], 1000)

    def test_unknown_unsupported_unreported_and_source_failure_are_not_reported_zero(self):
        for state in ('UNKNOWN', 'UNSUPPORTED', 'NOT_REPORTED', 'SOURCE_FAILED'):
            with self.subTest(state=state):
                value = metrics.parse_snapshot(None, observed_at=1000, identity=IDENTITY,
                                               source_state=state)
                self.assertEqual(value['sourceState'], state)
                self.assertEqual(value['engines'], [])
        self.assertEqual(metrics.measurement(None, state='UNSUPPORTED'),
                         {'state': 'UNSUPPORTED', 'value': None})
        self.assertEqual(metrics.measurement(0), {'state': 'REPORTED', 'value': 0})
        for value in (-1, float('nan'), float('inf'), True):
            with self.subTest(value=value), self.assertRaises(metrics.MetricsError):
                metrics.measurement(value)

    def test_numeric_grammar_and_general_measurements_reject_lossy_or_non_numeric_values(self):
        for value in ('1_000', '1__0', '0x10'):
            with self.subTest(value=value), self.assertRaises(metrics.MetricsError):
                snapshot(exposition(tokens=value))
        for value in (9007199254740993, '10'):
            with self.subTest(value=value), self.assertRaises(metrics.MetricsError):
                metrics.measurement(value)

    def test_histogram_sum_must_be_feasible_under_its_bucket_counts(self):
        with self.assertRaises(metrics.MetricsError):
            snapshot(exposition(buckets=(1, 1, 1), count=1, total=2))

    def test_created_type_and_duplicate_labels_are_rejected_without_echoing_values(self):
        examples = [exposition().replace('# TYPE vllm:generation_tokens_created gauge',
                                        '# TYPE vllm:generation_tokens_created counter'),
                    f'{RUNNING}{{engine="0",engine="1",model_name="example-model"}} 0\n',
                    f'{RUNNING}{{engine="0",model_name="bad\\tlabel"}} 0\n']
        for text in examples:
            with self.subTest(text=text[:30]), self.assertRaises(metrics.MetricsError) as caught:
                snapshot(text)
            self.assertRegex(str(caught.exception), r'^[A-Z_]+$')

    def test_truncated_histogram_and_mixed_timestamps_cannot_become_partial_zero(self):
        text = exposition().replace(f'{HIST}_bucket{{engine="0",model_name="example-model",le="+Inf"}} 3\n', '')
        with self.assertRaises(metrics.MetricsError):
            snapshot(text)
        text = exposition().replace(f'{HIST}_sum{{engine="0",model_name="example-model"}} 1.5',
                                    f'{HIST}_sum{{engine="0",model_name="example-model"}} 1.5 1000000')
        with self.assertRaises(metrics.MetricsError):
            snapshot(text)

    def test_metric_units_keep_cache_items_preemptions_and_tokens_separate(self):
        text = ''
        for name in ('vllm:mm_cache_queries_total', 'vllm:num_preemptions_total', TOKENS):
            text += f'{name}{{engine="0",model_name="example-model"}} 1\n'
        value = snapshot(text)
        self.assertEqual(series(value, 'counters', 'vllm:mm_cache_queries_total')['unit'], 'items')
        self.assertEqual(series(value, 'counters', 'vllm:num_preemptions_total')['unit'], 'preemptions')
        self.assertEqual(series(value, 'counters', TOKENS)['unit'], 'tokens')

    def test_bucket_series_and_label_bytes_have_independent_bounds(self):
        labels = f'{RUNNING}{{engine="0",model_name="' + 'x' * 257 + '"} 0\n'
        with self.assertRaises(metrics.MetricsError):
            snapshot(labels)
        lines = [f'vllm:num_requests_waiting_by_reason{{engine="0",model_name="example-model",reason="r{i}"}} 0'
                 for i in range(metrics.MAX_SERIES + 1)]
        with self.assertRaises(metrics.MetricsError):
            snapshot('\n'.join(lines))
        count = metrics.MAX_BUCKETS + 1
        text = exposition(bounds=tuple(str(i) for i in range(count - 1)) + ('+Inf',),
                          buckets=tuple(0 for _ in range(count)), count=0, total=0)
        with self.assertRaises(metrics.MetricsError):
            snapshot(text)

    def test_invalid_utf8_and_unreported_identity_cannot_escape_as_runtime_errors(self):
        with self.assertRaises(metrics.MetricsError):
            snapshot(b'\xff')
        broken = dict(IDENTITY, sourceId='\ud800')
        with self.assertRaises(metrics.MetricsError):
            snapshot(identity=broken)
        with self.assertRaises(metrics.MetricsError):
            snapshot(at='1000')

    def test_newline_escape_is_decoded_for_a_permitted_secondary_dimension(self):
        name = 'vllm:num_requests_waiting_by_reason'
        text = name + r'{engine="0",model_name="example-model",reason="future\nreason"} 0' + '\n'
        value = snapshot(text)
        self.assertEqual(series(value, 'gauges', name)['labels']['reason'], 'future\nreason')

    def test_type_metadata_whitespace_does_not_bypass_mismatch_detection(self):
        text = exposition().replace(f'# TYPE {TOKENS} counter', f'#\tTYPE\t{TOKENS}\tgauge')
        with self.assertRaises(metrics.MetricsError):
            snapshot(text)

    def test_equal_engine_labels_do_not_merge_different_models(self):
        value = snapshot(exposition(model='example-model-a', tokens=100)
                         + exposition(model='example-model-b', tokens=200))
        self.assertEqual(len(value['engines']), 2)
        values = {group['modelName']: group['counters'][0]['value'] for group in value['engines']}
        self.assertEqual(values, {'example-model-a': 100, 'example-model-b': 200})


class IntervalTests(unittest.TestCase):
    def setUp(self):
        self.previous = snapshot()
        self.current = snapshot(exposition(tokens=160, buckets=(2, 4, 6), count=6, total=3), at=1060)

    def test_real_elapsed_rates_and_histogram_distribution_mean_are_not_lifetime_values(self):
        value = metrics.interval(self.previous, self.current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'VALID')
        self.assertEqual(value['elapsedSeconds'], 60)
        counter = series(value, 'counters', TOKENS)
        self.assertEqual(counter['delta'], 60)
        self.assertEqual(counter['ratePerSecond'], 1)
        histogram = series(value, 'histograms', HIST)
        self.assertEqual(histogram['counts'], [1, 2, 3])
        self.assertEqual(histogram['count'], 3)
        self.assertEqual(histogram['sum'], 1.5)
        self.assertEqual(histogram['mean'], 0.5)

    def test_first_missing_identity_and_changed_envelope_require_baselines(self):
        self.assertEqual(metrics.interval(None, self.current, max_gap_seconds=120)['state'], 'BASELINE')
        for key in IDENTITY:
            changed = deepcopy(self.current)
            changed['identity'][key] += '-changed'
            value = metrics.interval(self.previous, changed, max_gap_seconds=120)
            self.assertEqual(value['state'], 'BASELINE')
            self.assertIsNone(series(value, 'counters', TOKENS)['ratePerSecond'])
        missing = deepcopy(self.current)
        missing['identity']['processId'] = None
        value = metrics.interval(self.previous, missing, max_gap_seconds=120)
        self.assertEqual(value['reason'], 'IDENTITY_NOT_REPORTED')

    def test_created_epoch_change_catches_a_restart_after_counters_pass_previous_totals(self):
        current = snapshot(exposition(tokens=500, created=1020, buckets=(3, 6, 9), count=9, total=5), at=1060)
        value = metrics.interval(self.previous, current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'BASELINE')
        for collection, name in [('counters', TOKENS), ('histograms', HIST)]:
            item = series(value, collection, name)
            self.assertEqual(item['reason'], 'CREATION_EPOCH_CHANGED')
            self.assertIsNone(item.get('delta', item.get('count')))

    def test_counter_decrease_and_creation_epoch_disappearance_are_not_negative_progress(self):
        for text in [exposition(tokens=1), exposition().replace(
                'vllm:generation_tokens_created{engine="0",model_name="example-model"} 900\n', '')]:
            value = metrics.interval(self.previous, snapshot(text, at=1060), max_gap_seconds=120)
            item = series(value, 'counters', TOKENS)
            self.assertEqual(item['state'], 'BASELINE')
            self.assertIsNone(item['delta'])
            self.assertIsNone(item['ratePerSecond'])

    def test_gap_duplicate_and_clock_reversal_do_not_produce_rates(self):
        for at, reason in [(1000, 'DUPLICATE_TIMESTAMP'), (999, 'CLOCK_MOVED_BACKWARDS'),
                           (1300, 'SAMPLE_GAP')]:
            value = metrics.interval(self.previous, snapshot(at=at), max_gap_seconds=120)
            self.assertEqual(value['reason'], reason)
            self.assertEqual(value['state'], 'BASELINE')
            self.assertIsNone(series(value, 'counters', TOKENS)['ratePerSecond'])

    def test_histogram_bucket_schema_change_and_impossible_interval_distribution_need_baseline(self):
        changed = snapshot(exposition(tokens=160, bounds=('0.2', '1', '+Inf')), at=1060)
        value = metrics.interval(self.previous, changed, max_gap_seconds=120)
        self.assertEqual(series(value, 'histograms', HIST)['reason'], 'BUCKET_SCHEMA_CHANGED')
        impossible = snapshot(exposition(tokens=160, buckets=(3, 3, 4), count=4, total=2), at=1060)
        value = metrics.interval(self.previous, impossible, max_gap_seconds=120)
        self.assertEqual(series(value, 'histograms', HIST)['state'], 'BASELINE')

    def test_zero_interval_count_has_no_mean_while_positive_zero_duration_has_zero_mean(self):
        value = metrics.interval(self.previous, snapshot(at=1060), max_gap_seconds=120)
        self.assertEqual(value['state'], 'VALID')
        self.assertEqual(series(value, 'histograms', HIST)['count'], 0)
        self.assertIsNone(series(value, 'histograms', HIST)['mean'])
        first = snapshot(exposition(buckets=(0, 0, 0), count=0, total=0))
        second = snapshot(exposition(buckets=(1, 1, 1), count=1, total=0), at=1060)
        value = metrics.interval(first, second, max_gap_seconds=120)
        self.assertEqual(series(value, 'histograms', HIST)['mean'], 0)

    def test_missing_series_is_unreported_and_model_change_is_not_combined(self):
        current = snapshot(f'{RUNNING}{{engine="0",model_name="example-model"}} 0\n', at=1060)
        value = metrics.interval(self.previous, current, max_gap_seconds=120)
        self.assertEqual(series(value, 'counters', TOKENS)['state'], 'NOT_REPORTED')
        self.assertIsNone(series(value, 'counters', TOKENS)['delta'])
        changed = snapshot(exposition(model='another-example-model'), at=1060)
        value = metrics.interval(self.previous, changed, max_gap_seconds=120)
        self.assertEqual(value['state'], 'BASELINE')

    def test_metric_sample_timestamps_define_the_counter_elapsed_when_reported(self):
        first = snapshot(f'{TOKENS}{{engine="0",model_name="example-model"}} 100 1000000\n')
        second = snapshot(f'{TOKENS}{{engine="0",model_name="example-model"}} 160 1030000\n', at=1060)
        value = metrics.interval(first, second, max_gap_seconds=120)
        item = series(value, 'counters', TOKENS)
        self.assertEqual(item['elapsedSeconds'], 30)
        self.assertEqual(item['ratePerSecond'], 2)
        self.assertEqual(item['fromSampleAt'], 1000)
        self.assertEqual(item['toSampleAt'], 1030)

    def test_failure_breaks_continuity_without_replacing_missing_values_with_zero(self):
        failed = metrics.parse_snapshot(None, observed_at=1030, identity=IDENTITY,
                                        source_state='SOURCE_FAILED')
        self.assertEqual(metrics.interval(self.previous, failed, max_gap_seconds=120)['state'], 'UNAVAILABLE')
        value = metrics.interval(failed, self.current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'BASELINE')
        self.assertIsNone(series(value, 'counters', TOKENS)['ratePerSecond'])

    def test_restored_snapshot_cannot_inject_extra_fields_or_numeric_strings(self):
        for collection, field, injected in [('gauges', 'extra', 'hidden-example'),
                                            ('counters', 'value', '160'),
                                            ('histograms', 'count', '6')]:
            current = deepcopy(self.current)
            item = current['engines'][0][collection][0]
            item[field] = injected
            with self.subTest(collection=collection), self.assertRaises(metrics.MetricsError):
                metrics.interval(self.previous, current, max_gap_seconds=120)

    def test_json_roundtrip_and_interval_do_not_mutate_either_cumulative_snapshot(self):
        previous = json.loads(json.dumps(self.previous, allow_nan=False))
        current = json.loads(json.dumps(self.current, allow_nan=False))
        before = deepcopy((previous, current))
        value = metrics.interval(previous, current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'VALID')
        self.assertEqual((previous, current), before)
        json.dumps(value, allow_nan=False)

    def test_creation_epoch_only_resets_its_matching_secondary_label_series(self):
        family = 'vllm:request_success_total'
        def document(*, changed=False):
            lines = [f'# TYPE {family} counter']
            for reason in ('stop', 'length'):
                labels = f'engine="0",model_name="example-model",finished_reason="{reason}"'
                value = (500 if reason == 'stop' else 160) if changed else 100
                epoch = 1020 if changed and reason == 'stop' else 900
                lines += [f'{family}{{{labels}}} {value}', f'vllm:request_success_created{{{labels}}} {epoch}']
            return '\n'.join(lines)
        previous = snapshot(document())
        current = snapshot(document(changed=True), at=1060)
        value = metrics.interval(previous, current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'PARTIAL')
        items = {item['labels']['finished_reason']: item for item in value['engines'][0]['counters']}
        self.assertEqual(items['stop']['reason'], 'CREATION_EPOCH_CHANGED')
        self.assertIsNone(items['stop']['delta'])
        self.assertEqual(items['length']['state'], 'VALID')
        self.assertEqual(items['length']['delta'], 60)

    def test_histogram_creation_epoch_does_not_reset_an_unchanged_counter_epoch(self):
        text = exposition(tokens=160, buckets=(2, 4, 6), count=6, total=3).replace(
            f'{HIST}_created{{engine="0",model_name="example-model"}} 900',
            f'{HIST}_created{{engine="0",model_name="example-model"}} 1020')
        value = metrics.interval(self.previous, snapshot(text, at=1060), max_gap_seconds=120)
        self.assertEqual(value['state'], 'PARTIAL')
        self.assertEqual(series(value, 'histograms', HIST)['reason'], 'CREATION_EPOCH_CHANGED')
        self.assertEqual(series(value, 'counters', TOKENS)['delta'], 60)

    def test_prompt_and_generation_length_histograms_preserve_token_distributions(self):
        names = ('vllm:request_prompt_tokens', 'vllm:request_generation_tokens')
        def document(multiplier):
            lines = []
            for name in names:
                labels = 'engine="0",model_name="example-model"'
                lines += [f'# TYPE {name} histogram']
                for boundary, count in zip(('1', '10', '100', '+Inf'), (0, 1, 2, 3)):
                    lines += [f'{name}_bucket{{{labels},le="{boundary}"}} {count * multiplier}']
                lines += [f'{name}_count{{{labels}}} {3 * multiplier}',
                          f'{name}_sum{{{labels}}} {120 * multiplier}',
                          f'{name}_created{{{labels}}} 900']
            return '\n'.join(lines)
        previous = snapshot(document(1))
        current = snapshot(document(2), at=1060)
        value = metrics.interval(previous, current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'VALID')
        for name in names:
            item = series(value, 'histograms', name)
            self.assertEqual(item['unit'], 'tokens')
            self.assertEqual(item['count'], 3)
            self.assertEqual(item['sum'], 120)
            self.assertEqual(item['mean'], 40)

    def test_disappearing_engine_makes_a_remaining_valid_interval_partial(self):
        previous = snapshot(exposition() + exposition(engine='1', tokens=200))
        value = metrics.interval(previous, self.current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'PARTIAL')
        self.assertEqual(value['reason'], 'ENGINE_NOT_REPORTED')
        self.assertEqual(len(value['missingEngineKeys']), 1)
        self.assertEqual(value['missingEngineKeys'][0]['engineId'], '1')
        self.assertEqual(series(value, 'counters', TOKENS)['state'], 'VALID')
        self.assertEqual(series(value, 'counters', TOKENS)['delta'], 60)

    def test_all_engines_disappearing_requires_a_baseline_without_fabricated_values(self):
        previous = snapshot(exposition() + exposition(engine='1', tokens=200))
        current = snapshot('', at=1060)
        value = metrics.interval(previous, current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'BASELINE')
        self.assertEqual(value['reason'], 'ENGINE_NOT_REPORTED')
        self.assertEqual(len(value['missingEngineKeys']), 2)
        self.assertEqual(value['engines'], [])
        self.assertEqual(current['sourceState'], 'OBSERVED')

    def test_empty_observed_samples_do_not_claim_valid_interval_evidence(self):
        previous = snapshot('')
        current = snapshot('', at=1060)
        value = metrics.interval(previous, current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'BASELINE')
        self.assertEqual(value['reason'], 'NO_INTERVAL_SERIES')
        self.assertEqual(value['missingEngineKeys'], [])
        self.assertEqual(value['engines'], [])
        self.assertEqual(current['sourceState'], 'OBSERVED')

    def test_only_gauges_preserve_observed_zero_without_claiming_an_interval(self):
        text = f'{RUNNING}{{engine="0",model_name="example-model"}} 0\n'
        value = metrics.interval(snapshot(text), snapshot(text, at=1060), max_gap_seconds=120)
        self.assertEqual(value['state'], 'BASELINE')
        self.assertEqual(value['reason'], 'NO_INTERVAL_SERIES')
        self.assertEqual(series(value, 'gauges', RUNNING)['value'], 0)
        self.assertEqual(value['engines'][0]['counters'], [])
        self.assertEqual(value['engines'][0]['histograms'], [])

    def test_unchanged_reported_engines_preserve_valid_zero_intervals(self):
        text = exposition() + exposition(engine='1', tokens=200)
        value = metrics.interval(snapshot(text), snapshot(text, at=1060), max_gap_seconds=120)
        self.assertEqual(value['state'], 'VALID')
        self.assertIsNone(value['reason'])
        self.assertEqual(value['missingEngineKeys'], [])
        for engine in ('0', '1'):
            self.assertEqual(series(value, 'gauges', RUNNING, engine=engine)['value'], 0)
            self.assertEqual(series(value, 'counters', TOKENS, engine=engine)['delta'], 0)
            self.assertEqual(series(value, 'counters', TOKENS, engine=engine)['ratePerSecond'], 0)
            self.assertEqual(series(value, 'histograms', HIST, engine=engine)['count'], 0)
            self.assertIsNone(series(value, 'histograms', HIST, engine=engine)['mean'])

    def test_zero_cumulative_observations_are_valid_interval_evidence(self):
        text = exposition(tokens=0, buckets=(0, 0, 0), count=0, total=0)
        value = metrics.interval(snapshot(text), snapshot(text, at=1060), max_gap_seconds=120)
        self.assertEqual(value['state'], 'VALID')
        self.assertEqual(series(value, 'counters', TOKENS)['delta'], 0)
        self.assertEqual(series(value, 'counters', TOKENS)['ratePerSecond'], 0)
        self.assertEqual(series(value, 'histograms', HIST)['count'], 0)
        self.assertIsNone(series(value, 'histograms', HIST)['mean'])

    def test_disappearing_engine_does_not_override_an_existing_identity_baseline(self):
        previous = snapshot(exposition() + exposition(engine='1', tokens=200))
        current = deepcopy(self.current)
        current['identity']['configId'] = 'b' * 64
        value = metrics.interval(previous, current, max_gap_seconds=120)
        self.assertEqual(value['state'], 'BASELINE')
        self.assertEqual(value['reason'], 'IDENTITY_CHANGED')
        self.assertEqual(len(value['missingEngineKeys']), 1)
        self.assertIsNone(series(value, 'counters', TOKENS)['ratePerSecond'])

    def test_restored_container_metric_names_raise_only_machine_errors(self):
        for collection in ('gauges', 'counters', 'histograms'):
            for malformed in ([], {}):
                for side in ('previous', 'current'):
                    with self.subTest(collection=collection, shape=type(malformed).__name__, side=side):
                        previous, current = deepcopy(self.previous), deepcopy(self.current)
                        document = previous if side == 'previous' else current
                        document['engines'][0][collection][0]['name'] = deepcopy(malformed)
                        try:
                            metrics.interval(previous, current, max_gap_seconds=120)
                        except Exception as caught:
                            self.assertIsInstance(caught, metrics.MetricsError)
                            self.assertEqual(caught.reason, 'SNAPSHOT_INVALID')
                        else:
                            self.fail('A malformed metric name must be rejected.')


if __name__ == '__main__':
    unittest.main()
