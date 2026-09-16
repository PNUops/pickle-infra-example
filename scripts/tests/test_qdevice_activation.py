#!/usr/bin/env python3
"""Exercise quorum rejection and locked configuration updates without live nodes."""
import copy
import hashlib
import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'activate-qdevice.py'
spec = importlib.util.spec_from_file_location('activation', SOURCE)
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


def status(votes=2):
    return f'Nodes:            2\nExpected votes:   {votes}\nTotal votes:      {votes}\nQuorum:           2  \nQuorate:          Yes\nFlags:            Quorate' + (' Qdevice' if votes == 3 else '') + '\n'


class InputAndReadinessTests(unittest.TestCase):
    def test_valid_explicit_mesh_inputs(self):
        activation.validate_inputs('prod-cluster', ['node-a', 'node-b'], '100.64.0.30', 'a' * 64, 'b' * 64)

    def test_invalid_input_cannot_reach_activation(self):
        valid = ['prod-cluster', ['node-a', 'node-b'], '100.64.0.30', 'a' * 64, 'b' * 64]
        for position, value in [(0, 'bad;name'), (1, ['node-a', 'node-a']), (1, ['-oBad', 'node-b']),
                                (1, ['node-a', 'node-b', 'node-c']), (2, '192.0.2.10'),
                                (2, '::1'), (2, '0100.64.0.30'), (3, 'A' * 64), (4, '')]:
            with self.subTest(value=value):
                values = copy.deepcopy(valid)
                values[position] = value
                with self.assertRaises(ValueError):
                    activation.validate_inputs(*values)

    def test_normal_vote_states_are_distinct(self):
        self.assertTrue(activation.quorum_ready(status(2), 2))
        self.assertTrue(activation.quorum_ready(status(3), 3))
        self.assertFalse(activation.quorum_ready(status(2), 3))
        self.assertFalse(activation.quorum_ready(status(3), 2))

    def test_missing_witness_or_vote_loss_is_not_healthy(self):
        for bad in [status(3).replace(' Qdevice', ''), status(3).replace('Total votes:      3', 'Total votes:      2'),
                    status(3).replace('Yes', 'No'), status(3).replace('Nodes:            2', 'Nodes:            1')]:
            self.assertFalse(activation.quorum_ready(bad, 3))

    def test_service_health_requires_tls_and_connected_state(self):
        good = {'quorum': status(3), 'active': 'active\n', 'enabled': 'enabled\n',
                'qdevice': 'State: Connected\nTLS: Yes (Client certificate sent)\n'}
        self.assertTrue(activation.service_ready(good))
        for key, value in [('active', 'inactive'), ('enabled', 'disabled'), ('qdevice', 'State: Connected\nTLS: No\n'),
                           ('qdevice', 'State: Disconnected\nTLS: Yes\n'), ('quorum', status(2))]:
            changed = {**good, key: value}
            self.assertFalse(activation.service_ready(changed))


