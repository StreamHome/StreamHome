"""Restore activation provenance, separate from immutable backup identities.

The controller stamps an offline candidate before activation and retains a private
binding to that file's inode and nonce. Ordinary, coordinated SQLite writers may
change rows/schema and WAL on that same installed file. They must not replace the
file or modify this table. This is continuity evidence for those trusted writers,
not authentication of arbitrary database content or a second lifecycle authority.
An already changed legacy installation cannot acquire provenance retrospectively.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Any, Callable, Mapping


TABLE = "streamhome_restore_lineage"
FIELDS = frozenset({"schema_version", "operation_id", "origin_generation", "kind", "input_sha256", "nonce", "device", "inode", "activation_size", "activation_sha256"})


class LineageError(RuntimeError):
    pass


def _identity(path: Path) -> tuple[int, str]:
    file_state = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(file_state.st_mode):
        raise LineageError("database lineage requires a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return file_state.st_size, digest.hexdigest()


def stamp_candidate(
    candidate: Path, *, operation_id: str, generation: int, kind: str,
    input_sha256: str, binding_path: Path,
    write_binding: Callable[[Path, Mapping[str, Any]], None],
) -> tuple[int, str]:
    """Called only for a verified offline copy, before destructive publication."""
    if kind not in {"candidate", "rollback"} or generation < 1:
        raise LineageError("activation lineage identity is invalid")
    nonce = secrets.token_hex(32)
    connection = sqlite3.connect(candidate)
    try:
        connection.execute(f"DROP TABLE IF EXISTS {TABLE}")
        connection.execute(f"CREATE TABLE {TABLE} (singleton INTEGER PRIMARY KEY CHECK(singleton=1), operation_id TEXT NOT NULL, origin_generation INTEGER NOT NULL, kind TEXT NOT NULL, input_sha256 TEXT NOT NULL, nonce TEXT NOT NULL)")
        connection.execute(f"INSERT INTO {TABLE} VALUES (1, ?, ?, ?, ?, ?)", (operation_id, generation, kind, input_sha256, nonce))
        connection.commit()
    finally:
        connection.close()
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    size, digest = _identity(candidate)
    file_state = candidate.lstat()
    write_binding(binding_path, {
        "schema_version": 1, "operation_id": operation_id,
        "origin_generation": generation, "kind": kind,
        "input_sha256": input_sha256, "nonce": nonce,
        "device": file_state.st_dev, "inode": file_state.st_ino,
        "activation_size": size, "activation_sha256": digest,
    })
    return size, digest


def _binding(path: Path) -> dict[str, Any]:
    file_state = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(file_state.st_mode)
            or file_state.st_size > 4096 or stat.S_IMODE(file_state.st_mode) & 0o077
            or (hasattr(os, "geteuid") and file_state.st_uid != os.geteuid())):
        raise LineageError("activation lineage binding is untrusted")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LineageError("activation lineage binding has duplicate fields")
            result[key] = value
        return result
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if (not isinstance(payload, dict) or set(payload) != FIELDS
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or any(type(payload[field]) is not int or payload[field] < 1 for field in ("origin_generation", "inode", "activation_size"))
            or type(payload["device"]) is not int or payload["device"] < 0
            or payload["kind"] not in {"candidate", "rollback"}
            or any(not isinstance(payload[field], str) or not re.fullmatch(r"[0-9a-f]{64}", payload[field]) for field in ("nonce", "input_sha256", "activation_sha256"))):
        raise LineageError("activation lineage binding is invalid")
    return payload


def verify_installed(
    database: Path, binding_path: Path, *, operation_id: str,
    generation: int, kind: str, input_sha256: str,
    activation_identity: tuple[int, str],
) -> dict[str, Any]:
    binding = _binding(binding_path)
    file_state = database.lstat()
    if (database.is_symlink() or not stat.S_ISREG(file_state.st_mode)
            or (hasattr(os, "geteuid") and file_state.st_uid != os.geteuid())
            or binding["operation_id"] != operation_id
            or binding["origin_generation"] > generation
            or binding["kind"] != kind or binding["input_sha256"] != input_sha256
            or (binding["activation_size"], binding["activation_sha256"]) != activation_identity
            or (file_state.st_dev, file_state.st_ino) != (binding["device"], binding["inode"])):
        raise LineageError("installed database continuity does not match activation")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(f"SELECT singleton,operation_id,origin_generation,kind,input_sha256,nonce FROM {TABLE}").fetchall()
        expected = (1, operation_id, binding["origin_generation"], kind, input_sha256, binding["nonce"])
        if rows != [expected]:
            raise LineageError("installed database provenance does not match activation")
    finally:
        connection.close()
    after = database.lstat()
    if (after.st_dev, after.st_ino) != (file_state.st_dev, file_state.st_ino):
        raise LineageError("installed database changed during inspection")
    return binding
