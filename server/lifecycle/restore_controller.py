"""Offline database-restore controller and crash-recovery protocol.

This module intentionally imports only the standard library, the lifecycle
authority, and the existing runtime ownership helper.  It is usable while the
FastAPI application is stopped and never imports configuration, database models,
routes, or application services.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest
import uuid

from . import backup_manifest, database_lineage, state
from .configuration_paths import ConfigurationPathError, resolve_temp_directory


JOURNAL_SCHEMA_VERSION = 1
MAX_JOURNAL_BYTES = 32 * 1024
REQUIRED_TABLES = frozenset({"user", "movie", "authsession"})
BACKUP_NAME_RE = re.compile(
    rf"(?:{backup_manifest.LEGACY_BACKUP_RE.pattern}|{backup_manifest.MANIFEST_BACKUP_RE.pattern})"
)
ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,80}$")


class RestoreError(RuntimeError):
    code = "restore_error"


class RestoreValidationError(RestoreError):
    code = "restore_candidate_invalid"


class RestoreConflictError(RestoreError):
    code = "restore_conflict"


class RestorePersistenceError(RestoreError):
    code = "restore_persistence_error"


class RestoreRecoveryRequired(RestoreError):
    code = "restore_recovery_required"


class RestoreResponseGateError(RestoreError):
    code = "restore_response_not_confirmed"


class RestoreProgress(str, Enum):
    CLAIMED = "claimed"
    CONTROLLER_OWNED = "controller_owned"
    DRAINED = "drained"
    RUNTIME_STOPPED = "runtime_stopped"
    CHECKPOINT_READY = "checkpoint_ready"
    CANDIDATE_STAGED = "candidate_staged"
    ACTIVATION_STARTED = "activation_started"
    ACTIVATION_COMPLETED = "activation_completed"
    VERIFYING_CANDIDATE = "verifying_candidate"
    ACCEPTED = "accepted"
    NORMAL_RECOVERY = "normal_recovery"
    ROLLBACK_STARTED = "rollback_started"
    ROLLBACK_ACTIVATED = "rollback_activated"
    VERIFYING_ROLLBACK = "verifying_rollback"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


class ActivationState(str, Enum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    COMPLETED = "completed"
    ROLLBACK_STARTED = "rollback_started"
    ROLLBACK_COMPLETED = "rollback_completed"


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RestorePaths:
    root: Path
    operation_id: str
    operation_dir: Path
    journal_path: Path
    journal_lock_path: Path
    candidate_path: Path
    rollback_path: Path
    database_path: Path
    backup_dir: Path


@dataclass(slots=True)
class RestoreHandoff:
    process: subprocess.Popen[bytes]
    operation_id: str
    generation: int
    _gate_fd: int
    _released: bool = False

    def release_after_response(self) -> None:
        if self._released:
            return
        try:
            os.write(self._gate_fd, b"1")
        except OSError:
            pass
        finally:
            os.close(self._gate_fd)
            self._released = True


def _await_response_completion(gate_fd: int, timeout: float = 15.0) -> None:
    """Require the explicit post-ASGI-response byte before destructive work."""

    try:
        ready, _, _ = select.select([gate_fd], [], [], timeout)
        if not ready:
            raise RestoreResponseGateError("restore response completion was not confirmed")
        if os.read(gate_fd, 1) != b"1":
            raise RestoreResponseGateError("restore response completion channel closed")
    finally:
        os.close(gate_fd)


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("restore write made no progress")
        remaining = remaining[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_private_directory(path: Path, description: str) -> None:
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or path.is_symlink():
        raise RestorePersistenceError(f"{description} is not a trusted directory")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise RestorePersistenceError(f"{description} has unexpected ownership")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise RestorePersistenceError(f"{description} permissions are not private")


def _ensure_private_directory(path: Path, description: str) -> None:
    if path.exists() or path.is_symlink():
        _validate_private_directory(path, description)
        return
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    _fsync_directory(path.parent)


def restore_paths(root: str | os.PathLike[str], operation_id: str) -> RestorePaths:
    installation = state.resolve_installation_root(root)
    canonical_id = str(uuid.UUID(operation_id))
    if canonical_id != operation_id:
        raise ValueError("operation_id must be a canonical UUID")
    lifecycle = state.lifecycle_paths(installation)
    operations = lifecycle.lifecycle_dir / "restore"
    operation_dir = operations / canonical_id
    return RestorePaths(
        root=installation,
        operation_id=canonical_id,
        operation_dir=operation_dir,
        journal_path=operation_dir / "journal.json",
        journal_lock_path=operation_dir / "journal.lock",
        candidate_path=operation_dir / "candidate.db",
        rollback_path=operation_dir / "rollback.db",
        database_path=installation / "server" / "database.db",
        backup_dir=installation / "server" / "backup",
    )


def _ensure_restore_directories(paths: RestorePaths) -> None:
    lifecycle = state.lifecycle_paths(paths.root)
    # Lifecycle directory creation and trust checks remain owned by state.py.
    state.inspect_state(paths.root)
    if not lifecycle.lifecycle_dir.exists():
        raise RestorePersistenceError("lifecycle authority directory is unavailable")
    restore_root = paths.operation_dir.parent
    _ensure_private_directory(restore_root, "restore operation directory")
    _ensure_private_directory(paths.operation_dir, "restore operation artifact directory")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RestorePersistenceError("restore journal contains duplicate fields")
        result[key] = value
    return result


def _decode_journal(raw: bytes) -> dict[str, Any]:
    def reject_constant(_value: str) -> None:
        raise RestorePersistenceError("restore journal contains a non-finite number")

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=reject_constant,
        )
    except RestoreError:
        raise
    except (UnicodeError, ValueError, TypeError) as exc:
        raise RestorePersistenceError("restore journal cannot be read") from exc
    if not isinstance(payload, dict):
        raise RestorePersistenceError("restore journal must be an object")
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or path.is_symlink() or st.st_size > MAX_JOURNAL_BYTES:
            raise RestorePersistenceError("restore journal is not a trusted file")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise RestorePersistenceError("restore journal has unexpected ownership")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise RestorePersistenceError("restore journal permissions are not private")
        payload = _decode_journal(path.read_bytes())
    except RestoreError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RestorePersistenceError("restore journal cannot be read") from exc
    return payload


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    replaced = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        replaced = True
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise RestorePersistenceError("restore journal could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _journal_lock(paths: RestorePaths):
    _ensure_restore_directories(paths)
    descriptor = os.open(
        paths.journal_lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    st = os.fstat(descriptor)
    if not stat.S_ISREG(st.st_mode):
        os.close(descriptor)
        raise RestorePersistenceError("restore journal lock is not a regular file")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        os.close(descriptor)
        raise RestorePersistenceError("restore journal lock has unexpected ownership")
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def _validate_journal(payload: Mapping[str, Any], paths: RestorePaths) -> dict[str, Any]:
    required = {
        "schema_version", "operation_id", "generation", "progress", "activation_state",
        "candidate_filename", "candidate_size", "candidate_sha256", "staged_size",
        "staged_sha256", "rollback_size", "rollback_sha256", "previous_size",
        "previous_sha256", "rollback_activation_size", "rollback_activation_sha256", "accepted",
        "outcome", "error_code", "created_at", "updated_at", "drain_timed_out",
    }
    if set(payload) != required:
        raise RestorePersistenceError("restore journal contains missing or unsupported fields")
    if payload.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise RestorePersistenceError("restore journal schema is unsupported")
    if payload.get("operation_id") != paths.operation_id:
        raise RestorePersistenceError("restore journal operation does not match")
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise RestorePersistenceError("restore journal generation is invalid")
    try:
        progress = RestoreProgress(payload.get("progress"))
        activation = ActivationState(payload.get("activation_state"))
    except (TypeError, ValueError) as exc:
        raise RestorePersistenceError("restore journal state is invalid") from exc
    filename = payload.get("candidate_filename")
    if not isinstance(filename, str) or not BACKUP_NAME_RE.fullmatch(filename):
        raise RestorePersistenceError("restore candidate identity is invalid")
    artifact_sizes = (
        "candidate_size", "staged_size", "rollback_size", "previous_size",
        "rollback_activation_size",
    )
    artifact_hashes = (
        "candidate_sha256", "staged_sha256", "rollback_sha256", "previous_sha256",
        "rollback_activation_sha256",
    )
    for field in artifact_sizes:
        value = payload.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise RestorePersistenceError("restore artifact size is invalid")
    for field in artifact_hashes:
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
            raise RestorePersistenceError("restore artifact digest is invalid")
    if payload["candidate_size"] <= 0 or payload["candidate_sha256"] is None:
        raise RestorePersistenceError("restore candidate identity is incomplete")
    for size_field, digest_field in (
        ("staged_size", "staged_sha256"),
        ("rollback_size", "rollback_sha256"),
        ("previous_size", "previous_sha256"),
        ("rollback_activation_size", "rollback_activation_sha256"),
    ):
        if (payload[size_field] is None) != (payload[digest_field] is None):
            raise RestorePersistenceError("restore artifact identity is incomplete")
    if not isinstance(payload.get("accepted"), bool):
        raise RestorePersistenceError("restore acceptance state is invalid")
    if not isinstance(payload.get("drain_timed_out"), bool):
        raise RestorePersistenceError("restore drain state is invalid")
    for field in ("created_at", "updated_at"):
        value = payload.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise RestorePersistenceError("restore journal timestamp is invalid")
    if payload["updated_at"] < payload["created_at"]:
        raise RestorePersistenceError("restore journal timestamps are inconsistent")
    outcome = payload.get("outcome")
    if outcome not in {None, "accepted", "succeeded", "rolled_back"}:
        raise RestorePersistenceError("restore outcome is invalid")
    error_code = payload.get("error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or not ERROR_CODE_RE.fullmatch(error_code)
    ):
        raise RestorePersistenceError("restore error code is invalid")

    accepted = payload["accepted"]
    if not accepted and outcome is not None:
        raise RestorePersistenceError("unaccepted restore cannot have an outcome")
    if accepted and outcome is None:
        raise RestorePersistenceError("accepted restore requires an outcome")
    if accepted and progress not in {
        RestoreProgress.ACCEPTED,
        RestoreProgress.NORMAL_RECOVERY,
        RestoreProgress.SUCCEEDED,
        RestoreProgress.ROLLED_BACK,
        RestoreProgress.RECOVERY_REQUIRED,
    }:
        raise RestorePersistenceError("restore acceptance is inconsistent with progress")
    expected_progress_for_outcome = {
        "accepted": {
            RestoreProgress.ACCEPTED,
            RestoreProgress.NORMAL_RECOVERY,
            RestoreProgress.RECOVERY_REQUIRED,
        },
        "succeeded": {RestoreProgress.SUCCEEDED, RestoreProgress.RECOVERY_REQUIRED},
        "rolled_back": {
            RestoreProgress.NORMAL_RECOVERY,
            RestoreProgress.ROLLED_BACK,
            RestoreProgress.RECOVERY_REQUIRED,
        },
    }
    if outcome is not None and progress not in expected_progress_for_outcome[outcome]:
        raise RestorePersistenceError("restore outcome is inconsistent with progress")
    if outcome in {"accepted", "succeeded"} and activation is not ActivationState.COMPLETED:
        raise RestorePersistenceError("accepted candidate activation is inconsistent")
    if outcome == "rolled_back" and activation is not ActivationState.ROLLBACK_COMPLETED:
        raise RestorePersistenceError("accepted rollback activation is inconsistent")
    if progress is RestoreProgress.RECOVERY_REQUIRED and error_code is None:
        raise RestorePersistenceError("restore recovery-required state needs an error code")
    if error_code is not None and progress not in {
        RestoreProgress.RECOVERY_REQUIRED,
        RestoreProgress.SUCCEEDED,
        RestoreProgress.ROLLED_BACK,
    }:
        raise RestorePersistenceError("restore error code is inconsistent with progress")

    staged = payload["staged_sha256"] is not None
    rollback = payload["rollback_sha256"] is not None
    previous = payload["previous_sha256"] is not None
    rollback_activation = payload["rollback_activation_sha256"] is not None
    early = {
        RestoreProgress.CLAIMED,
        RestoreProgress.DRAINED,
        RestoreProgress.CONTROLLER_OWNED,
        RestoreProgress.RUNTIME_STOPPED,
    }
    if progress in early and (staged or rollback or previous or rollback_activation):
        raise RestorePersistenceError("early restore progress contains destructive artifacts")
    if progress in {
        RestoreProgress.CHECKPOINT_READY,
        RestoreProgress.CANDIDATE_STAGED,
        RestoreProgress.ACTIVATION_STARTED,
        RestoreProgress.ACTIVATION_COMPLETED,
        RestoreProgress.VERIFYING_CANDIDATE,
        RestoreProgress.ACCEPTED,
        RestoreProgress.NORMAL_RECOVERY,
        RestoreProgress.ROLLBACK_STARTED,
        RestoreProgress.ROLLBACK_ACTIVATED,
        RestoreProgress.VERIFYING_ROLLBACK,
        RestoreProgress.SUCCEEDED,
        RestoreProgress.ROLLED_BACK,
    } and not (rollback and previous):
        raise RestorePersistenceError("restore checkpoint identity is missing")
    if progress in {
        RestoreProgress.CANDIDATE_STAGED,
        RestoreProgress.ACTIVATION_STARTED,
        RestoreProgress.ACTIVATION_COMPLETED,
        RestoreProgress.VERIFYING_CANDIDATE,
        RestoreProgress.ACCEPTED,
        RestoreProgress.SUCCEEDED,
    } and not staged:
        raise RestorePersistenceError("staged candidate identity is missing")
    if activation is ActivationState.NOT_STARTED and rollback_activation:
        raise RestorePersistenceError("rollback activation identity is premature")
    if activation is ActivationState.STARTED and not (staged and rollback and previous):
        raise RestorePersistenceError("candidate activation identity is incomplete")
    if activation is ActivationState.COMPLETED and not (staged and rollback and previous):
        raise RestorePersistenceError("completed activation identity is incomplete")
    if activation in {ActivationState.ROLLBACK_STARTED, ActivationState.ROLLBACK_COMPLETED} and not (
        rollback and previous and rollback_activation
    ):
        raise RestorePersistenceError("rollback activation identity is incomplete")

    if progress is RestoreProgress.ACTIVATION_STARTED and activation is not ActivationState.STARTED:
        raise RestorePersistenceError("candidate activation marker is inconsistent")
    if progress in {
        RestoreProgress.ACTIVATION_COMPLETED,
        RestoreProgress.VERIFYING_CANDIDATE,
    } and activation is not ActivationState.COMPLETED:
        raise RestorePersistenceError("candidate activation completion is inconsistent")
    if progress is RestoreProgress.ROLLBACK_STARTED and activation is not ActivationState.ROLLBACK_STARTED:
        raise RestorePersistenceError("rollback marker is inconsistent")
    if progress in {
        RestoreProgress.ROLLBACK_ACTIVATED,
        RestoreProgress.VERIFYING_ROLLBACK,
    } and activation is not ActivationState.ROLLBACK_COMPLETED:
        raise RestorePersistenceError("rollback completion is inconsistent")
    if progress is RestoreProgress.ACCEPTED and not (
        accepted and outcome == "accepted" and activation is ActivationState.COMPLETED
    ):
        raise RestorePersistenceError("restore acceptance state is inconsistent")
    if progress is RestoreProgress.SUCCEEDED and not (
        accepted and outcome == "succeeded" and activation is ActivationState.COMPLETED
    ):
        raise RestorePersistenceError("successful restore state is inconsistent")
    if progress is RestoreProgress.ROLLED_BACK and not (
        accepted and outcome == "rolled_back" and activation is ActivationState.ROLLBACK_COMPLETED
    ):
        raise RestorePersistenceError("rolled-back restore state is inconsistent")
    if progress is RestoreProgress.NORMAL_RECOVERY:
        if outcome == "accepted" and activation is not ActivationState.COMPLETED:
            raise RestorePersistenceError("accepted recovery activation is inconsistent")
        if outcome == "rolled_back" and activation is not ActivationState.ROLLBACK_COMPLETED:
            raise RestorePersistenceError("rollback recovery activation is inconsistent")
    return dict(payload)


_LEGAL_PROGRESS_TRANSITIONS: Mapping[RestoreProgress, frozenset[RestoreProgress]] = {
    RestoreProgress.CLAIMED: frozenset({RestoreProgress.DRAINED, RestoreProgress.CONTROLLER_OWNED, RestoreProgress.CHECKPOINT_READY, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.DRAINED: frozenset({RestoreProgress.CONTROLLER_OWNED, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.CONTROLLER_OWNED: frozenset({RestoreProgress.RUNTIME_STOPPED, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.RUNTIME_STOPPED: frozenset({RestoreProgress.CHECKPOINT_READY, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.CHECKPOINT_READY: frozenset({RestoreProgress.CANDIDATE_STAGED, RestoreProgress.ROLLBACK_STARTED, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.CANDIDATE_STAGED: frozenset({RestoreProgress.ACTIVATION_STARTED, RestoreProgress.ROLLBACK_STARTED, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.ACTIVATION_STARTED: frozenset({RestoreProgress.ACTIVATION_COMPLETED, RestoreProgress.CANDIDATE_STAGED, RestoreProgress.ROLLBACK_STARTED, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.ACTIVATION_COMPLETED: frozenset({RestoreProgress.VERIFYING_CANDIDATE, RestoreProgress.ROLLBACK_STARTED, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.VERIFYING_CANDIDATE: frozenset({RestoreProgress.ACCEPTED, RestoreProgress.ROLLBACK_STARTED, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.ACCEPTED: frozenset({RestoreProgress.NORMAL_RECOVERY, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.NORMAL_RECOVERY: frozenset({RestoreProgress.SUCCEEDED, RestoreProgress.ROLLED_BACK, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.ROLLBACK_STARTED: frozenset({RestoreProgress.ROLLBACK_ACTIVATED, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.ROLLBACK_ACTIVATED: frozenset({RestoreProgress.VERIFYING_ROLLBACK, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.VERIFYING_ROLLBACK: frozenset({RestoreProgress.NORMAL_RECOVERY, RestoreProgress.RECOVERY_REQUIRED}),
    RestoreProgress.SUCCEEDED: frozenset(),
    RestoreProgress.ROLLED_BACK: frozenset(),
    RestoreProgress.RECOVERY_REQUIRED: frozenset(),
}


def _validate_journal_transition(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if after["operation_id"] != before["operation_id"] or after["generation"] != before["generation"]:
        raise RestorePersistenceError("restore journal identity cannot change")
    previous = RestoreProgress(before["progress"])
    following = RestoreProgress(after["progress"])
    if following is not previous and following not in _LEGAL_PROGRESS_TRANSITIONS[previous]:
        raise RestorePersistenceError("restore journal progress transition is not allowed")
    if before["accepted"] and not after["accepted"]:
        raise RestorePersistenceError("restore acceptance cannot be revoked")
    for field in (
        "candidate_filename",
        "candidate_size",
        "candidate_sha256",
        "created_at",
    ):
        if after[field] != before[field]:
            raise RestorePersistenceError("restore journal immutable identity changed")
    for field in (
        "staged_size",
        "staged_sha256",
        "rollback_size",
        "rollback_sha256",
        "previous_size",
        "previous_sha256",
        "rollback_activation_size",
        "rollback_activation_sha256",
    ):
        if before[field] is not None and after[field] != before[field]:
            raise RestorePersistenceError("restore artifact identity cannot change")
    if before["drain_timed_out"] and not after["drain_timed_out"]:
        raise RestorePersistenceError("restore drain timeout cannot be revoked")
    before_outcome = before["outcome"]
    after_outcome = after["outcome"]
    if before_outcome is not None and after_outcome != before_outcome:
        if not (before_outcome == "accepted" and after_outcome == "succeeded"):
            raise RestorePersistenceError("restore outcome transition is not allowed")


def create_restore_journal(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    candidate: CandidateIdentity,
) -> dict[str, Any]:
    paths = restore_paths(root, operation_id)
    descriptor = _journal_lock(paths)
    try:
        if paths.journal_path.exists() or paths.journal_path.is_symlink():
            raise RestoreConflictError("restore journal already exists")
        now = time.time()
        payload = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "operation_id": operation_id,
            "generation": generation,
            "progress": RestoreProgress.CLAIMED.value,
            "activation_state": ActivationState.NOT_STARTED.value,
            "candidate_filename": candidate.filename,
            "candidate_size": candidate.size,
            "candidate_sha256": candidate.sha256,
            "staged_size": None,
            "staged_sha256": None,
            "rollback_size": None,
            "rollback_sha256": None,
            "previous_size": None,
            "previous_sha256": None,
            "rollback_activation_size": None,
            "rollback_activation_sha256": None,
            "accepted": False,
            "outcome": None,
            "error_code": None,
            "drain_timed_out": False,
            "created_at": now,
            "updated_at": now,
        }
        _atomic_write_json(paths.journal_path, payload)
        return payload
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def read_restore_journal(root: str | os.PathLike[str], operation_id: str) -> dict[str, Any]:
    paths = restore_paths(root, operation_id)
    descriptor = _journal_lock(paths)
    try:
        return _validate_journal(_read_json(paths.journal_path), paths)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def inspect_restore_journal(
    root: str | os.PathLike[str], operation_id: str
) -> dict[str, Any]:
    """Read a restore journal without creating its directory or lock file."""

    paths = restore_paths(root, operation_id)
    descriptor: int | None = None
    try:
        before = paths.journal_path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or paths.journal_path.is_symlink()
            or before.st_size > MAX_JOURNAL_BYTES
            or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise RestorePersistenceError("restore journal is not a trusted file")
        descriptor = os.open(
            paths.journal_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NOATIME", 0),
        )
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RestorePersistenceError("restore journal changed during inspection")
        chunks: list[bytes] = []
        remaining = MAX_JOURNAL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_JOURNAL_BYTES:
            raise RestorePersistenceError("restore journal exceeds its size limit")
        return _validate_journal(_decode_journal(raw), paths)
    except RestoreError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RestorePersistenceError("restore journal cannot be inspected") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def update_restore_journal(
    root: str | os.PathLike[str], operation_id: str, **changes: Any
) -> dict[str, Any]:
    paths = restore_paths(root, operation_id)
    descriptor = _journal_lock(paths)
    try:
        payload = _validate_journal(_read_json(paths.journal_path), paths)
        payload.update(changes)
        payload["updated_at"] = time.time()
        validated = _validate_journal(payload, paths)
        _validate_journal_transition(_validate_journal(_read_json(paths.journal_path), paths), validated)
        _atomic_write_json(paths.journal_path, validated)
        return validated
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def accept_guarded_runtime(
    root: str | os.PathLike[str], operation_id: str, *, generation: int, decision: str
) -> dict[str, Any]:
    """Commit or repeat one exact acceptance without rewinding recovery progress."""
    if decision not in {"accept", "rollback"}:
        raise RestoreConflictError("unsupported restore acceptance decision")
    paths = restore_paths(root, operation_id)
    descriptor = _journal_lock(paths)
    try:
        # Generation transfer takes journal then authority locks as well. Holding
        # both prevents a stale guarded request from accepting a transferred owner.
        with state.hold_operation_with_credential(
            root, operation_id=operation_id, generation=generation,
            expected_phase=state.LifecyclePhase.VERIFYING,
            expected_operation_type=state.OperationType.RESTORE,
        ) as authority:
            if authority.active_operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER:
                raise RestoreConflictError("restore acceptance owner does not match")
            before = _validate_journal(_read_json(paths.journal_path), paths)
            if before["generation"] != generation:
                raise RestoreConflictError("restore acceptance generation does not match")
            outcome = "accepted" if decision == "accept" else "rolled_back"
            if before["accepted"]:
                if before["outcome"] != outcome:
                    raise RestoreConflictError("restore acceptance decision cannot change")
                if before["progress"] not in {RestoreProgress.ACCEPTED.value, RestoreProgress.NORMAL_RECOVERY.value}:
                    raise RestoreConflictError("restore acceptance progress is not resumable")
                # An interrupted earlier response may have followed an uncertain
                # fsync outcome. Reaffirm durability before acknowledging a retry.
                try:
                    journal_fd = os.open(paths.journal_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        os.fsync(journal_fd)
                    finally:
                        os.close(journal_fd)
                    _fsync_directory(paths.operation_dir)
                except OSError as exc:
                    raise RestorePersistenceError("restore acceptance durability could not be reaffirmed") from exc
                return before
            payload = {**before, "accepted": True, "outcome": outcome,
                       "progress": (RestoreProgress.ACCEPTED.value if decision == "accept" else RestoreProgress.NORMAL_RECOVERY.value),
                       "updated_at": time.time()}
            validated = _validate_journal(payload, paths)
            _validate_journal_transition(before, validated)
            _atomic_write_json(paths.journal_path, validated)
            return validated
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def record_restore_completion_intent(
    paths: RestorePaths, *, generation: int, outcome: str
) -> None:
    """Persist verified terminal evidence before releasing shared authority."""

    journal = read_restore_journal(paths.root, paths.operation_id)
    current = state.inspect_state(paths.root)
    operation = current.active_operation
    if (
        operation is None
        or operation.operation_id != paths.operation_id
        or operation.operation_type is not state.OperationType.RESTORE
        or current.generation != generation
        or journal["generation"] != generation
        or journal["progress"] != RestoreProgress.NORMAL_RECOVERY.value
        or not journal["accepted"]
        or journal["outcome"] != ("accepted" if outcome == "succeeded" else "rolled_back")
        or outcome not in {"succeeded", "rolled_back"}
        or not installed_database_is_recognized(paths, journal)
    ):
        raise RestoreConflictError("restore completion evidence is not exact")
    completion_snapshot = paths.operation_dir / "completion.db"
    _copy_sqlite(paths.database_path, completion_snapshot, source_standalone=False)
    size, digest = artifact_identity(completion_snapshot)
    if not installed_database_is_recognized(paths, journal):
        raise RestoreConflictError("restore database continuity changed during completion")
    marker = {
        "schema_version": 2,
        "operation_id": paths.operation_id,
        "generation": generation,
        "outcome": outcome,
        "canonical_size": size,
        "canonical_sha256": digest,
    }
    _atomic_write_json(paths.operation_dir / "completion-intent.json", marker)


def reconcile_restore_completion_intent(
    root: str | os.PathLike[str], operation_id: str, generation: int
) -> bool:
    """Finalize only a verified, already-completed restore journal."""

    paths = restore_paths(root, operation_id)
    try:
        current = state.inspect_state(paths.root)
        # A later completed operation may advance the global generation; the
        # marker and restore journal must still agree on this exact restore.
        if current.active_operation is not None or current.generation < generation:
            return False
        marker = _read_json(paths.operation_dir / "completion-intent.json")
        if set(marker) != {
            "schema_version", "operation_id", "generation", "outcome",
            "canonical_size", "canonical_sha256",
        } or (
            type(marker["schema_version"]) is not int
            or marker["schema_version"] not in {1, 2}
            or marker["operation_id"] != operation_id
            or type(marker["generation"]) is not int
            or marker["generation"] != generation
            or marker["outcome"] not in {"succeeded", "rolled_back"}
            or type(marker["canonical_size"]) is not int
            or marker["canonical_size"] <= 0
            or not isinstance(marker["canonical_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", marker["canonical_sha256"])
        ):
            return False
        journal = inspect_restore_journal(paths.root, operation_id)
        expected = (
            (journal.get("staged_size"), journal.get("staged_sha256"))
            if marker["outcome"] == "succeeded"
            else (journal.get("rollback_activation_size"), journal.get("rollback_activation_sha256"))
        )
        if (
            journal["generation"] != generation
            or journal["progress"] != RestoreProgress.NORMAL_RECOVERY.value
            or not journal["accepted"]
            or journal["outcome"] != (
                "accepted" if marker["outcome"] == "succeeded" else "rolled_back"
            )
            or not installed_database_is_recognized(paths, journal)
        ):
            return False
        if marker["schema_version"] == 1:
            if expected != (marker["canonical_size"], marker["canonical_sha256"]) or artifact_identity(paths.database_path) != expected:
                return False
        else:
            # Once authority completes, normal coordinated writers may resume.
            # The immutable, private snapshot authenticates the exact completion
            # point; activation lineage authenticates continuity of the live DB.
            snapshot = paths.operation_dir / "completion.db"
            validate_sqlite_database(snapshot, standalone=True)
            if artifact_identity(snapshot) != (marker["canonical_size"], marker["canonical_sha256"]):
                return False
        update_restore_journal(
            paths.root,
            operation_id,
            progress=(
                RestoreProgress.SUCCEEDED.value
                if marker["outcome"] == "succeeded"
                else RestoreProgress.ROLLED_BACK.value
            ),
            outcome=marker["outcome"],
        )
        return True
    except (RestoreError, state.LifecycleError, OSError, TypeError, ValueError):
        return False


def transfer_restore_journal_generation(
    root: str | os.PathLike[str],
    operation_id: str,
    *,
    expected_generation: int,
    next_generation: int,
    journal_generation: int | None = None,
) -> dict[str, Any]:
    paths = restore_paths(root, operation_id)
    descriptor = _journal_lock(paths)
    try:
        payload = _validate_journal(_read_json(paths.journal_path), paths)
        expected_journal_generation = (
            expected_generation
            if journal_generation is None
            else journal_generation
        )
        if (
            payload["generation"] != expected_journal_generation
            or expected_journal_generation
            not in {expected_generation, expected_generation - 1}
            or next_generation != expected_generation + 1
        ):
            raise RestoreConflictError("restore journal generation transfer does not match")
        current = state.inspect_state(paths.root)
        operation = current.active_operation
        if not (
            operation is not None
            and operation.operation_id == operation_id
            and operation.operation_type is state.OperationType.RESTORE
            and current.generation == next_generation
        ):
            raise RestoreConflictError("restore authority generation transfer does not match")
        payload["generation"] = next_generation
        payload["updated_at"] = time.time()
        validated = _validate_journal(payload, paths)
        _atomic_write_json(paths.journal_path, validated)
        return validated
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def resume_restore_journal_for_operator(
    root: str | os.PathLike[str],
    operation_id: str,
    *,
    generation: int,
    action: str,
) -> dict[str, Any]:
    """Re-enter the canonical journal state machine from explicit recovery."""

    paths = restore_paths(root, operation_id)
    descriptor = _journal_lock(paths)
    try:
        payload = _validate_journal(_read_json(paths.journal_path), paths)
        current = state.inspect_state(paths.root)
        operation = current.active_operation
        if (
            operation is None
            or operation.operation_id != operation_id
            or operation.operation_type is not state.OperationType.RESTORE
            or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or current.generation != generation
            or payload["generation"] != generation
            or payload["progress"] != RestoreProgress.RECOVERY_REQUIRED.value
        ):
            raise RestoreConflictError("operator restore evidence no longer matches")
        if action == "safe_abort":
            return payload
        activation = ActivationState(payload["activation_state"])
        if action == "rollback":
            if (
                payload["accepted"]
                or activation is not ActivationState.NOT_STARTED
                or not payload.get("rollback_sha256")
            ):
                raise RestoreConflictError("operator rollback evidence no longer matches")
            progress = RestoreProgress.CHECKPOINT_READY
        elif action == "continue_recovery":
            if payload["accepted"]:
                progress = (
                    RestoreProgress.NORMAL_RECOVERY
                    if payload.get("outcome") == "rolled_back"
                    else RestoreProgress.ACCEPTED
                )
            elif activation is ActivationState.ROLLBACK_STARTED:
                progress = RestoreProgress.ROLLBACK_STARTED
            elif activation is ActivationState.ROLLBACK_COMPLETED:
                progress = RestoreProgress.ROLLBACK_ACTIVATED
            elif activation is ActivationState.STARTED:
                progress = RestoreProgress.ACTIVATION_STARTED
            elif activation is ActivationState.COMPLETED:
                progress = RestoreProgress.ACTIVATION_COMPLETED
            elif payload.get("staged_sha256"):
                progress = RestoreProgress.CANDIDATE_STAGED
            elif payload.get("rollback_sha256"):
                progress = RestoreProgress.CHECKPOINT_READY
            else:
                progress = RestoreProgress.CONTROLLER_OWNED
        else:
            raise RestoreConflictError("operator restore action is unsupported")
        payload["progress"] = progress.value
        payload["error_code"] = None
        payload["updated_at"] = time.time()
        validated = _validate_journal(payload, paths)
        _atomic_write_json(paths.journal_path, validated)
        return validated
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_identity(path: Path) -> tuple[int, str]:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or path.is_symlink():
        raise RestoreValidationError("restore artifact is not a regular file")
    return st.st_size, _sha256(path)


def installed_database_is_recognized(paths: RestorePaths, journal: Mapping[str, Any]) -> bool:
    """Verify activation continuity; legacy artifacts retain exact-hash semantics."""
    rollback = journal.get("outcome") == "rolled_back" or journal.get("activation_state") in {
        ActivationState.ROLLBACK_STARTED.value, ActivationState.ROLLBACK_COMPLETED.value,
    }
    kind = "rollback" if rollback else "candidate"
    expected = (
        (journal.get("rollback_activation_size"), journal.get("rollback_activation_sha256"))
        if rollback else (journal.get("staged_size"), journal.get("staged_sha256"))
    )
    input_digest = journal.get("rollback_sha256") if rollback else journal.get("candidate_sha256")
    binding = paths.operation_dir / f"{kind}-lineage.json"
    try:
        if not expected[1]:
            return False
        validate_sqlite_database(paths.database_path)
        if not binding.exists() and not binding.is_symlink():
            return artifact_identity(paths.database_path) == expected
        database_lineage.verify_installed(
            paths.database_path, binding, operation_id=paths.operation_id,
            generation=int(journal["generation"]), kind=kind,
            input_sha256=str(input_digest), activation_identity=expected,
        )
        return True
    except (RestoreError, database_lineage.LineageError, OSError, sqlite3.Error, ValueError, TypeError):
        return False


def consistent_database_identity(paths: RestorePaths) -> tuple[int, str]:
    """Hash a SQLite backup snapshot, including committed WAL, under writer drain."""
    snapshot = paths.operation_dir / f".completion-snapshot.{secrets.token_hex(8)}.db"
    try:
        _copy_sqlite(paths.database_path, snapshot, source_standalone=False)
        return artifact_identity(snapshot)
    finally:
        try:
            snapshot.unlink()
        except FileNotFoundError:
            pass


def captured_work_has_effect(
    lease_ids: frozenset[str],
    active_work: Sequence[Mapping[str, object]],
    effect: str,
) -> bool:
    """Inspect only work captured by one exact process-local drain fence."""

    return any(
        item.get("lease_id") in lease_ids
        and effect in tuple(item.get("effects", ()))
        for item in active_work
    )


def _reject_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            raise RestoreValidationError("restore candidate must be a standalone SQLite database")


def validate_sqlite_database(path: Path, *, standalone: bool = False) -> None:
    try:
        if standalone:
            _reject_sqlite_sidecars(path)
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise RestoreValidationError("restore candidate is not a SQLite database")
        immutable = "&immutable=1" if standalone else ""
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro{immutable}",
            uri=True,
        )
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RestoreValidationError("restore database integrity check failed")
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not REQUIRED_TABLES.issubset(tables):
                raise RestoreValidationError("restore database schema is incompatible")
        finally:
            connection.close()
    except RestoreError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise RestoreValidationError("restore database validation failed") from exc


def prevalidate_candidate(root: str | os.PathLike[str], filename: str) -> CandidateIdentity:
    installation = state.resolve_installation_root(root)
    if not isinstance(filename, str) or not BACKUP_NAME_RE.fullmatch(filename):
        raise RestoreValidationError("restore candidate name is invalid")
    configured_backup_root = installation / "server" / "backup"
    if configured_backup_root.is_symlink() or not configured_backup_root.is_dir():
        raise RestoreValidationError("restore backup directory is not trusted")
    backup_root = configured_backup_root.resolve()
    try:
        state.qualify_local_filesystem(
            backup_root,
            require_private=False,
            description="backup directory",
        )
    except state.LifecycleError as exc:
        raise RestoreValidationError("restore backup filesystem is unsupported") from exc
    candidate = backup_root / filename
    try:
        st = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(backup_root)
    except (OSError, ValueError) as exc:
        raise RestoreValidationError("restore candidate is unavailable") from exc
    if candidate.is_symlink() or not stat.S_ISREG(st.st_mode):
        raise RestoreValidationError("restore candidate is not a trusted regular file")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise RestoreValidationError("restore candidate has unexpected ownership")
    try:
        evidence = backup_manifest.validate_backup_pair(resolved)
    except backup_manifest.BackupManifestError as exc:
        raise RestoreValidationError(exc.code) from exc
    validate_sqlite_database(resolved, standalone=True)
    return CandidateIdentity(
        filename,
        evidence.database.size_bytes,
        evidence.database.sha256,
    )


def _copy_sqlite(source: Path, destination: Path, *, source_standalone: bool = True) -> None:
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
    connection: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        if source_standalone:
            _reject_sqlite_sidecars(source)
        immutable = "&immutable=1" if source_standalone else ""
        connection = sqlite3.connect(
            f"file:{source.as_posix()}?mode=ro{immutable}",
            uri=True,
        )
        target = sqlite3.connect(temporary)
        connection.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        target.close()
        target = None
        connection.close()
        connection = None
        os.chmod(temporary, 0o600)
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        validate_sqlite_database(temporary, standalone=True)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        _fsync_directory(destination.parent)
    except (OSError, sqlite3.Error) as exc:
        raise RestorePersistenceError("SQLite restore artifact could not be materialized") from exc
    finally:
        if target is not None:
            target.close()
        if connection is not None:
            connection.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _close_sqlite_journal(database_path: Path) -> None:
    if not database_path.is_file() or database_path.is_symlink():
        raise RestoreRecoveryRequired("canonical database is unavailable")
    try:
        connection = sqlite3.connect(database_path)
        try:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and int(checkpoint[0]) != 0:
                raise RestoreRecoveryRequired("canonical database WAL remains busy")
            mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
            if mode != "delete":
                raise RestoreRecoveryRequired("canonical database journal mode could not be closed")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RestoreRecoveryRequired("canonical database WAL checkpoint failed") from exc
    for suffix in ("-wal", "-shm"):
        if Path(f"{database_path}{suffix}").exists():
            raise RestoreRecoveryRequired("canonical database sidecar remains after SQLite closure")


def create_rollback_checkpoint(paths: RestorePaths) -> tuple[int, str, int, str]:
    require_no_media_publication_evidence(paths.root)
    _close_sqlite_journal(paths.database_path)
    previous_size, previous_hash = artifact_identity(paths.database_path)
    _copy_sqlite(paths.database_path, paths.rollback_path, source_standalone=True)
    validate_sqlite_database(paths.rollback_path, standalone=True)
    rollback_size, rollback_hash = artifact_identity(paths.rollback_path)
    return rollback_size, rollback_hash, previous_size, previous_hash


def stage_candidate(paths: RestorePaths, journal: Mapping[str, Any]) -> tuple[int, str]:
    identity = prevalidate_candidate(paths.root, str(journal["candidate_filename"]))
    if identity.size != journal["candidate_size"] or not hmac.compare_digest(
        identity.sha256, str(journal["candidate_sha256"])
    ):
        raise RestoreValidationError("restore source changed after acceptance")
    source = paths.backup_dir / identity.filename
    _copy_sqlite(source, paths.candidate_path, source_standalone=True)
    if artifact_identity(source) != (identity.size, identity.sha256):
        raise RestoreValidationError("restore source changed while it was staged")
    if paths.candidate_path.stat().st_dev != paths.database_path.parent.stat().st_dev:
        raise RestorePersistenceError("restore staging is not on the canonical database filesystem")
    validate_sqlite_database(paths.candidate_path, standalone=True)
    return database_lineage.stamp_candidate(
        paths.candidate_path, operation_id=paths.operation_id,
        generation=int(journal["generation"]), kind="candidate",
        input_sha256=str(journal["candidate_sha256"]),
        binding_path=paths.operation_dir / "candidate-lineage.json",
        write_binding=_atomic_write_json,
    )


def activate_staged_candidate(paths: RestorePaths, journal: Mapping[str, Any]) -> None:
    require_no_media_publication_evidence(paths.root)
    size, digest = artifact_identity(paths.candidate_path)
    if size != journal.get("staged_size") or not hmac.compare_digest(
        digest, str(journal.get("staged_sha256") or "")
    ):
        raise RestoreRecoveryRequired("staged candidate identity is ambiguous")
    update_restore_journal(
        paths.root,
        paths.operation_id,
        progress=RestoreProgress.ACTIVATION_STARTED.value,
        activation_state=ActivationState.STARTED.value,
    )
    os.replace(paths.candidate_path, paths.database_path)
    os.chmod(paths.database_path, 0o600)
    _fsync_directory(paths.database_path.parent)
    update_restore_journal(
        paths.root,
        paths.operation_id,
        progress=RestoreProgress.ACTIVATION_COMPLETED.value,
        activation_state=ActivationState.COMPLETED.value,
    )


def restore_rollback_checkpoint(paths: RestorePaths, journal: Mapping[str, Any]) -> None:
    require_no_media_publication_evidence(paths.root)
    size, digest = artifact_identity(paths.rollback_path)
    if size != journal.get("rollback_size") or not hmac.compare_digest(
        digest, str(journal.get("rollback_sha256") or "")
    ):
        raise RestoreRecoveryRequired("rollback checkpoint identity is ambiguous")
    # The failed candidate may have created a legitimate WAL.  Close it through
    # SQLite before replacing the main file so no stale sidecar can be replayed
    # against the rollback database.
    _close_sqlite_journal(paths.database_path)
    staged = paths.operation_dir / "rollback-activation.db"
    if journal["activation_state"] == ActivationState.ROLLBACK_STARTED.value:
        if artifact_identity(staged) != (journal["rollback_activation_size"], journal["rollback_activation_sha256"]):
            raise RestoreRecoveryRequired("pending rollback activation identity is ambiguous")
    else:
        _copy_sqlite(paths.rollback_path, staged)
        activation_size, activation_hash = database_lineage.stamp_candidate(
            staged, operation_id=paths.operation_id,
            generation=int(journal["generation"]), kind="rollback",
            input_sha256=str(journal["rollback_sha256"]),
            binding_path=paths.operation_dir / "rollback-lineage.json",
            write_binding=_atomic_write_json,
        )
        update_restore_journal(
            paths.root,
            paths.operation_id,
            progress=RestoreProgress.ROLLBACK_STARTED.value,
            activation_state=ActivationState.ROLLBACK_STARTED.value,
            rollback_activation_size=activation_size,
            rollback_activation_sha256=activation_hash,
        )
    os.replace(staged, paths.database_path)
    os.chmod(paths.database_path, 0o600)
    _fsync_directory(paths.database_path.parent)
    update_restore_journal(
        paths.root,
        paths.operation_id,
        progress=RestoreProgress.ROLLBACK_ACTIVATED.value,
        activation_state=ActivationState.ROLLBACK_COMPLETED.value,
    )


def require_no_media_publication_evidence(root: Path) -> None:
    """Never replace DB receipts while media publication recovery remains pending."""
    try:
        # Accepted placements deliberately outlive the database being restored.
        # Validate them even when there is no pending publication directory.
        from .storage_placement import (
            StoragePlacementRecoveryRequired, validate_placement_manifests,
        )
        try:
            validate_placement_manifests(root)
        except StoragePlacementRecoveryRequired as exc:
            raise RestoreRecoveryRequired("accepted storage placements require recovery") from exc
        database_path = root / "server" / "database.db"
        if database_path.exists():
            if database_path.is_symlink() or not database_path.is_file():
                raise RestoreRecoveryRequired("storage operation evidence is ambiguous")
            connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "storageoperation" in tables and connection.execute(
                    "SELECT 1 FROM storageoperation WHERE kind IN ('offload','cleanup','keep_local') "
                    "AND (status IN ('queued','running','blocked') OR phase='cleanup_debt') LIMIT 1"
                ).fetchone():
                    raise RestoreRecoveryRequired("storage operations must settle before database restore")
                if "storagereservation" in tables and connection.execute(
                    "SELECT 1 FROM storagereservation WHERE state='active' LIMIT 1"
                ).fetchone():
                    raise RestoreRecoveryRequired("storage reservations must settle before database restore")
            finally:
                connection.close()
        operations = resolve_temp_directory(root) / "media-operations"
        if not operations.exists() and not operations.is_symlink():
            return
        if operations.is_symlink() or not operations.is_dir():
            raise RestoreRecoveryRequired("media publication evidence root is ambiguous")
        if any(operations.iterdir()):
            raise RestoreRecoveryRequired("media publication recovery must finish before database restore")
    except RestoreError:
        raise
    except (ConfigurationPathError, OSError, sqlite3.Error) as exc:
        raise RestoreRecoveryRequired("media publication evidence cannot be inspected safely") from exc


def public_restore_status(root: str | os.PathLike[str], operation_id: str) -> dict[str, Any]:
    lifecycle_state = state.inspect_state(root)
    journal = read_restore_journal(root, operation_id)
    operation = lifecycle_state.active_operation
    matches = bool(
        operation
        and operation.operation_id == operation_id
        and operation.operation_type is state.OperationType.RESTORE
        and lifecycle_state.generation == journal["generation"]
    )
    recorded_progress = str(journal["progress"])
    terminal_recorded = recorded_progress in {
        RestoreProgress.SUCCEEDED.value,
        RestoreProgress.ROLLED_BACK.value,
    }
    progress = recorded_progress if matches or terminal_recorded else RestoreProgress.RECOVERY_REQUIRED.value
    return {
        "operationId": operation_id,
        "generation": journal["generation"],
        "phase": progress,
        "lifecyclePhase": operation.phase.value if matches and operation else ("idle" if terminal_recorded else "recovery_required"),
        "accepted": bool(journal["accepted"]),
        "outcome": journal.get("outcome"),
        "errorCode": journal.get("error_code"),
        "terminal": progress in {RestoreProgress.SUCCEEDED.value, RestoreProgress.ROLLED_BACK.value},
        "controllerActive": bool(
            (lease := state.read_controller_lease(root))
            and lease.operation_id == operation_id
            and lease.generation == journal["generation"]
            and state.controller_lease_is_current(lease)
        ),
    }


def _read_pipe_secret(fd: int) -> str:
    chunks = bytearray()
    while len(chunks) <= 256:
        block = os.read(fd, 256 - len(chunks))
        if not block:
            break
        chunks.extend(block)
    os.close(fd)
    try:
        capability = chunks.decode("ascii").strip()
    finally:
        for index in range(len(chunks)):
            chunks[index] = 0
    state.hash_owner_capability(capability)
    return capability


def spawn_restore_controller(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    owner_capability: str,
    timeout: float = 10.0,
) -> RestoreHandoff:
    installation = state.resolve_installation_root(root)
    ack_read, ack_write = os.pipe()
    cap_read, cap_write = os.pipe()
    gate_read, gate_write = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "lifecycle.restore_controller",
                "execute",
                "--root",
                str(installation),
                "--operation-id",
                operation_id,
                "--generation",
                str(generation),
                "--ack-fd",
                str(ack_write),
                "--capability-fd",
                str(cap_read),
                "--gate-fd",
                str(gate_read),
            ],
            cwd=str(installation / "server"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=(ack_write, cap_read, gate_read),
        )
        os.close(ack_write)
        ack_write = -1
        os.close(cap_read)
        cap_read = -1
        os.close(gate_read)
        gate_read = -1
        _write_all(cap_write, (owner_capability + "\n").encode("ascii"))
        os.close(cap_write)
        cap_write = -1
        ready, _, _ = select.select([ack_read], [], [], timeout)
        if not ready:
            raise RestoreConflictError("restore controller ownership was not acknowledged")
        acknowledgement = os.read(ack_read, 4096)
        try:
            payload = json.loads(acknowledgement.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise RestoreConflictError("restore controller acknowledgement is invalid") from exc
        acknowledged_generation = payload.get("generation") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("operation_id") != operation_id
            or acknowledged_generation != generation + 1
            or payload.get("owned") is not True
        ):
            raise RestoreConflictError("restore controller ownership was not proven")
        lease = state.read_controller_lease(installation)
        if (
            lease is None
            or lease.operation_id != operation_id
            or lease.generation != acknowledged_generation
            or lease.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
            or not state.controller_lease_is_current(lease)
        ):
            raise RestoreConflictError("restore controller lease was not established")
        return RestoreHandoff(process, operation_id, int(acknowledged_generation), gate_write)
    except Exception:
        if gate_write >= 0:
            os.close(gate_write)
        if process is not None and process.poll() is None:
            process.terminate()
        raise
    finally:
        for descriptor in (ack_read, ack_write, cap_read, cap_write, gate_read):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _read_env_value(root: Path, name: str, default: str) -> str:
    path = root / ".env"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return default
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if key.strip() == name:
            selected = value.strip()
            if len(selected) >= 2 and selected[0] == selected[-1] and selected[0] in "\"'":
                selected = selected[1:-1]
            return selected or default
    return default


def _web_port(root: Path) -> int:
    try:
        port = int(_read_env_value(root, "WEB_PORT", "3000"))
    except ValueError as exc:
        raise RestoreConflictError("configured web port is invalid") from exc
    if not 1 <= port <= 65535 or port == 8000:
        raise RestoreConflictError("configured web port is invalid")
    return port


def _runtime_control():
    from scratch import runtime_control

    return runtime_control


def _stop_supported_runtime(root: Path, web_port: int) -> None:
    if _runtime_control().stop_runtime(root, [8000, web_port], quiet=True, startup=False) != 0:
        raise RestoreRecoveryRequired("supported runtime shutdown could not be proven")


def _runtime_token() -> str:
    return secrets.token_urlsafe(32)


def _build_id(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "dev"


def _start_maintenance(root: Path, operation_id: str, web_port: int) -> subprocess.Popen[bytes]:
    token = _runtime_token()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(root / "server"),
            "STREAMHOME_INSTANCE_ROOT": str(root),
            "STREAMHOME_INSTANCE_TOKEN": token,
            "STREAMHOME_SERVICE": "maintenance",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(root / "server" / "scratch" / "maintenance_server.py"),
            "--port",
            str(web_port),
            "--root",
            str(root),
            "--operation-type",
            "restore",
            "--operation-id",
            operation_id,
        ],
        cwd=str(root),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RestoreRecoveryRequired("maintenance responder exited during startup")
        if not _runtime_control().port_is_available(web_port):
            return process
        time.sleep(0.05)
    process.terminate()
    raise RestoreRecoveryRequired("maintenance responder did not acquire the web port")


def _start_backend(root: Path, operation_id: str, generation: int) -> tuple[subprocess.Popen[bytes], str]:
    token = _runtime_token()
    build = _build_id(root)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(root / "server"),
            "STREAMHOME_INSTANCE_ROOT": str(root),
            "STREAMHOME_INSTANCE_TOKEN": token,
            "STREAMHOME_SERVICE": "backend",
            "STREAMHOME_BUILD_ID": build,
            "STREAMHOME_LIFECYCLE_STARTUP_MODE": "restore_verify",
            "STREAMHOME_LIFECYCLE_OPERATION_ID": operation_id,
            "STREAMHOME_LIFECYCLE_GENERATION": str(generation),
        }
    )
    log_path = restore_paths(root, operation_id).operation_dir / "runtime.log"
    descriptor = os.open(
        log_path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000", "--no-proxy-headers"],
            cwd=str(root / "server"),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        os.close(descriptor)
    try:
        _runtime_control().record_service(root, "backend", process.pid, token)
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        raise
    return process, build


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, Any]]:
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    selected_headers = {"Content-Type": "application/json", **dict(headers or {})}
    req = urlrequest.Request(url, data=encoded, headers=selected_headers, method=method)
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload if isinstance(payload, dict) else {}
    except urlerror.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        return exc.code, payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return 0, {}


def _wait_guarded_ready(
    root: Path,
    operation_id: str,
    generation: int,
    build: str,
    timeout: float = 90.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status_code, payload = _request_json("http://127.0.0.1:8000/api/health/ready")
        if (
            status_code == 200
            and payload.get("status") == "ready"
            and payload.get("readyKind") == "guarded"
            and payload.get("mode") == "restore_verify"
            and payload.get("buildId") == build
        ):
            current = state.inspect_state(root)
            operation = current.active_operation
            if operation and operation.operation_id == operation_id and current.generation == generation:
                return
        time.sleep(0.2)
    raise RestoreRecoveryRequired("guarded restore verification did not become ready")


def _commit_guarded_runtime(
    operation_id: str,
    generation: int,
    build: str,
    capability: str,
    decision: str,
) -> None:
    code, payload = _request_json(
        "http://127.0.0.1:8000/api/lifecycle/restore/commit",
        method="POST",
        body={
            "operation_id": operation_id,
            "generation": generation,
            "build_id": build,
            "decision": decision,
        },
        headers={"X-StreamHome-Restore-Commit": capability},
    )
    if code != 200 or payload.get("committed") is not True:
        raise RestoreRecoveryRequired("guarded restore commit was rejected")


def _wait_general_ready(build: str, timeout: float = 1800.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, payload = _request_json("http://127.0.0.1:8000/api/health/live")
        if (
            code == 200
            and payload.get("recoverySucceeded") is True
            and payload.get("workersStarted") is True
            and payload.get("buildId") == build
        ):
            return
        if code == 200 and payload.get("recoveryCompleted") and not payload.get("recoverySucceeded"):
            raise RestoreRecoveryRequired("post-restore mandatory recovery failed")
        time.sleep(0.5)
    raise RestoreRecoveryRequired("post-restore mandatory recovery did not complete")


def _start_web(root: Path, web_port: int, build: str) -> subprocess.Popen[bytes]:
    token = _runtime_token()
    environment = os.environ.copy()
    environment.update(
        {
            "NODE_ENV": "production",
            "WEB_PORT": str(web_port),
            "SETUP": _read_env_value(root, "SETUP", "false"),
            "PUBLIC_URL": _read_env_value(root, "PUBLIC_URL", f"http://localhost:{web_port}"),
            "TRUSTED_PROXY_CIDRS": _read_env_value(root, "TRUSTED_PROXY_CIDRS", "127.0.0.1/32,::1/128"),
            "STREAMHOME_BUILD_ID": build,
            "STREAMHOME_INSTANCE_ROOT": str(root),
            "STREAMHOME_INSTANCE_TOKEN": token,
            "STREAMHOME_SERVICE": "web",
        }
    )
    process = subprocess.Popen(
        [str(root / "web" / "node_modules" / ".bin" / "tsx"), str(root / "web" / "server.ts")],
        cwd=str(root / "web"),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _runtime_control().record_service(root, "web", process.pid, token)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RestoreRecoveryRequired("web runtime exited during startup")
            try:
                opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
                with opener.open(
                    f"http://127.0.0.1:{web_port}/api/health/live",
                    timeout=1,
                ) as response:
                    if response.headers.get("X-StreamHome-Web-Build") == build:
                        return process
            except (OSError, urlerror.URLError):
                pass
            time.sleep(0.1)
        raise RestoreRecoveryRequired("web runtime did not become ready with the expected build")
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        raise


def _transition_recovery_required(root: Path, operation_id: str, generation: int) -> bool:
    current = state.inspect_state(root)
    operation = current.active_operation
    if not operation or operation.operation_id != operation_id or current.generation != generation:
        return False
    if operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
        return True
    state.transition_operation_with_credential(
        root,
        operation_id=operation_id,
        generation=generation,
        expected_phase=operation.phase,
        next_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
    )
    return True


def _mark_recovery_required(paths: RestorePaths, generation: int, error_code: str) -> bool:
    try:
        transitioned = _transition_recovery_required(paths.root, paths.operation_id, generation)
    except state.LifecycleError:
        current = state.inspect_state(paths.root)
        operation = current.active_operation
        transitioned = bool(
            operation is not None
            and operation.operation_id == paths.operation_id
            and current.generation == generation
            and operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED
        )
        if not transitioned:
            raise
    if not transitioned:
        return False
    try:
        update_restore_journal(
            paths.root,
            paths.operation_id,
            progress=RestoreProgress.RECOVERY_REQUIRED.value,
            error_code=error_code,
        )
    except RestoreError:
        pass
    return transitioned


def _enter_fail_closed(
    paths: RestorePaths,
    generation: int,
    error_code: str,
    web_port: int,
) -> None:
    """Fence this exact restore and leave only maintenance/status available."""

    if not _mark_recovery_required(paths, generation, error_code):
        return
    try:
        _stop_supported_runtime(paths.root, web_port)
    except Exception:
        pass
    try:
        if _runtime_control().port_is_available(web_port):
            _start_maintenance(paths.root, paths.operation_id, web_port)
    except Exception:
        pass


def _verify_authority(root: Path, operation_id: str, generation: int, capability: str) -> state.LifecycleState:
    current = state.validate_operation_owner_current_phase(
        root,
        operation_id=operation_id,
        generation=generation,
        owner_capability=capability,
    )
    operation = current.active_operation
    if (
        operation is None
        or operation.operation_type is not state.OperationType.RESTORE
        or operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
    ):
        raise RestoreConflictError("restore lifecycle authority does not match")
    return current


def _recover_activation_state(paths: RestorePaths, journal: dict[str, Any]) -> dict[str, Any]:
    activation = ActivationState(journal["activation_state"])
    if activation is ActivationState.STARTED:
        canonical = artifact_identity(paths.database_path)
        staged = (
            (journal.get("staged_size"), journal.get("staged_sha256"))
            if journal.get("staged_sha256")
            else None
        )
        previous = (
            (journal.get("previous_size"), journal.get("previous_sha256"))
            if journal.get("previous_sha256")
            else None
        )
        if staged and canonical == staged:
            journal = update_restore_journal(
                paths.root,
                paths.operation_id,
                progress=RestoreProgress.ACTIVATION_COMPLETED.value,
                activation_state=ActivationState.COMPLETED.value,
            )
        elif previous and canonical == previous and paths.candidate_path.is_file():
            journal = update_restore_journal(
                paths.root,
                paths.operation_id,
                progress=RestoreProgress.CANDIDATE_STAGED.value,
                activation_state=ActivationState.NOT_STARTED.value,
            )
        else:
            raise RestoreRecoveryRequired("activation outcome is ambiguous")
    elif activation is ActivationState.ROLLBACK_STARTED:
        rollback_activation = paths.operation_dir / "rollback-activation.db"
        expected = (
            journal.get("rollback_activation_size"),
            journal.get("rollback_activation_sha256"),
        )
        if rollback_activation.is_file() and artifact_identity(rollback_activation) == expected:
            require_no_media_publication_evidence(paths.root)
            os.replace(rollback_activation, paths.database_path)
            os.chmod(paths.database_path, 0o600)
            _fsync_directory(paths.database_path.parent)
            canonical = expected
        else:
            canonical = artifact_identity(paths.database_path)
        if canonical == expected and expected[1]:
            journal = update_restore_journal(
                paths.root,
                paths.operation_id,
                progress=RestoreProgress.ROLLBACK_ACTIVATED.value,
                activation_state=ActivationState.ROLLBACK_COMPLETED.value,
            )
        else:
            raise RestoreRecoveryRequired("rollback activation outcome is ambiguous")
    return journal


def _complete_automatic_rollback(
    paths: RestorePaths,
    generation: int,
    capability: str,
    web_port: int,
) -> int:
    """Restore and verify the checkpoint before releasing lifecycle authority."""

    root = paths.root
    operation_id = paths.operation_id
    _stop_supported_runtime(root, web_port)
    _start_maintenance(root, operation_id, web_port)
    current = _verify_authority(root, operation_id, generation, capability)
    operation = current.active_operation
    if operation is None:
        raise RestoreRecoveryRequired("restore authority disappeared before rollback")
    operation_phase = operation.phase
    if operation_phase is state.LifecyclePhase.RECOVERY_REQUIRED:
        raise RestoreRecoveryRequired("recovery-required restore cannot roll back automatically")
    if operation_phase is state.LifecyclePhase.RESERVED:
        state.transition_operation_with_credential(
            root,
            operation_id=operation_id,
            generation=generation,
            expected_phase=state.LifecyclePhase.RESERVED,
            next_phase=state.LifecyclePhase.EXCLUSIVE,
        )
        operation_phase = state.LifecyclePhase.EXCLUSIVE
    if operation_phase is state.LifecyclePhase.EXCLUSIVE:
        state.transition_operation_with_credential(
            root,
            operation_id=operation_id,
            generation=generation,
            expected_phase=state.LifecyclePhase.EXCLUSIVE,
            next_phase=state.LifecyclePhase.VERIFYING,
        )
    elif operation_phase is not state.LifecyclePhase.VERIFYING:
        raise RestoreRecoveryRequired("restore phase cannot enter automatic rollback")

    journal = read_restore_journal(root, operation_id)
    _verify_authority(root, operation_id, generation, capability)
    if journal["activation_state"] != ActivationState.ROLLBACK_COMPLETED.value:
        restore_rollback_checkpoint(paths, journal)
        journal = read_restore_journal(root, operation_id)
    elif not installed_database_is_recognized(paths, journal):
        raise RestoreRecoveryRequired("activated rollback continuity is ambiguous")
    if journal["progress"] != RestoreProgress.VERIFYING_ROLLBACK.value:
        update_restore_journal(
            root, operation_id, progress=RestoreProgress.VERIFYING_ROLLBACK.value
        )
    _start_backend(root, operation_id, generation)
    build = _build_id(root)
    _wait_guarded_ready(root, operation_id, generation, build)
    _commit_guarded_runtime(operation_id, generation, build, capability, "rollback")
    update_restore_journal(
        root,
        operation_id,
        accepted=True,
        outcome="rolled_back",
        progress=RestoreProgress.NORMAL_RECOVERY.value,
    )
    _wait_general_ready(build)
    if not _runtime_control().terminate_owned_service(root, "maintenance"):
        raise RestoreRecoveryRequired("maintenance responder could not be stopped")
    _start_web(root, web_port, build)
    _verify_authority(root, operation_id, generation, capability)
    record_restore_completion_intent(paths, generation=generation, outcome="rolled_back")
    result = state.complete_operation_with_credential(
        root,
        operation_id=operation_id,
        generation=generation,
        expected_phase=state.LifecyclePhase.VERIFYING,
    )
    update_restore_journal(
        root,
        operation_id,
        progress=RestoreProgress.ROLLED_BACK.value,
        outcome="rolled_back",
        error_code=result.credential_cleanup_error,
    )
    return 0


def _run_controller(
    root: Path,
    operation_id: str,
    generation: int,
    capability: str,
    *,
    acknowledgement_fd: int | None,
    gate_fd: int | None,
    operator_action: str | None = None,
    operator_journal_generation: int | None = None,
    pre_transfer_check: Callable[[], None] | None = None,
    on_transfer: Callable[[int], None] | None = None,
) -> int:
    paths = restore_paths(root, operation_id)
    state.qualify_lifecycle_filesystem(root)
    state.qualify_local_filesystem(
        paths.database_path.parent,
        require_private=False,
        description="canonical database directory",
    )
    current = _verify_authority(root, operation_id, generation, capability)
    expected_phase = current.active_operation.phase if current.active_operation else state.LifecyclePhase.RESERVED
    if expected_phase is state.LifecyclePhase.RECOVERY_REQUIRED and operator_action is None:
        raise RestoreRecoveryRequired("recovery-required restore needs explicit operator action")
    if operator_action not in {None, "safe_abort", "continue_recovery", "rollback"}:
        raise RestoreConflictError("operator restore action is unsupported")
    if operator_action is not None and expected_phase is not state.LifecyclePhase.RECOVERY_REQUIRED:
        raise RestoreConflictError("operator restore action requires recovery-required authority")
    lease, transferred = state.acquire_transferred_controller_lease(
        root,
        operation_id=operation_id,
        generation=generation,
        expected_phase=expected_phase,
        owner_capability=capability,
        next_owner_kind=state.OwnerKind.RESTORE_CONTROLLER,
        pre_transfer_check=pre_transfer_check,
    )
    previous_generation = generation
    generation = transferred.state.generation
    capability = transferred.owner_capability
    with lease:
        try:
            journal = transfer_restore_journal_generation(
                root,
                operation_id,
                expected_generation=previous_generation,
                next_generation=generation,
                journal_generation=operator_journal_generation,
            )
        except Exception:
            _mark_recovery_required(paths, generation, "restore_handoff_persistence_failed")
            raise
        if on_transfer is not None:
            on_transfer(generation)
        if operator_action is not None:
            journal = resume_restore_journal_for_operator(
                root,
                operation_id,
                generation=generation,
                action=operator_action,
            )
        if (
            RestoreProgress(journal["progress"]) is RestoreProgress.RECOVERY_REQUIRED
            and operator_action is None
        ):
            _mark_recovery_required(paths, generation, str(journal["error_code"]))
            return 1
        if RestoreProgress(journal["progress"]) in {
            RestoreProgress.CLAIMED,
            RestoreProgress.DRAINED,
        }:
            journal = update_restore_journal(
                root, operation_id, progress=RestoreProgress.CONTROLLER_OWNED.value
            )
        if acknowledgement_fd is not None:
            _write_all(
                acknowledgement_fd,
                json.dumps(
                    {"owned": True, "operation_id": operation_id, "generation": generation},
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            os.close(acknowledgement_fd)
        if gate_fd is not None:
            try:
                _await_response_completion(gate_fd)
            except RestoreResponseGateError:
                _mark_recovery_required(paths, generation, "restore_response_not_confirmed")
                return 1

        web_port = _web_port(root)
        maintenance: subprocess.Popen[bytes] | None = None
        try:
            current = _verify_authority(root, operation_id, generation, capability)
            phase = current.active_operation.phase if current.active_operation else None
            if phase is state.LifecyclePhase.RECOVERY_REQUIRED and operator_action is None:
                raise RestoreRecoveryRequired("recovery-required restore needs explicit operator action")
            if phase is state.LifecyclePhase.RECOVERY_REQUIRED and operator_action == "safe_abort":
                if (
                    journal["accepted"]
                    or ActivationState(journal["activation_state"])
                    is not ActivationState.NOT_STARTED
                    or journal.get("staged_sha256")
                    or journal.get("rollback_sha256")
                    or paths.candidate_path.exists()
                    or paths.candidate_path.is_symlink()
                    or paths.rollback_path.exists()
                    or paths.rollback_path.is_symlink()
                ):
                    raise RestoreConflictError("restore abort evidence is no longer safe")
                state.complete_operation_with_credential(
                    root,
                    operation_id=operation_id,
                    generation=generation,
                    expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
                    expected_operation_type=state.OperationType.RESTORE,
                )
                return 0
            if phase is state.LifecyclePhase.RECOVERY_REQUIRED and operator_action is not None:
                state.transition_operation_with_credential(
                    root,
                    operation_id=operation_id,
                    generation=generation,
                    expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
                    next_phase=state.LifecyclePhase.EXCLUSIVE,
                )
                phase = state.LifecyclePhase.EXCLUSIVE
            if operator_action == "rollback":
                try:
                    return _complete_automatic_rollback(
                        paths, generation, capability, web_port
                    )
                except Exception:
                    _enter_fail_closed(
                        paths, generation, "restore_rollback_failed", web_port
                    )
                    return 1
            elif phase is state.LifecyclePhase.RESERVED:
                state.transition_operation_with_credential(
                    root,
                    operation_id=operation_id,
                    generation=generation,
                    expected_phase=state.LifecyclePhase.RESERVED,
                    next_phase=state.LifecyclePhase.EXCLUSIVE,
                )

            journal = _recover_activation_state(paths, journal)
            accepted = bool(journal["accepted"])
            accepted_on_entry = accepted
            progress = RestoreProgress(journal["progress"])
            if (
                journal["activation_state"] in {
                    ActivationState.COMPLETED.value,
                    ActivationState.ROLLBACK_COMPLETED.value,
                }
                and not installed_database_is_recognized(paths, journal)
            ):
                raise RestoreRecoveryRequired("installed restore database continuity is ambiguous")

            if not accepted and (
                ActivationState(journal["activation_state"])
                in {ActivationState.ROLLBACK_STARTED, ActivationState.ROLLBACK_COMPLETED}
                or progress
                in {
                    RestoreProgress.ROLLBACK_STARTED,
                    RestoreProgress.ROLLBACK_ACTIVATED,
                    RestoreProgress.VERIFYING_ROLLBACK,
                }
            ):
                try:
                    return _complete_automatic_rollback(
                        paths, generation, capability, web_port
                    )
                except Exception:
                    _enter_fail_closed(
                        paths, generation, "restore_rollback_failed", web_port
                    )
                    return 1

            if not accepted:
                _verify_authority(root, operation_id, generation, capability)
                _stop_supported_runtime(root, web_port)
                journal = update_restore_journal(
                    root, operation_id, progress=RestoreProgress.RUNTIME_STOPPED.value
                )
                maintenance = _start_maintenance(root, operation_id, web_port)

                if not journal.get("rollback_sha256"):
                    _verify_authority(root, operation_id, generation, capability)
                    rollback_size, rollback_hash, previous_size, previous_hash = create_rollback_checkpoint(paths)
                    journal = update_restore_journal(
                        root,
                        operation_id,
                        progress=RestoreProgress.CHECKPOINT_READY.value,
                        rollback_size=rollback_size,
                        rollback_sha256=rollback_hash,
                        previous_size=previous_size,
                        previous_sha256=previous_hash,
                    )
                if not journal.get("staged_sha256") and ActivationState(journal["activation_state"]) is ActivationState.NOT_STARTED:
                    _verify_authority(root, operation_id, generation, capability)
                    staged_size, staged_hash = stage_candidate(paths, journal)
                    journal = update_restore_journal(
                        root,
                        operation_id,
                        progress=RestoreProgress.CANDIDATE_STAGED.value,
                        staged_size=staged_size,
                        staged_sha256=staged_hash,
                    )
                if ActivationState(journal["activation_state"]) is ActivationState.NOT_STARTED:
                    _verify_authority(root, operation_id, generation, capability)
                    activate_staged_candidate(paths, journal)
                    journal = read_restore_journal(root, operation_id)

                current = _verify_authority(root, operation_id, generation, capability)
                if current.active_operation and current.active_operation.phase is state.LifecyclePhase.EXCLUSIVE:
                    state.transition_operation_with_credential(
                        root,
                        operation_id=operation_id,
                        generation=generation,
                        expected_phase=state.LifecyclePhase.EXCLUSIVE,
                        next_phase=state.LifecyclePhase.VERIFYING,
                    )
                update_restore_journal(
                    root, operation_id, progress=RestoreProgress.VERIFYING_CANDIDATE.value
                )
                try:
                    _start_backend(root, operation_id, generation)
                    build = _build_id(root)
                    _wait_guarded_ready(root, operation_id, generation, build)
                    _commit_guarded_runtime(operation_id, generation, build, capability, "accept")
                    journal = update_restore_journal(
                        root,
                        operation_id,
                        progress=RestoreProgress.ACCEPTED.value,
                        accepted=True,
                        outcome="accepted",
                    )
                except Exception:
                    if read_restore_journal(root, operation_id)["accepted"]:
                        raise RestoreRecoveryRequired(
                            "restore acceptance was persisted but its response was interrupted"
                        )
                    return _complete_automatic_rollback(
                        paths, generation, capability, web_port
                    )

            # Once accepted, rollback is forbidden. Resume or complete normal recovery.
            build = _build_id(root)
            accepted_decision = "rollback" if journal.get("outcome") == "rolled_back" else "accept"
            accepted_phase = state.LifecyclePhase.VERIFYING
            code, live = _request_json("http://127.0.0.1:8000/api/health/live")
            if accepted_on_entry or code != 200 or live.get("mode") != "restore_verify":
                _verify_authority(root, operation_id, generation, capability)
                _stop_supported_runtime(root, web_port)
                maintenance = _start_maintenance(root, operation_id, web_port)
                current = _verify_authority(root, operation_id, generation, capability)
                if (
                    current.active_operation
                    and current.active_operation.phase is state.LifecyclePhase.EXCLUSIVE
                ):
                    state.transition_operation_with_credential(
                        root,
                        operation_id=operation_id,
                        generation=generation,
                        expected_phase=state.LifecyclePhase.EXCLUSIVE,
                        next_phase=state.LifecyclePhase.VERIFYING,
                    )
                _start_backend(root, operation_id, generation)
                _wait_guarded_ready(root, operation_id, generation, build)
                _commit_guarded_runtime(
                    operation_id, generation, build, capability, accepted_decision
                )
            update_restore_journal(
                root, operation_id, progress=RestoreProgress.NORMAL_RECOVERY.value
            )
            _wait_general_ready(build)
            _verify_authority(root, operation_id, generation, capability)
            if not _runtime_control().terminate_owned_service(root, "maintenance"):
                raise RestoreRecoveryRequired("maintenance responder could not be stopped")
            maintenance = None
            if _runtime_control().port_is_available(web_port):
                _start_web(root, web_port, build)
            _verify_authority(root, operation_id, generation, capability)
            record_restore_completion_intent(
                paths,
                generation=generation,
                outcome=("rolled_back" if accepted_decision == "rollback" else "succeeded"),
            )
            result = state.complete_operation_with_credential(
                root,
                operation_id=operation_id,
                generation=generation,
                expected_phase=accepted_phase,
            )
            try:
                update_restore_journal(
                    root,
                    operation_id,
                    progress=(
                        RestoreProgress.ROLLED_BACK.value
                        if accepted_decision == "rollback"
                        else RestoreProgress.SUCCEEDED.value
                    ),
                    outcome="rolled_back" if accepted_decision == "rollback" else "succeeded",
                    error_code=result.credential_cleanup_error,
                )
            except RestoreError:
                # Lifecycle completion is authoritative; a terminal journal write
                # failure cannot resurrect or roll back the completed operation.
                pass
            return 0
        except Exception as exc:
            try:
                interrupted = read_restore_journal(root, operation_id)
                if not interrupted["accepted"] and interrupted.get("rollback_sha256"):
                    try:
                        return _complete_automatic_rollback(
                            paths, generation, capability, web_port
                        )
                    except Exception:
                        _enter_fail_closed(
                            paths, generation, "restore_rollback_failed", web_port
                        )
                        return 1
            except Exception:
                pass
            _enter_fail_closed(
                paths,
                generation,
                str(getattr(exc, "code", "restore_controller_failed")),
                web_port,
            )
            return 1
        finally:
            if maintenance is not None and maintenance.poll() is None:
                # Keep maintenance available on unresolved failure; successful
                # paths explicitly stop it before starting the web runtime.
                status = read_restore_journal(root, operation_id)
                if status["progress"] in {
                    RestoreProgress.SUCCEEDED.value,
                    RestoreProgress.ROLLED_BACK.value,
                }:
                    maintenance.terminate()


def run_operator_recovery(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    owner_capability: str,
    action: str,
    journal_generation: int | None,
    pre_transfer_check: Callable[[], None],
    on_transfer: Callable[[int], None],
) -> int:
    """Execute one freshly authorized operator action through the restore controller."""

    return _run_controller(
        state.resolve_installation_root(root),
        operation_id,
        generation,
        owner_capability,
        acknowledgement_fd=None,
        gate_fd=None,
        operator_action=action,
        operator_journal_generation=journal_generation,
        pre_transfer_check=pre_transfer_check,
        on_transfer=on_transfer,
    )


RECOVERY_RESERVATION_ABORTED = 10


def _resolve_abandoned_prejournal_claim(
    installation: Path,
    operation_id: str,
    generation: int,
    capability: str,
) -> bool:
    """Release a proven pre-destructive reservation left by a dead backend."""

    paths = restore_paths(installation, operation_id)
    if paths.journal_path.exists() or paths.journal_path.is_symlink():
        return False
    if paths.operation_dir.exists():
        try:
            unexpected = {
                entry.name for entry in paths.operation_dir.iterdir()
                if entry.name != paths.journal_lock_path.name
            }
        except OSError:
            return False
        if unexpected:
            return False
    owned = _runtime_control().discover_owned(
        _runtime_control().process_snapshot(), installation
    )
    if "backend" in owned.values():
        return False
    try:
        with state.acquire_controller_lease(
            installation,
            operation_id=operation_id,
            generation=generation,
            expected_phase=state.LifecyclePhase.RESERVED,
            owner_capability=capability,
        ):
            if paths.journal_path.exists() or paths.journal_path.is_symlink():
                return False
            state.complete_operation_with_credential(
                installation,
                operation_id=operation_id,
                generation=generation,
                expected_phase=state.LifecyclePhase.RESERVED,
            )
            return True
    except state.LifecycleError:
        return False


def recover_restore(root: str | os.PathLike[str]) -> int:
    installation = state.resolve_installation_root(root)
    lifecycle_state = state.inspect_state(installation)
    operation = lifecycle_state.active_operation
    if operation is None or operation.operation_type is not state.OperationType.RESTORE:
        return 2
    from . import recovery as operator_recovery

    if operator_recovery.operator_action_blocks_automatic_recovery(
        installation, operation.operation_id, lifecycle_state.generation
    ):
        operator_recovery.normalize_interrupted_operator(
            installation, operation.operation_id, lifecycle_state.generation
        )
        return 3
    if operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
        return 3
    capability = state.load_controller_capability(
        installation,
        operation_id=operation.operation_id,
        generation=lifecycle_state.generation,
    )
    paths = restore_paths(installation, operation.operation_id)
    if not paths.journal_path.exists() and not paths.journal_path.is_symlink():
        if _resolve_abandoned_prejournal_claim(
            installation,
            operation.operation_id,
            lifecycle_state.generation,
            capability,
        ):
            return RECOVERY_RESERVATION_ABORTED
        return 1
    try:
        journal = read_restore_journal(installation, operation.operation_id)
    except RestoreError:
        return 1
    if RestoreProgress(journal["progress"]) is RestoreProgress.RECOVERY_REQUIRED:
        return 3
    if RestoreProgress(journal["progress"]) in {
        RestoreProgress.CLAIMED,
        RestoreProgress.DRAINED,
    }:
        owned = _runtime_control().discover_owned(
            _runtime_control().process_snapshot(), installation
        )
        if "backend" in owned.values():
            return 1
    lease = state.read_controller_lease(installation)
    if (
        lease
        and lease.operation_id == operation.operation_id
        and lease.generation == lifecycle_state.generation
        and state.controller_lease_is_current(lease)
    ):
        return 0
    try:
        handoff = spawn_restore_controller(
            installation,
            operation_id=operation.operation_id,
            generation=lifecycle_state.generation,
            owner_capability=capability,
        )
        handoff.release_after_response()
        return 0
    except (RestoreError, state.LifecycleError, OSError):
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamhome-restore-controller")
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("--root", required=True)
    execute.add_argument("--operation-id", required=True)
    execute.add_argument("--generation", required=True, type=int)
    execute.add_argument("--ack-fd", required=True, type=int)
    execute.add_argument("--capability-fd", required=True, type=int)
    execute.add_argument("--gate-fd", required=True, type=int)
    recover = commands.add_parser("recover")
    recover.add_argument("--root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "recover":
            return recover_restore(arguments.root)
        if os.name == "posix":
            child = os.fork()
            if child > 0:
                return 0
            os.setsid()
        root = state.resolve_installation_root(arguments.root)
        capability = _read_pipe_secret(arguments.capability_fd)
        return _run_controller(
            root,
            arguments.operation_id,
            arguments.generation,
            capability,
            acknowledgement_fd=arguments.ack_fd,
            gate_fd=arguments.gate_fd,
        )
    except (RestoreError, state.LifecycleError, OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
