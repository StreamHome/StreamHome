"""Fenced launcher for the existing managed-update controller.

This module owns only lifecycle handoff/takeover and the private cutover permit.
The existing ``update.sh`` controller remains responsible for update preflight,
activation, guarded verification, rollback, and its detailed journal.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from . import state
from .layout import layout_for_root


ACK_TIMEOUT_SECONDS = 15.0
MAX_UPDATE_JOURNAL_BYTES = 64 * 1024
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
INSTALL_MODES = frozenset({"automatic", "when_idle", "now"})
UPDATE_DETAIL_PHASES = frozenset(
    {
        "queued",
        "update_available",
        "preflight",
        "waiting_for_idle",
        "stopping",
        "installing",
        "starting",
        "rolling_back",
        "recovering",
        "succeeded",
        "failed",
        "rolled_back",
        "rollback_failed",
    }
)
DETAIL_PHASE_TRANSITIONS = {
    "queued": frozenset({"queued", "preflight", "failed", "update_available"}),
    "update_available": frozenset({"update_available"}),
    "preflight": frozenset(
        {"preflight", "waiting_for_idle", "failed", "update_available", "rolled_back"}
    ),
    "waiting_for_idle": frozenset(
        {"waiting_for_idle", "stopping", "failed", "update_available", "rolled_back"}
    ),
    "stopping": frozenset({"stopping", "installing", "rolling_back", "recovering", "failed"}),
    "installing": frozenset({"installing", "starting", "rolling_back", "recovering", "rollback_failed"}),
    "starting": frozenset({"starting", "recovering", "rolling_back", "rollback_failed"}),
    "rolling_back": frozenset({"rolling_back", "recovering", "rolled_back", "rollback_failed"}),
    "recovering": frozenset({"recovering", "succeeded", "rolled_back", "rollback_failed"}),
    "succeeded": frozenset({"succeeded", "recovering"}),
    "rolled_back": frozenset({"rolled_back", "recovering"}),
    "failed": frozenset({"failed", "recovering"}),
    "rollback_failed": frozenset({"rollback_failed"}),
}
DETAIL_PHASES_BY_LIFECYCLE = {
    state.LifecyclePhase.RESERVED: frozenset(
        {
            "queued",
            "preflight",
            "waiting_for_idle",
            "failed",
            "update_available",
            "rolled_back",
        }
    ),
    state.LifecyclePhase.EXCLUSIVE: frozenset(
        {"waiting_for_idle", "stopping", "installing", "starting", "rolling_back", "recovering", "rolled_back"}
    ),
    state.LifecyclePhase.VERIFYING: frozenset(
        {"starting", "rolling_back", "recovering", "succeeded", "failed", "rolled_back"}
    ),
    state.LifecyclePhase.RECOVERY_REQUIRED: frozenset(
        {
            "queued",
            "preflight",
            "waiting_for_idle",
            "stopping",
            "installing",
            "starting",
            "rolling_back",
            "recovering",
            "failed",
            "rollback_failed",
        }
    ),
}
JOURNAL_MUTABLE_FIELDS = frozenset(
    {
        "phase",
        "message",
        "error",
        "failed_target",
        "current_commit",
        "target_commit",
        "automatic",
        "previous_commit",
        "update_available",
        "staging_dir",
        "web_artifacts_swapped",
        "web_dependencies_swapped",
        "python_dependencies_changed",
        "web_dependencies_changed",
        "web_build_required",
        "controller_pid",
        "controller_start_ticks",
        "started_at",
        "finished_at",
        "queued_at",
        "last_success_at",
        "recovery_requested_at",
        "diagnostic_id",
        "runtime_committed",
        "runtime_committed_at",
        "lifecycle_phase",
    }
)


class UpdateControllerError(RuntimeError):
    code = "update_controller_error"


class UpdateControllerConflict(UpdateControllerError):
    code = "update_controller_conflict"


class UpdateBaselineConflict(UpdateControllerConflict):
    code = "update_installed_baseline_conflict"


class UpdateControllerPersistenceError(UpdateControllerError):
    code = "update_controller_persistence_error"


@dataclass(frozen=True, slots=True)
class UpdateControllerHandoff:
    process: subprocess.Popen[bytes]
    operation_id: str
    generation: int


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("update controller write made no progress")
        remaining = remaining[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    replaced = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        replaced = True
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise UpdateControllerPersistenceError("update controller state could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _update_paths(root: str | os.PathLike[str], operation_id: str, generation: int) -> tuple[Path, Path, Path]:
    installation = state.resolve_installation_root(root)
    canonical_id = str(uuid.UUID(operation_id))
    if canonical_id != operation_id:
        raise ValueError("operation_id must be a canonical UUID")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ValueError("generation must be positive")
    run_dir = layout_for_root(installation).durable_runtime
    return (
        run_dir / "update-state.json",
        run_dir / "update-state.lock",
        state.lifecycle_paths(installation).lifecycle_dir
        / f"update-cutover.{canonical_id}.{generation}.permit",
    )


def _read_update_journal(path: Path) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise UpdateControllerPersistenceError(
                    "update journal contains duplicate fields"
                )
            payload[key] = value
        return payload

    def reject_nonfinite(_value: str) -> None:
        raise UpdateControllerPersistenceError(
            "update journal contains a non-finite number"
        )

    try:
        file_state = path.lstat()
        if (
            not stat.S_ISREG(file_state.st_mode)
            or path.is_symlink()
            or file_state.st_size > MAX_UPDATE_JOURNAL_BYTES
        ):
            raise UpdateControllerPersistenceError("update journal is not a trusted file")
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_nonfinite,
        )
    except UpdateControllerError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise UpdateControllerPersistenceError("update journal is unreadable") from exc
    if not isinstance(payload, dict):
        raise UpdateControllerPersistenceError("update journal must be an object")
    return payload


def read_update_journal(path: Path) -> dict[str, Any]:
    """Strictly decode one authority-sensitive update journal."""

    return _read_update_journal(path)


def terminal_update_journal_settled(root: Path) -> bool:
    """Recognize a completed installed release without reacquiring its retired owner.

    False means a known nonterminal phase still needs ordinary recovery. Invalid
    terminal evidence raises instead of falling through to legacy shell parsing.
    This probe never changes the checkout, journal or lifecycle authority.
    """
    root = state.resolve_installation_root(root)
    journal_path = layout_for_root(root).durable_runtime / "update-state.json"
    payload = read_update_journal(journal_path)
    phase = payload.get("phase")
    if not isinstance(phase, str) or phase not in {"succeeded", "rolled_back"}:
        if isinstance(phase, str) and phase in (set(DETAIL_PHASE_TRANSITIONS) | {"idle", "checking", "up_to_date"}):
            return False
        raise UpdateControllerConflict("update journal phase is invalid")

    def require(condition: bool) -> None:
        if not condition:
            raise UpdateControllerConflict("terminal update evidence is inconsistent")

    def timestamp(value: Any) -> bool:
        try:
            return type(value) in {int, float} and math.isfinite(value) and value > 0
        except OverflowError:
            return False

    operation_id = payload.get("transaction_id")
    try:
        require(isinstance(operation_id, str) and str(uuid.UUID(operation_id)) == operation_id)
    except (ValueError, AttributeError) as exc:
        raise UpdateControllerConflict("terminal update identity is invalid") from exc
    for key in ("current_commit", "target_commit", "previous_commit"):
        require(isinstance(payload.get(key), str) and COMMIT_RE.fullmatch(payload[key]) is not None)
    current = payload["current_commit"]
    require(current in {payload["target_commit"], payload["previous_commit"]})
    finished = payload.get("finished_at")
    require(timestamp(finished))
    committed = payload.get("runtime_committed")
    committed_at = payload.get("runtime_committed_at")
    require(type(committed) is bool)
    if committed:
        require(timestamp(committed_at) and committed_at <= finished)
    else:
        require(committed_at is None)
    if phase == "succeeded":
        # Successful recovery may have accepted the previous release after a
        # precommit rollback; current_commit identifies the accepted runtime.
        require(committed is True)
        require(payload.get("error") in (None, "") and payload.get("failed_target") in (None, ""))
    else:
        require(current == payload["previous_commit"])
        require(payload.get("failed_target") == payload["target_commit"])

    authority = state.inspect_state(root)
    require(authority.active_operation is None)
    generation = payload.get("lifecycle_generation")
    lifecycle_phase = payload.get("lifecycle_phase")
    if generation is None:
        require(authority.generation == 0 and lifecycle_phase in (None, ""))
    else:
        require(type(generation) is int and generation > 0 and generation == authority.generation)
        require(isinstance(lifecycle_phase, str) and lifecycle_phase in {"reserved", "exclusive", "verifying"})
        if committed:
            require(lifecycle_phase == "verifying")
    lease = state.read_controller_lease(root)
    if lease is not None:
        require(lease.operation_id == operation_id and lease.generation == generation)
        require(not state.controller_lease_is_current(lease))
    lock_path = state.lifecycle_paths(root).controller_lock_path
    try:
        lock_state = lock_path.lstat()
    except FileNotFoundError:
        lock_state = None
    if lock_state is not None:
        require(stat.S_ISREG(lock_state.st_mode) and not stat.S_IMODE(lock_state.st_mode) & 0o077)
        require(not hasattr(os, "geteuid") or lock_state.st_uid == os.geteuid())
        descriptor = os.open(lock_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            opened = os.fstat(descriptor)
            require((opened.st_dev, opened.st_ino) == (lock_state.st_dev, lock_state.st_ino))
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    validate_pre_cutover_baseline(root, current)
    require(read_update_journal(journal_path) == payload and state.inspect_state(root) == authority)
    return True


def pre_cutover_previous_commit(root: Path, payload: Mapping[str, Any], authority: state.LifecycleState) -> str | None:
    """Recognize the old blank queued identity only with unchanged Git evidence."""
    previous = payload.get("previous_commit")
    if isinstance(previous, str) and COMMIT_RE.fullmatch(previous):
        return previous
    operation = authority.active_operation
    if ((previous is not None and previous != "") or operation is None
            or operation.operation_type is not state.OperationType.UPDATE
            or operation.phase not in {state.LifecyclePhase.RESERVED, state.LifecyclePhase.RECOVERY_REQUIRED}
            or payload.get("transaction_id") != operation.operation_id
            or payload.get("phase") not in {"queued", "preflight", "waiting_for_idle", "failed"}
            or payload.get("runtime_committed") is not False
            or payload.get("staging_dir") or payload.get("web_artifacts_swapped")
            or payload.get("web_dependencies_swapped") or payload.get("controller_pid")
            or any((layout_for_root(root).durable_runtime / name).exists() or (layout_for_root(root).durable_runtime / name).is_symlink()
                for name in ("pre-update-database.db", "update-transaction.json"))):
        return None
    lease = state.read_controller_lease(root)
    if lease is not None and state.controller_lease_is_current(lease):
        return None
    recorded = payload.get("current_commit")
    if not isinstance(recorded, str) or not COMMIT_RE.fullmatch(recorded):
        return None
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return recorded if result.returncode == 0 and result.stdout.strip() == recorded else None


def validate_pre_cutover_baseline(root: Path, previous_commit: str) -> None:
    """Require the producer's installed HEAD before any update cutover effects."""
    if not isinstance(previous_commit, str) or not COMMIT_RE.fullmatch(previous_commit):
        raise UpdateBaselineConflict("update installed baseline is invalid")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateBaselineConflict("update installed baseline is unavailable") from exc
    if result.returncode != 0 or result.stdout.strip() != previous_commit:
        raise UpdateBaselineConflict("update installed baseline changed or is unavailable")


