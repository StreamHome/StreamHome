"""Root-owned first-install journal beneath an inode-bound service state root.

The protected /etc anchor is necessary because the service owns /var/lib/streamhome
and can rename its children. Every root write uses validated directory FDs, never
an unchecked service-controlled parent pathname.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat

from .provisioning_contract import STAGES, parse_intent
from .root_files import root_directory
from .rpc import ControlError


STATE = Path("/var/lib/streamhome")


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _state(intent):
    binding = {name: (device, inode) for name, device, inode in intent.directories}.get(str(STATE))
    parent = root_directory(STATE.parent)
    try:
        descriptor = os.open(STATE.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if (binding != _identity(metadata) or metadata.st_uid not in {0, intent.service_uid}
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid == intent.service_uid and metadata.st_gid != intent.service_gid):
            os.close(descriptor)
            raise ControlError("control_installation_directory_changed")
        return descriptor
    finally:
        os.close(parent)


def _journal_directory(intent):
    parent = _state(intent)
    current_name = ""
    bindings = {name: (device, inode) for name, device, inode in intent.mirror_directories}
    try:
        for component in ("provisioning", intent.installation_id):
            current_name = current_name + "/" + component if current_name else component
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            metadata = os.fstat(child)
            if (_identity(metadata) != bindings.get(current_name) or metadata.st_uid != 0
                    or metadata.st_gid != intent.service_gid or stat.S_IMODE(metadata.st_mode) != 0o750):
                os.close(child)
                raise ControlError("control_installation_mirror_changed")
            os.close(parent)
            parent = child
        return parent
    except BaseException:
        os.close(parent)
        raise


def _prefix(previous, candidate):
    append_only = {"directories", "mirror_directories", "stage", "application_commit", "execution_sha256",
                   "start_permit", "start_invocation", "start_boot_id", "start_stop_epoch"}
    if any(getattr(previous, name) != getattr(candidate, name) for name in previous.__dataclass_fields__ if name not in append_only):
        return False
    if STAGES.index(previous.stage) > STAGES.index(candidate.stage):
        return False
    for field in ("directories", "mirror_directories"):
        before = {name: (device, inode) for name, device, inode in getattr(previous, field)}
        after = {name: (device, inode) for name, device, inode in getattr(candidate, field)}
        if any(after.get(name) != value for name, value in before.items()):
            return False
    for name in ("application_commit", "execution_sha256", "start_invocation", "start_boot_id", "start_stop_epoch"):
        if getattr(previous, name) is not None and getattr(previous, name) != getattr(candidate, name):
            return False
    return ("absent", "issued", "consumed").index(previous.start_permit) <= ("absent", "issued", "consumed").index(candidate.start_permit)


def publish_mirror(intent) -> None:
    """Reconcile only a trusted monotonic prefix after the /etc anchor is durable."""
    if os.geteuid() != 0:
        raise ControlError("control_root_required")
    parent = _journal_directory(intent)
    temporary = ".install-" + secrets.token_hex(16)
    try:
        try:
            descriptor = os.open("install.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            if intent.stage != "intent":
                raise ControlError("control_installation_mirror_missing")
        else:
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_gid != intent.service_gid
                        or stat.S_IMODE(before.st_mode) != 0o640 or before.st_nlink != 1 or before.st_size > 32768):
                    raise ControlError("control_installation_mirror_untrusted")
                raw = handle.read(32769)
                after = os.fstat(handle.fileno())
                linked = os.stat("install.json", dir_fd=parent, follow_symlinks=False)
                identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_gid, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns, item.st_nlink)
                if len(raw) > 32768 or identity(before) != identity(after) or identity(before) != identity(linked):
                    raise ControlError("control_installation_mirror_changed")
            def pairs(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ControlError("control_installation_mirror_untrusted")
                    value[key] = item
                return value
            previous = parse_intent(json.loads(raw.decode(), object_pairs_hook=pairs))
            if not _prefix(previous, intent):
                raise ControlError("control_installation_mirror_conflict")
            if previous == intent:
                os.fsync(parent)
                return
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as output:
            os.fchown(output.fileno(), 0, intent.service_gid)
            os.fchmod(output.fileno(), 0o640)
            output.write((json.dumps(intent.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False)+"\n").encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, "install.json", src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        current = _journal_directory(intent)
        try:
            if _identity(os.fstat(current)) != _identity(os.fstat(parent)):
                raise ControlError("control_installation_mirror_changed")
        finally:
            os.close(current)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def reserve_mirror(intent, advance):
    """Before account/package creation, bind the state and root journal inodes."""
    if os.geteuid() != 0 or intent.stage != "intent":
        raise ControlError("control_initial_preparation_forbidden")
    parent = root_directory(STATE.parent)
    descriptor = None
    try:
        try:
            descriptor = os.open(STATE.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            os.mkdir(STATE.name, 0o700, dir_fd=parent)
            os.fsync(parent)
            descriptor = os.open(STATE.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        bindings = {name: (device, inode) for name, device, inode in intent.directories}
        metadata = os.fstat(descriptor)
        if str(STATE) not in bindings:
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700 or os.listdir(descriptor):
                raise ControlError("control_existing_directory_not_adopted")
            intent = advance(intent, directories=tuple(sorted((*intent.directories, (str(STATE), metadata.st_dev, metadata.st_ino)))))
        elif bindings[str(STATE)] != _identity(metadata) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ControlError("control_installation_directory_changed")
        current_name = ""
        for component in ("provisioning", intent.installation_id):
            current_name = current_name + "/" + component if current_name else component
            mirrors = {name: (device, inode) for name, device, inode in intent.mirror_directories}
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            except FileNotFoundError:
                if current_name in mirrors:
                    raise ControlError("control_installation_mirror_missing")
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
                os.fchown(child, 0, intent.service_gid)
                os.fchmod(child, 0o750)
                os.fsync(descriptor)
            metadata = os.fstat(child)
            if (metadata.st_uid != 0 or metadata.st_gid != intent.service_gid or stat.S_IMODE(metadata.st_mode) != 0o750
                    or current_name in mirrors and mirrors[current_name] != _identity(metadata)
                    or current_name not in mirrors and os.listdir(child)):
                os.close(child)
                raise ControlError("control_installation_mirror_changed")
            if current_name not in mirrors:
                try:
                    intent = advance(intent, mirror_directories=tuple(sorted((*intent.mirror_directories, (current_name, metadata.st_dev, metadata.st_ino)))))
                except BaseException:
                    os.close(child)
                    raise
            os.close(descriptor)
            descriptor = child
        publish_mirror(intent)
        return intent
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)
