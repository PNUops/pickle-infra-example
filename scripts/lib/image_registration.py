"""Register one measured template without moving catalog rows or changing other inventory."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
from urllib.parse import urlsplit
import uuid

from node_registration import (RegistrationError, Runner, identity_sql, json_literal, literal,
                               positive_int, require, utc_now, write_private)


@dataclass(frozen=True)
class Config:
    node: str
    node_public_id: str
    cluster: str
    api_url: str
    storage: str
    bridge_mtu: int
    template_vmid: int
    name: str
    version: int
    display_name: str
    os_family: str
    os_version: str
    ssh_username: str
    min_disk_gb: int
    notes: str | None
    build_manifest_file: str | None
    database: str
    database_hostname: str
    database_system_identifier: str
    database_socket_dir: str
    existing_public_id: str | None

    @classmethod
    def from_dict(cls, value: dict) -> Config:
        require(type(value) is dict and set(value) == {field.name for field in fields(cls)},
                'Configuration fields differ from the documented shape')
        config = cls(**value)
        for name in ('node', 'cluster', 'storage', 'os_family', 'database_hostname'):
            require(isinstance(getattr(config, name), str) and
                    re.fullmatch(r'[a-z][a-z0-9-]{0,62}', getattr(config, name)) is not None, f'Invalid {name}')
        require(isinstance(config.name, str) and re.fullmatch(r'[a-z][a-z0-9._-]{0,63}', config.name) is not None,
                'Invalid image name')
        for name in ('database', 'ssh_username'):
            require(isinstance(getattr(config, name), str) and
                    re.fullmatch(r'[a-z_][a-z0-9_-]{0,62}', getattr(config, name)) is not None, f'Invalid {name}')
        require(re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', config.os_version) is not None, 'Invalid OS release')
        require(isinstance(config.display_name, str) and 0 < len(config.display_name) <= 128 and
                not any(ord(char) < 32 for char in config.display_name), 'Invalid display name')
        require(config.notes is None or isinstance(config.notes, str) and len(config.notes) <= 4096, 'Invalid notes')
        for name in ('version', 'min_disk_gb', 'template_vmid'):
            positive_int(getattr(config, name), name)
            require(getattr(config, name) <= 2147483647, f'{name} exceeds its database range')
        require(100 <= config.template_vmid < 100000, 'Template VMID must be outside the managed guest sequence')
        require(type(config.bridge_mtu) is int and 1280 <= config.bridge_mtu <= 1500, 'Invalid bridge MTU')
        for value in (config.node_public_id, config.existing_public_id):
            if value is not None:
                require(str(uuid.UUID(value)) == value, 'Expected a canonical public UUID')
        require(config.node_public_id is not None, 'An existing node UUID is required')
        url = urlsplit(config.api_url)
        require(url.scheme == 'https' and url.hostname and url.port == 8006 and
                not url.username and not url.password and not url.path and not url.query and not url.fragment,
                'Expected an HTTPS node URL at port 8006 without credentials or a path')
        require(re.fullmatch(r'[0-9]{10,20}', config.database_system_identifier) is not None,
                'The observed PostgreSQL system identifier is required')
        for value in (config.database_socket_dir, config.build_manifest_file):
            if value is not None:
                require(isinstance(value, str) and Path(value).is_absolute() and
                        not any(char in value for char in '\x00\n\r'), 'Expected an absolute file or socket path')
        return config


def read_manifest(config: Config) -> dict | None:
    if config.build_manifest_file is None:
        return None
    descriptor = os.open(config.build_manifest_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        require(stat.S_ISREG(metadata.st_mode) and 0 < metadata.st_size <= 1024 * 1024, 'Invalid build manifest file')
        raw = source.read(1024 * 1024 + 1)
    require(len(raw) <= 1024 * 1024, 'Build manifest grew beyond its allowed size')
    value = json.loads(raw)
    require(type(value) is dict and value.get('templateVmid') == config.template_vmid and
            (value.get('osFamily'), value.get('osVersion'), value.get('ciUser')) ==
            (config.os_family, config.os_version, config.ssh_username), 'Build manifest identifies another template')
    algorithm = value.get('checksumAlgorithm')
    checksum = value.get('imageChecksum')
    require(algorithm in ('sha256', 'sha512') and isinstance(checksum, str) and
            re.fullmatch(r'[0-9a-fA-F]{' + str(64 if algorithm == 'sha256' else 128) + r'}', checksum) is not None,
            'Build manifest has no supported source-image checksum')
    require(isinstance(value.get('recipeRevision'), str) and
            re.fullmatch(r'[0-9a-f]{7,40}(?:-modified)?', value['recipeRevision']) is not None,
            'Build manifest must identify its recipe revision')
    require(datetime.fromisoformat(value['builtAt']).tzinfo is not None, 'Build timestamp requires a timezone')
    return {'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'source_checksum_algorithm': algorithm,
            'source_image_checksum': checksum.lower(), 'recipe_revision': value['recipeRevision'],
            'built_at': value['builtAt'], 'scope': 'Upstream image checksum and recipe record; not a final PVE disk hash'}


def collect(config: Config, runner: Runner, now: datetime | None = None) -> dict:
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == config.node,
            'Collect as root on the exact expected Proxmox node')
    cluster = runner.api('/cluster/status')
    require(any(row.get('type') == 'cluster' and row.get('name') == config.cluster and row.get('quorate') == 1
                for row in cluster), 'The expected cluster is not quorate')
    guests = runner.api('/cluster/resources', '--type', 'vm')
    candidates = [row for row in guests if row.get('vmid') == config.template_vmid]
    require(len(candidates) == 1 and candidates[0].get('type') == 'qemu' and
            candidates[0].get('node') == config.node and candidates[0].get('template') == 1 and
            candidates[0].get('status') == 'stopped', 'The VMID is not one stopped template on the expected node')
    template = runner.api(f'/nodes/{config.node}/qemu/{config.template_vmid}/config')
    require(template.get('template') == 1 and not template.get('lock'), 'Template is not stable for registration')
    net0 = template.get('net0', '')
    require(isinstance(net0, str), 'Template NIC is absent')
    pairs = [part.split('=', 1) for part in net0.split(',')]
    require(all(len(pair) == 2 for pair in pairs) and len({pair[0] for pair in pairs}) == len(pairs),
            'Template NIC has ambiguous options')
    nic = dict(pairs)
    require(nic.get('mtu') == str(config.bridge_mtu) and nic.get('firewall') == '1',
            'Template NIC must carry the declared MTU and firewall=1')
    disk = template.get('scsi0', '')
    require(isinstance(disk, str) and disk.startswith(config.storage + ':') and 'media=cdrom' not in disk,
            'Template scsi0 must be a disk on the registered node storage')
    volume_id = disk.split(',', 1)[0]
    volumes = runner.api(f'/nodes/{config.node}/storage/{config.storage}/content', '--vmid', str(config.template_vmid))
    matching = [volume for volume in volumes if volume.get('volid') == volume_id]
    require(len(matching) == 1 and matching[0].get('content') == 'images', 'Template disk volume is ambiguous')
    size = positive_int(matching[0].get('size'), 'template disk bytes')
    require(config.min_disk_gb * 1024**3 >= size, 'Declared minimum disk is smaller than the actual template disk')
    require(re.fullmatch(r'[0-9a-f]{40,64}', template.get('digest', '')) is not None, 'Template configuration has no digest')
    measured = now or utc_now()
    return {'schema_version': 1, 'config': asdict(config), 'measured_at': measured.isoformat(),
            'template': {'vmid': config.template_vmid, 'node': config.node, 'template': True,
                         'stopped': True, 'config_digest': template['digest'], 'net0': net0,
                         'root_volume': volume_id, 'root_volume_bytes': size},
            'build_provenance': read_manifest(config), 'mutations': 'none'}


def validate_report(report: dict, now: datetime | None = None) -> Config:
    require(type(report) is dict and set(report) == {'schema_version', 'config', 'measured_at', 'template',
                                                    'build_provenance', 'mutations'} and
            type(report['schema_version']) is int and report['schema_version'] == 1 and report['mutations'] == 'none',
            'Invalid template collection report')
    config = Config.from_dict(report['config'])
    measured = datetime.fromisoformat(report['measured_at'])
    require(measured.tzinfo is not None and 0 <= ((now or utc_now()) - measured).total_seconds() <= 900,
            'Template collection is stale or from the future')
    template = report['template']
    require(template.get('vmid') == config.template_vmid and template.get('node') == config.node and
            template.get('template') is True and template.get('stopped') is True and
            type(template.get('root_volume_bytes')) is int and
            0 < template['root_volume_bytes'] <= config.min_disk_gb * 1024**3 and
            str(template.get('root_volume', '')).startswith(config.storage + ':'), 'Template evidence is inconsistent')
    require(re.fullmatch(r'[0-9a-f]{40,64}', template.get('config_digest', '')) is not None, 'Missing template config digest')
    pairs = [part.split('=', 1) for part in template.get('net0', '').split(',')]
    require(all(len(pair) == 2 for pair in pairs) and len({pair[0] for pair in pairs}) == len(pairs), 'Ambiguous NIC evidence')
    nic = dict(pairs)
    require(nic.get('mtu') == str(config.bridge_mtu) and nic.get('firewall') == '1', 'NIC evidence differs from the declared requirements')
    proof = report['build_provenance']
    require((config.build_manifest_file is None) == (proof is None), 'Build manifest evidence is missing or unexpected')
    if proof is not None:
        require(type(proof) is dict and set(proof) == {'manifest_sha256', 'source_checksum_algorithm',
                'source_image_checksum', 'recipe_revision', 'built_at', 'scope'}, 'Unknown build evidence shape')
        require(re.fullmatch(r'[0-9a-f]{64}', proof['manifest_sha256']) is not None and
                proof['source_checksum_algorithm'] in ('sha256', 'sha512'), 'Invalid source-image checksum evidence')
        require(re.fullmatch(r'[0-9a-f]{' + str(64 if proof['source_checksum_algorithm'] == 'sha256' else 128) + r'}',
                            proof['source_image_checksum']) is not None, 'Invalid source-image checksum length')
        require(re.fullmatch(r'[0-9a-f]{7,40}(?:-modified)?', proof['recipe_revision']) is not None and
                datetime.fromisoformat(proof['built_at']).tzinfo is not None and
                proof['scope'] == 'Upstream image checksum and recipe record; not a final PVE disk hash',
                'Build evidence does not identify the recipe or its limited checksum scope')
    return config


def node_scoped_unique_sql() -> str:
    return """(EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conrelid='public.os_images'::regclass
      AND c.contype='u' AND (SELECT array_agg(a.attname::text ORDER BY a.attname)
       FROM pg_attribute a WHERE a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)) = ARRAY['name','node_id','version']::text[])
      AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conrelid='public.os_images'::regclass
       AND c.contype='u' AND (SELECT array_agg(a.attname::text ORDER BY a.attname)
        FROM pg_attribute a WHERE a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)) = ARRAY['name','version']::text[]))"""


def snapshot_sql(config: Config) -> str:
    return f"""BEGIN READ ONLY;
