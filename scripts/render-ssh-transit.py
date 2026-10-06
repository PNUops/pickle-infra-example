#!/usr/bin/env python3
"""Write a new offline SSH transit candidate directory without activating it."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from ssh_transit import Config, TransitError, source_firewall, units


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--source-nft', type=Path)
    parser.add_argument('--source-nft-sha256')
    args = parser.parse_args()
    if bool(args.source_nft) != bool(args.source_nft_sha256):
        parser.error('Supply source-nft and source-nft-sha256 together')
    config = Config.load(args.config.read_text())
    outputs = {name: text.encode('utf-8') for name, text in units(config).items()}
    if args.source_nft:
        outputs['source/nftables.conf'] = source_firewall(
            config, args.source_nft.read_bytes(), args.source_nft_sha256)
    # Exclusive directory creation preserves previous attempts and live backups.
    args.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = {'schema': 1, 'activation_authorized': False, 'files': {}}
    for name, payload in outputs.items():
        destination = args.output_dir / name
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        with destination.open('xb') as stream:
            stream.write(payload)
        destination.chmod(0o600)
        manifest['files'][name] = {'sha256': hashlib.sha256(payload).hexdigest(),
                                   'bytes': len(payload)}
    manifest_path = args.output_dir / 'manifest.json'
    with manifest_path.open('x') as stream:
        json.dump(manifest, stream, indent=2)
        stream.write('\n')
    manifest_path.chmod(0o600)
    print('Prepared unloaded SSH transit candidates; activation is separately approved')


if __name__ == '__main__':
    try:
        main()
    except (TransitError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