def _validate_update_journal_semantics(
    payload: Mapping[str, Any],
    *,
    authority: state.LifecycleState,
) -> None:
    operation = authority.active_operation
    transaction_id = payload.get("transaction_id")
    generation = payload.get("lifecycle_generation")
    lifecycle_phase_raw = payload.get("lifecycle_phase")
    detail_phase = payload.get("phase")
    runtime_committed = payload.get("runtime_committed", False)
    try:
        canonical_id = str(uuid.UUID(str(transaction_id)))
        lifecycle_phase = state.LifecyclePhase(str(lifecycle_phase_raw))
    except (ValueError, TypeError) as exc:
        raise UpdateControllerConflict("update journal lifecycle identity is invalid") from exc
    if (
        operation is None
        or operation.operation_type is not state.OperationType.UPDATE
        or canonical_id != transaction_id
        or operation.operation_id != transaction_id
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation != authority.generation
        or lifecycle_phase is not operation.phase
        or detail_phase not in UPDATE_DETAIL_PHASES
        or not isinstance(runtime_committed, bool)
    ):
        raise UpdateControllerConflict("update journal disagrees with lifecycle authority")
    if detail_phase not in DETAIL_PHASES_BY_LIFECYCLE[operation.phase]:
        raise UpdateControllerConflict("update journal phase is incompatible with lifecycle authority")
    target = payload.get("target_commit")
    previous = payload.get("previous_commit")
    if not isinstance(target, str) or not COMMIT_RE.fullmatch(target):
        raise UpdateControllerConflict("update target identity is invalid")
    if detail_phase in {
        "stopping",
        "installing",
        "starting",
        "rolling_back",
        "recovering",
        "succeeded",
        "rolled_back",
        "rollback_failed",
    } and (
        not isinstance(previous, str) or not COMMIT_RE.fullmatch(previous)
    ):
        raise UpdateControllerConflict("update rollback identity is invalid")
    if runtime_committed:
        if operation.phase not in {
            state.LifecyclePhase.VERIFYING,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        } or detail_phase not in {"starting", "recovering", "succeeded", "failed", "rolled_back"}:
            raise UpdateControllerConflict("committed update journal state is impossible")
        committed_at = payload.get("runtime_committed_at")
        if isinstance(committed_at, bool) or not isinstance(committed_at, (int, float)):
            raise UpdateControllerConflict("committed update timestamp is invalid")
    elif detail_phase == "succeeded":
        raise UpdateControllerConflict("successful update journal is missing its commit boundary")
    if detail_phase in {"rolling_back", "rollback_failed"}:
        if runtime_committed:
            raise UpdateControllerConflict("committed update cannot enter rollback")
    if detail_phase in {"rolling_back", "rolled_back", "rollback_failed"}:
        failed_target = payload.get("failed_target")
        if not isinstance(failed_target, str) or not COMMIT_RE.fullmatch(failed_target):
            raise UpdateControllerConflict("rollback journal is missing target identity")


