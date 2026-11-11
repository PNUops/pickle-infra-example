"""Would the core backup hook accept the next run?

The core PBS job's hook pins each container's configuration by SHA-256 and
refuses the whole job when any of them changed or is not running. A refused
job is silent until the next morning's backup monitor, so this check computes
the same comparison ahead of time. It reads the hook the job actually selects,
so a re-pinned hook is followed without a catalog change.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

from . import CheckError, _pve, bad, ok

KIND = 'backup_pin'
DETAIL = {'mismatched': [str], 'stopped': [str]}
REASONS = {
    'BACKUP_PIN_MISMATCH': ('설정이 바뀐 컨테이너가 있어 다음 코어 백업이 거부될 거예요: {mismatched}', '백업 거부 예정'),
    'BACKUP_CT_STOPPED': ('멈춘 컨테이너가 있어 다음 코어 백업이 거부될 거예요: {stopped}', '백업 거부 예정'),
    'BACKUP_JOB_MISSING': ('코어 백업 작업이 없거나 꺼져 있어요.', '백업 설정'),
    'BACKUP_HOOK_UNPARSED': ('백업 확인 스크립트의 고정값을 읽지 못했어요.', '점검 오류'),
}
JOB = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
HOOK_PATH = re.compile(r'/[A-Za-z0-9._/-]{1,250}\Z')
LOCK = b'lock: backup\n'


def read_bytes(path):
    return Path(path).read_bytes()


def validate(args):
    if not isinstance(args['job'], str) or not JOB.fullmatch(args['job']):
        raise ValueError('job must be a backup job id')
    for field, default in (('jobs_file', '/etc/pve/jobs.cfg'), ('lxc_dir', '/etc/pve/lxc')):
        value = args.get(field, default)
        if not isinstance(value, str) or not HOOK_PATH.fullmatch(value):
            raise ValueError(f'{field} must be an absolute path')


def job_section(text, job):
    """Return the key/value lines of `vzdump: <job>` in jobs.cfg, or None."""
    sections, current = {}, None
    for line in text.splitlines():
        if line and not line[0].isspace():
            current = line.strip()
            sections[current] = {}
        elif line.strip() and current is not None:
            key, _, value = line.strip().partition(' ')
            sections[current][key] = value.strip()
    return sections.get(f'vzdump: {job}')


def pins(hook_text):
    """The hook's `for vmid in ...` list and each `<id>) expected=<sha> ;;` pair."""
    loop = re.findall(r'^for vmid in ([0-9 ]+); do$', hook_text, re.M)
    expected = dict(re.findall(r'^\s*(\d+)\) expected=([0-9a-f]{64}) ;;$', hook_text, re.M))
    if len(loop) != 1:
        return None
    ids = loop[0].split()
    if not ids or sorted(ids) != sorted(expected) or len(set(ids)) != len(ids):
        return None
    return {int(vmid): expected[vmid] for vmid in ids}


def config_hash(data):
    """Hash as the hook does; one backup lock line means a run is in progress."""
    lines = data.splitlines(keepends=True)
    count = lines.count(LOCK)
    if count > 1:
        return None
    if count == 1:
        lines.remove(LOCK)
    return hashlib.sha256(b''.join(lines)).hexdigest()


def run(args, ctx):
    try:
        jobs = read_bytes(args.get('jobs_file', '/etc/pve/jobs.cfg')).decode('utf-8', 'replace')
    except OSError:
        raise CheckError('COMMAND_FAILED') from None
    section = job_section(jobs, args['job'])
    if section is None or section.get('enabled') == '0':
        return bad('FAIL', 'BACKUP_JOB_MISSING')
    script = section.get('script', '')
    if not HOOK_PATH.fullmatch(script):
        return {'status': 'ERROR', 'reason': 'BACKUP_HOOK_UNPARSED', 'detail': {}}
    try:
        pinned = pins(read_bytes(script).decode('utf-8', 'replace'))
    except OSError:
        raise CheckError('COMMAND_FAILED') from None
    if not pinned:
        return {'status': 'ERROR', 'reason': 'BACKUP_HOOK_UNPARSED', 'detail': {}}
    mismatched, stopped = [], []
    lxc_dir = args.get('lxc_dir', '/etc/pve/lxc')
    for vmid, expected in sorted(pinned.items()):
        try:
            actual = config_hash(read_bytes(f'{lxc_dir}/{vmid}.conf'))
        except FileNotFoundError:
            actual = None
        except OSError:
            raise CheckError('COMMAND_FAILED') from None
        if actual != expected:
            mismatched.append(_pve.ct_label(vmid))
        if ctx.run(['pct', 'status', str(vmid)]) != 'status: running':
            stopped.append(_pve.ct_label(vmid))
    if mismatched:
        return bad('FAIL', 'BACKUP_PIN_MISMATCH', mismatched=mismatched, stopped=stopped)
    if stopped:
        return bad('FAIL', 'BACKUP_CT_STOPPED', mismatched=mismatched, stopped=stopped)
    return ok()