class BootstrapEnvironmentTests(unittest.TestCase):
    def test_mesh_guard_rejects_invalid_address_with_optimized_parent_environment(self):
        script = (SOURCE.parent / 'configure-qnetd.sh').read_text()
        match = re.search(r"python3 ([^\n]+) <<'PY'\n(.*?)\nPY", script, re.S)
        self.assertIsNotNone(match)
        arguments = [value.replace('$mesh_ip', '192.0.2.9') for value in shlex.split(match.group(1))]
        with tempfile.TemporaryDirectory() as directory:
            command = Path(directory) / 'ip'
            command.write_text('#!/bin/sh\nprintf \'%s\n\' \'[{"addr_info":[{"local":"192.0.2.9"}]}]\'\n')
            command.chmod(0o700)
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ.get('PATH', ''),
                           'PYTHONOPTIMIZE': '1'}
            result = subprocess.run([sys.executable, *arguments], input=match.group(2), env=environment,
                                    capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('mesh address must be in shared space', result.stderr)


class LockedUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        pve = self.root / 'PVE'
        pve.mkdir()
        (pve / 'Cluster.pm').write_text(r'''
package PVE::Cluster;
use JSON::PP qw(decode_json);
sub fixture { open(my $f, '<', $ENV{QDEVICE_FIXTURE}) or die; local $/; return decode_json(<$f>); }
sub cfs_update { return; }
sub check_cfs_quorum { die "not quorate" unless fixture()->{quorate}; return 1; }
sub cfs_read_file { return fixture()->{conf}; }
sub get_members { return fixture()->{members}; }
sub cfs_lock_file {
    my ($name, $timeout, $callback) = @_;
    die 'wrong lock' unless $name eq 'corosync.conf' && $timeout == 10;
    if ($ENV{QDEVICE_DRIFT}) { open(my $f, '>>', $ENV{QDEVICE_CONFIG}) or die; print $f 'changed'; close($f); }
    eval { $callback->(); }; return;
}
1;
''')
        (pve / 'Corosync.pm').write_text(r'''
package PVE::Corosync;
use JSON::PP qw(encode_json);
sub nodelist { return $_[0]->{main}->{nodelist}->{node}; }
sub atomic_write_conf {
    my ($conf) = @_;
    $conf->{main}->{totem}->{config_version}++;
    open(my $f, '>', $ENV{QDEVICE_WRITTEN}) or die; print $f encode_json($conf); close($f);
}
1;
''')
        (pve / 'SSHInfo.pm').write_text(r'''
package PVE::SSHInfo;
sub ssh_info_to_command { my ($info, @opts) = @_; return ['/usr/bin/ssh', @opts, 'root@' . $info->{ip}]; }
1;
''')
        self.config = self.root / 'corosync.conf'
        self.config.write_text('original shared config\n')
        self.digest = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.written = self.root / 'written.json'
        self.fixture = {
            'quorate': True,
            'conf': {'main': {'totem': {'cluster_name': 'prod-cluster', 'config_version': 8},
                             'nodelist': {'node': {'node-a': {'quorum_votes': 1}, 'node-b': {'quorum_votes': 1}}},
                             'quorum': {'provider': 'corosync_votequorum'}, 'logging': {'to_syslog': 'yes'}}},
            'members': {'node-a': {'online': 1, 'ip': '192.0.2.10'}, 'node-b': {'online': 1, 'ip': '192.0.2.11'}}}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, apply=False, drift=False, digest=None):
        fixture = self.root / 'fixture.json'
        fixture.write_text(json.dumps(self.fixture))
        env = {**os.environ, 'PERL5LIB': str(self.root), 'QDEVICE_FIXTURE': str(fixture),
               'QDEVICE_CONFIG': str(self.config), 'QDEVICE_WRITTEN': str(self.written), 'QDEVICE_DRIFT': '1' if drift else ''}
        # Only the fixed filesystem location is substituted; the production lock,
        # validations and callback body are executed without reimplementation.
        self.assertEqual(activation.CONFIG_PROGRAM.count('/etc/pve/corosync.conf'), 1)
        program = activation.CONFIG_PROGRAM.replace('/etc/pve/corosync.conf', str(self.config))
        return subprocess.run(['perl', '-e', program, 'prod-cluster', 'node-a,node-b', '100.64.0.30',
                               digest or self.digest, '1' if apply else '0'],env=env,capture_output=True,text=True,timeout=10)

    def test_dry_run_does_not_write(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.written.exists())
        self.assertFalse(json.loads(result.stdout)['applied'])

    def test_activation_preserves_unrelated_configuration(self):
        result = self.invoke(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(self.written.read_text())
        expected = copy.deepcopy(self.fixture['conf'])
        expected['main']['totem']['config_version'] = 9
        expected['main']['quorum']['device'] = {'model': 'net', 'votes': 1, 'net': {'tls': 'required', 'host': '100.64.0.30', 'algorithm': 'ffsplit'}}
        self.assertEqual(actual, expected)

    def test_stale_digest_prevents_write(self):
        self.assertNotEqual(self.invoke(apply=True, digest='0' * 64).returncode, 0)
        self.assertFalse(self.written.exists())

    def test_change_while_acquiring_lock_prevents_write(self):
        self.assertNotEqual(self.invoke(apply=True, drift=True).returncode, 0)
        self.assertFalse(self.written.exists())

    def test_offline_node_prevents_write(self):
        self.fixture['members']['node-b']['online'] = 0
        self.assertNotEqual(self.invoke(apply=True).returncode, 0)
        self.assertFalse(self.written.exists())

    def test_wrong_cluster_prevents_write(self):
        self.fixture['conf']['main']['totem']['cluster_name'] = 'other'
        self.assertNotEqual(self.invoke(apply=True).returncode, 0)
        self.assertFalse(self.written.exists())

    def test_existing_qdevice_is_not_replaced(self):
        self.fixture['conf']['main']['quorum']['device'] = {'model': 'net'}
        self.assertNotEqual(self.invoke(apply=True).returncode, 0)
        self.assertFalse(self.written.exists())

    def test_vote_override_prevents_write(self):
        self.fixture['conf']['main']['quorum']['expected_votes'] = 1
        self.assertNotEqual(self.invoke(apply=True).returncode, 0)
        self.assertFalse(self.written.exists())

    def test_nonstandard_node_votes_prevent_write(self):
        self.fixture['conf']['main']['nodelist']['node']['node-b']['quorum_votes'] = 2
        self.assertNotEqual(self.invoke(apply=True).returncode, 0)
        self.assertFalse(self.written.exists())

    def test_no_quorum_prevents_write(self):
        self.fixture['quorate'] = False
        self.assertNotEqual(self.invoke(apply=True).returncode, 0)
        self.assertFalse(self.written.exists())


if __name__ == '__main__':
    unittest.main()
