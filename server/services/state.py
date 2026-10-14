import asyncio
import os
import subprocess
import time
from typing import Any, Dict, Union

from lifecycle.coordination import (
    ChildProcessIdentity,
    LeaseStateError,
    get_runtime_coordinator,
    run_cancellation_safe_thread,
)

# In-memory dictionary storing active download transient metrics:
# task_id -> {"progress": float, "speed": str, "eta": str}
ACTIVE_DOWNLOAD_METRICS: Dict[str, Dict[str, Any]] = {}

# In-memory process registry tracking active media subprocesses such as FFmpeg
# and Rclone. Update cutover must fail closed while any registered process exists.
# task_id -> asyncio.subprocess.Process or subprocess.Popen object reference
ACTIVE_PROCESSES: Dict[str, Union[asyncio.subprocess.Process, subprocess.Popen]] = {}
ACTIVE_PROCESS_LEASES: Dict[str, tuple[object, ChildProcessIdentity]] = {}

# Active HTTP traffic metrics for update idle detection
ACTIVE_HTTP_REQUESTS: int = 0
LAST_HTTP_ACTIVITY_TIMESTAMP: float = 0.0
MAINTENANCE_MODE: bool = False
MAINTENANCE_REASON: str = ""
BROWSER_PRESENCE: Dict[str, float] = {}
PRESENCE_TTL_SECONDS: int = 90
UPDATE_COMMIT_TOKEN: str = os.getenv("STREAMHOME_UPDATE_COMMIT_TOKEN", "")
UPDATE_TRANSACTION_ID: str = os.getenv("STREAMHOME_UPDATE_TRANSACTION", "")
RESTORE_COMMIT_DECISION: str = ""

import json

from config import config_dir
from services.logger import logger

_last_metrics_file_write = 0.0


def record_browser_presence(session_id: str, visible: bool) -> None:
    """Track only recently visible authenticated browser sessions."""
    if visible:
        BROWSER_PRESENCE[session_id] = time.time()
    else:
        BROWSER_PRESENCE.pop(session_id, None)


def active_browser_sessions(current_time: float | None = None) -> int:
    """Prune expired presence records and return the visible-browser count."""
    cutoff = (current_time if current_time is not None else time.time()) - PRESENCE_TTL_SECONDS
    expired = [session_id for session_id, seen_at in BROWSER_PRESENCE.items() if seen_at < cutoff]
    for session_id in expired:
        BROWSER_PRESENCE.pop(session_id, None)
    return len(BROWSER_PRESENCE)

def update_task_metrics(task_id: str, progress: float, speed: str = "0 KB/s", eta: str = "00:00:00", size: str = "0 MB", force_write: bool = False):
    global _last_metrics_file_write
    ACTIVE_DOWNLOAD_METRICS[task_id] = {
        "progress": round(progress, 2),
        "speed": speed,
        "eta": eta,
        "size": size
    }
    
    # Throttle file writes to once every 1.0 seconds unless force_write is True
    now = time.time()
    if force_write or now - _last_metrics_file_write >= 1.0:
        _last_metrics_file_write = now
        try:
            metrics_file = os.path.join(config_dir, "temp", "download_metrics.json")
            os.makedirs(os.path.dirname(metrics_file), exist_ok=True)
            with open(metrics_file, "w") as f:
                json.dump(ACTIVE_DOWNLOAD_METRICS, f)
        except Exception:
            pass

def get_task_metrics(task_id: str) -> Dict[str, Any]:
    return ACTIVE_DOWNLOAD_METRICS.get(task_id, {"progress": 0.0, "speed": "0 KB/s", "eta": "00:00:00", "size": "0 MB"})

def remove_task_metrics(task_id: str):
    ACTIVE_DOWNLOAD_METRICS.pop(task_id, None)
    try:
        metrics_file = os.path.join(config_dir, "temp", "download_metrics.json")
        if os.path.exists(metrics_file):
            with open(metrics_file, "w") as f:
                json.dump(ACTIVE_DOWNLOAD_METRICS, f)
    except Exception:
        pass

def register_process(task_id: str, process: Union[asyncio.subprocess.Process, subprocess.Popen]):
    """Register an active media subprocess for cancellation and update safety."""
    ACTIVE_PROCESSES[task_id] = process
    lease = get_runtime_coordinator().current_lease()
    if lease is not None and getattr(process, "pid", None):
        try:
            identity = lease.attach_child(int(process.pid))
            ACTIVE_PROCESS_LEASES[task_id] = (lease, identity)
        except (LeaseStateError, TypeError, ValueError):
            pass

def unregister_process(task_id: str) -> bool:
    """Remove a subprocess only after its termination is observable."""
    process = ACTIVE_PROCESSES.get(task_id)
    if process is not None and getattr(process, "returncode", None) is None:
        return False
    ACTIVE_PROCESSES.pop(task_id, None)
    association = ACTIVE_PROCESS_LEASES.pop(task_id, None)
    if association is not None:
        lease, identity = association
        try:
            lease.detach_child(identity)
        except LeaseStateError:
            pass
    return True

async def cancel_and_kill_process(task_id: str) -> bool:
    """Keep ownership visible until the child is terminated and reaped."""
    from lifecycle.media_work import settle, stop_process

    process = ACTIVE_PROCESSES.get(task_id)
    if process is None:
        unregister_process(task_id)
        return False
    try:
        if isinstance(process, subprocess.Popen):
            def stop_sync():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            _, cancelled = await settle(asyncio.to_thread(stop_sync))
        else:
            _, cancelled = await settle(stop_process(process))
        if cancelled:
            raise asyncio.CancelledError
        return True
    except Exception as error:
        logger.error("[Process Registry] Child termination remains unresolved: %s", type(error).__name__)
        return False
    finally:
        unregister_process(task_id)
