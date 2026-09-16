#!/usr/bin/env python3
"""Create two new, isolated Debian containers without touching an existing guest."""
from __future__ import annotations

import argparse
import base64
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


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    expected_node: str
    expected_cluster: str
    bridge: str
    subnet: str
    gateway: str
    app_ip: str
    db_ip: str
    proxy_ip: str
    nameserver: str
    mtu: int
    app_ctid: int
    db_ctid: int
    app_hostname: str
    db_hostname: str
    app_cores: int
    db_cores: int
    app_memory_mb: int
    db_memory_mb: int
    app_disk_gb: int
    db_disk_gb: int
    storage_reserve_gb: int
    storage: str
    template: str
    template_sha256: str
    postgresql_version: str
    jre_version: str
    db_name: str
    db_role: str
    api_env_file: str
    db_password_file: str
    db_ca_file: str
    db_cert_file: str
    db_key_file: str
    state_dir: str

    @classmethod
    def load(cls, path: Path) -> Config:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise BootstrapError('Configuration must contain exactly the documented fields')
        config = cls(**value)
        config.validate()
        return config

    def validate(self) -> None:
        for name in ('expected_node', 'expected_cluster', 'app_hostname', 'db_hostname', 'storage'):
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', getattr(self, name)):
                raise BootstrapError(f'Invalid {name}')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,14}', self.bridge):
            raise BootstrapError('Invalid bridge')
        if self.app_hostname == self.db_hostname:
            raise BootstrapError('Application and database hostnames must differ')
        network = ipaddress.IPv4Network(self.subnet, strict=True)
        if not 16 <= network.prefixlen <= 28:
            raise BootstrapError('Use an explicit IPv4 infrastructure subnet between /16 and /28')
        addresses = [ipaddress.IPv4Address(getattr(self, name)) for name in ('gateway', 'app_ip', 'db_ip', 'proxy_ip')]
        if len(set(addresses)) != 4 or any(a not in network or a in (network.network_address, network.broadcast_address) for a in addresses):
            raise BootstrapError('Core addresses must be distinct usable addresses in the subnet')
        ipaddress.IPv4Address(self.nameserver)
        for name in ('app_ctid', 'db_ctid'):
            if type(getattr(self, name)) is not int or not 100 <= getattr(self, name) <= 999:
                raise BootstrapError('Explicit container IDs must be in 100-999')
        if self.app_ctid == self.db_ctid:
            raise BootstrapError('Container IDs must differ')
        for name in ('app_cores', 'db_cores', 'app_memory_mb', 'db_memory_mb', 'app_disk_gb', 'db_disk_gb', 'storage_reserve_gb'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise BootstrapError(f'Invalid {name}')
        if type(self.mtu) is not int or not 1280 <= self.mtu <= 1500:
            raise BootstrapError('Invalid MTU')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+:vztmpl/debian-13-standard_[a-zA-Z0-9.+_-]+_amd64\.tar\.(?:zst|gz|xz)', self.template):
            raise BootstrapError('Supply an explicit Debian 13 amd64 PVE template volume')
        if not re.fullmatch(r'[0-9a-f]{64}', self.template_sha256):
            raise BootstrapError('Supply the verified template SHA-256')
        if not re.fullmatch(r'18\.[0-9]+-[A-Za-z0-9.+~]+', self.postgresql_version):
            raise BootstrapError('Pin a stable PostgreSQL 18 package version')
        if not re.fullmatch(r'25\.[A-Za-z0-9.+~:-]+', self.jre_version):
            raise BootstrapError('Pin the Java 25 package version used by the application')
        for name in ('db_name', 'db_role'):
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', getattr(self, name)):
                raise BootstrapError(f'Invalid {name}')
        for name in ('api_env_file', 'db_password_file', 'db_ca_file', 'db_cert_file', 'db_key_file', 'state_dir'):
            if not isinstance(getattr(self, name), str) or not Path(getattr(self, name)).is_absolute():
                raise BootstrapError(f'{name} must be an explicit absolute path')


def plan(c: Config) -> dict:
    return {
        'mode': 'dry-run', 'expected_node': c.expected_node, 'expected_cluster': c.expected_cluster,
        'template': c.template, 'template_sha256': c.template_sha256,
        'containers': [
            {'id': c.db_ctid, 'hostname': c.db_hostname, 'role': 'database', 'address': c.db_ip,
             'disk_gib': c.db_disk_gb, 'memory_mib': c.db_memory_mb, 'cores': c.db_cores},
            {'id': c.app_ctid, 'hostname': c.app_hostname, 'role': 'application', 'address': c.app_ip,
             'disk_gib': c.app_disk_gb, 'memory_mib': c.app_memory_mb, 'cores': c.app_cores}],
        'network': {'bridge': c.bridge, 'subnet': c.subnet, 'gateway': c.gateway, 'mtu': c.mtu},
        'packages': {'postgresql-18': c.postgresql_version, 'postgresql-client-18': c.postgresql_version,
                     'openjdk-25-jre-headless': c.jre_version},
        'credential_paths': {name: getattr(c, name) for name in
                             ('api_env_file', 'db_password_file', 'db_ca_file', 'db_cert_file', 'db_key_file')},
        'database': {'name': c.db_name, 'role': c.db_role, 'tls': 'verify-full', 'allowed_client': c.app_ip + '/32'},
        'state_dir': c.state_dir, 'api_started': False, 'schema_or_inventory_registered': False,
        'rollback': 'Retain both new containers and volumes; inspect the ownership manifest, then stop them. No automatic deletion.',
    }


class Runner:
    def run(self, args: list[str], *, data: bytes | None = None, label: str = 'command', timeout: int = 120) -> str:
        result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            # SQL and package errors may include input: never echo raw stderr or stdin.
            raise BootstrapError(f'{label} failed (exit {result.returncode}); inspect that owned resource separately')
        return result.stdout.decode()

    def guest(self, ctid: int, args: list[str], **kwargs) -> str:
        return self.run(['pct', 'exec', str(ctid), '--', *args], **kwargs)


def protected_file(path: str, *, private: bool) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise BootstrapError('Credential input must be readable and must not be a symlink') from error
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise BootstrapError('Credential input must be a regular file owned by the invoking account')
        if (stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)) != 0:
            raise BootstrapError('Credential input permissions are too broad')
        if info.st_size > 1024 * 1024:
            raise BootstrapError('Credential input is unexpectedly large')
        return source.read()


