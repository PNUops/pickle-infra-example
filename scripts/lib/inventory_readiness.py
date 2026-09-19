"""Narrow database writers for an IP pool and VM firewall node opt-in."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import ipaddress
import os
from pathlib import Path
import re
import socket
import stat
import uuid
from urllib.parse import urlsplit

from node_registration import (RegistrationError, Runner, identity_sql, json_literal, literal,
                               require, write_private)


def _canonical_uuid(value: str, label: str) -> str:
    require(isinstance(value, str) and str(uuid.UUID(value)) == value, f'{label} must be a canonical UUID')
    return value


def _database_fields(value) -> None:
    require(isinstance(value.database, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value.database) is not None,
            'Invalid database name')
    require(isinstance(value.database_hostname, str) and
            re.fullmatch(r'[a-z][a-z0-9-]{0,62}', value.database_hostname) is not None,
            'Invalid database hostname')
    require(isinstance(value.database_system_identifier, str) and
            re.fullmatch(r'[0-9]{10,20}', value.database_system_identifier) is not None,
            'Supply the expected PostgreSQL system identifier')
    require(isinstance(value.database_socket_dir, str) and Path(value.database_socket_dir).is_absolute() and
            not any(char in value.database_socket_dir for char in '\x00\r\n'),
            'Database socket directory must be an absolute path')


def _readiness_identity_sql() -> str:
    return "(" + identity_sql() + " || jsonb_build_object('server_version_num'," \
           "current_setting('server_version_num')::integer))"


def _database_identity(config, actual: dict) -> None:
    require(actual == {'database': config.database, 'user': 'postgres', 'local_socket': True,
                       'primary': True, 'system_identifier': config.database_system_identifier,
                       'server_version_num': actual.get('server_version_num')} and
            type(actual.get('server_version_num')) is int and
            180000 <= actual['server_version_num'] < 190000,
            'Expected PostgreSQL 18 with the reviewed database, system identifier, local connection, postgres role and primary state')


def _protected_backup(path: Path | None) -> Path:
    require(path is not None and path.is_absolute(), 'Apply requires an explicit protected backup directory')
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
            'The backup directory must be owned by root with mode 0700 and cannot be a symlink')
    return path


def _on_database_host(config) -> None:
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == config.database_hostname,
            'Run as root on the exact expected PostgreSQL host')


def _sorted_ranges(value: list[dict], network: ipaddress.IPv4Network) -> list[dict]:
    require(type(value) is list, 'reserved_ranges must be an array')
    parsed: list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]] = []
    for item in value:
        require(type(item) is dict and set(item) == {'from', 'to'}, 'Each reserved range needs only from and to')
        start, end = ipaddress.IPv4Address(item['from']), ipaddress.IPv4Address(item['to'])
        require(start in network and end in network and
                start not in (network.network_address, network.broadcast_address) and
                end not in (network.network_address, network.broadcast_address) and start <= end,
                'Reserved ranges must be ordered usable addresses inside the pool')
        parsed.append((start, end))
    parsed.sort()
    require(all(parsed[index - 1][1] < parsed[index][0] for index in range(1, len(parsed))),
            'Reserved ranges overlap')
    return [{'from': str(start), 'to': str(end)} for start, end in parsed]


@dataclass(frozen=True)
class IpPoolConfig:
    name: str
    cidr: str
    gateway: str
    dns: list[str]
    reserved_ranges: list[dict]
    database: str
    database_hostname: str
    database_system_identifier: str
    database_socket_dir: str
    existing_public_id: str | None

    @classmethod
    def from_dict(cls, value: dict) -> 'IpPoolConfig':
        require(type(value) is dict and set(value) == {field.name for field in fields(cls)},
                'Configuration fields differ from the documented shape')
        config = cls(**value)
        config.validate()
        return config

    def validate(self) -> None:
        require(isinstance(self.name, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,62}', self.name) is not None,
                'Invalid pool name')
        network = ipaddress.IPv4Network(self.cidr, strict=True)
        require(str(network) == self.cidr, 'Pool CIDR must use canonical prefix notation')
        require(network.prefixlen <= 30, 'Pool must contain usable IPv4 addresses')
        gateway = ipaddress.IPv4Address(self.gateway)
        require(str(gateway) == self.gateway and gateway in network and
                gateway not in (network.network_address, network.broadcast_address),
                'Gateway is outside the usable pool addresses')
        require(type(self.dns) is list and 1 <= len(self.dns) <= 8, 'Supply one to eight DNS addresses')
        canonical_dns = [str(ipaddress.IPv4Address(address)) for address in self.dns]
        require(canonical_dns == self.dns and len(set(canonical_dns)) == len(canonical_dns),
                'DNS addresses must be canonical, unique IPv4 addresses')
        require(_sorted_ranges(self.reserved_ranges, network) == self.reserved_ranges,
                'Reserved ranges must be canonical and ordered')
        _database_fields(self)
        if self.existing_public_id is not None:
            _canonical_uuid(self.existing_public_id, 'Existing pool public identifier')

    def desired(self) -> dict:
        return {'name': self.name, 'cidr': self.cidr, 'gateway': self.gateway,
                'dns': self.dns, 'reserved_ranges': self.reserved_ranges}


def ip_pool_snapshot_sql(config: IpPoolConfig) -> str:
    return f"""BEGIN READ ONLY;
