from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from config import settings
from lifecycle import state as lifecycle_state
from lifecycle.coordination import (
    CoordinationError,
    DrainSnapshot,
    EffectClass,
    get_runtime_coordinator,
)
from lifecycle.update_controller import (
    UpdateControllerError,
    mutate_update_journal,
    spawn_update_controller,
    validate_cutover_permit,
)
from services.backup import BACKUP_LOCK, is_database_idle
from services.logger import logger
from services.queue import queue_manager
from services.playback_prep import playback_prep_service
import services.state as state


REPOSITORY_URL = "https://github.com/StreamHome/StreamHome.git"
LEGACY_REPOSITORY_URL = "https://github.com/WaqSea/StreamHome.git"
TERMINAL_PHASES = {"idle", "up_to_date", "update_available", "succeeded", "failed", "rolled_back", "rollback_failed"}
BUSY_PHASES = {"preflight", "waiting_for_idle", "stopping", "installing", "starting", "rolling_back", "recovering"}
ACTIVE_PHASES = BUSY_PHASES | {"checking", "queued"}
RECOVERABLE_TARGET_ERRORS = {
    "shutdown_failed",
    "maintenance_start_failed",
    "database_checkpoint_failed",
    "update_failed",
    "update_rolled_back",
    "rollback_failed",
    "recovery_start_failed",
    "update_interrupted",
    "update_interrupted_rolled_back",
}
INSTALL_MODES = {"automatic", "when_idle", "now"}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
UPDATE_CHECK_LOCK = asyncio.Lock()
UPDATE_QUEUE_LOCK = asyncio.Lock()
UPDATE_CUTOVER_LOCK = asyncio.Lock()
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = WORKSPACE_ROOT / ".run"
STATUS_PATH = RUN_DIR / "update-state.json"
STATUS_LOCK_PATH = RUN_DIR / "update-state.lock"
LOG_PATH = WORKSPACE_ROOT / "update.log"
START_SCRIPT = WORKSPACE_ROOT / "start.sh"
UPDATE_SCRIPT = WORKSPACE_ROOT / "update.sh"
ORPHANED_CONTROLLER_SECONDS = 30
UPDATE_CUTOVER_DRAIN_SECONDS = 10.0
RUNTIME_IDENTITY_ENV_KEYS = {
    "STREAMHOME_INSTANCE_ROOT",
    "STREAMHOME_INSTANCE_TOKEN",
    "STREAMHOME_SERVICE",
    "STREAMHOME_UPDATE_TRANSACTION",
    "STREAMHOME_UPDATE_COMMIT_TOKEN",
    "STREAMHOME_LIFECYCLE_STARTUP_MODE",
    "STREAMHOME_LIFECYCLE_OPERATION_ID",
    "STREAMHOME_LIFECYCLE_GENERATION",
    "STREAMHOME_LIFECYCLE_FIRST_ADOPTION",
}


def detached_lifecycle_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in RUNTIME_IDENTITY_ENV_KEYS:
        environment.pop(key, None)
    return environment


def _default_status() -> dict[str, Any]:
    return {
        "phase": "idle",
        "message": "No update check has run yet.",
        "current_commit": "",
        "target_commit": "",
        "update_available": False,
        "automatic": False,
        "install_mode": "when_idle",
        "update_branch": "",
        "queued_at": None,
        "started_at": None,
        "finished_at": None,
        "last_checked_at": None,
        "last_success_at": None,
        "updated_at": None,
        "recovery_requested_at": None,
        "transaction_id": "",
        "lifecycle_generation": None,
        "lifecycle_phase": "",
        "diagnostic_id": "",
        "runtime_committed": False,
        "runtime_committed_at": None,
        "failed_target": "",
        "error": "",
    }


def read_update_state() -> dict[str, Any]:
    result = _default_status()
    try:
        if STATUS_PATH.is_file():
            payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                result.update(payload)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(f"[Update Service] Could not read persisted update state: {exc}")
    return result


class UpdateJournalReadError(RuntimeError):
    pass


def _read_update_state_strict_unlocked() -> dict[str, Any]:
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateJournalReadError("update journal is unreadable") from exc
    if not isinstance(payload, dict):
        raise UpdateJournalReadError("update journal is not an object")
    transaction_id = payload.get("transaction_id", "")
    generation = payload.get("lifecycle_generation")
    phase = payload.get("lifecycle_phase", "")
    if not isinstance(transaction_id, str):
        raise UpdateJournalReadError("update journal lifecycle identity is invalid")
    if generation is not None and (
        isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
    ):
        raise UpdateJournalReadError("update journal lifecycle generation is invalid")
    if not isinstance(phase, str):
        raise UpdateJournalReadError("update journal lifecycle phase is invalid")
    if generation is not None or phase:
        try:
            uuid.UUID(transaction_id)
            lifecycle_state.LifecyclePhase(phase)
        except (ValueError, AttributeError) as exc:
            raise UpdateJournalReadError("update journal lifecycle identity is invalid") from exc
        if generation is None or not phase:
            raise UpdateJournalReadError("update journal lifecycle identity is incomplete")
    return payload


def read_update_state_strict() -> dict[str, Any]:
    with _update_state_lock():
        return _read_update_state_strict_unlocked()


