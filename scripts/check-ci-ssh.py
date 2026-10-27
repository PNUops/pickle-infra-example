#!/usr/bin/env python3
"""Authenticate to pinned CI SSH endpoints without requesting remote sessions."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SPEC = importlib.util.spec_from_file_location('ci_netbird', Path(__file__).with_name('ci-netbird.py'))
network = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(network)


def authenticate(config, alias, control):
    started = False
    try:
        result = subprocess.run(['ssh', '-F', str(config), '-M', '-S', str(control), '-fNT', alias],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
        network.require(result.returncode == 0, 'Pinned SSH authentication failed')
        started = True
        result = subprocess.run(['ssh', '-F', str(config), '-S', str(control), '-O', 'check', alias],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        network.require(result.returncode == 0, 'Authenticated SSH control master is unavailable')
    finally:
        result = subprocess.run(['ssh', '-F', str(config), '-S', str(control), '-O', 'exit', alias],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        network.require(not started or result.returncode == 0, 'Owned SSH control master did not stop')


def check(env):
    network.identity(env)
    fields = ('EXAMPLE_DEV_HOST', 'EXAMPLE_DEV_PORT', 'EXAMPLE_HOP_SSH_KEY', 'EXAMPLE_DEV_KNOWN_HOSTS',
              'EXAMPLE_PROD_HOST', 'EXAMPLE_PROD_PORT', 'EXAMPLE_PROD_SSH_KEY', 'EXAMPLE_PROD_KNOWN_HOSTS')
    network.require(all(env.get(name, '').strip() for name in fields),
                    'Trusted SSH settings are incomplete')
    hop = network.vpn_host(env.get('EXAMPLE_DEV_VPN_HOST', ''))
    target = network.vpn_host(env.get('EXAMPLE_PROD_VPN_HOST', ''))
    hop_alias = network.ssh_alias(env['EXAMPLE_DEV_HOST'], env['EXAMPLE_DEV_PORT'])
    target_alias = network.ssh_alias(env['EXAMPLE_PROD_HOST'], env['EXAMPLE_PROD_PORT'])
    parent = network.state_path(env).parent
    with tempfile.TemporaryDirectory(prefix='pickle-ci-ssh-', dir=parent) as folder:
        root = Path(folder)
        network.write_private(root / 'hop.key', (env['EXAMPLE_HOP_SSH_KEY'].strip() + '\n').encode())
        network.write_private(root / 'production.key', (env['EXAMPLE_PROD_SSH_KEY'].strip() + '\n').encode())
        pins = env['EXAMPLE_DEV_KNOWN_HOSTS'].rstrip() + '\n' + env['EXAMPLE_PROD_KNOWN_HOSTS'].rstrip() + '\n'
        network.write_private(root / 'known_hosts', pins.encode())
        config = f'''Host *
  User root
  BatchMode yes
  IdentitiesOnly yes
  IdentityAgent none
  StrictHostKeyChecking yes
  UpdateHostKeys no
  PasswordAuthentication no
  KbdInteractiveAuthentication no
  ForwardAgent no
  RequestTTY no
  ConnectTimeout 10
  ConnectionAttempts 1
  ControlMaster no
  ControlPersist no
  ControlPath none
  UserKnownHostsFile "{root / 'known_hosts'}"
Host ci-hop
  HostName {hop}
  HostKeyAlias {hop_alias}
  Port {int(env['EXAMPLE_DEV_PORT'])}
  IdentityFile "{root / 'hop.key'}"
Host ci-production
  HostName {target}
  HostKeyAlias {target_alias}
  Port {int(env['EXAMPLE_PROD_PORT'])}
  IdentityFile "{root / 'production.key'}"
  ProxyJump ci-hop
'''
        network.write_private(root / 'config', config.encode())
        for alias in ('ci-hop', 'ci-production'):
            authenticate(root / 'config', alias, root / f'{alias}.sock')
    print('Pinned CI SSH authentication checks passed without remote sessions.')


if __name__ == '__main__':
    try:
        check(os.environ)
    except (network.Refusal, OSError, ValueError, subprocess.SubprocessError):
        print('Read-only CI SSH check failed; raw SSH output is not printed.', file=sys.stderr)
        sys.exit(1)
