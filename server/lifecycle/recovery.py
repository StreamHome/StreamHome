"""Evidence-driven lifecycle recovery planning and explicit operator execution.

Planning and retention classification remain read-only.  Execution persists a
sanitized intent, revalidates that exact plan under ``controller.lock``, rotates
generation/capability ownership, and delegates recovery to the Phase 4/5
controllers.  This module does not implement database or code activation itself.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid

from . import backup_manifest, restore_controller, state, update_controller
from .layout import layout_for_root


ACTION_RECORD_SCHEMA_VERSION = 1
MAX_ACTION_RECORD_BYTES = 8 * 1024
ACTION_IDENTIFIERS = frozenset({"safe_abort", "continue_recovery", "rollback"})
ACTION_RECORD_STATUSES = frozenset({"planned", "executing", "succeeded", "failed"})
ACTION_RESULT_CODES = frozenset(
    {
        "operator_cleanup_pending",
        "operator_controller_conflict",
        "operator_evidence_stale",
        "operator_execution_failed",
        "operator_filesystem_unsupported",
        "operator_recovery_completed",
        "operator_safe_abort_completed",
    }
)
EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,80}$")


class RecoveryPlanningError(RuntimeError):
    code = "recovery_plan_unavailable"


class RecoveryPlanningConflict(RecoveryPlanningError):
    code = "recovery_plan_conflict"


class RecoveryExecutionError(RecoveryPlanningError):
    code = "recovery_execution_failed"


class RecoveryEvidenceStale(RecoveryExecutionError):
    code = "recovery_evidence_stale"


@dataclass(frozen=True, slots=True)
class OperatorActionRecord:
    schema_version: int
    operation_id: str
    generation: int
    operation_type: str
    requested_action: str
    created_at: str
    evidence_identity: str
    status: str
    controller_generation: int | None
    result: str | None


@dataclass(frozen=True, slots=True)
class OperatorActionPaths:
    operation_dir: Path
    lock_path: Path
    record_path: Path


def _canonical_operation_id(value: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise RecoveryPlanningConflict("operation identity is invalid") from exc
    if canonical != value:
        raise RecoveryPlanningConflict("operation identity is invalid")
    return canonical


def _sanitize_reason(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and ERROR_CODE_RE.fullmatch(value) else fallback


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not backup_manifest.RFC3339_UTC_RE.fullmatch(value):
        raise RecoveryPlanningError("operator action timestamp is invalid")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RecoveryPlanningError("operator action timestamp is invalid") from exc
    return value


def parse_operator_action_record(payload: Any) -> OperatorActionRecord:
    """Validate one durable operator action record."""

    if not isinstance(payload, Mapping):
        raise RecoveryPlanningError("operator action record must be an object")
    expected = {
        "schema_version",
        "operation_id",
        "generation",
        "operation_type",
        "requested_action",
        "created_at",
        "evidence_identity",
        "status",
        "controller_generation",
        "result",
    }
    if set(payload) != expected:
        raise RecoveryPlanningError("operator action record contains missing or unknown fields")
    schema_version = payload["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != ACTION_RECORD_SCHEMA_VERSION
    ):
        raise RecoveryPlanningError("operator action schema is unsupported")
    operation_id = _canonical_operation_id(payload["operation_id"])
    generation = payload["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise RecoveryPlanningError("operator action generation is invalid")
    operation_type = payload["operation_type"]
    if operation_type not in {state.OperationType.UPDATE.value, state.OperationType.RESTORE.value}:
        raise RecoveryPlanningError("operator action operation type is invalid")
    action = payload["requested_action"]
    if action not in ACTION_IDENTIFIERS:
        raise RecoveryPlanningError("operator action is unsupported")
    evidence_identity = payload["evidence_identity"]
    if not isinstance(evidence_identity, str) or not EVIDENCE_ID_RE.fullmatch(evidence_identity):
        raise RecoveryPlanningError("operator action evidence identity is invalid")
    status_value = payload["status"]
    if status_value not in ACTION_RECORD_STATUSES:
        raise RecoveryPlanningError("operator action status is invalid")
    controller_generation = payload["controller_generation"]
    if controller_generation is not None and (
        isinstance(controller_generation, bool)
        or not isinstance(controller_generation, int)
        or controller_generation != generation + 1
    ):
        raise RecoveryPlanningError("operator action controller generation is invalid")
    if status_value == "planned" and controller_generation is not None:
        raise RecoveryPlanningError("planned operator action cannot own a controller generation")
    if status_value == "executing" and controller_generation is None:
        raise RecoveryPlanningError("executing operator action requires a controller generation")
    if status_value == "succeeded" and controller_generation is None:
        raise RecoveryPlanningError("succeeded operator action requires a controller generation")
    result = payload["result"]
    if result is not None and result not in ACTION_RESULT_CODES:
        raise RecoveryPlanningError("operator action result is invalid")
    if status_value in {"planned", "executing"} and result is not None:
        raise RecoveryPlanningError("unfinished operator action cannot have a result")
    if status_value in {"succeeded", "failed"} and result is None:
        raise RecoveryPlanningError("finished operator action requires a result")
    if status_value == "succeeded" and result not in {
        "operator_cleanup_pending",
        "operator_recovery_completed",
        "operator_safe_abort_completed",
    }:
        raise RecoveryPlanningError("succeeded operator action result is inconsistent")
    if status_value == "failed" and result in {
        "operator_cleanup_pending",
        "operator_recovery_completed",
        "operator_safe_abort_completed",
    }:
        raise RecoveryPlanningError("failed operator action result is inconsistent")
    return OperatorActionRecord(
        ACTION_RECORD_SCHEMA_VERSION,
        operation_id,
        generation,
        operation_type,
        action,
        _validate_timestamp(payload["created_at"]),
        evidence_identity,
        status_value,
        controller_generation,
        result,
    )


def parse_operator_action_json(raw: str | bytes) -> OperatorActionRecord:
    """Strictly decode a durable action record without persisting it."""

    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes) or len(encoded) > MAX_ACTION_RECORD_BYTES:
        raise RecoveryPlanningError("operator action record size is invalid")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryPlanningError("operator action record contains duplicate fields")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise RecoveryPlanningError("operator action record contains a non-finite number")

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=reject_constant,
        )
    except RecoveryPlanningError:
        raise
    except (UnicodeError, ValueError, TypeError) as exc:
        raise RecoveryPlanningError("operator action record cannot be decoded") from exc
    return parse_operator_action_record(payload)


def _action_record_payload(record: OperatorActionRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "operation_id": record.operation_id,
        "generation": record.generation,
        "operation_type": record.operation_type,
        "requested_action": record.requested_action,
        "created_at": record.created_at,
        "evidence_identity": record.evidence_identity,
        "status": record.status,
        "controller_generation": record.controller_generation,
        "result": record.result,
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_private_directory(path: Path, description: str) -> None:
    created = False
    try:
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=False)
            created = True
        except FileExistsError:
            pass
        file_state = path.lstat()
        if (
            not stat.S_ISDIR(file_state.st_mode)
            or path.is_symlink()
            or (hasattr(os, "geteuid") and file_state.st_uid != os.geteuid())
        ):
            raise RecoveryExecutionError(f"{description} is not trusted")
        os.chmod(path, 0o700)
        _fsync_directory(path)
        if created:
            _fsync_directory(path.parent)
    except RecoveryPlanningError:
        raise
    except OSError as exc:
        raise RecoveryExecutionError(f"{description} is unavailable") from exc


def _new_action_paths(root: Path, operation_id: str, generation: int) -> OperatorActionPaths:
    lifecycle = state.lifecycle_paths(root)
    actions_root = lifecycle.lifecycle_dir / "operator-actions"
    _ensure_private_directory(actions_root, "operator action directory")
    operation_dir = actions_root / operation_id
    _ensure_private_directory(operation_dir, "operator operation directory")
    token = secrets.token_hex(8)
    return OperatorActionPaths(
        operation_dir,
        operation_dir / "action.lock",
        operation_dir / f"action.{generation}.{token}.json",
    )


@contextmanager
def _operator_action_lock(paths: OperatorActionPaths) -> Iterator[None]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            paths.lock_path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryExecutionError("another operator recovery action is active") from exc
        yield
    except RecoveryPlanningError:
        raise
    except OSError as exc:
        raise RecoveryExecutionError("operator action lock is unavailable") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_action_record(path: Path, record: OperatorActionRecord) -> None:
    validated = parse_operator_action_record(_action_record_payload(record))
    encoded = (
        json.dumps(_action_record_payload(validated), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
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
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("operator action write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise RecoveryExecutionError("operator action record could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_operator_action_record(path: Path) -> OperatorActionRecord:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or path.is_symlink()
            or before.st_size > MAX_ACTION_RECORD_BYTES
            or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise RecoveryPlanningError("operator action record is not trusted")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NOATIME", 0),
        )
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RecoveryPlanningError("operator action record changed during inspection")
        raw = os.read(descriptor, MAX_ACTION_RECORD_BYTES + 1)
        if len(raw) > MAX_ACTION_RECORD_BYTES or os.read(descriptor, 1):
            raise RecoveryPlanningError("operator action record is too large")
        return parse_operator_action_json(raw)
    except RecoveryPlanningError:
        raise
    except OSError as exc:
        raise RecoveryPlanningError("operator action record cannot be inspected") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _replace_action_record(
    path: Path,
    record: OperatorActionRecord,
    *,
    status: str,
    controller_generation: int | None = None,
    result: str | None = None,
) -> OperatorActionRecord:
    current = read_operator_action_record(path)
    if current != record:
        raise RecoveryExecutionError("operator action record changed unexpectedly")
    legal = {
        "planned": frozenset({"executing", "failed"}),
        "executing": frozenset({"succeeded", "failed"}),
        "succeeded": frozenset({"succeeded"}),
        # A controller may complete authority immediately before its caller records
        # the outcome.  Exact terminal evidence may reconcile that failed audit
        # marker to succeeded for cleanup, but it never re-executes the action.
        "failed": frozenset({"succeeded"}),
    }
    if status not in legal[current.status]:
        raise RecoveryExecutionError("operator action status transition is invalid")
    updated = OperatorActionRecord(
        current.schema_version,
        current.operation_id,
        current.generation,
        current.operation_type,
        current.requested_action,
        current.created_at,
        current.evidence_identity,
        status,
        controller_generation if controller_generation is not None else current.controller_generation,
        result,
    )
    _write_action_record(path, updated)
    return updated


def operator_action_blocks_automatic_recovery(
    root: str | os.PathLike[str], operation_id: str, generation: int
) -> bool:
    """Fail closed when an operator-owned generation may still be interrupted."""

    try:
        installation = state.resolve_installation_root(root)
        canonical_id = _canonical_operation_id(operation_id)
        operation_dir = (
            state.lifecycle_paths(installation).lifecycle_dir
            / "operator-actions"
            / canonical_id
        )
        directory_state = operation_dir.lstat()
        if not stat.S_ISDIR(directory_state.st_mode) or operation_dir.is_symlink():
            return True
        records = tuple(operation_dir.glob("action.*.json"))
    except FileNotFoundError:
        return False
    except (OSError, RecoveryPlanningError, state.LifecycleError):
        return True
    for path in records:
        try:
            record = read_operator_action_record(path)
        except RecoveryPlanningError:
            return True
        if record.operation_id != canonical_id:
            return True
        if record.status in {"planned", "executing", "failed"} and (
            record.controller_generation == generation
            or (
                record.controller_generation is None
                and record.generation + 1 == generation
            )
        ):
            return True
    return False


def normalize_interrupted_operator(
    root: str | os.PathLike[str], operation_id: str, generation: int
) -> bool:
    """Only return a proven dead operator generation to plannable authority.

    This never dispatches the recorded action.  The controller lock is held while
    the exact action, lease, journal and lifecycle generation are rechecked.
    """

    installation = state.resolve_installation_root(root)
    canonical_id = _canonical_operation_id(operation_id)
    paths = state.lifecycle_paths(installation)
    descriptor: int | None = None
    try:
        descriptor = state._open_locked_file(paths.controller_lock_path, nonblocking=True)
        current = state.inspect_state(installation)
        operation = current.active_operation
        if (
            operation is None
            or operation.operation_type is state.OperationType.MANAGEMENT
            or operation.operation_id != canonical_id
            or current.generation != generation
            or operation.owner_kind is not (
                state.OwnerKind.RESTORE_CONTROLLER
                if operation.operation_type is state.OperationType.RESTORE
                else state.OwnerKind.UPDATER
            )
            or operation.phase not in {
                state.LifecyclePhase.RECOVERY_REQUIRED,
                state.LifecyclePhase.EXCLUSIVE,
                state.LifecyclePhase.VERIFYING,
            }
        ):
            return False
        lease = state.read_controller_lease(installation)
        if lease is not None and state.controller_lease_is_current(lease):
            return False
        record_dir = paths.lifecycle_dir / "operator-actions" / canonical_id
        directory_state = record_dir.lstat()
        if not stat.S_ISDIR(directory_state.st_mode) or record_dir.is_symlink():
            return False
        records = [
            read_operator_action_record(path)
            for path in record_dir.glob("action.*.json")
        ]
        if any(
            record.operation_id != canonical_id
            or record.operation_type != operation.operation_type.value
            or record.generation >= generation
            or (
                record.controller_generation is not None
                and record.controller_generation > generation
            )
            for record in records
        ):
            return False
        matching_request = [
            record for record in records if record.generation + 1 == generation
        ]
        if len(matching_request) != 1 or (
            matching_request[0].controller_generation != generation
            or matching_request[0].status != "executing"
        ):
            return False
        if operation.operation_type is state.OperationType.RESTORE:
            journal = restore_controller.inspect_restore_journal(installation, canonical_id)
            if journal["operation_id"] != canonical_id or journal["generation"] != generation:
                return False
            if journal["progress"] in {
                restore_controller.RestoreProgress.SUCCEEDED.value,
                restore_controller.RestoreProgress.ROLLED_BACK.value,
            }:
                return False
            if journal["progress"] != restore_controller.RestoreProgress.RECOVERY_REQUIRED.value:
                restore_controller.update_restore_journal(
                    installation,
                    canonical_id,
                    progress=restore_controller.RestoreProgress.RECOVERY_REQUIRED.value,
                    error_code="operator_controller_interrupted",
                )
        else:
            update_controller.reconcile_operator_journal_recovery_required(
                installation,
                operation_id=canonical_id,
                generation=generation,
                current_phase=operation.phase,
            )
        if operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED:
            state.transition_operation_with_credential(
                installation,
                operation_id=canonical_id,
                generation=generation,
                expected_phase=operation.phase,
                next_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
            )
        return True
    except (
        OSError, RecoveryPlanningError, state.LifecycleError,
        restore_controller.RestoreError, update_controller.UpdateControllerError,
    ):
        return False
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _interrupted_planned_action(
    root: Path,
    operation_id: str,
    generation: int,
    operation_type: str,
) -> tuple[OperatorActionRecord | None, bool]:
    """Find exact durable intent for the transfer-before-journal crash gap."""

    operation_dir = (
        state.lifecycle_paths(root).lifecycle_dir
        / "operator-actions"
        / operation_id
    )
    try:
        directory_state = operation_dir.lstat()
        if not stat.S_ISDIR(directory_state.st_mode) or operation_dir.is_symlink():
            return None, True
        matches: list[OperatorActionRecord] = []
        for path in operation_dir.glob("action.*.json"):
            record = read_operator_action_record(path)
            if (
                record.operation_id == operation_id
                and record.operation_type == operation_type
                and (
                    record.status == "planned"
                    or (
                        record.status == "failed"
                        and record.result == "operator_execution_failed"
                    )
                )
                and record.controller_generation is None
                and record.generation + 1 == generation
            ):
                matches.append(record)
    except FileNotFoundError:
        return None, False
    except (OSError, RecoveryPlanningError):
        return None, True
    if len(matches) > 1:
        return None, True
    return (matches[0] if matches else None), False


def _controller_status(root: Path, operation_id: str, generation: int) -> str:
    lock_path = state.lifecycle_paths(root).controller_lock_path
    lock_status = "free"
    descriptor: int | None = None
    try:
        try:
            lock_state = lock_path.lstat()
        except FileNotFoundError:
            lock_state = None
        if lock_state is not None:
            if (
                not stat.S_ISREG(lock_state.st_mode)
                or lock_path.is_symlink()
                or (hasattr(os, "geteuid") and lock_state.st_uid != os.geteuid())
                or stat.S_IMODE(lock_state.st_mode) & 0o077
            ):
                return "unknown"
            descriptor = os.open(
                lock_path,
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_status = "held"
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

        lease = state.read_controller_lease(root)
        if lease is None:
            return "unknown" if lock_status == "held" else "inactive"
        if lease.operation_id != operation_id or lease.generation != generation:
            return "unknown"
        if state.controller_lease_is_current(lease):
            return "active" if lock_status == "held" else "unknown"
        return "unknown" if lock_status == "held" else "inactive"
    except (OSError, state.LifecycleError):
        return "unknown"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _path_status(
    path: Path,
    *,
    expected_size: Any = None,
    expected_digest: Any = None,
) -> str:
    if not path.exists() and not path.is_symlink():
        return "absent"
    try:
        file_state = path.lstat()
        if not stat.S_ISREG(file_state.st_mode) or path.is_symlink():
            return "invalid"
        if expected_size is None and expected_digest is None:
            return "present"
        actual_size, actual_digest = restore_controller.artifact_identity(path)
        return "valid" if (actual_size, actual_digest) == (expected_size, expected_digest) else "invalid"
    except (OSError, restore_controller.RestoreError):
        return "invalid"


def _sqlite_status(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "absent"
    try:
        restore_controller.validate_sqlite_database(path, standalone=True)
        return "valid"
    except restore_controller.RestoreError:
        return "invalid"


def _canonical_identity(path: Path) -> tuple[int, str] | None:
    try:
        restore_controller.validate_sqlite_database(path, standalone=True)
        return restore_controller.artifact_identity(path)
    except restore_controller.RestoreError:
        return None


def _read_runtime_record(path: Path) -> Mapping[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate runtime record field")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid runtime record number: {value}")
            ),
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RecoveryPlanningError("runtime ownership record is invalid") from exc
    if not isinstance(payload, Mapping):
        raise RecoveryPlanningError("runtime ownership record is invalid")
    return payload


def _verified_process_snapshot(runtime_control: Any) -> dict[int, Any] | None:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return None
    snapshot: dict[int, Any] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        process = runtime_control.inspect_process(pid)
        if process is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                return None
            return None
        snapshot[pid] = process
    return snapshot


def _runtime_status(root: Path) -> str:
    try:
        from scratch import runtime_control

        record_states: list[str] = []
        for service in runtime_control.SERVICE_NAMES:
            record_path = layout_for_root(root).runtime / f"{service}.runtime.json"
            try:
                record_state = record_path.lstat()
            except FileNotFoundError:
                record_states.append("missing")
                continue
            if not stat.S_ISREG(record_state.st_mode) or record_path.is_symlink():
                return "unknown"
            try:
                record = _read_runtime_record(record_path)
                pid = record["pid"]
                start_ticks = record["start_ticks"]
            except (RecoveryPlanningError, KeyError):
                return "unknown"
            if (
                not isinstance(record, dict)
                or record.get("version") != 1
                or isinstance(pid, bool)
                or not isinstance(pid, int)
                or pid <= 0
                or not isinstance(start_ticks, str)
                or not start_ticks.isdigit()
                or record.get("root") != str(root)
                or record.get("service") != service
                or not isinstance(record.get("token"), str)
                or not record.get("token")
            ):
                return "unknown"
            process = runtime_control.inspect_process(pid)
            if process is None:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    record_states.append("stale")
                    continue
                except PermissionError:
                    return "unknown"
                except OSError:
                    return "unknown"
                return "unknown"
            if process.start_ticks != start_ticks:
                record_states.append("stale")
                continue
            if not runtime_control.record_matches(process, root, service, record):
                return "unknown"
            return "active"

        snapshot = _verified_process_snapshot(runtime_control)
        if snapshot is None:
            return "unknown"
        if runtime_control.discover_owned(snapshot, root):
            return "active"

        try:
            web_port = restore_controller._web_port(root)
        except restore_controller.RestoreError:
            return "unknown"
        if not all(runtime_control.port_is_available(port) for port in (8000, web_port)):
            return "unknown"
        if any(item not in {"missing", "stale"} for item in record_states):
            return "unknown"
        return "offline"
    except Exception:
        return "unknown"


def _restore_plan(
    root: Path,
    operation_id: str,
    generation: int,
    *,
    controller_status: str,
    runtime_status: str,
    expected_journal_generation: int | None = None,
) -> tuple[dict[str, str], bool | None, str, list[str]]:
    paths = restore_controller.restore_paths(root, operation_id)
    try:
        journal = restore_controller.inspect_restore_journal(root, operation_id)
    except restore_controller.RestoreError:
        return ({"journal": "invalid"}, None, "restore_evidence_invalid", [])
    journal_generation = (
        generation
        if expected_journal_generation is None
        else expected_journal_generation
    )
    if (
        journal.get("operation_id") != operation_id
        or journal.get("generation") != journal_generation
    ):
        return ({"journal": "invalid"}, None, "restore_evidence_invalid", [])

    source = paths.backup_dir / str(journal["candidate_filename"])
    try:
        source_evidence = backup_manifest.validate_backup_pair(source)
        source_status = (
            "valid"
            if (
                source_evidence.database.size_bytes,
                source_evidence.database.sha256,
            )
            == (journal["candidate_size"], journal["candidate_sha256"])
            else "invalid"
        )
    except backup_manifest.BackupManifestError:
        source_status = "invalid" if source.exists() or source.is_symlink() else "absent"

    artifacts = {
        "journal": "valid",
        "sourceBackup": source_status,
        "stagedCandidate": _path_status(
            paths.candidate_path,
            expected_size=journal.get("staged_size"),
            expected_digest=journal.get("staged_sha256"),
        ) if journal.get("staged_sha256") else _path_status(paths.candidate_path),
        "rollbackCheckpoint": _path_status(
            paths.rollback_path,
            expected_size=journal.get("rollback_size"),
            expected_digest=journal.get("rollback_sha256"),
        ) if journal.get("rollback_sha256") else _path_status(paths.rollback_path),
        "canonicalDatabase": "valid" if _canonical_identity(paths.database_path) else "invalid",
    }
    accepted = bool(journal["accepted"])
    reason = _sanitize_reason(journal.get("error_code"), "restore_recovery_required")
    if controller_status != "inactive" or runtime_status == "unknown":
        return artifacts, accepted, reason, []

    progress = restore_controller.RestoreProgress(journal["progress"])
    activation = restore_controller.ActivationState(journal["activation_state"])
    canonical = _canonical_identity(paths.database_path)
    staged_identity = (
        (journal.get("staged_size"), journal.get("staged_sha256"))
        if journal.get("staged_sha256")
        else None
    )
    previous_identity = (
        (journal.get("previous_size"), journal.get("previous_sha256"))
        if journal.get("previous_sha256")
        else None
    )
    rollback_identity = (
        (journal.get("rollback_activation_size"), journal.get("rollback_activation_sha256"))
        if journal.get("rollback_activation_sha256")
        else (journal.get("rollback_size"), journal.get("rollback_sha256"))
        if journal.get("rollback_sha256")
        else None
    )
    actions: list[str] = []
    early = progress in {
        restore_controller.RestoreProgress.CLAIMED,
        restore_controller.RestoreProgress.DRAINED,
        restore_controller.RestoreProgress.CONTROLLER_OWNED,
        restore_controller.RestoreProgress.RECOVERY_REQUIRED,
    }
    no_destructive_artifacts = (
        artifacts["stagedCandidate"] == "absent"
        and artifacts["rollbackCheckpoint"] == "absent"
        and activation is restore_controller.ActivationState.NOT_STARTED
    )
    # Completing authority over a surviving fenced backend could leave process-local
    # admission closed forever.  Restore abort is therefore offered only after the
    # supported runtime is positively offline.
    if (
        not accepted
        and early
        and no_destructive_artifacts
        and runtime_status == "offline"
    ):
        actions.append("safe_abort")
    recognized_canonical = canonical is not None and canonical in {
        item for item in (staged_identity, previous_identity, rollback_identity) if item is not None
    }
    installed_lineage = restore_controller.installed_database_is_recognized(paths, journal)
    if installed_lineage:
        artifacts["canonicalDatabase"] = "valid"
    recognized_canonical = recognized_canonical or installed_lineage
    if accepted:
        if installed_lineage:
            actions.append("continue_recovery")
    else:
        if source_status == "valid" and (
            (early and no_destructive_artifacts)
            or (recognized_canonical and artifacts["rollbackCheckpoint"] == "valid")
        ):
            actions.append("continue_recovery")
        if (
            artifacts["rollbackCheckpoint"] == "valid"
            and recognized_canonical
            and activation is restore_controller.ActivationState.NOT_STARTED
        ):
            actions.append("rollback")
    return artifacts, accepted, reason, sorted(set(actions))


def _git_commit_exists(root: Path, commit: Any) -> bool:
    if not isinstance(commit, str) or not update_controller.COMMIT_RE.fullmatch(commit):
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _installed_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and update_controller.COMMIT_RE.fullmatch(commit) else None


def _evidence_file_identity(path: Path) -> object:
    try:
        file_state = path.lstat()
        if not stat.S_ISREG(file_state.st_mode) or path.is_symlink():
            return "invalid"
        size, digest = restore_controller.artifact_identity(path)
        return [size, digest]
    except FileNotFoundError:
        return "absent"
    except (OSError, restore_controller.RestoreError):
        return "invalid"


def _durable_recovery_evidence(
    root: Path,
    operation_id: str,
    operation_type: state.OperationType,
    *,
    normalized_generation: int | None = None,
) -> str:
    """Hash exact private recovery facts without projecting those facts publicly."""

    if operation_type not in {state.OperationType.UPDATE, state.OperationType.RESTORE}:
        raise RecoveryPlanningConflict("operation recovery protocol is unavailable")
    try:
        if operation_type is state.OperationType.RESTORE:
            paths = restore_controller.restore_paths(root, operation_id)
            journal = dict(
                restore_controller.inspect_restore_journal(root, operation_id)
            )
            journal.pop("updated_at", None)
            if normalized_generation is not None:
                journal["generation"] = normalized_generation
            source = paths.backup_dir / str(journal["candidate_filename"])
            facts: Mapping[str, Any] = {
                "journal": journal,
                "source": _evidence_file_identity(source),
                "candidate": _evidence_file_identity(paths.candidate_path),
                "rollback": _evidence_file_identity(paths.rollback_path),
                "rollbackActivation": _evidence_file_identity(
                    paths.operation_dir / "rollback-activation.db"
                ),
                "canonical": _evidence_file_identity(paths.database_path),
                "canonicalSidecars": {
                    suffix: _evidence_file_identity(Path(f"{paths.database_path}{suffix}"))
                    for suffix in ("-wal", "-shm", "-journal")
                },
                "candidateLineage": _evidence_file_identity(paths.operation_dir / "candidate-lineage.json"),
                "rollbackLineage": _evidence_file_identity(paths.operation_dir / "rollback-lineage.json"),
            }
        else:
            journal_path = layout_for_root(root).durable_runtime / "update-state.json"
            journal = dict(update_controller.read_update_journal(journal_path))
            raw_journal_generation = journal.get("lifecycle_generation")
            if (
                isinstance(raw_journal_generation, bool)
                or not isinstance(raw_journal_generation, int)
                or raw_journal_generation <= 0
            ):
                raise ValueError("update journal generation is invalid")
            _, _, permit_path = update_controller._update_paths(
                root, operation_id, raw_journal_generation
            )
            journal.pop("updated_at", None)
            if normalized_generation is not None:
                journal["lifecycle_generation"] = normalized_generation
            raw_staging = journal.get("staging_dir")
            staging_commit: str | None = None
            staging_dirty: str | None = None
            if _safe_staging_status(root, raw_staging) == "valid":
                staging = Path(str(raw_staging))
                try:
                    staging_commit = subprocess.run(
                        ["git", "-C", str(staging), "rev-parse", "HEAD"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    ).stdout.strip()
                    staging_dirty = subprocess.run(
                        ["git", "-C", str(staging), "status", "--porcelain"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    ).stdout
                except (OSError, subprocess.SubprocessError):
                    staging_commit = "unknown"
                    staging_dirty = "unknown"
            facts = {
                "journal": journal,
                "installedCommit": _installed_commit(root),
                "checkpoint": _evidence_file_identity(
                    layout_for_root(root).durable_runtime / "pre-update-database.db"
                ),
                "permit": _evidence_file_identity(permit_path),
                "stagingCommit": staging_commit,
                "stagingDirty": staging_dirty,
            }
        encoded = json.dumps(
            facts, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, update_controller.UpdateControllerError, restore_controller.RestoreError):
        encoded = b"invalid"
    return hashlib.sha256(encoded).hexdigest()


def _safe_staging_status(root: Path, value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "absent"
    raw = Path(value)
    try:
        raw_state = raw.lstat()
        candidate = raw.resolve(strict=True)
        install_parent = root.resolve().parent
    except OSError:
        return "invalid"
    return (
        "valid"
        if stat.S_ISDIR(raw_state.st_mode)
        and not raw.is_symlink()
        and candidate.is_dir()
        and candidate.parent == install_parent
        and candidate.name.startswith(".streamhome-update.")
        else "invalid"
    )


def _queued_update_was_not_launched(root: Path, journal: Mapping[str, Any]) -> bool:
    """Recognize exact producer evidence for abandoning a queued claim only."""
    operation_id = journal.get("transaction_id")
    previous = journal.get("previous_commit")
    if (
        not isinstance(operation_id, str)
        or journal.get("phase") != "queued"
        or journal.get("runtime_committed") is not False
        or not isinstance(previous, str)
        or not update_controller.COMMIT_RE.fullmatch(previous)
        or journal.get("current_commit") != previous
        or journal.get("started_at") is not None
        or journal.get("controller_pid") is not None
        or journal.get("controller_start_ticks") not in (None, "")
        or journal.get("staging_dir") not in (None, "")
        or journal.get("web_artifacts_swapped") is not False
        or journal.get("web_dependencies_swapped") is not False
    ):
        return False
    return all(
        _path_is_absent(layout_for_root(root).durable_runtime / name)
        for name in (
            f"update-controller.{operation_id}.sh",
            "pre-update-database.db",
            "update-transaction.json",
        )
    )


def _update_plan(
    root: Path,
    operation_id: str,
    generation: int,
    authority: state.LifecycleState,
    *,
    controller_status: str,
    runtime_status: str,
    journal_authority_generation: int | None = None,
) -> tuple[dict[str, str], bool | None, str, list[str]]:
    journal_path, _, _ = update_controller._update_paths(root, operation_id, generation)
    try:
        journal = update_controller.read_update_journal(journal_path)
        journal_authority = authority
        if journal_authority_generation is not None:
            journal_authority = state.LifecycleState(
                authority.schema_version,
                journal_authority_generation,
                authority.active_operation,
            )
        previous = update_controller.pre_cutover_previous_commit(root, journal, journal_authority)
        if previous is not None:
            journal["previous_commit"] = previous
        update_controller._validate_update_journal_semantics(
            journal, authority=journal_authority
        )
    except update_controller.UpdateControllerError:
        return ({"journal": "invalid"}, None, "update_evidence_invalid", [])

    checkpoint = layout_for_root(root).durable_runtime / "pre-update-database.db"
    artifacts = {
        "journal": "valid",
        "databaseCheckpoint": _sqlite_status(checkpoint),
        "staging": _safe_staging_status(root, journal.get("staging_dir")),
        "targetCommit": "valid" if _git_commit_exists(root, journal.get("target_commit")) else "invalid",
        "previousCommit": "valid" if _git_commit_exists(root, journal.get("previous_commit")) else "invalid",
    }
    committed = bool(journal.get("runtime_committed"))
    reason = _sanitize_reason(journal.get("error"), "update_recovery_required")
    if controller_status != "inactive" or runtime_status == "unknown":
        return artifacts, committed, reason, []
    installed = _installed_commit(root)
    target = journal.get("target_commit")
    previous = journal.get("previous_commit")
    detail = str(journal.get("phase") or "")
    actions: list[str] = []
    if committed:
        if artifacts["targetCommit"] == "valid" and installed == target:
            actions.append("continue_recovery")
    else:
        if (
            detail in {"queued", "preflight", "waiting_for_idle", "failed"}
            and installed == previous
            and artifacts["databaseCheckpoint"] == "absent"
            and runtime_status == "offline"
        ):
            actions.append("safe_abort")
        elif (
            installed is not None
            and _git_commit_exists(root, installed)
            and artifacts["targetCommit"] == "valid"
            and artifacts["previousCommit"] == "valid"
            and runtime_status == "offline"
            and _queued_update_was_not_launched(root, journal)
        ):
            # The owner may have installed a different release after queueing.
            # An explicit abort releases only this unlaunched claim; it neither
            # restores the old Git HEAD nor starts the selected target.
            actions.append("safe_abort")
        if (
            detail in {"starting", "recovering"}
            and installed == target
            and artifacts["targetCommit"] == "valid"
        ):
            actions.append("continue_recovery")
        # Phase 5 does not persist an operation/generation-bound identity for
        # pre-update-database.db. SQLite validity (or absence) cannot prove that
        # checkpoint belongs to this transaction, so the planner never advertises
        # update rollback as an evidence-supported operator action.
    return artifacts, committed, reason, sorted(set(actions))


def _projection_with_evidence(
    *,
    operation_id: str,
    operation_type: str,
    generation: int,
    controller_status: str,
    runtime_status: str,
    reason: str,
    boundary_name: str,
    boundary: bool | None,
    artifacts: Mapping[str, str],
    actions: Sequence[str],
    durable_evidence: str,
) -> dict[str, Any]:
    projection = {
        "schemaVersion": 1,
        "operationId": operation_id,
        "operationReference": operation_id[:12],
        "operationType": operation_type,
        "generation": generation,
        "lifecyclePhase": state.LifecyclePhase.RECOVERY_REQUIRED.value,
        "controllerActive": controller_status == "active",
        "controllerStatus": controller_status,
        "runtimeStatus": runtime_status,
        "reasonCode": reason,
        boundary_name: boundary,
        "artifacts": dict(artifacts),
        "operatorActionRequired": True,
        "permittedActions": list(actions),
        "readOnly": True,
        "cleanupEligible": False,
    }
    evidence_payload = json.dumps(
        {
            "operationId": operation_id,
            "generation": generation,
            "operationType": operation_type,
            "reasonCode": reason,
            "boundary": boundary,
            "artifacts": artifacts,
            "permittedActions": list(actions),
            "controllerStatus": controller_status,
            "runtimeStatus": runtime_status,
            "durableEvidence": durable_evidence,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    projection["evidenceIdentity"] = (
        "plan-v1:" + hashlib.sha256(evidence_payload).hexdigest()[:32]
    )
    return projection


def _plan_recovery_exact(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    controller_status_override: str | None = None,
) -> dict[str, Any]:
    installation = state.resolve_installation_root(root)
    canonical_id = _canonical_operation_id(operation_id)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise RecoveryPlanningConflict("operation generation is invalid")
    authority = state.inspect_state(installation)
    operation = authority.active_operation
    if (
        operation is None
        or operation.operation_id != canonical_id
        or authority.generation != generation
        or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
    ):
        raise RecoveryPlanningConflict("exact recovery-required lifecycle authority does not match")
    if operation.operation_type is state.OperationType.MANAGEMENT:
        raise RecoveryPlanningConflict("management recovery requires its qualified controller")
    controller_status = (
        controller_status_override
        if controller_status_override is not None
        else _controller_status(installation, canonical_id, generation)
    )
    runtime_status = _runtime_status(installation)
    durable_evidence = _durable_recovery_evidence(
        installation, canonical_id, operation.operation_type
    )
    interrupted, interrupted_ambiguous = _interrupted_planned_action(
        installation,
        canonical_id,
        generation,
        operation.operation_type.value,
    )
    journal_generation = generation
    if interrupted is not None:
        if operation.operation_type is state.OperationType.RESTORE:
            try:
                journal_generation = int(
                    restore_controller.inspect_restore_journal(
                        installation, canonical_id
                    )["generation"]
                )
            except (KeyError, TypeError, ValueError, restore_controller.RestoreError):
                journal_generation = -1
        else:
            journal_path, _, _ = update_controller._update_paths(
                installation, canonical_id, generation
            )
            try:
                raw_generation = update_controller.read_update_journal(
                    journal_path
                ).get("lifecycle_generation")
                journal_generation = (
                    raw_generation
                    if isinstance(raw_generation, int)
                    and not isinstance(raw_generation, bool)
                    else -1
                )
            except update_controller.UpdateControllerError:
                journal_generation = -1
        if journal_generation not in {interrupted.generation, generation} or (
            interrupted.status == "failed"
            and journal_generation != interrupted.generation
        ):
            interrupted_ambiguous = True
    if operation.operation_type is state.OperationType.RESTORE:
        artifacts, boundary, reason, actions = _restore_plan(
            installation,
            canonical_id,
            generation,
            controller_status=controller_status,
            runtime_status=runtime_status,
            expected_journal_generation=journal_generation,
        )
        boundary_name = "accepted"
    else:
        artifacts, boundary, reason, actions = _update_plan(
            installation,
            canonical_id,
            generation,
            authority,
            controller_status=controller_status,
            runtime_status=runtime_status,
            journal_authority_generation=journal_generation,
        )
        boundary_name = "runtimeCommitted"
    if interrupted is not None and not interrupted_ambiguous:
        prior_durable_evidence = _durable_recovery_evidence(
            installation,
            canonical_id,
            operation.operation_type,
            normalized_generation=interrupted.generation,
        )
        prior = _projection_with_evidence(
            operation_id=canonical_id,
            operation_type=operation.operation_type.value,
            generation=interrupted.generation,
            controller_status=controller_status,
            runtime_status=runtime_status,
            reason=reason,
            boundary_name=boundary_name,
            boundary=boundary,
            artifacts=artifacts,
            actions=actions,
            durable_evidence=prior_durable_evidence,
        )
        if (
            prior["evidenceIdentity"] != interrupted.evidence_identity
            or interrupted.requested_action not in actions
        ):
            interrupted_ambiguous = True
        else:
            actions = [interrupted.requested_action]
    if interrupted_ambiguous:
        actions = []
        reason = "operator_action_evidence_invalid"
    return _projection_with_evidence(
        operation_id=canonical_id,
        operation_type=operation.operation_type.value,
        generation=generation,
        controller_status=controller_status,
        runtime_status=runtime_status,
        reason=reason,
        boundary_name=boundary_name,
        boundary=boundary,
        artifacts=artifacts,
        actions=actions,
        durable_evidence=durable_evidence,
    )


def plan_recovery(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
) -> dict[str, Any]:
    """Return a sanitized, read-only plan for one exact recovery-required operation."""

    return _plan_recovery_exact(
        root,
        operation_id=operation_id,
        generation=generation,
    )


def classify_retained_artifacts(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
) -> dict[str, Any]:
    """Classify retention without deleting, acknowledging, or changing authority."""

    installation = state.resolve_installation_root(root)
    canonical_id = _canonical_operation_id(operation_id)
    try:
        authority = state.inspect_state(installation)
    except state.LifecycleError:
        return {"classification": "ambiguous", "cleanupEligible": False}
    operation = authority.active_operation
    if operation is not None:
        if operation.operation_id != canonical_id:
            return {"classification": "ambiguous", "cleanupEligible": False}
        return {
            "classification": (
                "recovery_required"
                if operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED
                else "active_unresolved"
            ),
            "cleanupEligible": False,
        }
    try:
        journal = restore_controller.inspect_restore_journal(installation, canonical_id)
        terminal = journal["progress"] in {
            restore_controller.RestoreProgress.SUCCEEDED.value,
            restore_controller.RestoreProgress.ROLLED_BACK.value,
        }
        journal_generation = journal.get("generation")
        resolved = bool(
            terminal
            and isinstance(journal_generation, int)
            and not isinstance(journal_generation, bool)
            and 0 < journal_generation <= authority.generation
        )
        return {
            "classification": (
                "stale_resolved"
                if resolved and journal_generation < authority.generation
                else "resolved_terminal"
                if resolved
                else "ambiguous"
            ),
            "cleanupEligible": resolved,
        }
    except restore_controller.RestoreError:
        pass
    journal_path = layout_for_root(installation).durable_runtime / "update-state.json"
    try:
        payload = update_controller.read_update_journal(journal_path)
        terminal = _terminal_update_evidence(payload, canonical_id)
        journal_generation = payload.get("lifecycle_generation")
        resolved = bool(terminal and journal_generation <= authority.generation)
        return {
            "classification": (
                "stale_resolved"
                if resolved and journal_generation < authority.generation
                else "resolved_terminal"
                if resolved
                else "ambiguous"
            ),
            "cleanupEligible": resolved,
        }
    except update_controller.UpdateControllerError:
        return {"classification": "ambiguous", "cleanupEligible": False}


def _terminal_update_evidence(payload: Mapping[str, Any], operation_id: str) -> bool:
    """Recognize only self-consistent terminal update evidence after authority release."""

    generation = payload.get("lifecycle_generation")
    phase = payload.get("phase")
    target = payload.get("target_commit")
    previous = payload.get("previous_commit")
    finished_at = payload.get("finished_at")
    if (
        payload.get("transaction_id") != operation_id
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or phase not in {"succeeded", "rolled_back"}
        or not isinstance(target, str)
        or not update_controller.COMMIT_RE.fullmatch(target)
        or not isinstance(previous, str)
        or not update_controller.COMMIT_RE.fullmatch(previous)
        or isinstance(finished_at, bool)
        or not isinstance(finished_at, (int, float))
    ):
        return False
    committed = payload.get("runtime_committed")
    if phase == "succeeded":
        committed_at = payload.get("runtime_committed_at")
        return bool(
            committed is True
            and not isinstance(committed_at, bool)
            and isinstance(committed_at, (int, float))
        )
    return bool(
        committed is False
        and payload.get("failed_target") == target
        and payload.get("runtime_committed_at") is None
    )


def _verified_update_exit_twelve(
    root: Path, operation_id: str, controller_generation: int
) -> bool:
    """Recognize only the updater's exact already-reconciled success contract."""

    try:
        authority = state.inspect_state(root)
        journal = update_controller.read_update_journal(layout_for_root(root).durable_runtime / "update-state.json")
        if (
            authority.active_operation is not None
            or authority.generation != controller_generation
            or journal.get("transaction_id") != operation_id
            or journal.get("lifecycle_generation") != controller_generation
            or not _terminal_update_evidence(journal, operation_id)
            or _controller_status(root, operation_id, controller_generation) != "inactive"
        ):
            return False
        installed = _installed_commit(root)
        if journal["phase"] == "succeeded":
            return journal["runtime_committed"] is True and installed == journal["target_commit"]
        return journal["runtime_committed"] is False and installed == journal["previous_commit"]
    except (state.LifecycleError, update_controller.UpdateControllerError, OSError):
        return False


