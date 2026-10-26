#!/usr/bin/env python3
"""Dedicated sampling, fixed reading and delivery of typed inference observations."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from inference_observation import ObservationError
from observation_io import IoError, config_file, http_worker
from observation_runtime import fetch_metrics_worker, read_latest, report, sample
from observation_spool import StoreError, decoded, encoded


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ObservationError('ARGUMENT_INVALID')


def main():
    try:
        parser = QuietParser(description=__doc__)
        parser.add_argument('operation', choices=('sample', 'read', 'report', 'http-worker', 'metrics-worker'))
        parser.add_argument('--config', type=Path)
        parser.add_argument('--state-dir', type=Path)
        args = parser.parse_args()
        if args.operation.endswith('-worker'):
            if args.config is not None or args.state_dir is not None:
                raise ObservationError('ARGUMENT_INVALID')
            payload = sys.stdin.buffer.read(1024**2 + 1)
            if len(payload) > 1024**2:
                raise ObservationError('WORKER_INPUT_TOO_LARGE')
            request = decoded(payload)
            result = http_worker(request) if args.operation == 'http-worker' else fetch_metrics_worker(request)
            sys.stdout.buffer.write(encoded(result))
            return 0
        if args.config is None or args.state_dir is None:
            raise ObservationError('ARGUMENT_INVALID')
        config = config_file(args.config)
        if args.operation == 'read':
            result = read_latest(config, args.state_dir, original_command=os.environ.get('SSH_ORIGINAL_COMMAND'))
            sys.stdout.buffer.write(result)
            return 0
        result = sample(config, args.state_dir) if args.operation == 'sample' else report(config, args.state_dir)
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 0 if result['status'] in ('PERSISTED', 'WAITING', 'OK') else 1
    except (ObservationError, StoreError, IoError) as error:
        print(json.dumps({'status': 'OBSERVATION_ERROR', 'reason': str(error)}, sort_keys=True))
        return 2
    except Exception:
        print('{"status":"OBSERVATION_ERROR","reason":"OBSERVATION_CONFIGURATION_OR_IO_INVALID"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
