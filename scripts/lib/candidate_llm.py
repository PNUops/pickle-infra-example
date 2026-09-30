#!/usr/bin/env python3
"""Stage an owned, closed LLM gateway on the isolated candidate network."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import uuid

import isolated_core as core
import isolated_services as services

Error = core.BootstrapError
Runner = core.Runner
DESCRIPTION = 'candidate-llm:'


@dataclass(frozen=True)
class Config:
    expected_node: str
    expected_cluster: str
    candidate_id: str
    ctid: int
    hostname: str
    ip: str
    proxy_ctid: int
    proxy_hostname: str
    proxy_ip: str
    proxy_config_sha256: str
    bridge: str
    subnet: str
    gateway: str
    mtu: int
    nameserver: str
    storage: str
    disk_gb: int
    memory_mb: int
    cores: int
    storage_reserve_gb: int
    template: str
    template_sha256: str
    binary_file: str
    binary_sha256: str
    keygen_file: str
    keygen_sha256: str
    unit_file: str
    unit_sha256: str
    state_dir: str

    @classmethod
    def load(cls, path: Path) -> 'Config':
        values = json.loads(path.read_text())
        if not isinstance(values, dict) or set(values) != {f.name for f in fields(cls)}:
            raise Error('Configuration must contain exactly the documented fields')
        result = cls(**values)
        result.validate()
        return result

    def validate(self) -> None:
        for name in ('expected_node', 'expected_cluster', 'hostname', 'proxy_hostname', 'storage'):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', getattr(self, name)):
                raise Error(f'Invalid {name}')
        if self.hostname == self.proxy_hostname:
            raise Error('Gateway and proxy hostnames must differ')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,14}', self.bridge):
            raise Error('Invalid bridge')
        try:
            if str(uuid.UUID(self.candidate_id)) != self.candidate_id:
                raise ValueError
        except ValueError as exc:
            raise Error('candidate_id must be a canonical UUID') from exc
        for name in ('ctid', 'proxy_ctid'):
            value = getattr(self, name)
            if type(value) is not int or not 100 <= value <= 999:
                raise Error(f'Invalid {name}')
        if self.ctid == self.proxy_ctid:
            raise Error('Gateway and proxy CTIDs must differ')
        for name in ('disk_gb', 'memory_mb', 'cores', 'storage_reserve_gb'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise Error(f'Invalid {name}')
        if self.memory_mb < 4096:
            raise Error('Gateway memory must leave room above the unit MemoryMax')
        if self.mtu != 1370 or type(self.mtu) is not int:
            raise Error('Candidate MTU must be exactly 1370')
        network = ipaddress.IPv4Network(self.subnet, strict=True)
        addresses = [ipaddress.IPv4Address(value) for value in (self.ip, self.proxy_ip, self.gateway)]
        if len(set(addresses)) != 3 or any(a not in network or a in (network.network_address, network.broadcast_address) for a in addresses):
            raise Error('Candidate addresses must be distinct and usable in the subnet')
        ipaddress.IPv4Address(self.nameserver)
        if not re.fullmatch(r'[A-Za-z0-9_-]+:vztmpl/debian-13-standard_[A-Za-z0-9.+_-]+_amd64\.tar\.(?:zst|gz|xz)', self.template):
            raise Error('Supply an explicit cached Debian 13 amd64 template')
        for name in ('template_sha256', 'binary_sha256', 'keygen_sha256', 'unit_sha256', 'proxy_config_sha256'):
            if not re.fullmatch(r'[0-9a-f]{64}', getattr(self, name)):
                raise Error(f'Invalid {name}')
        for name in ('binary_file', 'keygen_file', 'unit_file', 'state_dir'):
            if not isinstance(getattr(self, name), str) or not Path(getattr(self, name)).is_absolute():
                raise Error(f'{name} must be an absolute path')


def plan(c: Config) -> dict:
    return {'mode': 'dry-run', 'candidate_id': c.candidate_id,
            'expected_node': c.expected_node, 'expected_cluster': c.expected_cluster,
            'container': {'id': c.ctid, 'hostname': c.hostname, 'address': c.ip,
                          'onboot': False, 'unprivileged': True},
            'network': {'bridge': c.bridge, 'gateway': c.gateway, 'mtu': c.mtu,
                        'expected_allowed_source': c.proxy_ip, 'proxy_config_sha256': c.proxy_config_sha256,
                        'port': 8081},
            'artifacts': {name: {'path': getattr(c, name + '_file'), 'sha256': getattr(c, name + '_sha256')}
                          for name in ('binary', 'keygen', 'unit')},
            'service': {'enabled': False, 'started': False, 'authorization': 'closed'},
            'initial_network_window': 'From first start through bounded APT preparation, the isolated bridge is the only network boundary until nftables is installed; SSH and Postfix units are masked after OS readback, and an owned CT is stopped on failure.',
            'proxy_changed': False, 'api_changed': False, 'state_dir': c.state_dir}


def read_artifact(path: str, digest: str, *, binary: bool = False) -> bytes:
    source = Path(path)
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise Error('Artifact must be a regular file without group/world write')
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise Error('Artifact checksum mismatch')
    if binary:
        services.require_linux_amd64_elf(content, source.name)
    return content


def require_idle_cluster(c: Config, r: Runner, nodes: list[str]) -> None:
    resources = json.loads(r.run(['pvesh', 'get', '/cluster/ha/resources',
                                  '--output-format', 'json'], label='HA resources'))
    if not isinstance(resources, list) or resources:
        raise Error('HA resources are present or ambiguous')
    if not nodes or c.expected_node not in nodes:
        raise Error('Expected node is absent from cluster status')
    for node in nodes:
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', node):
            raise Error('Cluster node name is invalid')
        tasks = json.loads(r.run(['pvesh', 'get', f'/nodes/{node}/tasks', '--source', 'active',
                                  '--limit', '500', '--output-format', 'json'], label='active PVE tasks'))
        if not isinstance(tasks, list) or tasks:
            raise Error('Active PVE tasks are present or ambiguous')


def require_proxy_identity(c: Config, r: Runner) -> None:
    path = Path(f'/etc/pve/nodes/{c.expected_node}/lxc/{c.proxy_ctid}.conf')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise Error('Proxy config must be a protected root-owned regular file')
    if hashlib.sha256(path.read_bytes()).hexdigest() != c.proxy_config_sha256:
        raise Error('Proxy config hash changed')
    text = r.run(['pct', 'config', str(c.proxy_ctid)], label='candidate proxy identity')
    hostname, description = core.pct_container_identity(text)
    if hostname != c.proxy_hostname or not re.fullmatch(r'isolated-services:[0-9a-f-]{36}:proxy', description):
        raise Error('Proxy is not the exact owned candidate container')
    run_id = description.removeprefix('isolated-services:').removesuffix(':proxy')
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError
    except ValueError as exc:
        raise Error('Proxy ownership identifier is invalid') from exc
    networks = re.findall(r'^net\d+:', text, re.MULTILINE)
    if networks != ['net0:']:
        raise Error('Proxy must have exactly one net0')
    bridge, address = services.api_net0_identity(text)
    if (bridge, address) != (c.bridge, c.proxy_ip):
        raise Error('Candidate proxy network identity differs')
    status = r.run(['pct', 'status', str(c.proxy_ctid)], label='candidate proxy status').strip()
    if status != 'status: running':
        raise Error('Candidate proxy must be running')
    if hashlib.sha256(path.read_bytes()).hexdigest() != c.proxy_config_sha256:
        raise Error('Proxy config changed during identity readback')


def preflight(c: Config, r: Runner) -> dict[str, bytes]:
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != c.expected_node:
        raise Error('Apply requires root on the exact expected node')
    status = json.loads(r.run(['pvesh', 'get', '/cluster/status', '--output-format', 'json'], label='cluster status'))
    cluster = next((item for item in status if item.get('type') == 'cluster'), {})
    if cluster.get('name') != c.expected_cluster or not cluster.get('quorate'):
        raise Error('Expected cluster does not have quorum')
    nodes = [item.get('name') for item in status if item.get('type') == 'node' and item.get('online')]
    if len(nodes) != len([item for item in status if item.get('type') == 'node']):
        raise Error('A cluster node is offline')
    require_idle_cluster(c, r, nodes)
    guests = json.loads(r.run(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'], label='guest inventory'))
    if any(int(item.get('vmid', -1)) == c.ctid for item in guests):
        raise Error('Requested CTID is in use')
    if any(Path('/etc/pve/nodes').glob(f'*/lxc/{c.ctid}.conf')) or any(Path('/etc/pve/nodes').glob(f'*/qemu-server/{c.ctid}.conf')):
        raise Error('Requested guest config already exists')
    require_proxy_identity(c, r)
    if Path(c.state_dir).exists() or Path(c.state_dir).is_symlink():
        raise Error('State directory already exists; do not resume blindly')
    parent = Path(c.state_dir).parent
    info = parent.stat()
    if parent.resolve() != parent or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise Error('State parent must be a root-owned 0700 directory')
    links = json.loads(r.run(['ip', '-j', 'address', 'show', 'dev', c.bridge], label='bridge inventory'))
    if len(links) != 1 or links[0].get('mtu') != c.mtu or not any(a.get('local') == c.gateway for a in links[0].get('addr_info', [])):
        raise Error('Candidate bridge, gateway or MTU differs')
    storage = json.loads(r.run(['pvesh', 'get', f'/nodes/{c.expected_node}/storage/{c.storage}/status', '--output-format', 'json'], label='storage capacity'))
    if not storage.get('active') or int(storage.get('avail', 0)) < (c.disk_gb + c.storage_reserve_gb) * 1024**3:
        raise Error('Storage lacks reserved headroom')
    volumes = json.loads(r.run(['pvesh', 'get', f'/nodes/{c.expected_node}/storage/{c.storage}/content', '--output-format', 'json'], label='volume inventory'))
    if any(int(item.get('vmid', -1)) == c.ctid for item in volumes):
        raise Error('Requested CTID already owns a volume')
    r.run(['arping', '-D', '-I', c.bridge, '-c', '3', '-w', '5', c.ip], label='duplicate address detection')
    template_path = Path(r.run(['pvesm', 'path', c.template], label='template path').strip())
    read_artifact(str(template_path), c.template_sha256)
    artifacts = {name: read_artifact(getattr(c, name + '_file'), getattr(c, name + '_sha256'), binary=name != 'unit')
                 for name in ('binary', 'keygen', 'unit')}
    unit = artifacts['unit'].decode()
    for line in ('User=pickle-llmgw', 'Group=pickle-llmgw',
                 'EnvironmentFile=/etc/pickle/llm-gateway.env',
                 'ExecStart=/opt/pickle/llm-gateway/bin/llm-gateway'):
        if line not in unit.splitlines():
            raise Error('Gateway unit differs from the reviewed service identity')
    return artifacts


FIREWALL_UNIT = '''[Unit]
Description=Candidate LLM guest firewall
DefaultDependencies=no
Before=network-pre.target networking.service
Wants=network-pre.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/candidate-llm.nft
[Install]
WantedBy=multi-user.target
'''
GATEWAY_DROPIN = '''[Unit]
Requires=candidate-llm-firewall.service
After=candidate-llm-firewall.service
'''
NETWORKING_DROPIN = '''[Unit]
Requires=candidate-llm-firewall.service
After=candidate-llm-firewall.service
'''


def firewall(c: Config) -> str:
    return f'''add table inet candidate_llm
flush table inet candidate_llm
table inet candidate_llm {{
    chain input {{
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        ip saddr {c.proxy_ip} tcp dport 8081 accept
        ip protocol icmp icmp type {{ destination-unreachable, time-exceeded, parameter-problem }} accept
        ip6 nexthdr ipv6-icmp icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert }} accept
    }}
    chain forward {{ type filter hook forward priority filter; policy drop; }}
    chain output {{ type filter hook output priority filter; policy accept; }}
}}
'''


class Bootstrap:
    def __init__(self, c: Config, r: Runner):
        self.c, self.r = c, r
        self.run_id = str(uuid.uuid4())
        self.manifest = {'run_id': self.run_id, 'candidate_id': c.candidate_id,
                         'plan': plan(c), 'attempted': False, 'created': False,
                         'completed': False}

    def save(self) -> None:
        target = Path(self.c.state_dir) / 'manifest.json'
        with tempfile.NamedTemporaryFile(mode='w', dir=self.c.state_dir, prefix='.manifest-', delete=False) as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(self.manifest, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
            temporary = output.name
        os.replace(temporary, target)

    def owned(self, *, timeout: int = 120) -> None:
        text = self.r.run(['pct', 'config', str(self.c.ctid)], label='candidate LLM identity', timeout=timeout)
        hostname, description = core.pct_container_identity(text)
        if (hostname, description) != (self.c.hostname, f'{DESCRIPTION}{self.run_id}'):
            raise Error('Candidate LLM ownership changed')
        values = dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line)
        if values.get('onboot') != '0' or values.get('unprivileged') != '1':
            raise Error('Candidate LLM startup or privilege boundary changed')
        bridge, address = services.api_net0_identity(text)
        if (bridge, address) != (self.c.bridge, self.c.ip):
            raise Error('Candidate LLM network identity changed')

    def guest(self, command: list[str], label: str) -> str:
        self.owned()
        return self.r.guest(self.c.ctid, command, label=label)

    def put(self, path: str, content: bytes | str, mode: str = '0644', owner: str = 'root:root') -> None:
        if isinstance(content, str):
            content = content.encode()
        self.guest(['test', '!', '-e', path], 'new guest path guard')
        self.guest(['test', '!', '-L', path], 'guest symlink guard')
        with tempfile.NamedTemporaryFile(dir=self.c.state_dir) as source:
            source.write(content)
            source.flush()
            self.r.run(['pct', 'push', str(self.c.ctid), source.name, path, '--perms', mode], label='candidate artifact push')
        self.guest(['chown', owner, path], 'artifact ownership')
        self.guest(['chmod', mode, path], 'artifact permissions')
        digest = self.guest(['sha256sum', path], 'guest artifact readback').split()[0]
        if digest != hashlib.sha256(content).hexdigest():
            raise Error('Guest artifact checksum differs')

    def package_network_preflight(self) -> None:
        sources = self.guest(['sh', '-c',
            'for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list '
            '/etc/apt/sources.list.d/*.sources; do '
            '[ -f "$f" ] || continue; cat -- "$f"; printf "\\n\\n"; done'], 'APT sources')
        for host, port in services.apt_source_endpoints(sources):
            answers = self.guest(['timeout', '10s', 'getent', 'ahostsv4', host], 'APT endpoint DNS')
            addresses = []
            for line in answers.splitlines():
                try:
                    address = str(ipaddress.IPv4Address(line.split()[0]))
                    if address not in addresses:
                        addresses.append(address)
                except (IndexError, ipaddress.AddressValueError):
                    continue
            if not addresses:
                raise Error('APT endpoint has no IPv4 address')
            for address in addresses:
                try:
                    self.guest(['timeout', '10s', 'bash', '-c',
                                'exec 3<>"/dev/tcp/$1/$2"', 'package-endpoint', address, str(port)],
                               'APT endpoint connect')
                    break
                except Error:
                    continue
            else:
                raise Error('APT endpoint is unreachable')

    def package(self, phase: str, runtime: int, command: list[str]) -> None:
        unit = f'candidate-llm-{self.run_id}-{phase}.service'
        existing = self.guest(['systemctl', 'show', unit, '-p', 'LoadState', '--value'], 'package unit collision')
        if existing.strip() not in ('', 'not-found'):
            raise Error('Candidate package unit already exists')
        self.owned()
        self.r.guest(self.c.ctid,
                     ['systemd-run', '--wait', '--collect', '--quiet', '--expand-environment=no',
                      '--unit=' + unit, '--property=Type=exec',
                      f'--property=RuntimeMaxSec={runtime}s', '--property=TimeoutStopSec=20s',
                      '--property=KillMode=control-group', '--property=SendSIGKILL=yes',
                      '--property=UMask=0022', '--property=StandardOutput=journal',
                      '--property=StandardError=journal', '--', *command],
                     label=f'candidate {phase}', timeout=runtime + 60)

    def disable_guest_ssh(self) -> None:
        units = ('ssh.socket', 'ssh.service', 'postfix.service', 'postfix-resolvconf.path')
        names = ' '.join(units)
        self.guest(['sh', '-c',
                    f'systemctl disable --now {names} 2>/dev/null || true; '
                    f'systemctl mask --now {names}'], 'template network services disabled')
        for unit in units:
            state = self.guest(['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'UnitFileState'], 'template service state')
            if 'ActiveState=inactive' not in state.splitlines() or 'UnitFileState=masked' not in state.splitlines():
                raise Error('Guest template network service did not remain masked')

    def verify_guest_network(self) -> None:
        resolver = self.guest(['cat', '/etc/resolv.conf'], 'configured DNS readback')
        servers = [line.split()[1] for line in resolver.splitlines()
                   if line.split() and line.split()[0] == 'nameserver' and len(line.split()) > 1]
        if servers != [self.c.nameserver]:
            raise Error('Guest resolver differs from configured nameserver')
        listeners = self.guest(['ss', '-H', '-ltn'], 'closed TCP listener readback')
        if listeners.strip():
            raise Error('Candidate guest has an unexpected TCP listener')

    def stop_owned_on_failure(self) -> str:
        try:
            self.owned(timeout=15)
        except (Error, OSError, subprocess.TimeoutExpired):
            return 'ownership_unverified; no stop attempted'
        try:
            status = self.r.run(['pct', 'status', str(self.c.ctid)],
                                label='failed candidate status', timeout=10).strip()
            if status == 'status: stopped':
                return 'already_stopped'
            if status != 'status: running':
                return 'status_ambiguous; no stop attempted'
            try:
                self.r.run(['pct', 'stop', str(self.c.ctid)],
                           label='failed candidate stop', timeout=45)
            except (Error, OSError, subprocess.TimeoutExpired):
                pass
            readback = self.r.run(['pct', 'status', str(self.c.ctid)],
                                  label='failed candidate stop readback', timeout=10).strip()
            return 'stopped' if readback == 'status: stopped' else 'stop_unverified'
        except (Error, OSError, subprocess.TimeoutExpired):
            return 'stop_failed_or_unverified'

    def stage_executables(self, artifacts: dict[str, bytes]) -> None:
        self.guest(['install', '-d', '-o', 'root', '-g', 'root', '-m', '0755',
                    '/opt/pickle', '/opt/pickle/llm-gateway', '/opt/pickle/llm-gateway/bin'], 'binary directory')
        self.put('/opt/pickle/llm-gateway/bin/llm-gateway', artifacts['binary'], '0755', 'root:root')
        self.put('/opt/pickle/llm-gateway/bin/llm-keygen', artifacts['keygen'], '0750', 'root:pickle-llmgw')

    def apply(self) -> None:
        c = self.c
        artifacts = preflight(c, self.r)
        Path(c.state_dir).mkdir(mode=0o700)
        self.manifest['attempted'] = True
        self.save()
        try:
            status = json.loads(self.r.run(['pvesh', 'get', '/cluster/status', '--output-format', 'json'], label='cluster status before creation'))
            cluster = next((item for item in status if item.get('type') == 'cluster'), {})
            nodes = [item for item in status if item.get('type') == 'node']
            if cluster.get('name') != c.expected_cluster or not cluster.get('quorate') or not nodes or any(not item.get('online') for item in nodes):
                raise Error('Candidate cluster changed before container creation')
            require_idle_cluster(c, self.r, [item.get('name') for item in nodes])
            require_proxy_identity(c, self.r)
            prefix = ipaddress.IPv4Network(c.subnet).prefixlen
            self.r.run(['pct', 'create', str(c.ctid), c.template, '--hostname', c.hostname,
                        '--description', f'{DESCRIPTION}{self.run_id}', '--storage', c.storage,
                        '--rootfs', f'{c.storage}:{c.disk_gb}', '--cores', str(c.cores),
                        '--memory', str(c.memory_mb), '--swap', '256', '--unprivileged', '1',
                        '--onboot', '0', '--start', '0', '--nameserver', c.nameserver,
                        '--net0', f'name=eth0,bridge={c.bridge},ip={c.ip}/{prefix},gw={c.gateway},mtu={c.mtu}'],
                       label='candidate LLM creation', timeout=300)
            self.manifest['created'] = True
            self.save()
            self.owned()
            # Guest nftables is not yet installed. The existing isolated bridge
            # is the only network boundary from this first start through APT;
            # do not describe this phase as guest-firewall protected.
            self.r.run(['pct', 'start', str(c.ctid)], label='candidate LLM start')
            release = self.guest(['cat', '/etc/os-release'], 'guest OS identity')
            if not re.search(r'^VERSION_ID="?13"?$', release, re.MULTILINE):
                raise Error('Guest is not Debian 13')
            # Close template SSH and mail units before package network access.
            self.disable_guest_ssh()
            self.verify_guest_network()
            self.package_network_preflight()
            self.guest(['test', '!', '-e', '/usr/sbin/policy-rc.d'], 'new package guard')
            self.guest(['test', '!', '-L', '/usr/sbin/policy-rc.d'], 'package guard symlink')
            self.guest(['sh', '-c', 'printf "#!/bin/sh\\nexit 101\\n" > /usr/sbin/policy-rc.d; chmod 755 /usr/sbin/policy-rc.d'], 'package autostart guard')
            self.package('index', 240, ['/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive',
                                        '/usr/bin/apt-get', 'update', '--error-on=any'])
            self.package('upgrade', 840, ['/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive',
                                          '/usr/bin/apt-get', 'upgrade', '--with-new-pkgs', '-y'])
            self.package('packages', 540, ['/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive',
                                           '/usr/bin/apt-get', 'install', '-y', '--no-install-recommends',
                                           'ca-certificates', 'nftables'])
            self.disable_guest_ssh()
            self.verify_guest_network()
            self.guest(['systemctl', 'mask', '--now', 'nftables.service'], 'single firewall owner')
            self.put('/etc/candidate-llm.nft', firewall(c))
            self.put('/etc/systemd/system/candidate-llm-firewall.service', FIREWALL_UNIT)
            self.guest(['install', '-d', '-m', '0755', '/etc/systemd/system/networking.service.d'], 'network dependency directory')
            self.put('/etc/systemd/system/networking.service.d/10-candidate-llm.conf', NETWORKING_DROPIN)
            self.guest(['nft', '-c', '-f', '/etc/candidate-llm.nft'], 'firewall syntax')
            self.guest(['systemctl', 'daemon-reload'], 'firewall unit reload')
            self.guest(['systemctl', 'enable', '--now', 'candidate-llm-firewall.service'], 'private input policy')
            self.guest(['rm', '/usr/sbin/policy-rc.d'], 'remove package autostart guard')
            self.guest(['useradd', '--system', '--user-group', '--home-dir', '/opt/pickle/llm-gateway', '--shell', '/usr/sbin/nologin', 'pickle-llmgw'], 'service account')
            self.guest(['install', '-d', '-o', 'root', '-g', 'pickle-llmgw', '-m', '0750', '/var/lib/pickle-llm-gateway'], 'state directory')
            self.stage_executables(artifacts)
            self.put('/var/lib/pickle-llm-gateway/snapshot.json', '{"generation":1,"serviceEnabled":false,"models":[],"keys":[]}\n', '0640', 'pickle-llmgw:pickle-llmgw')
            self.guest(['install', '-d', '-m', '0755', '/etc/pickle', '/etc/systemd/system/llm-gateway.service.d'], 'gateway config directories')
            self.put('/etc/pickle/llm-gateway.env',
                     f'LLMGW_LISTEN={c.ip}:8081\nLLMGW_SNAPSHOT_PATH=/var/lib/pickle-llm-gateway/snapshot.json\nLLMGW_SPOOL_DIR=/var/lib/pickle-llm-gateway/spool\n',
                     '0640', 'root:pickle-llmgw')
            self.put('/etc/systemd/system/llm-gateway.service', artifacts['unit'])
            self.put('/etc/systemd/system/llm-gateway.service.d/10-candidate-firewall.conf', GATEWAY_DROPIN)
            self.guest(['systemctl', 'daemon-reload'], 'gateway unit reload')
            self.guest(['systemctl', 'disable', '--now', 'llm-gateway.service'], 'gateway stays disabled')
            enabled = self.guest(['systemctl', 'show', 'llm-gateway.service', '-p', 'UnitFileState', '--value'], 'gateway enabled state').strip()
            active = self.guest(['systemctl', 'show', 'llm-gateway.service', '-p', 'ActiveState', '--value'], 'gateway active state').strip()
            if (enabled, active) != ('disabled', 'inactive'):
                raise Error('Gateway application did not remain disabled')
            self.disable_guest_ssh()
            self.verify_guest_network()
            self.manifest['completed'] = True
            self.manifest['boundary'] = 'Container retained onboot=0; gateway disabled and stopped; no ingress or API changed.'
            self.save()
        except BaseException:
            self.manifest['completed'] = False
            self.manifest['failure_stop'] = self.stop_owned_on_failure()
            self.manifest['boundary'] = 'Partial candidate retained; inspect ownership, stop result and manifest. Do not destroy automatically.'
            self.save()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        c = Config.load(args.config)
        if args.apply:
            Bootstrap(c, Runner()).apply()
            print('Candidate LLM staged; gateway remains disabled and stopped.')
        else:
            print(json.dumps(plan(c), indent=2))
        return 0
    except (Error, OSError, ValueError, KeyError, UnicodeDecodeError, subprocess.TimeoutExpired) as error:
        print(f'Candidate LLM bootstrap stopped: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
