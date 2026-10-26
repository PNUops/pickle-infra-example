"""Bounded private-file and subprocess I/O for observation-only programs."""

import base64
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import signal
import ssl
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

from observation_spool import StoreError, decoded, encoded


MAX_SAMPLE_BYTES = 512 * 1024
MAX_ACK_BYTES = 16384
PROTOCOL = 'read-inference-observation-v1'


class IoError(Exception):
    """Fixed transport reasons contain no response, payload or credentials."""


def private_bytes(path, maximum=65536):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > maximum):
                raise IoError('PRIVATE_FILE_INVALID')
            result = bytearray()
            while len(result) <= maximum:
                block = os.read(descriptor, min(65536, maximum + 1 - len(result)))
                if not block:
                    break
                result.extend(block)
            after = os.fstat(descriptor)
            if (len(result) != info.st_size or len(result) > maximum
                    or (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise IoError('PRIVATE_FILE_CHANGED')
            return bytes(result)
        finally:
            os.close(descriptor)
    except OSError:
        raise IoError('PRIVATE_FILE_INVALID') from None


def config_file(path):
    try:
        return decoded(private_bytes(path))
    except StoreError:
        raise IoError('CONFIGURATION_INVALID') from None


def credential(path):
    try:
        value = private_bytes(path, 4096).decode('ascii').strip()
    except UnicodeError:
        raise IoError('CREDENTIAL_INVALID') from None
    if not re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,4096}', value):
        raise IoError('CREDENTIAL_INVALID')
    return value


def bounded_process(argv, *, timeout, maximum, input_bytes=b''):
    if (not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv)
            or type(timeout) not in (int, float) or not 0 < timeout <= 60
            or type(maximum) is not int or not 1 <= maximum <= 1024**2
            or not isinstance(input_bytes, bytes) or len(input_bytes) > 1024**2):
        raise IoError('TRANSPORT_LIMIT_INVALID')
    if not sys.platform.startswith('linux') or not hasattr(os, 'WNOWAIT') or not hasattr(os, 'waitid'):
        raise IoError('TRANSPORT_PLATFORM_UNSUPPORTED')
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    selector = selectors.DefaultSelector()
    result = bytearray()
    offset = 0
    deadline = time.monotonic() + timeout
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, 'output')
        if input_bytes:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IoError('TRANSPORT_TIMEOUT')
            events = selector.select(remaining)
            if not events:
                raise IoError('TRANSPORT_TIMEOUT')
            for key, _ in events:
                if key.data == 'input':
                    try:
                        written = os.write(process.stdin.fileno(), input_bytes[offset:offset + 8192])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        raise IoError('TRANSPORT_FAILED') from None
                    offset += written
                    if offset == len(input_bytes):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    try:
                        block = os.read(process.stdout.fileno(), min(8192, maximum + 1 - len(result)))
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(process.stdout)
                    else:
                        result.extend(block)
                        if len(result) > maximum:
                            raise IoError('TRANSPORT_TOO_LARGE')
        # Keep the leader unreaped until its whole owned process group is stopped.
        while True:
            outcome = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if outcome is not None:
                if outcome.si_code != os.CLD_EXITED or outcome.si_status != 0:
                    raise IoError('TRANSPORT_FAILED')
                return bytes(result)
            if time.monotonic() >= deadline:
                raise IoError('TRANSPORT_TIMEOUT')
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    finally:
        selector.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            # Even a denied signal must not leave an already exited leader unreaped.
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


def ssh_command(config):
    required = {'address', 'port', 'user', 'identity_file', 'known_hosts_file'}
    if not isinstance(config, dict) or set(config) != required:
        raise IoError('SSH_CONFIGURATION_INVALID')
    try:
        address = ipaddress.ip_address(config['address'])
    except (ValueError, TypeError):
        raise IoError('SSH_CONFIGURATION_INVALID') from None
    shared = address.version == 4 and address in ipaddress.ip_network('100.64.0.0/10')
    if (not (address.is_private or shared) or address.is_unspecified or address.is_multicast
            or address.is_link_local or address.is_reserved
            or type(config['port']) is not int or not 1 <= config['port'] <= 65535
            or not isinstance(config['user'], str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', config['user'])):
        raise IoError('SSH_CONFIGURATION_INVALID')
    for name in ('identity_file', 'known_hosts_file'):
        path = Path(config[name])
        if not path.is_absolute() or '..' in path.parts:
            raise IoError('SSH_CONFIGURATION_INVALID')
        private_bytes(path)
    options = ['BatchMode=yes', 'IdentitiesOnly=yes', 'StrictHostKeyChecking=yes',
               'UserKnownHostsFile=' + config['known_hosts_file'], 'GlobalKnownHostsFile=/dev/null',
               'IdentityAgent=none', 'ForwardAgent=no', 'ClearAllForwardings=yes',
               'ExitOnForwardFailure=yes', 'ControlMaster=no', 'ControlPath=none',
               'PermitLocalCommand=no', 'ProxyCommand=none', 'ProxyJump=none', 'RequestTTY=no',
               'ConnectTimeout=3', 'ConnectionAttempts=1', 'ServerAliveInterval=3', 'ServerAliveCountMax=1']
    argv = ['ssh', '-F', '/dev/null']
    for option in options:
        argv.extend(['-o', option])
    return [*argv, '-i', config['identity_file'], '-p', str(config['port']), '-l', config['user'],
            '--', str(address), PROTOCOL]


def endpoint(value, *, metrics=False):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(x) < 32 for x in value):
        raise IoError('ENDPOINT_INVALID')
    try:
        parsed = urlsplit(value)
        if (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or not parsed.hostname or not 1 <= (parsed.port or (80 if metrics else 443)) <= 65535):
            raise ValueError()
        if metrics:
            if parsed.scheme != 'http' or not ipaddress.ip_address(parsed.hostname).is_loopback or parsed.path != '/metrics':
                raise ValueError()
        elif parsed.scheme != 'https' or parsed.path != '/internal/inference-observations':
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise IoError('ENDPOINT_INVALID') from None


def http_worker(request, *, connection_factory=None):
    if not isinstance(request, dict) or set(request) != {'url', 'token', 'body'}:
        raise IoError('HTTP_WORKER_INVALID')
    url = endpoint(request['url'])
    if not isinstance(request['token'], str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,4096}', request['token']):
        raise IoError('CREDENTIAL_INVALID')
    try:
        body = base64.b64decode(request['body'], validate=True)
    except (ValueError, TypeError):
        raise IoError('HTTP_WORKER_INVALID') from None
    if not 0 < len(body) <= MAX_SAMPLE_BYTES:
        raise IoError('HTTP_BODY_TOO_LARGE')
    context = ssl.create_default_context()
    factory = connection_factory or http.client.HTTPSConnection
    connection = factory(url.hostname, url.port or 443, timeout=3, context=context)
    try:
        connection.connect()
        connection.sock.settimeout(5)
        connection.request('POST', url.path, body=body,
                           headers={'Authorization': 'Bearer ' + request['token'], 'Content-Type': 'application/json'})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise IoError('HTTP_REDIRECT_DENIED')
        if response.status != 200:
            return {'status': response.status}
        content = response.read(MAX_ACK_BYTES + 1)
        if len(content) > MAX_ACK_BYTES:
            raise IoError('HTTP_ACK_TOO_LARGE')
        try:
            return {'status': response.status, 'ack': decoded(content)}
        except StoreError:
            raise IoError('HTTP_ACK_INVALID') from None
    finally:
        connection.close()
