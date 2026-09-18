"""Measured node registration without changing any other platform inventory."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
from urllib.parse import urlsplit
import uuid


class RegistrationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RegistrationError(message)


def positive_int(value, name: str, *, zero: bool = False) -> int:
    require(type(value) is int and value >= (0 if zero else 1), f'Invalid {name}')
    return value


@dataclass(frozen=True)
class Config:
    node: str
    cluster: str
    api_url: str
    api_address: str
    ca_file: str
    bridge: str
    bridge_mtu: int
    storage: str
    pool_name: str
    pool_cidr: str
    pool_gateway: str
    reserve_cpu_threads: int
    reserve_memory_mb: int
    reserve_disk_gb: int
    gpu_node: bool
    database: str
    database_hostname: str
    database_system_identifier: str
    database_socket_dir: str
    existing_public_id: str | None

    @classmethod
    def from_dict(cls, value: dict) -> Config:
        require(type(value) is dict and set(value) == {f.name for f in fields(cls)},
                'Configuration fields differ from the documented shape')
        config = cls(**value)
        config.validate()
        return config

    def validate(self) -> None:
        for name in ('node', 'cluster', 'storage', 'pool_name', 'database_hostname'):
            require(isinstance(getattr(self, name), str) and
                    re.fullmatch(r'[a-z][a-z0-9-]{0,62}', getattr(self, name)) is not None, f'Invalid {name}')
        require(re.fullmatch(r'[a-z][a-z0-9_]{0,62}', self.database) is not None, 'Invalid database name')
        require(re.fullmatch(r'[0-9]{10,20}', self.database_system_identifier) is not None,
                'Supply the expected PostgreSQL system identifier')
        require(re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,14}', self.bridge) is not None, 'Invalid bridge name')
        require(type(self.bridge_mtu) is int and 1280 <= self.bridge_mtu <= 1500, 'Invalid bridge MTU')
        for name in ('ca_file', 'database_socket_dir'):
            value = getattr(self, name)
            require(isinstance(value, str) and Path(value).is_absolute() and not any(c in value for c in '\n\r\x00'),
                    f'{name} must be an absolute path')
        url = urlsplit(self.api_url)
        require(url.scheme == 'https' and url.hostname is not None and url.port == 8006 and
                not url.username and not url.password and not url.path and not url.query and not url.fragment,
                'API URL must be an HTTPS hostname at port 8006 without a path or credentials')
        require(all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', part)
                    for part in url.hostname.split('.')), 'Invalid API TLS hostname')
        ipaddress.IPv4Address(self.api_address)
        network = ipaddress.IPv4Network(self.pool_cidr, strict=True)
        gateway = ipaddress.IPv4Address(self.pool_gateway)
        require(gateway in network and gateway not in (network.network_address, network.broadcast_address),
                'Pool gateway is outside the usable pool addresses')
        for name in ('reserve_cpu_threads', 'reserve_memory_mb', 'reserve_disk_gb'):
            positive_int(getattr(self, name), name, zero=True)
        require(type(self.gpu_node) is bool, 'gpu_node must explicitly be true or false')
        if self.existing_public_id is not None:
            require(str(uuid.UUID(self.existing_public_id)) == self.existing_public_id,
                    'Existing public node identifier must be a canonical UUID')


class Runner:
    def run(self, command: list[str], *, data: str | None = None, timeout: int = 40) -> str:
        environment = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
        result = subprocess.run(command, input=data, text=True, capture_output=True,
                                timeout=timeout, env=environment)
        if result.returncode:
            raise RegistrationError(f'{command[0]} failed with exit {result.returncode}; inspect that command separately')
        return result.stdout.strip()

    def api(self, path: str, *arguments: str):
        return json.loads(self.run(['pvesh', 'get', path, *arguments, '--output-format', 'json']))

    def postgres(self, config: Config, sql: str):
        command = ['runuser', '-u', 'postgres', '--', '/usr/bin/psql', '-h', config.database_socket_dir,
                   '-U', 'postgres', '-d', config.database, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-f', '-']
        output = self.run(command, data=sql, timeout=50)
        return json.loads(output)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def capacity(physical: dict, config: Config, measured_at: str) -> dict:
    require(set(physical) == {'cpu_threads', 'memory_mb', 'disk_gb'}, 'Unexpected capacity dimensions')
    reserved = {'cpu_threads': config.reserve_cpu_threads, 'memory_mb': config.reserve_memory_mb,
                'disk_gb': config.reserve_disk_gb}
    for key, value in physical.items():
        positive_int(value, 'physical ' + key)
        require(reserved[key] < value, f'The {key} reserve leaves no allocatable capacity')
    return {'schema_version': 1, 'physical': physical, 'reserved': reserved,
            'allocatable': {key: physical[key] - reserved[key] for key in physical},
            'measured_at': measured_at}


def collect(config: Config, runner: Runner, now: datetime | None = None) -> dict:
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == config.node,
            'Collect as root on the exact expected Proxmox node')
    rows = runner.api('/cluster/status')
    cluster = [row for row in rows if row.get('type') == 'cluster']
    require(len(cluster) == 1 and cluster[0].get('name') == config.cluster and cluster[0].get('quorate') == 1,
            'The expected cluster must be quorate')
    require(any(row.get('type') == 'node' and row.get('name') == config.node and row.get('online') == 1 for row in rows),
            'The expected node is not online in the cluster')
    links = json.loads(runner.run(['ip', '-j', '-d', 'link', 'show', 'dev', config.bridge]))
    require(len(links) == 1 and links[0].get('linkinfo', {}).get('info_kind') == 'bridge' and
            links[0].get('mtu') == config.bridge_mtu and 'UP' in links[0].get('flags', []),
            'The expected guest bridge and MTU must already be configured and up')
    addresses = json.loads(runner.run(['ip', '-j', '-4', 'address']))
    require(any(item.get('local') == config.api_address for row in addresses for item in row.get('addr_info', [])),
            'The API transport address does not belong to this node')
    # Standby nodes share the pool without owning its gateway address locally.
    status = runner.api(f'/nodes/{config.node}/status')
    storage = runner.api(f'/nodes/{config.node}/storage/{config.storage}/status')
    storage_config = runner.api('/storage/' + config.storage)
    require(storage_config.get('type') == 'lvmthin' and storage.get('active') == 1 and
            storage.get('enabled', 1) == 1, 'The selected storage must be an enabled active LVM thin pool')
    vg, pool = storage_config.get('vgname', ''), storage_config.get('thinpool', '')
    require(all(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_+.-]{0,126}', name) for name in (vg, pool)),
            'Invalid thin-pool identity')
    volume = json.loads(runner.run(['lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix',
                                   '-o', 'vg_name,lv_name,lv_size,segtype,lv_active', vg + '/' + pool]))
    volumes = [row for section in volume.get('report', []) for row in section.get('lv', [])]
    require(len(volumes) == 1 and volumes[0].get('vg_name') == vg and volumes[0].get('lv_name') == pool and
            volumes[0].get('segtype') == 'thin-pool' and volumes[0].get('lv_active') == 'active',
            'The selected PVE storage does not identify one active local thin pool')
    disk_bytes = positive_int(storage.get('total'), 'storage total')
    require(Decimal(volumes[0]['lv_size']) == disk_bytes, 'LVM and PVE disagree on the physical thin-pool size')
    available = positive_int(storage.get('avail'), 'storage available', zero=True)
    require(available >= config.reserve_disk_gb * 1024**3, 'Available thin-pool space is below the required reserve')
    host = urlsplit(config.api_url).hostname
    require(runner.run(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error', '--connect-timeout', '5',
                        '--max-time', '15', '--cacert', config.ca_file, '--resolve',
                        f'{host}:8006:{config.api_address}', config.api_url + '/', '-o', '/dev/null', '-w', '%{http_code}']) == '200',
            'CA and hostname verified Proxmox HTTPS did not return 200')
    measured_at = (now or utc_now()).isoformat()
    physical = {'cpu_threads': positive_int(status.get('cpuinfo', {}).get('cpus'), 'CPU threads'),
                'memory_mb': positive_int(status.get('memory', {}).get('total'), 'memory total') // 1024**2,
                'disk_gb': disk_bytes // 1024**3}
    return {'schema_version': 1, 'config': asdict(config), 'measured_at': measured_at,
            'boot_id': runner.run(['cat', '/proc/sys/kernel/random/boot_id']),
            'placement_capacity': capacity(physical, config, measured_at),
            'thin_pool': {'vg': vg, 'pool': pool, 'total_bytes': disk_bytes, 'available_bytes': available},
            'checks': {'cluster': True, 'local_api_address': True, 'bridge': True, 'thin_pool': True,
                       'ca_hostname_https': True, 'local_gateway_required': False},
            'ca_sha256': hashlib.sha256(Path(config.ca_file).read_bytes()).hexdigest()}


def validate_report(report: dict, now: datetime | None = None) -> Config:
    require(report.get('schema_version') == 1, 'Unsupported collection report')
    config = Config.from_dict(report['config'])
    measured = datetime.fromisoformat(report['measured_at'])
    require(measured.tzinfo is not None and 0 <= ((now or utc_now()) - measured).total_seconds() <= 900,
            'Collect fresh node evidence within 15 minutes before registration')
    require(report['placement_capacity'] == capacity(report['placement_capacity']['physical'], config, report['measured_at']),
            'Capacity does not equal the measured values minus explicit reserves')
    require(report['checks'] == {'cluster': True, 'local_api_address': True, 'bridge': True, 'thin_pool': True,
                                 'ca_hostname_https': True, 'local_gateway_required': False}, 'Collection checks did not all pass')
    require(str(uuid.UUID(report['boot_id'])) == report['boot_id'], 'Invalid collected boot identifier')
    require(re.fullmatch(r'[0-9a-f]{64}', report['ca_sha256']) is not None, 'Missing public CA fingerprint')
    physical = report['placement_capacity']['physical']
    require(physical['cpu_threads'] <= 2147483647 and physical['memory_mb'] <= 2147483647,
            'Capacity exceeds the existing database column range')
    require(report['thin_pool']['total_bytes'] // 1024**3 == physical['disk_gb'] and
            report['thin_pool']['available_bytes'] >= config.reserve_disk_gb * 1024**3,
            'Thin-pool evidence does not cover the disk reserve')
    return config


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def json_literal(value) -> str:
    if value is None:
        return 'NULL::jsonb'
    # Existing labels are arbitrary data and must not close the surrounding DO
    # dollar quote. Base64 keeps them out of both SQL quoting grammars.
    encoded = base64.b64encode(json.dumps(value, separators=(',', ':')).encode()).decode('ascii')
    return "convert_from(decode(" + literal(encoded) + ", 'base64'), 'UTF8')::jsonb"


def identity_sql() -> str:
    return """jsonb_build_object(
      'database', current_database(), 'user', current_user,
      'local_socket', inet_server_addr() IS NULL, 'primary', NOT pg_is_in_recovery(),
      'system_identifier', (SELECT system_identifier::text FROM pg_control_system()))"""


def snapshot_sql(config: Config) -> str:
    return f"""BEGIN READ ONLY;
