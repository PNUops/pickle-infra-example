#!/usr/bin/env python3
"""Safety properties for owned network policy and lock failure handling."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "lib"))
from production_network import firewall_plan, nft_guard_text, parse_netbird_accept_mark, validate_config, validate_sdn_inventory

CONFIG = {
    "schema": 1, "cluster": "example-prod", "zone": "prodvx", "gateway_owner": "pve-a",
    "host_mtu": 1420, "guest_mtu": 1370, "uplink": "vmbr0", "mesh_interface": "wt0",
    "nodes": {"pve-a": {"campus": "192.0.2.30", "mesh": "100.64.0.30", "bmc_interface": "nic1", "bmc_address": "198.51.100.2"},
              "pve-b": {"campus": "192.0.2.31", "mesh": "100.64.0.31"}},
    "vnets": {"pinfra": {"vni": 927000, "cidr": "100.65.0.0/16", "gateway": "100.65.0.1"},
              "pguest": {"vni": 928000, "cidr": "100.66.0.0/16", "gateway": "100.66.0.1"}},
    "service_sources": {"api": "100.65.1.20", "proxy": "100.65.1.10", "sshgw": "100.65.1.30", "relay": "100.64.0.1"},
}
FILTER = '-A FORWARD -m mark --mark 0x1bd20 -j ACCEPT\n'
MANGLE = '-A NETBIRD-RT-PRE -i wt0 -m addrtype --dst-type LOCAL -j MARK --set-xmark 0x1bd20/0xffffffff\n'
PVE_SOURCE = 'my $FWACCEPTMARK_ON = "0x80000000/0x80000000";'


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProductionNetworkTests(unittest.TestCase):
    def test_transient_selfcheck_cleanup_accepts_units_collected_after_success(self):
        module = load_script('production-network')
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1] == 'stop':
                return subprocess.CompletedProcess(argv, 5, '', f'Unit {argv[2]} not loaded.')
            return subprocess.CompletedProcess(argv, 0, 'not-found\n', '')

        with patch.object(module, 'run', side_effect=fake_run):
            module.stop_transient_unit('example-selfcheck.timer')
            module.stop_transient_unit('example-selfcheck.service')
        self.assertEqual([call[0][2] for call in calls if call[0][1] == 'stop'],
                         ['example-selfcheck.timer', 'example-selfcheck.service'])

    def test_transient_selfcheck_cleanup_stops_loaded_units_without_state_probe(self):
        module = load_script('production-network')
        with patch.object(module, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            module.stop_transient_unit('example-selfcheck.timer')
        run.assert_called_once_with(['systemctl', 'stop', 'example-selfcheck.timer'], check=False)

    def test_transient_selfcheck_cleanup_does_not_hide_stop_errors_or_loaded_units(self):
        module = load_script('production-network')
        failures = [
            [subprocess.CompletedProcess([], 1, '', 'Access denied'),
             subprocess.CompletedProcess([], 0, 'not-found\n', '')],
            [subprocess.CompletedProcess([], 5, '', 'stop failed'),
             subprocess.CompletedProcess([], 0, 'loaded\n', '')],
        ]
        for results in failures:
            with self.subTest(results=results), patch.object(module, 'run', side_effect=results):
                with self.assertRaisesRegex(RuntimeError, 'systemctl stop'):
                    module.stop_transient_unit('example-selfcheck.timer')

    def test_transient_selfcheck_cleanup_does_not_hide_state_query_failure(self):
        module = load_script('production-network')
        with patch.object(module, 'run', side_effect=[
                subprocess.CompletedProcess([], 5, '', 'Unit not loaded'),
                RuntimeError('systemctl show failed')]):
            with self.assertRaisesRegex(RuntimeError, 'systemctl show failed'):
                module.stop_transient_unit('example-selfcheck.service')

    def test_commit_refuses_disabled_bridge_filtering_without_writing(self):
        module = load_script('production-network')
        for disabled in ('net.bridge.bridge-nf-call-iptables', 'net.bridge.bridge-nf-call-ip6tables'):
            with patch.object(module, 'cluster_check'), patch.object(module, 'mesh_profile', return_value={'mtu': 1420}), \
                 patch.object(module, 'check_links'), patch.object(module, 'guest_firewall_check'), \
                 patch.object(module, 'sysctl', side_effect=lambda key: '0' if key == disabled else '1') as sysctl, \
                 patch.object(module, 'install_chains') as install:
                with self.assertRaisesRegex(AssertionError, 'bridged guest filtering'):
                    module.validate_current(CONFIG, 'pve-a', {})
                install.assert_not_called()
                self.assertTrue(all(len(call.args) == 1 for call in sysctl.call_args_list))

    def test_initial_routes_reject_overlap_even_when_not_on_main_table(self):
        module = load_script('production-network')
        result = subprocess.CompletedProcess([], 0, json.dumps([{'dst': '100.66.9.0/24', 'table': 100}]), '')
        with patch.object(module, 'run', return_value=result):
            with self.assertRaisesRegex(AssertionError, 'existing route overlaps'):
                module.check_initial_routes(CONFIG)

    def test_controller_ssh_does_not_reuse_a_management_connection(self):
        module = load_script('verify-production-network')
        with patch.object(module, 'run', return_value='pve-a') as run:
            module.ssh('pve-a', ['hostname', '-s'], mesh='100.64.0.30', campus='192.0.2.30')
        command = run.call_args.args[0]
        for option in ('ControlPath=none', 'ProxyJump=none', 'ProxyCommand=none', 'Hostname=100.64.0.30', 'HostKeyAlias=192.0.2.30'):
            self.assertIn(option, command)

    def test_optimized_python_cannot_disable_safety_checks(self):
        code = f'import sys;sys.path.insert(0,{str(SCRIPTS / "lib")!r});import production_network'
        result = subprocess.run([sys.executable, '-B', '-O', '-c', code], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('safety checks require normal Python mode', result.stderr)

    def test_known_marks_are_accepted_but_ambiguous_or_partial_masks_are_refused(self):
        self.assertEqual(parse_netbird_accept_mark(FILTER, MANGLE, PVE_SOURCE), (0x1bd20, 0xffffffff, 0x80000000))
        for malformed in (FILTER + '-A FORWARD -m mark --mark 0x42 -j ACCEPT\n',
                          FILTER.replace('0x1bd20', '0x1bd20/0xffff')):
            with self.assertRaises(AssertionError):
                parse_netbird_accept_mark(malformed, MANGLE, PVE_SOURCE)
        with self.assertRaises(AssertionError):
            parse_netbird_accept_mark(FILTER, '', PVE_SOURCE)

    def test_mark_cleanup_never_matches_or_clears_pve_bits(self):
        plan = firewall_plan(CONFIG, 'pve-a', (0x1bd20, 0xffffffff, 0x80000000), True)
        for rules in plan.values():
            for rule in rules['mangle']:
                self.assertIn(rule[rule.index('--physdev-in') + 1], ('vxlan_pinfra', 'vxlan_pguest'))
                value, mask = (int(part, 0) for part in rule[rule.index('--mark') + 1].split('/'))
                clear = int(rule[rule.index('--set-xmark') + 1].split('/')[1], 0)
                self.assertEqual(mask, 0xffffffff)
                self.assertEqual(value & clear, value)
                self.assertEqual(clear & 0x80000000, 0)
                self.assertNotEqual((value | 0x80000000) & mask, value)

    def test_bridge_and_mark_conflicts_fail_before_building_a_plan(self):
        config = copy.deepcopy(CONFIG)
        config['vnets']['pguest']['cidr'] = config['vnets']['pinfra']['cidr']
        with self.assertRaises(AssertionError):
            validate_config(config)
        with self.assertRaises(AssertionError):
            parse_netbird_accept_mark(FILTER.replace('0x1bd20', '0x8001bd20'),
                                      MANGLE.replace('0x1bd20', '0x8001bd20'), PVE_SOURCE)

    def test_unrelated_zone_and_modified_owned_vnet_are_never_adopted(self):
        with self.assertRaises(AssertionError):
            validate_sdn_inventory(CONFIG, [{'zone': 'someoneelse'}], [])
        with self.assertRaises(AssertionError):
            validate_sdn_inventory(CONFIG, [], [{'vnet': 'pguest', 'zone': 'prodvx', 'tag': 3}])

    def test_standby_has_no_nat_and_no_routed_guest_egress(self):
        plan = firewall_plan(CONFIG, 'pve-b', (0x1bd20, 0xffffffff, 0x80000000), False)
        self.assertEqual(plan['iptables']['nat'], [])
        self.assertNotIn(['-i', 'pguest', '-o', 'vmbr0', '-j', 'RETURN'], plan['iptables']['forward'])
        self.assertIn(['-i', 'pguest', '-o', 'pguest', '-j', 'RETURN'], plan['iptables']['forward'])

    def test_bmc_guard_precedes_all_forward_permissions(self):
        plan = firewall_plan(CONFIG, 'pve-a', (0x1bd20, 0xffffffff, 0x80000000), True)
        for family in plan.values():
            self.assertEqual(family['forward'][:2], [['-i', 'nic1', '-j', 'DROP'], ['-o', 'nic1', '-j', 'DROP']])
        for family in plan.values():
            self.assertFalse(any(rule[-1] == 'ACCEPT' for rule in family['forward']))

    def test_all_host_destinations_are_blocked_before_uplink_permission(self):
        rules = firewall_plan(CONFIG, 'pve-a', (0x1bd20, 0xffffffff, 0x80000000), True)['iptables']['forward']
        outbound = rules.index(['-i', 'pguest', '-o', 'vmbr0', '-j', 'RETURN'])
        for node in CONFIG['nodes'].values():
            for destination in (node['campus'], node['mesh']):
                blocked = rules.index(['-i', 'pguest', '-d', destination, '-j', 'DROP'])
                self.assertLess(blocked, outbound)

    def test_priority_guards_precede_legacy_filter_and_do_not_globally_accept(self):
        text = nft_guard_text(CONFIG, 'pve-a', (0x1bd20, 0xffffffff, 0x80000000), True, 'owned_guard')
        self.assertIn('hook input priority -20', text)
        self.assertIn('hook forward priority -20', text)
        self.assertIn('ip daddr 192.0.2.31 counter drop', text)
        self.assertNotIn('counter accept', text)

    def test_running_baseline_reload_cannot_be_ignored(self):
        spec = importlib.util.spec_from_file_location('production_sdn_wait_test', SCRIPTS / 'production-sdn.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, 'api', return_value={'status': 'stopped', 'exitstatus': 'OK'}), \
             patch.object(module, 'active_network_tasks', return_value={'old-task'}):
            with self.assertRaisesRegex(AssertionError, 'baseline networking task'):
                module.wait_apply(CONFIG, 'UPID:pve-a:1', {'pve-a': {'old-task'}, 'pve-b': set()})

    def test_vtep_filter_does_not_block_guest_vxlan_egress(self):
        plan = firewall_plan(CONFIG, 'pve-a', (0x1bd20, 0xffffffff, 0x80000000), True)
        for family in plan.values():
            for rule in family['raw']:
                self.assertIn('--dst-type', rule)
                self.assertEqual(rule[rule.index('--dst-type') + 1], 'LOCAL')

    def test_sdn_failure_rolls_back_only_under_its_own_lock(self):
        spec = importlib.util.spec_from_file_location('production_sdn_test', SCRIPTS / 'production-sdn.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        def fake_api(method, path, *args):
            calls.append((method, path, args))
            if (method, path) == ('create', '/cluster/sdn/lock'):
                return 'test-lock'
            if (method, path) == ('create', '/cluster/sdn/zones'):
                raise RuntimeError('injected failure')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'network.json'
            config.write_text(json.dumps(CONFIG))
            owner = Path(directory) / 'owner.json'
            with patch.object(module, 'OWNER_FILE', owner), patch.object(module, 'api', side_effect=fake_api), \
                 patch.object(module, 'check_cluster', return_value=([], [])), \
                 patch.object(module.os, 'geteuid', return_value=0), patch.object(module.socket, 'gethostname', return_value='pve-a'), \
                 patch.object(sys, 'argv', ['production-sdn.py', '--config', str(config), '--apply']):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    module.main()
            self.assertFalse(owner.exists())
        self.assertIn(('create', '/cluster/sdn/rollback', ('--lock-token', 'test-lock', '--release-lock', '0')), calls)
        self.assertIn(('delete', '/cluster/sdn/lock', ('--lock-token', 'test-lock')), calls)
        self.assertFalse(any('--force' in call[2] for call in calls))


if __name__ == '__main__':
    unittest.main()
