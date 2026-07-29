from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import settings
from services.backup import BACKUP_LOCK, is_database_idle
from services.logger import logger
from services.queue import queue_manager
import services.state as state


REPOSITORY_URL = "https://github.com/StreamHome/StreamHome.git"
LEGACY_REPOSITORY_URL = "https://github.com/WaqSea/StreamHome.git"
TERMINAL_PHASES = {"idle", "up_to_date", "update_available", "succeeded", "failed", "rolled_back", "rollback_failed"}
BUSY_PHASES = {"preflight", "waiting_for_idle", "stopping", "installing", "starting", "rolling_back"}
INSTALL_MODES = {"automatic", "when_idle", "now"}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
UPDATE_CHECK_LOCK = asyncio.Lock()
UPDATE_QUEUE_LOCK = asyncio.Lock()
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = WORKSPACE_ROOT / ".run"
STATUS_PATH = RUN_DIR / "update-state.json"
LOG_PATH = WORKSPACE_ROOT / "update.log"
UPDATE_SCRIPT = WORKSPACE_ROOT / "update.sh"


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


def write_update_state(**changes: Any) -> dict[str, Any]:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    result = read_update_state()
    result.update(changes)
    result["updated_at"] = time.time()
    temporary = STATUS_PATH.with_name(f"{STATUS_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, STATUS_PATH)
    return result


def read_update_log(lines: int = 80) -> list[str]:
    try:
        content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return content[-max(1, min(lines, 200)) :]
    except OSError:
        return []


def get_git_path() -> str:
    return shutil.which("git") or "git"


async def run_git_cmd(args: list[str], cwd: Path = WORKSPACE_ROOT) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            get_git_path(),
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return (
            process.returncode,
            stdout.decode(errors="ignore").strip(),
            stderr.decode(errors="ignore").strip(),
        )
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


async def check_for_update_details() -> dict[str, Any]:
    async with UPDATE_CHECK_LOCK:
        checked_at = time.time()
        current = await current_commit()
        write_update_state(
            phase="checking",
            message="Checking the official StreamHome repository.",
            current_commit=current,
            error="",
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
        if settings.UPDATE_REQUIRE_SIGNED_COMMITS:
            code, _, error = await run_git_cmd(["verify-commit", target])
            if code != 0:
                logger.error(f"[Update Service] Signature verification failed: {error}")
                return write_update_state(
                    phase="failed",
                    message="The available commit does not satisfy signed-update policy.",
                    current_commit=current,
                    target_commit=target,
                    update_available=False,
                    error="signature_verification_failed",
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
    if state.ACTIVE_PROCESSES:
        blockers.append(f"{len(state.ACTIVE_PROCESSES)} active media process{'es' if len(state.ACTIVE_PROCESSES) != 1 else ''}")
    if BACKUP_LOCK.locked():
        blockers.append("a backup or restore operation is active")
    if not await is_database_idle():
        blockers.append("playback, ingestion, or download activity is present")
    if BACKUP_LOCK.locked() and "a backup or restore operation is active" not in blockers:
        blockers.append("a backup or restore operation is active")
    if state.ACTIVE_PROCESSES and not any("active media process" in blocker for blocker in blockers):
        blockers.append(f"{len(state.ACTIVE_PROCESSES)} active media process{'es' if len(state.ACTIVE_PROCESSES) != 1 else ''}")
    return blockers


async def protected_cutover_blockers() -> list[str]:
    """Return work that an administrator-requested immediate cutover must not interrupt."""
    blockers: list[str] = []
    if state.ACTIVE_HTTP_REQUESTS:
        blockers.append(f"{state.ACTIVE_HTTP_REQUESTS} active API request{'s' if state.ACTIVE_HTTP_REQUESTS != 1 else ''}")
    if state.ACTIVE_PROCESSES:
        blockers.append(f"{len(state.ACTIVE_PROCESSES)} active media process{'es' if len(state.ACTIVE_PROCESSES) != 1 else ''}")
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
    owner_path = RUN_DIR / "update.lock" / "owner.pid"
    try:
        owner = int(owner_path.read_text(encoding="utf-8").strip())
        os.kill(owner, 0)
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        return True
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
        for mode in ("--execute", "--manual-execute", "--recover-interrupted")
    )


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
        if status.get("phase") == "queued" or status.get("phase") in BUSY_PHASES or update_lock_active():
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
        )


async def cancel_queued_update() -> dict[str, Any]:
    async with UPDATE_QUEUE_LOCK:
        status = read_update_state()
        if status.get("phase") != "queued":
            raise RuntimeError("update_not_queued")
        return write_update_state(
            phase="update_available",
            message="The pending update was cancelled.",
            automatic=False,
            queued_at=None,
            error="",
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
        if not UPDATE_SCRIPT.is_file() or os.name == "nt":
            write_update_state(
                phase="failed",
                message="Automatic lifecycle updates require the supported Linux installation.",
                error="unsupported_update_platform",
                finished_at=time.time(),
            )
            return False
        token = secrets.token_urlsafe(32)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        token_path = RUN_DIR / f"update-handoff.{secrets.token_hex(8)}.token"
        token_path.write_text(token, encoding="utf-8")
        if os.name != "nt":
            os.chmod(token_path, 0o600)
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
            process = await asyncio.create_subprocess_exec(
                "bash",
                str(UPDATE_SCRIPT),
                "--queue",
                target,
                current,
                "true" if status.get("automatic") else "false",
                str(token_path),
                cwd=str(WORKSPACE_ROOT),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            state.UPDATE_HANDOFF_TOKEN = ""
            token_path.unlink(missing_ok=True)
            logger.error(f"[Update Service] Could not launch detached updater: {exc}")
            write_update_state(
                phase="failed",
                message="The detached update controller could not be launched.",
                error="update_handoff_failed",
                finished_at=time.time(),
            )
            return False
        return_code = await process.wait()
        if return_code != 0:
            state.UPDATE_HANDOFF_TOKEN = ""
            token_path.unlink(missing_ok=True)
            write_update_state(
                phase="failed",
                message="The detached update controller could not be queued.",
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
    if initial_delay_seconds > 0:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=initial_delay_seconds)
        except asyncio.TimeoutError:
            pass
    while not stop_event.is_set():
        try:
            status = read_update_state()
            if state.MAINTENANCE_MODE and status.get("phase") in TERMINAL_PHASES:
                state.MAINTENANCE_MODE = False
                state.MAINTENANCE_REASON = ""
                state.UPDATE_HANDOFF_TOKEN = ""
            if status.get("phase") == "queued":
                await launch_queued_update_if_ready()
            elif settings.SETUP_COMPLETE and settings.AUTO_UPDATE_ENABLED and status.get("phase") not in BUSY_PHASES:
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
