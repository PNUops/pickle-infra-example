#!/usr/bin/env python3
"""Offline custody, exact ingress and persistence safety checks."""
import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / 'lib'))
from ssh_transit import Config, TransitError, source_firewall, units

CONFIG = Config('100.64.0.2', '100.64.0.1', '192.0.2.30', '198.18.1.30',
                '192.0.2.10', '/opt/pickle/sshgw/bin/sshgw-proxyfront')
SOURCE = b'''#!/usr/sbin/nft -f
flush ruleset
table inet sshgw {
    chain input {
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        iifname "wg0" ip saddr 100.64.0.1 tcp dport 22 accept
    }
    chain forward { type filter hook forward priority filter; policy drop; }
}
'''


class SSHTransitTests(unittest.TestCase):
    def test_source_firewall_preserves_original_bytes_after_one_added_rule(self):
        result = source_firewall(CONFIG, SOURCE, hashlib.sha256(SOURCE).hexdigest())
        added = (b'        iifname "wg0" ip saddr 100.64.0.1 ip daddr 100.64.0.2 '
                 b'tcp dport 2224 ct state new accept\n')
        self.assertEqual(result.count(added), 1)
        self.assertEqual(result.replace(added, b''), SOURCE)

    def test_pinned_patch_rejects_drift_and_existing_owner(self):
        for content, sha in ((SOURCE + b'# changed\n', hashlib.sha256(SOURCE).hexdigest()),
                             (SOURCE.replace(b'22 accept', b'2224 accept'), None),
                             (SOURCE.replace(b'chain input', b'chain forward'), None),
                             (SOURCE.replace(b'table inet sshgw', b'table inet other'), None),
                             (SOURCE.replace(b'100.64.0.1', b'100.64.0.3'), None)):
            with self.subTest(content=content), self.assertRaises(TransitError):
                source_firewall(CONFIG, content, sha or hashlib.sha256(content).hexdigest())

    def test_broad_addresses_and_unit_injection_are_rejected(self):
        for change in ({'transit_source': '0.0.0.0'}, {'relay_peer': '100.64.0.1/32'},
                       {'source_listen': '100.64.0.1'}, {'target_node': '127.0.0.1'},
                       {'target_binary': '/opt/pickle/../sshgw-proxyfront'},
                       {'target_binary': '/opt/pickle/sshgw-proxyfront\nExecStart=/bin/sh'},
                       {'target_binary': '/opt/pickle/%i/sshgw-proxyfront'}):
            with self.subTest(change=change), self.assertRaises(TransitError):
                units(dataclasses.replace(CONFIG, **change))

    def test_units_keep_raw_proxy_bytes_and_trust_only_the_exact_transit_peer(self):
        generated = units(CONFIG)
        socket = generated['source/pickle-ssh-transit.socket']
        self.assertIn('ListenStream=100.64.0.2:2224\nBindToDevice=wg0\nAccept=no', socket)
        self.assertIn('WantedBy=sockets.target', socket)
        self.assertIn('Requires=wg-quick@wg0.service nftables.service', socket)
        service = generated['source/pickle-ssh-transit.service']
        self.assertIn('/usr/lib/systemd/systemd-socket-proxyd 192.0.2.30:2224', service)
        self.assertNotIn('--proxy-protocol', service)
        self.assertNotIn('[Install]', service)
        frontend = generated['target/pickle-ssh-transit-front.service']
        self.assertIn('--listen 198.18.1.30:2224 --upstream 127.0.0.1:2222 --peer 192.0.2.10/32', frontend)
        self.assertNotIn('EnvironmentFile=', frontend)
        self.assertIn('Requires=isolated-services-firewall.service networking.service sshpiperd.service', frontend)
        self.assertNotIn('nftables.service', frontend)
        self.assertIn('WantedBy=multi-user.target', frontend)

    def test_cli_is_offline_exclusive_and_preserves_previous_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = directory / 'config.json'
            config.write_text(json.dumps(dataclasses.asdict(CONFIG)))
            output = directory / 'candidate'
            command = [sys.executable, '-B', str(SCRIPTS / 'render-ssh-transit.py'),
                       '--config', str(config), '--output-dir', str(output)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            before = {str(p): p.read_bytes() for p in output.rglob('*') if p.is_file()}
            self.assertFalse(json.loads((output / 'manifest.json').read_text())['activation_authorized'])
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(before, {str(p): p.read_bytes() for p in output.rglob('*') if p.is_file()})
            unknown = subprocess.run(command + ['--apply'], capture_output=True, text=True)
            self.assertEqual(unknown.returncode, 2)


if __name__ == '__main__':
    unittest.main()
