#!/usr/bin/env python3
"""Offline sample, delivery and crash-boundary cases with dummy observations."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from inference_observation import (ObservationError, canonical_bytes, for_reporter,
                                   instant, instant_millis, validate_sample)
from observation_io import PROTOCOL
from observation_runtime import read_host, read_latest, report, sample
from observation_spool import ObservationStore, StoreError
from inference_metrics import parse_snapshot


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/inference-observations.json').read_text())
SOURCE = FIXTURE['samples'][0]['sourceId']
EPOCH = FIXTURE['samples'][0]['epoch']
START = instant_millis(FIXTURE['samples'][0]['observedAt'])


def core(index=0):
    return {key: deepcopy(value) for key, value in FIXTURE['samples'][index].items()
            if key != 'reporterReceivedAt'}


def exposition(value):
    lines = []
    for group in value['inference']['engines']:
        common = {'model_name': group['modelName'], 'engine': group['engineId']}
        for kind in ('gauges', 'counters', 'histograms'):
            for item in group[kind]:
                name = item['name']
                lines.append(f'# TYPE {name} ' + {'gauges': 'gauge', 'counters': 'counter', 'histograms': 'histogram'}[kind])
                labels = {**common, **item['labels']}
                def label(extra=None):
                    return '{' + ','.join(key + '=' + json.dumps(raw) for key, raw in {**labels, **(extra or {})}.items()) + '}'
                if kind == 'histograms':
                    for bound, count in zip(item['bounds'], item['counts']):
                        lines.append(f'{name}_bucket{label({"le": str(bound)})} {count}')
                    lines.extend([f'{name}_count{label()} {item["count"]}', f'{name}_sum{label()} {item["sum"]}'])
                else:
                    lines.append(f'{name}{label()} {item["value"]}')
    for name in value['inference']['unsupportedMetricNames']:
        lines.append(name + '{extra="discarded-dummy-label"} 999')
    return ('\n'.join(lines) + '\n').encode()


class Crash(Exception):
    pass


class FakeApi:
    def __init__(self, received=START + 2000):
        self.received = received
        self.posts = []
        self.accepted = {}
        self.status = 200
        self.mutate_ack = lambda value: value

    def __call__(self, config, token, payload):
        self.posts.append(payload)
        if self.status != 200:
            return {'status': self.status}
        value = json.loads(payload)
        key = (value['sourceId'], value['epoch'], value['sequence'])
        digest = hashlib.sha256(payload).hexdigest()
        previous = self.accepted.get(key)
        if previous is not None and previous != digest:
            return {'status': 409}
        self.accepted[key] = digest
        ack = {'sourceId': key[0], 'epoch': key[1], 'sequence': key[2],
               'status': 'DUPLICATE' if previous else 'ACCEPTED',
               'contentSha256': digest, 'receivedAt': instant(self.received)}
        return {'status': 200, 'ack': self.mutate_ack(ack)}


class ObservationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.producer = self.directory / 'producer'
        self.outbox = self.directory / 'reporter'
        self.sample_config = {'version': 1, 'source_id': SOURCE, 'expected_hostname': 'model-host',
                              'metrics_url': 'http://127.0.0.1:8000/metrics', 'metrics_credential_file': None,
                              'service': 'model-serving.service', 'configuration_id': 'synthetic-configuration-1', 'gpu_index': 0}
        self.reader_config = {'version': 1, 'source_id': SOURCE, 'expected_hostname': 'model-host'}
        self.report_config = {'version': 1, 'source_id': SOURCE, 'expected_hostname': 'observation-reporter',
                              'ssh': {}, 'api_url': 'https://api.example.invalid/internal/inference-observations',
                              'credential_file': str(self.directory / 'token'), 'max_pending_samples': 20,
                              'max_pending_bytes': 1024**2, 'max_exports_per_run': 2}
        self.host = lambda config, now: (deepcopy(core()['host']),
                                         {key: value for key, value in core()['inference']['identity'].items() if key != 'sourceId'})
        self.api = FakeApi()

    def run_report(self, value=None, **options):
        options.setdefault('now', START + 2000)
        return report(self.report_config, self.outbox, hostname='observation-reporter',
                      source_reader=lambda: canonical_bytes(core() if value is None else value),
                      token_reader=lambda _: 'dummy-token', sender=self.api, **options)

    def state_and_pending(self):
        with ObservationStore(self.outbox).locked() as box:
            return box.load_control('state.json'), box.pending(20)

    def test_actual_sampler_reader_and_reporter_match_shared_wire_bytes(self):
        with ObservationStore(self.producer).locked() as box:
            box.save_control('state.json', {'version': 1, 'kind': 'producer', 'sourceId': SOURCE,
                                           'epoch': EPOCH, 'lastSequence': 0,
                                           'lastPublishedUtcMinute': START // 60000 - 1})
        sample(self.sample_config, self.producer, now=START, hostname='model-host',
               metrics_reader=lambda _: exposition(core()), host_reader=self.host)
        producer = read_latest(self.reader_config, self.producer, hostname='model-host', original_command=PROTOCOL)
        self.assertEqual(producer, canonical_bytes(core()))
        result = report(self.report_config, self.outbox, now=START + 2000,
                        hostname='observation-reporter', source_reader=lambda: producer,
                        token_reader=lambda _: 'dummy-token', sender=self.api)
        self.assertEqual(result['delivered'], 1)
        self.assertEqual(self.api.posts, [FIXTURE['requestBodies'][0].encode()])
        self.assertEqual(result['pending'], 0)

    def test_restart_and_epoch_state_preserve_one_publication_per_utc_minute(self):
        calls = []
        acquire = lambda _: (calls.append('GET') or exposition(core()))
        first = sample(self.sample_config, self.producer, now=START, hostname='model-host',
                       metrics_reader=acquire, host_reader=self.host)
        self.assertEqual(first['status'], 'WAITING')
        self.assertEqual(calls, [])
        published = sample(self.sample_config, self.producer, now=START + 60000, hostname='model-host',
                           metrics_reader=acquire, host_reader=self.host)
        sample(self.sample_config, self.producer, now=START + 61000, hostname='model-host',
               metrics_reader=acquire, host_reader=self.host)
        following = sample(self.sample_config, self.producer, now=START + 120000, hostname='model-host',
                           metrics_reader=acquire, host_reader=self.host)
        self.assertEqual(calls, ['GET', 'GET'])
        self.assertEqual((published['sequence'], following['sequence']), (1, 2))
        self.assertEqual(published['epoch'], following['epoch'])

    def test_metrics_failure_persists_unknown_instead_of_zero(self):
        sample(self.sample_config, self.producer, now=START, hostname='model-host')
        def failed(_):
            raise OSError()
        sample(self.sample_config, self.producer, now=START + 60000, hostname='model-host',
               metrics_reader=failed, host_reader=self.host)
        value = json.loads(read_latest(self.reader_config, self.producer,
                                      hostname='model-host', original_command=PROTOCOL))
        self.assertEqual(value['inference']['sourceState'], 'SOURCE_FAILED')
        self.assertEqual(value['inference']['engines'], [])
        self.assertEqual(value['host']['metrics']['swap_used_bytes']['value'], 0)

    def test_reader_never_creates_state_and_rejects_other_fixed_commands(self):
        with self.assertRaisesRegex(StoreError, '^DIRECTORY_NOT_PRIVATE$'):
            read_latest(self.reader_config, self.producer, hostname='model-host', original_command=PROTOCOL)
        self.assertFalse(self.producer.exists())
        for command in ('read-health-v1', PROTOCOL + '; echo bad', None):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ObservationError, '^READER_COMMAND_DENIED$'):
                    read_latest(self.reader_config, self.producer, hostname='model-host', original_command=command)

    def test_failed_delivery_retries_identical_bytes_and_preserves_first_import_time(self):
        self.api.status = 503
        first = self.run_report()
        state, pending = self.state_and_pending()
        self.assertEqual(state['checkpoints'], {})
        self.assertEqual(first['pending'], 1)
        self.api.status = 200
        self.api.received = START + 62000
        second = self.run_report(now=START + 62000)
        self.assertEqual(second['pending'], 0)
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(self.api.posts[0], self.api.posts[1])
        value = json.loads(self.api.posts[-1])
        self.assertEqual(value['reporterReceivedAt'], instant(START + 2000))
        self.assertEqual(value['observedAt'], core()['observedAt'])

    def test_unacknowledged_fresh_import_does_not_claim_a_successful_delivery_lag(self):
        self.api.status = 503
        result = self.run_report()
        self.assertIsNone(result['deliveryLagSeconds'])
        self.api.status = 200
        self.api.received = START + 62000
        result = self.run_report(now=START + 62000)
        self.assertEqual(result['deliveryLagSeconds'], 62)

    def test_mismatched_ack_never_advances_checkpoint_or_removes_pending(self):
        for field, wrong in [('sourceId', '00000000-0000-4000-8000-000000000999'),
                             ('epoch', '00000000-0000-4000-8000-000000000999'),
                             ('sequence', 99), ('status', 'REJECTED'),
                             ('contentSha256', '0' * 64), ('receivedAt', 'not-a-time')]:
            with self.subTest(field=field):
                self.outbox = self.directory / ('outbox-' + field)
                self.api.mutate_ack = lambda value: {**value, field: wrong}
                self.run_report()
                state, pending = self.state_and_pending()
                self.assertEqual(len(pending), 1)
                self.assertEqual(state['checkpoints'], {})

    def test_crash_after_ack_before_checkpoint_replays_the_same_immutable_request(self):
        self.crash_boundary('after_ack', expect_checkpoint=False)

    def test_crash_after_checkpoint_before_unlink_keeps_ack_order_and_request_bytes(self):
        self.crash_boundary('after_checkpoint', expect_checkpoint=True)

    def crash_boundary(self, point, expect_checkpoint):
        def boundary(at):
            if at == point:
                raise Crash()
        with self.assertRaises(Crash):
            self.run_report(boundary=boundary)
        state, pending = self.state_and_pending()
        self.assertEqual(bool(state['checkpoints']), expect_checkpoint)
        self.assertEqual(len(pending), 1)
        result = self.run_report(now=START + 62000)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(len(self.api.accepted), 1)
        self.assertEqual(self.api.posts[0], self.api.posts[1])

    def test_rejected_old_or_conflicting_samples_are_retained_for_owner_action(self):
        for status in (409, 422, 401):
            with self.subTest(status=status):
                self.outbox = self.directory / ('outbox-' + str(status))
                self.api.status = status
                result = self.run_report()
                self.assertEqual(result['pending'], 1)
                self.assertEqual(self.state_and_pending()[0]['checkpoints'], {})

    def test_source_mismatch_or_invalid_payload_never_enters_the_outbox(self):
        for value in ({'raw': 'untrusted-dummy'}, {**core(), 'sourceId': '00000000-0000-4000-8000-000000000999'}):
            with self.subTest(value=list(value)):
                result = self.run_report(value)
                self.assertEqual(result['pending'], 0)
                self.assertEqual(self.api.posts, [])

    def test_state_loss_does_not_reinitialize_pending_or_existing_latest_data(self):
        self.api.status = 503
        self.run_report()
        (self.outbox / 'state.json').unlink()
        before = len(self.api.posts)
        with self.assertRaisesRegex(ObservationError, '^REPORTER_STATE_LOST$'):
            self.run_report()
        self.assertEqual(len(self.api.posts), before)
        with ObservationStore(self.outbox).locked() as box:
            self.assertEqual(len(box.pending(1)), 1)
        with ObservationStore(self.producer).locked() as box:
            box.save_control('latest.json', core())
        with self.assertRaisesRegex(ObservationError, '^PRODUCER_STATE_LOST$'):
            sample(self.sample_config, self.producer, now=START, hostname='model-host',
                   metrics_reader=lambda _: self.fail('must not acquire after state loss'))

    def test_full_outbox_preserves_the_unacknowledged_original_without_zero_filling(self):
        self.report_config['max_pending_samples'] = 1
        self.api.status = 503
        self.run_report()
        result = self.run_report(core(1), now=START + 62000)
        self.assertEqual(result['pending'], 1)
        _, pending = self.state_and_pending()
        self.assertEqual(pending[0].sequence, 1)
        self.assertEqual(pending[0].document['observedAt'], core()['observedAt'])
        self.assertEqual(result['latestAcknowledgedObservedAt'], None)

    def test_permanent_rejection_is_quarantined_without_deletion_or_repeated_post(self):
        self.api.status = 409
        self.run_report()
        self.api.status = 200
        result = self.run_report(now=START + 62000)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(result['quarantined'], 1)
        self.assertEqual(result['pending'], 1)
        state, pending = self.state_and_pending()
        self.assertEqual(state['checkpoints'], {})
        self.assertEqual(pending[0].payload, self.api.posts[0])

    def test_quarantined_records_do_not_starve_a_new_valid_record(self):
        self.api.status = 409
        self.run_report()
        self.run_report(core(1), now=START + 62000)
        third = core(1)
        third['sequence'] = 3
        third['observedAt'] = instant(START + 120000)
        third['inference']['observedAt'] = (START + 120000) / 1000
        self.api.status = 200
        result = self.run_report(third, now=START + 122000)
        self.assertEqual(result['quarantined'], 2)
        self.assertEqual(result['pending'], 2)
        self.assertEqual(result['delivered'], 1)
        self.assertEqual(len(self.api.posts), 3)

    def test_cpu_load_and_gpu_unsupported_are_typed_without_health_state_writes(self):
        proc = self.directory / 'proc'
        (proc / 'sys/kernel/random').mkdir(parents=True)
        (proc / 'sys/kernel/random/boot_id').write_text(core()['inference']['identity']['bootId'])
        (proc / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 10 kB\nSwapFree: 10 kB\n')
        (proc / 'loadavg').write_text('0.75 0.50 0.25 1/10 123\n')
        def runner(argv, **_):
            return b'InvocationID=1234567890abcdef1234567890abcdef' if argv[0] == 'systemctl' else b'0, 40, [N/A], [N/A], [N/A]'
        host, identity = read_host(self.sample_config, START, runner=runner, proc=proc)
        self.assertEqual(host['metrics']['cpu_load1'], {'state': 'OBSERVED', 'value': 0.75})
        self.assertEqual(host['metrics']['gpu_utilization_percent']['value'], 0)
        self.assertEqual(host['metrics']['gpu_memory_total_bytes'], {'state': 'UNSUPPORTED', 'value': None})
        self.assertEqual(host['metrics']['swap_used_bytes']['value'], 0)
        (proc / 'loadavg').unlink()
        host, _ = read_host(self.sample_config, START, runner=runner, proc=proc)
        self.assertEqual(host['metrics']['cpu_load1'], {'state': 'NOT_REPORTED', 'value': None})

    def secondary_label(self, text):
        value = core()
        gauge = value['inference']['engines'][0]['gauges'][1]
        gauge['name'] = 'vllm:num_requests_waiting_by_reason'
        gauge['labels'] = {'reason': text}
        return value

    def test_secondary_empty_and_newline_values_are_not_trimmed_or_dropped(self):
        for text in ('', 'first\nsecond'):
            with self.subTest(text=repr(text)):
                value = self.secondary_label(text)
                validate_sample(value)
                self.assertEqual(for_reporter(value, received_at_millis=START + 2000)
                                 ['inference']['engines'][0]['gauges'][1]['labels']['reason'], text)

    def test_secondary_nul_is_rejected_before_queue_and_persisted_as_source_failure(self):
        value = self.secondary_label('first\x00second')
        result = self.run_report(value)
        self.assertEqual(self.api.posts, [])
        self.assertEqual(result['pending'], 0)
        payload = exposition(value).replace(b'\\u0000', b'\x00')
        parsed = parse_snapshot(payload, observed_at=START / 1000, identity=core()['inference']['identity'])
        self.assertIn('\x00', next(item for item in parsed['engines'][0]['gauges']
                                  if item['name'] == 'vllm:num_requests_waiting_by_reason')['labels']['reason'])
        sample(self.sample_config, self.producer, now=START - 60000, hostname='model-host')
        sample(self.sample_config, self.producer, now=START, hostname='model-host',
               metrics_reader=lambda _: payload, host_reader=self.host)
        latest = json.loads(read_latest(self.reader_config, self.producer,
                                       hostname='model-host', original_command=PROTOCOL))
        self.assertEqual(latest['inference']['sourceState'], 'SOURCE_FAILED')
        self.assertEqual(latest['inference']['engines'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
