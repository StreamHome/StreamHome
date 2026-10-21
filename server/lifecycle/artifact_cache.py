"""Verified disposable artifact reuse; callers retain lifecycle write custody."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable
import uuid

from . import state


class ArtifactCacheError(RuntimeError):
    code = "runtime_artifact_cache_failed"


class _CorruptArtifact(ArtifactCacheError):
    pass


class _AbsentArtifact(ArtifactCacheError):
    pass


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_uid, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _private_file(metadata, limit: int) -> None:
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o7177 or metadata.st_nlink != 1 or metadata.st_size > limit):
        raise ArtifactCacheError("An artifact cache file is not qualified private evidence.")


def _copy_verified(source: Path, destination: Path, expected: str, *, limit: int, checkpoint) -> None:
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise _AbsentArtifact("The pinned artifact is not cached.") from exc
    created = False
    complete = False
    try:
        with os.fdopen(source_fd, "rb") as reader:
            before = os.fstat(reader.fileno())
            _private_file(before, limit)
            target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            created = True
            digest, total = hashlib.sha256(), 0
            with os.fdopen(target_fd, "wb") as output:
                while True:
                    checkpoint()
                    block = reader.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if total > limit:
                        raise ArtifactCacheError("An artifact cache entry exceeded its size bound.")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            if (_identity(before) != _identity(os.fstat(reader.fileno()))
                    or _identity(before) != _identity(source.lstat())):
                raise ArtifactCacheError("An artifact cache entry changed during verification.")
            if digest.hexdigest() != expected:
                raise _CorruptArtifact("An artifact cache entry failed its pinned digest.")
            checkpoint()
            complete = True
    finally:
        if created and not complete:
            destination.unlink(missing_ok=True)


def cached_artifact(destination: Path, expected: str, *, fetch: Callable[[Path], None],
                    checkpoint: Callable[[], None] | None = None,
                    cache_root: Path = Path("/var/cache/streamhome/artifacts"), limit: int = 200 * 1024 * 1024) -> None:
    """Copy only fully verified pinned bytes; never execute or admit a runtime.

    Corrupt private cache entries are retained under a unique evidence name.
    Unsafe paths fail closed. A cache miss uses the caller's existing bounded
    verified downloader; a hit needs no network. The hash lock is not authority.
    """
    if (os.geteuid() == 0 or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or type(limit) is not int or not 0 < limit <= 200 * 1024 * 1024):
        raise ArtifactCacheError("Artifact preparation requires an unprivileged pinned input.")
    checkpoint = checkpoint or (lambda: None)
    checkpoint()
    if destination.exists() or destination.is_symlink():
        raise ArtifactCacheError("An existing destination cannot be replaced by the cache.")
    state._validate_directory(cache_root.parent, "artifact cache parent")
    cache_root.mkdir(mode=0o700, exist_ok=True)
    state._validate_directory(cache_root, "artifact cache directory")
    entry = cache_root / expected
    lock_fd = os.open(cache_root / (expected + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    temporary = cache_root / (".fetch-" + uuid.uuid4().hex)
    try:
        _private_file(os.fstat(lock_fd), 0)
        deadline = time.monotonic() + 900
        while True:
            checkpoint()
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ArtifactCacheError("The artifact cache lock exceeded its wait bound.")
                time.sleep(0.1)
        try:
            _copy_verified(entry, destination, expected, limit=limit, checkpoint=checkpoint)
            return
        except _AbsentArtifact:
            # Only failure to open an absent entry is a miss. A file removed
            # during verification is ambiguous evidence and never a cache hit.
            if entry.exists() or entry.is_symlink() or not destination.parent.is_dir():
                raise
        except _CorruptArtifact:
            os.replace(entry, cache_root / (expected + ".corrupt-" + uuid.uuid4().hex))
            state._fsync_directory(cache_root)
        checkpoint()
        fetch(temporary)
        _copy_verified(temporary, destination, expected, limit=limit, checkpoint=checkpoint)
        if entry.exists() or entry.is_symlink():
            raise ArtifactCacheError("The cache slot changed before publication.")
        os.replace(temporary, entry)
        state._fsync_directory(cache_root)
    finally:
        temporary.unlink(missing_ok=True)
        os.close(lock_fd)