SET LOCAL statement_timeout='20s';
SET LOCAL search_path=pg_catalog,public;
SELECT jsonb_build_object('identity',{identity_sql()},'node_unique',{node_scoped_unique_sql()},
 'node',(SELECT to_jsonb(n) FROM public.nodes n WHERE n.name={literal(config.node)}),
 'images',(SELECT coalesce(jsonb_agg(to_jsonb(i) ORDER BY i.id),'[]'::jsonb) FROM public.os_images i
    WHERE i.name={literal(config.name)} AND i.version={config.version}));
COMMIT;
"""


def preview(config: Config, report: dict, snapshot: dict) -> dict:
    require(snapshot['identity'] == {'database': config.database, 'user': 'postgres', 'local_socket': True,
            'primary': True, 'system_identifier': config.database_system_identifier}, 'Wrong PostgreSQL identity')
    require(snapshot.get('node_unique') is True, 'Install the node-scoped catalog schema before registration')
    node = snapshot.get('node')
    require(node is not None and node['public_id'] == config.node_public_id and node['status'] == 'MAINTENANCE' and
            node['api_host'] == config.api_url and node['storage'] == config.storage,
            'The exact registered node must be in MAINTENANCE with matching API and storage')
    require(type(node['labels']) is dict and type(node['labels'].get('node_registration')) is dict and
            node['labels']['node_registration'].get('cluster') == config.cluster,
            'Node registration belongs to another cluster')
    nic_requirement = node['labels'].get('vm_nic_requirements')
    require(type(nic_requirement) is dict and nic_requirement == {'schema_version': 1, 'mtu': config.bridge_mtu, 'firewall': True}
            and type(nic_requirement['schema_version']) is int and type(nic_requirement['mtu']) is int
            and nic_requirement['firewall'] is True,
            'Register the node NIC preparation requirements before registering images')
    fields = ('name', 'display_name', 'os_family', 'os_version', 'ssh_username', 'version', 'min_disk_gb', 'notes')
    desired = {field: getattr(config, field) for field in fields}
    desired.update(node_id=node['id'], proxmox_vmid=config.template_vmid, status='DISABLED', public_id=None)
    for image in snapshot['images']:
        require(all(image[field] == desired[field] for field in ('name', 'version', 'os_family', 'os_version', 'ssh_username', 'min_disk_gb')),
                'A replica revision must have the same OS, SSH account and disk floor as its existing catalog identity')
    existing = [image for image in snapshot['images'] if image['node_id'] == node['id']]
    require(len(existing) <= 1, 'Multiple catalog rows claim the same node revision')
    if existing:
        image = existing[0]
        require(image['public_id'] == config.existing_public_id, 'Supply the exact existing image UUID before an idempotent registration')
        require(all(image[field] == desired[field] for field in (*fields, 'node_id', 'proxmox_vmid')),
                'An existing image revision is immutable; register a new revision instead of replacing it')
        desired.update(status=image['status'], public_id=image['public_id'])
    else:
        require(config.existing_public_id is None, 'The explicitly identified image is absent')
    return desired


def apply_sql(config: Config, snapshot: dict, desired: dict) -> str:
    return f"""BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SET LOCAL search_path=pg_catalog,public;