def _ensure_recovery_required(root: Path, operation_id: str) -> None:
    """Return a still-owned failed action to the automatic-recovery terminal phase."""

    try:
        current = state.inspect_state(root)
        operation = current.active_operation
        if operation is None or operation.operation_id != operation_id:
            return
        if operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
            return
        state.transition_operation_with_credential(
            root,
            operation_id=operation_id,
            generation=current.generation,
            expected_phase=operation.phase,
            next_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
        )
    except state.LifecycleError:
        return


def _unlink_exact_regular(path: Path) -> bool:
    try:
        file_state = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(file_state.st_mode) or path.is_symlink():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _path_is_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _cleanup_restore_resolution(
    root: Path,
    operation_id: str,
    controller_generation: int,
    action: str,
) -> bool:
    paths = restore_controller.restore_paths(root, operation_id)
    try:
        journal = restore_controller.inspect_restore_journal(root, operation_id)
    except restore_controller.RestoreError:
        return False
    if journal.get("generation") != controller_generation:
        return False
    if action == "safe_abort":
        if (
            journal.get("accepted")
            or journal.get("activation_state")
            != restore_controller.ActivationState.NOT_STARTED.value
            or journal.get("staged_sha256")
            or journal.get("rollback_sha256")
        ):
            return False
        try:
            entries = tuple(paths.operation_dir.iterdir())
        except OSError:
            return False
        allowed = {paths.journal_path.name, paths.journal_lock_path.name}
        if any(entry.name not in allowed for entry in entries):
            return False
        if not all(_unlink_exact_regular(entry) for entry in entries):
            return False
        try:
            paths.operation_dir.rmdir()
            _fsync_directory(paths.operation_dir.parent)
            return True
        except OSError:
            return False
    classification = classify_retained_artifacts(root, operation_id=operation_id)
    if not classification.get("cleanupEligible"):
        return False
    if journal.get("progress") not in {
        restore_controller.RestoreProgress.SUCCEEDED.value,
        restore_controller.RestoreProgress.ROLLED_BACK.value,
    }:
        return False
    transient = (
        paths.candidate_path,
        paths.rollback_path,
        paths.operation_dir / "rollback-activation.db",
        paths.operation_dir / "completion.db",
    )
    if not all(_unlink_exact_regular(path) for path in transient):
        return False
    try:
        _fsync_directory(paths.operation_dir)
        return True
    except OSError:
        return False


