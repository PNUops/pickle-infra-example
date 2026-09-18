#!/usr/bin/env python3
"""Node collection, reservation and narrowly scoped database-write tests."""
from dataclasses import asdict, replace
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/lib'))
from node_registration import Config, RegistrationError, Runner, apply_sql, capacity, collect, preview, register, snapshot_sql, utc_now, validate_report

CONFIG = Config.from_dict(json.loads((ROOT / 'examples/node-registration.json').read_text()))
PUBLIC_ID = 'ea9b79b8-cb30-4ee0-b3c2-b05a1a65b8c0'


def report(config=CONFIG):
    measured_at = utc_now().isoformat()
    return {'schema_version': 1, 'config': asdict(config), 'measured_at': measured_at,
            'boot_id': 'f49b7688-df8c-4854-9e8f-7916b6f51920',
            'placement_capacity': capacity({'cpu_threads': 32, 'memory_mb': 65536, 'disk_gb': 1024}, config, measured_at),
            'thin_pool': {'vg': 'pve', 'pool': 'data', 'total_bytes': 1024**4, 'available_bytes': 900 * 1024**3},
            'checks': {'cluster': True, 'local_api_address': True, 'bridge': True, 'thin_pool': True,
                       'ca_hostname_https': True, 'local_gateway_required': False}, 'ca_sha256': 'a' * 64}


def snapshot(config=CONFIG, *, existing=False):
    pool = {'id': 7, 'name': config.pool_name, 'cidr': config.pool_cidr, 'gateway': config.pool_gateway}
    node = {'id': 3, 'public_id': PUBLIC_ID, 'name': config.node, 'api_host': config.api_url,
            'status': 'MAINTENANCE', 'cpu_threads': 32, 'memory_mb': 57344, 'disk_capacity_gb': 1024,
            'vm_bridge': config.bridge, 'storage': config.storage, 'ip_pool_id': pool['id'],
            'labels': {'gpu': config.gpu_node, 'operator-label': 'keep'}} if existing else None
    return {'identity': {'database': config.database, 'user': 'postgres', 'local_socket': True,
                         'primary': True, 'system_identifier': config.database_system_identifier},
            'pool': pool, 'node': node, 'allocated_memory_mb': 0, 'allocated_vcpu': 0, 'allocated_disk_gb': 0}


class CollectionRunner(Runner):
    def api(self, path, *arguments):
        if path == '/cluster/status':
            return [{'type': 'cluster', 'name': CONFIG.cluster, 'quorate': 1},
                    {'type': 'node', 'name': CONFIG.node, 'online': 1}]
        if path.endswith('/status') and '/storage/' not in path:
            return {'cpuinfo': {'cpus': 32}, 'memory': {'total': 65536 * 1024**2}}
        if path.startswith('/storage/'):
            return {'type': 'lvmthin', 'vgname': 'pve', 'thinpool': 'data'}
        return {'active': 1, 'enabled': 1, 'total': 1024**4, 'avail': 900 * 1024**3}

    def run(self, command, **kwargs):
        if command[:3] == ['ip', '-j', '-d']:
            return json.dumps([{'ifname': CONFIG.bridge, 'flags': ['UP'], 'mtu': 1370,
                                'linkinfo': {'info_kind': 'bridge'}}])
        if command[:3] == ['ip', '-j', '-4']:
            return json.dumps([{'ifname': 'uplink', 'addr_info': [{'local': CONFIG.api_address}]}])
        if command[0] == 'lvs':
            return json.dumps({'report': [{'lv': [{'vg_name': 'pve', 'lv_name': 'data', 'lv_size': str(1024**4),
                                                  'segtype': 'thin-pool', 'lv_active': 'active'}]}]})
        if command[0] == 'curl':
            assert '--cacert' in command and '--resolve' in command and '-k' not in command
            return '200'
        return 'f49b7688-df8c-4854-9e8f-7916b6f51920'


