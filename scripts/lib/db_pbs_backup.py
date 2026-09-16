#!/usr/bin/env python3
"""Encrypted PostgreSQL dump backup with PBS readback and current remote freshness checks."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid

from isolated_core import BootstrapError, protected_file

ARCHIVE = 'platform-db.pxar'
ARCHIVE_INDEX = ARCHIVE + '.didx'
PROOF_ARCHIVE = 'verification.pxar'
PG_BIN = '/usr/lib/postgresql/18/bin/'
PG_SOCKET = '/var/run/postgresql'
WARNING_AGE = 600
FAILED_AGE = 900


class BackupError(RuntimeError):
    pass


class MailNotSent(BackupError):
    """SMTP has not accepted any message data."""


class MailUncertain(BackupError):
    """SMTP acceptance cannot be established safely."""


@dataclass(frozen=True)
class Config:
    expected_hostname: str
    monitor_hostname: str
    instance_id: str
    database: str
    expected_system_identifier: str
    minimum_schema_version: int
    source_address: str
    repository: str
    namespace: str
    backup_id: str
    server_fingerprint: str
    password_file: str
    encryption_key_file: str
    encryption_password_file: str | None
    escrow_receipt_file: str
    state_dir: str
    monitor_state_dir: str
    mail_config_file: str | None

    @classmethod
    def load(cls, path: Path) -> Config:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise BackupError('Configuration must contain exactly the documented fields')
        result = cls(**value)
        result.validate()
        return result

    def validate(self) -> None:
        uuid.UUID(self.instance_id)
        for name in ('expected_hostname', 'monitor_hostname', 'backup_id'):
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', getattr(self, name)):
                raise BackupError(f'Invalid {name}')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', self.database):
            raise BackupError('Invalid database name')
        if not re.fullmatch(r'[0-9]{1,24}', self.expected_system_identifier):
            raise BackupError('An observed PostgreSQL system identifier is required')
        if type(self.minimum_schema_version) is not int or self.minimum_schema_version < 1:
            raise BackupError('An application schema baseline is required')
        import ipaddress
        ipaddress.IPv4Address(self.source_address)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+@pbs![A-Za-z0-9_-]+@[A-Za-z0-9.-]+(?::[0-9]+)?:[A-Za-z0-9_-]+', self.repository):
            raise BackupError('Use an explicit remote PBS API-token repository without an embedded password')
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,6}', self.namespace):
            raise BackupError('Use an explicit non-root namespace')
        if not re.fullmatch(r'[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){31}', self.server_fingerprint):
            raise BackupError('A verified PBS TLS fingerprint is required')
        for name in ('password_file', 'encryption_key_file', 'encryption_password_file',
                     'escrow_receipt_file', 'state_dir', 'monitor_state_dir', 'mail_config_file'):
            value = getattr(self, name)
            if value is not None and (not Path(value).is_absolute() or '..' in Path(value).parts):
                raise BackupError(f'{name} must be an explicit absolute path')
        if self.monitor_hostname == self.expected_hostname or self.monitor_state_dir == self.state_dir:
            raise BackupError('Monitoring must have a separate host identity and state directory')
        for name in ('password_file', 'encryption_key_file', 'encryption_password_file', 'escrow_receipt_file'):
            value = getattr(self, name)
            if value is not None and any(Path(value).is_relative_to(Path(root)) for root in (self.state_dir, self.monitor_state_dir)):
                raise BackupError('Key, password and escrow files must be outside the backup state and payload tree')


def plan(c: Config) -> dict:
    return {'mode': 'dry-run', 'expected_hostname': c.expected_hostname, 'database': c.database,
            'namespace': c.namespace, 'group': 'host/' + c.backup_id,
            'verification_group': 'host/' + c.backup_id + '-verified', 'monitor_host': c.monitor_hostname,
            'interval_seconds': 300, 'warning_seconds': WARNING_AGE, 'failure_seconds': FAILED_AGE,
            'retention': {'keep-last': 288, 'keep-daily': 7, 'keep-weekly': 4},
            'success_requires': ['pg_dump completed', 'encrypted PBS backup completed',
                                 'committed snapshot manifest matches', 'restored payload hashes match',
                                 'restored custom dump is readable'],
            'freshness_probe': 'current remote manifest plus archive identity, never local checkpoint alone',
            'routine_guest_vm_backups': False, 'mail_sent': False,
            'boundary': 'File-level verified recovery point; full database and service recovery remains a separate drill'}


def unit_files() -> dict[str, str]:
    executable = '/bin/bash /opt/pickle/db-backup/bin/db-pbs-backup.sh --config /etc/pickle-db-backup/config.json'
    service = ('[Service]\nType=oneshot\nUser=root\nUMask=0077\nNoNewPrivileges=true\n'
               'PrivateTmp=true\nNice=10\nIOSchedulingClass=best-effort\nIOSchedulingPriority=7\n')
    return {
        'pickle-db-pbs-backup.service':
            '[Unit]\nDescription=Encrypted PostgreSQL backup and PBS readback\n'
            'Requires=postgresql@18-main.service\nAfter=network-online.target postgresql@18-main.service\n'
            'Wants=network-online.target\nConditionPathExists=/etc/pickle-db-backup/enable-backup\n'
            + service + 'TimeoutStartSec=1200\nExecStart=' + executable + ' --run\n',
        'pickle-db-pbs-backup.timer':
            '[Unit]\nDescription=Back up the application database every five minutes\n'
            '[Timer]\nOnCalendar=*-*-* *:0/5:00 UTC\nAccuracySec=1s\nPersistent=true\n'
            '[Install]\nWantedBy=timers.target\n',
        'pickle-db-pbs-monitor.service':
            '[Unit]\nDescription=Independent PBS recovery-point freshness monitor\n'
            'After=network-online.target\nWants=network-online.target\n'
            'ConditionPathExists=/etc/pickle-db-backup/config.json\n'
            + service + 'TimeoutStartSec=120\nSuccessExitStatus=1\nExecStart=' + executable + ' --notify-status\n',
        'pickle-db-pbs-monitor.timer':
            '[Unit]\nDescription=Check the current remote database recovery point every minute\n'
            '[Timer]\nOnCalendar=*-*-* *:*:00 UTC\nAccuracySec=1s\nPersistent=true\n'
            '[Install]\nWantedBy=timers.target\n',
    }


class Runner:
    def run(self, args, *, data=None, env=None, output=None, label='command', timeout=240) -> bytes:
        with open(output, 'xb') if output is not None else _no_output_file() as target:
            result = subprocess.run(args, input=data, stdin=subprocess.DEVNULL if data is None else None,
                                    stdout=target if target else subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, timeout=timeout)
        if result.returncode:
            raise BackupError(label + f' failed (exit {result.returncode})')
        return result.stdout if output is None else b''


@contextmanager
def _no_output_file():
    yield None


def digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name('.' + path.name + '.' + str(uuid.uuid4()))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as target:
            json.dump(value, target, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def lock(path: Path, *, blocking: bool = True):
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise BackupError('Another backup still owns the job lock') from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def require_local_host(c: Config, *, monitor=False) -> None:
    expected = c.monitor_hostname if monitor else c.expected_hostname
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != expected:
        raise BackupError('This operation requires root on the expected database container')


def require_state(c: Config, *, monitor=False) -> Path:
    state = Path(c.monitor_state_dir if monitor else c.state_dir)
    info = state.lstat()
    if not state.is_dir() or state.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise BackupError('Backup state must be a protected directory owned by the invoking account')
    owner = json.loads(protected_file(str(state / 'owner.json'), private=True))
    if owner != {'instance_id': c.instance_id, 'database': c.database, 'role': 'monitor' if monitor else 'backup'}:
        raise BackupError('Backup state belongs to another database instance')
    return state


def initialize(c: Config, *, monitor=False) -> None:
    require_local_host(c, monitor=monitor)
    state = Path(c.monitor_state_dir if monitor else c.state_dir)
    state.mkdir(mode=0o700)
    atomic_json(state / 'owner.json', {'instance_id': c.instance_id, 'database': c.database,
                                     'role': 'monitor' if monitor else 'backup'})
    (state / 'spool').mkdir(mode=0o700)
    (state / 'records').mkdir(mode=0o700)


def client_environment(c: Config) -> dict[str, str]:
    key = protected_file(c.encryption_key_file, private=True)
    receipt = json.loads(protected_file(c.escrow_receipt_file, private=True))
    if (receipt.get('key_file_sha256') != hashlib.sha256(key).hexdigest()
            or receipt.get('outside_datastore') is not True
            or not receipt.get('recovery_copy_verified_at') or not receipt.get('custody_reference')):
        raise BackupError('An external key-recovery custody receipt matching this key is required')
    verified_time = datetime.fromisoformat(receipt['recovery_copy_verified_at'])
    if verified_time.tzinfo is None:
        raise BackupError('Key custody receipt must use an explicit timezone')
    verified = verified_time.timestamp()
    if verified > time.time():
        raise BackupError('Key custody receipt is dated in the future')
    protected_file(c.password_file, private=True)
    # Do not inherit a higher-precedence PBS_PASSWORD_FD/CMD or a different repository.
    env = {name: os.environ[name] for name in ('PATH', 'LANG', 'HOME', 'TZ') if name in os.environ}
    env.update({'PBS_PASSWORD_FILE': c.password_file, 'PBS_FINGERPRINT': c.server_fingerprint})
    if c.encryption_password_file is not None:
        protected_file(c.encryption_password_file, private=True)
        env['PBS_ENCRYPTION_PASSWORD_FILE'] = c.encryption_password_file
    else:
        env['PBS_ENCRYPTION_PASSWORD'] = ''
    return env


class Client:
    def __init__(self, c: Config, runner: Runner):
        self.c, self.runner = c, runner
        self.env = client_environment(c)
        self.deadline = None

    def call(self, words, *, label, timeout=240):
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise BackupError('PBS monitoring time budget exhausted')
        return self.runner.run(['proxmox-backup-client', *words, '--repository', self.c.repository,
                                '--ns', self.c.namespace], env=self.env, label=label, timeout=timeout)

    def backup(self, payload: Path, epoch: int) -> None:
        self.require_no_implicit_key_export()
        self.call(['backup', ARCHIVE + ':' + str(payload), '--backup-type', 'host',
                   '--backup-id', self.c.backup_id, '--backup-time', str(epoch),
                   '--crypt-mode', 'encrypt', '--keyfile', self.c.encryption_key_file,
                   '--change-detection-mode', 'legacy'], label='encrypted PBS backup')

    def index(self, snapshot: str) -> dict:
        raw = self.call(['restore', snapshot, 'index.json', '-', '--keyfile', self.c.encryption_key_file],
                        label='current PBS snapshot manifest', timeout=30)
        return json.loads(raw)

    def restore(self, snapshot: str, target: Path) -> None:
        self.call(['restore', snapshot, ARCHIVE, str(target), '--keyfile', self.c.encryption_key_file],
                  label='PBS archive readback')

    def backup_receipt(self, payload: Path, epoch: int) -> None:
        self.require_no_implicit_key_export()
        self.call(['backup', PROOF_ARCHIVE + ':' + str(payload), '--backup-type', 'host',
                   '--backup-id', self.c.backup_id + '-verified', '--backup-time', str(epoch),
                   '--crypt-mode', 'encrypt', '--keyfile', self.c.encryption_key_file,
                   '--change-detection-mode', 'legacy'], label='encrypted verification receipt', timeout=60)

    @staticmethod
    def require_no_implicit_key_export() -> None:
        if (Path.home() / '.config/proxmox-backup/master-public.pem').exists():
            raise BackupError('An undeclared default master key would export key material; use the dedicated client context')

    def receipt(self, snapshot: str) -> dict:
        with tempfile.TemporaryDirectory(prefix='pbs-receipt-') as temporary:
            target = Path(temporary) / 'restored'
            self.call(['restore', snapshot, PROOF_ARCHIVE, str(target), '--keyfile', self.c.encryption_key_file],
                      label='current encrypted verification receipt', timeout=30)
            if {p.name for p in target.iterdir()} != {'receipt.json'} or (target / 'receipt.json').is_symlink():
                raise BackupError('Unexpected verification receipt archive content')
            return json.loads((target / 'receipt.json').read_text())

    def snapshots(self, *, proofs=False) -> list[int]:
        group_id = self.c.backup_id + ('-verified' if proofs else '')
        rows = json.loads(self.call(['snapshot', 'list', 'host/' + group_id, '--output-format', 'json'],
                                   label='current PBS group inventory', timeout=30))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise BackupError('Invalid PBS snapshot inventory')
        epochs = []
        archive = PROOF_ARCHIVE if proofs else ARCHIVE
        for row in rows:
            if row.get('backup-type') != 'host' or row.get('backup-id') != group_id or type(row.get('backup-time')) is not int:
                raise BackupError('PBS snapshot inventory crosses the configured group')
            contents = row.get('files')
            if not isinstance(contents, list):
                raise BackupError('PBS snapshot file list is invalid')
            names = set()
            for entry in contents:
                filename = entry.get('filename') if isinstance(entry, dict) else entry
                if not isinstance(filename, str):
                    raise BackupError('PBS snapshot filename is invalid')
                names.add(filename)
            if archive in names or archive + '.didx' in names:
                epochs.append(row['backup-time'])
        return sorted(set(epochs), reverse=True)

    def forget_confirmed(self, epoch: int, *, proofs=False) -> None:
        snapshot = snapshot_name(self.c, epoch, proofs=proofs)
        try:
            self.call(['snapshot', 'forget', snapshot], label='paired retention removal', timeout=60)
        except BackupError:
            if epoch in self.snapshots(proofs=proofs):
                raise
        if epoch in self.snapshots(proofs=proofs):
            raise BackupError('PBS still lists the retention removal target')

    def prune(self, verified_epoch: int) -> None:
        group_id = self.c.backup_id + '-verified'
        options = ['prune', 'host/' + group_id, '--keep-last', '288', '--keep-daily', '7',
                   '--keep-weekly', '4', '--output-format', 'json']
        preview = json.loads(self.call([*options, '--dry-run'], label='scoped PBS retention preview', timeout=60))
        if not isinstance(preview, list) or not all(isinstance(row, dict) for row in preview):
            raise BackupError('PBS retention preview is not a snapshot list')
        if any(row.get('backup-type') != 'host' or row.get('backup-id') != group_id for row in preview):
            raise BackupError('PBS retention preview crosses the configured backup group')
        current = [row for row in preview if row.get('backup-time') == verified_epoch]
        if len(current) != 1 or current[0].get('keep') is not True:
            raise BackupError('PBS retention would not preserve the newly verified recovery point')
        # Apply the official prune selection as pairs, so data and proof cannot age out differently.
        data_epochs = set(self.snapshots())
        for row in preview:
            if row.get('keep') is True:
                continue
            if row.get('keep') is not False or type(row.get('backup-time')) is not int:
                raise BackupError('Invalid PBS retention decision')
            epoch = row['backup-time']
            proof_snapshot = snapshot_name(self.c, epoch, proofs=True)
            record = self.receipt(proof_snapshot)
            validate_receipt(self.c, record, epoch)
            if epoch in data_epochs:
                actual = manifest_fingerprint(self.c, self.index(record['snapshot']), epoch)
                if actual != record['manifest_fingerprint']:
                    raise BackupError('Retention target differs from its verified data manifest')
                self.forget_confirmed(epoch)
            self.forget_confirmed(epoch, proofs=True)


def snapshot_name(c: Config, epoch: int, *, proofs=False) -> str:
    stamp = datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return f'host/{c.backup_id}{"-verified" if proofs else ""}/{stamp}'


def manifest_fingerprint(c: Config, index: dict, epoch: int, *, proofs=False) -> str:
    group_id = c.backup_id + ('-verified' if proofs else '')
    archive = PROOF_ARCHIVE + '.didx' if proofs else ARCHIVE_INDEX
    if (not isinstance(index, dict) or index.get('backup-type') != 'host' or index.get('backup-id') != group_id
            or index.get('backup-time') != epoch or not is_sha256(index.get('signature'))):
        raise BackupError('PBS manifest identity or signature is missing or inconsistent')
    files = index.get('files')
    if not isinstance(files, list) or not all(isinstance(entry, dict) for entry in files):
        raise BackupError('PBS manifest file inventory is invalid')
    entries = [entry for entry in files if entry.get('filename') == archive]
    if (len(entries) != 1 or entries[0].get('crypt-mode') != 'encrypt'
            or not is_sha256(entries[0].get('csum'))):
        raise BackupError('PBS manifest does not identify one encrypted database archive')
    unprotected = index.get('unprotected', {})
    if not isinstance(unprotected, dict):
        raise BackupError('PBS manifest metadata is invalid')
    verification = unprotected.get('verify_state', {})
    if isinstance(verification, dict) and str(verification.get('state', '')).lower() == 'failed':
        raise BackupError('PBS currently reports failed verification')
    # Verification notes may change, but the signature and decrypting-key identity must not.
    protected = {key: value for key, value in index.items() if key != 'unprotected'}
    protected['key-fingerprint'] = unprotected.get('key-fingerprint')
    return hashlib.sha256(json.dumps(protected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def is_sha256(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def postgres_environment() -> dict[str, str]:
    # Pin the local peer-authenticated cluster; inherited libpq settings are not inputs.
    return {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/var/lib/postgresql'}


def check_source(c: Config, runner: Runner) -> dict:
    addresses = json.loads(runner.run(['ip', '-j', 'address', 'show'], label='database source address'))
    if not any(a.get('local') == c.source_address for link in addresses for a in link.get('addr_info', [])):
        raise BackupError('The expected primary database address is not assigned here')
    sql = ("SELECT json_build_object('database',current_database(),'system_identifier',"
           "(SELECT system_identifier::text FROM pg_control_system()),'recovery',pg_is_in_recovery(),"
           "'major',current_setting('server_version_num')::int/10000,'schema',"
           "(SELECT max(version::int) FROM flyway_schema_history WHERE success),"
           "'failed_migrations',(SELECT count(*) FROM flyway_schema_history WHERE NOT success),"
           "'database_bytes',pg_database_size(current_database()));")
    raw = runner.run(['runuser', '-u', 'postgres', '--', PG_BIN + 'psql', '-X', '-qAt',
                      '-h', PG_SOCKET, '-p', '5432', '-U', 'postgres', '-w',
                      '-v', 'ON_ERROR_STOP=1', '-d', c.database, '-c', sql],
                     env=postgres_environment(), label='database identity and schema')
    observed = json.loads(raw)
    if (observed.get('database') != c.database or observed.get('system_identifier') != c.expected_system_identifier
            or observed.get('recovery') is not False or observed.get('major') != 18
            or observed.get('failed_migrations') != 0
            or not isinstance(observed.get('schema'), int) or observed['schema'] < c.minimum_schema_version):
        raise BackupError('The source is not the configured primary application database')
    return observed


def clean_owned_failed_spool(c: Config, state: Path) -> None:
    candidates = []
    # Validate the whole candidate set before deleting anything. Unknown content is never swept up.
    for directory in (state / 'spool').iterdir():
        try:
            uuid.UUID(directory.name)
        except ValueError as error:
            raise BackupError('Unrecognized spool entry; inspect it without automatic cleanup') from error
        if directory.is_symlink() or not directory.is_dir():
            raise BackupError('Unsafe spool entry; automatic cleanup refused')
        record = json.loads(protected_file(str(directory / 'attempt.json'), private=True))
        if record.get('instance_id') != c.instance_id or record.get('run_id') != directory.name:
            raise BackupError('Spool ownership mismatch; automatic cleanup refused')
        if record.get('status') not in ('STARTED', 'FAILED_OR_UNCONFIRMED', 'VERIFIED') or type(record.get('recovery_point_epoch')) is not int:
            raise BackupError('Spool attempt state is unrecognized; automatic cleanup refused')
        candidates.append((record['recovery_point_epoch'], directory, record))
    # Keep the most recent unsuccessful local dump; PBS retention is independent of these temporary copies.
    for _, directory, record in sorted(candidates, key=lambda item: item[0], reverse=True)[1:]:
        atomic_json(state / 'records' / ('local-spool-' + directory.name + '.json'),
                    dict(record, local_copy_removed=True))
        shutil.rmtree(directory)


def verify_readback(payload: Path, restored: Path, runner: Runner) -> dict[str, str]:
    expected = {'database.dump', 'roles.sql', 'manifest.json'}
    if {p.name for p in restored.iterdir()} != expected:
        raise BackupError('Restored payload has missing or unexpected files')
    hashes = {}
    for name in sorted(expected):
        original, returned = payload / name, restored / name
        if not returned.is_file() or returned.is_symlink() or digest(original) != digest(returned):
            raise BackupError('PBS readback checksum mismatch')
        hashes[name] = digest(returned)
    runner.run([PG_BIN + 'pg_restore', '--list', str(restored / 'database.dump')], label='restored PostgreSQL archive readability')
    return hashes


def run_backup(c: Config, runner: Runner, client_factory=Client) -> dict:
    require_local_host(c)
    state = require_state(c)
    with lock(state / 'backup.lock', blocking=False):
        source = check_source(c, runner)
        client = client_factory(c, runner)
        clean_owned_failed_spool(c, state)
        required_free = int(source['database_bytes']) * 3 + 2 * 1024**3
        if shutil.disk_usage(state / 'spool').free < required_free:
            raise BackupError('Insufficient temporary headroom; protecting the source database filesystem')
        run_id = str(uuid.uuid4())
        run = state / 'spool' / run_id
        run.mkdir(mode=0o700)
        payload = run / 'payload'
        payload.mkdir(mode=0o700)
        # Capture before pg_dump obtains its snapshot: a conservative lower bound, not upload completion.
        epoch = int(time.time())
        snapshot = snapshot_name(c, epoch)
        record = {'instance_id': c.instance_id, 'run_id': run_id, 'snapshot': snapshot,
                  'recovery_point_epoch': epoch, 'status': 'STARTED'}
        atomic_json(run / 'attempt.json', record)
        try:
            runner.run(['runuser', '-u', 'postgres', '--', PG_BIN + 'pg_dump', '--format=custom',
                        '-h', PG_SOCKET, '-p', '5432', '-U', 'postgres', '-w', '--dbname', c.database],
                       env=postgres_environment(), output=payload / 'database.dump', label='PostgreSQL consistent dump')
            runner.run(['runuser', '-u', 'postgres', '--', PG_BIN + 'pg_dumpall', '--globals-only', '--no-role-passwords',
                        '-h', PG_SOCKET, '-p', '5432', '-U', 'postgres', '-w'],
                       env=postgres_environment(), output=payload / 'roles.sql', label='database roles without passwords')
            atomic_json(payload / 'manifest.json', {'instance_id': c.instance_id, 'source': source,
                        'recovery_point_epoch': epoch, 'dump_sha256': digest(payload / 'database.dump'),
                        'roles_sha256': digest(payload / 'roles.sql'), 'scope': 'application database and roles only'})
            client.backup(payload, epoch)
            fingerprint = manifest_fingerprint(c, client.index(snapshot), epoch)
            restored = run / 'readback'
            client.restore(snapshot, restored)
            hashes = verify_readback(payload, restored, runner)
            record.update(status='VERIFIED', verified_epoch=int(time.time()),
                          manifest_fingerprint=fingerprint, payload_sha256=hashes,
                          database=c.database, system_identifier=c.expected_system_identifier,
                          receipt_snapshot=snapshot_name(c, epoch, proofs=True))
            proof = run / 'proof'
            proof.mkdir(mode=0o700)
            atomic_json(proof / 'receipt.json', record)
            client.backup_receipt(proof, epoch)
            manifest_fingerprint(c, client.index(record['receipt_snapshot']), epoch, proofs=True)
            if client.receipt(record['receipt_snapshot']) != record:
                raise BackupError('Published encrypted verification receipt did not round-trip')
            with lock(state / 'state.lock'):
                previous = state / 'checkpoint.json'
                if previous.exists() and json.loads(previous.read_text()).get('recovery_point_epoch', 0) >= epoch:
                    raise BackupError('Recovery-point clock did not advance; retaining the previous checkpoint')
                atomic_json(state / 'checkpoint.json', record)
            atomic_json(state / 'records' / (run_id + '.json'), record)
            # Retention follows verification and is limited to this exact host group and namespace.
            try:
                client.prune(epoch)
                maintenance = None
            except BackupError:
                maintenance = 'RETENTION_FAILED'
            with lock(state / 'state.lock'):
                atomic_json(state / 'maintenance.json', {'error': maintenance})
            shutil.rmtree(run)
            return dict(record, retention='OK' if maintenance is None else 'FAILED')
        except BaseException:
            record['status'] = 'FAILED_OR_UNCONFIRMED'
            atomic_json(run / 'attempt.json', record)
            # Keep this owned local attempt for diagnosis; it never advances the recovery checkpoint.
            raise


def validate_receipt(c: Config, record: dict, epoch: int) -> None:
    if (not isinstance(record, dict) or record.get('instance_id') != c.instance_id
            or record.get('database') != c.database
            or not isinstance(record.get('system_identifier'), str)
            or not re.fullmatch(r'[0-9]{1,24}', record['system_identifier'])
            or record.get('status') != 'VERIFIED' or record.get('recovery_point_epoch') != epoch
            or record.get('snapshot') != snapshot_name(c, epoch)
            or record.get('receipt_snapshot') != snapshot_name(c, epoch, proofs=True)
            or type(record.get('verified_epoch')) is not int or record['verified_epoch'] < epoch
            or not is_sha256(record.get('manifest_fingerprint'))
            or not isinstance(record.get('payload_sha256'), dict)
            or set(record['payload_sha256']) != {'database.dump', 'roles.sql', 'manifest.json'}
            or not all(is_sha256(value) for value in record['payload_sha256'].values())):
        raise BackupError('Verification receipt does not identify this database recovery point')


def freshness(c: Config, runner: Runner, client_factory=Client, clock=None) -> dict:
    require_local_host(c, monitor=True)
    state = require_state(c, monitor=True)
    clock = time.time if clock is None else clock
    try:
        client = client_factory(c, runner)
        client.deadline = time.monotonic() + 45
        proof_epochs = client.snapshots(proofs=True)
        data_epochs = client.snapshots()
        if not proof_epochs:
            return {'status': 'FAILED', 'reason': 'NO_VERIFIED_CHECKPOINT', 'recovery_point_epoch': None}
        epoch = proof_epochs[0]
        if epoch not in data_epochs:
            return {'status': 'FAILED', 'reason': 'PBS_DATA_SNAPSHOT_MISSING', 'recovery_point_epoch': epoch}
        proof_snapshot = snapshot_name(c, epoch, proofs=True)
        manifest_fingerprint(c, client.index(proof_snapshot), epoch, proofs=True)
        checkpoint = client.receipt(proof_snapshot)
        validate_receipt(c, checkpoint, epoch)
        fingerprint = manifest_fingerprint(c, client.index(checkpoint['snapshot']), epoch)
        # Network latency counts toward the age; sample only after the complete remote probe.
        now = int(clock())
        result = {'recovery_point_epoch': epoch, 'last_readback_epoch': checkpoint['verified_epoch'],
                  'snapshot': checkpoint['snapshot'], 'age_seconds': now - epoch,
                  'probe': 'current PBS groups, encrypted verification receipt and data manifest'}
        if epoch > now or checkpoint['verified_epoch'] > now:
            return dict(result, status='FAILED', reason='CLOCK_MOVED_BACKWARDS')
        if fingerprint != checkpoint['manifest_fingerprint']:
            return dict(result, status='FAILED', reason='REMOTE_MANIFEST_CHANGED')
    except (BackupError, BootstrapError, ValueError, OSError, TypeError, KeyError, OverflowError, subprocess.TimeoutExpired):
        return {'status': 'FAILED', 'reason': 'PBS_SNAPSHOT_UNAVAILABLE', 'recovery_point_epoch': None}
    if now - epoch >= FAILED_AGE:
        return dict(result, status='FAILED', reason='RECOVERY_POINT_TOO_OLD')
    if now - epoch >= WARNING_AGE:
        return dict(result, status='WARNING', reason='RECOVERY_POINT_AGING')
    # This local cache is diagnostic only; every positive report above was reconstructed remotely.
    atomic_json(state / 'last-remote-check.json', result)
    return dict(result, status='HEALTHY', reason='VERIFIED_AND_REMOTE_AVAILABLE')


def send_mail(c: Config, report: dict, *, test=False) -> bool:
    if c.mail_config_file is None:
        return False
    settings = json.loads(protected_file(c.mail_config_file, private=True))
    if settings.get('enabled') is not True:
        return False
    for field in ('host', 'username', 'sender', 'recipient', 'password_file'):
        if not isinstance(settings.get(field), str) or not settings[field] or '\n' in settings[field] or '\r' in settings[field]:
            raise BackupError('Invalid mail settings')
    for field in ('sender', 'recipient'):
        if not re.fullmatch(r'[^\s<>,;@]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', settings[field]):
            raise BackupError('Mail sender and recipient must each be one explicit address')
    if settings.get('tls_mode') not in ('starttls', 'tls'):
        raise BackupError('SMTP requires verified TLS')
    password = protected_file(settings['password_file'], private=True).decode().rstrip('\n')
    message = EmailMessage()
    message['From'], message['To'] = settings['sender'], settings['recipient']
    labels = {'HEALTHY': '정상', 'WARNING': '주의', 'FAILED': '실패'}
    message['Subject'] = '[Pickle 백업] ' + ('알림 테스트' if test else labels.get(report['status'], report['status']))
    def stamp(value):
        return (datetime.fromtimestamp(value, timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S KST')
                if isinstance(value, int) else '확인되지 않음')
    message.set_content(('운영 백업 알림 테스트입니다.\n' if test else 'DB 백업의 복구 가능 시점 상태가 변경됐습니다.\n')
                        + '\n상태: ' + labels.get(report['status'], report['status'])
                        + '\n판정: ' + report['reason']
                        + '\n검증된 데이터 시점: ' + stamp(report.get('recovery_point_epoch'))
                        + '\n파일 복원 대조 시점: ' + stamp(report.get('last_readback_epoch'))
                        + '\nPBS 스냅샷: ' + report.get('snapshot', '확인되지 않음') + '\n')
    context = ssl.create_default_context()
    smtp = None
    try:
        smtp = (smtplib.SMTP_SSL(settings['host'], int(settings['port']), timeout=20, context=context)
                if settings['tls_mode'] == 'tls' else smtplib.SMTP(settings['host'], int(settings['port']), timeout=20))
        if settings['tls_mode'] == 'starttls':
            smtp.starttls(context=context)
        smtp.login(settings['username'], password)
    except (OSError, smtplib.SMTPException):
        if smtp is not None:
            smtp.close()
        raise MailNotSent('SMTP connection or authentication failed before message submission') from None
    try:
        smtp.send_message(message)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
        raise MailNotSent('SMTP explicitly rejected message submission') from None
    except (OSError, smtplib.SMTPException):
        raise MailUncertain('SMTP message acceptance is unknown') from None
    finally:
        # Closing the socket cannot turn an acknowledged DATA response into an unknown send.
        try:
            smtp.close()
        except OSError:
            pass
    return True


def notify(c: Config, report: dict, *, test=False, sender=send_mail) -> dict:
    state = require_state(c, monitor=True)
    if c.mail_config_file is None:
        return {'delivery': 'DISABLED'}
    settings = json.loads(protected_file(c.mail_config_file, private=True))
    if settings.get('enabled') is not True:
        return {'delivery': 'DISABLED'}
    path = state / ('mail-test.json' if test else 'notification.json')
    key = 'one-test' if test else report['status'] + ':' + report['reason']
    with lock(state / 'notification.lock'):
        prior = json.loads(path.read_text()) if path.exists() else {}
        now = int(time.time())
        attempts = 0
        if prior.get('key') == key:
            delivery = prior.get('delivery')
            if delivery == 'SENT':
                return prior
            if test or delivery in ('ATTEMPTED', 'UNCERTAIN', 'RETRIES_EXHAUSTED'):
                raise BackupError('Notification needs operator review; no automatic duplicate send')
            attempts = prior.get('attempts', 0)
            if delivery == 'NOT_SENT' and now < prior.get('retry_after', 0):
                raise BackupError('Notification was not sent; a bounded retry is pending')
        # Record the attempt before SMTP: an uncertain network result must not resend a test automatically.
        attempts += 1
        atomic_json(path, {'key': key, 'delivery': 'ATTEMPTED', 'time': now, 'attempts': attempts})
        try:
            delivered = sender(c, report, test=test)
        except MailNotSent:
            delivery = 'RETRIES_EXHAUSTED' if test or attempts >= 3 else 'NOT_SENT'
            receipt = {'key': key, 'delivery': delivery, 'time': int(time.time()), 'attempts': attempts}
            if delivery == 'NOT_SENT':
                receipt['retry_after'] = int(time.time()) + (60 if attempts == 1 else 300)
            atomic_json(path, receipt)
            raise BackupError('Notification was not sent; inspect delivery state and bounded retry status') from None
        except (MailUncertain, OSError, smtplib.SMTPException):
            atomic_json(path, {'key': key, 'delivery': 'UNCERTAIN', 'time': int(time.time()), 'attempts': attempts})
            raise BackupError('Mail delivery is uncertain; operator review is required before another send') from None
        receipt = {'key': key, 'delivery': 'SENT' if delivered else 'DISABLED', 'time': int(time.time()), 'attempts': attempts}
        atomic_json(path, receipt)
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    modes = parser.add_mutually_exclusive_group()
    for name in ('dry-run', 'render-units', 'initialize', 'initialize-monitor', 'run', 'status', 'notify-status', 'test-email'):
        modes.add_argument('--' + name, action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        c = Config.load(args.config)
        if args.render_units:
            print(json.dumps(unit_files(), indent=2))
        elif args.initialize:
            initialize(c)
            print('Created protected state; no timer or remote backup was started.')
        elif args.initialize_monitor:
            initialize(c, monitor=True)
            print('Created independent monitor state; no mail or timer was started.')
        elif args.run:
            result = run_backup(c, Runner())
            print(json.dumps(result, indent=2))
            return 0 if result['retention'] == 'OK' else 1
        elif args.status or args.notify_status:
            report = freshness(c, Runner())
            if args.notify_status:
                notify(c, report)
            print(json.dumps(report, indent=2))
            return {'HEALTHY': 0, 'WARNING': 1, 'FAILED': 2}[report['status']]
        elif args.test_email:
            require_local_host(c, monitor=True)
            receipt = notify(c, {'status': 'TEST', 'reason': 'ONE_EXPLICIT_NOTIFICATION'}, test=True)
            if receipt.get('delivery') != 'SENT':
                raise BackupError('No confirmed test mail delivery; disabled or uncertain attempts are not retried automatically')
            print('The one test notification has a SENT receipt; no duplicate is sent on re-run.')
        else:
            print(json.dumps(plan(c), indent=2))
        return 0
    except (BackupError, BootstrapError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print('Backup operation stopped: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
