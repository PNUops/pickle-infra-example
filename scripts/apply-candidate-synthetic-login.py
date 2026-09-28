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
import time


STAGE = Path('/root/pickle-synthetic-login-ingress')
SOURCE = STAGE / 'candidate-synthetic-login.conf'
TARGET = Path('/etc/nginx/conf.d/pickle-interim-tls.conf')
BACKUP = STAGE / 'ct1200-inner-health.before.conf'
LOCK = Path('/run/lock/pickle-synthetic-login-ingress.lock')
BASE_SHA = '4a457f2d58fe16f8d11af4b6be6f3a4c8e8c73ac52b8d9010eb8152964c3346c'
TRIAL_SHA = '81c3205d89587ed8538243a6996f32cbd2631bb446cbf81d15a4cec65d68b185'
MATRIX_DEADLINE_SECONDS = 10.0
MATRIX_RETRY_SECONDS = 0.25


class MatrixMismatch(RuntimeError):
    """A completed probe returned a status outside the expected route matrix."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class MatrixTimeout(RuntimeError):
    """Fresh connections never converged on the installed configuration."""


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
    allowed = '203.0.113.14'
    default_host = 'staging.example.com'
    checks = [
        ('/__ingress_probe', allowed, default_host, 'GET', '200'),
        ('/', allowed, default_host, 'GET', '404'),
        ('/api/v1/admin', allowed, default_host, 'GET', '404'),
        ('/api/v1/vms', allowed, default_host, 'GET', '404'),
        ('/api/v1/llm-keys', allowed, default_host, 'GET', '404'),
        ('/api/auth/login', allowed, default_host, 'GET', '404'),
        ('/__ingress_probe', '198.51.100.99', default_host, 'GET', '403'),
        ('/__ingress_probe', allowed, 'other.example', 'GET', '421'),
    ]
    if trial:
        checks.extend([
            ('/login', allowed, default_host, 'GET', '200'),
            ('/login', allowed, default_host, 'HEAD', '200'),
            ('/api/v1/meta/status', allowed, default_host, 'GET', '200'),
            ('/login', allowed, default_host, 'POST', '404'),
            ('/api/v1/auth/login', allowed, default_host, 'GET', '404'),
            ('/api/v1/me', allowed, default_host, 'POST', '404'),
        ])
    else:
        checks.append(('/login', allowed, default_host, 'GET', '404'))
    for path, source, host, method, expected in checks:
        actual = probe(path, source, host=host, method=method)
        if actual != expected:
            old_health_worker = (trial and actual == '404' and
                                 (path, method) in (('/login', 'GET'),
                                                    ('/login', 'HEAD'),
                                                    ('/api/v1/meta/status', 'GET')))
            old_trial_worker = (not trial and actual == '200' and
                                (path, method) == ('/login', 'GET'))
            raise MatrixMismatch(f'{method} {path} from {source} Host {host}: '
                                 f'expected {expected}, got {actual}',
                                 retryable=old_health_worker or old_trial_worker)


def wait_matrix(trial):
    deadline = time.monotonic() + MATRIX_DEADLINE_SECONDS
    attempts = 0
    while True:
        attempts += 1
        try:
            matrix(trial)
            return attempts
        except MatrixMismatch as error:
            if not error.retryable:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                current_sha = digest(TARGET.read_bytes())
                raise MatrixTimeout(
                    f'new connections did not converge within '
                    f'{MATRIX_DEADLINE_SECONDS:g}s after {attempts} attempts; '
                    f'last mismatch: {error}; installed_sha256={current_sha}') from error
            time.sleep(min(MATRIX_RETRY_SECONDS, remaining))


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
    attempts = wait_matrix(False)
    print(json.dumps({'mode': 'recover', 'target_sha256': BASE_SHA,
                      'health_only_new_connections': True,
                      'existing_sessions_not_revoked': True,
                      'matrix_attempts': attempts}, sort_keys=True))


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
        attempts = wait_matrix(True)
    except BaseException as error:
        try:
            recover()
        except BaseException as rollback_error:
            current_sha = digest(TARGET.read_bytes())
            raise RuntimeError(f'apply failed ({type(error).__name__}): {error}; '
                               f'recovery failed ({type(rollback_error).__name__}): '
                               f'{rollback_error}; installed_sha256={current_sha}') from error
        raise RuntimeError(f'apply failed ({type(error).__name__}): {error}; '
                           f'health-only restored for new connections; '
                           f'installed_sha256={digest(TARGET.read_bytes())}') from error
    print(json.dumps({'mode': 'apply', 'target_sha256': TRIAL_SHA,
                      'backup_sha256': BASE_SHA, 'probe_matrix': True,
                      'matrix_attempts': attempts}, sort_keys=True))


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