class NodeRegistrationTests(unittest.TestCase):
    def test_standby_collection_does_not_require_local_gateway_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            ca = Path(directory) / 'public-ca.pem'
            ca.write_text('public certificate fixture')
            config = replace(CONFIG, ca_file=str(ca))
            with patch('node_registration.os.geteuid', return_value=0), \
                 patch('node_registration.socket.gethostname', return_value=CONFIG.node):
                result = collect(config, CollectionRunner())
        self.assertFalse(result['checks']['local_gateway_required'])
        self.assertEqual(result['placement_capacity']['allocatable'], {'cpu_threads': 28, 'memory_mb': 57344, 'disk_gb': 896})

    def test_configuration_requires_explicit_reserves_and_database_identity(self):
        for missing in ('reserve_cpu_threads', 'reserve_memory_mb', 'reserve_disk_gb', 'gpu_node', 'database_system_identifier'):
            config = asdict(CONFIG)
            del config[missing]
            with self.assertRaises(RegistrationError):
                Config.from_dict(config)
        for change in ({'node': "bad';drop"}, {'api_url': 'https://user:password@node.example:8006'},
                       {'reserve_memory_mb': True}, {'pool_cidr': '100.66.0.7/16'}):
            with self.assertRaises((RegistrationError, ValueError)):
                Config.from_dict({**asdict(CONFIG), **change})

    def test_reserves_cannot_consume_the_whole_node(self):
        with self.assertRaises(RegistrationError):
            capacity({'cpu_threads': 4, 'memory_mb': 65536, 'disk_gb': 1024}, CONFIG, utc_now().isoformat())

    def test_stale_or_tampered_collection_is_rejected(self):
        evidence = report()
        with self.assertRaises(RegistrationError):
            validate_report(evidence, utc_now() + timedelta(minutes=16))
        evidence['placement_capacity']['allocatable']['memory_mb'] += 1
        with self.assertRaises(RegistrationError):
            validate_report(evidence)

    def test_new_node_is_maintenance_and_existing_pool_is_mandatory(self):
        desired = preview(report(), snapshot())
        self.assertEqual(desired['status'], 'MAINTENANCE')
        self.assertEqual(desired['cpu_threads'], 32)
        self.assertEqual(desired['memory_mb'], 57344)
        self.assertEqual(desired['disk_capacity_gb'], 1024)
        self.assertEqual(desired['labels']['vm_nic_requirements'], {'schema_version': 1, 'mtu': 1370, 'firewall': True})
        for change in ({'pool': None}, {'pool': {**snapshot()['pool'], 'cidr': '198.18.0.0/16'}}):
            with self.assertRaises(RegistrationError):
                preview(report(), {**snapshot(), **change})

    def test_existing_status_uuid_topology_and_unrelated_labels_are_preserved(self):
        config = replace(CONFIG, existing_public_id=PUBLIC_ID, gpu_node=True)
        before = snapshot(config, existing=True)
        before['node']['status'] = 'OFFLINE'
        desired = preview(report(config), before)
        self.assertEqual(desired['status'], 'OFFLINE')
        self.assertEqual(desired['public_id'], PUBLIC_ID)
        self.assertTrue(desired['labels']['gpu'])
        self.assertEqual(desired['labels']['operator-label'], 'keep')
        self.assertNotIn('vm_nic_requirements', desired['labels'])
        before['node']['storage'] = 'other-storage'
        with self.assertRaises(RegistrationError):
            preview(report(config), before)

    def test_unknown_reservation_schemas_and_registration_metadata_are_preserved_by_refusal(self):
        config = replace(CONFIG, existing_public_id=PUBLIC_ID)
        for key, value in [('placement_capacity', {'schema_version': 2}), ('placement_capacity', None),
                           ('node_registration', 'unrecognized'), ('node_registration', None)]:
            before = snapshot(config, existing=True)
            before['node']['labels'][key] = value
            with self.assertRaises(RegistrationError):
                preview(report(config), before)
            self.assertEqual(before['node']['labels'][key], value)
        before = snapshot(config, existing=True)
        before['node']['labels']['placement_capacity'] = report(config)['placement_capacity']
        before['node']['labels']['placement_capacity']['allocatable']['disk_gb'] += 1
        with self.assertRaises(RegistrationError):
            preview(report(config), before)

    def test_gpu_role_is_explicit_and_cannot_change_during_reregistration(self):
        self.assertTrue(preview(report(replace(CONFIG, gpu_node=True)), snapshot())['labels']['gpu'])
        config = replace(CONFIG, existing_public_id=PUBLIC_ID, gpu_node=True)
        with self.assertRaises(RegistrationError):
            preview(report(config), snapshot(CONFIG, existing=True))

    def test_existing_nic_requirements_are_preserved_or_refused_without_reconfiguration(self):
        config = replace(CONFIG, existing_public_id=PUBLIC_ID)
        for value in [None, {'schema_version': 2, 'mtu': 1370, 'firewall': True},
                      {'schema_version': 1, 'mtu': 1500, 'firewall': True},
                      {'schema_version': 1, 'mtu': 1370, 'firewall': False}]:
            before = snapshot(config, existing=True)
            before['node']['labels']['vm_nic_requirements'] = value
            with self.assertRaises(RegistrationError):
                preview(report(config), before)
        before = snapshot(config, existing=True)
        before['node']['labels']['vm_nic_requirements'] = {'schema_version': 1, 'mtu': 1370, 'firewall': True}
        self.assertEqual(preview(report(config), before)['labels']['vm_nic_requirements'],
                         before['node']['labels']['vm_nic_requirements'])

    def test_existing_uuid_and_maintenance_are_required_for_a_capacity_change(self):
        with self.assertRaises(RegistrationError):
            preview(report(), snapshot(existing=True))
        config = replace(CONFIG, existing_public_id=PUBLIC_ID)
        before = snapshot(config, existing=True)
        before['node']['status'] = 'ACTIVE'
        with self.assertRaises(RegistrationError):
            preview(report(config), before)
        evidence = report(config)
        before['node']['labels']['placement_capacity'] = evidence['placement_capacity']
        self.assertEqual(preview(evidence, before)['status'], 'ACTIVE')
        before['node']['memory_mb'] = 60000
        with self.assertRaises(RegistrationError):
            preview(evidence, before)

    def test_wrong_database_and_excess_allocated_memory_are_rejected(self):
        for key, value in [('database', 'wrong'), ('system_identifier', '7600000000000000002'),
                           ('local_socket', False), ('primary', False), ('user', 'app')]:
            before = snapshot()
            before['identity'][key] = value
            with self.assertRaises(RegistrationError):
                preview(report(), before)
        config = replace(CONFIG, existing_public_id=PUBLIC_ID)
        before = snapshot(config, existing=True)
        before['allocated_memory_mb'] = 60000
        with self.assertRaises(RegistrationError):
            preview(report(config), before)
        for field, amount in [('allocated_vcpu', 29), ('allocated_disk_gb', 897)]:
            before = snapshot(config, existing=True)
            before[field] = amount
            with self.assertRaises(RegistrationError):
                preview(report(config), before)

    def test_default_registration_executes_only_read_only_sql(self):
        statements = []
        class DatabaseRunner(Runner):
            def postgres(self, config, sql):
                statements.append(sql)
                return snapshot(config)
        with patch('node_registration.os.geteuid', return_value=0), \
             patch('node_registration.socket.gethostname', return_value=CONFIG.database_hostname):
            result = register(report(), 'a' * 64, DatabaseRunner())
        self.assertEqual(result['mode'], 'dry-run')
        self.assertEqual(len(statements), 1)
        self.assertIn('BEGIN READ ONLY', statements[0])
        self.assertNotRegex(statements[0], r'(?i)\b(insert|update|delete|nextval)\b')


