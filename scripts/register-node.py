#!/usr/bin/env python3
"""Collect one PVE node or preview/apply its registration in an isolated database."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from node_registration import Config, RegistrationError, Runner, collect, register, require, write_private


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    collector = commands.add_parser('collect', help='Read-only measurements on the expected PVE node')
    collector.add_argument('--config', type=Path, required=True)
    collector.add_argument('--output', type=Path, required=True)
    registration = commands.add_parser('register', help='Read-only preview unless --apply is present')
    registration.add_argument('--inventory', type=Path, required=True)
    registration.add_argument('--inventory-sha256', required=True)
    registration.add_argument('--apply', action='store_true')
    registration.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    runner = Runner()
    if args.mode == 'collect':
        config = Config.from_dict(json.loads(args.config.read_text()))
        report = collect(config, runner)
        write_private(args.output, report)
        print(json.dumps({'inventory': str(args.output), 'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
                          'node': config.node, 'mode': 'read-only collection'}, indent=2))
    else:
        raw = args.inventory.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        require(digest == args.inventory_sha256, 'Inventory checksum differs from the reviewed input')
        print(json.dumps(register(json.loads(raw), digest, runner, apply=args.apply, backup_dir=args.backup_dir), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RegistrationError, ValueError, KeyError, OSError) as error:
        print(f'node registration: {error}', file=sys.stderr)
        sys.exit(1)
