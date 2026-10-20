"""Read-only managed-installation identity, without application imports.

This module never registers, adopts, initializes or repairs an installation.
The descriptor is path/identity evidence, not lifecycle or setup authority.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Sequence
import uuid


DESCRIPTOR_SCHEMA_VERSION = 1
LAYOUT_VERSION = 1
MAX_DESCRIPTOR_BYTES = 8192
CONFIGURATION_DIRECTORY = Path("/etc/streamhome")
DESCRIPTOR_NAME = "install.json"


class InstallationEvidenceError(ValueError):
    """Unsafe, inconsistent or unsupported evidence; never an empty destination."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InstallationStatus(str, Enum):
    PROVISIONING = "provisioning"
    READY = "ready"


@dataclass(frozen=True)
class ManagedPaths:
    application: Path = Path("/opt/streamhome")
    configuration: Path = CONFIGURATION_DIRECTORY
    state: Path = Path("/var/lib/streamhome")
    cache: Path = Path("/var/cache/streamhome")
    runtime: Path = Path("/run/streamhome")
    control: Path = Path("/usr/local/libexec/streamhome")
    command: Path = Path("/usr/local/bin/streamhome")

    @property
    def database(self) -> Path:
        return self.state / "database.db"

    @property
    def lifecycle(self) -> Path:
        return self.state / "lifecycle"


STANDARD_PATHS = ManagedPaths()


@dataclass(frozen=True)
class InstallationDescriptor:
    installation_id: str
    status: InstallationStatus
    service_uid: int
    service_gid: int
    paths: ManagedPaths = STANDARD_PATHS

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": DESCRIPTOR_SCHEMA_VERSION,
            "layout_version": LAYOUT_VERSION,
            "installation_id": self.installation_id,
            "status": self.status.value,
            "service_uid": self.service_uid,
            "service_gid": self.service_gid,
            "paths": {
                "application": str(self.paths.application),
                "configuration": str(self.paths.configuration),
                "state": str(self.paths.state),
            },
        }


@dataclass(frozen=True)
class DescriptorSnapshot:
    descriptor: InstallationDescriptor
    sha256: str


def _fail(code: str, message: str) -> InstallationEvidenceError:
    return InstallationEvidenceError(code, message)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("invalid_descriptor", "Installation evidence contains duplicate fields.")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise _fail("invalid_descriptor", "Installation evidence contains an invalid number.")