DO $image_registration$
DECLARE n public.nodes%ROWTYPE; current_images jsonb; d jsonb := {json_literal(desired)};
BEGIN
 IF {identity_sql()} IS DISTINCT FROM {json_literal(snapshot['identity'])} OR NOT ({node_scoped_unique_sql()}) THEN
   RAISE EXCEPTION 'Database identity or catalog uniqueness changed';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('image-registration:' || {literal(config.name)} || ':' || {literal(str(config.version))},0));
 PERFORM id FROM public.os_images WHERE name={literal(config.name)} AND version={config.version} ORDER BY id FOR UPDATE;
 SELECT * INTO n FROM public.nodes WHERE name={literal(config.node)} FOR UPDATE;
 IF to_jsonb(n) IS DISTINCT FROM {json_literal(snapshot['node'])} OR n.status::text <> 'MAINTENANCE' THEN
   RAISE EXCEPTION 'Node changed since the preview';
 END IF;
 SELECT coalesce(jsonb_agg(to_jsonb(i) ORDER BY i.id),'[]'::jsonb) INTO current_images FROM public.os_images i
   WHERE i.name={literal(config.name)} AND i.version={config.version};
 IF current_images IS DISTINCT FROM {json_literal(snapshot['images'])} THEN
   RAISE EXCEPTION 'Image revision changed since the preview';
 END IF;
 IF d->>'public_id' IS NULL THEN
   INSERT INTO public.os_images(name,display_name,os_family,os_version,ssh_username,proxmox_vmid,node_id,version,min_disk_gb,status,notes)
   VALUES(d->>'name',d->>'display_name',d->>'os_family',d->>'os_version',d->>'ssh_username',
     (d->>'proxmox_vmid')::integer,n.id,(d->>'version')::integer,(d->>'min_disk_gb')::integer,'DISABLED',d->>'notes');
 END IF;
