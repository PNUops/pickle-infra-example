"""Age of the newest verified database recovery point in the remote PBS groups.

Reimplemented, not ported line for line, from the freshness check in
scripts/lib/db_pbs_backup.py. That check also restores the encrypted
verification receipt and compares manifests, which needs the encryption key,
its escrow receipt and a scratch directory; it stays where it is. This module
reads the same two snapshot lists (the data group and its "-verified" proof
group) with the same proxmox-backup-client invocation, repository, namespace,
password file and pinned server fingerprint taken from the monitor's own
profile, and applies the same rule: the recovery point is the newest proof
point, and it only counts while the data snapshot of that point still exists.
"""
from __future__ import annotations

import json
import re

from . import CheckError, bad, ok
from ._pbs import SHARED_REASONS, absolute_path, kst_label, read_private_json

KIND = 'db_backup'
DETAIL = {'age_min': int, 'recovery_point_kst': str}
REASONS = dict(SHARED_REASONS, **{
    'RECOVERY_POINT_AGING': ('마지막 DB 복구 지점이 {age_min}분 전이에요.', '백업 지연'),
    'RECOVERY_POINT_TOO_OLD': ('마지막 DB 복구 지점이 {age_min}분 전이라 너무 오래됐어요.', '백업 중단'),
    'NO_VERIFIED_CHECKPOINT': ('검증을 마친 DB 복구 지점이 하나도 없어요.', '백업 없음'),
    'PBS_DATA_SNAPSHOT_MISSING': ('검증된 DB 복구 지점의 데이터가 백업 서버에 없어요.', '백업 이상'),
    'CLOCK_AHEAD': ('복구 지점 시각이 현재 시각보다 뒤에 있어요.', '시계 이상'),
})
ARCHIVE = 'platform-db.pxar'
PROOF_ARCHIVE = 'verification.pxar'
REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+@pbs![A-Za-z0-9_-]+@[A-Za-z0-9.-]+(?::[0-9]+)?:[A-Za-z0-9_-]+\Z')
NAMESPACE = re.compile(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,6}\Z')
BACKUP_ID = re.compile(r'[a-z][a-z0-9-]{0,62}\Z')
FINGERPRINT = re.compile(r'[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){31}\Z')


class Unavailable(Exception):
    pass


def validate(args):
    if not absolute_path(args.get('config')):
        raise ValueError('config')
    warn, fail = args.get('warn_age', 600), args.get('fail_age', 900)
    if type(warn) is not int or type(fail) is not int or not 60 <= warn < fail:
        raise ValueError('warn_age must be below fail_age')
    timeout = args.get('timeout', 20)
    if type(timeout) is not int or not 5 <= timeout <= 20:
        raise ValueError('timeout')


def load_profile(path):
    profile = read_private_json(path, max_bytes=262144)
    if (not isinstance(profile.get('repository'), str) or not REPOSITORY.fullmatch(profile['repository'])
            or not isinstance(profile.get('namespace'), str) or not NAMESPACE.fullmatch(profile['namespace'])
            or not isinstance(profile.get('backup_id'), str) or not BACKUP_ID.fullmatch(profile['backup_id'])
            or not isinstance(profile.get('server_fingerprint'), str)
            or not FINGERPRINT.fullmatch(profile['server_fingerprint'])
            or not absolute_path(profile.get('password_file'))):
        raise CheckError('BACKUP_CONFIG_INVALID')
    return profile


def epochs(raw, group_id, archive):
    """Snapshot times in one group that hold the archive; Unavailable on a malformed list."""
    try:
        rows = json.loads(raw)
    except ValueError:
        raise Unavailable() from None
    if not isinstance(rows, list):
        raise Unavailable()
    found = set()
    for row in rows:
        if (not isinstance(row, dict) or row.get('backup-type') != 'host' or row.get('backup-id') != group_id
                or type(row.get('backup-time')) is not int or not isinstance(row.get('files'), list)):
            raise Unavailable()
        names = set()
        for entry in row['files']:
            name = entry.get('filename') if isinstance(entry, dict) else entry
            if not isinstance(name, str):
                raise Unavailable()
            names.add(name)
        if archive in names or archive + '.didx' in names:
            found.add(row['backup-time'])
    return found


def _list(ctx, profile, group_id, timeout):
    # ctx.run passes a fixed environment, so the two PBS variables the original
    # client environment sets go through env(1). Only a file path and the
    # public server fingerprint appear on the command line, never the secret.
    argv = ['env', 'PBS_PASSWORD_FILE=' + profile['password_file'],
            'PBS_FINGERPRINT=' + profile['server_fingerprint'],
            'proxmox-backup-client', 'snapshot', 'list', 'host/' + group_id, '--output-format', 'json',
            '--repository', profile['repository'], '--ns', profile['namespace']]
    try:
        return ctx.run(argv, timeout=timeout)
    except CheckError:
        raise Unavailable() from None


def run(args, ctx):
    profile = load_profile(args['config'])
    backup_id = profile['backup_id']
    timeout = args.get('timeout', 20)
    try:
        proofs = epochs(_list(ctx, profile, backup_id + '-verified', timeout), backup_id + '-verified', PROOF_ARCHIVE)
        data = epochs(_list(ctx, profile, backup_id, timeout), backup_id, ARCHIVE)
    except Unavailable:
        return bad('FAIL', 'PBS_UNAVAILABLE')
    if not proofs:
        return bad('FAIL', 'NO_VERIFIED_CHECKPOINT')
    point = max(proofs)
    age = int(ctx.now) - point
    detail = {'age_min': max(age, 0) // 60, 'recovery_point_kst': kst_label(point, '%H:%M')}
    if point not in data:
        return bad('FAIL', 'PBS_DATA_SNAPSHOT_MISSING', **detail)
    if age < 0:
        return bad('FAIL', 'CLOCK_AHEAD', **detail)
    if age >= args.get('fail_age', 900):
        return bad('FAIL', 'RECOVERY_POINT_TOO_OLD', **detail)
    if age >= args.get('warn_age', 600):
        return bad('WARN', 'RECOVERY_POINT_AGING', **detail)
    return ok(**detail)