SET LOCAL statement_timeout='20s';
SET LOCAL search_path=pg_catalog,public;
SELECT jsonb_build_object('identity',{_readiness_identity_sql()},
 'pool',(SELECT to_jsonb(p) FROM public.ip_pools p WHERE p.name={literal(config.name)}),
 'pools',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',p.id,'public_id',p.public_id,
    'name',p.name,'cidr',p.cidr) ORDER BY p.id),'[]'::jsonb) FROM public.ip_pools p),
 'nodes',(SELECT coalesce(jsonb_agg(to_jsonb(n) ORDER BY n.id),'[]'::jsonb) FROM public.nodes n
    WHERE n.ip_pool_id=(SELECT p.id FROM public.ip_pools p WHERE p.name={literal(config.name)})),
 'allocations',(SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY a.id),'[]'::jsonb)
    FROM public.ip_allocations a WHERE a.pool_id=(SELECT p.id FROM public.ip_pools p
      WHERE p.name={literal(config.name)})),
 'addresses_in_cidr',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',a.id,'pool_id',a.pool_id,'ip',a.ip)
    ORDER BY a.id),'[]'::jsonb) FROM public.ip_allocations a WHERE a.ip << {literal(config.cidr)}::cidr));
COMMIT;
"""


def preview_ip_pool(config: IpPoolConfig, snapshot: dict) -> dict:
    _database_identity(config, snapshot['identity'])
    desired_network = ipaddress.IPv4Network(config.cidr)
    for pool in snapshot['pools']:
        network = ipaddress.IPv4Network(pool['cidr'])
        if pool['name'] != config.name:
            require(not network.overlaps(desired_network),
                    'The requested CIDR overlaps another registered pool')
    pool = snapshot.get('pool')
    if pool is None:
        require(config.existing_public_id is None, 'The explicitly identified existing pool is absent')
        require(not snapshot['addresses_in_cidr'], 'Existing IP allocations occupy the requested CIDR')
        return {**config.desired(), 'public_id': None, 'mode': 'insert'}
    require(config.existing_public_id == pool['public_id'],
            'Supply the exact existing pool public UUID for a no-op confirmation')
    actual = {'name': pool['name'], 'cidr': pool['cidr'],
              'gateway': str(ipaddress.ip_interface(pool['gateway']).ip),
              'dns': pool['dns'], 'reserved_ranges': pool['reserved_ranges']}
    require(actual == config.desired(), 'Existing pool configuration differs; this tool never edits a pool')
    require(all(row['pool_id'] == pool['id'] for row in snapshot['addresses_in_cidr']),
            'An allocation from another pool overlaps this pool CIDR')
    return {**actual, 'public_id': pool['public_id'], 'mode': 'noop'}


def ip_pool_apply_sql(config: IpPoolConfig, snapshot: dict, desired: dict) -> str:
    return f"""BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SET LOCAL standard_conforming_strings=on;
