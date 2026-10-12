#!/usr/bin/python3 -I
"""Prepare a new host-only development SSH key and GitHub sealed-box request."""

import argparse
import base64
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
from datetime import datetime, timezone
import uuid

KEY_ROOT = Path('/root/pickle-dev-deploy-key')
RECEIVER = Path('/opt/pickle/dev-deploy/receiver.py')
BACKUP_ROOT = Path('/pickle/backup/dev-deploy-key')


class Refusal(RuntimeError):
    pass


def private_file(path):
    metadata = path.lstat()
    if path.is_symlink() or not path.is_file() or metadata.st_uid != 0 or metadata.st_mode & 0o077:
        raise Refusal('private key preparation input must be root-owned and private')
    return path.read_bytes()


def sealed_box(message, public_key, library_path=None, library_sha=None):
    if library_path:
        selected = Path(library_path)
        metadata = selected.lstat()
        if (selected.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022
                or not library_sha or hashlib.sha256(selected.read_bytes()).hexdigest() != library_sha):
            raise Refusal('extracted signed runtime differs from its protected SHA-256')
        name = str(selected)
    else:
        name = ctypes.util.find_library('sodium')
    if not name:
        raise Refusal('existing libsodium runtime is required; no private key leaves the host')
    library = ctypes.CDLL(name)
    library.sodium_init.restype = ctypes.c_int
    if library.sodium_init() < 0:
        raise Refusal('libsodium initialization failed')
    library.crypto_box_publickeybytes.restype = ctypes.c_size_t
    library.crypto_box_sealbytes.restype = ctypes.c_size_t
    if len(public_key) != library.crypto_box_publickeybytes():
        raise Refusal('GitHub public key has an unexpected size')
    output = ctypes.create_string_buffer(len(message) + library.crypto_box_sealbytes())
    source = ctypes.create_string_buffer(message)
    recipient = ctypes.create_string_buffer(public_key)
    library.crypto_box_seal.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p]
    library.crypto_box_seal.restype = ctypes.c_int
    try:
        if library.crypto_box_seal(output, source, len(message), recipient) != 0:
            raise Refusal('GitHub secret sealed-box encryption failed')
        return base64.b64encode(output.raw).decode()
    finally:
        ctypes.memset(source, 0, len(source))


def create_key(root):
    if root.exists() or root.is_symlink():
        raise Refusal('new development key directory already exists; never overwrite it')
    root.mkdir(mode=0o700)
    key = root / 'id_ed25519'
    subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'pickle-dev-deploy-only',
                    '-f', str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    return key


def append_restricted_row(public_key, authorized, expected_sha, receiver_sha):
    if hashlib.sha256(RECEIVER.read_bytes()).hexdigest() != receiver_sha:
        raise Refusal('installed frozen receiver differs from the reviewed SHA-256')
    metadata = RECEIVER.lstat()
    if RECEIVER.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise Refusal('installed frozen receiver is not protected')
    path = authorized.resolve(strict=True)
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise Refusal('existing authorized rows are not root-owned and protected')
    parts = public_key.decode().strip().split()
    if len(parts) < 2 or parts[0] != 'ssh-ed25519':
        raise Refusal('new development public key has an unexpected format')
    row = ('restrict,command="/usr/bin/python3 -I /opt/pickle/dev-deploy/receiver.py" '
           + parts[0] + ' ' + parts[1] + ' pickle-dev-deploy-only\n').encode()
    with path.open('r+b') as target:
        fcntl.flock(target, fcntl.LOCK_EX)
        before = target.read()
        if hashlib.sha256(before).hexdigest() != expected_sha:
            raise Refusal('authorized rows changed since the operator readback')
        if parts[1].encode() in before:
            raise Refusal('new key is already registered; no authorized row was changed')
        directory = BACKUP_ROOT / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex)
        directory.mkdir(parents=True, mode=0o700)
        backup = directory / 'authorized_keys.before'
        with backup.open('xb') as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        target.seek(0, 2)
        target.write((b'' if not before or before.endswith(b'\n') else b'\n') + row)
        target.flush()
        os.fsync(target.fileno())
        target.seek(0)
        if not target.read().startswith(before):
            raise Refusal('original authorized rows were not preserved')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('generate', 'seal', 'install-row'))
    parser.add_argument('--github-public-key')
    parser.add_argument('--authorized-keys', default='/root/.ssh/authorized_keys')
    parser.add_argument('--expected-authorized-sha256')
    parser.add_argument('--receiver-sha256')
    parser.add_argument('--libsodium-library')
    parser.add_argument('--libsodium-sha256')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise Refusal('development key preparation runs only on the platform host as root')
    config = json.loads(Path('/etc/pickle/dev-deploy-receiver.json').read_text())
    if socket.gethostname().split('.')[0] != config['host']:
        raise Refusal('host-only key preparation cannot run on a developer machine or another host')
    os.umask(0o077)
    key = KEY_ROOT / 'id_ed25519'
    if args.action == 'generate':
        create_key(KEY_ROOT)
        print('new host-only development key prepared; existing keys and authorized rows unchanged')
    elif args.action == 'seal':
        if not args.github_public_key:
            raise Refusal('GitHub recipient public-key metadata is required')
        metadata = json.loads(private_file(Path(args.github_public_key)))
        recipient = base64.b64decode(metadata['key'], validate=True)
        encrypted = sealed_box(private_file(key), recipient, args.libsodium_library, args.libsodium_sha256)
        # Ciphertext only: the SSH private key is never written to stdout.
        print(json.dumps({'key_id': metadata['key_id'], 'encrypted_value': encrypted}))
    else:
        if not args.expected_authorized_sha256 or not args.receiver_sha256:
            raise Refusal('authorized-row before SHA and reviewed receiver SHA are required')
        append_restricted_row((KEY_ROOT / 'id_ed25519.pub').read_bytes(), Path(args.authorized_keys),
                              args.expected_authorized_sha256, args.receiver_sha256)
        print('one restricted development row appended; original key rows preserved')


if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'development key preparation refused: {exc}', file=sys.stderr)
        sys.exit(1)