def _cleanup_update_resolution(
    root: Path,
    operation_id: str,
    controller_generation: int,
    action: str,
) -> bool:
    journal_path, _, permit_path = update_controller._update_paths(
        root, operation_id, controller_generation
    )
    try:
        payload = update_controller.read_update_journal(journal_path)
    except update_controller.UpdateControllerError:
        return False
    if (
        payload.get("transaction_id") != operation_id
        or payload.get("lifecycle_generation") != controller_generation
    ):
        return False
    if action == "safe_abort":
        if payload.get("runtime_committed") is True or payload.get("phase") != "failed":
            return False
    elif not _terminal_update_evidence(payload, operation_id):
        return False
    # Cleanup can be retried much later than the operator action. Qualify the
    # current mounts containing every deletion target, not the earlier plan's.
    run_dir = layout_for_root(root).durable_runtime
    lifecycle_dir = state.lifecycle_paths(root).lifecycle_dir
    state.qualify_local_filesystem(root, require_private=False, description="installation directory")
    state.qualify_local_filesystem(run_dir, require_private=True, description="update artifact directory")
    state.qualify_local_filesystem(lifecycle_dir, require_private=True, description="lifecycle artifact directory")
    state.qualify_local_filesystem(root.parent, require_private=False, description="update staging parent")
    raw_staging = payload.get("staging_dir")
    if raw_staging is not None and not isinstance(raw_staging, str):
        return False
    staging_path: Path | None = None
    staging_identity: tuple[int, int] | None = None
    if raw_staging:
        candidate = Path(raw_staging)
        try:
            install_parent = root.resolve(strict=True).parent
        except (OSError, RuntimeError):
            return False
        if (
            not candidate.is_absolute()
            or candidate.parent != install_parent
            or not candidate.name.startswith(".streamhome-update.")
        ):
            return False
        try:
            candidate_state = candidate.lstat()
        except FileNotFoundError:
            pass  # A prior partial cleanup may already have removed staging.
        except OSError:
            return False
        else:
            if (
                not stat.S_ISDIR(candidate_state.st_mode)
                or candidate.is_symlink()
                or _safe_staging_status(root, raw_staging) != "valid"
            ):
                return False
            state.qualify_local_filesystem(
                candidate, require_private=False, description="update staging directory"
            )
            try:
                qualified_state = candidate.lstat()
            except OSError:
                return False
            if (
                _safe_staging_status(root, raw_staging) != "valid"
                or (qualified_state.st_dev, qualified_state.st_ino)
                != (candidate_state.st_dev, candidate_state.st_ino)
            ):
                return False
            staging_path = candidate
            staging_identity = (qualified_state.st_dev, qualified_state.st_ino)
    paths = [
        permit_path,
        layout_for_root(root).durable_runtime / f"update-controller.{operation_id}.sh",
        layout_for_root(root).durable_runtime / f"update-cancel.{operation_id}.requested",
        layout_for_root(root).durable_runtime / f"update-recovery.{operation_id}.requested",
        layout_for_root(root).durable_runtime / f"update-recovery.{operation_id[:12]}.requested",
    ]
    checkpoint = layout_for_root(root).durable_runtime / "pre-update-database.db"
    if action == "safe_abort":
        if not _path_is_absent(checkpoint):
            return False
    if action != "safe_abort":
        paths.append(checkpoint)
    # Finish validating the complete cleanup set before the first deletion.
    existing_paths: list[Path] = []
    for path in paths:
        if path.parent != (lifecycle_dir if path == permit_path else run_dir):
            return False
        try:
            artifact_state = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if not stat.S_ISREG(artifact_state.st_mode) or path.is_symlink():
            return False
        existing_paths.append(path)
    if staging_path is not None:
        try:
            current_staging = staging_path.lstat()
        except OSError:
            return False
        if (
            staging_identity != (current_staging.st_dev, current_staging.st_ino)
            or _safe_staging_status(root, raw_staging) != "valid"
        ):
            return False
        try:
            shutil.rmtree(staging_path)
            _fsync_directory(staging_path.parent)
        except OSError:
            return False
    for path in existing_paths:
        try:
            path.unlink()
        except OSError:
            return False
    try:
        _fsync_directory(layout_for_root(root).durable_runtime)
        return True
    except OSError:
        return False