@contextmanager
def _update_state_lock():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with STATUS_LOCK_PATH.open("a+b") as lock_file:
        if sys.platform == "win32":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def write_update_state(**changes: Any) -> dict[str, Any]:
    with _update_state_lock():
        result = (
            _read_update_state_strict_unlocked()
            if STATUS_PATH.exists() or STATUS_PATH.is_symlink()
            else _default_status()
        )
        committed = result.get("runtime_committed") is True
        committed_at = result.get("runtime_committed_at")
        previous_transaction = str(result.get("transaction_id") or "")
        next_transaction = str(changes.get("transaction_id") or previous_transaction)
        replacing_transaction = bool(
            next_transaction and next_transaction != previous_transaction
        )
        generation = changes.get("lifecycle_generation")
        lifecycle_phase = changes.get("lifecycle_phase")
        guarded_replacement = replacing_transaction and generation is not None
        authority_guard = (
            lifecycle_state.hold_operation_with_credential(
                WORKSPACE_ROOT,
                operation_id=next_transaction,
                generation=generation,
                expected_phase=lifecycle_phase,
                expected_operation_type=lifecycle_state.OperationType.UPDATE,
            )
            if guarded_replacement
            else lifecycle_state.hold_idle_authority(WORKSPACE_ROOT)
        )
        try:
            with authority_guard as authority:
                operation = authority.active_operation
                if guarded_replacement and (
                    operation is None
                    or operation.owner_kind is not lifecycle_state.OwnerKind.UPDATER
                    or operation.phase is not lifecycle_state.LifecyclePhase.RESERVED
                ):
                    raise UpdateJournalReadError(
                        "update journal transaction replacement is not authorized"
                    )
                if committed and changes.get("runtime_committed") is False and not replacing_transaction:
                    raise UpdateJournalReadError("runtime_committed cannot be reverted")
                result.update(changes)
                if committed and not replacing_transaction:
                    result["runtime_committed"] = True
                    result["runtime_committed_at"] = committed_at
                result["updated_at"] = time.time()
                temporary = STATUS_PATH.with_name(
                    f"{STATUS_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp"
                )
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
                    with os.fdopen(
                        descriptor, "w", encoding="utf-8", newline="\n"
                    ) as state_file:
                        state_file.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
                        state_file.flush()
                        os.fsync(state_file.fileno())
                    os.replace(temporary, STATUS_PATH)
                    os.chmod(STATUS_PATH, 0o600, follow_symlinks=False)
                    if sys.platform != "win32":
                        directory_fd = os.open(RUN_DIR, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                return result
        except (TypeError, ValueError, lifecycle_state.LifecycleError) as exc:
            raise UpdateJournalReadError(
                "update journal mutation is not authorized"
            ) from exc


def write_authoritative_update_state(
    *,
    operation_id: str,
    generation: int,
    expected_lifecycle_phase: lifecycle_state.LifecyclePhase | str,
    expected_detail_phases: frozenset[str] | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """Mutate lifecycle-critical update detail through the canonical strict path."""

    return mutate_update_journal(
        WORKSPACE_ROOT,
        operation_id=operation_id,
        generation=generation,
        expected_lifecycle_phase=expected_lifecycle_phase,
        expected_detail_phases=expected_detail_phases,
        changes=changes,
    )


def _reserve_update_lifecycle() -> lifecycle_state.LifecycleState:
    """Reserve shared lifecycle exclusion and persist its credential out of band."""

    try:
        return lifecycle_state.claim_operation_with_credential(
            WORKSPACE_ROOT,
            operation_type=lifecycle_state.OperationType.UPDATE,
            owner_kind=lifecycle_state.OwnerKind.UPDATER,
        ).state
    except lifecycle_state.LifecycleConflictError as exc:
        raise RuntimeError("lifecycle_conflict") from exc
    except lifecycle_state.LifecycleError as exc:
        raise RuntimeError("lifecycle_unavailable") from exc


def _release_unjournaled_update_claim(claimed: lifecycle_state.LifecycleState) -> None:
    """Release a claim only when the update journal definitely did not adopt it."""

    operation = claimed.active_operation
    if operation is None:  # pragma: no cover - claim contract
        return
    try:
        with _update_state_lock():
            journal = _read_update_state_strict_unlocked()
            if (
                str(journal.get("transaction_id") or "") == operation.operation_id
                and journal.get("lifecycle_generation") == claimed.generation
            ):
                return
            result = lifecycle_state.complete_operation_with_credential(
                WORKSPACE_ROOT,
                operation_id=operation.operation_id,
                generation=claimed.generation,
                expected_phase=lifecycle_state.LifecyclePhase.RESERVED,
                expected_operation_type=lifecycle_state.OperationType.UPDATE,
            )
            if result.credential_cleanup_error:
                logger.warning(
                    "[Update Service] Unjournaled lifecycle claim completed with "
                    f"credential cleanup warning: {result.credential_cleanup_error}"
                )
    except UpdateJournalReadError as exc:
        logger.warning(f"[Update Service] Preserving lifecycle claim because {exc}")
    except lifecycle_state.LifecycleError as exc:
        logger.error(f"[Update Service] Unjournaled lifecycle claim release failed: {exc.code}")


def _lifecycle_identity(
    status: dict[str, Any],
) -> tuple[str, int, lifecycle_state.LifecyclePhase] | None:
    operation_id = str(status.get("transaction_id") or "")
    generation = status.get("lifecycle_generation")
    try:
        operation_id = str(uuid.UUID(operation_id))
    except (AttributeError, ValueError):
        return None
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        return None
    raw_phase = status.get("lifecycle_phase")
    if not isinstance(raw_phase, str) or not raw_phase:
        return None
    try:
        phase = lifecycle_state.LifecyclePhase(raw_phase)
    except ValueError:
        return None
    return operation_id, generation, phase


def _release_update_lifecycle(status: dict[str, Any]) -> bool:
    """Release only the exact update lifecycle operation recorded by this journal."""

    identity = _lifecycle_identity(status)
    if identity is None:
        return False
    operation_id, generation, phase = identity
    result = lifecycle_state.complete_operation_with_credential(
        WORKSPACE_ROOT,
        operation_id=operation_id,
        generation=generation,
        expected_phase=phase,
        expected_operation_type=lifecycle_state.OperationType.UPDATE,
    )
    if result.credential_cleanup_error:
        logger.warning(
            "[Update Service] Lifecycle authority completed with credential cleanup warning: "
            f"{result.credential_cleanup_error}"
        )
    return True


def _ensure_update_lifecycle(status: dict[str, Any]) -> dict[str, Any]:
    identity = _lifecycle_identity(status)
    if identity is not None:
        operation_id, generation, phase = identity
        try:
            current = lifecycle_state.validate_operation_with_credential(
                WORKSPACE_ROOT,
                operation_id=operation_id,
                generation=generation,
                expected_phase=phase,
            )
        except lifecycle_state.LifecycleConflictError as exc:
            raise RuntimeError("lifecycle_conflict") from exc
        except lifecycle_state.LifecycleError as exc:
            raise RuntimeError("lifecycle_unavailable") from exc
        operation = current.active_operation
        if operation is None or operation.operation_type is not lifecycle_state.OperationType.UPDATE:
            raise RuntimeError("lifecycle_conflict")
        return status
    claimed = _reserve_update_lifecycle()
    operation = claimed.active_operation
    if operation is None:  # pragma: no cover - claim contract
        raise RuntimeError("lifecycle_unavailable")
    try:
        return write_update_state(
            transaction_id=operation.operation_id,
            lifecycle_generation=claimed.generation,
            lifecycle_phase=lifecycle_state.LifecyclePhase.RESERVED.value,
            runtime_committed=False,
            runtime_committed_at=None,
            recovery_requested_at=None,
            diagnostic_id="",
            previous_commit="",
            staging_dir="",
            web_artifacts_swapped=False,
            web_dependencies_swapped=False,
            controller_pid=None,
            controller_start_ticks="",
        )
    except Exception:
        _release_unjournaled_update_claim(claimed)
        raise


def _record_terminal_update(status: dict[str, Any], **changes: Any) -> dict[str, Any]:
    identity = _lifecycle_identity(status)
    if identity is None:
        result = write_update_state(**changes)
    else:
        result = write_authoritative_update_state(
            operation_id=identity[0],
            generation=identity[1],
            expected_lifecycle_phase=identity[2],
            expected_detail_phases=frozenset({str(status.get("phase") or "")}),
            **changes,
        )
    try:
        _release_update_lifecycle(status)
    except lifecycle_state.LifecycleError as exc:
        logger.error(f"[Update Service] Exact lifecycle release failed: {exc.code}")
    return result


def _shared_lifecycle_state() -> lifecycle_state.LifecycleState | None:
    try:
        return lifecycle_state.inspect_state(WORKSPACE_ROOT)
    except lifecycle_state.LifecycleError:
        return None


def _require_lifecycle_idle_for_discovery() -> None:
    try:
        current = lifecycle_state.inspect_state(WORKSPACE_ROOT)
    except lifecycle_state.LifecycleError as exc:
        raise RuntimeError("lifecycle_unavailable") from exc
    if current.active_operation is not None:
        raise RuntimeError("update_in_progress")


def _write_discovery_update_state(**changes: Any) -> dict[str, Any]:
    _require_lifecycle_idle_for_discovery()
    return write_update_state(**changes)


def update_lifecycle_projection(status: dict[str, Any]) -> dict[str, Any]:
    """Project update detail without allowing it to override shared authority."""

    result = dict(status)
    current = _shared_lifecycle_state()
    if current is None:
        result["lifecycle_status"] = "unavailable"
        return result
    operation = current.active_operation
    if operation is None:
        result["lifecycle_status"] = "idle"
        return result
    result["lifecycle_status"] = "active"
    result["authoritative_operation_type"] = operation.operation_type.value
    result["authoritative_lifecycle_phase"] = operation.phase.value
    identity = _lifecycle_identity(status)
    matches_update = bool(
        operation.operation_type is lifecycle_state.OperationType.UPDATE
        and identity is not None
        and identity[0] == operation.operation_id
        and identity[1] == current.generation
    )
    if matches_update:
        result["lifecycle_phase"] = operation.phase.value
        if str(result.get("phase") or "") in TERMINAL_PHASES:
            result["phase"] = "recovering"
            result["message"] = "Shared update lifecycle ownership remains unresolved."
            result["error"] = "lifecycle_state_mismatch"
    else:
        result["phase"] = "lifecycle_blocked"
        result["message"] = "Another installation lifecycle operation is active."
        result["error"] = "lifecycle_conflict"
    return result


def read_update_log(lines: int = 80) -> list[str]:
    try:
        content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return content[-max(1, min(lines, 200)) :]
    except OSError:
        return []


def get_git_path() -> str:
    return shutil.which("git") or "git"


async def run_git_cmd(
    args: list[str],
    cwd: Path = WORKSPACE_ROOT,
    timeout_seconds: float = 120,
) -> tuple[int, str, str]:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            get_git_path(),
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        return (
            process.returncode,
            stdout.decode(errors="ignore").strip(),
            stderr.decode(errors="ignore").strip(),
        )
    except asyncio.TimeoutError:
        if process is not None and process.returncode is None:
            try:
                if sys.platform != "win32":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
        logger.error(f"[Update Service] Git command timed out after {timeout_seconds:g} seconds: {' '.join(args)}")
        return -1, "", "git_command_timeout"
    except Exception as exc:
        logger.error(f"[Update Service] Git command failed: {type(exc).__name__}: {exc}")
        return -1, "", str(exc)


def _normalized_remote(value: str) -> str:
    return value.strip().removesuffix("/").removesuffix(".git").lower()


async def initialize_remote() -> bool:
    code, remote, error = await run_git_cmd(["remote", "get-url", "origin"])
    if code != 0:
        logger.error(f"[Update Service] Cannot read origin: {error}")
        return False
    allowed = {_normalized_remote(REPOSITORY_URL), _normalized_remote(LEGACY_REPOSITORY_URL)}
    if _normalized_remote(remote) not in allowed:
        logger.error(f"[Update Service] Refusing unexpected Git origin: {remote}")
        return False
    if _normalized_remote(remote) == _normalized_remote(LEGACY_REPOSITORY_URL):
        code, _, error = await run_git_cmd(["remote", "set-url", "origin", REPOSITORY_URL])
        if code != 0:
            logger.error(f"[Update Service] Could not migrate the legacy origin: {error}")
            return False
    return True


async def is_git_clean() -> bool:
    code, output, error = await run_git_cmd(["status", "--porcelain", "--untracked-files=normal"])
    if code != 0:
        logger.error(f"[Update Service] Git status failed: {error}")
        return False
    return not output


async def get_active_branch() -> str:
    code, output, _ = await run_git_cmd(["branch", "--show-current"])
    return output if code == 0 and output else "detached"


async def current_commit() -> str:
    code, output, _ = await run_git_cmd(["rev-parse", "HEAD"])
    return output if code == 0 and COMMIT_RE.fullmatch(output) else ""


async def verify_update_commit_range(current: str, target: str) -> tuple[bool, str]:
    if not settings.UPDATE_REQUIRE_SIGNED_COMMITS:
        return True, ""
    trusted_signers = {
        value.strip().replace(" ", "").upper()
        for value in settings.UPDATE_TRUSTED_SIGNERS.split(",")
        if value.strip()
    }
    if not trusted_signers:
        return False, "trusted signer configuration is missing"
    code, commit_list, error = await run_git_cmd(["rev-list", "--reverse", f"{current}..{target}"])
    commits = [commit for commit in commit_list.splitlines() if COMMIT_RE.fullmatch(commit)]
    if code != 0 or not commits:
        return False, error or "signed commit range is empty"
    for commit in commits:
        verify_code, _, verify_error = await run_git_cmd(["verify-commit", commit])
        fingerprint_code, fingerprint, fingerprint_error = await run_git_cmd(
            ["show", "-s", "--format=%GF", commit]
        )
        normalized_fingerprint = fingerprint.strip().replace(" ", "").upper()
        if (
            verify_code != 0
            or fingerprint_code != 0
            or not normalized_fingerprint
            or normalized_fingerprint not in trusted_signers
        ):
            return False, verify_error or fingerprint_error or f"untrusted signer for {commit}"
    return True, ""


async def check_for_update_details() -> dict[str, Any]:
    async with UPDATE_CHECK_LOCK:
        _require_lifecycle_idle_for_discovery()
        checked_at = time.time()
        current = await current_commit()
        _write_discovery_update_state(
            phase="checking",
            message="Checking the official StreamHome repository.",
            current_commit=current,
            update_available=False,
            error="",
            runtime_committed=False,
            runtime_committed_at=None,
        )
        if not current:
            return _write_discovery_update_state(
                phase="failed",
                message="The installed StreamHome commit could not be identified.",
                current_commit="",
                update_available=False,
                error="local_commit_unavailable",
                last_checked_at=checked_at,
            )
        if not await initialize_remote():
            return _write_discovery_update_state(
                phase="failed",
                message="The Git origin is not the official StreamHome repository.",
                current_commit=current,
                update_available=False,
                error="untrusted_origin",
                last_checked_at=checked_at,
            )
        if not await is_git_clean():
            return _write_discovery_update_state(
                phase="failed",
                message="Local source changes must be committed or moved before updating.",
                current_commit=current,
                update_available=False,
                error="dirty_worktree",
                last_checked_at=checked_at,
            )
        branch = settings.UPDATE_BRANCH
        if not BRANCH_RE.fullmatch(branch) or ".." in branch:
            return _write_discovery_update_state(
                phase="failed",
                message="The configured update branch is invalid.",
                current_commit=current,
                update_available=False,
                error="invalid_update_branch",
                last_checked_at=checked_at,
            )
        shallow_code, shallow_value, shallow_error = await run_git_cmd(["rev-parse", "--is-shallow-repository"])
        if shallow_code != 0:
            logger.error(f"[Update Service] Shallow-check failed: {shallow_error}")
            return _write_discovery_update_state(
                phase="failed",
                message="The installation's Git history could not be inspected.",
                current_commit=current,
                update_available=False,
                error="git_history_unavailable",
                last_checked_at=checked_at,
            )
        fetch_args = (
            ["fetch", "--unshallow", "origin", branch]
            if shallow_value == "true"
            else ["fetch", "--prune", "origin", branch]
        )
        code, _, error = await run_git_cmd(fetch_args)
        if code != 0:
            logger.error(f"[Update Service] Fetch failed: {error}")
            return _write_discovery_update_state(
                phase="failed",
                message="The official update source could not be reached.",
                current_commit=current,
                update_available=False,
                error="update_fetch_failed",
                last_checked_at=checked_at,
            )
        code, target, error = await run_git_cmd(["rev-parse", f"origin/{branch}"])
        if code != 0 or not COMMIT_RE.fullmatch(target):
            logger.error(f"[Update Service] Target resolution failed: {error}")
            return _write_discovery_update_state(
                phase="failed",
                message=f"The configured update branch '{branch}' could not be resolved.",
                current_commit=current,
                update_available=False,
                error="target_unavailable",
                last_checked_at=checked_at,
            )
        if current == target:
            return _write_discovery_update_state(
                phase="up_to_date",
                message="StreamHome is up to date.",
                current_commit=current,
                target_commit=target,
                update_available=False,
                error="",
                last_checked_at=checked_at,
            )
        if settings.UPDATE_REQUIRE_SIGNED_COMMITS:
            signatures_valid, signature_error = await verify_update_commit_range(current, target)
            if not signatures_valid:
                logger.error(f"[Update Service] Signature verification failed: {signature_error}")
                return _write_discovery_update_state(
                    phase="failed",
                    message="The available commit range does not satisfy signed-update policy.",
                    current_commit=current,
                    target_commit=target,
                    update_available=False,
                    error="signature_verification_failed",
                    last_checked_at=checked_at,
                )
        code, _, _ = await run_git_cmd(["merge-base", "--is-ancestor", current, target])
        if code != 0:
            return _write_discovery_update_state(
                phase="failed",
                message="The available commit is not a fast-forward from this installation.",
                current_commit=current,
                target_commit=target,
                update_available=False,
                error="update_not_fast_forward",
                last_checked_at=checked_at,
            )
        previous = read_update_state()
        failed_target = str(previous.get("failed_target") or "")
        blocked = failed_target == target
        return _write_discovery_update_state(
            phase="failed" if blocked else "update_available",
            message=(
                "This update previously failed and will not be retried automatically."
                if blocked
                else "A newer StreamHome commit is available."
            ),
            current_commit=current,
            target_commit=target,
            update_available=not blocked,
            error="failed_target_suppressed" if blocked else "",
            last_checked_at=checked_at,
        )


async def check_for_github_updates() -> bool:
    """Compatibility wrapper used by the terminal control panel."""
    return bool((await check_for_update_details()).get("update_available"))


def _minutes_since_http_activity() -> float:
    if state.LAST_HTTP_ACTIVITY_TIMESTAMP <= 0:
        return float("inf")
    return max(0.0, (time.time() - state.LAST_HTTP_ACTIVITY_TIMESTAMP) / 60)


def active_media_work_count() -> int:
    """Count non-cancellable media subprocesses that must finish before cutover."""

    preparation_jobs = sum(not task.done() for task in playback_prep_service.active_jobs.values())
    return len(state.ACTIVE_PROCESSES) + preparation_jobs


async def idle_blockers() -> list[str]:
    blockers: list[str] = []
    browsers = state.active_browser_sessions()
    if browsers:
        blockers.append(f"{browsers} active browser session{'s' if browsers != 1 else ''}")
    if state.ACTIVE_HTTP_REQUESTS:
        blockers.append(f"{state.ACTIVE_HTTP_REQUESTS} active API request{'s' if state.ACTIVE_HTTP_REQUESTS != 1 else ''}")
    idle_minutes = _minutes_since_http_activity()
    if idle_minutes < settings.UPDATE_IDLE_MINUTES:
        blockers.append(
            f"only {int(idle_minutes)} of {settings.UPDATE_IDLE_MINUTES} required idle minutes elapsed"
        )
    media_work = active_media_work_count()
    if media_work:
        blockers.append(f"{media_work} active media operation{'s' if media_work != 1 else ''}")
    coordinated_work = len(get_runtime_coordinator().active_work())
    if coordinated_work:
        blockers.append(
            f"{coordinated_work} lifecycle-coordinated operation{'s' if coordinated_work != 1 else ''} active"
        )
    if BACKUP_LOCK.locked():
        blockers.append("a backup or restore operation is active")
    if not await is_database_idle():
        blockers.append("playback, ingestion, or download activity is present")
    if BACKUP_LOCK.locked() and "a backup or restore operation is active" not in blockers:
        blockers.append("a backup or restore operation is active")
    return blockers


async def protected_cutover_blockers() -> list[str]:
    """Return work that an administrator-requested immediate cutover must not interrupt."""
    blockers: list[str] = []
    if state.ACTIVE_HTTP_REQUESTS:
        blockers.append(f"{state.ACTIVE_HTTP_REQUESTS} active API request{'s' if state.ACTIVE_HTTP_REQUESTS != 1 else ''}")
    media_work = active_media_work_count()
    if media_work:
        blockers.append(f"{media_work} active media operation{'s' if media_work != 1 else ''}")
    coordinated_work = len(get_runtime_coordinator().active_work())
    if coordinated_work:
        blockers.append(
            f"{coordinated_work} lifecycle-coordinated operation{'s' if coordinated_work != 1 else ''} active"
        )
    if BACKUP_LOCK.locked():
        blockers.append("a backup or restore operation is active")
    if queue_manager.active_tasks:
        blockers.append(f"{len(queue_manager.active_tasks)} active ingestion or download task{'s' if len(queue_manager.active_tasks) != 1 else ''}")
    return blockers


async def queued_launch_blockers(status: dict[str, Any]) -> list[str]:
    if status.get("install_mode") == "now" and not status.get("automatic"):
        return await protected_cutover_blockers()
    return await idle_blockers()


async def is_system_idle() -> bool:
    return not await idle_blockers()


def maintenance_window_open(current: datetime | None = None) -> bool:
    start = settings.UPDATE_MAINTENANCE_START
    end = settings.UPDATE_MAINTENANCE_END
    if not start and not end:
        return True
    if not start or not end:
        return False
    try:
        start_minutes = int(start[:2]) * 60 + int(start[3:])
        end_minutes = int(end[:2]) * 60 + int(end[3:])
    except (TypeError, ValueError):
        return False
    now = current or datetime.now()
    current_minutes = now.hour * 60 + now.minute
    if start_minutes == end_minutes:
        return True
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    return current_minutes >= start_minutes or current_minutes < end_minutes


def update_lock_active() -> bool:
    lock_path = RUN_DIR / "update.lock"
    owner_path = lock_path / "owner.pid"
    try:
        owner = int(owner_path.read_text(encoding="utf-8").strip())
        os.kill(owner, 0)
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        return True
    try:
        expected_start = (lock_path / "owner.start").read_text(encoding="utf-8").strip()
    except OSError:
        expected_start = ""
    if expected_start:
        try:
            stat_line = Path(f"/proc/{owner}/stat").read_text(encoding="utf-8")
            actual_start = stat_line[stat_line.rfind(")") + 2 :].split()[19]
        except (IndexError, OSError, ValueError):
            return False
        if actual_start != expected_start:
            return False
    try:
        inspected = subprocess.run(
            ["ps", "-p", str(owner), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    command = inspected.stdout.strip()
    return any(
        script in command and mode in command
        for script in ("update.sh", "update-controller")
        for mode in ("--execute", "--manual-execute", "--recover-interrupted", "--finalize-recovery")
    )


def shared_update_controller_active(status: dict[str, Any]) -> bool:
    identity = _lifecycle_identity(status)
    if identity is None:
        return False
    operation_id, generation, _ = identity
    try:
        lease = lifecycle_state.read_controller_lease(WORKSPACE_ROOT)
    except lifecycle_state.LifecycleError:
        return True
    return bool(
        lease is not None
        and lease.operation_id == operation_id
        and lease.generation == generation
        and lifecycle_state.controller_lease_is_current(lease)
    )


def install_lock_active() -> bool:
    """Report whether the public installer currently owns this installation."""

    lock_path = WORKSPACE_ROOT.with_name(f"{WORKSPACE_ROOT.name}.install.lock")
    owner_path = lock_path / "owner.pid"
    try:
        owner = int(owner_path.read_text(encoding="utf-8").strip())
        os.kill(owner, 0)
    except (OSError, ValueError):
        return False
    return True


def orphaned_controller_stale(status: dict[str, Any], current_time: float | None = None) -> bool:
    now = time.time() if current_time is None else current_time
    lease_path = RUN_DIR / "update-lease.json"
    try:
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        lease = {}
    transaction = str(status.get("transaction_id") or "")
    if transaction and lease.get("transaction_id") == transaction:
        try:
            return now - float(lease.get("heartbeat_at") or 0) >= ORPHANED_CONTROLLER_SECONDS
        except (TypeError, ValueError):
            return True
    try:
        return now - float(status.get("updated_at") or 0) >= ORPHANED_CONTROLLER_SECONDS
    except (TypeError, ValueError):
        return True


def _probe_local_runtime(expected_commit: str, expected_transaction: str = "") -> bool:
    expected_build = expected_commit[:12]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        api_request = urllib.request.Request(
            f"http://127.0.0.1:8000/api/health?update_probe={time.time_ns()}",
            headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        )
        with opener.open(api_request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if (
                response.status >= 400
                or payload.get("status") != "ready"
                or str(payload.get("buildId") or "") != expected_build
                or (
                    expected_transaction
                    and str(payload.get("updateTransaction") or "") != expected_transaction
                )
            ):
                return False
        web_request = urllib.request.Request(
            f"http://127.0.0.1:{settings.WEB_PORT}/?update_probe={time.time_ns()}",
            headers={"Cache-Control": "no-cache"},
        )
        with opener.open(web_request, timeout=2) as response:
            return (
                200 <= response.status < 400
                and str(response.headers.get("X-StreamHome-Web-Build") or "") == expected_build
            )
    except Exception:
        return False


async def local_runtime_ready(expected_commit: str, expected_transaction: str = "") -> bool:
    if not COMMIT_RE.fullmatch(expected_commit):
        return False
    return await asyncio.to_thread(_probe_local_runtime, expected_commit, expected_transaction)


def _commit_guarded_runtime(transaction: str, target: str) -> bool:
    token_path = RUN_DIR / f"update-commit.{transaction}.token"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not token:
        return False
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/update/commit",
        method="POST",
        data=json.dumps({"transaction_id": transaction, "target_commit": target}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-StreamHome-Update-Commit": token,
        },
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
            committed = 200 <= response.status < 300
    except Exception:
        return False
    if committed:
        token_path.unlink(missing_ok=True)
    return committed


async def commit_guarded_runtime(transaction: str, target: str) -> bool:
    return await asyncio.to_thread(_commit_guarded_runtime, transaction, target)


def cleanup_reconciled_transaction(status: dict[str, Any]) -> None:
    transaction = str(status.get("transaction_id") or "")
    lease_path = RUN_DIR / "update-lease.json"
    try:
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        lease = {}
    if not transaction or lease.get("transaction_id") == transaction:
        try:
            lease_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"[Update Service] Could not remove reconciled artifact {lease_path.name}: {exc}")
    for path in (
        RUN_DIR / "pre-update-database.db",
        RUN_DIR / "maintenance.pid",
        RUN_DIR / "maintenance.start",
        RUN_DIR / f"update-recovery.{transaction}.requested",
        RUN_DIR / f"update-recovery.{transaction[:12]}.requested",
        RUN_DIR / f"update-cancel.{transaction}.requested",
        RUN_DIR / f"update-commit.{transaction}.token",
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"[Update Service] Could not remove reconciled artifact {path.name}: {exc}")
    raw_staging = str(status.get("staging_dir") or "")
    if raw_staging:
        try:
            staging = Path(raw_staging).resolve()
            install_parent = WORKSPACE_ROOT.resolve().parent
        except OSError:
            staging = None
            install_parent = None
        if (
            staging is not None
            and install_parent is not None
            and staging.parent == install_parent
            and staging.name.startswith(".streamhome-update.")
            and staging.is_dir()
        ):
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                logger.warning(f"[Update Service] Could not remove reconciled staging artifacts: {exc}")

    # Keep shared lifecycle exclusion until update-owned artifacts are reconciled.
    # Release remains exact-owner/generation guarded and fails closed on mismatch.
    current = lifecycle_state.inspect_state(WORKSPACE_ROOT)
    operation = current.active_operation
    if (
        operation is None
        or operation.operation_type is not lifecycle_state.OperationType.UPDATE
        or operation.operation_id != str(status.get("transaction_id") or "")
    ):
        return
    exact_status = dict(status)
    exact_status["transaction_id"] = operation.operation_id
    exact_status["lifecycle_generation"] = current.generation
    exact_status["lifecycle_phase"] = operation.phase.value
    _release_update_lifecycle(exact_status)


async def reconcile_orphaned_update() -> bool:
    status = read_update_state()
    phase = str(status.get("phase") or "")
    if update_lock_active() or shared_update_controller_active(status):
        return False
    try:
        authority = lifecycle_state.inspect_state(WORKSPACE_ROOT)
    except lifecycle_state.LifecycleError:
        return False
    operation = authority.active_operation
    identity = _lifecycle_identity(status)
    if operation is not None:
        if operation.operation_type is not lifecycle_state.OperationType.UPDATE:
            return False
        if operation.phase is lifecycle_state.LifecyclePhase.RECOVERY_REQUIRED:
            return False
        if (
            identity is None
            or identity[0] != operation.operation_id
            or identity[1] != authority.generation
        ):
            return False
        if identity[2] is not operation.phase:
            try:
                status = write_authoritative_update_state(
                    operation_id=operation.operation_id,
                    generation=authority.generation,
                    expected_lifecycle_phase=operation.phase,
                    expected_detail_phases=frozenset({phase}),
                )
                phase = str(status.get("phase") or "")
            except Exception:
                return False
    elif identity is not None and phase in ACTIVE_PHASES:
        # A lifecycle-aware active journal without authority is ambiguous. Do
        # not recreate ownership from subordinate update detail.
        return False
    installed = await current_commit()
    target = str(status.get("target_commit") or "")
    previous = str(status.get("previous_commit") or "")
    error = str(status.get("error") or "")
    if phase in TERMINAL_PHASES and error in RECOVERABLE_TARGET_ERRORS:
        if operation is not None:
            return False
        transaction = str(status.get("transaction_id") or "")
        runtime_committed = bool(status.get("runtime_committed"))
        if (
            not target
            or installed != target
            or not await local_runtime_ready(target, "" if runtime_committed else transaction)
        ):
            return False
        if transaction and not runtime_committed and not await commit_guarded_runtime(transaction, target):
            return False
        completed_at = time.time()
        write_update_state(
            phase="succeeded",
            message="The target release was manually recovered and both local services are healthy.",
            current_commit=target,
            update_available=False,
            error="",
            failed_target="",
            staging_dir="",
            web_artifacts_swapped=False,
            finished_at=completed_at,
            last_success_at=completed_at,
        )
        cleanup_reconciled_transaction(status)
        return True
    resumable_detail = BUSY_PHASES | {"succeeded", "rolled_back"}
    if phase not in resumable_detail or not orphaned_controller_stale(status):
        return False
    transaction = str(status.get("transaction_id") or "")
    runtime_committed = bool(status.get("runtime_committed"))
    target_ready = installed == target and await local_runtime_ready(
        target,
        "" if runtime_committed else transaction,
    )
    previous_ready = installed == previous and await local_runtime_ready(previous)
    if target_ready and operation is None:
        if transaction and not runtime_committed and not await commit_guarded_runtime(transaction, target):
            target_ready = False
    if target_ready and operation is None:
        write_update_state(
            phase="succeeded",
            message="The updated release is healthy; interrupted controller bookkeeping was reconciled.",
            current_commit=target,
            update_available=False,
            error="",
            failed_target="",
            staging_dir="",
            web_artifacts_swapped=False,
            finished_at=time.time(),
            last_success_at=time.time(),
        )
        cleanup_reconciled_transaction(status)
        return True
    if previous_ready and operation is None:
        write_update_state(
            phase="rolled_back",
            message="The previous release is healthy; interrupted controller bookkeeping was reconciled.",
            current_commit=previous,
            update_available=False,
            error="update_interrupted_rolled_back",
            failed_target=target,
            staging_dir="",
            web_artifacts_swapped=False,
            finished_at=time.time(),
        )
        cleanup_reconciled_transaction(status)
        return True

    transaction = str(status.get("transaction_id") or "legacy")
    safe_transaction = transaction if transaction.replace("-", "").isalnum() else "legacy"
    request_path = RUN_DIR / f"update-recovery.{safe_transaction}.requested"
    try:
        request_path.parent.mkdir(parents=True, exist_ok=True)
        with request_path.open("x", encoding="utf-8") as request_file:
            request_file.write(f"{time.time():.6f}\n")
    except FileExistsError:
        return False
    except OSError as exc:
        logger.error(f"[Update Service] Could not reserve orphaned-update recovery: {exc}")
        return False
    if operation is not None:
        try:
            status = write_authoritative_update_state(
                operation_id=operation.operation_id,
                generation=authority.generation,
                expected_lifecycle_phase=operation.phase,
                expected_detail_phases=frozenset({phase}),
                phase="recovering",
                message=(
                    "The update controller stopped unexpectedly. Controlled recovery will "
                    "replace any surviving fenced runtime before lifecycle completion."
                ),
                error="controller_lost",
                recovery_requested_at=time.time(),
            )
        except Exception:
            return False
    else:
        status = write_update_state(
            phase="recovering",
            message="The update controller stopped unexpectedly. Automatic recovery is starting.",
            error="controller_lost",
            recovery_requested_at=time.time(),
        )
    try:
        with LOG_PATH.open("ab", buffering=0) as log_handle:
            await asyncio.create_subprocess_exec(
                str(START_SCRIPT),
                cwd=str(WORKSPACE_ROOT),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                env=detached_lifecycle_environment(),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error(f"[Update Service] Orphaned-update recovery handoff failed: {exc}")
        if operation is not None:
            try:
                lifecycle_state.transition_operation_with_credential(
                    WORKSPACE_ROOT,
                    operation_id=operation.operation_id,
                    generation=authority.generation,
                    expected_phase=operation.phase,
                    next_phase=lifecycle_state.LifecyclePhase.RECOVERY_REQUIRED,
                )
                write_authoritative_update_state(
                    operation_id=operation.operation_id,
                    generation=authority.generation,
                    expected_lifecycle_phase=lifecycle_state.LifecyclePhase.RECOVERY_REQUIRED,
                    expected_detail_phases=frozenset({"recovering"}),
                    phase="rollback_failed",
                    message="Automatic recovery could not be queued. Run ./start.sh from the StreamHome directory.",
                    error="recovery_handoff_failed",
                    failed_target=target,
                    finished_at=time.time(),
                )
            except Exception:
                pass
        else:
            write_update_state(
                phase="rollback_failed",
                message="Automatic recovery could not be queued. Run ./start.sh from the StreamHome directory.",
                error="recovery_handoff_failed",
                finished_at=time.time(),
            )
        return False
    return True


async def queue_update(
    *,
    automatic: bool,
    allow_failed_target: bool = False,
    install_mode: str = "when_idle",
) -> dict[str, Any]:
    async with UPDATE_QUEUE_LOCK:
        resolved_mode = "automatic" if automatic else install_mode
        if resolved_mode not in INSTALL_MODES or (not automatic and resolved_mode == "automatic"):
            raise RuntimeError("invalid_install_mode")
        status = read_update_state()
        if status.get("phase") in ACTIVE_PHASES or update_lock_active() or install_lock_active():
            raise RuntimeError("update_in_progress")
        target = str(status.get("target_commit") or "")
        if target and target == status.get("failed_target") and not allow_failed_target:
            raise RuntimeError("failed_target_suppressed")
        retrying_failed_target = (
            allow_failed_target
            and target == status.get("failed_target")
            and COMMIT_RE.fullmatch(target)
        )
        if (not status.get("update_available") and not retrying_failed_target) or not COMMIT_RE.fullmatch(target):
            status = await check_for_update_details()
            target = str(status.get("target_commit") or "")
            retrying_failed_target = (
                allow_failed_target
                and target == status.get("failed_target")
                and COMMIT_RE.fullmatch(target)
            )
        if (not status.get("update_available") and not retrying_failed_target) or not COMMIT_RE.fullmatch(target):
            raise RuntimeError(str(status.get("error") or "no_update_available"))
        if target == status.get("failed_target") and not allow_failed_target:
            raise RuntimeError("failed_target_suppressed")
        claimed = _reserve_update_lifecycle()
        operation = claimed.active_operation
        if operation is None:  # pragma: no cover - claim contract
            raise RuntimeError("lifecycle_unavailable")
        try:
            return write_update_state(
                phase="queued",
                message=(
                    "Immediate update requested. Isolated preflight will start now."
                    if resolved_mode == "now"
                    else "Update queued until StreamHome is idle."
                ),
                automatic=automatic,
                install_mode=resolved_mode,
                update_branch=settings.UPDATE_BRANCH,
                queued_at=time.time(),
                started_at=None,
                finished_at=None,
                recovery_requested_at=None,
                diagnostic_id="",
                error="",
                runtime_committed=False,
                runtime_committed_at=None,
                transaction_id=operation.operation_id,
                lifecycle_generation=claimed.generation,
                lifecycle_phase=lifecycle_state.LifecyclePhase.RESERVED.value,
                previous_commit="",
                staging_dir="",
                web_artifacts_swapped=False,
                web_dependencies_swapped=False,
                controller_pid=None,
                controller_start_ticks="",
            )
        except Exception:
            _release_unjournaled_update_claim(claimed)
            raise


async def cancel_queued_update() -> dict[str, Any]:
    async with UPDATE_QUEUE_LOCK:
        async with UPDATE_CUTOVER_LOCK:
            try:
                with _update_state_lock():
                    status = _read_update_state_strict_unlocked()
            except UpdateJournalReadError as exc:
                raise RuntimeError("update_not_cancellable") from exc
            phase = str(status.get("phase") or "")
            identity = _lifecycle_identity(status)
            if identity is None:
                raise RuntimeError("update_not_cancellable")
            operation_id, generation, journal_phase = identity
            if journal_phase is not lifecycle_state.LifecyclePhase.RESERVED:
                raise RuntimeError("update_not_cancellable")
            try:
                current = lifecycle_state.validate_operation_with_credential(
                    WORKSPACE_ROOT,
                    operation_id=operation_id,
                    generation=generation,
                    expected_phase=lifecycle_state.LifecyclePhase.RESERVED,
                )
            except lifecycle_state.LifecycleError as exc:
                raise RuntimeError("update_not_cancellable") from exc
            operation = current.active_operation
            if operation is None or operation.operation_type is not lifecycle_state.OperationType.UPDATE:
                raise RuntimeError("update_not_cancellable")
            if phase == "queued" and not shared_update_controller_active(status):
                return _record_terminal_update(
                    status,
                    phase="update_available",
                    message="The pending update was cancelled.",
                    automatic=False,
                    queued_at=None,
                    error="",
                )
            if phase not in {"queued", "preflight", "waiting_for_idle"}:
                raise RuntimeError("update_not_queued")
            request_path = RUN_DIR / f"update-cancel.{operation_id}.requested"
            request_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                request_path,
                os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(descriptor)
            os.chmod(request_path, 0o600)
            return write_authoritative_update_state(
                operation_id=operation_id,
                generation=generation,
                expected_lifecycle_phase=lifecycle_state.LifecyclePhase.RESERVED,
                expected_detail_phases=frozenset({phase}),
                message="Cancellation requested. The updater will stop before protected cutover.",
            )


def prepare_official_update_script() -> Path:
    """Validate the installation-root updater and restore its executable mode."""

    try:
        workspace = WORKSPACE_ROOT.resolve(strict=True)
        candidate = UPDATE_SCRIPT
        if candidate.is_symlink():
            raise OSError("update.sh must not be a symbolic link")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != workspace or resolved.name != "update.sh" or not resolved.is_file():
            raise OSError("update.sh is not the installation-root updater")
        if resolved.read_bytes()[:20] != b"#!/usr/bin/env bash\n":
            raise OSError("update.sh does not have the expected Bash header")
        current_mode = resolved.stat().st_mode
        os.chmod(resolved, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return resolved
    except OSError as exc:
        raise RuntimeError("update_script_unavailable") from exc


async def prepare_update_cutover(
    *,
    operation_id: str,
    generation: int,
    cutover_token: str,
) -> DrainSnapshot:
    """Close process-local admission and advance the exact update to cutover."""

    async with UPDATE_CUTOVER_LOCK:
        validate_cutover_permit(
            WORKSPACE_ROOT,
            operation_id=operation_id,
            generation=generation,
            supplied_token=cutover_token,
        )
        with _update_state_lock():
            journal = _read_update_state_strict_unlocked()
        if (
            journal.get("transaction_id") != operation_id
            or journal.get("lifecycle_generation") != generation
            or journal.get("lifecycle_phase") != lifecycle_state.LifecyclePhase.RESERVED.value
            or str(journal.get("phase") or "") not in {"preflight", "waiting_for_idle"}
        ):
            raise RuntimeError("lifecycle_conflict")
        cancellation_path = RUN_DIR / f"update-cancel.{operation_id}.requested"
        if cancellation_path.exists() or cancellation_path.is_symlink():
            raise RuntimeError("update_cancelled")
        current = lifecycle_state.validate_operation_with_credential(
            WORKSPACE_ROOT,
            operation_id=operation_id,
            generation=generation,
            expected_phase=lifecycle_state.LifecyclePhase.RESERVED,
        )
        operation = current.active_operation
        if operation is None or operation.operation_type is not lifecycle_state.OperationType.UPDATE:
            raise RuntimeError("lifecycle_conflict")
        coordinator = get_runtime_coordinator()
        snapshot = coordinator.close_admission(
            list(EffectClass),
            operation_id=operation_id,
            generation=generation,
        )
        try:
            drained = await coordinator.drain(snapshot, timeout=UPDATE_CUTOVER_DRAIN_SECONDS)
            if not drained.complete:
                coordinator.release_fence(snapshot)
                raise RuntimeError("cutover_busy")
            if cancellation_path.exists() or cancellation_path.is_symlink():
                coordinator.release_fence(snapshot)
                raise RuntimeError("update_cancelled")
            lifecycle_state.transition_operation_with_credential(
                WORKSPACE_ROOT,
                operation_id=operation_id,
                generation=generation,
                expected_phase=lifecycle_state.LifecyclePhase.RESERVED,
                next_phase=lifecycle_state.LifecyclePhase.EXCLUSIVE,
            )
        except Exception:
            try:
                authoritative = lifecycle_state.inspect_state(WORKSPACE_ROOT)
                active = authoritative.active_operation
                still_reserved = bool(
                    active is not None
                    and active.operation_id == operation_id
                    and authoritative.generation == generation
                    and active.phase is lifecycle_state.LifecyclePhase.RESERVED
                )
            except lifecycle_state.LifecycleError:
                still_reserved = False
            if still_reserved:
                try:
                    coordinator.release_fence(snapshot)
                except CoordinationError:
                    pass
            raise
        try:
            write_authoritative_update_state(
                operation_id=operation_id,
                generation=generation,
                expected_lifecycle_phase=lifecycle_state.LifecyclePhase.EXCLUSIVE,
                expected_detail_phases=frozenset({"preflight", "waiting_for_idle"}),
                phase="stopping",
                message="Prepared release is ready. Stopping StreamHome for protected activation.",
            )
        except Exception:
            # Authority is already exclusive. Keep admission closed and let
            # controller/startup recovery reconcile the subordinate journal.
            raise RuntimeError("update_journal_unavailable")
        return snapshot


def _record_controller_launch_failure(
    status: dict[str, Any],
    *,
    operation_id: str,
    generation: int,
) -> None:
    """Release only a definitely untransferred claim; otherwise preserve it."""

    try:
        current = lifecycle_state.inspect_state(WORKSPACE_ROOT)
    except lifecycle_state.LifecycleError:
        logger.error(
            "[Update Service] Update controller handoff failed while lifecycle authority "
            "was unavailable; the journal was preserved unchanged."
        )
        return
    operation = current.active_operation
    if (
        operation is not None
        and operation.operation_id == operation_id
        and current.generation == generation
        and operation.phase is lifecycle_state.LifecyclePhase.RESERVED
    ):
        _record_terminal_update(
            status,
            phase="failed",
            message="The managed update controller could not be launched.",
            error="update_controller_handoff_failed",
            finished_at=time.time(),
        )
        return
    if (
        operation is not None
        and operation.operation_type is lifecycle_state.OperationType.UPDATE
        and operation.operation_id == operation_id
    ):
        try:
            write_authoritative_update_state(
                operation_id=operation.operation_id,
                generation=current.generation,
                expected_lifecycle_phase=operation.phase,
                expected_detail_phases=frozenset({str(status.get("phase") or "")}),
                phase="recovering",
                message=(
                    "Update controller handoff became indeterminate; lifecycle ownership "
                    "remains fail-closed."
                ),
                error="update_controller_handoff_failed",
                finished_at=time.time(),
            )
        except Exception:
            logger.error(
                "[Update Service] Update controller handoff became indeterminate; "
                "authority and journal were preserved for fail-closed recovery."
            )


async def launch_queued_update_if_ready() -> bool:
    async with UPDATE_QUEUE_LOCK:
        status = read_update_state()
        if status.get("phase") != "queued":
            return False
        if install_lock_active():
            identity = _lifecycle_identity(status)
            if identity is not None:
                write_authoritative_update_state(
                    operation_id=identity[0],
                    generation=identity[1],
                    expected_lifecycle_phase=identity[2],
                    expected_detail_phases=frozenset({"queued"}),
                    message="Waiting for the active public installer to finish.",
                )
            return False
        if status.get("automatic") and not maintenance_window_open():
            return False
        blockers = await queued_launch_blockers(status)
        if blockers:
            identity = _lifecycle_identity(status)
            if identity is not None:
                write_authoritative_update_state(
                    operation_id=identity[0],
                    generation=identity[1],
                    expected_lifecycle_phase=identity[2],
                    expected_detail_phases=frozenset({"queued"}),
                    message=f"Waiting for idle: {blockers[0]}.",
                )
            return False
        try:
            status = _ensure_update_lifecycle(status)
        except RuntimeError as exc:
            logger.error(
                "[Update Service] Queued update lifecycle validation failed; "
                f"journal preserved unchanged: {exc}"
            )
            return False
        target = str(status.get("target_commit") or "")
        current = await current_commit()
        if not COMMIT_RE.fullmatch(target) or not COMMIT_RE.fullmatch(current):
            _record_terminal_update(
                status,
                phase="failed",
                message="The queued update lost its validated commit information.",
                error="invalid_update_state",
                finished_at=time.time(),
            )
            return False
        if os.name == "nt":
            _record_terminal_update(
                status,
                phase="failed",
                message="Automatic lifecycle updates require the supported Linux installation.",
                error="unsupported_update_platform",
                finished_at=time.time(),
            )
            return False
        signatures_valid, signature_error = await verify_update_commit_range(current, target)
        if not signatures_valid:
            logger.error(f"[Update Service] Launch signature verification failed: {signature_error}")
            _record_terminal_update(
                status,
                phase="failed",
                message="The queued target no longer satisfies signed-update policy.",
                error="signature_verification_failed",
                failed_target=target,
                finished_at=time.time(),
            )
            return False
        try:
            update_script = prepare_official_update_script()
        except RuntimeError as exc:
            logger.error(f"[Update Service] The official update.sh launch path is unavailable: {exc.__cause__}")
            _record_terminal_update(
                status,
                phase="failed",
                message="The installation-root update.sh file is missing, untrusted, or could not be made executable.",
                error="update_script_unavailable",
                finished_at=time.time(),
            )
            return False
        lifecycle_identity = _lifecycle_identity(status)
        if lifecycle_identity is None:
            _record_terminal_update(
                status,
                phase="failed",
                message="The queued update lost its shared lifecycle identity.",
                error="lifecycle_unavailable",
                finished_at=time.time(),
            )
            return False
        try:
            with LOG_PATH.open("ab", buffering=0) as log_handle:
                handoff = await asyncio.to_thread(
                    spawn_update_controller,
                    WORKSPACE_ROOT,
                    operation_id=lifecycle_identity[0],
                    generation=lifecycle_identity[1],
                    target_commit=target,
                    previous_commit=current,
                    automatic=bool(status.get("automatic")),
                    install_mode=str(status.get("install_mode") or "when_idle"),
                    update_branch=str(status.get("update_branch") or settings.UPDATE_BRANCH),
                    log_handle=log_handle,
                )
        except (
            ValueError,
            OSError,
            subprocess.SubprocessError,
            UpdateControllerError,
            lifecycle_state.LifecycleError,
        ) as exc:
            logger.error(f"[Update Service] Could not establish the managed update controller: {type(exc).__name__}")
            _record_controller_launch_failure(
                status,
                operation_id=lifecycle_identity[0],
                generation=lifecycle_identity[1],
            )
            return False
        authoritative = lifecycle_state.inspect_state(WORKSPACE_ROOT)
        authoritative_operation = authoritative.active_operation
        if (
            authoritative_operation is None
            or authoritative_operation.operation_id != handoff.operation_id
            or authoritative.generation != handoff.generation
            or authoritative_operation.operation_type is not lifecycle_state.OperationType.UPDATE
        ):
            raise RuntimeError("lifecycle_conflict")
        return True


async def pull_and_install_updates() -> bool:
    """Compatibility wrapper: queue a validated launch of the official update.sh."""
    try:
        await queue_update(automatic=False)
        await launch_queued_update_if_ready()
        return True
    except RuntimeError as exc:
        logger.error(f"[Update Service] Could not queue update: {exc}")
        return False


async def automatic_update_worker(stop_event: asyncio.Event, initial_delay_seconds: float = 60) -> None:
    """Check for updates periodically and execute queued work only after fail-closed idle checks."""
    try:
        await reconcile_orphaned_update()
    except Exception as exc:
        logger.error(f"[Update Service] Startup update reconciliation failed: {type(exc).__name__}: {exc}")
    if initial_delay_seconds > 0:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=initial_delay_seconds)
        except asyncio.TimeoutError:
            pass
    while not stop_event.is_set():
        try:
            if await reconcile_orphaned_update():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            status = read_update_state()
            if status.get("phase") == "queued":
                await launch_queued_update_if_ready()
            elif settings.SETUP_COMPLETE and settings.AUTO_UPDATE_ENABLED and status.get("phase") not in ACTIVE_PHASES:
                last_checked = float(status.get("last_checked_at") or 0)
                interval = settings.UPDATE_CHECK_INTERVAL_HOURS * 60 * 60
                if time.time() - last_checked >= interval:
                    checked = await check_for_update_details()
                    if checked.get("update_available"):
                        await queue_update(automatic=True)
                        await launch_queued_update_if_ready()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[Update Worker] {type(exc).__name__}: {exc}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
