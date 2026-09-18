#!/usr/bin/env python3
"""Collect one stopped template and register it as a disabled node-local image."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from image_registration import Config, RegistrationError, Runner, collect, register, require, write_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    reader = modes.add_parser('collect', help='Read-only template collection on the expected PVE node')
    reader.add_argument('--config', type=Path, required=True)
    reader.add_argument('--output', type=Path, required=True)
    registrar = modes.add_parser('register', help='Read-only preview unless --apply is supplied')
    registrar.add_argument('--inventory', type=Path, required=True)
    registrar.add_argument('--inventory-sha256', required=True)
    registrar.add_argument('--apply', action='store_true')
    registrar.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    if args.mode == 'collect':
        report = collect(Config.from_dict(json.loads(args.config.read_text())), Runner())
        write_private(args.output, report)
        print(json.dumps({'inventory': str(args.output), 'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest()}, indent=2))
    else:
        raw = args.inventory.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        require(digest == args.inventory_sha256, 'Inventory checksum differs from the reviewed input')
        print(json.dumps(register(json.loads(raw), digest, Runner(), apply=args.apply, backup_dir=args.backup_dir), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RegistrationError, ValueError, KeyError, OSError, TypeError) as error:
        print(f'image registration: {error}', file=sys.stderr)
        sys.exit(1)
