"""Small shared helpers: protected file reads, atomic writes, time formatting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile

KST = timezone(timedelta(hours=9))


class OpsError(Exception):
    """An error whose message is safe to print: it never carries protected input."""


def read_protected(path, *, private=False, limit=1 << 20, require_root=True):
    """Read a regular file that nobody but its owner can rewrite.

    O_NOFOLLOW refuses a symlink swap, and the ownership check refuses a file a
    less privileged account could have replaced.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022
                or (require_root and info.st_uid != 0)
                or (private and stat.S_IMODE(info.st_mode) & 0o077)):
            raise OpsError('protected file permissions are not acceptable')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise OpsError('file exceeds its size limit')
        return raw
    finally:
        os.close(fd)


def read_json(path, **kwargs):
    return json.loads(read_protected(path, **kwargs))


def atomic_write(path, data: bytes, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.opswatch-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value, mode=0o600):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode(), mode)


def kst(epoch, fmt='%m-%d %H:%M'):
    return datetime.fromtimestamp(epoch, KST).strftime(fmt)


def kst_day(epoch):
    return datetime.fromtimestamp(epoch, KST).strftime('%Y-%m-%d')


def kst_hour(epoch):
    return datetime.fromtimestamp(epoch, KST).hour


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}초'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes}분'
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f'{hours}시간 {minutes}분' if minutes else f'{hours}시간'
    days, hours = divmod(hours, 24)
    return f'{days}일 {hours}시간' if hours else f'{days}일'
