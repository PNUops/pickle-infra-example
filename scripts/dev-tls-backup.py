#!/usr/bin/env python3
"""Pull encrypted development TLS, publish to PBS and verify an exact restore."""
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import stat
import re

STATE = Path('/var/lib/pickle-dev-tls/state.json')


def state_directory(path):
    if not path.is_absolute():
        raise ValueError('Absolute state path required')
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError('State directory custody mismatch')
    for ancestor in path.parent.parents:
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('State ancestor custody mismatch')


def read_state(path=STATE):
    state_directory(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096:
            raise ValueError('State file custody mismatch')
        value = json.loads(stream.read(4097))
    if type(value) is not dict or set(value) != {'version', 'point', 'archive_sha256', 'certificate_sha256', 'ciphertext_sha256'} or type(value['version']) is not int or value['version'] != 1 or type(value['point']) is not int or value['point'] <= 0:
        raise ValueError('Malformed state')
    for key in ('archive_sha256', 'certificate_sha256', 'ciphertext_sha256'):
        if type(value[key]) is not str or not re.fullmatch('[a-f0-9]{64}', value[key]):
            raise ValueError('Malformed state digest')
    return value


def write_state(value, path=STATE):
    state_directory(path)
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write((json.dumps(value, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(name): os.unlink(name)


def point_exists(config, point, env):
    rows = json.loads(run(['proxmox-backup-client', 'snapshot', 'list', '--output-format', 'json',
                          '--repository', config['reader_repository'], '--ns', config['namespace']], env=env))
    if type(rows) is not list:
        raise ValueError('Malformed remote snapshot inventory')
    matched = []
    for row in rows:
        if type(row) is not dict or type(row.get('backup-time')) is not int or type(row.get('files')) is not list:
            raise ValueError('Malformed remote snapshot row')
        if row.get('backup-type') == 'host' and row.get('backup-id') == config['backup_id'] and row['backup-time'] == point:
            files = [entry.get('filename') if type(entry) is dict else entry for entry in row['files']]
            if not all(type(name) is str for name in files):
                raise ValueError('Malformed remote archive inventory')
            matched.append('tls.pxar.didx' in files or 'tls.pxar' in files)
    if len(matched) > 1:
        raise ValueError('Duplicate remote snapshot')
    return bool(matched and matched[0])


def run(argv, payload=None, env=None):
    return subprocess.run(argv, input=payload, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=60,
                          env=env, check=True).stdout


def inspect_archive(raw, lineage):
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Archive too large')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        if len(members) != 9 or len(set(names)) != 9:
            raise ValueError('Unexpected archive members')
        renewal = 'renewal/' + lineage + '.conf'
        if renewal not in names or not tar.getmember(renewal).isreg():
            raise ValueError('Renewal config missing')
        material = {}
        for stem in ('cert', 'chain', 'fullchain', 'privkey'):
            name = 'live/' + lineage + '/' + stem + '.pem'
            link = tar.getmember(name)
            prefix = '../../archive/' + lineage + '/' + stem
            suffix = link.linkname.removeprefix(prefix).removesuffix('.pem')
            if not link.issym() or not link.linkname.startswith(prefix) or not suffix.isdigit():
                raise ValueError('Unexpected live link')
            target = 'archive/' + lineage + '/' + stem + suffix + '.pem'
            item = tar.getmember(target)
            if not item.isreg() or not 0 < item.size <= 1024 * 1024:
                raise ValueError('Unexpected archive file')
            material[stem] = tar.extractfile(item).read()
        records = []
        for item in sorted(members, key=lambda entry: entry.name):
            if item.issym():
                records.append([item.name, 'link', item.linkname])
            elif item.isreg():
                records.append([item.name, 'file', hashlib.sha256(tar.extractfile(item).read()).hexdigest()])
            else:
                raise ValueError('Unexpected member type')
        digest = hashlib.sha256(json.dumps(records, separators=(',', ':')).encode()).hexdigest()
    cert_pub = run(['openssl', 'x509', '-pubkey', '-noout'], material['cert'])
    key_pub = run(['openssl', 'pkey', '-pubout'], material['privkey'])
    if cert_pub != key_pub:
        raise ValueError('Certificate key mismatch')
    run(['openssl', 'x509', '-checkhost', lineage, '-noout'], material['cert'])
    run(['openssl', 'x509', '-checkend', '86400', '-noout'], material['cert'])
    return {'certificate_sha256': hashlib.sha256(material['cert']).hexdigest(), 'archive_sha256': digest}


def main():
    config = json.loads(Path('/etc/pickle-dev-tls/backup.json').read_text())
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != config['hostname']:
        raise ValueError('Wrong backup host')
    if config['namespace'] != 'dev-tls' or config['backup_id'] != 'node-dev-dev-tls':
        raise ValueError('Wrong development backup target')
    ssh = ['ssh', '-p', str(config['ssh_port']), '-o', 'BatchMode=yes',
           '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'UpdateHostKeys=no', '-o', 'IdentitiesOnly=yes',
           '-o', 'UserKnownHostsFile=' + config['known_hosts'],
           '-i', config['ssh_key'], config['ssh_destination'], 'dev-tls-export']
    ciphertext = run(ssh)
    if not 0 < len(ciphertext) < 3 * 1024 * 1024:
        raise ValueError('Ciphertext size invalid')
    decrypt = ['openssl', 'cms', '-decrypt', '-binary', '-inform', 'DER',
               '-recip', config['recipient_certificate'], '-inkey', config['recipient_key']]
    inspected = inspect_archive(run(decrypt, ciphertext), config['lineage'])
    previous = read_state()
    point = max(int(time.time()), previous['point'] + 1 if previous else 0)
    with tempfile.TemporaryDirectory(prefix='pickle-dev-tls-', dir='/run') as stage:
        folder = Path(stage)
        data = folder / 'data'
        data.mkdir(mode=0o700)
        (data / 'lineage.cms').write_bytes(ciphertext)
        common = ['--repository', config['writer_repository'], '--ns', config['namespace'],
                  '--keyfile', config['pbs_key']]
        client_home = folder / 'client-home'
        client_home.mkdir(mode=0o700)
        env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': str(client_home),
               'LANG': 'C.UTF-8'}
        env['PBS_PASSWORD_FILE'] = config['reader_token']
        env['PBS_FINGERPRINT'] = config['pbs_fingerprint']
        if previous and previous['archive_sha256'] == inspected['archive_sha256'] and point_exists(config, previous['point'], env):
            print(json.dumps({'unchanged': True, 'point': previous['point'], **inspected}, sort_keys=True))
            return
        env['PBS_PASSWORD_FILE'] = config['writer_token']
        run(['proxmox-backup-client', 'backup', 'tls.pxar:' + str(data),
             '--backup-id', config['backup_id'], '--backup-time', str(point),
             '--backup-type', 'host', '--crypt-mode', 'encrypt', *common], env=env)
        restored = folder / 'restored'
        env['PBS_PASSWORD_FILE'] = config['reader_token']
        from datetime import datetime, timezone
        snapshot = 'host/' + config['backup_id'] + '/' + datetime.fromtimestamp(point, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        run(['proxmox-backup-client', 'restore', snapshot, 'tls.pxar', str(restored),
             '--repository', config['reader_repository'], '--ns', config['namespace'],
             '--keyfile', config['pbs_key']], env=env)
        returned = (restored / 'lineage.cms').read_bytes()
        if returned != ciphertext or inspect_archive(run(decrypt, returned), config['lineage']) != inspected:
            raise ValueError('PBS restore mismatch')
    write_state({'version': 1, 'point': point, **inspected,
                 'ciphertext_sha256': hashlib.sha256(ciphertext).hexdigest()})
    print(json.dumps({'snapshot': snapshot, **inspected,
                      'ciphertext_sha256': hashlib.sha256(ciphertext).hexdigest(),
                      'restore_verified': True}, sort_keys=True))


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError):
        print('Development TLS backup failed', file=sys.stderr)
        sys.exit(1)
