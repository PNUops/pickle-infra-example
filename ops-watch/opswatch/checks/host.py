"""The Proxmox host itself: thin pool, root disk, cluster quorum and clock."""
from __future__ import annotations

import re

from . import CheckError, _pve, bad, ok

KIND = 'host'
DETAIL = {'worst_pct': int, 'offset_ms': int, 'problem': [str]}
REASONS = {
    'THIN_POOL_HIGH': ('가상 디스크 저장 공간(thin pool) 사용률이 {worst_pct}%예요.', '디스크 부족'),
    'HOST_DISK_HIGH': ('서버 시스템 디스크 사용률이 {worst_pct}%예요.', '디스크 부족'),
    'QUORUM_LOST': ('클러스터 정족수를 잃었어요. 가상머신 생성과 설정 변경이 막혀요.', '클러스터 이상'),
    'CLOCK_DRIFT': ('서버 시계가 {offset_ms}ms 어긋나 있어요.', '시간 동기화'),
    'CLOCK_UNSYNCED': ('서버 시계가 시간 서버와 동기화되어 있지 않아요.', '시간 동기화'),
    'HOST_PROBLEMS': ('서버에 여러 문제가 있어요: {problem}', '서버 이상'),
}
POOL = re.compile(r'[A-Za-z0-9+_.-]{1,64}/[A-Za-z0-9+_.-]{1,64}\Z')
RANK = {'OK': 0, 'WARN': 1, 'FAIL': 2}


def validate(args):
    pools = args.get('thin_pools', ['pve/data'])
    if not isinstance(pools, list) or len(pools) > 4 or any(not isinstance(p, str) or not POOL.fullmatch(p) for p in pools):
        raise ValueError('thin_pools must be vg/lv names')
    pairs = (('thin_warn', 70, 'thin_fail', 85), ('disk_warn', 80, 'disk_fail', 90))
    for warn_key, warn_default, fail_key, fail_default in pairs:
        warn = _pve.number(args.get(warn_key, warn_default), warn_key, 1, 100)
        fail = _pve.number(args.get(fail_key, fail_default), fail_key, 1, 100)
        if warn > fail:
            raise ValueError(f'{warn_key} must not exceed {fail_key}')
    if type(args.get('cluster', False)) is not bool:
        raise ValueError('cluster must be true or false')
    warn = _pve.number(args.get('offset_warn_ms', 1000), 'offset_warn_ms', 1, 600000)
    fail = _pve.number(args.get('offset_fail_ms', 5000), 'offset_fail_ms', 1, 600000)
    if warn > fail:
        raise ValueError('offset_warn_ms must not exceed offset_fail_ms')


def _percent(text):
    text = text.strip().replace(',', '.')
    if not re.fullmatch(r'\d{1,3}(\.\d+)?', text):
        raise CheckError('CHECK_CRASHED')
    return float(text)


def _level(value, warn, fail):
    return 'FAIL' if value >= fail else 'WARN' if value >= warn else 'OK'


def thin_pools(args, ctx):
    worst = 0.0
    for pool in args.get('thin_pools', ['pve/data']):
        out = ctx.run(['lvs', '--noheadings', '--nosuffix', '--separator', '|',
                       '-o', 'data_percent,metadata_percent', pool])
        rows = [line.strip() for line in out.splitlines() if line.strip()]
        # Exactly one row: more would mean the name matched something other than one pool.
        if len(rows) != 1 or rows[0].count('|') != 1:
            raise CheckError('CHECK_CRASHED')
        worst = max(worst, *(_percent(part) for part in rows[0].split('|')))
    return worst


def root_disk(ctx):
    lines = ctx.run(['df', '-P', '/']).splitlines()
    fields = lines[-1].split() if len(lines) == 2 else []
    if len(fields) < 6 or not re.fullmatch(r'\d{1,3}%', fields[4]):
        raise CheckError('CHECK_CRASHED')
    return int(fields[4][:-1])


def quorate(ctx):
    try:
        out = ctx.run(['pvecm', 'status'])
    except CheckError as error:
        # pvecm exits nonzero when corosync cannot answer, which is itself the outage.
        if error.reason == 'COMMAND_FAILED':
            return False
        raise
    return re.search(r'^Quorate:\s+Yes\s*$', out, re.M) is not None


def clock(ctx):
    """Return (offset in ms, synchronised) from chronyc tracking."""
    out = ctx.run(['chronyc', 'tracking'])
    leap = re.search(r'^Leap status\s*:\s*(.+?)\s*$', out, re.M)
    offset = re.search(r'^System time\s*:\s*([0-9.]+) seconds (fast|slow) of NTP time\s*$', out, re.M)
    if not leap or not offset:
        raise CheckError('CHECK_CRASHED')
    return int(round(float(offset.group(1)) * 1000)), leap.group(1) != 'Not synchronised'


def run(args, ctx):
    found = []  # (status, reason, problem label)
    thin = thin_pools(args, ctx)
    disk = root_disk(ctx)
    level = _level(thin, args.get('thin_warn', 70), args.get('thin_fail', 85))
    if level != 'OK':
        found.append((level, 'THIN_POOL_HIGH', 'thin pool 사용률'))
    level = _level(disk, args.get('disk_warn', 80), args.get('disk_fail', 90))
    if level != 'OK':
        found.append((level, 'HOST_DISK_HIGH', '시스템 디스크'))
    if args.get('cluster', False) and not quorate(ctx):
        found.append(('FAIL', 'QUORUM_LOST', '클러스터 정족수'))
    try:
        offset_ms, synced = clock(ctx)
    except CheckError as error:
        # A stopped chronyd must not hide a full disk or a lost quorum behind a
        # monitoring error; it is itself a host problem worth naming.
        if error.reason != 'COMMAND_FAILED':
            raise
        offset_ms, synced = 0, False
    level = _level(offset_ms, args.get('offset_warn_ms', 1000), args.get('offset_fail_ms', 5000))
    if level != 'OK':
        found.append((level, 'CLOCK_DRIFT', '시계'))
    elif not synced:
        found.append(('WARN', 'CLOCK_UNSYNCED', '시계'))
    # The pool and the root disk share one percentage field: when only one of
    # them is the problem the sentence names that one, otherwise the fuller.
    space = {label for _, _, label in found} & {'thin pool 사용률', '시스템 디스크'}
    worst = thin if space == {'thin pool 사용률'} else disk if space == {'시스템 디스크'} else max(thin, disk)
    detail = {'worst_pct': int(worst), 'offset_ms': offset_ms}
    if not found:
        return ok(**detail)
    status = max((entry[0] for entry in found), key=RANK.get)
    reason = found[0][1] if len(found) == 1 else 'HOST_PROBLEMS'
    return bad(status, reason, problem=[label for _, _, label in found], **detail)