def _cleanup_after_resolution(
    root: Path,
    operation_id: str,
    operation_type: str,
    controller_generation: int,
    action: str,
) -> bool:
    if operation_type not in {state.OperationType.UPDATE.value, state.OperationType.RESTORE.value}:
        return False
    try:
        with state.hold_idle_authority(root):
            if _controller_status(root, operation_id, controller_generation) != "inactive":
                return False
            if operation_type == state.OperationType.RESTORE.value:
                return _cleanup_restore_resolution(
                    root, operation_id, controller_generation, action
                )
            return _cleanup_update_resolution(
                root, operation_id, controller_generation, action
            )
    except state.LifecycleError:
        return False


def _find_action_record(
    root: Path,
    operation_id: str,
    controller_generation: int,
) -> tuple[Path, OperatorActionRecord]:
    operation_dir = (
        state.lifecycle_paths(root).lifecycle_dir
        / "operator-actions"
        / operation_id
    )
    try:
        directory_state = operation_dir.lstat()
        if not stat.S_ISDIR(directory_state.st_mode) or operation_dir.is_symlink():
            raise RecoveryPlanningConflict("operator action evidence is not trusted")
        candidates: list[tuple[Path, OperatorActionRecord]] = []
        for path in operation_dir.glob("action.*.json"):
            record = read_operator_action_record(path)
            if (
                record.operation_id == operation_id
                and record.controller_generation == controller_generation
            ):
                candidates.append((path, record))
    except RecoveryPlanningError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise RecoveryPlanningConflict("operator action evidence is unavailable") from exc
    if len(candidates) != 1:
        raise RecoveryPlanningConflict("exact operator action evidence is ambiguous")
    return candidates[0]


