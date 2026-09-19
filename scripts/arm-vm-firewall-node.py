#!/usr/bin/env python3
"""Preview or add the durable VM firewall opt-in label to one parked node."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from inventory_readiness import VmFirewallNodeConfig, arm_vm_firewall_node
from node_registration import RegistrationError, Runner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    config = VmFirewallNodeConfig.from_dict(json.loads(args.config.read_text()))
    print(json.dumps(arm_vm_firewall_node(config, Runner(), apply=args.apply,
                                          backup_dir=args.backup_dir), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RegistrationError, ValueError, KeyError, OSError) as error:
        print(f'VM firewall node opt-in: {error}', file=sys.stderr)
        sys.exit(1)