SET LOCAL statement_timeout = '20s';
SET LOCAL search_path = pg_catalog, public;
SELECT jsonb_build_object('identity', {identity_sql()},
 'pool', (SELECT to_jsonb(p) FROM public.ip_pools p WHERE name = {literal(config.pool_name)}),
 'node', (SELECT to_jsonb(n) FROM public.nodes n WHERE name = {literal(config.node)}),
 'allocated_memory_mb', (SELECT coalesce(sum(v.memory_mb), 0) FROM public.vms v
    JOIN public.nodes n ON n.id = v.node_id WHERE n.name = {literal(config.node)}
    AND v.deleted_at IS NULL AND v.status <> 'DELETED'),
 'allocated_vcpu', (SELECT coalesce(sum(v.vcpu), 0) FROM public.vms v
    JOIN public.nodes n ON n.id = v.node_id WHERE n.name = {literal(config.node)}
    AND v.deleted_at IS NULL AND v.status <> 'DELETED'),
 'allocated_disk_gb', (SELECT coalesce(sum(v.disk_gb), 0) FROM public.vms v
    JOIN public.nodes n ON n.id = v.node_id WHERE n.name = {literal(config.node)}
    AND v.deleted_at IS NULL AND v.status <> 'DELETED'));
COMMIT;
"""


def validate_database(config: Config, identity: dict) -> None:
    require(identity == {'database': config.database, 'user': 'postgres', 'local_socket': True,
                         'primary': True, 'system_identifier': config.database_system_identifier},
            'Wrong PostgreSQL database, system identifier, connection, role or recovery state')


def preview(report: dict, snapshot: dict) -> dict:
    config = Config.from_dict(report['config'])
    validate_database(config, snapshot['identity'])
    pool, old = snapshot.get('pool'), snapshot.get('node')
    require(pool is not None and pool['name'] == config.pool_name and pool['cidr'] == config.pool_cidr and
            str(ipaddress.ip_interface(pool['gateway']).ip) == config.pool_gateway,
            'The existing pool name, CIDR and gateway must match; this tool never creates or edits pools')
    cap = report['placement_capacity']
    labels = {'gpu': config.gpu_node, 'placement_capacity': cap, 'node_registration': {'cluster': config.cluster}}
    nic_requirements = {'schema_version': 1, 'mtu': config.bridge_mtu, 'firewall': True}
    if old is not None:
        require(config.existing_public_id == old['public_id'], 'Supply the exact existing public UUID before re-registering a node')
        require((old['api_host'], old['vm_bridge'], old['storage'], old['ip_pool_id']) ==
                (config.api_url, config.bridge, config.storage, pool['id']),
                'Existing node routing/storage identity differs; refusing an implicit move')
        require(type(old['labels']) is dict, 'Existing node labels are not an object')
        origin = old['labels'].get('node_registration', {})
        require(type(origin) is dict, 'Existing node_registration is not an object; preserve it for review')
        require(not origin or origin.get('cluster') == config.cluster, 'Existing node belongs to another recorded cluster')
        old_capacity = old['labels'].get('placement_capacity', {})
        if 'placement_capacity' in old['labels']:
            validate_capacity_document(old_capacity)
        if 'vm_nic_requirements' in old['labels']:
            require(old['labels']['vm_nic_requirements'] == nic_requirements and
                    type(old['labels']['vm_nic_requirements'].get('schema_version')) is int and
                    type(old['labels']['vm_nic_requirements'].get('mtu')) is int and
                    old['labels']['vm_nic_requirements'].get('firewall') is True,
                    'Existing VM NIC requirements differ; re-registration does not change them')
        old_gpu = old['labels'].get('gpu', False)
        require(type(old_gpu) is bool and old_gpu == config.gpu_node,
                'The declared GPU node role differs; re-registration does not change an existing role')
        before = {key: value for key, value in old_capacity.items() if key != 'measured_at'}
        after = {key: value for key, value in cap.items() if key != 'measured_at'}
        unchanged = before == after and (old['cpu_threads'], old['memory_mb'], old['disk_capacity_gb']) == (
            cap['physical']['cpu_threads'], cap['allocatable']['memory_mb'], cap['physical']['disk_gb'])
        require(old['status'] != 'ACTIVE' or unchanged, 'Park the node in MAINTENANCE before changing placement reserves or capacity')
        require(snapshot['allocated_memory_mb'] <= cap['allocatable']['memory_mb'],
                'Registered VM memory exceeds the proposed allocatable memory')
        require(snapshot['allocated_vcpu'] <= cap['allocatable']['cpu_threads'],
                'Registered VM CPU exceeds the proposed allocatable CPU')
        require(snapshot['allocated_disk_gb'] <= cap['allocatable']['disk_gb'],
                'Registered VM disks exceed the proposed allocatable disk budget')
        labels['node_registration'] = {**origin, 'cluster': config.cluster}
        labels = {**old['labels'], **labels}
    else:
        require(config.existing_public_id is None, 'The explicitly identified existing node is absent')
        # Only new nodes receive the new preparation requirement; old labels are never silently enabled.
        labels['vm_nic_requirements'] = nic_requirements
    return {'name': config.node, 'api_host': config.api_url, 'status': old['status'] if old else 'MAINTENANCE',
            'cpu_threads': cap['physical']['cpu_threads'], 'memory_mb': cap['allocatable']['memory_mb'],
            'disk_capacity_gb': cap['physical']['disk_gb'], 'vm_bridge': config.bridge, 'storage': config.storage,
            'ip_pool_id': pool['id'], 'labels': labels,
            'public_id': old['public_id'] if old else None}


def validate_capacity_document(value) -> None:
    require(type(value) is dict and set(value) == {'schema_version', 'physical', 'reserved', 'allocatable', 'measured_at'} and
            type(value.get('schema_version')) is int and value['schema_version'] == 1,
            'Unknown stored placement_capacity schema; preserve it for review')
    dimensions = {'cpu_threads', 'memory_mb', 'disk_gb'}
    for group in ('physical', 'reserved', 'allocatable'):
        require(type(value[group]) is dict and set(value[group]) == dimensions,
                'Malformed stored placement_capacity dimensions; preserve them for review')
        for key, number in value[group].items():
            positive_int(number, 'stored ' + group + ' ' + key, zero=group == 'reserved')
    require(all(value['physical'][key] - value['reserved'][key] == value['allocatable'][key] for key in dimensions),
            'Stored placement_capacity has an inconsistent reservation')
    require(isinstance(value['measured_at'], str), 'Stored placement capacity has no measurement time')
    require(datetime.fromisoformat(value['measured_at']).tzinfo is not None,
            'Stored placement capacity time must include its timezone')


def apply_sql(config: Config, snapshot: dict, desired: dict) -> str:
    """Lock and compare the exact reviewed rows before the only table write."""
    expected = json_literal(snapshot.get('node'))
    return f"""BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SET LOCAL standard_conforming_strings = on;