@unittest.skipUnless(os.environ.get('PICKLE_TEST_POSTGRES_IMAGE'), 'opt in with an already present PostgreSQL image digest')
class PostgreSQLRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = os.environ['PICKLE_TEST_POSTGRES_IMAGE']
        if not re.fullmatch(r'(?:[a-z0-9./:_-]+@)?sha256:[a-f0-9]{64}', image):
            raise RuntimeError('Use an explicit local PostgreSQL image digest')
        subprocess.run(['docker', 'image', 'inspect', image], check=True, capture_output=True)
        cls.container = 'node-registration-test-' + uuid.uuid4().hex
        subprocess.run(['docker', 'run', '--detach', '--rm', '--network', 'none', '--read-only',
                        '--name', cls.container, '--label', 'pickle.test=node-registration',
                        '--tmpfs', '/var/lib/postgresql:rw', '--tmpfs', '/var/run/postgresql:rw', '--tmpfs', '/tmp:rw',
                        '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', '--env', 'PGDATA=/var/lib/postgresql/test-data', image],
                       check=True, capture_output=True)
        cls.addClassCleanup(lambda: subprocess.run(['docker', 'rm', '--force', cls.container], check=True, capture_output=True))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            ready = subprocess.run(['docker', 'exec', cls.container, 'pg_isready', '-U', 'postgres'], capture_output=True)
            process = subprocess.run(['docker', 'exec', cls.container, 'cat', '/proc/1/comm'], capture_output=True, text=True)
            if ready.returncode == 0 and process.returncode == 0 and process.stdout.strip() == 'postgres':
                break
            time.sleep(0.2)
        else:
            raise RuntimeError('Disposable PostgreSQL did not start')
        cls.pg('CREATE DATABASE pickle_example;', database='postgres', parse=False)
        identifier = cls.pg('SELECT system_identifier::text FROM pg_control_system();', parse=False).strip()
        cls.config = replace(CONFIG, database_system_identifier=identifier)

    @classmethod
    def pg(cls, sql, *, database='pickle_example', parse=True, success=True):
        result = subprocess.run(['docker', 'exec', '-i', cls.container, 'psql', '-U', 'postgres', '-d', database,
                                 '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-f', '-'], input=sql, text=True, capture_output=True)
        if success and result.returncode:
            raise AssertionError(result.stderr)
        if not success:
            return result
        return json.loads(result.stdout) if parse else result.stdout

    def setUp(self):
        self.pg('''DROP SCHEMA public CASCADE; CREATE SCHEMA public;
CREATE TYPE node_status AS ENUM ('ACTIVE','MAINTENANCE','OFFLINE');
CREATE TABLE ip_pools(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,name text UNIQUE NOT NULL,cidr cidr,gateway inet);
CREATE TABLE nodes(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
 name text UNIQUE NOT NULL,api_host text,status node_status DEFAULT 'ACTIVE',cpu_threads integer,memory_mb integer,
 disk_capacity_gb bigint,vm_bridge text,storage text,ip_pool_id bigint REFERENCES ip_pools(id),labels jsonb DEFAULT '{}',updated_at timestamptz DEFAULT now());
CREATE TABLE vms(node_id bigint REFERENCES nodes(id),vcpu integer,memory_mb integer,disk_gb integer,status text,deleted_at timestamptz);
CREATE TABLE relays(note text); INSERT INTO relays VALUES ('keep');
INSERT INTO ip_pools(name,cidr,gateway) VALUES ('guest-private','100.66.0.0/16','100.66.0.1');
''', parse=False)

    def test_preview_is_read_only_and_apply_preserves_pool_other_rows_and_identity(self):
        evidence = report(self.config)
        before = self.pg(snapshot_sql(self.config))
        desired = preview(evidence, before)
        untouched = self.pg('SELECT jsonb_build_object(\'pool\',(SELECT to_jsonb(p) FROM ip_pools p),\'relay\',(SELECT to_jsonb(r) FROM relays r));')
        applied = self.pg(apply_sql(self.config, before, desired))['node']
        self.assertEqual(applied['id'], 1)
        self.assertEqual(applied['status'], 'MAINTENANCE')
        self.assertEqual(applied['disk_capacity_gb'], 1024)
        self.pg("UPDATE nodes SET status='OFFLINE',labels=labels || '{\"gpu\":true,\"operator\":\"keep\"}'::jsonb;", parse=False)
        config = replace(self.config, existing_public_id=applied['public_id'], gpu_node=True)
        before = self.pg(snapshot_sql(config))
        before['node']['labels']['operator'] = "$node_registration$; arbitrary note 'quoted'"
        encoded = json.dumps(before['node']['labels']).replace("'", "''")
        self.pg("UPDATE nodes SET labels='" + encoded + "'::jsonb;", parse=False)
        before = self.pg(snapshot_sql(config))
        desired = preview(report(config), before)
        updated = self.pg(apply_sql(config, before, desired))['node']
        self.assertEqual((updated['id'], updated['public_id'], updated['status']), (applied['id'], applied['public_id'], 'OFFLINE'))
        self.assertTrue(updated['labels']['gpu'])
        self.assertEqual(updated['labels']['operator'], "$node_registration$; arbitrary note 'quoted'")
        self.assertEqual(untouched, self.pg('SELECT jsonb_build_object(\'pool\',(SELECT to_jsonb(p) FROM ip_pools p),\'relay\',(SELECT to_jsonb(r) FROM relays r));'))

    def test_concurrent_row_change_rolls_back_without_overwriting_it(self):
        evidence = report(self.config)
        before = self.pg(snapshot_sql(self.config))
        desired = preview(evidence, before)
        self.pg("UPDATE ip_pools SET gateway='100.66.0.2';", parse=False)
        failed = self.pg(apply_sql(self.config, before, desired), success=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.pg('SELECT count(*) FROM nodes;'), 0)
        self.assertEqual(self.pg("SELECT to_jsonb(host(gateway)) FROM ip_pools;"), '100.66.0.2')


if __name__ == '__main__':
    unittest.main()
