#!/usr/bin/env python3
"""Template provenance, exact replica identity and narrow registration tests."""
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
from image_registration import Config, RegistrationError, Runner, apply_sql, collect, node_scoped_unique_sql, preview, read_manifest, register, snapshot_sql, validate_report
from node_registration import utc_now

CONFIG = Config.from_dict(json.loads((ROOT / 'examples/image-registration.json').read_text()))
IMAGE_UUID = 'cfa5dcf3-cb13-4db2-b8fb-42c473617ce7'


class TemplateRunner(Runner):
    def __init__(self):
        self.config = {'template': 1, 'digest': 'a' * 40,
                       'net0': 'virtio=02:00:00:00:00:01,bridge=guest0,mtu=1370,firewall=1',
                       'scsi0': 'local-lvm:base-1101-disk-0,size=8G', 'cipassword': 'must-not-enter-the-report'}
        self.resources = [{'vmid': 1101, 'node': CONFIG.node, 'type': 'qemu', 'template': 1, 'status': 'stopped'}]
        self.size = 8 * 1024**3

    def api(self, path, *arguments):
        if path == '/cluster/status':
            return [{'type': 'cluster', 'name': CONFIG.cluster, 'quorate': 1}]
        if path == '/cluster/resources':
            return self.resources
        if path.endswith('/config'):
            return self.config
        if path.endswith('/content'):
            return [{'volid': 'local-lvm:base-1101-disk-0', 'content': 'images', 'size': self.size}]
        raise AssertionError('Unexpected PVE read')


def report(config=CONFIG, runner=None):
    with patch('image_registration.os.geteuid', return_value=0), \
            patch('image_registration.socket.gethostname', return_value=config.node):
        return collect(config, runner or TemplateRunner())


def snapshot(config=CONFIG):
    return {'identity': {'database': config.database, 'user': 'postgres', 'local_socket': True,
                         'primary': True, 'system_identifier': config.database_system_identifier},
            'node_unique': True,
            'node': {'id': 2, 'public_id': config.node_public_id, 'status': 'MAINTENANCE',
                     'api_host': config.api_url, 'storage': config.storage,
                     'labels': {'node_registration': {'cluster': config.cluster},
                                'vm_nic_requirements': {'schema_version': 1, 'mtu': 1370, 'firewall': True}}},
            'images': []}


def image(node=1, status='ACTIVE'):
    return {'id': 1, 'public_id': IMAGE_UUID, 'name': CONFIG.name, 'display_name': CONFIG.display_name,
            'os_family': CONFIG.os_family, 'os_version': CONFIG.os_version, 'ssh_username': CONFIG.ssh_username,
            'version': CONFIG.version, 'min_disk_gb': CONFIG.min_disk_gb, 'notes': CONFIG.notes,
            'node_id': node, 'proxmox_vmid': CONFIG.template_vmid if node == 2 else 1001, 'status': status}