SET LOCAL search_path = pg_catalog, public;
DO $node_registration$
DECLARE p public.ip_pools%ROWTYPE; n public.nodes%ROWTYPE; current_node jsonb;
BEGIN
  IF {identity_sql()} IS DISTINCT FROM {json_literal(snapshot['identity'])} THEN
    RAISE EXCEPTION 'PostgreSQL identity changed before apply';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('node-registration:' || {literal(config.node)}, 0));
  SELECT * INTO p FROM public.ip_pools WHERE name = {literal(config.pool_name)} FOR SHARE;
  IF to_jsonb(p) IS DISTINCT FROM {json_literal(snapshot['pool'])} THEN
    RAISE EXCEPTION 'IP pool changed since the preview';
  END IF;
  SELECT * INTO n FROM public.nodes WHERE name = {literal(config.node)} FOR UPDATE;
  current_node := CASE WHEN n.id IS NULL THEN NULL ELSE to_jsonb(n) END;
  IF current_node IS DISTINCT FROM {expected} THEN
    RAISE EXCEPTION 'Node changed since the preview';
  END IF;
  IF n.id IS NOT NULL AND (SELECT coalesce(sum(memory_mb),0) FROM public.vms
       WHERE node_id=n.id AND deleted_at IS NULL AND status <> 'DELETED') > {desired['memory_mb']} THEN
    RAISE EXCEPTION 'VM memory changed beyond the reserved capacity';
  END IF;
  IF n.id IS NOT NULL AND (SELECT coalesce(sum(vcpu),0) FROM public.vms
       WHERE node_id=n.id AND deleted_at IS NULL AND status <> 'DELETED') > {desired['labels']['placement_capacity']['allocatable']['cpu_threads']} THEN
    RAISE EXCEPTION 'VM CPU changed beyond the reserved capacity';
  END IF;
  IF n.id IS NOT NULL AND (SELECT coalesce(sum(disk_gb),0) FROM public.vms
       WHERE node_id=n.id AND deleted_at IS NULL AND status <> 'DELETED') > {desired['labels']['placement_capacity']['allocatable']['disk_gb']} THEN
    RAISE EXCEPTION 'VM disks changed beyond the reserved capacity';
  END IF;
  IF n.id IS NULL THEN
    INSERT INTO public.nodes(name,api_host,status,cpu_threads,memory_mb,disk_capacity_gb,vm_bridge,storage,ip_pool_id,labels)
    VALUES ({literal(config.node)},{literal(config.api_url)},'MAINTENANCE',{desired['cpu_threads']},
      {desired['memory_mb']},{desired['disk_capacity_gb']},{literal(config.bridge)},{literal(config.storage)},p.id,{json_literal(desired['labels'])});
  ELSE
    UPDATE public.nodes SET cpu_threads={desired['cpu_threads']}, memory_mb={desired['memory_mb']},
      disk_capacity_gb={desired['disk_capacity_gb']}, labels={json_literal(desired['labels'])}, updated_at=now()
      WHERE id=n.id;
  END IF;
