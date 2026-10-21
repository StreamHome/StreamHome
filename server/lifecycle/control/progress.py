"""Bounded display-only progress; never stage receipts or OS authorization.

Polling does not need another interpreter/module-inventory verification on each
tick. Root can display only fixed known labels after opening service-owned data
without following links. No value from this reader selects a command or state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid

from .root_files import root_directory
from .support import PREPARATION_STAGES


def read_at(directory: int, uid: int, parts: tuple[str, ...]) -> dict | None:
    parent = os.dup(directory)
    file = None
    try:
        for component in parts[:-1]:
            if component in {'', '.', '..'} or '/' in component:
                return None
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            os.close(parent)
            parent = child
            metadata = os.fstat(parent)
            if metadata.st_uid != uid or metadata.st_mode & 0o7022:
                return None
        if not parts or parts[-1] != 'progress.json':
            return None
        file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        before = os.fstat(file)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_mode & 0o7177
                or before.st_nlink != 1 or before.st_size > 512):
            return None
        raw = os.read(file, 513)
        after, linked = os.fstat(file), os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_gid, item.st_mode,
                                 item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if len(raw) > 512 or identity(before) != identity(after) or identity(after) != identity(linked):
            return None
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError('duplicate progress field')
                value[key] = item
            return value
        value = json.loads(raw, object_pairs_hook=pairs)
        if (not isinstance(value, dict) or set(value) != {'stage', 'status'}
                or value['stage'] not in PREPARATION_STAGES or value['status'] not in {'running', 'done'}):
            return None
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        return None
    finally:
        if file is not None:
            os.close(file)
        os.close(parent)


def read_display_progress(kind: str, uid: int, operation_id: str) -> dict | None:
    try:
        valid = isinstance(operation_id, str) and str(uuid.UUID(operation_id)) == operation_id and bool(uuid.UUID(operation_id).int)
    except ValueError:
        valid = False
    if not valid:
        return None
    if kind == 'install':
        ancestor, name, parts = Path('/var/lib'), 'streamhome', ('preparation', 'progress.json')
    elif kind == 'repair':
        ancestor, name, parts = Path('/var/cache'), 'streamhome', ('preparation', operation_id, 'progress.json')
    else:
        return None
    parent = root_directory(ancestor)
    directory = None
    try:
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        metadata = os.fstat(directory)
        if metadata.st_uid != uid or metadata.st_mode & 0o7022:
            return None
        return read_at(directory, uid, parts)
    except OSError:
        return None
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)
