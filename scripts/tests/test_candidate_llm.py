#!/usr/bin/env python3
"""Check the isolated gateway plan and its closed network boundary."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import candidate_llm as llm


class CandidateLlmTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = llm.Config.load(Path(__file__).resolve().parents[2] / 'examples/candidate-llm.json')

    def test_plan_keeps_application_closed(self) -> None:
        result = llm.plan(self.config)
        self.assertFalse(result['container']['onboot'])
        self.assertEqual(result['service'], {'enabled': False, 'started': False, 'authorization': 'closed'})
        self.assertFalse(result['proxy_changed'])
        self.assertFalse(result['api_changed'])
        self.assertIn('until nftables is installed', result['initial_network_window'])

    def test_firewall_accepts_only_candidate_proxy(self) -> None:
        rules = llm.firewall(self.config)
        self.assertIn('policy drop', rules)
        self.assertIn(f'ip saddr {self.config.proxy_ip} tcp dport 8081 accept', rules)
        self.assertEqual(rules.count('tcp dport'), 1)

    def test_rejects_colliding_addresses_and_undersized_memory(self) -> None:
        for change in ({'ip': self.config.proxy_ip}, {'memory_mb': 2048}):
            with self.subTest(change=change), self.assertRaises(llm.Error):
                replace(self.config, **change).validate()

    def test_rejects_ha_resources_and_active_tasks(self) -> None:
        class Inventory:
            def __init__(self, ha, tasks):
                self.ha, self.tasks = ha, tasks

            def run(self, args, **kwargs):
                if args[2] == '/cluster/ha/resources':
                    return json.dumps(self.ha)
                return json.dumps(self.tasks.get(args[2], []))

        nodes = ['compute-a', 'compute-b']
        with self.assertRaisesRegex(llm.Error, 'HA resources'):
            llm.require_idle_cluster(self.config, Inventory([{'sid': 'ct:203'}], {}), nodes)
        with self.assertRaisesRegex(llm.Error, 'Active PVE tasks'):
            llm.require_idle_cluster(self.config, Inventory([], {
                '/nodes/compute-b/tasks': [{'upid': 'UPID:compute-b:123'}]}), nodes)
        llm.require_idle_cluster(self.config, Inventory([], {}), nodes)

    def test_proxy_identity_requires_pinned_owned_running_single_net0(self) -> None:
        raw = b'pinned-proxy-config\n'
        config = replace(self.config, proxy_config_sha256=hashlib.sha256(raw).hexdigest())
        class Runner:
            def __init__(self):
                self.description = 'isolated-services:11111111-2222-4333-8444-555555555555:proxy'
                self.extra_net = ''
                self.status = 'status: running\n'

            def run(self, args, **kwargs):
                if args[:2] == ['pct', 'config']:
                    return (f'hostname: {config.proxy_hostname}\ndescription: {self.description}\n'
                            f'net0: name=eth0,bridge={config.bridge},ip={config.proxy_ip}/16\n'
                            f'{self.extra_net}')
                if args[:2] == ['pct', 'status']:
                    return self.status
                raise AssertionError('unexpected command')

        runner = Runner()
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)
        with patch.object(llm.Path, 'lstat', return_value=info), patch.object(llm.Path, 'read_bytes', return_value=raw):
            llm.require_proxy_identity(config, runner)
            runner.extra_net = 'net1: name=eth1,bridge=other\n'
            with self.assertRaisesRegex(llm.Error, 'exactly one net0'):
                llm.require_proxy_identity(config, runner)
            runner.extra_net = ''
            runner.status = 'status: stopped\n'
            with self.assertRaisesRegex(llm.Error, 'must be running'):
                llm.require_proxy_identity(config, runner)
            runner.status = 'status: running\n'
            runner.description = 'isolated-services:11111111-2222-4333-8444-555555555555:sshgw'
            with self.assertRaisesRegex(llm.Error, 'exact owned candidate'):
                llm.require_proxy_identity(config, runner)
        with patch.object(llm.Path, 'lstat', return_value=info), patch.object(llm.Path, 'read_bytes', return_value=b'changed'):
            with self.assertRaisesRegex(llm.Error, 'hash changed'):
                llm.require_proxy_identity(config, Runner())

    def test_requires_both_ssh_units_masked_and_inactive(self) -> None:
        class Guest(llm.Bootstrap):
            def __init__(self, config, state):
                super().__init__(config, None)
                self.state = state
                self.commands = []

            def guest(self, command, label):
                self.commands.append(command)
                return self.state if command[:2] == ['systemctl', 'show'] else ''

        safe = Guest(self.config, 'ActiveState=inactive\nUnitFileState=masked\n')
        safe.disable_guest_ssh()
        self.assertIn('systemctl mask --now ssh.socket ssh.service', safe.commands[0][2])
        self.assertEqual([command[2] for command in safe.commands[1:]], ['ssh.socket', 'ssh.service'])
        for state in ('ActiveState=active\nUnitFileState=masked\n',
                      'ActiveState=inactive\nUnitFileState=enabled\n'):
            with self.subTest(state=state), self.assertRaisesRegex(llm.Error, 'did not remain masked'):
                Guest(self.config, state).disable_guest_ssh()

    def test_failure_stops_only_the_exact_owned_container(self) -> None:
        class Runner:
            def __init__(self):
                self.calls = []
                self.stopped = False
                self.description = None

            def run(self, args, **kwargs):
                self.calls.append((args, kwargs))
                if args[:2] == ['pct', 'config']:
                    return (f'hostname: {self_hostname}\n'
                            f'description: {self.description}\n'
                            f'onboot: 0\nunprivileged: 1\n'
                            f'net0: name=eth0,bridge={self_bridge},ip={self_ip}/16\n')
                if args[:2] == ['pct', 'status']:
                    return 'status: stopped\n' if self.stopped else 'status: running\n'
                if args[:2] == ['pct', 'stop']:
                    self.stopped = True
                    return ''
                self.fail('unexpected command')

            def fail(self, message):
                raise AssertionError(message)

        self_hostname, self_bridge, self_ip = self.config.hostname, self.config.bridge, self.config.ip
        runner = Runner()
        bootstrap = llm.Bootstrap(self.config, runner)
        runner.description = f'{llm.DESCRIPTION}{bootstrap.run_id}'
        self.assertEqual(bootstrap.stop_owned_on_failure(), 'stopped')
        self.assertTrue(runner.stopped)
        self.assertEqual([call[0][:2] for call in runner.calls].count(['pct', 'stop']), 1)
        stop_call = next(call for call in runner.calls if call[0][:2] == ['pct', 'stop'])
        self.assertEqual(stop_call[1]['timeout'], 45)
        runner.stopped = False
        runner.calls.clear()
        runner.description = 'another-owner'
        self.assertIn('ownership_unverified', bootstrap.stop_owned_on_failure())
        self.assertNotIn(['pct', 'stop'], [call[0][:2] for call in runner.calls])

    def test_apply_records_stop_result_when_creation_partly_fails(self) -> None:
        class Runner:
            def run(self, args, **kwargs):
                if args[0:2] == ['pvesh', 'get'] and args[2] == '/cluster/status':
                    return json.dumps([{'type': 'cluster', 'name': 'example-cluster', 'quorate': 1},
                                       {'type': 'node', 'name': 'compute-a', 'online': 1}])
                if args[0:2] == ['pvesh', 'get']:
                    return '[]'
                if args[0:2] == ['pct', 'create']:
                    raise llm.Error('partial create')
                raise AssertionError('unexpected command')

        bootstrap = llm.Bootstrap(self.config, Runner())
        with patch.object(llm, 'preflight', return_value={}), \
                patch.object(llm, 'require_proxy_identity'), \
                patch.object(llm.Path, 'mkdir'), \
                patch.object(bootstrap, 'save'), \
                patch.object(bootstrap, 'stop_owned_on_failure', return_value='stopped') as stop:
            with self.assertRaisesRegex(llm.Error, 'partial create'):
                bootstrap.apply()
        stop.assert_called_once_with()
        self.assertEqual(bootstrap.manifest['failure_stop'], 'stopped')
        self.assertFalse(bootstrap.manifest['completed'])

    def test_executables_remain_root_owned(self) -> None:
        class Stage(llm.Bootstrap):
            def __init__(self, config):
                super().__init__(config, None)
                self.calls = []

            def guest(self, command, label):
                self.calls.append(('guest', command))
                return ''

            def put(self, path, content, mode='0644', owner='root:root'):
                self.calls.append(('put', path, mode, owner))

        stage = Stage(self.config)
        stage.stage_executables({'binary': b'binary', 'keygen': b'keygen'})
        self.assertEqual(stage.calls[0][1][3:5], ['root', '-g'])
        self.assertIn(('put', '/opt/pickle/llm-gateway/bin/llm-gateway', '0755', 'root:root'), stage.calls)
        self.assertIn(('put', '/opt/pickle/llm-gateway/bin/llm-keygen', '0750', 'root:pickle-llmgw'), stage.calls)


if __name__ == '__main__':
    unittest.main()
