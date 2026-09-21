"""Read-only operator recovery evidence and planning.

Phase 6A deliberately exposes no transition, takeover, cleanup, or execution API.
The shared lifecycle authority and operation-specific journals remain authoritative.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Mapping, Sequence
import uuid

from . import backup_manifest, restore_controller, state, update_controller


ACTION_RECORD_SCHEMA_VERSION = 1
MAX_ACTION_RECORD_BYTES = 8 * 1024
ACTION_IDENTIFIERS = frozenset({"safe_abort", "continue_recovery", "rollback"})
ACTION_RECORD_STATUSES = frozenset({"planned", "executing", "succeeded", "failed"})
EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,80}$")


class RecoveryPlanningError(RuntimeError):
    code = "recovery_plan_unavailable"


class RecoveryPlanningConflict(RecoveryPlanningError):
    code = "recovery_plan_conflict"


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
    result: str | None


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
    """Validate the future Phase 6B record without creating or executing it."""

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
    if operation_type not in {item.value for item in state.OperationType}:
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
    result = payload["result"]
    if result is not None and (
        not isinstance(result, str) or not ERROR_CODE_RE.fullmatch(result)
    ):
        raise RecoveryPlanningError("operator action result is invalid")
    if status_value in {"planned", "executing"} and result is not None:
        raise RecoveryPlanningError("unfinished operator action cannot have a result")
    if status_value in {"succeeded", "failed"} and result is None:
        raise RecoveryPlanningError("finished operator action requires a result")
    return OperatorActionRecord(
        ACTION_RECORD_SCHEMA_VERSION,
        operation_id,
        generation,
        operation_type,
        action,
        _validate_timestamp(payload["created_at"]),
        evidence_identity,
        status_value,
        result,
    )


def parse_operator_action_json(raw: str | bytes) -> OperatorActionRecord:
    """Strictly decode the future durable action record without persisting it."""

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
            record_path = root / ".run" / f"{service}.runtime.json"
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
) -> tuple[dict[str, str], bool | None, str, list[str]]:
    paths = restore_controller.restore_paths(root, operation_id)
    try:
        journal = restore_controller.inspect_restore_journal(root, operation_id)
    except restore_controller.RestoreError:
        return ({"journal": "invalid"}, None, "restore_evidence_invalid", [])
    if journal.get("operation_id") != operation_id or journal.get("generation") != generation:
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
    if not accepted and early and no_destructive_artifacts:
        actions.append("safe_abort")
    recognized_canonical = canonical is not None and canonical in {
        item for item in (staged_identity, previous_identity, rollback_identity) if item is not None
    }
    if accepted:
        expected = rollback_identity if journal.get("outcome") == "rolled_back" else staged_identity
        if canonical is not None and expected is not None and canonical == expected:
            actions.append("continue_recovery")
    else:
        if source_status == "valid" and (
            (early and no_destructive_artifacts)
            or (recognized_canonical and artifacts["rollbackCheckpoint"] == "valid")
        ):
            actions.append("continue_recovery")
        if artifacts["rollbackCheckpoint"] == "valid" and recognized_canonical:
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


def _update_plan(
    root: Path,
    operation_id: str,
    generation: int,
    authority: state.LifecycleState,
    *,
    controller_status: str,
    runtime_status: str,
) -> tuple[dict[str, str], bool | None, str, list[str]]:
    journal_path, _, _ = update_controller._update_paths(root, operation_id, generation)
    try:
        journal = update_controller.read_update_journal(journal_path)
        update_controller._validate_update_journal_semantics(journal, authority=authority)
    except update_controller.UpdateControllerError:
        return ({"journal": "invalid"}, None, "update_evidence_invalid", [])

    checkpoint = root / ".run" / "pre-update-database.db"
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
        ):
            actions.append("safe_abort")
        if (
            detail in {"starting", "recovering"}
            and installed == target
            and artifacts["targetCommit"] == "valid"
        ):
            actions.append("continue_recovery")
        # Phase 5 does not persist an operation/generation-bound identity for
        # pre-update-database.db. SQLite validity (or absence) cannot prove that
        # checkpoint belongs to this transaction, so Phase 6A never advertises
        # update rollback as an evidence-supported operator action.
    return artifacts, committed, reason, sorted(set(actions))


def plan_recovery(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    generation: int,
) -> dict[str, Any]:
    """Return a sanitized, read-only plan for one exact recovery-required operation."""

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
    controller_status = _controller_status(installation, canonical_id, generation)
    runtime_status = _runtime_status(installation)
    if operation.operation_type is state.OperationType.RESTORE:
        artifacts, boundary, reason, actions = _restore_plan(
            installation,
            canonical_id,
            generation,
            controller_status=controller_status,
            runtime_status=runtime_status,
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
        )
        boundary_name = "runtimeCommitted"
    projection = {
        "schemaVersion": 1,
        "operationId": canonical_id,
        "operationReference": canonical_id[:12],
        "operationType": operation.operation_type.value,
        "generation": generation,
        "lifecyclePhase": operation.phase.value,
        "controllerActive": controller_status == "active",
        "controllerStatus": controller_status,
        "runtimeStatus": runtime_status,
        "reasonCode": reason,
        boundary_name: boundary,
        "artifacts": artifacts,
        "operatorActionRequired": True,
        "permittedActions": actions,
        "readOnly": True,
        "cleanupEligible": False,
    }
    evidence_payload = json.dumps(
        {
            "operationId": canonical_id,
            "generation": generation,
            "operationType": operation.operation_type.value,
            "reasonCode": reason,
            "boundary": boundary,
            "artifacts": artifacts,
            "permittedActions": actions,
            "controllerStatus": controller_status,
            "runtimeStatus": runtime_status,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    projection["evidenceIdentity"] = (
        "plan-v1:" + hashlib.sha256(evidence_payload).hexdigest()[:32]
    )
    return projection


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
    journal_path = installation / ".run" / "update-state.json"
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamhome-lifecycle-recovery")
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--operation-id", required=True)
    plan.add_argument("--generation", required=True, type=int)
    classify = commands.add_parser("classify")
    classify.add_argument("--operation-id", required=True)
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
        else:
            payload = classify_retained_artifacts(
                arguments.root,
                operation_id=arguments.operation_id,
            )
        sys.stdout.write(json.dumps({"ok": True, **payload}, sort_keys=True) + "\n")
        return 0
    except (RecoveryPlanningError, state.LifecycleError, ValueError) as exc:
        code = getattr(exc, "code", "recovery_plan_unavailable")
        sys.stderr.write(json.dumps({"ok": False, "error": code}, sort_keys=True) + "\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
