#!/usr/bin/env python3
"""Replace only CT1200's health vhost for a narrow synthetic login trial."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess


STAGE = Path('/root/pickle-synthetic-login-ingress')
SOURCE = STAGE / 'candidate-synthetic-login.conf'
TARGET = Path('/etc/nginx/conf.d/pickle-interim-tls.conf')
BACKUP = STAGE / 'ct1200-inner-health.before.conf'
LOCK = Path('/run/lock/pickle-synthetic-login-ingress.lock')
BASE_SHA = '4a457f2d58fe16f8d11af4b6be6f3a4c8e8c73ac52b8d9010eb8152964c3346c'
TRIAL_SHA = '81c3205d89587ed8538243a6996f32cbd2631bb446cbf81d15a4cec65d68b185'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pinned(path, expected, mode):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
            info.st_gid == 0 and info.st_nlink == 1 and
            stat.S_IMODE(info.st_mode) == mode, f'file metadata differs: {path}')
    data = path.read_bytes()
    require(digest(data) == expected, f'file hash differs: {path}')
    return data


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f'{command[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def atomic(path, data, mode):
    temporary = path.parent / ('.' + path.name + '.synthetic-login')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def probe(path, source, host='staging.example.com', method='GET'):
    raw = socket.create_connection(('127.0.0.1', 8443), timeout=3)
    raw.sendall(f'PROXY TCP4 {source} 127.0.0.1 50123 8443\r\n'.encode())
    context = ssl.create_default_context()
    with context.wrap_socket(raw, server_hostname='staging.example.com') as conn:
        conn.settimeout(5)
        conn.sendall((f'{method} {path} HTTP/1.1\r\nHost: {host}\r\n'
                      'X-Forwarded-For: 203.0.113.77\r\nConnection: close\r\n\r\n').encode())
        response = bytearray()
        while True:
            part = conn.recv(8192)
            if not part:
                break
            response.extend(part)
            require(len(response) < 1048576, 'probe response too large')
    return bytes(response).split(b'\r\n', 1)[0].split()[1].decode()


def matrix(trial):
    require(probe('/__ingress_probe', '203.0.113.14') == '200',
            'existing health route failed')
    require(probe('/', '203.0.113.14') == '404', 'root opened')
    require(probe('/api/v1/admin', '203.0.113.14') == '404', 'admin route opened')
    require(probe('/api/v1/vms', '203.0.113.14') == '404', 'VM route opened')
    require(probe('/api/v1/llm-keys', '203.0.113.14') == '404', 'LLM route opened')
    require(probe('/api/auth/login', '203.0.113.14') == '404',
            'unversioned auth route opened')
    require(probe('/__ingress_probe', '198.51.100.99') == '403',
            'unlisted client opened')
    require(probe('/__ingress_probe', '203.0.113.14', host='other.example') == '421',
            'wrong Host opened')
    if trial:
        require(probe('/login', '203.0.113.14') == '200', 'login index unavailable')
        require(probe('/login', '203.0.113.14', method='HEAD') == '200',
                'login HEAD unavailable')
        require(probe('/api/v1/meta/status', '203.0.113.14') == '200',
                'API status unavailable')
        require(probe('/login', '203.0.113.14', method='POST') == '404',
                'login POST opened')
        require(probe('/api/v1/auth/login', '203.0.113.14') == '404',
                'auth GET opened')
        require(probe('/api/v1/me', '203.0.113.14', method='POST') == '404',
                'me POST opened')
    else:
        require(probe('/login', '203.0.113.14') == '404',
                'login remains open after recovery')


def recover():
    original = pinned(BACKUP, BASE_SHA, 0o600)
    current = TARGET.read_bytes()
    require(digest(current) in (BASE_SHA, TRIAL_SHA), 'unknown active vhost')
    pinned(TARGET, digest(current), 0o644)
    if digest(current) == TRIAL_SHA:
        atomic(TARGET, original, 0o644)
    run('nginx', '-t')
    run('nginx', '-s', 'reload')
    pinned(TARGET, BASE_SHA, 0o644)
    matrix(False)
    print(json.dumps({'mode': 'recover', 'target_sha256': BASE_SHA,
                      'health_only_new_connections': True,
                      'existing_sessions_not_revoked': True}, sort_keys=True))


def apply():
    source = pinned(SOURCE, TRIAL_SHA, 0o600)
    original = pinned(TARGET, BASE_SHA, 0o644)
    backup_exists = BACKUP.exists() or BACKUP.is_symlink()
    if backup_exists:
        pinned(BACKUP, BASE_SHA, 0o600)
    require(run('systemctl', 'is-active', 'nginx.service') == 'active',
            'nginx is inactive')
    run('nginx', '-t')
    matrix(False)
    if not backup_exists:
        atomic(BACKUP, original, 0o600)
    pinned(BACKUP, BASE_SHA, 0o600)
    try:
        atomic(TARGET, source, 0o644)
        run('nginx', '-t')
        run('nginx', '-s', 'reload')
        pinned(TARGET, TRIAL_SHA, 0o644)
        matrix(True)
    except BaseException as error:
        try:
            recover()
        except BaseException as rollback_error:
            raise RuntimeError(f'apply failed: {error}; recovery failed: '
                               f'{rollback_error}') from error
        raise RuntimeError(f'apply failed: {error}; health-only restored') from error
    print(json.dumps({'mode': 'apply', 'target_sha256': TRIAL_SHA,
                      'backup_sha256': BASE_SHA, 'probe_matrix': True}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('apply', 'recover'))
    args = parser.parse_args()
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0]
            == 'example-proxy', 'run only as root inside CT1200')
    info = STAGE.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and
            stat.S_IMODE(info.st_mode) == 0o700, 'stage must be root:0700')
    os.umask(0o077)
    lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+') as lock:
        lock_info = os.fstat(lock.fileno())
        require(stat.S_ISREG(lock_info.st_mode) and lock_info.st_uid == 0 and
                lock_info.st_nlink == 1 and stat.S_IMODE(lock_info.st_mode) == 0o600,
                'lock file metadata differs')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.mode == 'apply':
            apply()
        else:
            recover()


if __name__ == '__main__':
    main()