def parse_descriptor(raw: bytes) -> InstallationDescriptor:
    """Decode bounded data only. Ownership must be verified by the caller."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_DESCRIPTOR_BYTES:
        raise _fail("invalid_descriptor", "Installation evidence has an invalid size.")
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except InstallationEvidenceError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _fail("invalid_descriptor", "Installation evidence is not valid JSON.") from exc
    expected = {
        "schema_version", "layout_version", "installation_id", "status",
        "service_uid", "service_gid", "paths",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise _fail("invalid_descriptor", "Installation evidence has unexpected fields.")
    for name, expected_version in (
        ("schema_version", DESCRIPTOR_SCHEMA_VERSION), ("layout_version", LAYOUT_VERSION),
    ):
        if type(payload[name]) is not int or payload[name] != expected_version:
            raise _fail("unsupported_descriptor", "Installation evidence version is unsupported.")
    identity = payload["installation_id"]
    try:
        valid_identity = isinstance(identity, str) and str(uuid.UUID(identity)) == identity
    except ValueError:
        valid_identity = False
    if not valid_identity:
        raise _fail("invalid_descriptor", "Installation identity must be a canonical UUID.")
    try:
        status = InstallationStatus(payload["status"])
    except (ValueError, TypeError) as exc:
        raise _fail("invalid_descriptor", "Installation registration status is invalid.") from exc
    for field in ("service_uid", "service_gid"):
        if type(payload[field]) is not int or not 0 < payload[field] < 2**32 - 1:
            raise _fail("invalid_descriptor", "Service identity must be unprivileged.")
    expected_paths = {
        "application": str(STANDARD_PATHS.application),
        "configuration": str(STANDARD_PATHS.configuration),
        "state": str(STANDARD_PATHS.state),
    }
    if payload["paths"] != expected_paths:
        raise _fail("invalid_descriptor", "Managed installation paths must match the standard layout.")
    return InstallationDescriptor(identity, status, payload["service_uid"], payload["service_gid"])


def validate_managed_port(value: int | str) -> int:
    """Managed web policy only; does not bind sockets or change legacy ports."""
    if type(value) is int:
        port = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value):
        port = int(value, 10)
    else:
        raise _fail("invalid_port", "Web port must be an ASCII decimal integer.")
    if not 1024 <= port <= 65535 or port == 8000:
        raise _fail("invalid_port", "Managed web port must be 1024-65535, excluding 8000.")
    return port


def _validate_metadata(metadata: os.stat_result, *, owner_uid: int, directory: bool) -> None:
    correct_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (
        not correct_type or metadata.st_uid != owner_uid
        or stat.S_IMODE(metadata.st_mode) & 0o7022
        or (not directory and (metadata.st_nlink != 1 or metadata.st_mode & 0o111))
    ):
        raise _fail("untrusted_descriptor", "Installation evidence has unsafe ownership, type or permissions.")


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
        metadata.st_ctime_ns, metadata.st_mode, metadata.st_uid, metadata.st_gid,
        metadata.st_nlink,
    )


def read_descriptor_at(directory_fd: int, *, owner_uid: int = 0) -> DescriptorSnapshot | None:
    """Read inside an already trusted directory capability.

    The privileged caller must supply its own protected directory FD and retain
    the root-owner default. owner_uid supports isolated unprivileged fixtures.
    No pathname or ownership override is exposed by the command entry point.
    """
    _validate_metadata(os.fstat(directory_fd), owner_uid=owner_uid, directory=True)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor_fd = os.open(DESCRIPTOR_NAME, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _fail("untrusted_descriptor", "Installation evidence cannot be opened safely.") from exc
    try:
        before = os.fstat(descriptor_fd)
        _validate_metadata(before, owner_uid=owner_uid, directory=False)
        if before.st_size > MAX_DESCRIPTOR_BYTES:
            raise _fail("invalid_descriptor", "Installation evidence has an invalid size.")
        chunks: list[bytes] = []
        length = 0
        while length <= MAX_DESCRIPTOR_BYTES:
            chunk = os.read(descriptor_fd, min(4096, MAX_DESCRIPTOR_BYTES + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor_fd)
        current = os.stat(DESCRIPTOR_NAME, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise _fail("descriptor_changed", "Installation evidence changed; inspect again.")
        return DescriptorSnapshot(parse_descriptor(raw), hashlib.sha256(raw).hexdigest())
    except OSError as exc:
        raise _fail("descriptor_changed", "Installation evidence could not be read consistently.") from exc
    finally:
        os.close(descriptor_fd)


def inspect_installation() -> DescriptorSnapshot | None:
    """Inspect the fixed managed descriptor; never discover/adopt a checkout."""
    if sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW"):
        raise _fail("unsupported_host", "Managed installation inspection requires Linux file semantics.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        directory_fd = os.open("/", flags)
    except OSError as exc:
        raise _fail("untrusted_descriptor", "Filesystem root cannot be inspected safely.") from exc
    try:
        _validate_metadata(os.fstat(directory_fd), owner_uid=0, directory=True)
        for component in CONFIGURATION_DIRECTORY.parts[1:]:
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise _fail("untrusted_descriptor", "Configuration directory cannot be opened safely.") from exc
            os.close(directory_fd)
            directory_fd = child_fd
            _validate_metadata(os.fstat(directory_fd), owner_uid=0, directory=True)
        snapshot = read_descriptor_at(directory_fd)
        current = os.stat(CONFIGURATION_DIRECTORY, follow_symlinks=False)
        opened = os.fstat(directory_fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise _fail("descriptor_changed", "Configuration directory changed; inspect again.")
        return snapshot
    except OSError as exc:
        code = "descriptor_changed" if exc.errno == errno.ENOENT else "untrusted_descriptor"
        raise _fail(code, "Installation evidence cannot be inspected safely.") from exc
    finally:
        os.close(directory_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only managed StreamHome installation inspection.")
    parser.add_argument("command", nargs="?", choices=("inspect",), help="Inspect the fixed managed descriptor as JSON.")
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0
    try:
        snapshot = inspect_installation()
    except InstallationEvidenceError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}))
        return 2
    if snapshot is None:
        print(json.dumps({"ok": True, "managed": False, "legacy_discovery_required": True}))
        return 4
    print(json.dumps({"ok": True, "managed": True, "descriptor": snapshot.descriptor.payload(), "sha256": snapshot.sha256}))
    return 0 if snapshot.descriptor.status is InstallationStatus.READY else 3


if __name__ == "__main__":
    raise SystemExit(main())