END
$node_registration$;
SELECT jsonb_build_object('node', to_jsonb(n)) FROM public.nodes n WHERE name={literal(config.node)};
COMMIT;
"""


def write_private(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def register(report: dict, report_digest: str, runner: Runner, *, apply: bool = False,
             backup_dir: Path | None = None, now: datetime | None = None) -> dict:
    config = validate_report(report, now)
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == config.database_hostname,
            'Register as root on the exact expected PostgreSQL host')
    snapshot = runner.postgres(config, snapshot_sql(config))
    desired = preview(report, snapshot)
    summary = {key: desired[key] for key in ('name', 'public_id', 'status', 'cpu_threads', 'memory_mb', 'disk_capacity_gb')}
    summary.update({'mode': 'apply' if apply else 'dry-run', 'gpu_node': config.gpu_node,
                    'placement_capacity': desired['labels']['placement_capacity'],
                    'vm_nic_requirements': desired['labels'].get('vm_nic_requirements'),
                    'image_registration': False, 'automatic_activation': False,
                    'boundary': 'CPU and disk reservations require the placement consumer to honor allocatable labels before activation. Disk capacity remains a physical advisory total.'})
    if not apply:
        return summary
    require(backup_dir is not None and backup_dir.is_absolute(), 'Apply requires an explicit protected backup directory')
    info = backup_dir.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
            'The backup directory must be owned by root with mode 0700 and cannot be a symlink')
    operation = uuid.uuid4().hex
    before_file = backup_dir / (operation + '-before.json')
    write_private(before_file, {'operation_id': operation, 'inventory_sha256': report_digest,
                                'snapshot': snapshot, 'requested': desired, 'phase': 'before_apply'})
    applied = runner.postgres(config, apply_sql(config, snapshot, desired))
    require(applied['node']['status'] == desired['status'] and
            (desired['public_id'] is None or applied['node']['public_id'] == desired['public_id']),
            'Apply result identity/status differs; inspect the saved before record and do not retry blindly')
    write_private(backup_dir / (operation + '-after.json'), {'operation_id': operation, 'node': applied['node'], 'phase': 'applied'})
    summary['public_id'] = applied['node']['public_id']
    summary['before_record'] = str(before_file)
    return summary
