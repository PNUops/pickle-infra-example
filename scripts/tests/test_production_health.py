#!/usr/bin/env python3
"""Offline checks for the production health net0 parser."""
import importlib.util
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True

MODULE = Path(__file__).resolve().parents[1] / 'production-health.py'
SPEC = importlib.util.spec_from_file_location('production_health', MODULE)
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


class Net0MtuTest(unittest.TestCase):
    def test_reads_configured_mtu(self):
        text = ('hostname: gateway\n'
                'net0: name=eth0,bridge=infranet,gw=198.18.0.1,hwaddr=BC:24:11:00:00:01,'
                'ip=198.18.1.40/16,mtu=1370,type=veth\n')
        self.assertEqual(health.net0_mtu(text), 1370)

    def test_absent_mtu_inherits_bridge(self):
        self.assertIsNone(health.net0_mtu('net0: name=eth0,bridge=vmbr1,ip=192.0.2.10/24,type=veth\n'))

    def test_rejects_missing_duplicate_or_malformed_values(self):
        for text in ('hostname: gateway\n',
                     'net0: name=eth0,mtu=1370\nnet0: name=eth0,mtu=1370\n',
                     'net0: name=eth0,mtu=1370,mtu=1500\n',
                     'net0: name=eth0,mtu=auto\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                health.net0_mtu(text)


if __name__ == '__main__':
    unittest.main()
