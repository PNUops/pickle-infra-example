"""Daily encrypted core container archives visible in a PVE storage backed by PBS.

Ported from the observation half of scripts/core-pbs-monitor.py: the same
storage query, the same per-archive validity rule, the same daily window and
deadline, and the same protected-baseline requirement. Incident state, mail
and retries are the judge's job now and are not carried over.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
import json
import re

from . import CheckError, bad, ok
from ._pbs import KST, SHARED_REASONS, kst_label

KIND = 'core_backup'
DETAIL = {
    'pending': [str],
    'missing': [str],
    'invalid': [str],
    'missing_count': int,
    'last_backup_kst': str,
    'checked': int,
}
REASONS = dict(SHARED_REASONS, **{
    'BACKUP_MISSING': ('오늘 받아야 할 코어 백업 {missing_count}건이 마감 시각까지 없어요.', '백업 누락'),
    'BACKUP_INVALID_METADATA': ('최신 코어 백업의 정보가 예상과 달라 정상 백업으로 볼 수 없어요.', '백업 이상'),
    'PROTECTED_BASELINE_MISSING': ('보호 지정된 기준 백업이 보이지 않아요.', '백업 이상'),
})
CLOCK = re.compile(r'([01]\d|2[0-3]):[0-5]\d\Z')
NAME = re.compile(r'[a-z][a-z0-9-]{0,31}\Z')
FINGERPRINT = re.compile(r'[0-9a-f]{2}(?::[0-9a-f]{2}){31}\Z')
STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z')


def validate(args):
    for key in ('node', 'storage'):
        if not isinstance(args.get(key), str) or not NAME.fullmatch(args[key]):
            raise ValueError(key)
    ctids = args.get('ctids')
    if (not isinstance(ctids, list) or not ctids or len(ctids) > 16
            or any(type(c) is not int or not 100 <= c <= 999999 for c in ctids) or len(set(ctids)) != len(ctids)):
        raise ValueError('ctids')
    if not isinstance(args.get('fingerprint'), str) or not FINGERPRINT.fullmatch(args['fingerprint']):
        raise ValueError('fingerprint')
    protected = args.get('protected', {})
    if not isinstance(protected, dict):
        raise ValueError('protected')
    for ctid, volid in protected.items():
        if not ctid.isdigit() or int(ctid) not in ctids or not isinstance(volid, str):
            raise ValueError('protected')
        prefix = f'{args["storage"]}:backup/ct/{ctid}/'
        if not volid.startswith(prefix) or not STAMP.fullmatch(volid[len(prefix):]):
            raise ValueError('protected')
    for key, default in (('window', '03:17'), ('deadline', '04:30')):
        if not isinstance(args.get(key, default), str) or not CLOCK.fullmatch(args.get(key, default)):
            raise ValueError(key)
    if _clock(args.get('window', '03:17')) >= _clock(args.get('deadline', '04:30')):
        raise ValueError('the window must open before the deadline')
    timeout = args.get('timeout', 40)
    if type(timeout) is not int or not 5 <= timeout <= 40:
        raise ValueError('timeout')


def _clock(text):
    hour, minute = text.split(':')
    return time(int(hour), int(minute))


def _valid(row, ctid, args, now_epoch):
    volid = row.get('volid')
    prefix = f'{args["storage"]}:backup/ct/{ctid}/'
    if not isinstance(volid, str) or not volid.startswith(prefix) or not STAMP.fullmatch(volid[len(prefix):]):
        return False
    try:
        snapshot_epoch = int(datetime.strptime(volid[len(prefix):], '%Y-%m-%dT%H:%M:%SZ')
                             .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return False
    return (type(row.get('ctime')) is int and snapshot_epoch == row['ctime'] and row['ctime'] <= now_epoch
            and row.get('format') == 'pbs-ct' and row.get('subtype') == 'lxc'
            and row.get('content') == 'backup' and row.get('encrypted') == args['fingerprint']
            and type(row.get('size')) is int and row['size'] > 0)


def inspect(content, args, now_epoch):
    """Judge one storage listing. Pure, so tests can drive it without a command."""
    now = datetime.fromtimestamp(now_epoch, KST)
    window = datetime.combine(now.date(), _clock(args.get('window', '03:17')), KST).timestamp()
    deadline = datetime.combine(now.date(), _clock(args.get('deadline', '04:30')), KST).timestamp()
    protected = args.get('protected', {})
    behind, invalid, baseline, last_points, overdue = [], [], [], [], []
    for ctid in args['ctids']:
        label = f'CT{ctid}'
        rows = [row for row in content if isinstance(row, dict) and row.get('vmid') == ctid]
        valid_rows = [row for row in rows if _valid(row, ctid, args, now_epoch)]
        if valid_rows:
            last_points.append(max(row['ctime'] for row in valid_rows))
        manual = protected.get(str(ctid))
        if manual is not None and not any(row.get('volid') == manual and row.get('protected') == 1
                                          for row in valid_rows):
            baseline.append(label)
        daily = [row for row in rows if row.get('volid') != manual and type(row.get('ctime')) is int]
        if not daily:
            behind.append(label)
            overdue.append(label)
            continue
        newest = max(daily, key=lambda row: row['ctime'])
        if not _valid(newest, ctid, args, now_epoch):
            invalid.append(label)
        elif newest['ctime'] < window:
            behind.append(label)
            if newest['ctime'] < window - 86400:
                # Yesterday's run is missing too, so waiting for today's
                # deadline would report a recovery that never happened.
                overdue.append(label)
    detail = {'checked': len(args['ctids'])}
    if last_points and (behind or invalid):
        detail['last_backup_kst'] = kst_label(min(last_points))
    if baseline:
        return bad('FAIL', 'PROTECTED_BASELINE_MISSING', missing=baseline, missing_count=len(baseline), **detail)
    if not behind and not invalid:
        return ok(**detail)
    if now_epoch < deadline:
        if overdue:
            return bad('FAIL', 'BACKUP_MISSING', missing=overdue, missing_count=len(overdue), **detail)
        # Today's run may still be going; the original monitor called this PENDING.
        return ok(pending=behind + invalid, **detail)
    if invalid:
        return bad('FAIL', 'BACKUP_INVALID_METADATA', invalid=invalid, missing=behind,
                   missing_count=len(behind), **detail)
    return bad('FAIL', 'BACKUP_MISSING', missing=behind, missing_count=len(behind), **detail)


def run(args, ctx):
    command = ['pvesh', 'get', f'/nodes/{args["node"]}/storage/{args["storage"]}/content',
               '--content', 'backup', '--output-format', 'json']
    try:
        content = json.loads(ctx.run(command, timeout=args.get('timeout', 40)))
    except (CheckError, ValueError):
        # A storage backed by an unreachable PBS fails here too; the original
        # monitor reported this as a failed backup check, not a checker fault.
        return bad('FAIL', 'PBS_UNAVAILABLE')
    if not isinstance(content, list):
        return bad('FAIL', 'PBS_UNAVAILABLE')
    return inspect(content, args, ctx.now)