def mutate_update_journal(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    expected_lifecycle_phase: state.LifecyclePhase | str,
    changes: Mapping[str, Any],
    expected_detail_phases: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Apply one exact authority-bound update-journal mutation.

    The detailed journal remains subordinate to lifecycle state.  This function
    deliberately refuses unreadable state and preserves the accepted-runtime
    boundary monotonically.
    """

    unexpected = set(changes) - JOURNAL_MUTABLE_FIELDS
    if unexpected:
        raise ValueError("update journal mutation contains unsupported fields")
    expected_lifecycle = state.LifecyclePhase(expected_lifecycle_phase)
    journal_path, lock_path, _ = _update_paths(root, operation_id, generation)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        payload = _read_update_journal(journal_path)
        journal_generation = payload.get("lifecycle_generation")
        current_detail_phase = payload.get("phase")
        if (
            payload.get("transaction_id") != operation_id
            or isinstance(journal_generation, bool)
            or journal_generation != generation
            or current_detail_phase not in UPDATE_DETAIL_PHASES
            or (
                expected_detail_phases is not None
                and current_detail_phase not in expected_detail_phases
            )
        ):
            raise UpdateControllerConflict("update journal identity or phase does not match")
        with state.hold_operation_with_credential(
            root,
            operation_id=operation_id,
            generation=generation,
            expected_phase=expected_lifecycle,
            expected_operation_type=state.OperationType.UPDATE,
        ) as authority:
            operation = authority.active_operation
            if operation is None or operation.owner_kind is not state.OwnerKind.UPDATER:
                raise UpdateControllerConflict("update lifecycle authority does not match")
            next_detail_phase = changes.get("phase", current_detail_phase)
            if (
                not isinstance(next_detail_phase, str)
                or next_detail_phase not in DETAIL_PHASE_TRANSITIONS[current_detail_phase]
            ):
                raise UpdateControllerConflict("update journal transition is not allowed")
            updated = dict(payload)
            updated.update(changes)
            updated["transaction_id"] = operation_id
            updated["lifecycle_generation"] = generation
            updated["lifecycle_phase"] = expected_lifecycle.value
            if payload.get("runtime_committed") is True:
                if changes.get("runtime_committed") is False:
                    raise UpdateControllerConflict("runtime_committed cannot be reverted")
                updated["runtime_committed"] = True
                updated["runtime_committed_at"] = payload.get("runtime_committed_at")
            now = time.time()
            if next_detail_phase in {
                "preflight",
                "waiting_for_idle",
                "stopping",
                "installing",
                "starting",
                "rolling_back",
                "recovering",
            }:
                updated["started_at"] = updated.get("started_at") or now
            if next_detail_phase in {"succeeded", "failed", "rolled_back", "rollback_failed"}:
                updated["finished_at"] = now
            if next_detail_phase == "succeeded":
                updated["last_success_at"] = now
                updated["queued_at"] = None
            updated["updated_at"] = now
            _validate_update_journal_semantics(updated, authority=authority)
            _private_atomic_write(
                journal_path,
                (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            return updated
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _record_transferred_generation(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    previous_generation: int,
    generation: int,
    lifecycle_phase: state.LifecyclePhase,
    target_commit: str,
    previous_commit: str,
) -> None:
    journal_path, lock_path, _ = _update_paths(root, operation_id, generation)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        payload = _read_update_journal(journal_path)
        journal_generation = payload.get("lifecycle_generation")
        if (
            payload.get("transaction_id") != operation_id
            or isinstance(journal_generation, bool)
            or not isinstance(journal_generation, int)
            or journal_generation <= 0
            or journal_generation > previous_generation
        ):
            raise UpdateControllerConflict("update journal does not own the transferred lifecycle operation")
        payload["lifecycle_generation"] = generation
        payload["lifecycle_phase"] = lifecycle_phase.value
        payload["target_commit"] = target_commit
        payload["previous_commit"] = previous_commit
        payload["updated_at"] = time.time()
        with state.hold_operation_with_credential(
            root,
            operation_id=operation_id,
            generation=generation,
            expected_phase=lifecycle_phase,
            expected_operation_type=state.OperationType.UPDATE,
        ) as authority:
            _validate_update_journal_semantics(payload, authority=authority)
            _private_atomic_write(
                journal_path,
                (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def reconcile_operator_journal_recovery_required(
    root: Path,
    *,
    operation_id: str,
    generation: int,
    current_phase: state.LifecyclePhase,
    pre_cutover_baseline_rejected: bool = False,
) -> None:
    """Reconcile only the subordinate phase after rejected or dead control.

    A dead operator's caller holds controller.lock. A rejected pre-cutover
    baseline has not launched a child and retains the exact transaction identity.
    No update action is resumed by this journal-only operation.
    """

    journal_path, lock_path, _ = _update_paths(root, operation_id, generation)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        payload = _read_update_journal(journal_path)
        current = state.inspect_state(root)
        operation = current.active_operation
        if (
            operation is None
            or operation.operation_id != operation_id
            or operation.operation_type is not state.OperationType.UPDATE
            or current.generation != generation
            or operation.phase is not current_phase
            or payload.get("transaction_id") != operation_id
            or payload.get("lifecycle_generation") != generation
        ):
            raise UpdateControllerConflict("interrupted operator update identity changed")
        journal_phase = state.LifecyclePhase(str(payload.get("lifecycle_phase")))
        allowed_phases = {
            state.LifecyclePhase.RECOVERY_REQUIRED,
            state.LifecyclePhase.EXCLUSIVE,
            state.LifecyclePhase.VERIFYING,
        }
        if (
            pre_cutover_baseline_rejected
            and payload.get("runtime_committed") is False
            and payload.get("phase") in {"queued", "preflight", "waiting_for_idle"}
        ):
            allowed_phases.add(state.LifecyclePhase.RESERVED)
        if journal_phase not in allowed_phases:
            raise UpdateControllerConflict("interrupted operator journal phase is invalid")
        prior = state.LifecycleState(
            current.schema_version,
            generation,
            state.ActiveOperation(
                operation.operation_id,
                operation.operation_type,
                journal_phase,
                operation.owner_kind,
                operation.owner_nonce_hash,
            ),
        )
        _validate_update_journal_semantics(payload, authority=prior)
        if journal_phase is not state.LifecyclePhase.RECOVERY_REQUIRED:
            updated = dict(payload)
            updated["lifecycle_phase"] = state.LifecyclePhase.RECOVERY_REQUIRED.value
            if not pre_cutover_baseline_rejected:
                updated["updated_at"] = time.time()
            recovery_authority = state.LifecycleState(
                current.schema_version,
                generation,
                state.ActiveOperation(
                    operation.operation_id,
                    operation.operation_type,
                    state.LifecyclePhase.RECOVERY_REQUIRED,
                    operation.owner_kind,
                    operation.owner_nonce_hash,
                ),
            )
            _validate_update_journal_semantics(updated, authority=recovery_authority)
            with state.hold_operation_with_credential(
                root,
                operation_id=operation_id,
                generation=generation,
                expected_phase=current_phase,
                expected_operation_type=state.OperationType.UPDATE,
            ):
                _private_atomic_write(
                    journal_path,
                    (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
    except (OSError, ValueError) as exc:
        raise UpdateControllerConflict("interrupted operator journal is unavailable") from exc
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _permit_payload(operation_id: str, generation: int, token: str) -> bytes:
    return (
        json.dumps(
            {
                "operation_id": operation_id,
                "generation": generation,
                "token": token,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def create_cutover_permit(
    root: str | os.PathLike[str], operation_id: str, generation: int
) -> Path:
    _, _, permit_path = _update_paths(root, operation_id, generation)
    token = secrets.token_urlsafe(32)
    _private_atomic_write(permit_path, _permit_payload(operation_id, generation, token))
    return permit_path


def _read_cutover_permit(
    root: str | os.PathLike[str], operation_id: str, generation: int
) -> tuple[Path, str]:
    _, _, permit_path = _update_paths(root, operation_id, generation)
    try:
        file_state = permit_path.lstat()
        if (
            not stat.S_ISREG(file_state.st_mode)
            or permit_path.is_symlink()
            or stat.S_IMODE(file_state.st_mode) & 0o077
            or file_state.st_size > 1024
        ):
            raise UpdateControllerConflict("update cutover permit is not trusted")
        payload = json.loads(permit_path.read_text(encoding="utf-8"))
    except UpdateControllerError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise UpdateControllerConflict("update cutover permit is unavailable") from exc
    if not isinstance(payload, dict) or set(payload) != {"operation_id", "generation", "token"}:
        raise UpdateControllerConflict("update cutover permit is invalid")
    token = payload.get("token")
    if (
        payload.get("operation_id") != operation_id
        or payload.get("generation") != generation
        or not isinstance(token, str)
        or len(token) < 32
    ):
        raise UpdateControllerConflict("update cutover permit does not match")
    return permit_path, token


def validate_cutover_permit(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    supplied_token: str,
) -> None:
    _, token = _read_cutover_permit(root, operation_id, generation)
    if not supplied_token or not hmac.compare_digest(token, supplied_token):
        raise UpdateControllerConflict("update cutover authorization failed")
    current = state.validate_operation_with_credential(
        root,
        operation_id=operation_id,
        generation=generation,
    )
    operation = current.active_operation
    if operation is None or operation.operation_type is not state.OperationType.UPDATE:
        raise UpdateControllerConflict("update lifecycle authority does not match")


def validate_inherited_controller(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    expected_phase: str,
    supervisor_pid: int,
) -> None:
    """Validate that the shell is the direct child of the fenced supervisor."""

    paths = state.lifecycle_paths(root)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            paths.controller_lock_path,
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            raise UpdateControllerConflict("update controller lock is not owned")
    except OSError as exc:
        raise UpdateControllerConflict("update controller lock is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    current = state.validate_operation_with_credential(
        root,
        operation_id=operation_id,
        generation=generation,
        expected_phase=expected_phase,
    )
    operation = current.active_operation
    lease = state.read_controller_lease(root)
    controller_pid = supervisor_pid
    if (
        operation is None
        or operation.operation_type is not state.OperationType.UPDATE
        or operation.owner_kind is not state.OwnerKind.UPDATER
        or lease is None
        or lease.operation_id != operation_id
        or lease.generation != generation
        or lease.pid != controller_pid
        or not state.controller_lease_is_current(lease)
    ):
        raise UpdateControllerConflict("update controller lease does not match")


def remove_cutover_permit(
    root: str | os.PathLike[str], operation_id: str, generation: int
) -> None:
    _, _, permit_path = _update_paths(root, operation_id, generation)
    try:
        permit_path.unlink()
        _fsync_directory(permit_path.parent)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UpdateControllerPersistenceError("update cutover permit could not be removed") from exc


def _controller_environment(
    root: Path,
    *,
    operation_id: str,
    generation: int,
    lifecycle_phase: state.LifecyclePhase,
    target_commit: str,
    automatic: bool,
    install_mode: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "STREAMHOME_INSTANCE_ROOT",
        "STREAMHOME_INSTANCE_TOKEN",
        "STREAMHOME_SERVICE",
        "STREAMHOME_UPDATE_TRANSACTION",
        "STREAMHOME_UPDATE_COMMIT_TOKEN",
        "STREAMHOME_LIFECYCLE_STARTUP_MODE",
        "STREAMHOME_LIFECYCLE_FIRST_ADOPTION",
        "STREAMHOME_LIFECYCLE_CONTROLLER_FD",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "STREAMHOME_INSTALL_DIR": str(root),
            "STREAMHOME_UPDATE_AUTOMATIC": "true" if automatic else "false",
            "STREAMHOME_UPDATE_INSTALL_MODE": install_mode,
            "STREAMHOME_LIFECYCLE_OPERATION_ID": operation_id,
            "STREAMHOME_LIFECYCLE_GENERATION": str(generation),
            "STREAMHOME_LIFECYCLE_PHASE": lifecycle_phase.value,
            "STREAMHOME_LIFECYCLE_CONTROLLER_OWNED": "true",
        }
    )
    return environment


def _materialize_target_controller(root: Path, operation_id: str, target_commit: str) -> Path:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{target_commit}:update.sh"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.startswith(b"#!/usr/bin/env bash\n"):
        raise UpdateControllerConflict("validated target update controller is unavailable")
    destination = layout_for_root(root).durable_runtime / f"update-controller.{operation_id}.sh"
    try:
        destination.unlink()
        _fsync_directory(destination.parent)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise UpdateControllerPersistenceError("stale update controller could not be removed") from exc
    _private_atomic_write(destination, result.stdout)
    os.chmod(destination, 0o700)
    _fsync_directory(destination.parent)
    return destination


def _mark_recovery_required(
    root: Path, operation_id: str, generation: int, phase: state.LifecyclePhase
) -> None:
    if phase is state.LifecyclePhase.RECOVERY_REQUIRED:
        return
    try:
        state.transition_operation_with_credential(
            root,
            operation_id=operation_id,
            generation=generation,
            expected_phase=phase,
            next_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
        )
    except state.LifecycleError:
        pass


def _mark_pre_cutover_baseline_rejected(root: Path, operation_id: str, generation: int) -> None:
    """Retain exact evidence and expose explicit recovery without starting a child."""
    _mark_recovery_required(root, operation_id, generation, state.LifecyclePhase.RESERVED)
    try:
        reconcile_operator_journal_recovery_required(
            root,
            operation_id=operation_id,
            generation=generation,
            current_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
            pre_cutover_baseline_rejected=True,
        )
    except (UpdateControllerError, state.LifecycleError, OSError):
        # If phase normalization itself is uncertain, keep both original evidence
        # and durable recovery authority fail-closed. No baseline is invented.
        pass


def _run_controller(
    root: Path,
    *,
    operation_id: str,
    generation: int,
    target_commit: str,
    previous_commit: str,
    automatic: bool,
    install_mode: str,
    update_branch: str,
    acknowledgement_fd: int | None,
    recovery: bool,
    operator_action: str | None = None,
    pre_transfer_check: Callable[[], None] | None = None,
    on_transfer: Callable[[int], None] | None = None,
) -> int:
    state.qualify_lifecycle_filesystem(root)
    state.qualify_local_filesystem(
        root,
        require_private=False,
        description="installation directory",
    )
    current = state.inspect_state(root)
    operation = current.active_operation
    if (
        operation is None
        or operation.operation_id != operation_id
        or current.generation != generation
        or operation.operation_type is not state.OperationType.UPDATE
        or (
            operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED
            and operator_action is None
        )
    ):
        raise UpdateControllerConflict("update lifecycle authority is not resumable")
    if operator_action not in {None, "safe_abort", "continue_recovery"}:
        raise UpdateControllerConflict("operator update action is unsupported")
    if operator_action is not None and operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED:
        raise UpdateControllerConflict("operator update action requires recovery-required authority")
    pre_cutover = operation.phase is state.LifecyclePhase.RESERVED
    if pre_cutover:
        try:
            validate_pre_cutover_baseline(root, previous_commit)
        except UpdateControllerError:
            _mark_pre_cutover_baseline_rejected(root, operation_id, generation)
            raise
    capability = state.load_controller_capability(
        root, operation_id=operation_id, generation=generation
    )
    lease, transferred = state.acquire_transferred_controller_lease(
        root,
        operation_id=operation_id,
        generation=generation,
        expected_phase=operation.phase,
        owner_capability=capability,
        next_owner_kind=state.OwnerKind.UPDATER,
        pre_transfer_check=pre_transfer_check,
    )
    transferred_operation = transferred.state.active_operation
    if transferred_operation is None:  # pragma: no cover - transfer contract
        raise UpdateControllerConflict("update controller transfer lost its operation")
    current_generation = transferred.state.generation
    current_phase = transferred_operation.phase
    controller_process: subprocess.Popen[bytes] | None = None
    try:
        _record_transferred_generation(
            root,
            operation_id=operation_id,
            previous_generation=generation,
            generation=current_generation,
            lifecycle_phase=current_phase,
            target_commit=target_commit,
            previous_commit=previous_commit,
        )
        if on_transfer is not None:
            on_transfer(current_generation)
        if operator_action == "safe_abort":
            journal_path, _, _ = _update_paths(root, operation_id, current_generation)
            payload = _read_update_journal(journal_path)
            _validate_update_journal_semantics(payload, authority=transferred.state)
            if (
                payload.get("runtime_committed") is True
                or payload.get("phase") not in {"queued", "preflight", "waiting_for_idle", "failed"}
                or (layout_for_root(root).durable_runtime / "pre-update-database.db").exists()
                or (layout_for_root(root).durable_runtime / "pre-update-database.db").is_symlink()
            ):
                raise UpdateControllerConflict("update abort evidence is no longer safe")
            if payload.get("phase") != "failed":
                mutate_update_journal(
                    root,
                    operation_id=operation_id,
                    generation=current_generation,
                    expected_lifecycle_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
                    expected_detail_phases=frozenset(
                        {"queued", "preflight", "waiting_for_idle"}
                    ),
                    changes={
                        "phase": "failed",
                        "message": "The unresolved update was safely abandoned by explicit operator action.",
                        "error": "operator_safe_abort",
                        "queued_at": None,
                    },
                )
            state.complete_operation_with_credential(
                root,
                operation_id=operation_id,
                generation=current_generation,
                expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
                expected_operation_type=state.OperationType.UPDATE,
            )
            return 0
        if operator_action == "continue_recovery":
            journal_path, _, _ = _update_paths(
                root, operation_id, current_generation
            )
            recovery_payload = _read_update_journal(journal_path)
            _validate_update_journal_semantics(
                recovery_payload, authority=transferred.state
            )
            recovery_phase = (
                state.LifecyclePhase.VERIFYING
                if recovery_payload.get("runtime_committed") is True
                else state.LifecyclePhase.EXCLUSIVE
            )
            state.transition_operation_with_credential(
                root,
                operation_id=operation_id,
                generation=current_generation,
                expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
                next_phase=state.LifecyclePhase.EXCLUSIVE,
            )
            if recovery_phase is state.LifecyclePhase.VERIFYING:
                state.transition_operation_with_credential(
                    root,
                    operation_id=operation_id,
                    generation=current_generation,
                    expected_phase=state.LifecyclePhase.EXCLUSIVE,
                    next_phase=state.LifecyclePhase.VERIFYING,
                )
            current_phase = recovery_phase
            mutate_update_journal(
                root,
                operation_id=operation_id,
                generation=current_generation,
                expected_lifecycle_phase=recovery_phase,
                expected_detail_phases=frozenset({"starting", "recovering"}),
                changes={
                    "phase": "recovering",
                    "message": "Explicit operator recovery is continuing the protected update.",
                    "error": "",
                },
            )
        create_cutover_permit(root, operation_id, current_generation)
        if recovery:
            command = [str(root / "update.sh"), "--recover-interrupted", str(root)]
        else:
            controller = _materialize_target_controller(root, operation_id, target_commit)
            command = [
                str(controller),
                "--manual-execute",
                target_commit,
                previous_commit,
                update_branch,
                "true",
                str(root),
            ]
        environment = _controller_environment(
            root,
            operation_id=operation_id,
            generation=current_generation,
            lifecycle_phase=current_phase,
            target_commit=target_commit,
            automatic=automatic,
            install_mode=install_mode,
        )
        if pre_cutover:
            # Materialization and ownership handoff may outlive an owner's clean
            # checkout change. The process-local cutover checks again after drain.
            validate_pre_cutover_baseline(root, previous_commit)
        controller_process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env=environment,
        )
        if acknowledgement_fd is not None:
            _write_all(
                acknowledgement_fd,
                json.dumps(
                    {
                        "operation_id": operation_id,
                        "generation": current_generation,
                        "pid": os.getpid(),
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            os.close(acknowledgement_fd)
            acknowledgement_fd = None
        return_code = controller_process.wait()
        if operator_action is not None and return_code != 0:
            current = state.inspect_state(root)
            current_operation = current.active_operation
            if (
                current_operation is not None
                and current_operation.operation_id == operation_id
                and current.generation == current_generation
            ):
                _mark_recovery_required(
                    root,
                    operation_id,
                    current_generation,
                    current_operation.phase,
                )
        return return_code
    except Exception as exc:
        if controller_process is not None and controller_process.poll() is None:
            controller_process.terminate()
            try:
                controller_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                controller_process.kill()
                controller_process.wait(timeout=5)
        failure_phase = current_phase
        if operator_action is not None:
            try:
                failed_state = state.inspect_state(root)
                failed_operation = failed_state.active_operation
                if (
                    failed_operation is not None
                    and failed_operation.operation_id == operation_id
                    and failed_state.generation == current_generation
                ):
                    failure_phase = failed_operation.phase
            except state.LifecycleError:
                pass
        if isinstance(exc, UpdateBaselineConflict) and pre_cutover and controller_process is None:
            _mark_pre_cutover_baseline_rejected(root, operation_id, current_generation)
        else:
            _mark_recovery_required(root, operation_id, current_generation, failure_phase)
        raise
    finally:
        if acknowledgement_fd is not None:
            os.close(acknowledgement_fd)
        try:
            remove_cutover_permit(root, operation_id, current_generation)
        except UpdateControllerError:
            pass
        # The Python supervisor alone owns controller.lock. The shell and its
        # descendants never inherit that descriptor.
        if not lease._released:
            lease.release()


def _operator_recovery_identity(
    root: Path,
    operation_id: str,
    generation: int,
    journal_generation: int | None = None,
) -> tuple[str, str, bool, str, str]:
    current = state.inspect_state(root)
    operation = current.active_operation
    if (
        operation is None
        or operation.operation_id != operation_id
        or operation.operation_type is not state.OperationType.UPDATE
        or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
        or current.generation != generation
    ):
        raise UpdateControllerConflict("operator update authority does not match")
    journal_path, _, _ = _update_paths(root, operation_id, generation)
    payload = _read_update_journal(journal_path)
    expected_journal_generation = (
        generation if journal_generation is None else journal_generation
    )
    if expected_journal_generation not in {generation, generation - 1}:
        raise UpdateControllerConflict("operator update journal generation is invalid")
    journal_authority = state.LifecycleState(
        current.schema_version,
        expected_journal_generation,
        current.active_operation,
    )
    previous = pre_cutover_previous_commit(root, payload, journal_authority)
    if previous is not None:
        payload["previous_commit"] = previous
    _validate_update_journal_semantics(payload, authority=journal_authority)
    target = payload.get("target_commit")
    previous = payload.get("previous_commit")
    automatic = payload.get("automatic", False)
    install_mode = payload.get("install_mode") or "when_idle"
    update_branch = payload.get("update_branch") or "main"
    if (
        not isinstance(target, str)
        or not COMMIT_RE.fullmatch(target)
        or not isinstance(previous, str)
        or not COMMIT_RE.fullmatch(previous)
        or not isinstance(automatic, bool)
        or install_mode not in INSTALL_MODES
        or not isinstance(update_branch, str)
        or not BRANCH_RE.fullmatch(update_branch)
        or ".." in update_branch
    ):
        raise UpdateControllerConflict("operator update journal identity is invalid")
    return target, previous, automatic, str(install_mode), update_branch


def run_operator_recovery(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    action: str,
    journal_generation: int | None,
    pre_transfer_check: Callable[[], None],
    on_transfer: Callable[[int], None],
) -> int:
    """Execute one freshly authorized action through the managed-update controller."""

    installation = state.resolve_installation_root(root)
    target, previous, automatic, install_mode, update_branch = _operator_recovery_identity(
        installation, operation_id, generation, journal_generation
    )
    return _run_controller(
        installation,
        operation_id=operation_id,
        generation=generation,
        target_commit=target,
        previous_commit=previous,
        automatic=automatic,
        install_mode=install_mode,
        update_branch=update_branch,
        acknowledgement_fd=None,
        recovery=True,
        operator_action=action,
        pre_transfer_check=pre_transfer_check,
        on_transfer=on_transfer,
    )


def spawn_update_controller(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    target_commit: str,
    previous_commit: str,
    automatic: bool,
    install_mode: str,
    update_branch: str,
    log_handle: Any,
) -> UpdateControllerHandoff:
    installation = state.resolve_installation_root(root)
    if not COMMIT_RE.fullmatch(target_commit) or not COMMIT_RE.fullmatch(previous_commit):
        raise ValueError("update commits must be full SHA-1 identities")
    if install_mode not in INSTALL_MODES:
        raise ValueError("update install mode is invalid")
    if not BRANCH_RE.fullmatch(update_branch) or ".." in update_branch:
        raise ValueError("update branch is invalid")
    acknowledgement_read, acknowledgement_write = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        command = [
            sys.executable,
            "-m",
            "lifecycle.update_controller",
            "run",
            "--root",
            str(installation),
            "--operation-id",
            operation_id,
            "--generation",
            str(generation),
            "--target-commit",
            target_commit,
            "--previous-commit",
            previous_commit,
            "--install-mode",
            install_mode,
            "--update-branch",
            update_branch,
            "--ack-fd",
            str(acknowledgement_write),
        ]
        if automatic:
            command.append("--automatic")
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(installation / "server")
        process = subprocess.Popen(
            command,
            cwd=str(installation),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            pass_fds=(acknowledgement_write,),
            env=environment,
        )
        os.close(acknowledgement_write)
        acknowledgement_write = -1
        ready, _, _ = select.select([acknowledgement_read], [], [], ACK_TIMEOUT_SECONDS)
        if not ready:
            raise UpdateControllerConflict("update controller ownership was not acknowledged")
        raw = os.read(acknowledgement_read, 4096)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise UpdateControllerConflict("update controller acknowledgement is invalid") from exc
        expected_generation = generation + 1
        if (
            not isinstance(payload, Mapping)
            or payload.get("operation_id") != operation_id
            or payload.get("generation") != expected_generation
            or payload.get("pid") != process.pid
        ):
            raise UpdateControllerConflict("update controller acknowledgement does not match")
        current = state.validate_operation_with_credential(
            installation,
            operation_id=operation_id,
            generation=expected_generation,
        )
        operation = current.active_operation
        lease = state.read_controller_lease(installation)
        if (
            operation is None
            or operation.operation_type is not state.OperationType.UPDATE
            or operation.owner_kind is not state.OwnerKind.UPDATER
            or lease is None
            or lease.operation_id != operation_id
            or lease.generation != expected_generation
            or lease.pid != process.pid
            or not state.controller_lease_is_current(lease)
        ):
            raise UpdateControllerConflict("update controller lease was not established")
        return UpdateControllerHandoff(process, operation_id, expected_generation)
    except Exception:
        if process is not None and process.poll() is None:
            process.terminate()
        raise
    finally:
        os.close(acknowledgement_read)
        if acknowledgement_write >= 0:
            os.close(acknowledgement_write)


def _recover_identity(root: Path) -> tuple[str, int, str, str, bool, str, str, bool]:
    current = state.inspect_state(root)
    operation = current.active_operation
    if (
        operation is None
        or operation.operation_type is not state.OperationType.UPDATE
        or operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED
    ):
        raise UpdateControllerConflict("automatic update recovery is not authorized")
    from . import recovery as operator_recovery

    if operator_recovery.operator_action_blocks_automatic_recovery(
        root, operation.operation_id, current.generation
    ):
        operator_recovery.normalize_interrupted_operator(
            root, operation.operation_id, current.generation
        )
        raise UpdateControllerConflict("explicit operator recovery cannot be resumed automatically")
    journal_path = layout_for_root(root).durable_runtime / "update-state.json"
    try:
        payload = _read_update_journal(journal_path)
    except UpdateControllerError:
        _mark_recovery_required(root, operation.operation_id, current.generation, operation.phase)
        raise
    journal_generation = payload.get("lifecycle_generation")
    if (
        payload.get("transaction_id") != operation.operation_id
        or isinstance(journal_generation, bool)
        or not isinstance(journal_generation, int)
        or journal_generation <= 0
        or journal_generation > current.generation
    ):
        _mark_recovery_required(root, operation.operation_id, current.generation, operation.phase)
        raise UpdateControllerConflict("update journal disagrees with lifecycle authority")
    journal_phase = payload.get("phase")
    reconciled = dict(payload)
    reconciled["lifecycle_generation"] = current.generation
    reconciled["lifecycle_phase"] = operation.phase.value
    previous = pre_cutover_previous_commit(root, payload, current)
    if previous is not None:
        reconciled["previous_commit"] = previous
    try:
        _validate_update_journal_semantics(reconciled, authority=current)
    except UpdateControllerError:
        _mark_recovery_required(root, operation.operation_id, current.generation, operation.phase)
        raise
    if journal_phase not in {
        "queued",
        "preflight",
        "waiting_for_idle",
        "stopping",
        "installing",
        "starting",
        "rolling_back",
        "recovering",
        "succeeded",
        "rolled_back",
    }:
        _mark_recovery_required(root, operation.operation_id, current.generation, operation.phase)
        raise UpdateControllerConflict("update journal is not safely resumable")
    target = payload.get("target_commit")
    previous = reconciled.get("previous_commit")
    install_mode = payload.get("install_mode") or "when_idle"
    update_branch = payload.get("update_branch") or "main"
    if (
        not isinstance(target, str)
        or not COMMIT_RE.fullmatch(target)
        or not isinstance(previous, str)
        or not COMMIT_RE.fullmatch(previous)
        or install_mode not in INSTALL_MODES
        or not isinstance(update_branch, str)
        or not BRANCH_RE.fullmatch(update_branch)
        or ".." in update_branch
    ):
        _mark_recovery_required(root, operation.operation_id, current.generation, operation.phase)
        raise UpdateControllerConflict("update recovery journal identity is invalid")
    if operation.phase is state.LifecyclePhase.RESERVED:
        try:
            validate_pre_cutover_baseline(root, previous)
        except UpdateControllerError:
            _mark_pre_cutover_baseline_rejected(root, operation.operation_id, current.generation)
            raise
    return (
        operation.operation_id,
        current.generation,
        target,
        previous,
        bool(payload.get("automatic")),
        str(install_mode),
        update_branch,
        journal_phase == "queued",
    )


def request_cutover(
    root: str | os.PathLike[str], operation_id: str, generation: int
) -> int:
    from urllib import error as urlerror
    from urllib import request as urlrequest

    _, token = _read_cutover_permit(root, operation_id, generation)
    request = urlrequest.Request(
        "http://127.0.0.1:8000/api/update/cutover",
        method="POST",
        data=json.dumps(
            {"operation_id": operation_id, "generation": generation},
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-StreamHome-Update-Cutover": token,
        },
    )
    try:
        with urlrequest.build_opener(urlrequest.ProxyHandler({})).open(request, timeout=15) as response:
            return 0 if 200 <= response.status < 300 else 1
    except urlerror.HTTPError as exc:
        return 10 if exc.code == 409 else 1
    except OSError:
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamhome-update-controller")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--operation-id", required=True)
    run.add_argument("--generation", type=int, required=True)
    run.add_argument("--target-commit", required=True)
    run.add_argument("--previous-commit", required=True)
    run.add_argument("--install-mode", choices=sorted(INSTALL_MODES), required=True)
    run.add_argument("--update-branch", required=True)
    run.add_argument("--automatic", action="store_true")
    run.add_argument("--ack-fd", type=int)
    recover = subparsers.add_parser("recover")
    recover.add_argument("--root", type=Path, required=True)
    cutover = subparsers.add_parser("request-cutover")
    cutover.add_argument("--root", type=Path, required=True)
    cutover.add_argument("--operation-id", required=True)
    cutover.add_argument("--generation", type=int, required=True)
    remove_permit = subparsers.add_parser("remove-permit")
    remove_permit.add_argument("--root", type=Path, required=True)
    remove_permit.add_argument("--operation-id", required=True)
    remove_permit.add_argument("--generation", type=int, required=True)
    validate_controller = subparsers.add_parser("validate-controller")
    validate_controller.add_argument("--root", type=Path, required=True)
    validate_controller.add_argument("--operation-id", required=True)
    validate_controller.add_argument("--generation", type=int, required=True)
    validate_controller.add_argument("--expected-phase", required=True)
    validate_controller.add_argument("--supervisor-pid", type=int, required=True)
    write_journal = subparsers.add_parser("write-journal")
    write_journal.add_argument("--root", type=Path, required=True)
    write_journal.add_argument("--operation-id", required=True)
    write_journal.add_argument("--generation", type=int, required=True)
    write_journal.add_argument("--expected-lifecycle-phase", required=True)
    write_journal.add_argument("--expected-detail-phase", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        root = state.resolve_installation_root(arguments.root)
        if arguments.command == "request-cutover":
            return request_cutover(root, arguments.operation_id, arguments.generation)
        if arguments.command == "remove-permit":
            remove_cutover_permit(root, arguments.operation_id, arguments.generation)
            return 0
        if arguments.command == "validate-controller":
            validate_inherited_controller(
                root,
                operation_id=arguments.operation_id,
                generation=arguments.generation,
                expected_phase=arguments.expected_phase,
                supervisor_pid=arguments.supervisor_pid,
            )
            return 0
        if arguments.command == "write-journal":
            raw = sys.stdin.buffer.read(MAX_UPDATE_JOURNAL_BYTES + 1)
            if len(raw) > MAX_UPDATE_JOURNAL_BYTES:
                raise UpdateControllerConflict("update journal mutation is too large")
            try:
                changes = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError, TypeError) as exc:
                raise UpdateControllerConflict("update journal mutation is invalid") from exc
            if not isinstance(changes, Mapping):
                raise UpdateControllerConflict("update journal mutation must be an object")
            mutate_update_journal(
                root,
                operation_id=arguments.operation_id,
                generation=arguments.generation,
                expected_lifecycle_phase=arguments.expected_lifecycle_phase,
                expected_detail_phases=(
                    frozenset(arguments.expected_detail_phase)
                    if arguments.expected_detail_phase
                    else None
                ),
                changes=changes,
            )
            return 0
        if arguments.command == "recover":
            (
                operation_id,
                generation,
                target,
                previous,
                automatic,
                install_mode,
                update_branch,
                resume_queued,
            ) = _recover_identity(root)
            return _run_controller(
                root,
                operation_id=operation_id,
                generation=generation,
                target_commit=target,
                previous_commit=previous,
                automatic=automatic,
                install_mode=install_mode,
                update_branch=update_branch,
                acknowledgement_fd=None,
                recovery=not resume_queued,
            )
        return _run_controller(
            root,
            operation_id=arguments.operation_id,
            generation=arguments.generation,
            target_commit=arguments.target_commit,
            previous_commit=arguments.previous_commit,
            automatic=arguments.automatic,
            install_mode=arguments.install_mode,
            update_branch=arguments.update_branch,
            acknowledgement_fd=arguments.ack_fd,
            recovery=False,
        )
    except (ValueError, state.LifecycleError, UpdateControllerError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
