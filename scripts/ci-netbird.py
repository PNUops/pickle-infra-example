#!/usr/bin/env python3
"""Connect only a disposable hosted deployment runner to NetBird."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time

VERSION = '0.81.5'
PACKAGE_SHA256 = 'd4cdd97734055f810ff4b5af8eacac3b19a2a50e3ff594fb4353151725374800'
PACKAGE_URL = f'https://pkgs.netbird.io/debian/pool/netbird_{VERSION}_linux_amd64.deb'
OVERLAY = ipaddress.ip_network('100.64.0.0/10')


class Refusal(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refusal(message)


def transport(env):
    mode = env.get('DEPLOY_TRANSPORT', '').strip()
    raw_key = env.get('NETBIRD_SETUP_KEY', '')
    key = raw_key.strip()
    mode = mode or ('netbird' if raw_key else 'native')
    require(mode in ('native', 'netbird'), 'Unsupported deployment transport')
    require(mode != 'netbird' or key, 'NetBird transport requires its dedicated setup key')
    return mode


def vpn_host(host):
    try:
        value = ipaddress.ip_address(host)
    except ValueError as error:
        raise Refusal('VPN host must be an overlay IPv4 address') from error
    require(value.version == 4 and value in OVERLAY, 'VPN host is outside the overlay range')
    return str(value)


def ssh_alias(host, port):
    require(re.fullmatch(r'[A-Za-z0-9.-]+', host or ''), 'Invalid trusted SSH host alias')
    require(re.fullmatch(r'[0-9]{1,5}', port or ''), 'Invalid trusted SSH port')
    require(1 <= int(port) <= 65535, 'Invalid trusted SSH port')
    return host if int(port) == 22 else f'[{host}]:{int(port)}'


def identity(env):
    require(env.get('GITHUB_ACTIONS') == 'true'
            and env.get('RUNNER_ENVIRONMENT') == 'github-hosted'
            and env.get('RUNNER_OS') == 'Linux' and env.get('RUNNER_ARCH') == 'X64',
            'NetBird bootstrap requires a disposable GitHub-hosted Linux X64 runner')
    run, attempt, job = (env.get(k, '') for k in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB'))
    require(run.isdigit() and attempt.isdigit() and re.fullmatch(r'[A-Za-z0-9_-]+', job),
            'Invalid runner ownership identity')
    name = f'ci-{run}-{attempt}-{job}'.lower().replace('_', '-')
    require(len(name) <= 63, 'Runner peer name is too long')
    return {'run': run, 'attempt': attempt, 'job': job}, name


def state_path(env):
    parent = Path(env.get('RUNNER_TEMP', ''))
    require(parent.is_absolute() and parent.is_dir() and not parent.is_symlink(),
            'Runner temporary directory is unavailable')
    return parent / 'pickle-ci-netbird'


def command(argv, timeout=30, check=True):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    require(not check or result.returncode == 0, 'NetBird runner operation failed')
    return result


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def tcp_ready(peer, port, timeout):
    try:
        with socket.create_connection((peer, port), timeout=timeout):
            return True
    except OSError:
        return False


def ready(status, expected_peer, port, timeout):
    details = status.get('peers', {}).get('details')
    if details is None:
        return False
    require(isinstance(details, list) and all(
        isinstance(row, dict) and isinstance(row.get('netbirdIp'), str) for row in details),
        'Malformed NetBird peer details')
    actual = {row.get('netbirdIp', '').split('/')[0] for row in details}
    require(actual <= {expected_peer}, 'Runner received an unexpected peer; check its dedicated group')
    control_ready = (status.get('management', {}).get('connected') is True
            and status.get('signal', {}).get('connected') is True
            and status.get('sshServer', {}).get('enabled') is False
            and status.get('daemonVersion') == VERSION and actual == {expected_peer})
    return control_ready and tcp_ready(expected_peer, port, timeout)


def emit_mode(env, mode):
    output = Path(env.get('GITHUB_OUTPUT', ''))
    require(output.is_absolute() and output.is_file() and not output.is_symlink(),
            'GitHub step output is unavailable')
    with output.open('a') as stream:
        stream.write(f'mode={mode}\n')


def existing_state():
    return any(Path(path).exists() for path in ('/var/lib/netbird', '/etc/netbird',
                                               '/etc/systemd/system/netbird.service.d'))


def start(env):
    mode = transport(env)
    if mode == 'native':
        emit_mode(env, mode)
        return
    owner, name = identity(env)
    peer = vpn_host(env.get('EXAMPLE_DEV_VPN_HOST', ''))
    port = env.get('EXAMPLE_DEV_SSH_PORT', '')
    ssh_alias('trusted.invalid', port)
    key = env.get('NETBIRD_SETUP_KEY', '').strip()
    require(re.fullmatch(r'[A-Fa-f0-9-]{36}', key), 'Malformed dedicated setup key')
    require(shutil.which('netbird') is None, 'Refusing an existing NetBird installation')
    require(not existing_state(), 'Refusing existing NetBird identity or service state')
    state = state_path(env)
    require(not state.exists() and not state.is_symlink(), 'Runner network state already exists')
    state.mkdir(mode=0o700)
    write_private(state / 'owner.json', json.dumps(owner, sort_keys=True).encode())
    secret = state / 'setup.key'
    try:
        write_private(secret, (key + '\n').encode())
        package = state / 'netbird.deb'
        command(['curl', '--disable', '--proto', '=https', '--tlsv1.2', '--fail', '--silent', '--show-error',
                 '--max-time', '30', '--output', str(package), PACKAGE_URL], timeout=35)
        require(package.is_file() and package.stat().st_size <= 32 * 1024 * 1024
                and hashlib.sha256(package.read_bytes()).hexdigest() == PACKAGE_SHA256,
                'NetBird package digest differs')
        dropin = state / '10-ci.conf'
        write_private(dropin, b'[Service]\nEnvironment=NB_DISABLE_SSH_CONFIG=true\n')
        command(['sudo', 'install', '-d', '-m', '755', '/etc/systemd/system/netbird.service.d'])
        command(['sudo', 'install', '-m', '644', str(dropin),
                 '/etc/systemd/system/netbird.service.d/10-pickle-ci.conf'])
        command(['sudo', 'dpkg', '-i', str(package)], timeout=60)
        require(command(['/usr/bin/netbird', 'version']).stdout.decode().strip() == VERSION,
                'Installed NetBird version differs')
        command(['sudo', '/usr/bin/netbird', 'up', '--setup-key-file', str(secret),
                 '--hostname', name, '--disable-dns', '--disable-client-routes',
                 '--disable-server-routes', '--allow-server-ssh=false',
                 '--allow-remote-jobs=false'], timeout=60)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            result = command(['sudo', '/usr/bin/netbird', 'status', '--json'],
                             timeout=min(5, remaining), check=False)
            remaining = deadline - time.monotonic()
            if remaining > 0 and result.returncode == 0 and ready(
                    json.loads(result.stdout), peer, int(port), min(2, remaining)):
                emit_mode(env, mode)
                return
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise Refusal('NetBird did not become ready within the bounded wait')
    finally:
        secret.unlink(missing_ok=True)


def stop_client():
    try:
        command(['sudo', '/usr/bin/netbird', 'down'], timeout=20, check=False)
    finally:
        command(['sudo', '/usr/bin/netbird', 'service', 'stop'], timeout=20)


def stop(env):
    owner, _ = identity(env)
    state = state_path(env)
    if not state.exists():
        return
    require(state.is_dir() and not state.is_symlink(), 'Runner network state is not owned')
    marker = state / 'owner.json'
    require(marker.is_file() and not marker.is_symlink()
            and json.loads(marker.read_text()) == owner, 'Runner network owner differs')
    try:
        if Path('/usr/bin/netbird').exists():
            stop_client()
    finally:
        (state / 'setup.key').unlink(missing_ok=True)
    names = {'owner.json', 'netbird.deb', '10-ci.conf'}
    for path in state.iterdir():
        require(path.name in names and path.is_file() and not path.is_symlink(),
                'Unexpected runner state file; preserve it')
    for path in state.iterdir():
        path.unlink()
    state.rmdir()


def main(argv, env):
    require(argv, 'Expected start, stop, ssh-alias or vpn-host')
    if argv == ['start']:
        start(env)
    elif argv == ['stop']:
        stop(env)
    elif len(argv) == 3 and argv[0] == 'ssh-alias':
        print(ssh_alias(argv[1], argv[2]))
    elif len(argv) == 2 and argv[0] == 'vpn-host':
        print(vpn_host(argv[1]))
    else:
        raise Refusal('Unsupported runner operation')


if __name__ == '__main__':
    try:
        main(sys.argv[1:], os.environ)
    except (Refusal, OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('Deployment network preparation failed; native fallback is disabled.', file=sys.stderr)
        sys.exit(1)