def validate_fresh_api_env(raw: bytes) -> None:
    required = {'PICKLE_JWT_SECRET', 'PICKLE_CREDENTIALS_KEY', 'PICKLE_SEED_SYSADMIN_EMAIL',
                'PICKLE_SEED_SYSADMIN_PASSWORD', 'PICKLE_SEED_ORGADMIN_EMAIL', 'PICKLE_SEED_ORGADMIN_PASSWORD'}
    values = {}
    for line in raw.decode().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in required or key in values or not value or re.search(r'[\s\x00"\'`$\\]', value):
            raise BootstrapError('API input must contain only the six documented fresh credential assignments')
        values[key] = value
    if set(values) != required or len(values['PICKLE_JWT_SECRET']) < 32:
        raise BootstrapError('Required fresh API credentials are missing or too short')
    try:
        if len(base64.b64decode(values['PICKLE_CREDENTIALS_KEY'], validate=True)) != 32:
            raise ValueError()
    except ValueError as error:
        raise BootstrapError('Credential encryption key must decode to 32 bytes') from error
    if any(len(values[name]) < 16 for name in ('PICKLE_SEED_SYSADMIN_PASSWORD', 'PICKLE_SEED_ORGADMIN_PASSWORD')):
        raise BootstrapError('Fresh bootstrap account passwords must be at least 16 characters')


