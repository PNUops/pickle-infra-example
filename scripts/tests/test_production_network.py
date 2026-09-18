#!/usr/bin/env python3
"""Safety properties for owned network policy and lock failure handling."""
import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "lib"))
from production_network import NetBirdMarkNotReady, firewall_plan, nft_guard_text, parse_netbird_accept_mark, validate_config, validate_sdn_inventory

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
    def test_boot_readiness_waits_for_typed_absences_without_reconciling(self):
        module = load_script('production-network')
        now = [0.0]
        with patch.object(module, 'reconcile_readiness', side_effect=[
                module.ClusterQuorumNotReady('quorum'), module.MeshNotReady('mesh'),
                (False, (0x1bd20, 0xffffffff, 0x80000000))]) as readiness, \
             patch.object(module, 'reconcile') as reconcile, \
             patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(module.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            result = module.wait_for_startup_readiness(CONFIG, 'pve-a', {}, 5)
        self.assertEqual(result, {'attempts': 3, 'elapsed_seconds': 2.0})
        self.assertEqual(readiness.call_count, 3)
        reconcile.assert_not_called()

    def test_boot_readiness_times_out_each_typed_absence(self):
        module = load_script('production-network')
        for error in (module.ClusterQuorumNotReady('quorum'), module.MeshNotReady('mesh'),
                      NetBirdMarkNotReady('marks'), module.GuestFirewallNotReady('firewall')):
            now = [0.0]
            with self.subTest(error=error), patch.object(module, 'reconcile_readiness', side_effect=error), \
                 patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
                 patch.object(module.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
                with self.assertRaisesRegex(RuntimeError, 'after 2 attempts in 2.000s'):
                    module.wait_for_startup_readiness(CONFIG, 'pve-a', {}, 2)

    def test_boot_readiness_rejects_fatal_layout_and_command_errors_immediately(self):
        module = load_script('production-network')
        for error in (AssertionError('unexpected cluster identity'),
                      AssertionError('unexpected VXLAN remote peer'),
                      RuntimeError('iptables failed')):
            with self.subTest(error=error), \
                 patch.object(module, 'reconcile_readiness', side_effect=error), \
                 patch.object(module.time, 'sleep') as sleep:
                with self.assertRaises(type(error)):
                    module.wait_for_startup_readiness(CONFIG, 'pve-a', {}, 120)
                sleep.assert_not_called()

    def test_boot_readiness_does_not_accept_completion_at_deadline(self):
        module = load_script('production-network')
        now = [0.0]

        def late_success(*_args):
            now[0] = 2.0
            return False, (0x1bd20, 0xffffffff, 0x80000000)

        with patch.object(module, 'reconcile_readiness', side_effect=late_success), \
             patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(module.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'after 1 attempts in 2.000s'):
                module.wait_for_startup_readiness(CONFIG, 'pve-a', {}, 2)
        sleep.assert_not_called()

    def test_reconcile_revalidates_before_any_mutation(self):
        module = load_script('production-network')
        with patch.object(module, 'reconcile_readiness',
                          side_effect=AssertionError('baseline changed')), \
             patch.object(module, 'install_chains') as install, \
             patch.object(module, 'sysctl') as sysctl:
            with self.assertRaisesRegex(AssertionError, 'baseline changed'):
                module.reconcile(CONFIG, 'pve-a', {})
        install.assert_not_called()
        sysctl.assert_not_called()

    def test_guest_firewall_absence_retries_only_after_explicit_legacy_enable(self):
        module = load_script('production-network')
        with patch.object(module, 'api', return_value=[{'id': 'qemu/100'}]), \
             patch.object(module.Path, 'read_text', return_value='[OPTIONS]\nenable: 1\n'), \
             patch.object(module, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            with self.assertRaises(module.GuestFirewallNotReady):
                module.guest_firewall_check()
        with patch.object(module, 'api', return_value=[{'id': 'qemu/100'}]), \
             patch.object(module.Path, 'read_text', return_value='[OPTIONS]\nenable: 1\n'), \
             patch.object(module, 'run', side_effect=RuntimeError('iptables failed')):
            with self.assertRaisesRegex(RuntimeError, 'iptables failed'):
                module.guest_firewall_check()
        with patch.object(module, 'api', return_value=[{'id': 'qemu/100'}]), \
             patch.object(module.Path, 'read_text', return_value='[OPTIONS]\nenable: 0\n'), \
             patch.object(module, 'run') as run:
            with self.assertRaisesRegex(AssertionError, 'not enabled'):
                module.guest_firewall_check()
        run.assert_not_called()

    def test_cluster_wait_classifies_only_expected_identity_without_quorum(self):
        module = load_script('production-network')
        rows = [{'type': 'cluster', 'name': CONFIG['cluster'], 'quorate': 0},
                *[{'type': 'node', 'name': name} for name in CONFIG['nodes']]]

        def cluster_api(status_rows, ha=None, options=None):
            def fake(path, *_args):
                if path == '/cluster/status':
                    return status_rows
                if path == '/cluster/ha/resources':
                    return [] if ha is None else ha
                return {} if options is None else options
            return fake

        with patch.object(module, 'api', side_effect=cluster_api(rows)):
            with self.assertRaises(module.ClusterQuorumNotReady):
                module.cluster_check(CONFIG)
        wrong = copy.deepcopy(rows)
        wrong[0]['name'] = 'other-cluster'
        with patch.object(module, 'api', side_effect=cluster_api(wrong)):
            with self.assertRaisesRegex(AssertionError, 'cluster identity'):
                module.cluster_check(CONFIG)
        wrong_members = copy.deepcopy(rows[:-1])
        with patch.object(module, 'api', side_effect=cluster_api(wrong_members)):
            with self.assertRaisesRegex(AssertionError, 'cluster membership'):
                module.cluster_check(CONFIG)
        with patch.object(module, 'api', side_effect=cluster_api(rows, ha=[{'vmid': 100}])):
            with self.assertRaisesRegex(AssertionError, 'HA resources'):
                module.cluster_check(CONFIG)

        ready = copy.deepcopy(rows)
        ready[0]['quorate'] = 1
        with patch.object(module, 'api', side_effect=cluster_api(ready, options={})):
            module.cluster_check(CONFIG)
        for invalid in (None, True, '0', 1.0):
            with self.subTest(invalid=invalid), \
                 patch.object(module, 'api', side_effect=cluster_api(
                         ready, options={'nftables': invalid})):
                with self.assertRaisesRegex(AssertionError, 'nftables backend'):
                    module.cluster_check(CONFIG)

    def test_start_guests_is_guarded_and_never_uses_the_vendor_unit(self):
        module = load_script('production-network')
        state = {'phase': 'active', 'pending': False,
                 'guest_start_recovery': {'status': 'COMPLETED', 'boot_id': 'old-boot'}}
        calls = []
        timeouts = []
        def fake_run(argv, **kwargs):
            calls.append(argv)
            timeouts.append(kwargs.get('timeout'))
            stdout = ('ActiveState=inactive\nExecMainStartTimestampMonotonic=0\n'
                      if argv[:2] == ['systemctl', 'show'] else '')
            return subprocess.CompletedProcess(argv, 0, stdout, '')
        with patch.object(module, 'reconcile_readiness', side_effect=lambda *_: calls.append('ready')), \
             patch.object(module, 'validate_current', side_effect=lambda *_: calls.append('current')), \
             patch.object(module, 'local_checks', side_effect=lambda *_: calls.append('local')), \
             patch.object(module, 'run', side_effect=fake_run), \
             patch.object(module, 'write_json'), patch.object(module, 'current_boot_id', return_value='new-boot'), \
             patch.object(module.time, 'time', side_effect=[10, 11]):
            module.start_guests(CONFIG, 'pve-a', state)
        self.assertEqual(calls[:3], ['ready', 'current', 'local'])
        self.assertEqual(calls[3:], [
            ['/usr/share/pve-manager/helpers/pve-startall-delay'],
            'ready', 'current', 'local',
            ['systemctl', 'show', 'pve-guests.service', '-p', 'ActiveState',
             '-p', 'ExecMainStartTimestampMonotonic'],
            ['pvesh', '--nooutput', 'create', '/nodes/localhost/startall']])
        self.assertFalse(any(isinstance(call, list) and 'start' in call for call in calls))
        self.assertEqual([timeout for timeout in timeouts if timeout is not None],
                         [module.GUEST_START_DELAY_TIMEOUT, module.GUEST_STARTALL_TIMEOUT])
        self.assertEqual(state['guest_start_recovery']['status'], 'COMPLETED')
        self.assertEqual(state['guest_start_recovery']['boot_id'], 'new-boot')

    def test_start_guests_refuses_uncommitted_or_failed_validation(self):
        module = load_script('production-network')
        for state, failure in (({'phase': 'active', 'pending': True}, None),
                               ({'phase': 'active', 'pending': False}, AssertionError('invalid'))):
            with self.subTest(state=state), \
                 patch.object(module, 'reconcile_readiness', side_effect=failure), \
                 patch.object(module, 'current_boot_id', return_value='boot'), \
                 patch.object(module, 'run') as run:
                with self.assertRaises(AssertionError):
                    module.start_guests(CONFIG, 'pve-a', state)
                run.assert_not_called()

        with patch.object(module, 'reconcile_readiness'), \
             patch.object(module, 'validate_current'), patch.object(module, 'local_checks'), \
             patch.object(module, 'current_boot_id', return_value='boot'), \
             patch.object(module, 'run', return_value=subprocess.CompletedProcess(
                     [], 0, 'ActiveState=active\nExecMainStartTimestampMonotonic=42\n', '')) as run:
            with self.assertRaisesRegex(AssertionError, 'was not skipped'):
                module.start_guests(CONFIG, 'pve-a', {'phase': 'active', 'pending': False})
        self.assertEqual(run.call_count, 2)

        for previous in ({'status': 'COMPLETED', 'boot_id': 'boot'},
                         {'status': 'STARTING', 'boot_id': 'older'},
                         {'status': 'UNKNOWN', 'boot_id': 'older'},
                         {'status': 'COMPLETED'}):
            state = {'phase': 'active', 'pending': False, 'guest_start_recovery': previous}
            with self.subTest(previous=previous), \
                 patch.object(module, 'current_boot_id', return_value='boot'), \
                 patch.object(module, 'run') as run:
                with self.assertRaises(AssertionError):
                    module.start_guests(CONFIG, 'pve-a', state)
                run.assert_not_called()

    def test_start_guests_revalidates_after_delay_before_starting(self):
        module = load_script('production-network')
        state = {'phase': 'active', 'pending': False}
        with patch.object(module, 'current_boot_id', return_value='boot'), \
             patch.object(module, 'reconcile_readiness', side_effect=[None, AssertionError('stale')]), \
             patch.object(module, 'validate_current'), patch.object(module, 'local_checks'), \
             patch.object(module, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
             patch.object(module, 'write_json') as write:
            with self.assertRaisesRegex(AssertionError, 'stale'):
                module.start_guests(CONFIG, 'pve-a', state)
        self.assertEqual(run.call_args_list, [
            call(['/usr/share/pve-manager/helpers/pve-startall-delay'],
                 timeout=module.GUEST_START_DELAY_TIMEOUT)])
        write.assert_not_called()
        self.assertNotIn('guest_start_recovery', state)

    def test_start_guests_records_unknown_outcome_and_blocks_retry(self):
        module = load_script('production-network')
        state = {'phase': 'active', 'pending': False}
        results = [
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0,
                'ActiveState=inactive\nExecMainStartTimestampMonotonic=0\n', ''),
            subprocess.TimeoutExpired('pvesh', module.GUEST_STARTALL_TIMEOUT),
        ]
        with patch.object(module, 'reconcile_readiness'), patch.object(module, 'validate_current'), \
             patch.object(module, 'local_checks'), patch.object(module, 'run', side_effect=results), \
             patch.object(module, 'current_boot_id', return_value='boot'), \
             patch.object(module, 'write_json') as write, \
             patch.object(module.time, 'time', side_effect=[10, 11]):
            with self.assertRaisesRegex(RuntimeError, 'outcome is unknown'):
                module.start_guests(CONFIG, 'pve-a', state)
        self.assertEqual(state['guest_start_recovery']['status'], 'UNKNOWN')
        self.assertEqual(write.call_count, 2)
        with patch.object(module, 'run') as run:
            with patch.object(module, 'current_boot_id', return_value='other-boot'), \
                 self.assertRaisesRegex(AssertionError, 'needs operator investigation'):
                module.start_guests(CONFIG, 'pve-a', state)
        run.assert_not_called()

    def test_boot_unit_holds_guest_dependency_with_bounded_headroom(self):
        module = load_script('production-network')
        text = (SCRIPTS / 'production-network.py').read_text()
        self.assertIn('Before=pve-guests.service', text)
        self.assertIn('Requires=' + module.UNIT, text)
        self.assertIn('After=' + module.UNIT, text)
        self.assertIn('TimeoutStartSec=180s', text)
        self.assertIn('--startup-wait-seconds 120 --apply', text)
        self.assertNotIn('Upholds=', text)
        self.assertNotIn('systemctl", "start", "pve-guests', text)

    def test_startup_wait_is_boot_reconcile_only_and_defaults_to_immediate(self):
        module = load_script('production-network')
        module.validate_startup_wait('reconcile', 0, False, False)
        module.validate_startup_wait('reconcile', 120, True, True)
        for values in (('activate', 120, True, True), ('reconcile', 121, True, True),
                       ('reconcile', 120, False, True), ('reconcile', 120, True, False),
                       ('reconcile', -1, True, True)):
            with self.subTest(values=values), self.assertRaises(AssertionError):
                module.validate_startup_wait(*values)

    def test_mark_parser_distinguishes_only_transient_empty_rules(self):
        with self.assertRaises(NetBirdMarkNotReady):
            parse_netbird_accept_mark('', MANGLE, PVE_SOURCE)
        with self.assertRaises(NetBirdMarkNotReady):
            parse_netbird_accept_mark(FILTER, '', PVE_SOURCE)
        with self.assertRaises(AssertionError):
            parse_netbird_accept_mark(FILTER + FILTER, MANGLE, PVE_SOURCE)
        with self.assertRaisesRegex(AssertionError, 'ambiguous global accept mark'):
            parse_netbird_accept_mark(FILTER + FILTER.replace('0x1bd20', '0x42'), '', PVE_SOURCE)
        with self.assertRaisesRegex(AssertionError, 'setter/mask'):
            parse_netbird_accept_mark(FILTER.replace('0x1bd20', '0x1bd20/0xffff'), '', PVE_SOURCE)
        with self.assertRaisesRegex(AssertionError, 'unknown NetBird ingress'):
            parse_netbird_accept_mark(FILTER, MANGLE.replace('-i wt0', '-i wt1'), PVE_SOURCE)
        with self.assertRaisesRegex(AssertionError, 'unknown NetBird ingress'):
            parse_netbird_accept_mark('', MANGLE.replace('-i wt0', '-i wt1'), PVE_SOURCE)
        with self.assertRaisesRegex(AssertionError, 'not permitted'):
            parse_netbird_accept_mark('', MANGLE.replace('LOCAL', 'UNICAST'), PVE_SOURCE)
        with self.assertRaisesRegex(AssertionError, 'setter/mask'):
            parse_netbird_accept_mark('', MANGLE.replace('0xffffffff', '0xffff'), PVE_SOURCE)

    def test_mark_readiness_retries_empty_rules_until_two_strict_successes(self):
        module = load_script('production-network')
        expected = (0x1bd20, 0xffffffff, 0x80000000)
        now = [0.0]
        state = {}
        with patch.object(module, 'accept_mark', side_effect=[
                NetBirdMarkNotReady('not ready'), expected, expected]), \
             patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(module.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)), \
             patch.object(module, 'write_json'):
            self.assertEqual(module.wait_for_accept_mark(expected, state), expected)
        self.assertEqual(state['accept_mark_readiness']['attempts'], 3)
        self.assertEqual(state['accept_mark_readiness']['consecutive_successes'], 2)
        self.assertTrue(state['accept_mark_readiness']['ready'])
        self.assertEqual(state['accept_mark_readiness']['elapsed_seconds'], 0.5)

    def test_mark_readiness_times_out_without_real_sleep(self):
        module = load_script('production-network')
        now = [0.0]
        state = {}
        with patch.object(module, 'MARK_READY_TIMEOUT', 0.5), \
             patch.object(module, 'accept_mark', side_effect=NetBirdMarkNotReady('not ready')), \
             patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(module.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)), \
             patch.object(module, 'write_json'):
            with self.assertRaisesRegex(RuntimeError, 'after 2 attempts in 0.500s'):
                module.wait_for_accept_mark((0x1bd20, 0xffffffff, 0x80000000), state)
        self.assertEqual(state['accept_mark_readiness']['attempts'], 2)
        self.assertFalse(state['accept_mark_readiness']['ready'])

    def test_mark_readiness_rejects_changed_layout_and_command_errors_immediately(self):
        module = load_script('production-network')
        expected = (0x1bd20, 0xffffffff, 0x80000000)
        for failure in ((0x1bd20, 0xffff, 0x80000000), RuntimeError('iptables failed')):
            effect = failure if isinstance(failure, Exception) else None
            with self.subTest(failure=failure), \
                 patch.object(module, 'accept_mark', side_effect=effect, return_value=failure), \
                 patch.object(module.time, 'sleep') as sleep, patch.object(module, 'write_json'):
                with self.assertRaises((AssertionError, RuntimeError)):
                    module.wait_for_accept_mark(expected, {})
                sleep.assert_not_called()

    def test_mark_readiness_does_not_accept_success_at_the_deadline(self):
        module = load_script('production-network')
        expected = (0x1bd20, 0xffffffff, 0x80000000)
        now = [0.0]

        def late_success():
            now[0] = 0.5
            return expected

        with patch.object(module, 'MARK_READY_TIMEOUT', 0.5), \
             patch.object(module, 'accept_mark', side_effect=late_success), \
             patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(module.time, 'sleep') as sleep, patch.object(module, 'write_json'):
            with self.assertRaisesRegex(RuntimeError, 'after 1 attempts in 0.500s'):
                module.wait_for_accept_mark(expected, {})
        sleep.assert_not_called()

    def test_successful_rollback_disarms_only_its_timer_after_verification(self):
        module = load_script('production-network')
        calls = []
        with patch.object(module, 'local_checks', side_effect=lambda *_: calls.append('checked')), \
             patch.object(module, 'stop_transient_unit', side_effect=lambda unit: calls.append(unit)):
            module.finish_successful_rollback(CONFIG, 'pve-a', {})
        self.assertEqual(calls, ['checked', module.TIMER + '.timer'])

    def test_failed_rollback_verification_preserves_the_timer(self):
        module = load_script('production-network')
        with patch.object(module, 'local_checks', side_effect=AssertionError('verification failed')), \
             patch.object(module, 'stop_transient_unit') as stop:
            with self.assertRaisesRegex(AssertionError, 'verification failed'):
                module.finish_successful_rollback(CONFIG, 'pve-a', {})
        stop.assert_not_called()

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

    def test_sdn_submit_accepts_json_and_preserves_vendor_diagnostics(self):
        module = load_script('production-sdn')
        upid = 'UPID:pve-a:00000001:00000002:00000003:reloadnetworkall::root@pam:'
        outputs = [(json.dumps(upid) + '\n', ''),
                   ('pve-b: reloading network config\ninfo: executing /usr/bin/dpkg -l ifupdown2\n'
                    'pve-a: reloading network config\ninfo: executing /usr/bin/dpkg -l ifupdown2\n'
                    + json.dumps(upid) + '\n',
                    'pve-b: reloading network config\ninfo: executing /usr/bin/dpkg -l ifupdown2\n'
                    'pve-a: reloading network config\ninfo: executing /usr/bin/dpkg -l ifupdown2\n'),
                   ('vendor notice with no stable format\n' + json.dumps(upid) + '\n',
                    'vendor notice with no stable format\n')]
        for output, diagnostics in outputs:
            result = subprocess.CompletedProcess([], 0, stdout=output, stderr='')
            errors = io.StringIO()
            with self.subTest(output=output), patch.object(module.subprocess, 'run', return_value=result), \
                 contextlib.redirect_stderr(errors):
                self.assertEqual(module.submit_sdn_apply(CONFIG, 'test-lock'), upid)
            if diagnostics:
                self.assertIn(diagnostics, errors.getvalue())
            else:
                self.assertEqual(errors.getvalue(), '')

    def test_sdn_submit_rejects_all_other_unexpected_results(self):
        module = load_script('production-sdn')
        valid = 'UPID:pve-a:00000001:00000002:00000003:reloadnetworkall::root@pam:'
        cases = [
            (subprocess.CompletedProcess([], 1, stdout='', stderr='permission denied'), 'PVE set'),
            (subprocess.CompletedProcess([], 0, stdout='', stderr=''), 'no task identifier'),
            (subprocess.CompletedProcess([], 0, stdout='not-json\n', stderr=''), 'invalid task identifier'),
            (subprocess.CompletedProcess([], 0, stdout='{}\n', stderr=''), 'unexpected task identifier'),
            (subprocess.CompletedProcess([], 0, stdout=json.dumps(valid.replace('pve-a', 'pve-b', 1)) + '\n', stderr=''),
             'unexpected task identifier'),
            (subprocess.CompletedProcess([], 0, stdout=json.dumps(valid.replace('reloadnetworkall', 'srvreload')) + '\n', stderr=''),
             'unexpected task identifier'),
            (subprocess.CompletedProcess([], 0, stdout=json.dumps(valid) + '\ntrailing vendor output\n', stderr=''),
             'invalid task identifier'),
        ]
        for result, message in cases:
            with self.subTest(stdout=result.stdout, returncode=result.returncode), \
                 patch.object(module.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(RuntimeError, message):
                    module.submit_sdn_apply(CONFIG, 'test-lock')

    def test_sdn_submit_failure_preserves_owner_and_pending_state(self):
        module = load_script('production-sdn')
        calls = []
        def fake_api(method, path, *args):
            calls.append((method, path, args))
            if (method, path) == ('create', '/cluster/sdn/lock'):
                return 'test-lock'
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'network.json'
            config.write_text(json.dumps(CONFIG))
            owner = Path(directory) / 'owner.json'
            with patch.object(module, 'OWNER_FILE', owner), patch.object(module, 'api', side_effect=fake_api), \
                 patch.object(module, 'submit_sdn_apply', side_effect=RuntimeError('uncertain submit')), \
                 patch.object(module, 'check_cluster', return_value=([], [])), \
                 patch.object(module, 'active_network_tasks', return_value=set()), \
                 patch.object(module, 'network_tasks', return_value={}), \
                 patch.object(module.os, 'geteuid', return_value=0), patch.object(module.socket, 'gethostname', return_value='pve-a'), \
                 patch.object(sys, 'argv', ['production-sdn.py', '--config', str(config), '--apply']):
                with self.assertRaisesRegex(RuntimeError, 'uncertain submit'):
                    module.main()
            self.assertTrue(owner.exists())
        self.assertNotIn(('create', '/cluster/sdn/rollback', ('--lock-token', 'test-lock', '--release-lock', '0')), calls)
        self.assertIn(('delete', '/cluster/sdn/lock', ('--lock-token', 'test-lock')), calls)

    def test_lock_release_failure_does_not_hide_uncertain_submit(self):
        module = load_script('production-sdn')
        def fake_api(method, path, *args):
            if (method, path) == ('create', '/cluster/sdn/lock'):
                return 'test-lock'
            if (method, path) == ('delete', '/cluster/sdn/lock'):
                raise RuntimeError('release evidence')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'network.json'
            config.write_text(json.dumps(CONFIG))
            owner = Path(directory) / 'owner.json'
            errors = io.StringIO()
            with patch.object(module, 'OWNER_FILE', owner), patch.object(module, 'api', side_effect=fake_api), \
                 patch.object(module, 'submit_sdn_apply', side_effect=RuntimeError('primary evidence')), \
                 patch.object(module, 'check_cluster', return_value=([], [])), \
                 patch.object(module, 'active_network_tasks', return_value=set()), \
                 patch.object(module, 'network_tasks', return_value={}), \
                 patch.object(module.os, 'geteuid', return_value=0), patch.object(module.socket, 'gethostname', return_value='pve-a'), \
                 patch.object(sys, 'argv', ['production-sdn.py', '--config', str(config), '--apply']), \
                 contextlib.redirect_stderr(errors):
                with self.assertRaisesRegex(RuntimeError, 'primary evidence'):
                    module.main()
            self.assertTrue(owner.exists())
            self.assertIn('release evidence', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