SET LOCAL search_path=pg_catalog,public;
DO $ip_pool_registration$
DECLARE current_pool jsonb; current_pools jsonb; current_nodes jsonb; current_allocations jsonb;
BEGIN
  IF {_readiness_identity_sql()} IS DISTINCT FROM {json_literal(snapshot['identity'])} THEN
    RAISE EXCEPTION 'PostgreSQL identity changed before apply';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('ip-pool-registration',0));
  PERFORM 1 FROM public.ip_pools ORDER BY id FOR SHARE;
  SELECT to_jsonb(p) INTO current_pool FROM public.ip_pools p WHERE p.name={literal(config.name)} FOR UPDATE;
  SELECT coalesce(jsonb_agg(jsonb_build_object('id',p.id,'public_id',p.public_id,'name',p.name,'cidr',p.cidr)
    ORDER BY p.id),'[]'::jsonb) INTO current_pools FROM public.ip_pools p;
  IF current_pool IS DISTINCT FROM {json_literal(snapshot.get('pool'))}
     OR current_pools IS DISTINCT FROM {json_literal(snapshot['pools'])} THEN
    RAISE EXCEPTION 'IP pool inventory changed since preview';
  END IF;
  SELECT coalesce(jsonb_agg(to_jsonb(n) ORDER BY n.id),'[]'::jsonb) INTO current_nodes
    FROM public.nodes n WHERE n.ip_pool_id=(SELECT p.id FROM public.ip_pools p WHERE p.name={literal(config.name)});
  SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY a.id),'[]'::jsonb) INTO current_allocations
    FROM public.ip_allocations a WHERE a.pool_id=(SELECT p.id FROM public.ip_pools p WHERE p.name={literal(config.name)});
  IF current_nodes IS DISTINCT FROM {json_literal(snapshot['nodes'])}
     OR current_allocations IS DISTINCT FROM {json_literal(snapshot['allocations'])} THEN
    RAISE EXCEPTION 'Pool references changed since preview';
  END IF;
  IF current_pool IS NULL THEN
    IF EXISTS (SELECT 1 FROM public.ip_allocations a WHERE a.ip << {literal(config.cidr)}::cidr) THEN
      RAISE EXCEPTION 'Existing allocation entered the requested CIDR';
    END IF;
    INSERT INTO public.ip_pools(name,cidr,gateway,dns,reserved_ranges)
    VALUES ({literal(config.name)},{literal(config.cidr)}::cidr,{literal(config.gateway)}::inet,
      {json_literal(config.dns)},{json_literal(config.reserved_ranges)});
  END IF;
END
$ip_pool_registration$;
SELECT jsonb_build_object('pool',to_jsonb(p),'node_count',(SELECT count(*) FROM public.nodes n WHERE n.ip_pool_id=p.id),
 'allocation_count',(SELECT count(*) FROM public.ip_allocations a WHERE a.pool_id=p.id))