def preflight(c: Config, r: Runner) -> dict[str, bytes]:
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != c.expected_node:
        raise BootstrapError('Apply requires root on the exact expected node')
    status = json.loads(r.run(['pvesh', 'get', '/cluster/status', '--output-format', 'json'], label='cluster status'))
    cluster = next((row for row in status if row.get('type') == 'cluster'), {})
    if cluster.get('name') != c.expected_cluster or not cluster.get('quorate'):
        raise BootstrapError('The expected cluster must have quorum')
    guests = json.loads(r.run(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'], label='guest inventory'))
    if any(int(row.get('vmid', -1)) in (c.app_ctid, c.db_ctid) for row in guests):
        raise BootstrapError('A requested CTID is already used anywhere in the cluster')
    for ctid in (c.app_ctid, c.db_ctid):
        if any(Path('/etc/pve/nodes').glob(f'*/lxc/{ctid}.conf')) or any(Path('/etc/pve/nodes').glob(f'*/qemu-server/{ctid}.conf')):
            raise BootstrapError('A requested guest configuration already exists')
    if Path(c.state_dir).exists() or Path(c.state_dir).is_symlink():
        raise BootstrapError('State directory already exists; never overwrite or resume blindly')
    if not Path(c.state_dir).parent.is_dir():
        raise BootstrapError('Create the protected state parent directory explicitly first')
    parent = Path(c.state_dir).parent
    parent_info = parent.stat()
    if parent.resolve() != parent or parent_info.st_uid != 0 or stat.S_IMODE(parent_info.st_mode) & 0o077:
        raise BootstrapError('State parent must be a root-owned 0700 directory without symlink traversal')
    links = json.loads(r.run(['ip', '-j', 'address', 'show', 'dev', c.bridge], label='bridge inventory'))
    if len(links) != 1 or links[0].get('mtu') != c.mtu or not any(a.get('local') == c.gateway for a in links[0].get('addr_info', [])):
        raise BootstrapError('The bridge, gateway address and measured MTU must already be configured')
    store = json.loads(r.run(['pvesh', 'get', f'/nodes/{c.expected_node}/storage/{c.storage}/status',
                             '--output-format', 'json'], label='storage capacity'))
    needed = (c.app_disk_gb + c.db_disk_gb + c.storage_reserve_gb) * 1024**3
    if not store.get('active') or int(store.get('avail', 0)) < needed:
        raise BootstrapError('Target storage lacks the explicitly reserved headroom')
    volumes = json.loads(r.run(['pvesh', 'get', f'/nodes/{c.expected_node}/storage/{c.storage}/content',
                               '--output-format', 'json'], label='orphan volume inventory'))
    if any(int(row.get('vmid', -1)) in (c.app_ctid, c.db_ctid) for row in volumes):
        raise BootstrapError('A requested guest ID already owns a volume; do not reuse it')
    for address in (c.app_ip, c.db_ip):
        r.run(['arping', '-D', '-I', c.bridge, '-c', '3', '-w', '5', address], label='duplicate address detection')
    template = Path(r.run(['pvesm', 'path', c.template], label='template path').strip())
    if not template.is_file() or template.is_symlink():
        raise BootstrapError('A verified local template archive is required; no automatic download')
    with template.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != c.template_sha256:
            raise BootstrapError('Template checksum mismatch')
    inputs = {name: protected_file(getattr(c, name), private=name in ('api_env_file', 'db_password_file', 'db_key_file'))
              for name in ('api_env_file', 'db_password_file', 'db_ca_file', 'db_cert_file', 'db_key_file')}
    validate_fresh_api_env(inputs['api_env_file'])
    password = inputs['db_password_file'].decode().rstrip('\n')
    if not re.fullmatch(r'[A-Za-z0-9_+=/.-]{32,128}', password):
        raise BootstrapError('DB password must be one freshly generated 32-128 character base64-safe line')
    with tempfile.TemporaryDirectory(prefix='isolated-core-tls-') as temporary:
        paths = {}
        for name in ('db_ca_file', 'db_cert_file', 'db_key_file'):
            path = Path(temporary) / name
            path.write_bytes(inputs[name])
            path.chmod(0o600)
            paths[name] = str(path)
        r.run(['openssl', 'verify', '-purpose', 'sslserver', '-verify_hostname', c.db_hostname,
               '-CAfile', paths['db_ca_file'], paths['db_cert_file']], label='DB TLS chain and hostname')
        cert_public = r.run(['openssl', 'x509', '-in', paths['db_cert_file'], '-pubkey', '-noout'], label='certificate public key')
        private_public = r.run(['openssl', 'pkey', '-in', paths['db_key_file'], '-passin', 'pass:', '-pubout'], label='DB private-key match')
        if not cert_public or cert_public != private_public:
            raise BootstrapError('DB certificate and private key do not match')
    inputs['db_password_file'] = password.encode()
    return inputs


def db_config(c: Config) -> tuple[str, str]:
    config = f"""listen_addresses = '127.0.0.1,{c.db_ip}'
ssl = on
ssl_cert_file = '/etc/postgresql/isolated-core/server.crt'
ssl_key_file = '/etc/postgresql/isolated-core/server.key'
ssl_min_protocol_version = 'TLSv1.2'
password_encryption = 'scram-sha-256'
hba_file = '/etc/postgresql/isolated-core/pg_hba.conf'
"""
    hba = f"""local all postgres peer
local all all reject
hostnossl all all 0.0.0.0/0 reject
hostnossl all all ::/0 reject
hostssl {c.db_name} {c.db_role} {c.app_ip}/32 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::/0 reject
"""
    return config, hba


def firewall(c: Config, role: str) -> str:
    address, port = (c.app_ip, 5432) if role == 'database' else (c.proxy_ip, 80)
    return f"""add table inet isolated_core
flush table inet isolated_core
table inet isolated_core {{
    chain input {{
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        ip saddr {address} tcp dport {port} accept
        ip protocol icmp icmp type {{ destination-unreachable, time-exceeded, parameter-problem }} accept
        ip6 nexthdr ipv6-icmp icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert }} accept
    }}
    chain forward {{ type filter hook forward priority filter; policy drop; }}
    chain output {{ type filter hook output priority filter; policy accept; }}
}}
"""


API_UNIT = """[Unit]
Description=Isolated Pickle API
After=network-online.target
Wants=network-online.target
Requires=isolated-core-firewall.service
After=isolated-core-firewall.service
ConditionPathExists=/etc/pickle/allow-api-start

[Service]
User=pickle
Group=pickle
EnvironmentFile=/etc/pickle/api.env
EnvironmentFile=/etc/pickle/core.env
WorkingDirectory=/opt/pickle/api
ExecStart=/usr/bin/java -Xmx2g -jar /opt/pickle/api/current.jar --spring.profiles.active=dev --server.address=127.0.0.1 --jobrunr.background-job-server.enabled=false --jobrunr.dashboard.enabled=false --pickle.network-policy.enabled=false
Restart=on-failure
RestartSec=5
MemoryMax=3G
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def api_environment(c: Config, password: bytes) -> bytes:
    return (f"SPRING_PROFILES_ACTIVE=dev\nSERVER_ADDRESS=127.0.0.1\n"
            f"PICKLE_DB_URL=jdbc:postgresql://{c.db_hostname}:5432/{c.db_name}?sslmode=verify-full&sslrootcert=/etc/pickle/db-ca.crt\n"
            f"PICKLE_DB_USER={c.db_role}\nPICKLE_DB_PASSWORD={password.decode()}\n"
            "JOBRUNR_BACKGROUND_JOB_SERVER_ENABLED=false\nPICKLE_JOBRUNR_DASH_ENABLED=false\n"
            "PICKLE_NETWORK_POLICY_ENABLED=false\n").encode()


def nginx(c: Config) -> str:
    return f"""server {{
    listen {c.app_ip}:80;
    server_name {c.app_hostname};
    allow {c.proxy_ip};
    deny all;
    root /var/www/pickle-console;
    add_header X-Content-Type-Options nosniff always;
    location /api/ {{
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $http_x_real_ip;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }}
    location / {{ try_files $uri $uri/ /index.html; }}
}}
"""


class Bootstrap:
    def __init__(self, config: Config, runner: Runner):
        self.c, self.r = config, runner
        self.run_id = str(uuid.uuid4())
        self.manifest = {'run_id': self.run_id, 'plan': plan(config), 'attempted': [], 'created': [], 'completed': False}

    def save(self) -> None:
        state = Path(self.c.state_dir)
        temp = state / '.manifest.tmp'
        with temp.open('x') as target:
            json.dump(self.manifest, target, indent=2)
        os.replace(temp, state / 'manifest.json')

    def owned(self, ctid: int, hostname: str) -> None:
        text = self.r.run(['pct', 'config', str(ctid)], label='owned container identity')
        values = dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line)
        if values.get('hostname') != hostname or values.get('description') != 'isolated-core:' + self.run_id:
            raise BootstrapError('Container ownership changed; refusing further writes')

    def put(self, ctid: int, path: str, content: bytes | str, mode: str = '0644', owner: str = 'root:root') -> None:
        if isinstance(content, str):
            content = content.encode()
        self.owned(ctid, self.c.db_hostname if ctid == self.c.db_ctid else self.c.app_hostname)
        self.r.guest(ctid, ['test', '!', '-e', path], label='new guest file guard')
        self.r.guest(ctid, ['test', '!', '-L', path], label='guest symlink guard')
        with tempfile.NamedTemporaryFile(prefix='isolated-core-', dir=self.c.state_dir) as source:
            source.write(content)
            source.flush()
            self.r.run(['pct', 'push', str(ctid), source.name, path], label='new owned guest file')
        self.r.guest(ctid, ['chown', owner, path], label='guest file owner')
        self.r.guest(ctid, ['chmod', mode, path], label='guest file permissions')

    def create(self, role: str) -> int:
        c = self.c
        is_db = role == 'database'
        ctid, hostname = (c.db_ctid, c.db_hostname) if is_db else (c.app_ctid, c.app_hostname)
        disk, cores, memory = (c.db_disk_gb, c.db_cores, c.db_memory_mb) if is_db else (c.app_disk_gb, c.app_cores, c.app_memory_mb)
        address = c.db_ip if is_db else c.app_ip
        prefix = ipaddress.IPv4Network(c.subnet).prefixlen
        self.manifest['attempted'].append({'id': ctid, 'hostname': hostname, 'role': role})
        self.save()
        self.r.run(['pct', 'create', str(ctid), c.template, '--hostname', hostname,
                    '--description', 'isolated-core:' + self.run_id, '--storage', c.storage,
                    '--rootfs', f'{c.storage}:{disk}', '--cores', str(cores), '--memory', str(memory),
                    '--swap', '512', '--unprivileged', '1', '--onboot', '0', '--start', '0',
                    '--nameserver', c.nameserver,
                    '--net0', f'name=eth0,bridge={c.bridge},ip={address}/{prefix},gw={c.gateway},mtu={c.mtu}'],
                   label='new container creation', timeout=300)
        self.manifest['created'].append({'id': ctid, 'hostname': hostname, 'role': role})
        self.save()
        self.owned(ctid, hostname)
        self.r.run(['pct', 'start', str(ctid)], label='new container start')
        self.r.guest(ctid, ['test', '-f', '/etc/debian_version'], label='Debian template guard')
        os_release = self.r.guest(ctid, ['cat', '/etc/os-release'], label='guest OS identity')
        if not re.search(r'^VERSION_ID="?13"?$', os_release, re.MULTILINE):
            raise BootstrapError('New guest is not Debian 13')
        return ctid

    def packages(self, ctid: int, role: str) -> None:
        r, c = self.r, self.c
        self.owned(ctid, c.db_hostname if role == 'database' else c.app_hostname)
        self.put(ctid, '/usr/sbin/policy-rc.d', '#!/bin/sh\n# Bootstrap-owned; suppress package service autostarts.\nexit 101\n', '0755')
        r.guest(ctid, ['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'update'], label='signed Debian index', timeout=300)
        r.guest(ctid, ['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'upgrade', '--with-new-pkgs', '-y'],
                label='new guest security updates', timeout=900)
        r.guest(ctid, ['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '--no-install-recommends',
                       'ca-certificates', 'curl', 'postgresql-common', 'nftables', 'python3'], label='guest bootstrap packages', timeout=600)
        # The Debian-signed postgresql-common package supplies the official PGDG installer.
        r.guest(ctid, ['bash', '/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh', '-y'],
                label='official signed PGDG repository', timeout=300)
        package_versions = {'postgresql-client-18': c.postgresql_version}
        if role == 'database':
            r.guest(ctid, ['systemctl', 'mask', 'postgresql.service', 'postgresql@18-main.service'], label='prevent premature database start')
            package_versions['postgresql-18'] = c.postgresql_version
        else:
            package_versions['openjdk-25-jre-headless'] = c.jre_version
        for package, expected in package_versions.items():
            policy = r.guest(ctid, ['apt-cache', 'policy', package], label='signed package candidate')
            candidate = re.search(r'^\s*Candidate:\s*(\S+)', policy, re.MULTILINE)
            if candidate is None or candidate.group(1) != expected:
                raise BootstrapError('Signed package candidate changed; review versions before continuing')
        install = [f'{package}={version}' for package, version in package_versions.items()]
        if role == 'application':
            install.append('nginx')
        r.guest(ctid, ['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '--no-install-recommends', *install],
                label='pinned guest packages', timeout=900)
        r.guest(ctid, ['systemctl', 'mask', '--now', 'nftables.service'], label='prevent a second guest firewall owner')
        self.put(ctid, '/etc/isolated-core.nft', firewall(c, role))
        self.put(ctid, '/etc/systemd/system/isolated-core-firewall.service',
                 '[Unit]\nDescription=Isolated core input policy\nDefaultDependencies=no\nAfter=local-fs.target\n'
                 'Before=network-pre.target shutdown.target\nWants=network-pre.target\nConflicts=shutdown.target\n'
                 '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/sbin/nft -f /etc/isolated-core.nft\n'
                 '[Install]\nWantedBy=multi-user.target\n')
        r.guest(ctid, ['nft', '-c', '-f', '/etc/isolated-core.nft'], label='guest firewall syntax')
        r.guest(ctid, ['systemctl', 'daemon-reload'], label='guest unit reload')
        r.guest(ctid, ['systemctl', 'enable', '--now', 'isolated-core-firewall.service'], label='guest private input policy')
        r.guest(ctid, ['rm', '/usr/sbin/policy-rc.d'], label='remove the bootstrap-owned autostart guard')
        receipt = r.guest(ctid, ['dpkg-query', '-W', '-f=${Package}\t${Version}\n', *package_versions], label='installed package receipt')
        self.manifest.setdefault('package_receipts', {})[str(ctid)] = receipt.splitlines()
        self.save()

    def database(self, ctid: int, inputs: dict[str, bytes]) -> None:
        c, r = self.c, self.r
        self.owned(ctid, c.db_hostname)
        r.guest(ctid, ['install', '-d', '-o', 'postgres', '-g', 'postgres', '-m', '0700', '/etc/postgresql/isolated-core'], label='new DB TLS directory')
        for name, target, mode in [('db_cert_file', 'server.crt', '0644'), ('db_key_file', 'server.key', '0600')]:
            self.put(ctid, '/etc/postgresql/isolated-core/' + target, inputs[name], mode, 'postgres:postgres')
        config, hba = db_config(c)
        self.put(ctid, '/etc/postgresql/isolated-core/pg_hba.conf', hba, '0600', 'postgres:postgres')
        self.put(ctid, '/etc/postgresql/18/main/conf.d/90-isolated-core.conf', config, '0644', 'postgres:postgres')
        r.guest(ctid, ['install', '-d', '/etc/systemd/system/postgresql@18-main.service.d'], label='database dependency directory')
        self.put(ctid, '/etc/systemd/system/postgresql@18-main.service.d/10-isolated-core.conf',
                 '[Unit]\nRequires=isolated-core-firewall.service\nAfter=isolated-core-firewall.service\n')
        r.guest(ctid, ['systemctl', 'daemon-reload'], label='database dependency reload')
        r.guest(ctid, ['systemctl', 'unmask', 'postgresql.service', 'postgresql@18-main.service'], label='database startup preparation')
        r.guest(ctid, ['systemctl', 'enable', '--now', 'postgresql@18-main.service'], label='private TLS database start')
        query = f"SELECT count(*) FROM pg_roles WHERE rolname='{c.db_role}'; SELECT count(*) FROM pg_database WHERE datname='{c.db_name}';"
        check = r.guest(ctid, ['runuser', '-u', 'postgres', '--', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1'],
                        data=query.encode(), label='fresh DB identity guard')
        if check.strip().splitlines() != ['0', '0']:
            raise BootstrapError('The requested database or role already exists; refusing replacement')
        sql = ("SET password_encryption='scram-sha-256';\n"
               f"CREATE ROLE \"{c.db_role}\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '{inputs['db_password_file'].decode()}';\n"
               f"CREATE DATABASE \"{c.db_name}\" OWNER \"{c.db_role}\";\n")
        r.guest(ctid, ['runuser', '-u', 'postgres', '--', 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1'],
                data=sql.encode(), label='fresh role and empty database creation')

    def application(self, ctid: int, inputs: dict[str, bytes]) -> None:
        c, r = self.c, self.r
        self.owned(ctid, c.app_hostname)
        r.guest(ctid, ['useradd', '--system', '--user-group', '--home-dir', '/opt/pickle', '--shell', '/usr/sbin/nologin', 'pickle'], label='new application account')
        r.guest(ctid, ['install', '-d', '-o', 'pickle', '-g', 'pickle', '/opt/pickle/api/releases', '/var/lib/pickle'], label='application directories')
        r.guest(ctid, ['chmod', '0700', '/var/lib/pickle'], label='private mock-mail directory')
        r.guest(ctid, ['install', '-d', '-o', 'root', '-g', 'pickle', '-m', '0750', '/etc/pickle'], label='new application config directory')
        r.guest(ctid, ['install', '-d', '/var/www/pickle-console'], label='console directory')
        self.put(ctid, '/etc/pickle/api.env', inputs['api_env_file'], '0640', 'root:pickle')
        self.put(ctid, '/etc/pickle/core.env', api_environment(c, inputs['db_password_file']), '0640', 'root:pickle')
        self.put(ctid, '/etc/pickle/db-ca.crt', inputs['db_ca_file'], '0644')
        self.put(ctid, '/etc/systemd/system/pickle-api.service', API_UNIT)
        self.put(ctid, '/etc/nginx/conf.d/isolated-core.conf', nginx(c))
        r.guest(ctid, ['install', '-d', '/etc/systemd/system/nginx.service.d'], label='proxy dependency directory')
        self.put(ctid, '/etc/systemd/system/nginx.service.d/10-isolated-core.conf',
                 '[Unit]\nRequires=isolated-core-firewall.service\nAfter=isolated-core-firewall.service\n')
        # Append only on this newly created guest, outside the PVE-managed hosts block.
        r.guest(ctid, ['python3', '-c',
                      "import pathlib,sys; p=pathlib.Path('/etc/hosts'); p.open('a').write('\\n'+sys.argv[1]+' '+sys.argv[2]+'\\n')",
                      c.db_ip, c.db_hostname], label='private DB hostname')
        r.guest(ctid, ['systemctl', 'daemon-reload'], label='application unit reload')
        # The absent allow-api-start marker independently prevents accidental starts.
        r.guest(ctid, ['systemctl', 'disable', 'pickle-api.service'], label='defer API startup')
        r.guest(ctid, ['nginx', '-t'], label='private application proxy syntax')
        r.guest(ctid, ['systemctl', 'enable', '--now', 'nginx'], label='private console proxy start')
        connection = (f'host={c.db_hostname} hostaddr={c.db_ip} port=5432 dbname={c.db_name} '
                      f'user={c.db_role} sslmode=verify-full sslrootcert=/etc/pickle/db-ca.crt connect_timeout=10')
        bridge = "import os,sys; os.environ['PGPASSWORD']=sys.stdin.read(); os.execvp(sys.argv[1],sys.argv[1:])"
        result = r.guest(ctid, ['runuser', '-u', 'pickle', '--', 'python3', '-c', bridge,
                               'psql', connection, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-c',
                               'SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()'],
                         data=inputs['db_password_file'], label='application-to-DB verify-full SCRAM connection')
        if result.strip() != 't':
            raise BootstrapError('The database connection did not confirm TLS')

    def apply(self) -> None:
        c = self.c
        inputs = preflight(c, self.r)
        os.umask(0o077)
        Path(c.state_dir).mkdir(mode=0o700)
        self.save()
        try:
            db = self.create('database')
            self.packages(db, 'database')
            self.database(db, inputs)
            app = self.create('application')
            self.packages(app, 'application')
            self.application(app, inputs)
            self.manifest['completed'] = True
            self.manifest['boundary'] = 'Bootstrap only: API not started, no application schema, inventory, imported data or public routing'
            self.save()
        except BaseException:
            self.manifest['boundary'] = 'Partial bootstrap retained; do not rerun over existing guests or destroy them automatically'
            self.save()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        if not args.apply:
            print(json.dumps(plan(config), indent=2))
            return 0
        Bootstrap(config, Runner()).apply()
        print('Isolated containers prepared. API, schema, inventory and public routing remain inactive.')
        return 0
    except (BootstrapError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f'Bootstrap stopped: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
