#!/usr/bin/env python3
"""Prepare two new, stopped-by-default proxy and SSH-gateway containers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
import hashlib
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
import uuid

import isolated_core as core

BootstrapError = core.BootstrapError
Runner = core.Runner

SSHPIPERD_VERSION = 'v1.6.1'
SSHPIPERD_ASSET_SHA256 = '95d423a70e843a7512a72fbb16c0fa5dc59217cf064cb3e277520fddffded1e1'
DESCRIPTION_PREFIX = 'isolated-services:'
APT_UPDATE_RUNTIME_SECONDS = 240
APT_UPGRADE_RUNTIME_SECONDS = 840
APT_INSTALL_RUNTIME_SECONDS = 540
TRANSIENT_STOP_SECONDS = 20
HOST_TIMEOUT_MARGIN_SECONDS = 40


@dataclass(frozen=True)
class Config:
    expected_node: str
    expected_cluster: str
    bridge: str
    subnet: str
    gateway: str
    mtu: int
    nameserver: str
    storage: str
    storage_reserve_gb: int
    template: str
    template_sha256: str
    nginx_version: str
    proxy_ctid: int
    proxy_hostname: str
    proxy_ip: str
    proxy_cores: int
    proxy_memory_mb: int
    proxy_disk_gb: int
    sshgw_ctid: int
    sshgw_hostname: str
    sshgw_ip: str
    sshgw_cores: int
    sshgw_memory_mb: int
    sshgw_disk_gb: int
    api_ctid: int
    api_ip: str
    console_origin: str
    candidate_id: str
    proxy_env_file: str
    sshgw_env_file: str
    forbidden_token_hashes_file: str
    proxy_agent_file: str
    proxy_agent_sha256: str
    proxy_unit_file: str
    proxy_unit_sha256: str
    proxy_nginx_file: str
    proxy_nginx_sha256: str
    sshpiperd_archive_file: str
    sshpiperd_archive_sha256: str
    sshgw_route_plugin_file: str
    sshgw_route_plugin_sha256: str
    sshgw_terminal_bridge_file: str
    sshgw_terminal_bridge_sha256: str
    sshpiperd_unit_file: str
    sshpiperd_unit_sha256: str
    terminal_unit_file: str
    terminal_unit_sha256: str
    state_dir: str

    @classmethod
    def load(cls, path: Path) -> 'Config':
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise BootstrapError('Configuration must contain exactly the documented fields')
        result = cls(**value)
        result.validate()
        return result

    def validate(self) -> None:
        for name in ('expected_node', 'expected_cluster', 'proxy_hostname',
                     'sshgw_hostname', 'storage'):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(
                    r'[a-z][a-z0-9-]{0,62}', getattr(self, name)):
                raise BootstrapError(f'Invalid {name}')
        if self.proxy_hostname == self.sshgw_hostname:
            raise BootstrapError('Service hostnames must differ')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,14}', self.bridge):
            raise BootstrapError('Invalid bridge')
        network = ipaddress.IPv4Network(self.subnet, strict=True)
        if not 16 <= network.prefixlen <= 28:
            raise BootstrapError('Use an explicit IPv4 infrastructure subnet between /16 and /28')
        addresses = [ipaddress.IPv4Address(getattr(self, name)) for name in
                     ('gateway', 'proxy_ip', 'sshgw_ip', 'api_ip')]
        if len(set(addresses)) != 4 or any(address not in network or address in
                (network.network_address, network.broadcast_address) for address in addresses):
            raise BootstrapError('Service addresses must be distinct usable addresses in the subnet')
        ipaddress.IPv4Address(self.nameserver)
        for name in ('proxy_ctid', 'sshgw_ctid', 'api_ctid'):
            value = getattr(self, name)
            if type(value) is not int or not 100 <= value <= 999:
                raise BootstrapError('Explicit container IDs must be in 100-999')
        if len({self.proxy_ctid, self.sshgw_ctid, self.api_ctid}) != 3:
            raise BootstrapError('Proxy, SSH gateway and API container IDs must differ')
        for name in ('proxy_cores', 'proxy_memory_mb', 'proxy_disk_gb',
                     'sshgw_cores', 'sshgw_memory_mb', 'sshgw_disk_gb',
                     'storage_reserve_gb'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise BootstrapError(f'Invalid {name}')
        if type(self.mtu) is not int or self.mtu != 1370:
            raise BootstrapError('Candidate service MTU must be exactly 1370')
        if not re.fullmatch(
                r'[A-Za-z0-9_-]+:vztmpl/debian-13-standard_[A-Za-z0-9.+_-]+_amd64\.tar\.(?:zst|gz|xz)',
                self.template):
            raise BootstrapError('Supply an explicit cached Debian 13 amd64 PVE template volume')
        if not re.fullmatch(r'1\.[0-9]+\.[0-9]+-1~trixie', self.nginx_version):
            raise BootstrapError('Pin one stable nginx.org Debian 13 package version')
        try:
            if str(uuid.UUID(self.candidate_id)) != self.candidate_id:
                raise ValueError
        except ValueError as error:
            raise BootstrapError('candidate_id must be one canonical UUID') from error
        if not re.fullmatch(r'https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?',
                            self.console_origin):
            raise BootstrapError('console_origin must be one explicit HTTPS origin without a path')
        for name in [field.name for field in fields(self) if field.name.endswith('_sha256')]:
            if not re.fullmatch(r'[0-9a-f]{64}', getattr(self, name)):
                raise BootstrapError(f'{name} must be a lowercase SHA-256')
        if self.sshpiperd_archive_sha256 != SSHPIPERD_ASSET_SHA256:
            raise BootstrapError(f'sshpiperd must be the reviewed {SSHPIPERD_VERSION} linux x86_64 release')
        for name in [field.name for field in fields(self)
                     if field.name.endswith('_file') or field.name == 'state_dir']:
            value = getattr(self, name)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise BootstrapError(f'{name} must be an explicit absolute path')


ARTIFACTS = (
    ('proxy_agent_file', 'proxy_agent_sha256'),
    ('proxy_unit_file', 'proxy_unit_sha256'),
    ('proxy_nginx_file', 'proxy_nginx_sha256'),
    ('sshpiperd_archive_file', 'sshpiperd_archive_sha256'),
    ('sshgw_route_plugin_file', 'sshgw_route_plugin_sha256'),
    ('sshgw_terminal_bridge_file', 'sshgw_terminal_bridge_sha256'),
    ('sshpiperd_unit_file', 'sshpiperd_unit_sha256'),
    ('terminal_unit_file', 'terminal_unit_sha256'),
)


def plan(c: Config) -> dict:
    return {
        'mode': 'dry-run',
        'candidate_id': c.candidate_id,
        'expected_node': c.expected_node,
        'expected_cluster': c.expected_cluster,
        'template': {'volume': c.template, 'sha256': c.template_sha256},
        'network': {'bridge': c.bridge, 'subnet': c.subnet, 'gateway': c.gateway,
                    'mtu': c.mtu, 'api_address': c.api_ip},
        'containers': [
            {'role': 'proxy', 'id': c.proxy_ctid, 'hostname': c.proxy_hostname,
             'address': c.proxy_ip, 'cores': c.proxy_cores,
             'memory_mib': c.proxy_memory_mb, 'disk_gib': c.proxy_disk_gb,
             'onboot': False},
            {'role': 'sshgw', 'id': c.sshgw_ctid, 'hostname': c.sshgw_hostname,
             'address': c.sshgw_ip, 'cores': c.sshgw_cores,
             'memory_mib': c.sshgw_memory_mb, 'disk_gib': c.sshgw_disk_gb,
             'onboot': False},
        ],
        'artifacts': {path: {'path': getattr(c, path), 'sha256': getattr(c, digest)}
                      for path, digest in ARTIFACTS},
        'credential_paths': {'proxy': c.proxy_env_file, 'sshgw': c.sshgw_env_file,
                             'legacy_token_hashes': c.forbidden_token_hashes_file},
        'services': {'enabled': False, 'started': False,
                     'excluded': ['sshgw-proxyfront', 'WireGuard', 'relay']},
        'api_changed': False,
    }


def parse_env(raw: bytes, required: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.decode().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if (not separator or key not in required or key in values or not value
                or re.search(r'[\s\x00"\'`$\\]', value)):
            raise BootstrapError('Protected candidate env contains an undocumented or unsafe assignment')
        values[key] = value
    if set(values) != required:
        raise BootstrapError('Protected candidate env is missing a required assignment')
    for key, value in values.items():
        if key.endswith('_TOKEN') and not re.fullmatch(r'[A-Za-z0-9_+=/.-]{32,128}', value):
            raise BootstrapError('Candidate tokens must be fresh 32-128 character base64-safe values')
    return values


def require_linux_amd64_elf(content: bytes, label: str) -> None:
    # ELF64, little endian, e_machine=EM_X86_64. Reject host-native artifacts
    # before they reach a guest whose services deliberately remain stopped.
    if (len(content) < 20 or content[:4] != b'\x7fELF' or content[4:6] != b'\x02\x01'
            or int.from_bytes(content[18:20], 'little') != 62):
        raise BootstrapError(f'{label} must be one Linux amd64 ELF binary')


def parse_token_hashes(raw: bytes) -> set[str]:
    values = {line.strip() for line in raw.decode().splitlines()
              if line.strip() and not line.lstrip().startswith('#')}
    if not 1 <= len(values) <= 32 or any(not re.fullmatch(r'[0-9a-f]{64}', value)
                                         for value in values):
        raise BootstrapError('Legacy token denial metadata must contain lowercase SHA-256 lines')
    return values


def validate_candidate_envs(c: Config, proxy: dict[str, str], sshgw: dict[str, str],
                            forbidden_hashes: set[str]) -> None:
    if proxy['PICKLE_CANDIDATE_ID'] != c.candidate_id or sshgw['PICKLE_CANDIDATE_ID'] != c.candidate_id:
        raise BootstrapError('Candidate env identity does not match this isolated plan')
    tokens = {proxy['PICKLE_PROXY_AGENT_TOKEN'], sshgw['PICKLE_SSHGW_TOKEN'],
              sshgw['PICKLE_TERMINAL_CONTROL_TOKEN']}
    if len(tokens) != 3:
        raise BootstrapError('Each candidate control link requires an independent fresh token')
    if any(hashlib.sha256(token.encode()).hexdigest() in forbidden_hashes for token in tokens):
        raise BootstrapError('A candidate token matches legacy token custody metadata')


def read_inputs(c: Config) -> tuple[dict[str, str], dict[str, str], dict[str, bytes], bytes]:
    proxy = parse_env(core.protected_file(c.proxy_env_file, private=True),
                      {'PICKLE_CANDIDATE_ID', 'PICKLE_PROXY_AGENT_TOKEN'})
    sshgw = parse_env(core.protected_file(c.sshgw_env_file, private=True),
                      {'PICKLE_CANDIDATE_ID', 'PICKLE_SSHGW_TOKEN',
                       'PICKLE_TERMINAL_CONTROL_TOKEN'})
    denial_raw = core.protected_file(c.forbidden_token_hashes_file, private=True)
    validate_candidate_envs(c, proxy, sshgw, parse_token_hashes(denial_raw))
    artifacts: dict[str, bytes] = {}
    for path_name, sha_name in ARTIFACTS:
        path = Path(getattr(c, path_name))
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise BootstrapError(f'{path_name} must be a regular non-symlink file')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != getattr(c, sha_name):
            raise BootstrapError(f'{path_name} checksum mismatch')
        artifacts[path_name] = content
    for name in ('proxy_agent_file', 'sshgw_route_plugin_file',
                 'sshgw_terminal_bridge_file'):
        require_linux_amd64_elf(artifacts[name], name)
    return proxy, sshgw, artifacts, denial_raw


def require_protected_parent(path_value: str) -> None:
    parent = Path(path_value).parent
    info = parent.stat()
    if parent.resolve() != parent or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise BootstrapError('Candidate input parent must be a root-owned 0700 directory')


def api_net0_identity(config_text: str) -> tuple[str, str]:
    lines = [line.split(':', 1)[1].strip() for line in config_text.splitlines()
             if re.fullmatch(r'net0:\s*.*', line)]
    if len(lines) != 1:
        raise BootstrapError('API LXC must have exactly one unambiguous net0 configuration')
    values: dict[str, str] = {}
    for token in lines[0].split(','):
        key, separator, value = token.partition('=')
        if not separator or not key or not value or key in values:
            raise BootstrapError('API LXC net0 contains a missing or duplicate token')
        values[key] = value
    if 'bridge' not in values or 'ip' not in values:
        raise BootstrapError('API LXC net0 must name its bridge and static IPv4 address')
    try:
        interface = ipaddress.ip_interface(values['ip'])
    except ValueError as error:
        raise BootstrapError('API LXC net0 does not contain one static IP prefix') from error
    if not isinstance(interface, ipaddress.IPv4Interface):
        raise BootstrapError('API LXC net0 must use static IPv4')
    return values['bridge'], str(interface.ip)


def apt_source_endpoints(source_text: str) -> list[tuple[str, int]]:
    """Extract unique HTTP(S) endpoints from legacy and deb822 APT sources."""
    uris: list[str] = []
    paragraphs = re.split(r'\n[ \t]*\n', source_text)
    for paragraph in paragraphs:
        lines = [line for line in paragraph.splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        if not lines:
            continue
        if any(line.lstrip().startswith(('deb ', 'deb-src ')) for line in lines):
            if any(not line.lstrip().startswith(('deb ', 'deb-src ')) for line in lines):
                raise BootstrapError('APT source paragraph mixes legacy and deb822 syntax')
            for raw in lines:
                try:
                    tokens = shlex.split(raw.strip())
                except ValueError as error:
                    raise BootstrapError('APT source definition is malformed') from error
                position = 1
                options: list[str] = []
                if position < len(tokens) and tokens[position].startswith('['):
                    while position < len(tokens):
                        options.append(tokens[position])
                        if tokens[position].endswith(']'):
                            break
                        position += 1
                    if not options[-1].endswith(']'):
                        raise BootstrapError('APT source options are unterminated')
                    position += 1
                if any(option.strip('[]').lower() == 'enabled=no' for option in options):
                    continue
                if position >= len(tokens):
                    raise BootstrapError('APT source definition has no URI')
                uris.append(tokens[position])
            continue
        fields: dict[str, str] = {}
        current: str | None = None
        for raw in lines:
            if raw[:1].isspace():
                if current is None:
                    raise BootstrapError('APT deb822 continuation has no field')
                fields[current] += ' ' + raw.strip()
                continue
            key, separator, value = raw.partition(':')
            normalized = key.strip().lower()
            if (not separator or not re.fullmatch(r'[a-z][a-z0-9-]*', normalized)
                    or normalized in fields):
                raise BootstrapError('APT deb822 source paragraph is malformed')
            fields[normalized] = value.strip()
            current = normalized
        enabled = fields.get('enabled', 'yes').lower()
        if enabled not in ('yes', 'no'):
            raise BootstrapError('APT deb822 Enabled field must be yes or no')
        if enabled == 'no':
            continue
        types = fields.get('types', '').split()
        if not types or any(value not in ('deb', 'deb-src') for value in types):
            raise BootstrapError('APT deb822 Types field is missing or unsupported')
        stanza_uris = fields.get('uris', '').split()
        if not stanza_uris:
            raise BootstrapError('APT deb822 source paragraph has no URI')
        uris.extend(stanza_uris)
    endpoints: list[tuple[str, int]] = []
    for uri in uris:
        parsed = urlsplit(uri)
        if (parsed.scheme not in ('http', 'https') or parsed.hostname is None
                or parsed.username is not None or parsed.password is not None):
            raise BootstrapError('APT source plan contains an unsupported endpoint')
        try:
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        except ValueError as error:
            raise BootstrapError('APT source plan contains an invalid port') from error
        endpoint = (parsed.hostname, port)
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    if not endpoints:
        raise BootstrapError('APT source plan did not expose any HTTP(S) endpoint')
    return endpoints


def apt_update_command() -> list[str]:
    # APT 3 may otherwise return success after a transient index failure and
    # leave the caller using a stale or incomplete cache.
    return ['/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/apt-get',
            'update', '--error-on=any']


def transient_unit_name(run_id: str, role: str, phase: str) -> str:
    if (not re.fullmatch(r'[0-9a-f-]{36}', run_id)
            or role not in ('proxy', 'sshgw')
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', phase)):
        raise BootstrapError('Transient package unit identity is invalid')
    return f'pickle-isolated-services-{run_id}-{role}-{phase}.service'


def firewall(c: Config, role: str) -> str:
    if role == 'proxy':
        accepts = f'ip saddr {c.api_ip} tcp dport 9443 accept'
    else:
        accepts = (f'ip saddr {c.proxy_ip} tcp dport 8082 accept\n'
                   f'        ip saddr {c.api_ip} tcp dport 8083 accept')
    return f'''add table inet isolated_services
flush table inet isolated_services
table inet isolated_services {{
    chain input {{
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        {accepts}
        ip protocol icmp icmp type {{ destination-unreachable, time-exceeded, parameter-problem }} accept
        ip6 nexthdr ipv6-icmp icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert }} accept
    }}
    chain forward {{ type filter hook forward priority filter; policy drop; }}
    chain output {{ type filter hook output priority filter; policy accept; }}
}}
'''


FIREWALL_UNIT = '''[Unit]
Description=Isolated service input policy
DefaultDependencies=no
After=local-fs.target
Before=network-pre.target shutdown.target
Wants=network-pre.target
Conflicts=shutdown.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/isolated-services.nft
[Install]
WantedBy=multi-user.target
'''

NETWORKING_DROPIN = '''[Unit]
Requires=isolated-services-firewall.service
After=isolated-services-firewall.service
'''

SERVICE_DROPIN = '''[Unit]
Requires=isolated-services-firewall.service networking.service
After=isolated-services-firewall.service networking.service
'''


def proxy_environment(c: Config, values: dict[str, str]) -> str:
    return (f'PICKLE_PROXY_AGENT_TOKEN={values["PICKLE_PROXY_AGENT_TOKEN"]}\n'
            f'PICKLE_PROXY_AGENT_LISTEN={c.proxy_ip}:9443\n'
            f'PICKLE_PROXY_AGENT_ALLOWED_SRC={c.api_ip}\n'
            'PICKLE_PROXY_AGENT_WILDCARD_CERTS=\n')


def sshgw_environment(c: Config, values: dict[str, str]) -> str:
    return (f'PICKLE_SSHGW_API_BASE=http://{c.api_ip}:8080\n'
            f'PICKLE_SSHGW_TOKEN={values["PICKLE_SSHGW_TOKEN"]}\n'
            'PICKLE_SSHGW_UPSTREAM_KEY_FILE=/etc/pickle/sshgw/upstream_ed25519_key\n'
            f'PICKLE_TERMINAL_CONTROL_TOKEN={values["PICKLE_TERMINAL_CONTROL_TOKEN"]}\n'
            f'PICKLE_TERMINAL_WS_LISTEN={c.sshgw_ip}:8082\n'
            f'PICKLE_TERMINAL_CONTROL_LISTEN={c.sshgw_ip}:8083\n'
            'PICKLE_TERMINAL_KEY_FILE=/etc/pickle/sshgw/terminal_ed25519_key\n'
            f'PICKLE_TERMINAL_WS_PEER={c.proxy_ip}\n'
            f'PICKLE_TERMINAL_CONTROL_PEER={c.api_ip}\n'
            f'PICKLE_TERMINAL_CONSOLE_ORIGIN={c.console_origin}\n')


def candidate_nginx(c: Config) -> str:
    return f'''map $remote_addr $pickle_client_ip {{ default $remote_addr; }}
server {{
    listen {c.proxy_ip}:80 default_server;
    server_name _;
    return 503;
}}
'''


def preflight(c: Config, r: Runner) -> tuple[dict[str, str], dict[str, str], dict[str, bytes], bytes]:
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != c.expected_node:
        raise BootstrapError('Apply requires root on the exact expected node')
    status = json.loads(r.run(['pvesh', 'get', '/cluster/status', '--output-format', 'json'],
                              label='cluster status'))
    cluster = next((row for row in status if row.get('type') == 'cluster'), {})
    if cluster.get('name') != c.expected_cluster or not cluster.get('quorate'):
        raise BootstrapError('The expected cluster must have quorum')
    guests = json.loads(r.run(['pvesh', 'get', '/cluster/resources', '--type', 'vm',
                               '--output-format', 'json'], label='guest inventory'))
    if any(int(row.get('vmid', -1)) in (c.proxy_ctid, c.sshgw_ctid) for row in guests):
        raise BootstrapError('A requested CTID is already used anywhere in the cluster')
    api = [row for row in guests if int(row.get('vmid', -1)) == c.api_ctid]
    if (len(api) != 1 or api[0].get('type') != 'lxc'
            or api[0].get('node') != c.expected_node):
        raise BootstrapError('The expected API LXC must already exist on the target node')
    api_config = r.run(['pct', 'config', str(c.api_ctid)],
                       label='API container network identity')
    api_bridge, api_address = api_net0_identity(api_config)
    if api_bridge != c.bridge or api_address != c.api_ip:
        raise BootstrapError('The API LXC net0 bridge or static address does not match this plan')
    for ctid in (c.proxy_ctid, c.sshgw_ctid):
        if (any(Path('/etc/pve/nodes').glob(f'*/lxc/{ctid}.conf'))
                or any(Path('/etc/pve/nodes').glob(f'*/qemu-server/{ctid}.conf'))):
            raise BootstrapError('A requested guest configuration already exists')
    state = Path(c.state_dir)
    if state.exists() or state.is_symlink():
        raise BootstrapError('State directory already exists; never overwrite or resume blindly')
    parent = state.parent
    if not parent.is_dir():
        raise BootstrapError('Create the protected state parent explicitly first')
    info = parent.stat()
    if parent.resolve() != parent or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise BootstrapError('State parent must be a root-owned 0700 directory without symlink traversal')
    links = json.loads(r.run(['ip', '-j', 'address', 'show', 'dev', c.bridge],
                             label='bridge inventory'))
    if (len(links) != 1 or links[0].get('mtu') != c.mtu
            or not any(address.get('local') == c.gateway
                       for address in links[0].get('addr_info', []))):
        raise BootstrapError('The bridge, gateway address and measured MTU must already be configured')
    store = json.loads(r.run(['pvesh', 'get', f'/nodes/{c.expected_node}/storage/{c.storage}/status',
                              '--output-format', 'json'], label='storage capacity'))
    needed = (c.proxy_disk_gb + c.sshgw_disk_gb + c.storage_reserve_gb) * 1024**3
    if not store.get('active') or int(store.get('avail', 0)) < needed:
        raise BootstrapError('Target storage lacks the explicitly reserved headroom')
    volumes = json.loads(r.run(['pvesh', 'get', f'/nodes/{c.expected_node}/storage/{c.storage}/content',
                                '--output-format', 'json'], label='orphan volume inventory'))
    if any(int(row.get('vmid', -1)) in (c.proxy_ctid, c.sshgw_ctid) for row in volumes):
        raise BootstrapError('A requested guest ID already owns a volume; do not reuse it')
    for address in (c.proxy_ip, c.sshgw_ip):
        r.run(['arping', '-D', '-I', c.bridge, '-c', '3', '-w', '5', address],
              label='duplicate address detection')
    template = Path(r.run(['pvesm', 'path', c.template], label='template path').strip())
    if not template.is_file() or template.is_symlink():
        raise BootstrapError('A verified cached template archive is required; no automatic download')
    with template.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != c.template_sha256:
            raise BootstrapError('Template checksum mismatch')
    require_protected_parent(c.proxy_env_file)
    require_protected_parent(c.sshgw_env_file)
    require_protected_parent(c.forbidden_token_hashes_file)
    return read_inputs(c)


class Bootstrap:
    def __init__(self, config: Config, runner: Runner):
        self.c, self.r = config, runner
        self.run_id = str(uuid.uuid4())
        self.manifest = {'run_id': self.run_id, 'candidate_id': config.candidate_id,
                         'plan': plan(config), 'attempted': [], 'created': [],
                         'completed': False}

    def save(self) -> None:
        path = Path(self.c.state_dir)
        temporary = path / '.manifest.tmp'
        with temporary.open('x') as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(self.manifest, target, indent=2)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path / 'manifest.json')
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def identity(self, role: str) -> tuple[int, str]:
        if role == 'proxy':
            return self.c.proxy_ctid, self.c.proxy_hostname
        return self.c.sshgw_ctid, self.c.sshgw_hostname

    def owned(self, role: str) -> None:
        ctid, hostname = self.identity(role)
        text = self.r.run(['pct', 'config', str(ctid)], label=f'{role} container identity')
        actual_hostname, description = core.pct_container_identity(text)
        if actual_hostname != hostname or description != f'{DESCRIPTION_PREFIX}{self.run_id}:{role}':
            raise BootstrapError(f'{role} container ownership changed')
        record = next((row for row in self.manifest['created'] if row.get('role') == role), None)
        if record is not None and record.get('machine_id') is not None:
            machine_id = self.r.guest(ctid, ['cat', '/etc/machine-id'],
                                      label=f'{role} machine identity').strip()
            if machine_id != record['machine_id']:
                raise BootstrapError(f'{role} machine identity changed')

    def put(self, role: str, path: str, content: bytes | str,
            mode: str = '0644', owner: str = 'root:root') -> None:
        if isinstance(content, str):
            content = content.encode()
        ctid, _ = self.identity(role)
        self.owned(role)
        self.r.guest(ctid, ['test', '!', '-e', path], label='new guest file guard')
        self.r.guest(ctid, ['test', '!', '-L', path], label='guest symlink guard')
        with tempfile.NamedTemporaryFile(prefix='isolated-services-', dir=self.c.state_dir) as source:
            source.write(content)
            source.flush()
            self.r.run(['pct', 'push', str(ctid), source.name, path, '--perms', mode],
                       label='new owned guest file')
        self.r.guest(ctid, ['chown', owner, path], label='guest file owner')
        self.r.guest(ctid, ['chmod', mode, path], label='guest file permissions')
        parts = self.r.guest(ctid, ['sha256sum', path], label='guest file checksum').split()
        digest = parts[0] if parts else ''
        if digest != hashlib.sha256(content).hexdigest():
            raise BootstrapError('Guest file checksum readback failed')

    def create(self, role: str) -> None:
        c = self.c
        ctid, hostname = self.identity(role)
        if role == 'proxy':
            address, cores, memory, disk = c.proxy_ip, c.proxy_cores, c.proxy_memory_mb, c.proxy_disk_gb
        else:
            address, cores, memory, disk = c.sshgw_ip, c.sshgw_cores, c.sshgw_memory_mb, c.sshgw_disk_gb
        prefix = ipaddress.IPv4Network(c.subnet).prefixlen
        self.manifest['attempted'].append({'role': role, 'id': ctid, 'hostname': hostname})
        self.save()
        self.r.run(['pct', 'create', str(ctid), c.template, '--hostname', hostname,
                    '--description', f'{DESCRIPTION_PREFIX}{self.run_id}:{role}',
                    '--storage', c.storage, '--rootfs', f'{c.storage}:{disk}',
                    '--cores', str(cores), '--memory', str(memory), '--swap', '256',
                    '--unprivileged', '1', '--onboot', '0', '--start', '0',
                    '--nameserver', c.nameserver,
                    '--net0', f'name=eth0,bridge={c.bridge},ip={address}/{prefix},gw={c.gateway},mtu={c.mtu}'],
                   label='new container creation', timeout=300)
        self.manifest['created'].append({'role': role, 'id': ctid, 'hostname': hostname})
        self.save()
        self.owned(role)
        self.r.run(['pct', 'start', str(ctid)], label='new container start')
        self.r.guest(ctid, ['test', '-f', '/etc/debian_version'], label='Debian template guard')
        release = self.r.guest(ctid, ['cat', '/etc/os-release'], label='guest OS identity')
        if not re.search(r'^VERSION_ID="?13"?$', release, re.MULTILINE):
            raise BootstrapError('New guest is not Debian 13')
        machine_id = self.r.guest(ctid, ['cat', '/etc/machine-id'],
                                  label='guest machine identity').strip()
        if not re.fullmatch(r'[0-9a-f]{32}', machine_id):
            raise BootstrapError('New guest has no valid machine identity')
        self.manifest['created'][-1]['machine_id'] = machine_id
        self.save()
        self.ensure_mtu(role)

    def ensure_mtu(self, role: str) -> None:
        ctid, _ = self.identity(role)
        # Reuse the tested parent checks and hook renderer without reusing app/DB ownership.
        manager = self.r.guest(ctid, ['sh', '-c',
            "if dpkg-query -W -f='${Status}' ifupdown2 2>/dev/null | grep -Fqx 'install ok installed'; then printf ifupdown2; elif dpkg-query -W -f='${Status}' ifupdown 2>/dev/null | grep -Fqx 'install ok installed'; then printf ifupdown; else printf unknown; fi"],
            label='guest network manager')
        if manager == 'ifupdown2':
            support = self.r.guest(ctid, ['cat', '/etc/network/ifupdown2/ifupdown2.conf'],
                                   label='ifupdown2 addon support')
            if not core.ifupdown2_addon_scripts_enabled(support):
                raise BootstrapError('ifupdown2 addon script support is not explicitly enabled')
        elif manager != 'ifupdown':
            raise BootstrapError('Guest network manager is unsupported or not installed')
        state = self.r.guest(ctid, ['sh', '-c',
            'parent=/etc/network/if-pre-up.d; if [ -L "$parent" ]; then printf symlink; elif [ -e "$parent" ] && [ ! -d "$parent" ]; then printf non-directory; elif [ ! -e "$parent" ]; then printf missing; else stat -c "%u:%g %a" -- "$parent"; fi'],
            label='guest hook parent state').strip()
        if state == 'missing':
            self.owned(role)
            self.r.guest(ctid, ['install', '-d', '-o', 'root', '-g', 'root', '-m', '0755',
                                '/etc/network/if-pre-up.d'], label='create guest hook parent')
            state = '0:0 755'
        match = re.fullmatch(r'(\d+):(\d+) ([0-7]{3,4})', state)
        if not match or match.group(1) != '0' or match.group(2) != '0' or (int(match.group(3), 8) & 0o022):
            raise BootstrapError('Guest hook parent ownership or permissions are unsafe')
        self.put(role, '/etc/network/if-pre-up.d/isolated-services-mtu',
                 core.render_guest_mtu_hook(self.c.mtu), '0755')
        self.r.guest(ctid, ['/usr/sbin/ip', 'link', 'set', 'dev', 'eth0', 'mtu', str(self.c.mtu)],
                     label='guest MTU apply')
        link = json.loads(self.r.guest(ctid, ['ip', '-j', 'link', 'show', 'dev', 'eth0'],
                                       label='guest MTU readback'))
        if len(link) != 1 or link[0].get('ifname') != 'eth0' or link[0].get('mtu') != self.c.mtu:
            raise BootstrapError('Guest eth0 MTU readback did not match the configured MTU')

    def endpoint_preflight(self, role: str, host: str, port: int, label: str) -> None:
        ctid, _ = self.identity(role)
        if (not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host)
                or not 1 <= port <= 65535):
            raise BootstrapError('Package endpoint is not one DNS hostname and TCP port')
        answers = self.r.guest(
            ctid, ['timeout', '10s', 'getent', 'ahostsv4', host],
            label=label + ' DNS resolution', timeout=15)
        addresses: list[str] = []
        for line in answers.splitlines():
            fields = line.split()
            if not fields:
                continue
            try:
                address = str(ipaddress.IPv4Address(fields[0]))
            except ipaddress.AddressValueError:
                continue
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise BootstrapError(f'{label} did not resolve to IPv4')
        for address in addresses:
            try:
                self.r.guest(
                    ctid,
                    ['timeout', '10s', 'bash', '-c',
                     'exec 3<>"/dev/tcp/$1/$2"', 'package-endpoint', address, str(port)],
                    label=label + ' TCP connect', timeout=15)
                return
            except BootstrapError:
                continue
        raise BootstrapError(f'{label} has no reachable IPv4 TCP endpoint')

    def apt_network_preflight(self, role: str) -> None:
        ctid, _ = self.identity(role)
        sources = self.r.guest(
            ctid, ['sh', '-c',
                   'for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list '
                   '/etc/apt/sources.list.d/*.sources; do '
                   '[ -f "$f" ] || continue; cat -- "$f"; printf "\\n\\n"; done'],
            label='APT source definitions', timeout=30)
        for host, port in apt_source_endpoints(sources):
            self.endpoint_preflight(role, host, port, f'APT endpoint {host}:{port}')

    def transient_package_command(self, role: str, phase: str, runtime_seconds: int,
                                  command: list[str], label: str) -> None:
        if type(runtime_seconds) is not int or runtime_seconds <= TRANSIENT_STOP_SECONDS:
            raise BootstrapError('Transient package runtime is invalid')
        ctid, _ = self.identity(role)
        self.owned(role)
        unit = transient_unit_name(self.run_id, role, phase)
        load_state = self.r.guest(
            ctid, ['systemctl', 'show', unit, '--property=LoadState', '--value'],
            label=label + ' unit collision')
        if load_state.strip() not in ('', 'not-found'):
            raise BootstrapError(f'Transient package unit already exists: {unit}')
        self.r.guest(
            ctid,
            ['systemd-run', '--wait', '--collect', '--quiet', '--expand-environment=no',
             '--unit=' + unit,
             '--property=Type=exec',
             f'--property=RuntimeMaxSec={runtime_seconds}s',
             f'--property=TimeoutStopSec={TRANSIENT_STOP_SECONDS}s',
             '--property=KillMode=control-group', '--property=SendSIGKILL=yes',
             '--property=UMask=0022',
             '--property=StandardOutput=journal', '--property=StandardError=journal',
             '--', *command],
            label=label,
            timeout=runtime_seconds + TRANSIENT_STOP_SECONDS + HOST_TIMEOUT_MARGIN_SECONDS)

    def packages_and_firewall(self, role: str) -> None:
        ctid, _ = self.identity(role)
        self.apt_network_preflight(role)
        self.put(role, '/usr/sbin/policy-rc.d', '#!/bin/sh\nexit 101\n', '0755')
        self.transient_package_command(
            role, 'debian-index', APT_UPDATE_RUNTIME_SECONDS,
            apt_update_command(), 'signed Debian index')
        self.transient_package_command(
            role, 'debian-upgrade', APT_UPGRADE_RUNTIME_SECONDS,
            ['/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/apt-get',
             'upgrade', '--with-new-pkgs', '-y'], 'new guest security updates')
        packages = ['ca-certificates', 'nftables', 'openssh-client', 'python3']
        if role == 'proxy':
            packages.extend(['curl', 'gnupg'])
        self.transient_package_command(
            role, 'debian-packages', APT_INSTALL_RUNTIME_SECONDS,
            ['/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/apt-get', 'install',
             '-y', '--no-install-recommends', *packages], 'fresh guest packages')
        if role == 'proxy':
            key = '/run/nginx_signing.key'
            self.endpoint_preflight(role, 'nginx.org', 443, 'nginx.org package endpoint')
            self.r.guest(ctid, ['test', '!', '-e', '/usr/share/keyrings/nginx-archive-keyring.gpg'],
                         label='new nginx apt keyring guard')
            self.r.guest(ctid, ['curl', '--proto', '=https', '--tlsv1.2',
                                '--connect-timeout', '10', '--max-time', '30', '-fsSLo', key,
                                core.NGINX_KEY_URL], label='official nginx signing key', timeout=40)
            detail = self.r.guest(ctid, ['gpg', '--batch', '--with-colons', '--show-keys', key],
                                  label='nginx signing key fingerprint')
            if f'fpr:::::::::{core.NGINX_SIGNING_FINGERPRINT}:' not in detail:
                raise BootstrapError('Official nginx signing fingerprint is absent')
            self.r.guest(ctid, ['gpg', '--batch', '--yes', '--dearmor', '--output',
                                '/usr/share/keyrings/nginx-archive-keyring.gpg', key],
                         label='nginx apt keyring')
            self.put(role, '/etc/apt/sources.list.d/nginx-stable.list', core.NGINX_REPOSITORY)
            self.r.guest(ctid, ['rm', '-f', key], label='remove downloaded nginx signing key')
            self.transient_package_command(
                role, 'nginx-index', APT_UPDATE_RUNTIME_SECONDS,
                apt_update_command(), 'signed nginx stable index')
            policy = self.r.guest(ctid, ['apt-cache', 'policy', 'nginx'], label='signed nginx candidate')
            candidate = re.search(r'^\s*Candidate:\s*(\S+)', policy, re.MULTILINE)
            if candidate is None or candidate.group(1) != self.c.nginx_version \
                    or 'https://nginx.org/packages/debian' not in policy:
                raise BootstrapError('Reviewed nginx.org package candidate changed')
            self.transient_package_command(
                role, 'nginx-package', APT_INSTALL_RUNTIME_SECONDS,
                ['/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/apt-get', 'install',
                 '-y', '--no-install-recommends', f'nginx={self.c.nginx_version}'],
                'pinned nginx package')
        self.r.guest(ctid, ['systemctl', 'mask', '--now', 'nftables.service'],
                     label='prevent a second guest firewall owner')
        self.put(role, '/etc/isolated-services.nft', firewall(self.c, role))
        self.put(role, '/etc/systemd/system/isolated-services-firewall.service', FIREWALL_UNIT)
        self.r.guest(ctid, ['install', '-d', '/etc/systemd/system/networking.service.d'],
                     label='network dependency directory')
        self.put(role, '/etc/systemd/system/networking.service.d/10-isolated-services.conf',
                 NETWORKING_DROPIN)
        self.r.guest(ctid, ['nft', '-c', '-f', '/etc/isolated-services.nft'],
                     label='guest firewall syntax')
        self.r.guest(ctid, ['systemctl', 'daemon-reload'], label='guest unit reload')
        self.r.guest(ctid, ['systemctl', 'enable', '--now', 'isolated-services-firewall.service'],
                     label='guest private input policy')
        self.r.guest(ctid, ['rm', '/usr/sbin/policy-rc.d'], label='remove package autostart guard')
        names = ['ca-certificates', 'nftables', 'openssh-client', 'python3']
        if role == 'proxy':
            names.extend(['curl', 'gnupg', 'nginx'])
        receipt = self.r.guest(ctid, ['dpkg-query', '-W', '-f=${Package}\t${Version}\n', *names],
                               label='installed package receipt')
        self.manifest.setdefault('package_receipts', {})[role] = receipt.splitlines()
        self.save()

    def push_artifact(self, role: str, source: bytes, target: str,
                      mode: str = '0755', owner: str = 'root:root') -> None:
        self.put(role, target, source, mode, owner)

    def proxy(self, values: dict[str, str], artifacts: dict[str, bytes]) -> None:
        role = 'proxy'
        ctid, _ = self.identity(role)
        self.r.guest(ctid, ['install', '-d', '-m', '0755', '/etc/nginx/pickle.d',
                            '/var/lib/pickle-proxy-agent'], label='proxy directories')
        self.r.guest(ctid, ['install', '-d', '-m', '0700', '/etc/pickle-proxy-agent'],
                     label='proxy config directory')
        self.put(role, '/etc/pickle-proxy-agent/agent.env', proxy_environment(self.c, values), '0600')
        self.push_artifact(role, artifacts['proxy_agent_file'], '/usr/local/bin/pickle-proxy-agent')
        self.push_artifact(role, artifacts['proxy_unit_file'],
                           '/etc/systemd/system/pickle-proxy-agent.service', '0644')
        self.push_artifact(role, artifacts['proxy_nginx_file'],
                           '/etc/nginx/conf.d/pickle-base.conf', '0644')
        self.put(role, '/etc/nginx/conf.d/isolated-services.conf', candidate_nginx(self.c))
        self.r.guest(ctid, ['rm', '-f', '/etc/nginx/conf.d/default.conf'], label='remove nginx default')
        for unit in ('nginx.service', 'pickle-proxy-agent.service'):
            directory = f'/etc/systemd/system/{unit}.d'
            self.r.guest(ctid, ['install', '-d', directory], label='service dependency directory')
            self.put(role, directory + '/10-isolated-services.conf', SERVICE_DROPIN)
        self.r.guest(ctid, ['systemctl', 'daemon-reload'], label='proxy unit reload')
        self.r.guest(ctid, ['nginx', '-t'], label='proxy nginx syntax')
        self.r.guest(ctid, ['systemd-analyze', 'verify', 'pickle-proxy-agent.service'],
                     label='proxy unit verification')
        self.r.guest(ctid, ['systemctl', 'disable', '--now', 'nginx.service',
                            'pickle-proxy-agent.service'], label='proxy services parked')

    def sshgw(self, values: dict[str, str], artifacts: dict[str, bytes]) -> None:
        role = 'sshgw'
        ctid, _ = self.identity(role)
        self.r.guest(ctid, ['useradd', '--system', '--user-group', '--home-dir', '/opt/pickle',
                            '--shell', '/usr/sbin/nologin', 'pickle'], label='sshgw account')
        self.r.guest(ctid, ['install', '-d', '-o', 'pickle', '-g', 'pickle',
                            '/opt/pickle/sshgw', '/opt/pickle/sshgw/bin'], label='sshgw directories')
        self.r.guest(ctid, ['install', '-d', '-o', 'root', '-g', 'pickle', '-m', '0750',
                            '/etc/pickle'], label='sshgw config parent')
        self.r.guest(ctid, ['install', '-d', '-o', 'pickle', '-g', 'pickle', '-m', '0750',
                            '/etc/pickle/sshgw'], label='sshgw key directory')
        self.put(role, '/etc/pickle/sshgw.env', sshgw_environment(self.c, values),
                 '0640', 'root:pickle')
        for name, comment in (('upstream_ed25519_key', 'pickle-platform-upstream'),
                              ('terminal_ed25519_key', 'pickle-terminal-bridge'),
                              ('ssh_host_ed25519_key', 'pickle-sshgw-host')):
            path = '/etc/pickle/sshgw/' + name
            self.r.guest(ctid, ['test', '!', '-e', path], label='new SSH private key guard')
            self.r.guest(ctid, ['test', '!', '-e', path + '.pub'], label='new SSH public key guard')
            self.r.guest(ctid, ['runuser', '-u', 'pickle', '--', 'ssh-keygen', '-q', '-t', 'ed25519',
                                '-N', '', '-C', comment, '-f', path], label='one-time SSH key generation')
            self.r.guest(ctid, ['chmod', '0600', path], label='SSH private key permissions')
            self.r.guest(ctid, ['chmod', '0644', path + '.pub'], label='SSH public key permissions')
        self.push_artifact(role, artifacts['sshgw_route_plugin_file'],
                           '/opt/pickle/sshgw/bin/sshgw-route-plugin', '0755', 'pickle:pickle')
        self.push_artifact(role, artifacts['sshgw_terminal_bridge_file'],
                           '/opt/pickle/sshgw/bin/sshgw-terminal-bridge', '0755', 'pickle:pickle')
        with tempfile.NamedTemporaryFile(prefix='sshpiperd-', dir=self.c.state_dir) as binary:
            with tarfile.open(fileobj=io.BytesIO(artifacts['sshpiperd_archive_file']), mode='r:gz') as archive:
                member = archive.getmember('sshpiperd')
                if not member.isfile() or member.size <= 0 or member.size > 200 * 1024 * 1024:
                    raise BootstrapError('Reviewed sshpiperd archive layout changed')
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BootstrapError('Reviewed sshpiperd archive has no binary body')
                binary.write(extracted.read())
                binary.flush()
                binary.seek(0)
                sshpiperd = binary.read()
            require_linux_amd64_elf(sshpiperd, 'sshpiperd')
            self.push_artifact(role, sshpiperd, '/usr/local/bin/sshpiperd')
            self.manifest['sshpiperd'] = {'version': SSHPIPERD_VERSION,
                                           'archive_sha256': SSHPIPERD_ASSET_SHA256,
                                           'binary_sha256': hashlib.sha256(sshpiperd).hexdigest()}
        self.push_artifact(role, artifacts['sshpiperd_unit_file'],
                           '/etc/systemd/system/sshpiperd.service', '0644')
        self.push_artifact(role, artifacts['terminal_unit_file'],
                           '/etc/systemd/system/sshgw-terminal-bridge.service', '0644')
        for unit in ('sshpiperd.service', 'sshgw-terminal-bridge.service'):
            directory = f'/etc/systemd/system/{unit}.d'
            self.r.guest(ctid, ['install', '-d', directory], label='service dependency directory')
            self.put(role, directory + '/10-isolated-services.conf', SERVICE_DROPIN)
        self.r.guest(ctid, ['systemctl', 'daemon-reload'], label='sshgw unit reload')
        self.r.guest(ctid, ['systemd-analyze', 'verify', 'sshpiperd.service',
                            'sshgw-terminal-bridge.service'], label='sshgw unit verification')
        self.r.guest(ctid, ['systemctl', 'disable', '--now', 'sshpiperd.service',
                            'sshgw-terminal-bridge.service'], label='sshgw services parked')
        self.export_public_keys()

    def export_public_keys(self) -> None:
        ctid, _ = self.identity('sshgw')
        directory = Path(self.c.state_dir) / 'public-keys'
        directory.mkdir(mode=0o700)
        metadata = {}
        for name in ('upstream_ed25519_key.pub', 'terminal_ed25519_key.pub',
                     'ssh_host_ed25519_key.pub'):
            target = directory / name
            self.r.run(['pct', 'pull', str(ctid), '/etc/pickle/sshgw/' + name, str(target)],
                       label='public key export')
            target.chmod(0o644)
            raw = target.read_bytes()
            if not raw.startswith(b'ssh-ed25519 ') or len(raw) > 1024:
                raise BootstrapError('Exported SSH public key is malformed')
            metadata[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'path': str(target)}
        self.manifest['public_keys'] = metadata

    def validate_services(self) -> None:
        proxy, sshgw = self.c.proxy_ctid, self.c.sshgw_ctid
        failure: BaseException | None = None
        try:
            self.r.guest(proxy, ['systemctl', 'start', 'nginx.service', 'pickle-proxy-agent.service'],
                         label='temporary proxy health start')
            self.r.guest(sshgw, ['systemctl', 'start', 'sshpiperd.service',
                                 'sshgw-terminal-bridge.service'], label='temporary sshgw health start')
            self.r.guest(proxy, ['systemctl', 'is-active', 'nginx.service',
                                 'pickle-proxy-agent.service'], label='proxy health')
            self.r.guest(sshgw, ['systemctl', 'is-active', 'sshpiperd.service',
                                 'sshgw-terminal-bridge.service'], label='sshgw health')
            probe = ('import socket,sys; '
                     'targets=[tuple(item.rsplit(":",1)) for item in sys.argv[1:]]; '
                     '[(lambda s:(s.settimeout(2),s.connect((host,int(port))),s.close()))(socket.socket()) '
                     'for host,port in targets]')
            self.r.guest(proxy, ['python3', '-c', probe, f'{self.c.proxy_ip}:9443'],
                         label='proxy listener health')
            self.r.guest(sshgw, ['python3', '-c', probe, '127.0.0.1:2222',
                                 f'{self.c.sshgw_ip}:8082', f'{self.c.sshgw_ip}:8083'],
                         label='sshgw listener health')
        except BaseException as error:
            failure = error
        cleanup_errors: list[BaseException] = []
        try:
            self.r.guest(proxy, ['systemctl', 'disable', '--now', 'pickle-proxy-agent.service',
                                 'nginx.service'], label='proxy health cleanup')
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            self.r.guest(sshgw, ['systemctl', 'disable', '--now', 'sshpiperd.service',
                                 'sshgw-terminal-bridge.service'], label='sshgw health cleanup')
        except BaseException as error:
            cleanup_errors.append(error)
        errors = ([failure] if failure is not None else []) + cleanup_errors
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup('Service validation or cleanup failed', errors)

    def apply(self, validate: bool) -> None:
        proxy_values, sshgw_values, artifacts, denial_raw = preflight(self.c, self.r)
        os.umask(0o077)
        Path(self.c.state_dir).mkdir(mode=0o700)
        self.save()
        self.manifest['credential_custody'] = {
            'candidate_id': self.c.candidate_id,
            'proxy_token_sha256': hashlib.sha256(
                proxy_values['PICKLE_PROXY_AGENT_TOKEN'].encode()).hexdigest(),
            'sshgw_token_sha256': hashlib.sha256(
                sshgw_values['PICKLE_SSHGW_TOKEN'].encode()).hexdigest(),
            'terminal_control_token_sha256': hashlib.sha256(
                sshgw_values['PICKLE_TERMINAL_CONTROL_TOKEN'].encode()).hexdigest(),
            'legacy_denial_list_sha256': hashlib.sha256(denial_raw).hexdigest(),
            'legacy_denial_count': len(parse_token_hashes(denial_raw)),
        }
        self.save()
        try:
            self.create('proxy')
            self.packages_and_firewall('proxy')
            self.proxy(proxy_values, artifacts)
            self.create('sshgw')
            self.packages_and_firewall('sshgw')
            self.sshgw(sshgw_values, artifacts)
            machine_ids = {row.get('machine_id') for row in self.manifest['created']}
            if len(machine_ids) != 2 or None in machine_ids:
                raise BootstrapError('New proxy and SSH gateway guests share a machine identity')
            if validate:
                self.validate_services()
            self.manifest['completed'] = True
            self.manifest['validated_services'] = validate
            self.manifest['boundary'] = ('Containers remain onboot=0 with application services disabled and stopped; '
                                         'API, public ingress, WireGuard, relay and guest VM state were not changed')
            self.save()
        except BaseException:
            self.manifest['boundary'] = ('Partial bootstrap retained; do not rerun over existing guests or destroy '
                                         'them automatically')
            self.save()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--validate-services', action='store_true')
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        if args.validate_services and not args.apply:
            raise BootstrapError('--validate-services is valid only with --apply')
        if not args.apply:
            print(json.dumps(plan(config), indent=2))
            return 0
        Bootstrap(config, Runner()).apply(args.validate_services)
        print('Candidate proxy and SSH gateway prepared; services remain disabled and stopped.')
        return 0
    except (BootstrapError, KeyError, ValueError, OSError, subprocess.TimeoutExpired,
            tarfile.TarError) as error:
        print(f'Bootstrap stopped: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
