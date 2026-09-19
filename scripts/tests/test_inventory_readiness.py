#!/usr/bin/env python3
"""Safety tests for the narrow IP pool and node firewall readiness writers."""
from dataclasses import asdict, replace
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'lib'))
from inventory_readiness import (IpPoolConfig, VmFirewallNodeConfig, arm_vm_firewall_node,
                                 ip_pool_apply_sql, ip_pool_snapshot_sql, preview_ip_pool,
                                 preview_vm_firewall_node, register_ip_pool,
                                 vm_firewall_node_apply_sql)
from node_registration import RegistrationError, Runner


IDENTITY = {'database': 'pickle_example', 'user': 'postgres', 'local_socket': True,
            'primary': True, 'system_identifier': '7600000000000000001',
            'server_version_num': 180000}
POOL_ID = '10000000-0000-4000-8000-000000000001'
NODE_ID = '20000000-0000-4000-8000-000000000001'
POOL = IpPoolConfig('guest-private', '198.19.0.0/16', '198.19.0.1',
                    ['192.0.2.53', '198.51.100.53'],
                    [{'from': '198.19.0.1', 'to': '198.19.0.31'}],
                    'pickle_example', 'db-example', IDENTITY['system_identifier'],
                    '/var/run/postgresql', None)
NODE = VmFirewallNodeConfig('pve-example', NODE_ID, 'https://pve.example.invalid:8006',
                            'guestbr0', 'local-lvm', 1370, 'vm-firewall-live-20260919',
                            'pickle_example', 'db-example', IDENTITY['system_identifier'],
                            '/var/run/postgresql')


def pool_row(config=POOL):
    return {'id': 1, 'public_id': POOL_ID, 'name': config.name, 'cidr': config.cidr,
            'gateway': config.gateway, 'dns': config.dns,
            'reserved_ranges': config.reserved_ranges,
            'created_at': '2026-09-19T00:00:00+00:00', 'updated_at': '2026-09-19T00:00:00+00:00'}


def pool_snapshot(config=POOL, *, existing=False):
    pool = pool_row(config) if existing else None
    return {'identity': {**IDENTITY, 'system_identifier': config.database_system_identifier},
            'pool': pool,
            'pools': [] if pool is None else [{key: pool[key] for key in ('id', 'public_id', 'name', 'cidr')}],
            'nodes': [], 'allocations': [], 'addresses_in_cidr': []}


def node_row(config=NODE):
    return {'id': 7, 'public_id': config.node_public_id, 'name': config.node,
            'api_host': config.api_host, 'status': 'MAINTENANCE', 'cpu_threads': 32,
            'memory_mb': 57344, 'disk_capacity_gb': 1024, 'vm_bridge': config.bridge,
            'storage': config.storage, 'ip_pool_id': 1,
            'labels': {'operator': 'keep', 'vm_nic_requirements': {
                'schema_version': 1, 'mtu': config.bridge_mtu, 'firewall': True}},
            'created_at': '2026-09-19T00:00:00+00:00', 'updated_at': '2026-09-19T00:00:00+00:00'}


