"""Small root-only protected file publisher; no lifecycle/application imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat

from .rpc import ControlError


_OPERATION = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_JSON_PATH = re.compile(r"/etc/streamhome/(?:install\.json|control/(?:stop-epoch\.json|installation\.json|first-start\.json|administration-owner\.json|first-start-attempts/" + _OPERATION + r"\.json|operations/" + _OPERATION + r"/(?:(?:request|dispatch|os-receipt|bridge)|(?:recovery|bridge|os-receipt)-[1-9][0-9]{0,18}|recovery-review-" + _OPERATION + r")\.json|preparations/" + _OPERATION + r"/request\.json))$")
_UNIT_PATHS = frozenset({"/etc/systemd/system/streamhome.service", "/etc/systemd/system/streamhome-operation@.service", "/etc/systemd/system/streamhome-provision@.service", "/etc/systemd/system/streamhome-prepare@.service", "/etc/systemd/system/streamhome-startup@.service", "/etc/polkit-1/rules.d/49-streamhome-operations.rules"})


def _root_directory(metadata) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o7022:
        raise ControlError("control_root_directory_untrusted")


def root_directory(path: Path, *, create: bool = False, gid: int = 0, mode: int = 0o755) -> int:
    if os.geteuid() != 0 or not path.is_absolute() or ".." in path.parts or mode not in {0o700, 0o750, 0o755}:
        raise ControlError("control_root_required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent = os.open("/", flags)
    try:
        _root_directory(os.fstat(parent))
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=mode, dir_fd=parent)
                child = os.open(component, flags, dir_fd=parent)
                os.fchown(child, 0, gid)
                os.fchmod(child, mode)
                os.fsync(parent)
            try:
                _root_directory(os.fstat(child))
            except BaseException:
                os.close(child)
                raise
            os.close(parent)
            parent = child
        return parent
    except BaseException:
        os.close(parent)
        raise


def atomic_root_bytes(path: Path, raw: bytes, *, gid: int = 0, mode: int = 0o644) -> None:
    if (os.geteuid() != 0 or (str(path) not in _UNIT_PATHS and not _JSON_PATH.fullmatch(str(path)))
            or not isinstance(raw, bytes) or len(raw) > 128 * 1024 or mode not in {0o600, 0o640, 0o644}):
        raise ControlError("control_root_destination_invalid")
    parent = root_directory(path.parent)
    temporary = ".root-control-" + secrets.token_hex(16)
    try:
        try:
            previous = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous is not None and (not stat.S_ISREG(previous.st_mode) or previous.st_uid != 0 or previous.st_mode & 0o7133 or previous.st_nlink != 1):
            raise ControlError("control_root_file_untrusted")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchown(handle.fileno(), 0, gid)
            os.fchmod(handle.fileno(), mode)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def atomic_root_json(path: Path, value: dict, *, gid: int = 0, mode: int = 0o644) -> None:
    atomic_root_bytes(path, (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(), gid=gid, mode=mode)