class ImageRegistrationTests(unittest.TestCase):
    def test_collection_preserves_only_reviewable_template_provenance(self):
        evidence = report()
        self.assertEqual(validate_report(evidence), CONFIG)
        self.assertNotIn('cipassword', json.dumps(evidence))
        self.assertNotIn('must-not-enter-the-report', json.dumps(evidence))
        self.assertEqual(evidence['template']['root_volume_bytes'], 8 * 1024**3)
        self.assertIsNone(evidence['build_provenance'])

    def test_collection_refuses_non_templates_and_other_node_resources(self):
        for changed in ({'template': 0}, {'status': 'running'}, {'type': 'lxc'}, {'node': 'other-node'}):
            runner = TemplateRunner()
            runner.resources[0].update(changed)
            with self.assertRaises(RegistrationError):
                report(runner=runner)

    def test_missing_nic_flags_and_understated_minimum_disk_are_rejected(self):
        for nic in ['virtio=02:00:00:00:00:01,mtu=1500,firewall=1',
                    'virtio=02:00:00:00:00:01,mtu=1370',
                    'virtio=02:00:00:00:00:01,mtu=1370,firewall=1,firewall=0']:
            runner = TemplateRunner()
            runner.config['net0'] = nic
            with self.assertRaises(RegistrationError):
                report(runner=runner)
        runner = TemplateRunner()
        runner.size = 10 * 1024**3 + 1
        with self.assertRaises(RegistrationError):
            report(runner=runner)

    def test_stale_evidence_is_not_an_activation_or_registration_ticket(self):
        evidence = report()
        with self.assertRaises(RegistrationError):
            validate_report(evidence, utc_now() + timedelta(minutes=16))
        evidence['build_provenance'] = {'unexpected': 'must not be printed'}
        with self.assertRaises(RegistrationError):
            validate_report(evidence)

    def test_new_replica_is_disabled_without_relocating_the_existing_public_image(self):
        before = snapshot()
        original = image()
        before['images'] = [original.copy()]
        desired = preview(CONFIG, report(), before)
        self.assertEqual((desired['node_id'], desired['status'], desired['public_id']), (2, 'DISABLED', None))
        self.assertEqual(before['images'], [original])
        self.assertEqual(before['node']['status'], 'MAINTENANCE')

    def test_existing_revision_is_noop_with_the_same_uuid_and_status(self):
        config = replace(CONFIG, existing_public_id=IMAGE_UUID)
        before = snapshot(config)
        before['images'] = [image(node=2)]
        desired = preview(config, report(config), before)
        self.assertEqual((desired['public_id'], desired['status']), (IMAGE_UUID, 'ACTIVE'))
        for field, value in [('proxmox_vmid', 1201), ('os_version', '24.04'), ('ssh_username', 'other'),
                             ('min_disk_gb', 20), ('display_name', 'Changed label')]:
            changed = image(node=2)
            changed[field] = value
            before['images'] = [changed]
            with self.assertRaises(RegistrationError):
                preview(config, report(config), before)

    def test_schema_node_and_uuid_guards_precede_any_write(self):
        for change in [('status', 'ACTIVE'), ('public_id', IMAGE_UUID), ('storage', 'other-storage')]:
            before = snapshot()
            before['node'][change[0]] = change[1]
            with self.assertRaises(RegistrationError):
                preview(CONFIG, report(), before)
        before = snapshot()
        before['node_unique'] = False
        with self.assertRaises(RegistrationError):
            preview(CONFIG, report(), before)

    def test_default_register_only_uses_a_read_only_transaction(self):
        statements = []
        class DatabaseRunner(Runner):
            def postgres(self, config, sql):
                statements.append(sql)
                return snapshot(config)
        with patch('image_registration.os.geteuid', return_value=0), \
                patch('image_registration.socket.gethostname', return_value=CONFIG.database_hostname):
            result = register(report(), 'a' * 64, DatabaseRunner())
        self.assertEqual(result['mode'], 'dry-run')
        self.assertEqual(result['status'], 'DISABLED')
        self.assertEqual(len(statements), 1)
        self.assertIn('BEGIN READ ONLY', statements[0])
        self.assertNotRegex(statements[0], r'(?i)\b(insert|update|delete|nextval)\b')

    def test_manifest_preserves_source_checksum_without_claiming_a_built_disk_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'manifest.json'
            content = {'templateVmid': 1101, 'osFamily': 'ubuntu', 'osVersion': '26.04', 'ciUser': 'ubuntu',
                       'checksumAlgorithm': 'sha256', 'imageChecksum': 'b' * 64,
                       'recipeRevision': 'abcdef123', 'builtAt': '2026-09-18T00:00:00Z'}
            path.write_text(json.dumps(content))
            evidence = read_manifest(replace(CONFIG, build_manifest_file=str(path)))
            self.assertEqual(evidence['source_image_checksum'], 'b' * 64)
            self.assertIn('not a final PVE disk hash', evidence['scope'])
            content['templateVmid'] = 1102
            path.write_text(json.dumps(content))
            with self.assertRaises(RegistrationError):
                read_manifest(replace(CONFIG, build_manifest_file=str(path)))

    def test_managed_guest_vmids_cannot_be_used_as_catalog_templates(self):
        with self.assertRaises(RegistrationError):
            Config.from_dict({**asdict(CONFIG), 'template_vmid': 100001})


