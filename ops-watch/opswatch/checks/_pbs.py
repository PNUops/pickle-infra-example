"""Shared pieces of the PBS-backed checks. Not a check itself (no KIND).

Reason codes that more than one module returns are defined here once, so the
registry never sees the same code with two texts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat

from . import CheckError

# Korea has no daylight saving time; a fixed offset needs no tz database.
KST = timezone(timedelta(hours=9))

SHARED_REASONS = {
    'PBS_UNAVAILABLE': ('백업 서버(PBS)에서 정보를 읽지 못했어요.', '백업 서버 응답 없음'),
    'BACKUP_CONFIG_INVALID': ('점검에 필요한 백업 설정 파일을 읽지 못했어요.', '점검 오류'),
}


def kst_label(epoch, fmt='%m-%d %H:%M'):
    return datetime.fromtimestamp(epoch, KST).strftime(fmt)


def read_private_json(path, max_bytes=65536):
    """Read a root-only JSON file the way the original monitors require it.

    Anything wrong with the file is the checker's own problem, so it is an
    observation failure (ERROR), never a backup verdict.
    """
    try:
        target = Path(path)
        info = target.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_size > max_bytes):
            raise CheckError('BACKUP_CONFIG_INVALID')
        value = json.loads(target.read_bytes())
    except (OSError, ValueError):
        raise CheckError('BACKUP_CONFIG_INVALID') from None
    if not isinstance(value, dict):
        raise CheckError('BACKUP_CONFIG_INVALID')
    return value


def read_private_text(path, max_bytes=4096):
    try:
        target = Path(path)
        info = target.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_size > max_bytes):
            raise CheckError('BACKUP_CONFIG_INVALID')
        return target.read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        raise CheckError('BACKUP_CONFIG_INVALID') from None


def absolute_path(value):
    return isinstance(value, str) and value.startswith('/') and '..' not in Path(value).parts