class InventoryReadinessTests(unittest.TestCase):
    def test_wrappers_import_without_executing(self):
        for name in ('register-ip-pool.py', 'arm-vm-firewall-node.py'):
            spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / name)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

    def test_pool_config_rejects_noncanonical_and_overlapping_inputs(self):
        bad = [
            {'cidr': '198.19.0.1/16'},
            {'cidr': '198.19.0.0/' + '.'.join(('255', '255', '0', '0'))},
            {'gateway': '203.0.113.1'},
            {'dns': ['192.0.2.53', '192.0.2.53']},
            {'reserved_ranges': [{'from': '198.19.0.20', 'to': '198.19.0.30'},
                                 {'from': '198.19.0.25', 'to': '198.19.0.40'}]},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises((RegistrationError, ValueError)):
                IpPoolConfig.from_dict({**asdict(POOL), **change})

    def test_new_pool_and_exact_existing_pool_noop(self):
        desired = preview_ip_pool(POOL, pool_snapshot())
        self.assertEqual(desired['mode'], 'insert')
        existing = replace(POOL, existing_public_id=POOL_ID)
        confirmed = preview_ip_pool(existing, pool_snapshot(existing, existing=True))
        self.assertEqual((confirmed['mode'], confirmed['public_id']), ('noop', POOL_ID))

    def test_pool_refuses_changes_overlap_and_foreign_allocations(self):
        existing = replace(POOL, existing_public_id=POOL_ID)
        changed = pool_snapshot(existing, existing=True)
        changed['pool']['dns'] = ['203.0.113.53']
        with self.assertRaises(RegistrationError):
            preview_ip_pool(existing, changed)
        overlap = pool_snapshot()
        overlap['pools'] = [{'id': 9, 'public_id': str(uuid.uuid4()), 'name': 'other',
                             'cidr': '198.19.128.0/17'}]
        with self.assertRaises(RegistrationError):
            preview_ip_pool(POOL, overlap)
        occupied = pool_snapshot()
        occupied['addresses_in_cidr'] = [{'id': 4, 'pool_id': 9, 'ip': '198.19.0.40'}]
        with self.assertRaises(RegistrationError):
            preview_ip_pool(POOL, occupied)

    def test_pool_dry_run_is_one_read_only_query(self):
        statements = []

        class DatabaseRunner(Runner):
            def postgres(self, config, sql):
                statements.append(sql)
                return pool_snapshot(config)

        with patch('inventory_readiness.os.geteuid', return_value=0), \
             patch('inventory_readiness.socket.gethostname', return_value=POOL.database_hostname):
            result = register_ip_pool(POOL, DatabaseRunner())
        self.assertEqual((result['mode'], result['apply']), ('insert', False))
        self.assertEqual(len(statements), 1)
        self.assertIn('BEGIN READ ONLY', statements[0])
        self.assertNotRegex(statements[0], r'(?i)\b(insert|update|delete|nextval)\b')

    def test_node_label_preserves_every_other_label_and_stays_maintenance(self):
        snapshot = {'identity': IDENTITY, 'node': node_row()}
        desired = preview_vm_firewall_node(NODE, snapshot)
        self.assertEqual(desired['mode'], 'label-add')
        self.assertEqual(desired['status'], 'MAINTENANCE')
        self.assertEqual(desired['labels']['operator'], 'keep')
        self.assertEqual(desired['labels']['vm_firewall_policy'], {'schema_version': 1})
        snapshot['node']['labels'] = desired['labels']
        self.assertEqual(preview_vm_firewall_node(NODE, snapshot)['mode'], 'noop')

    def test_node_refuses_identity_state_nic_and_unknown_policy_changes(self):
        changes = [
            ('status', 'ACTIVE'), ('public_id', str(uuid.uuid4())),
            ('api_host', 'https://other.example.invalid:8006'), ('vm_bridge', 'wrongbr0'),
            ('storage', 'other-lvm')]
        for field, value in changes:
            row = node_row()
            row[field] = value
            with self.subTest(field=field), self.assertRaises(RegistrationError):
                preview_vm_firewall_node(NODE, {'identity': IDENTITY, 'node': row})
        for value in ({'schema_version': 2, 'mtu': 1370, 'firewall': True},
                      {'schema_version': 1, 'mtu': 1500, 'firewall': True}):
            row = node_row()
            row['labels']['vm_nic_requirements'] = value
            with self.assertRaises(RegistrationError):
                preview_vm_firewall_node(NODE, {'identity': IDENTITY, 'node': row})
        for value in (None, [], {'schema_version': 2}, {'schema_version': 1, 'extra': True}):
            row = node_row()
            row['labels']['vm_firewall_policy'] = value
            with self.subTest(policy=value), self.assertRaises(RegistrationError):
                preview_vm_firewall_node(NODE, {'identity': IDENTITY, 'node': row})

    def test_sql_rechecks_identity_rows_references_and_parked_state(self):
        pool_before = pool_snapshot()
        pool_desired = preview_ip_pool(POOL, pool_before)
        pool_sql = ip_pool_apply_sql(POOL, pool_before, pool_desired)
        for fragment in ('pg_advisory_xact_lock', 'FOR SHARE', 'FOR UPDATE',
                         'Pool references changed since preview', 'Existing allocation entered'):
            self.assertIn(fragment, pool_sql)
        node_before = {'identity': IDENTITY, 'node': node_row()}
        node_desired = preview_vm_firewall_node(NODE, node_before)
        node_sql = vm_firewall_node_apply_sql(NODE, node_before, node_desired)
        for fragment in ('pg_advisory_xact_lock', 'FOR UPDATE', "n.status::text <> 'MAINTENANCE'",
                         'Node changed since preview'):
            self.assertIn(fragment, node_sql)
        self.assertNotIn("status='ACTIVE'", node_sql)

    def test_database_identity_and_evidence_identifier_are_required(self):
        for key, value in [('database', 'wrong'), ('system_identifier', '7600000000000000002'),
                           ('local_socket', False), ('primary', False), ('user', 'pickle'),
                           ('server_version_num', 170000)]:
            identity = {**IDENTITY, key: value}
            with self.assertRaises(RegistrationError):
                preview_vm_firewall_node(NODE, {'identity': identity, 'node': node_row()})
        with self.assertRaises(RegistrationError):
            VmFirewallNodeConfig.from_dict({**asdict(NODE), 'capability_evidence_id': 'placeholder'})
        with self.assertRaises(RegistrationError):
            VmFirewallNodeConfig.from_dict({**asdict(NODE),
                                             'api_host': 'https://user@example.invalid:8006'})


@unittest.skipUnless(os.environ.get('PICKLE_TEST_POSTGRES_IMAGE'),
                     'opt in with an already present PostgreSQL image digest')
class PostgreSQLInventoryReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = os.environ['PICKLE_TEST_POSTGRES_IMAGE']
        if not re.fullmatch(r'(?:[a-z0-9./:_-]+@)?sha256:[a-f0-9]{64}', image):
            raise RuntimeError('Use an explicit local PostgreSQL image digest')
        subprocess.run(['docker', 'image', 'inspect', image], check=True, capture_output=True)
        cls.container = 'inventory-readiness-test-' + uuid.uuid4().hex
        subprocess.run(['docker', 'run', '--detach', '--rm', '--network', 'none', '--read-only',
                        '--name', cls.container, '--label', 'pickle.test=inventory-readiness',
                        '--tmpfs', '/var/lib/postgresql:rw', '--tmpfs', '/var/run/postgresql:rw',
                        '--tmpfs', '/tmp:rw', '--env', 'POSTGRES_HOST_AUTH_METHOD=trust',
                        '--env', 'PGDATA=/var/lib/postgresql/test-data', image],
                       check=True, capture_output=True)
        cls.addClassCleanup(lambda: subprocess.run(
            ['docker', 'rm', '--force', cls.container], check=True, capture_output=True))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            ready = subprocess.run(['docker', 'exec', cls.container, 'pg_isready', '-U', 'postgres'],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError('Disposable PostgreSQL did not start')
        cls.pg('CREATE DATABASE pickle_example;', database='postgres', parse=False)
        identifier = cls.pg('SELECT system_identifier::text FROM pg_control_system();', parse=False).strip()
        cls.pool = replace(POOL, database_system_identifier=identifier)
        cls.node = replace(NODE, database_system_identifier=identifier)

    @classmethod
    def pg(cls, sql, *, database='pickle_example', parse=True, success=True):
        result = subprocess.run(['docker', 'exec', '-i', cls.container, 'psql', '-U', 'postgres',
                                 '-d', database, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-f', '-'],
                                input=sql, text=True, capture_output=True)
        if success and result.returncode:
            raise AssertionError(result.stderr)
        if not success:
            return result
        return json.loads(result.stdout) if parse else result.stdout

    def setUp(self):
        self.pg('''DROP SCHEMA public CASCADE; CREATE SCHEMA public;
CREATE TYPE node_status AS ENUM ('ACTIVE','MAINTENANCE','OFFLINE');
CREATE TYPE allocation_status AS ENUM ('ALLOCATED','RELEASED');
CREATE TABLE ip_pools(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
 name text UNIQUE NOT NULL,cidr cidr NOT NULL,gateway inet NOT NULL,dns jsonb NOT NULL,reserved_ranges jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE nodes(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
 name text UNIQUE NOT NULL,api_host text,status node_status DEFAULT 'ACTIVE',cpu_threads integer,memory_mb integer,
 disk_capacity_gb bigint,vm_bridge text,storage text,ip_pool_id bigint REFERENCES ip_pools(id),labels jsonb DEFAULT '{}',
 created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now());
CREATE TABLE ip_allocations(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,pool_id bigint REFERENCES ip_pools(id),
 public_id uuid NOT NULL DEFAULT gen_random_uuid(),ip inet,status allocation_status,vm_id bigint,
 allocated_at timestamptz DEFAULT now(),released_at timestamptz);
''', parse=False)

    def test_pool_insert_noop_and_node_label_preserve_unrelated_state(self):
        before = self.pg(ip_pool_snapshot_sql(self.pool))
        desired = preview_ip_pool(self.pool, before)
        created = self.pg(ip_pool_apply_sql(self.pool, before, desired))
        self.assertEqual((created['node_count'], created['allocation_count']), (0, 0))
        existing_pool = replace(self.pool, existing_public_id=created['pool']['public_id'])
        before = self.pg(ip_pool_snapshot_sql(existing_pool))
        self.assertEqual(preview_ip_pool(existing_pool, before)['mode'], 'noop')
        self.pg("""INSERT INTO nodes(public_id,name,api_host,status,cpu_threads,memory_mb,disk_capacity_gb,
 vm_bridge,storage,ip_pool_id,labels) VALUES ('20000000-0000-4000-8000-000000000001','pve-example',
 'https://pve.example.invalid:8006','MAINTENANCE',32,57344,1024,'guestbr0','local-lvm',1,
 '{"operator":"keep","vm_nic_requirements":{"schema_version":1,"mtu":1370,"firewall":true}}');""", parse=False)
        node_before = self.pg("""BEGIN READ ONLY; SELECT jsonb_build_object('identity',
 jsonb_build_object('database',current_database(),'user',current_user,'local_socket',inet_server_addr() IS NULL,
 'primary',NOT pg_is_in_recovery(),'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
 'server_version_num',current_setting('server_version_num')::integer),
 'node',(SELECT to_jsonb(n) FROM nodes n WHERE name='pve-example')); COMMIT;""")
        node_desired = preview_vm_firewall_node(self.node, node_before)
        applied = self.pg(vm_firewall_node_apply_sql(self.node, node_before, node_desired))['node']
        self.assertEqual(applied['status'], 'MAINTENANCE')
        self.assertEqual(applied['labels']['operator'], 'keep')
        self.assertEqual(applied['labels']['vm_firewall_policy'], {'schema_version': 1})

    def test_apply_rejects_concurrent_pool_reference_and_node_state_changes(self):
        before = self.pg(ip_pool_snapshot_sql(self.pool))
        desired = preview_ip_pool(self.pool, before)
        self.pg("""INSERT INTO ip_pools(name,cidr,gateway,dns,reserved_ranges)
 VALUES ('other','203.0.113.0/24','203.0.113.1','["203.0.113.53"]','[]');""", parse=False)
        result = self.pg(ip_pool_apply_sql(self.pool, before, desired), success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pg("SELECT count(*) FROM ip_pools WHERE name='guest-private';"), 0)

        self.pg("""INSERT INTO ip_pools(name,cidr,gateway,dns,reserved_ranges)
 VALUES ('guest-private','198.19.0.0/16','198.19.0.1','["192.0.2.53","198.51.100.53"]',
 '[{"from":"198.19.0.1","to":"198.19.0.31"}]');
INSERT INTO nodes(public_id,name,api_host,status,cpu_threads,memory_mb,disk_capacity_gb,
 vm_bridge,storage,ip_pool_id,labels) VALUES ('20000000-0000-4000-8000-000000000001','pve-example',
 'https://pve.example.invalid:8006','MAINTENANCE',32,57344,1024,'guestbr0','local-lvm',2,
 '{"operator":"keep","vm_nic_requirements":{"schema_version":1,"mtu":1370,"firewall":true}}');""",
                parse=False)
        node_before = self.pg("""BEGIN READ ONLY; SELECT jsonb_build_object('identity',
 jsonb_build_object('database',current_database(),'user',current_user,'local_socket',inet_server_addr() IS NULL,
 'primary',NOT pg_is_in_recovery(),'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
 'server_version_num',current_setting('server_version_num')::integer),
 'node',(SELECT to_jsonb(n) FROM nodes n WHERE name='pve-example')); COMMIT;""")
        node_desired = preview_vm_firewall_node(self.node, node_before)
        self.pg("UPDATE nodes SET status='ACTIVE' WHERE name='pve-example';", parse=False)
        result = self.pg(vm_firewall_node_apply_sql(self.node, node_before, node_desired), success=False)
        self.assertNotEqual(result.returncode, 0)
        state = self.pg("SELECT jsonb_build_object('status',status,'labels',labels) FROM nodes WHERE name='pve-example';")
        self.assertEqual(state['status'], 'ACTIVE')
        self.assertNotIn('vm_firewall_policy', state['labels'])


if __name__ == '__main__':
    unittest.main()