@unittest.skipUnless(os.environ.get('PICKLE_TEST_POSTGRES_IMAGE'), 'opt in with an already present PostgreSQL image digest')
class PostgreSQLImageRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image_ref = os.environ['PICKLE_TEST_POSTGRES_IMAGE']
        if not re.fullmatch(r'(?:[a-z0-9./:_-]+@)?sha256:[a-f0-9]{64}', image_ref):
            raise RuntimeError('Use an explicit local PostgreSQL image digest')
        subprocess.run(['docker', 'image', 'inspect', image_ref], check=True, capture_output=True)
        cls.container = 'image-registration-test-' + uuid.uuid4().hex
        subprocess.run(['docker', 'run', '--detach', '--rm', '--network', 'none', '--read-only',
                        '--name', cls.container, '--label', 'pickle.test=image-registration',
                        '--tmpfs', '/var/lib/postgresql:rw', '--tmpfs', '/var/run/postgresql:rw', '--tmpfs', '/tmp:rw',
                        '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', '--env', 'PGDATA=/var/lib/postgresql/test-data', image_ref],
                       check=True, capture_output=True)
        cls.addClassCleanup(lambda: subprocess.run(['docker', 'rm', '--force', cls.container], check=True, capture_output=True))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            ready = subprocess.run(['docker', 'exec', cls.container, 'pg_isready', '-U', 'postgres'], capture_output=True)
            process = subprocess.run(['docker', 'exec', cls.container, 'cat', '/proc/1/comm'], capture_output=True, text=True)
            if ready.returncode == 0 and process.stdout.strip() == 'postgres':
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
CREATE TYPE catalog_status AS ENUM ('ACTIVE','DISABLED');
CREATE TABLE nodes(id bigint PRIMARY KEY,public_id uuid UNIQUE NOT NULL,name text UNIQUE NOT NULL,
 api_host text,status node_status,storage text,labels jsonb);
CREATE TABLE os_images(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 public_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),name text,display_name text,os_family text,
 os_version text,ssh_username text,proxmox_vmid integer,node_id bigint REFERENCES nodes(id),version integer,
 min_disk_gb integer,status catalog_status,notes text,updated_at timestamptz DEFAULT now(),
 UNIQUE(node_id,name,version));