FROM public.ip_pools p WHERE p.name={literal(config.name)};
COMMIT;
"""


def register_ip_pool(config: IpPoolConfig, runner: Runner, *, apply: bool = False,
                     backup_dir: Path | None = None) -> dict:
    _on_database_host(config)
    snapshot = runner.postgres(config, ip_pool_snapshot_sql(config))
    desired = preview_ip_pool(config, snapshot)
    summary = {**desired, 'operation': 'ip-pool-registration',
               'apply': apply, 'nodes_preserved': len(snapshot['nodes']),
               'allocations_preserved': len(snapshot['allocations'])}
    if not apply:
        return summary
    directory = _protected_backup(backup_dir)
    operation = uuid.uuid4().hex
    before = directory / f'{operation}-before.json'
    write_private(before, {'operation_id': operation, 'operation': 'ip-pool-registration',
                           'config': asdict(config), 'snapshot': snapshot,
                           'requested': desired, 'phase': 'before_apply'})
    applied = runner.postgres(config, ip_pool_apply_sql(config, snapshot, desired))
    require(applied['pool']['name'] == config.name and applied['pool']['cidr'] == config.cidr and
            applied['node_count'] == len(snapshot['nodes']) and
            applied['allocation_count'] == len(snapshot['allocations']),
            'Apply result differs; inspect the saved before record and do not retry blindly')
    write_private(directory / f'{operation}-after.json',
                  {'operation_id': operation, 'operation': 'ip-pool-registration',
                   'result': applied, 'phase': 'applied'})
    summary['public_id'] = applied['pool']['public_id']
    summary['before_record'] = str(before)
    return summary


@dataclass(frozen=True)
class VmFirewallNodeConfig:
    node: str
    node_public_id: str
    api_host: str
    bridge: str
    storage: str
    bridge_mtu: int
    capability_evidence_id: str
    database: str
    database_hostname: str
    database_system_identifier: str
    database_socket_dir: str

    @classmethod
    def from_dict(cls, value: dict) -> 'VmFirewallNodeConfig':
        require(type(value) is dict and set(value) == {field.name for field in fields(cls)},
                'Configuration fields differ from the documented shape')
        config = cls(**value)
        config.validate()
        return config

    def validate(self) -> None:
        for name in ('node', 'storage'):
            require(isinstance(getattr(self, name), str) and
                    re.fullmatch(r'[a-z][a-z0-9-]{0,62}', getattr(self, name)) is not None,
                    f'Invalid {name}')
        require(isinstance(self.bridge, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,14}', self.bridge),
                'Invalid bridge')
        url = urlsplit(self.api_host)
        require(url.scheme == 'https' and url.hostname is not None and url.port == 8006 and
                not url.username and not url.password and not url.path and not url.query and not url.fragment,
                'Expected an HTTPS Proxmox API hostname at port 8006 without credentials or a path')
        require(all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', part)
                    for part in url.hostname.split('.')), 'Invalid Proxmox API hostname')
        require(type(self.bridge_mtu) is int and 1280 <= self.bridge_mtu <= 1500, 'Invalid bridge MTU')
        _canonical_uuid(self.node_public_id, 'Node public identifier')
        require(isinstance(self.capability_evidence_id, str) and
                re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:-]{7,127}', self.capability_evidence_id) is not None and
                self.capability_evidence_id.lower() not in {'placeholder', 'changeme', 'example-evidence'},
                'Supply the reviewed live capability evidence identifier')
        _database_fields(self)


def vm_firewall_node_snapshot_sql(config: VmFirewallNodeConfig) -> str:
    return f"""BEGIN READ ONLY;
SET LOCAL statement_timeout='20s';
SET LOCAL search_path=pg_catalog,public;
SELECT jsonb_build_object('identity',{_readiness_identity_sql()},
 'node',(SELECT to_jsonb(n) FROM public.nodes n WHERE n.name={literal(config.node)}));
