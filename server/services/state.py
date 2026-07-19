import asyncio
import subprocess
from typing import Dict, Any, Union

# In-memory dictionary storing active download transient metrics:
# task_id -> {"progress": float, "speed": str, "eta": str}
ACTIVE_DOWNLOAD_METRICS: Dict[str, Dict[str, Any]] = {}

# In-memory process registry tracking active FFmpeg subprocesses:
# task_id -> asyncio.subprocess.Process or subprocess.Popen object reference
ACTIVE_PROCESSES: Dict[str, Union[asyncio.subprocess.Process, subprocess.Popen]] = {}

# Active HTTP traffic metrics for update idle detection
ACTIVE_HTTP_REQUESTS: int = 0
LAST_HTTP_ACTIVITY_TIMESTAMP: float = 0.0

import json
import os
import time

from config import config_dir
from services.logger import logger

_last_metrics_file_write = 0.0

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

def register_process(task_id: str, process: asyncio.subprocess.Process):
    """Registers an active subprocess reference to prevent zombie processes on deletion."""
    ACTIVE_PROCESSES[task_id] = process

def unregister_process(task_id: str):
    """Removes a finished or cancelled subprocess from the registry."""
    ACTIVE_PROCESSES.pop(task_id, None)

async def cancel_and_kill_process(task_id: str) -> bool:
    """Explicitly terminates or kills a running OS process registered to a task."""
    process = ACTIVE_PROCESSES.pop(task_id, None)
    if not process:
        return False
        
    try:
        logger.info(f"[Process Registry] Terminating active FFmpeg process for task: {task_id}")
        process.terminate()
        try:
            # Give the process 2 seconds to clean up and exit gracefully
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, process.wait, 2.0)
            logger.info(f"[Process Registry] Process for task {task_id} exited gracefully.")
        except Exception:
            logger.warning(f"[Process Registry] Process did not respond to SIGTERM. Killing task {task_id}...")
            process.kill()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, process.wait)
            logger.info(f"[Process Registry] Process for task {task_id} killed successfully.")
        return True
    except Exception as e:
        logger.error(f"[Process Registry] Error trying to kill process for task {task_id}: {type(e).__name__}")
        return False