CREATE TABLE unrelated_inventory(value text); INSERT INTO unrelated_inventory VALUES ('keep');
''', parse=False)
        config = self.config
        labels = json.dumps({'node_registration': {'cluster': config.cluster},
                             'vm_nic_requirements': {'schema_version': 1, 'mtu': 1370, 'firewall': True}})
        self.pg(f"INSERT INTO nodes VALUES (1,gen_random_uuid(),'other-node','https://other.example.com:8006','ACTIVE','local-lvm','{{}}'),"
                f"(2,'{config.node_public_id}','{config.node}','{config.api_url}','MAINTENANCE','local-lvm','{labels}');"
                f"INSERT INTO os_images(name,display_name,os_family,os_version,ssh_username,proxmox_vmid,node_id,version,min_disk_gb,status,notes) "
                f"VALUES ('{config.name}','Original display','ubuntu','26.04','ubuntu',1001,1,1,10,'ACTIVE',null);", parse=False)

    def test_new_node_replica_and_idempotent_rerun_preserve_every_existing_identity_and_status(self):
        original = self.pg('SELECT to_jsonb(i) FROM os_images i WHERE node_id=1;')
        config = replace(self.config, notes="$image_registration$ ' quoted operator note")
        evidence = report(config)
        before = self.pg(snapshot_sql(config))
        desired = preview(config, evidence, before)
        created = self.pg(apply_sql(config, before, desired))['image']
        self.assertEqual(created['status'], 'DISABLED')
        self.assertNotEqual(created['public_id'], original['public_id'])
        self.assertEqual(self.pg('SELECT to_jsonb(i) FROM os_images i WHERE node_id=1;'), original)
        self.assertEqual(self.pg("SELECT to_jsonb(status) FROM nodes WHERE id=2;"), 'MAINTENANCE')
        self.assertEqual(self.pg('SELECT to_jsonb(value) FROM unrelated_inventory;'), 'keep')
        self.pg("UPDATE os_images SET status='ACTIVE' WHERE node_id=2;", parse=False)
        config = replace(config, existing_public_id=created['public_id'])
        before = self.pg(snapshot_sql(config))
        desired = preview(config, report(config), before)
        current = self.pg('SELECT to_jsonb(i) FROM os_images i WHERE node_id=2;')
        self.assertEqual(self.pg(apply_sql(config, before, desired))['image'], current)

    def test_legacy_global_uniqueness_is_detected_before_any_registration(self):
        self.pg('ALTER TABLE os_images ADD UNIQUE(name,version);', parse=False)
        before = self.pg(snapshot_sql(self.config))
        self.assertFalse(before['node_unique'])
        with self.assertRaises(RegistrationError):
            preview(self.config, report(self.config), before)
        self.assertEqual(self.pg('SELECT count(*) FROM os_images;'), 1)

    @staticmethod
    def catalog_schema_sql():
        script = (ROOT / 'scripts/apply-os-catalog.sh').read_text()
        start = script.index('    with unique_keys as (')
        end = script.index('    from unique_keys;', start) + len('    from unique_keys;')
        return script[start:end]

    def catalog_schema_mode(self):
        return self.pg(self.catalog_schema_sql(), parse=False).strip()

    def test_catalog_schema_global_only_returns_legacy_global(self):
        self.pg('ALTER TABLE os_images DROP CONSTRAINT os_images_node_id_name_version_key; ALTER TABLE os_images ADD UNIQUE(name,version);', parse=False)
        self.assertEqual(self.catalog_schema_mode(), 'legacy-global')
        self.assertEqual(self.pg(f'SELECT {node_scoped_unique_sql()};', parse=False).strip(), 'f')

    def test_catalog_schema_node_only_returns_node_scoped_and_registration_expression_true(self):
        self.assertEqual(self.catalog_schema_mode(), 'node-scoped')
        self.assertEqual(self.pg(f'SELECT {node_scoped_unique_sql()};', parse=False).strip(), 't')

    def test_catalog_schema_both_returns_both(self):
        self.pg('ALTER TABLE os_images ADD UNIQUE(name,version);', parse=False)
        self.assertEqual(self.catalog_schema_mode(), 'both')
        self.assertEqual(self.pg(f'SELECT {node_scoped_unique_sql()};', parse=False).strip(), 'f')

    def test_catalog_schema_neither_returns_neither(self):
        self.pg('ALTER TABLE os_images DROP CONSTRAINT os_images_node_id_name_version_key;', parse=False)
        self.assertEqual(self.catalog_schema_mode(), 'neither')
        self.assertEqual(self.pg(f'SELECT {node_scoped_unique_sql()};', parse=False).strip(), 'f')

    def test_changed_inventory_after_preview_cannot_be_overwritten_or_partially_registered(self):
        before = self.pg(snapshot_sql(self.config))
        desired = preview(self.config, report(self.config), before)
        self.pg("UPDATE os_images SET notes='changed meanwhile' WHERE node_id=1;", parse=False)
        result = self.pg(apply_sql(self.config, before, desired), success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pg('SELECT count(*) FROM os_images;'), 1)
        self.assertEqual(self.pg('SELECT to_jsonb(notes) FROM os_images WHERE node_id=1;'), 'changed meanwhile')

    def test_apply_locks_image_revision_before_node(self):
        sql = apply_sql(self.config, snapshot(self.config), preview(self.config, report(self.config), snapshot(self.config)))
        self.assertLess(sql.index('PERFORM id FROM public.os_images'), sql.index('SELECT * INTO n FROM public.nodes'))


if __name__ == '__main__':
    unittest.main()