COMMIT;
"""


def preview_vm_firewall_node(config: VmFirewallNodeConfig, snapshot: dict) -> dict:
    _database_identity(config, snapshot['identity'])
    node = snapshot.get('node')
    require(node is not None and node['public_id'] == config.node_public_id and
            node['api_host'] == config.api_host and node['vm_bridge'] == config.bridge and
            node['storage'] == config.storage, 'Exact node UUID, API, bridge and storage must match')
    require(node['status'] == 'MAINTENANCE', 'Park the node in MAINTENANCE before firewall opt-in')
    require(type(node['labels']) is dict, 'Node labels must be an object')
    nic = node['labels'].get('vm_nic_requirements')
    require(nic == {'schema_version': 1, 'mtu': config.bridge_mtu, 'firewall': True} and
            type(nic.get('schema_version')) is int and type(nic.get('mtu')) is int and nic.get('firewall') is True,
            'Node VM NIC requirements differ from the reviewed capability')
    policy_present = 'vm_firewall_policy' in node['labels']
    existing = node['labels'].get('vm_firewall_policy')
    require(not policy_present or existing == {'schema_version': 1} and
            type(existing.get('schema_version')) is int,
            'Existing VM firewall policy label has an unknown shape')
    labels = {**node['labels'], 'vm_firewall_policy': {'schema_version': 1}}
    return {'node': config.node, 'public_id': config.node_public_id, 'status': 'MAINTENANCE',
            'labels': labels, 'mode': 'noop' if policy_present else 'label-add',
            'capability_evidence_id': config.capability_evidence_id}


def vm_firewall_node_apply_sql(config: VmFirewallNodeConfig, snapshot: dict, desired: dict) -> str:
    return f"""BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SET LOCAL standard_conforming_strings=on;
SET LOCAL search_path=pg_catalog,public;
DO $vm_firewall_node$
DECLARE n public.nodes%ROWTYPE;
BEGIN
  IF {_readiness_identity_sql()} IS DISTINCT FROM {json_literal(snapshot['identity'])} THEN
    RAISE EXCEPTION 'PostgreSQL identity changed before apply';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('vm-firewall-node:' || {literal(config.node)},0));
  SELECT * INTO n FROM public.nodes WHERE name={literal(config.node)} FOR UPDATE;
  IF to_jsonb(n) IS DISTINCT FROM {json_literal(snapshot['node'])} THEN
    RAISE EXCEPTION 'Node changed since preview';
  END IF;
  IF n.status::text <> 'MAINTENANCE' OR n.public_id::text <> {literal(config.node_public_id)}
     OR n.api_host <> {literal(config.api_host)} OR n.vm_bridge <> {literal(config.bridge)}
     OR n.storage <> {literal(config.storage)} THEN
    RAISE EXCEPTION 'Node identity or parked state changed';
  END IF;
  IF n.labels IS DISTINCT FROM {json_literal(desired['labels'])} THEN
    UPDATE public.nodes SET labels={json_literal(desired['labels'])},updated_at=now() WHERE id=n.id;
  END IF;
END
$vm_firewall_node$;
SELECT jsonb_build_object('node',to_jsonb(n)) FROM public.nodes n WHERE n.name={literal(config.node)};
COMMIT;
"""


def arm_vm_firewall_node(config: VmFirewallNodeConfig, runner: Runner, *, apply: bool = False,
                         backup_dir: Path | None = None) -> dict:
    _on_database_host(config)
    snapshot = runner.postgres(config, vm_firewall_node_snapshot_sql(config))
    desired = preview_vm_firewall_node(config, snapshot)
    summary = {key: desired[key] for key in ('node', 'public_id', 'status', 'mode', 'capability_evidence_id')}
    summary['operation'] = 'vm-firewall-node-opt-in'
    summary['apply'] = apply
    summary['automatic_activation'] = False
    if not apply:
        return summary
    directory = _protected_backup(backup_dir)
    operation = uuid.uuid4().hex
    before = directory / f'{operation}-before.json'
    write_private(before, {'operation_id': operation, 'operation': 'vm-firewall-node-opt-in',
                           'config': asdict(config), 'snapshot': snapshot,
                           'requested': desired, 'phase': 'before_apply'})
    applied = runner.postgres(config, vm_firewall_node_apply_sql(config, snapshot, desired))
    node = applied['node']
    require(node['public_id'] == config.node_public_id and node['status'] == 'MAINTENANCE' and
            node['labels'] == desired['labels'],
            'Apply result differs; inspect the saved before record and do not retry blindly')
    write_private(directory / f'{operation}-after.json',
                  {'operation_id': operation, 'operation': 'vm-firewall-node-opt-in',
                   'capability_evidence_id': config.capability_evidence_id,
                   'node': node, 'phase': 'applied'})
    summary['before_record'] = str(before)
    return summary
