"""Private, bounded immutable records for one observation source."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4


MAX_SEQUENCE = 2**53 - 1
RECORD = re.compile(r'sample-([a-f0-9]{32})-([0-9]{16})\.json\Z')
CONTROLS = frozenset(('state.json', 'status.json', 'latest.json'))


class StoreError(Exception):
    """A fixed reason; file contents and paths are not part of the message."""


def encoded(value):
    if not isinstance(value, dict):
        raise StoreError('DOCUMENT_INVALID')
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError('DOCUMENT_INVALID') from None


def decoded(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise StoreError('DOCUMENT_INVALID')
            result[key] = value
        return result

    def constant(_):
        raise StoreError('DOCUMENT_INVALID')

    try:
        value = json.loads(payload.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=constant)
        encoded(value)
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise StoreError('DOCUMENT_INVALID') from None


def record_name(epoch, sequence):
    try:
        if not isinstance(epoch, str) or str(UUID(epoch)) != epoch:
            raise ValueError()
        if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
            raise ValueError()
        return f'sample-{UUID(epoch).hex}-{sequence:016d}.json'
    except (ValueError, AttributeError):
        raise StoreError('RECORD_KEY_INVALID') from None


@dataclass(frozen=True)
class Record:
    name: str
    epoch: str
    sequence: int
    document: dict
    payload: bytes
    sha256: str


class ObservationStore:
    def __init__(self, directory, *, max_samples=4096, max_bytes=64 * 1024 * 1024,
                 max_sample_bytes=512 * 1024):
        if (type(max_samples) is not int or not 1 <= max_samples <= 200000
                or type(max_bytes) is not int or not 1 <= max_bytes <= 16 * 1024**3
                or type(max_sample_bytes) is not int or not 1 <= max_sample_bytes <= 1024**2):
            raise StoreError('STORE_LIMIT_INVALID')
        self.directory = Path(directory)
        self.max_samples = max_samples
        self.max_bytes = max_bytes
        self.max_sample_bytes = max_sample_bytes
        self.fd = None
        self.read_only = False

    @contextmanager
    def locked(self, *, read_only=False):
        directory_fd = lock_fd = None
        try:
            try:
                if not read_only:
                    self.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                raise StoreError('DIRECTORY_NOT_PRIVATE') from None
            info = os.fstat(directory_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise StoreError('DIRECTORY_NOT_PRIVATE')
            try:
                flags = os.O_RDONLY | os.O_NOFOLLOW if read_only else os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
                lock_fd = os.open('.store.lock', flags,
                                  0o600, dir_fd=directory_fd)
                self._private(os.fstat(lock_fd))
                fcntl.flock(lock_fd, (fcntl.LOCK_SH if read_only else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreError('ALREADY_RUNNING') from None
            except OSError:
                raise StoreError('FILE_NOT_PRIVATE') from None
            self.fd = directory_fd
            self.read_only = read_only
            yield self
        finally:
            self.fd = None
            self.read_only = False
            if lock_fd is not None:
                os.close(lock_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def _opened(self):
        if self.fd is None:
            raise StoreError('STORE_NOT_LOCKED')

    @staticmethod
    def _private(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise StoreError('FILE_NOT_PRIVATE')

    def _read(self, name, limit):
        self._opened()
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise StoreError('FILE_NOT_PRIVATE') from None
        try:
            before = os.fstat(descriptor)
            self._private(before)
            if before.st_size > limit:
                raise StoreError('FILE_TOO_LARGE')
            payload = bytearray()
            while len(payload) <= limit:
                block = os.read(descriptor, min(65536, limit + 1 - len(payload)))
                if not block:
                    break
                payload.extend(block)
            after = os.fstat(descriptor)
            signature = lambda value: (value.st_dev, value.st_ino, value.st_mode,
                                       value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if len(payload) > limit or len(payload) != before.st_size or signature(before) != signature(after):
                raise StoreError('CONTENT_CHANGED')
            return bytes(payload)
        finally:
            os.close(descriptor)

    def _write(self, name, payload, *, replace):
        self._opened()
        if self.read_only:
            raise StoreError('STORE_READ_ONLY')
        temporary = '.tmp-' + uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.fd)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise StoreError('WRITE_FAILED')
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if replace:
                os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            else:
                os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                        follow_symlinks=False)
                os.unlink(temporary, dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def _entries(self):
        self._opened()
        records = []
        with os.scandir(self.fd) as entries:
            for entry in entries:
                if entry.name == '.store.lock':
                    continue
                if entry.name in CONTROLS:
                    self._private(entry.stat(follow_symlinks=False))
                    continue
                match = RECORD.fullmatch(entry.name)
                if not match:
                    raise StoreError('STORE_ENTRY_INVALID')
                info = entry.stat(follow_symlinks=False)
                self._private(info)
                if info.st_size > self.max_sample_bytes:
                    raise StoreError('FILE_TOO_LARGE')
                sequence = int(match[2])
                if sequence > MAX_SEQUENCE:
                    raise StoreError('STORE_ENTRY_INVALID')
                records.append((entry.name, str(UUID(match[1])), sequence, info.st_size))
                if len(records) > self.max_samples:
                    raise StoreError('STORE_FULL')
        if sum(row[3] for row in records) > self.max_bytes:
            raise StoreError('STORE_FULL')
        return sorted(records, key=lambda row: (row[1], row[2]))

    def put(self, epoch, sequence, document):
        name = record_name(epoch, sequence)
        payload = encoded(document)
        if len(payload) > self.max_sample_bytes:
            raise StoreError('SAMPLE_TOO_LARGE')
        existing = self._read(name, self.max_sample_bytes)
        if existing is not None:
            if existing != payload:
                raise StoreError('CONTENT_CONFLICT')
            return False
        records = self._entries()
        if len(records) >= self.max_samples or sum(row[3] for row in records) + len(payload) > self.max_bytes:
            raise StoreError('STORE_FULL')
        self._write(name, payload, replace=False)
        return True

    def pending(self, limit, *, exclude_keys=()):
        if type(limit) is not int or not 1 <= limit <= 256:
            raise StoreError('BATCH_LIMIT_INVALID')
        excluded = set(exclude_keys)
        if len(excluded) > 64:
            raise StoreError('BATCH_LIMIT_INVALID')
        for epoch, sequence in excluded:
            record_name(epoch, sequence)
        selected = [row for row in self._entries() if (row[1], row[2]) not in excluded][:limit]
        result = []
        for name, epoch, sequence, _ in selected:
            payload = self._read(name, self.max_sample_bytes)
            if payload is None:
                raise StoreError('CONTENT_CHANGED')
            result.append(Record(name, epoch, sequence, decoded(payload), payload,
                                 hashlib.sha256(payload).hexdigest()))
        return result

    def stats(self):
        rows = self._entries()
        return {'samples': len(rows), 'bytes': sum(row[3] for row in rows)}

    def get(self, epoch, sequence):
        name = record_name(epoch, sequence)
        payload = self._read(name, self.max_sample_bytes)
        if payload is None:
            return None
        return Record(name, epoch, sequence, decoded(payload), payload,
                      hashlib.sha256(payload).hexdigest())

    def ack(self, record):
        if self.read_only:
            raise StoreError('STORE_READ_ONLY')
        if not isinstance(record, Record) or record.name != record_name(record.epoch, record.sequence):
            raise StoreError('RECORD_KEY_INVALID')
        payload = self._read(record.name, self.max_sample_bytes)
        if payload is None:
            return False
        if hashlib.sha256(payload).hexdigest() != record.sha256:
            raise StoreError('CONTENT_CHANGED')
        os.unlink(record.name, dir_fd=self.fd)
        os.fsync(self.fd)
        return True

    def load_control(self, name):
        if name not in CONTROLS:
            raise StoreError('CONTROL_NAME_INVALID')
        payload = self._read(name, self.max_sample_bytes if name == 'latest.json' else 65536)
        return None if payload is None else decoded(payload)

    def save_control(self, name, value):
        if name not in CONTROLS:
            raise StoreError('CONTROL_NAME_INVALID')
        payload = encoded(value)
        maximum = self.max_sample_bytes if name == 'latest.json' else 65536
        if len(payload) > maximum:
            raise StoreError('CONTROL_TOO_LARGE')
        self._read(name, maximum)
        self._write(name, payload, replace=True)