def _resolution_is_exact(
    root: Path,
    record: OperatorActionRecord,
    controller_generation: int,
) -> bool:
    authority = state.inspect_state(root)
    if authority.active_operation is not None:
        return False
    if record.operation_type == state.OperationType.RESTORE.value:
        try:
            journal = restore_controller.inspect_restore_journal(root, record.operation_id)
        except restore_controller.RestoreError:
            return False
        if journal.get("generation") != controller_generation:
            return False
        if record.requested_action == "safe_abort":
            return bool(
                not journal.get("accepted")
                and journal.get("activation_state")
                == restore_controller.ActivationState.NOT_STARTED.value
                and not journal.get("staged_sha256")
                and not journal.get("rollback_sha256")
            )
        if journal.get("progress") == restore_controller.RestoreProgress.NORMAL_RECOVERY.value:
            if not restore_controller.reconcile_restore_completion_intent(
                root, record.operation_id, controller_generation
            ):
                return False
            journal = restore_controller.inspect_restore_journal(root, record.operation_id)
        return journal.get("progress") in {
            restore_controller.RestoreProgress.SUCCEEDED.value,
            restore_controller.RestoreProgress.ROLLED_BACK.value,
        }
    journal_path, _, _ = update_controller._update_paths(
        root, record.operation_id, controller_generation
    )
    try:
        payload = update_controller.read_update_journal(journal_path)
    except update_controller.UpdateControllerError:
        return False
    if (
        payload.get("transaction_id") != record.operation_id
        or payload.get("lifecycle_generation") != controller_generation
    ):
        return False
    if record.requested_action == "safe_abort":
        return bool(
            payload.get("runtime_committed") is False
            and payload.get("phase") == "failed"
        )
    return _terminal_update_evidence(payload, record.operation_id)


