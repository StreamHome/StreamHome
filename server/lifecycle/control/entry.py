#!/usr/bin/python3 -I
"""Installed publisher-authenticated entry/rescue loader (standard library only).

The installer distributes this file in the signed bootstrap artifact. Never run
the mutable checkout copy as root. Its installed parents and every loaded module
must be root-owned and not writable by the service account or other users.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


CONTROL = Path("/usr/local/libexec/streamhome")


class EntryError(RuntimeError):
    pass


def qualified_support(support: dict) -> tuple[int, ...]:
    deadlines = {"startup_recovery": 900, "startup_extension": 30, "runtime_drain": 45, "runtime_stop": 60, "controller": 3600}
    if not isinstance(support, dict) or type(support.get("schema_version")) is not int or support["schema_version"] not in {1, 2}:
        raise EntryError("The installed control support contract is incompatible.")
    fields = {"schema_version", "platform", "deadlines_seconds"}
    if support["schema_version"] == 2:
        fields.add("provisioning_journal_versions")
        deadlines["installation"] = 7200
    versions = [1] if support["schema_version"] == 1 else support.get("provisioning_journal_versions")
    if (set(support) != fields or support["platform"] != "ubuntu-24.04-x86_64-glibc-python-3.12.3-systemd-cgroup2"
            or support["deadlines_seconds"] != deadlines
            or any(type(value) is not int for value in support["deadlines_seconds"].values())
            or versions != ([1] if support["schema_version"] == 1 else [1, 2])
            or any(type(value) is not int for value in versions)):
        raise EntryError("The installed control support contract is incompatible.")
    return tuple(versions)


def _protected(path: Path, *, directory: bool = False):
    metadata = path.lstat()
    if (metadata.st_uid != 0 or metadata.st_mode & 0o7022
            or (not stat.S_ISDIR(metadata.st_mode) if directory else not stat.S_ISREG(metadata.st_mode))
            or not directory and metadata.st_nlink != 1):
        raise EntryError("The installed control entry is not protected. Use the independently verified recovery artifact.")
    return metadata


def _read(path: Path, limit: int = 4 * 1024 * 1024) -> bytes:
    before = _protected(path)
    if before.st_size > limit:
        raise EntryError("The installed control evidence exceeds its bound.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
            raise EntryError("The installed control evidence changed.")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise EntryError("The installed control evidence exceeds its bound.")
    return raw


def _decode(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EntryError("The installed control evidence is ambiguous.")
            result[key] = value
        return result
    value = json.loads(raw.decode(), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise EntryError("The installed control evidence is invalid.")
    return value


def load_bundle(*, rescue: bool = False) -> tuple[Path, dict, str]:
    if Path(__file__) != CONTROL / "provision":
        raise EntryError("Use the installed absolute control entry. Checkout scripts cannot run with elevated privileges.")
    current = Path("/")
    _protected(current, directory=True)
    for component in CONTROL.parts[1:]:
        current /= component
        _protected(current, directory=True)
    _protected(CONTROL / "provision")
    selection = _decode(_read(CONTROL / ("rescue.json" if rescue else "selected.json"), 4096))
    if set(selection) != {"schema_version", "bundle_id", "manifest_sha256"} or type(selection["schema_version"]) is not int or selection["schema_version"] != 1:
        raise EntryError("The installed control selection is invalid.")
    identity, digest = selection["bundle_id"], selection["manifest_sha256"]
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity) or identity == "00000000-0000-0000-0000-000000000000" or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise EntryError("The installed control identity is invalid.")
    versions = CONTROL / ("rescue-versions" if rescue else "versions")
    _protected(versions, directory=True)
    bundle = versions / identity
    _protected(bundle, directory=True)
    raw = _read(bundle / "manifest.json")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise EntryError("The installed control manifest changed. Use the independently verified rescue entry.")
    manifest = _decode(raw)
    if (set(manifest) != {"schema_version", "bundle_id", "entry_protocol", "authority_versions", "control_protocol", "files", "support"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["bundle_id"] != identity or manifest["entry_protocol"] != 1
            or manifest["authority_versions"] != [1, 2] or manifest["control_protocol"] != 1
            or not isinstance(manifest["files"], dict) or not 1 <= len(manifest["files"]) <= 256):
        raise EntryError("The installed control package is incompatible.")
    qualified_support(manifest["support"])
    for name, expected in manifest["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 3 or relative.parts[:2] != ("lib", "lifecycle") or relative.suffix != ".py" or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise EntryError("The installed control manifest contains an unsafe module.")
        current = bundle
        for component in relative.parts[:-1]:
            current /= component
            _protected(current, directory=True)
        if hashlib.sha256(_read(bundle / relative, 2 * 1024 * 1024)).hexdigest() != expected:
            raise EntryError("An installed control module changed. Use the independently verified rescue entry.")
    # Disallow unmanifested importable code, including namespace packages,
    # extension modules, sourceless bytecode and interpreter startup hooks.
    actual = set()
    for path in (bundle / "lib").rglob("*"):
        if path.is_dir():
            _protected(path, directory=True)
        elif path.suffix == ".py":
            actual.add(path.relative_to(bundle).as_posix())
        else:
            raise EntryError("The installed control package contains an unmanifested file.")
    if actual != set(manifest["files"]):
        raise EntryError("The installed control module inventory changed.")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(bundle / "lib"))
    return bundle, manifest, digest


def main() -> int:
    arguments = sys.argv[1:]
    rescue = bool(arguments and arguments[0] == "--rescue")
    if rescue:
        arguments = arguments[1:]
    bundle, manifest, digest = load_bundle(rescue=rescue)
    from lifecycle.control.privileged import dispatch
    return dispatch(arguments, bundle=bundle, manifest=manifest, manifest_sha256=digest, rescue=rescue)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EntryError, OSError, ValueError) as exc:
        print("StreamHome control: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