END
$image_registration$;
SELECT jsonb_build_object('image',to_jsonb(i)) FROM public.os_images i
 WHERE i.node_id=(SELECT id FROM public.nodes WHERE name={literal(config.node)}) AND i.name={literal(config.name)} AND i.version={config.version};
COMMIT;
"""


def register(report: dict, digest: str, runner: Runner, *, apply=False, backup_dir: Path | None = None, now=None) -> dict:
    config = validate_report(report, now)
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == config.database_hostname,
            'Register as root on the exact expected PostgreSQL host')
    snapshot = runner.postgres(config, snapshot_sql(config))
    desired = preview(config, report, snapshot)
    result = {key: desired[key] for key in ('name', 'version', 'node_id', 'proxmox_vmid', 'status', 'public_id')}
    result.update(mode='apply' if apply else 'dry-run', node_status='MAINTENANCE', automatic_activation=False,
                  build_provenance=report['build_provenance'], template_config_digest=report['template']['config_digest'])
    if not apply:
        return result
    require(backup_dir is not None and backup_dir.is_absolute(), 'Apply requires a protected backup directory')
    metadata = backup_dir.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o700,
            'Backup directory must be root-owned 0700 and not a symlink')
    operation = uuid.uuid4().hex
    before = backup_dir / (operation + '-image-before.json')
    write_private(before, {'operation_id': operation, 'inventory_sha256': digest, 'inventory': report,
                           'snapshot': snapshot, 'desired': desired})
    applied = runner.postgres(config, apply_sql(config, snapshot, desired))['image']
    require(all(applied[key] == desired[key] for key in desired if key != 'public_id') and
            (desired['public_id'] is None or applied['public_id'] == desired['public_id']), 'Applied image differs; inspect the saved before record')
    write_private(backup_dir / (operation + '-image-after.json'), {'operation_id': operation, 'image': applied})
    result.update(public_id=applied['public_id'], before_record=str(before))
    return result