def cleanup_resolved_recovery(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
) -> dict[str, Any]:
    """Retry exact post-resolution cleanup without changing lifecycle authority."""

    installation = state.resolve_installation_root(root)
    canonical_id = _canonical_operation_id(operation_id)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise RecoveryPlanningConflict("controller generation is invalid")
    state.qualify_lifecycle_filesystem(installation)
    path, record = _find_action_record(installation, canonical_id, generation)
    paths = OperatorActionPaths(path.parent, path.parent / "action.lock", path)
    with _operator_action_lock(paths):
        record = read_operator_action_record(path)
        if (
            record.operation_id != canonical_id
            or record.controller_generation != generation
            or record.status not in {"executing", "failed", "succeeded"}
        ):
            raise RecoveryPlanningConflict("operator cleanup evidence does not match")
        if record.status == "succeeded" and record.result in {
            "operator_recovery_completed",
            "operator_safe_abort_completed",
        }:
            return {
                "operationReference": canonical_id[:12],
                "controllerGeneration": generation,
                "status": "succeeded",
                "resultCode": record.result,
                "cleanupCompleted": True,
            }
        if (
            record.status == "succeeded"
            and record.result == "operator_cleanup_pending"
            and record.operation_type == state.OperationType.RESTORE.value
            and record.requested_action == "safe_abort"
            and _path_is_absent(
                restore_controller.restore_paths(
                    installation, canonical_id
                ).operation_dir
            )
        ):
            record = _replace_action_record(
                path,
                record,
                status="succeeded",
                controller_generation=generation,
                result="operator_safe_abort_completed",
            )
            return {
                "operationReference": canonical_id[:12],
                "controllerGeneration": generation,
                "status": "succeeded",
                "resultCode": record.result,
                "cleanupCompleted": True,
            }
        if not _resolution_is_exact(installation, record, generation):
            raise RecoveryPlanningConflict("operator action is not authoritatively resolved")
        cleanup_complete = _cleanup_after_resolution(
            installation,
            canonical_id,
            record.operation_type,
            generation,
            record.requested_action,
        )
        if record.status != "succeeded":
            record = _replace_action_record(
                path,
                record,
                status="succeeded",
                controller_generation=generation,
                result="operator_cleanup_pending",
            )
        if cleanup_complete:
            record = _replace_action_record(
                path,
                record,
                status="succeeded",
                controller_generation=generation,
                result=(
                    "operator_safe_abort_completed"
                    if record.requested_action == "safe_abort"
                    else "operator_recovery_completed"
                ),
            )
        return {
            "operationReference": canonical_id[:12],
            "controllerGeneration": generation,
            "status": "succeeded",
            "resultCode": record.result,
            "cleanupCompleted": cleanup_complete,
        }


