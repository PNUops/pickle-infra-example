#!/usr/bin/python3 -I
"""Forced-command development artifact receiver; never accept uploaded tools."""

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import fcntl
import gzip
import tarfile
import shutil

CONFIG = Path('/etc/pickle/dev-deploy-receiver.json')
COMMAND = 'pickle-dev-deploy-v1'
TARGETS = ('api', 'console', 'llm-gateway')
HEX40 = re.compile(r'[0-9a-f]{40}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')


class Refusal(RuntimeError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal('duplicate protocol key')
        result[key] = value
    return result


def protected(path, directory=False):
    path = Path(path)
    for ancestor in path.parents:
        metadata = ancestor.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise Refusal('receiver input ancestor is not protected')
    metadata = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise Refusal('receiver input is not root-owned and protected')
    return path


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_manifest(config):
    manifest = json.loads(protected(config['manifest']).read_text(), object_pairs_hook=unique_object)
    if manifest.get('host') != config['host'] or manifest.get('environment') != 'dev':
        raise Refusal('receiver can select only its development host and manifest')
    if manifest.get('forbidden_ctids') != config['forbidden_ctids'] or not config['forbidden_ctids']:
        raise Refusal('preserved source exclusion differs from the frozen receiver scope')
    if manifest.get('chat_db', {}).get('ctid') != config['database_ctid']:
        raise Refusal('receiver development database differs from its frozen scope')
    for target in TARGETS:
        ctid = manifest.get('targets', {}).get(target, {}).get('ctid')
        if ctid != config['target_ctids'][target] or ctid in config['forbidden_ctids']:
            raise Refusal('receiver cannot select a source or other service CT')
    return manifest


def header(stream, config, manifest):
    line = stream.readline(4097)
    if len(line) > 4096 or not line.endswith(b'\n'):
        raise Refusal('bounded single-line deployment header is required')
    value = json.loads(line, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != {'version', 'action', 'target', 'commit', 'sha256', 'size'}:
        raise Refusal('unsupported deployment protocol fields')
    if type(value['version']) is not int or value['version'] != 1 or value['action'] not in ('check', 'apply'):
        raise Refusal('unsupported development operation')
    target = value['target']
    if target not in TARGETS or not isinstance(value['commit'], str) or not HEX40.fullmatch(value['commit']):
        raise Refusal('invalid target or source commit')
    if not isinstance(value['sha256'], str) or not HEX64.fullmatch(value['sha256']):
        raise Refusal('artifact digest is invalid')
    if value['commit'] != manifest['allowed_commits'].get(target):
        raise Refusal('source commit differs from the frozen development manifest')
    if type(value['size']) is not int or not 0 < value['size'] <= config['maximum_bytes'][target]:
        raise Refusal('artifact size exceeds the development receiver limit')
    pin = manifest.get('approved_artifacts', {}).get(target, {}).get(value['commit'])
    if (not isinstance(pin, dict) or not isinstance(pin.get('sha256'), str)
            or not HEX64.fullmatch(pin['sha256']) or value['sha256'] != pin['sha256']
            or ('size' in pin and (type(pin['size']) is not int or value['size'] != pin['size']))):
        raise Refusal('artifact differs from the protected development digest or size pin')
    return value


class BoundedArchive:
    def __init__(self, stream, maximum):
        self.stream, self.remaining = stream, maximum

    def read(self, size=-1):
        requested = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        value = self.stream.read(requested)
        self.remaining -= len(value)
        if self.remaining < 0:
            raise Refusal('expanded artifact exceeds its development receiver limit')
        return value


def bounded_archive(path, config, target):
    if target == 'api':
        return
    with gzip.open(path, 'rb') as compressed:
        reader = BoundedArchive(compressed, config['maximum_expanded_bytes'][target])
        with tarfile.open(fileobj=reader, mode='r|') as archive:
            count = 0
            total = 0
            for member in archive:
                count += 1
                total += member.size
                if count > config['maximum_archive_entries'] or total > config['maximum_expanded_bytes'][target]:
                    raise Refusal('archive inventory exceeds its development receiver limit')
                if not (member.isfile() or member.isdir()):
                    raise Refusal('development archive must contain only regular files and directories')


def receive(stream, config, request):
    root = protected(config['staging_root'], directory=True)
    folder = root / request['target'] / request['commit'] / request['sha256']
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    protected(folder, directory=True)
    total = sum(path.stat().st_size for path in root.rglob('*') if path.is_file() and not path.is_symlink())
    if (total + request['size'] > config['maximum_staging_bytes']
            or shutil.disk_usage(root).free < request['size'] + config['minimum_free_bytes']):
        raise Refusal('development receiver staging budget or disk headroom is exhausted')
    name = 'artifact.jar' if request['target'] == 'api' else 'artifact.tar.gz'
    output = folder / name
    descriptor, temporary_name = tempfile.mkstemp(prefix='.incoming-', dir=folder)
    temporary = Path(temporary_name)
    owned = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, 'wb') as destination:
            remaining = request['size']
            checksum = hashlib.sha256()
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise Refusal('artifact stream ended before its declared size')
                destination.write(chunk)
                checksum.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise Refusal('artifact stream contains trailing bytes or another request')
            if checksum.hexdigest() != request['sha256']:
                raise Refusal('artifact stream SHA-256 differs from its header')
            destination.flush()
            os.fsync(destination.fileno())
        if output.exists() or output.is_symlink():
            protected(output)
            if output.stat().st_size != request['size'] or digest(output) != request['sha256']:
                raise Refusal('existing protected artifact differs; never overwrite it')
        else:
            os.link(temporary, output, follow_symlinks=False)
        return output
    finally:
        if temporary.exists():
            current = temporary.lstat()
            if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
                raise Refusal('owned incoming temporary identity changed; cleanup refused')
            temporary.unlink()


def timeout(_signal, _frame):
    raise Refusal('development artifact receive deadline exceeded')


def execute(config, request, artifact):
    tool = protected(config['trusted_tool'])
    if digest(tool) != config['trusted_tool_sha256']:
        raise Refusal('installed deployment tool differs from its frozen SHA-256')
    # Only installed code is executed, with a fixed environment and argv.
    process = subprocess.Popen(['/usr/bin/python3', '-I', str(tool), request['action'], 'dev',
                                request['target'], request['commit'], str(artifact), request['sha256']],
                               env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'},
                               start_new_session=True)
    try:
        process.wait(timeout=config['deployment_timeout_seconds'])
    except BaseException as exc:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise Refusal('installed development deployment exceeded its execution deadline') from exc
        raise
    if process.returncode:
        raise Refusal('installed development deployment tool refused this artifact')



def main():
    if os.geteuid() != 0 or os.environ.get('SSH_ORIGINAL_COMMAND') != COMMAND or len(sys.argv) != 1:
        raise Refusal('only the fixed development SSH command is accepted')
    config = json.loads(protected(CONFIG).read_text(), object_pairs_hook=unique_object)
    if socket.gethostname().split('.')[0] != config['host']:
        raise Refusal('development receiver host differs from its frozen scope')
    os.umask(0o077)
    manifest = validate_manifest(config)
    for selected_signal in (signal.SIGALRM, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected_signal, timeout)
    signal.alarm(config['receive_timeout_seconds'])
    request = header(sys.stdin.buffer, config, manifest)
    lock_path = protected(config['lock_parent'], directory=True) / 'dev-receiver.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal('another development receiver owns the lock') from exc
        artifact = receive(sys.stdin.buffer, config, request)
        bounded_archive(artifact, config, request["target"])
        signal.alarm(0)
        validate_manifest(config)
        execute(config, request, artifact)
    print('development artifact received and selected operation completed')


if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f'dev receiver refused: {exc}', file=sys.stderr)
        sys.exit(1)
