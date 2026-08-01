from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from config import settings
from services.backup import BACKUP_LOCK, is_database_idle
from services.logger import logger
from services.playback_prep import playback_prep_service
from services.queue import queue_manager
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
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = WORKSPACE_ROOT / ".run"
STATUS_PATH = RUN_DIR / "update-state.json"
STATUS_LOCK_PATH = RUN_DIR / "update-state.lock"
LOG_PATH = WORKSPACE_ROOT / "update.log"
START_SCRIPT = WORKSPACE_ROOT / "start.sh"
ORPHANED_CONTROLLER_SECONDS = 30
RUNTIME_IDENTITY_ENV_KEYS = {
    "STREAMHOME_INSTANCE_ROOT",
    "STREAMHOME_INSTANCE_TOKEN",
    "STREAMHOME_SERVICE",
    "STREAMHOME_UPDATE_TRANSACTION",
    "STREAMHOME_UPDATE_COMMIT_TOKEN",
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
        "queued_at": None,
        "started_at": None,
        "finished_at": None,
        "last_checked_at": None,
        "last_success_at": None,
        "updated_at": None,
        "recovery_requested_at": None,
        "transaction_id": "",
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
        result = read_update_state()
        result.update(changes)
        result["updated_at"] = time.time()
        temporary = STATUS_PATH.with_name(f"{STATUS_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as state_file:
            state_file.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, STATUS_PATH)
        if sys.platform != "win32":
            directory_fd = os.open(RUN_DIR, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
        checked_at = time.time()
        current = await current_commit()
        write_update_state(
            phase="checking",
            message="Checking the official StreamHome repository.",
            current_commit=current,
            update_available=False,
            error="",
            runtime_committed=False,
            runtime_committed_at=None,
        )
        if not current:
            return write_update_state(
                phase="failed",
                message="The installed StreamHome commit could not be identified.",
                current_commit="",
                update_available=False,
                error="local_commit_unavailable",
                last_checked_at=checked_at,
            )
        if not await initialize_remote():
            return write_update_state(
                phase="failed",
                message="The Git origin is not the official StreamHome repository.",
                current_commit=current,
                update_available=False,
                error="untrusted_origin",
                last_checked_at=checked_at,
            )
        if not await is_git_clean():
            return write_update_state(
                phase="failed",
                message="Local source changes must be committed or moved before updating.",
                current_commit=current,
                update_available=False,
                error="dirty_worktree",
                last_checked_at=checked_at,
            )
        branch = settings.UPDATE_BRANCH
        if not BRANCH_RE.fullmatch(branch) or ".." in branch:
            return write_update_state(
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
            return write_update_state(
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
            return write_update_state(
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
            return write_update_state(
                phase="failed",
                message=f"The configured update branch '{branch}' could not be resolved.",
                current_commit=current,
                update_available=False,
                error="target_unavailable",
                last_checked_at=checked_at,
            )
        if current == target:
            return write_update_state(
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
                return write_update_state(
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
            return write_update_state(
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
        return write_update_state(
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
    """Count registered subprocesses and scheduled adaptive preparation work."""

    return len(state.ACTIVE_PROCESSES) + len(playback_prep_service.active_jobs)


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
    if BACKUP_LOCK.locked():
        blockers.append("a backup or restore operation is active")
    if queue_manager.active_tasks:
        blockers.append(f"{len(queue_manager.active_tasks)} active ingestion or download task{'s' if len(queue_manager.active_tasks) != 1 else ''}")
    return blockers


async def queued_launch_blockers(status: dict[str, Any]) -> list[str]:
    if status.get("install_mode") == "now" and not status.get("automatic"):
        return []
    return await idle_blockers()


async def update_handoff_blockers(install_mode: str) -> list[str]:
    if install_mode == "now":
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
    if not raw_staging:
        return
    try:
        staging = Path(raw_staging).resolve()
        install_parent = WORKSPACE_ROOT.resolve().parent
    except OSError:
        return
    if staging.parent == install_parent and staging.name.startswith(".streamhome-update.") and staging.is_dir():
        try:
            shutil.rmtree(staging)
        except OSError as exc:
            logger.warning(f"[Update Service] Could not remove reconciled staging artifacts: {exc}")


async def reconcile_orphaned_update() -> bool:
    status = read_update_state()
    phase = str(status.get("phase") or "")
    if update_lock_active():
        return False
    installed = await current_commit()
    target = str(status.get("target_commit") or "")
    previous = str(status.get("previous_commit") or "")
    error = str(status.get("error") or "")
    if phase in TERMINAL_PHASES and error in RECOVERABLE_TARGET_ERRORS:
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
    if phase not in BUSY_PHASES or not orphaned_controller_stale(status):
        return False
    transaction = str(status.get("transaction_id") or "")
    runtime_committed = bool(status.get("runtime_committed"))
    target_ready = installed == target and await local_runtime_ready(
        target,
        "" if runtime_committed else transaction,
    )
    previous_ready = installed == previous and await local_runtime_ready(previous)
    if target_ready:
        if transaction and not runtime_committed and not await commit_guarded_runtime(transaction, target):
            target_ready = False
    if target_ready:
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
    if previous_ready:
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
    write_update_state(
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
        if status.get("phase") in ACTIVE_PHASES or update_lock_active():
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
        return write_update_state(
            phase="queued",
            message=(
                "Immediate update requested. Isolated preflight will start now."
                if resolved_mode == "now"
                else "Update queued until StreamHome is idle."
            ),
            automatic=automatic,
            install_mode=resolved_mode,
            queued_at=time.time(),
            started_at=None,
            finished_at=None,
            error="",
            runtime_committed=False,
            runtime_committed_at=None,
        )


async def cancel_queued_update() -> dict[str, Any]:
    async with UPDATE_QUEUE_LOCK:
        status = read_update_state()
        phase = str(status.get("phase") or "")
        if phase == "queued":
            return write_update_state(
                phase="update_available",
                message="The pending update was cancelled.",
                automatic=False,
                queued_at=None,
                error="",
            )
        if phase not in {"preflight", "waiting_for_idle"}:
            raise RuntimeError("update_not_queued")
        transaction = str(status.get("transaction_id") or "")
        if not transaction or not transaction.replace("-", "").isalnum():
            raise RuntimeError("update_not_cancellable")
        request_path = RUN_DIR / f"update-cancel.{transaction}.requested"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.touch(exist_ok=True)
        return write_update_state(
            message="Cancellation requested. The updater will stop before protected cutover.",
        )


async def launch_queued_update_if_ready() -> bool:
    async with UPDATE_QUEUE_LOCK:
        status = read_update_state()
        if status.get("phase") != "queued":
            return False
        if status.get("automatic") and not maintenance_window_open():
            return False
        blockers = await queued_launch_blockers(status)
        if blockers:
            write_update_state(message=f"Waiting for idle: {blockers[0]}.")
            return False
        target = str(status.get("target_commit") or "")
        current = await current_commit()
        if not COMMIT_RE.fullmatch(target) or not COMMIT_RE.fullmatch(current):
            write_update_state(
                phase="failed",
                message="The queued update lost its validated commit information.",
                error="invalid_update_state",
                finished_at=time.time(),
            )
            return False
        if os.name == "nt":
            write_update_state(
                phase="failed",
                message="Automatic lifecycle updates require the supported Linux installation.",
                error="unsupported_update_platform",
                finished_at=time.time(),
            )
            return False
        signatures_valid, signature_error = await verify_update_commit_range(current, target)
        if not signatures_valid:
            logger.error(f"[Update Service] Launch signature verification failed: {signature_error}")
            write_update_state(
                phase="failed",
                message="The queued target no longer satisfies signed-update policy.",
                error="signature_verification_failed",
                failed_target=target,
                finished_at=time.time(),
            )
            return False
        token = secrets.token_urlsafe(32)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        token_path = RUN_DIR / f"update-handoff.{secrets.token_hex(8)}.token"
        token_descriptor = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(token_descriptor, "w", encoding="utf-8") as token_file:
            token_file.write(token)
            token_file.flush()
            os.fsync(token_file.fileno())
        controller_path = RUN_DIR / f"update-controller.{secrets.token_hex(8)}.sh"
        code, controller_source, controller_error = await run_git_cmd(["show", f"{target}:update.sh"])
        if code != 0 or not controller_source.startswith("#!/usr/bin/env bash"):
            token_path.unlink(missing_ok=True)
            controller_path.unlink(missing_ok=True)
            logger.error(f"[Update Service] Target controller extraction failed: {controller_error}")
            write_update_state(
                phase="failed",
                message="The target release does not contain a valid update controller.",
                error="target_update_controller_unavailable",
                finished_at=time.time(),
            )
            return False
        controller_path.write_text(controller_source.rstrip("\n") + "\n", encoding="utf-8")
        os.chmod(controller_path, 0o700)
        state.UPDATE_HANDOFF_TOKEN = token
        write_update_state(
            phase="preflight",
            message=(
                "The detached updater is preparing and validating the target release for immediate installation."
                if status.get("install_mode") == "now"
                else "The detached updater is preparing and validating the target release."
            ),
            started_at=time.time(),
            error="",
        )
        try:
            with LOG_PATH.open("ab", buffering=0) as log_handle:
                await asyncio.create_subprocess_exec(
                    "bash",
                    str(controller_path),
                    "--execute",
                    target,
                    current,
                    "true" if status.get("automatic") else "false",
                    str(token_path),
                    str(WORKSPACE_ROOT),
                    cwd=str(WORKSPACE_ROOT),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                    env=detached_lifecycle_environment(),
                )
        except (OSError, subprocess.SubprocessError) as exc:
            state.UPDATE_HANDOFF_TOKEN = ""
            token_path.unlink(missing_ok=True)
            controller_path.unlink(missing_ok=True)
            logger.error(f"[Update Service] Could not launch detached updater: {exc}")
            write_update_state(
                phase="failed",
                message="The detached update controller could not be launched.",
                error="update_handoff_failed",
                finished_at=time.time(),
            )
            return False
        return True


async def pull_and_install_updates() -> bool:
    """Compatibility wrapper: queue a validated update for the safe external controller."""
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
            if state.MAINTENANCE_MODE and not state.UPDATE_TRANSACTION_ID and status.get("phase") in TERMINAL_PHASES:
                state.MAINTENANCE_MODE = False
                state.MAINTENANCE_REASON = ""
                state.UPDATE_HANDOFF_TOKEN = ""
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