def _execution_result_code(exc: BaseException) -> str:
    if isinstance(exc, RecoveryEvidenceStale):
        return "operator_evidence_stale"
    if isinstance(exc, state.ControllerLockError):
        return "operator_controller_conflict"
    if isinstance(exc, state.UnsupportedFilesystemError):
        return "operator_filesystem_unsupported"
    return "operator_execution_failed"


def execute_recovery(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
    action: str,
    evidence_identity: str,
) -> dict[str, Any]:
    """Execute exactly one fresh-plan-supported operator recovery action."""

    installation = state.resolve_installation_root(root)
    canonical_id = _canonical_operation_id(operation_id)
    if action not in ACTION_IDENTIFIERS:
        raise RecoveryPlanningConflict("operator action is unsupported")
    if not isinstance(evidence_identity, str) or not EVIDENCE_ID_RE.fullmatch(
        evidence_identity
    ):
        raise RecoveryPlanningConflict("operator evidence identity is invalid")
    state.qualify_lifecycle_filesystem(installation)
    initial_plan = plan_recovery(
        installation,
        operation_id=canonical_id,
        generation=generation,
    )
    if (
        initial_plan["evidenceIdentity"] != evidence_identity
        or action not in initial_plan["permittedActions"]
    ):
        raise RecoveryEvidenceStale("operator plan is stale or does not permit the action")
    paths = _new_action_paths(installation, canonical_id, generation)
    record: OperatorActionRecord | None = None
    transferred_generation: int | None = None
    with _operator_action_lock(paths):
        fresh_plan = plan_recovery(
            installation,
            operation_id=canonical_id,
            generation=generation,
        )
        if (
            fresh_plan["evidenceIdentity"] != evidence_identity
            or action not in fresh_plan["permittedActions"]
        ):
            raise RecoveryEvidenceStale("operator plan changed before intent persistence")
        record = OperatorActionRecord(
            ACTION_RECORD_SCHEMA_VERSION,
            canonical_id,
            generation,
            str(fresh_plan["operationType"]),
            action,
            _timestamp(),
            evidence_identity,
            "planned",
            None,
            None,
        )
        _write_action_record(paths.record_path, record)

        def pre_transfer_check() -> None:
            current = _plan_recovery_exact(
                installation,
                operation_id=canonical_id,
                generation=generation,
                controller_status_override="inactive",
            )
            if (
                current["evidenceIdentity"] != evidence_identity
                or action not in current["permittedActions"]
            ):
                raise RecoveryEvidenceStale(
                    "operator evidence changed before ownership transfer"
                )

        def on_transfer(next_generation: int) -> None:
            nonlocal record, transferred_generation
            transferred_generation = next_generation
            assert record is not None
            record = _replace_action_record(
                paths.record_path,
                record,
                status="executing",
                controller_generation=next_generation,
            )

        try:
            if record.operation_type == state.OperationType.RESTORE.value:
                capability = state.load_controller_capability(
                    installation,
                    operation_id=canonical_id,
                    generation=generation,
                )
                try:
                    restore_journal_generation = int(
                        restore_controller.inspect_restore_journal(
                            installation, canonical_id
                        )["generation"]
                    )
                except (KeyError, TypeError, ValueError, restore_controller.RestoreError) as exc:
                    raise RecoveryEvidenceStale(
                        "restore journal generation is unavailable"
                    ) from exc
                return_code = restore_controller.run_operator_recovery(
                    installation,
                    operation_id=canonical_id,
                    generation=generation,
                    owner_capability=capability,
                    action=action,
                    journal_generation=restore_journal_generation,
                    pre_transfer_check=pre_transfer_check,
                    on_transfer=on_transfer,
                )
            else:
                update_journal_path, _, _ = update_controller._update_paths(
                    installation, canonical_id, generation
                )
                try:
                    raw_update_generation = update_controller.read_update_journal(
                        update_journal_path
                    ).get("lifecycle_generation")
                except update_controller.UpdateControllerError as exc:
                    raise RecoveryEvidenceStale(
                        "update journal generation is unavailable"
                    ) from exc
                if (
                    isinstance(raw_update_generation, bool)
                    or not isinstance(raw_update_generation, int)
                ):
                    raise RecoveryEvidenceStale(
                        "update journal generation is unavailable"
                    )
                return_code = update_controller.run_operator_recovery(
                    installation,
                    operation_id=canonical_id,
                    generation=generation,
                    action=action,
                    journal_generation=raw_update_generation,
                    pre_transfer_check=pre_transfer_check,
                    on_transfer=on_transfer,
                )
            current_authority = state.inspect_state(installation)
            current_operation = current_authority.active_operation
            if (
                return_code not in {0, 12}
                or (
                    return_code == 12
                    and (
                        record.operation_type != state.OperationType.UPDATE.value
                        or transferred_generation is None
                        or not _verified_update_exit_twelve(
                            installation, canonical_id, transferred_generation
                        )
                    )
                )
            ) or (
                current_operation is not None
                and current_operation.operation_id == canonical_id
            ):
                raise RecoveryExecutionError("operator recovery did not resolve authority")
            if transferred_generation is None:
                raise RecoveryExecutionError("operator controller ownership was not transferred")
            record = _replace_action_record(
                paths.record_path,
                record,
                status="succeeded",
                controller_generation=transferred_generation,
                result="operator_cleanup_pending",
            )
            cleanup_complete = bool(
                _resolution_is_exact(installation, record, transferred_generation)
                and _cleanup_after_resolution(
                    installation,
                    canonical_id,
                    record.operation_type,
                    transferred_generation,
                    action,
                )
            )
            if cleanup_complete:
                record = _replace_action_record(
                    paths.record_path,
                    record,
                    status="succeeded",
                    controller_generation=transferred_generation,
                    result=(
                        "operator_safe_abort_completed"
                        if action == "safe_abort"
                        else "operator_recovery_completed"
                    ),
                )
            return {
                "operationReference": canonical_id[:12],
                "operationType": record.operation_type,
                "requestGeneration": generation,
                "controllerGeneration": transferred_generation,
                "requestedAction": action,
                "status": "succeeded",
                "resultCode": record.result,
                "cleanupCompleted": cleanup_complete,
            }
        except Exception as exc:
            _ensure_recovery_required(installation, canonical_id)
            if record is not None and record.status in {"planned", "executing"}:
                try:
                    record = _replace_action_record(
                        paths.record_path,
                        record,
                        status="failed",
                        controller_generation=transferred_generation,
                        result=_execution_result_code(exc),
                    )
                except RecoveryPlanningError:
                    pass
            if isinstance(exc, RecoveryPlanningError):
                raise
            if isinstance(exc, (state.LifecycleError, restore_controller.RestoreError, update_controller.UpdateControllerError)):
                raise RecoveryExecutionError("operator recovery failed closed") from exc
            raise RecoveryExecutionError("operator recovery failed closed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamhome-lifecycle-recovery")
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--operation-id", required=True)
    plan.add_argument("--generation", required=True, type=int)
    execute = commands.add_parser("execute")
    execute.add_argument("--operation-id", required=True)
    execute.add_argument("--generation", required=True, type=int)
    execute.add_argument("--action", required=True, choices=sorted(ACTION_IDENTIFIERS))
    execute.add_argument("--evidence-identity", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("--operation-id", required=True)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--operation-id", required=True)
    cleanup.add_argument("--generation", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "plan":
            payload = plan_recovery(
                arguments.root,
                operation_id=arguments.operation_id,
                generation=arguments.generation,
            )
        elif arguments.command == "execute":
            payload = execute_recovery(
                arguments.root,
                operation_id=arguments.operation_id,
                generation=arguments.generation,
                action=arguments.action,
                evidence_identity=arguments.evidence_identity,
            )
        elif arguments.command == "classify":
            payload = classify_retained_artifacts(
                arguments.root,
                operation_id=arguments.operation_id,
            )
        else:
            payload = cleanup_resolved_recovery(
                arguments.root,
                operation_id=arguments.operation_id,
                generation=arguments.generation,
            )
        sys.stdout.write(json.dumps({"ok": True, **payload}, sort_keys=True) + "\n")
        return 0
    except (RecoveryPlanningError, state.LifecycleError, ValueError) as exc:
        code = getattr(exc, "code", "recovery_plan_unavailable")
        sys.stderr.write(json.dumps({"ok": False, "error": code}, sort_keys=True) + "\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
