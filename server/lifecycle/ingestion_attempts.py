"""Ingestion attempt handoff without abandoning registered native writers."""
from __future__ import annotations

import subprocess


def require_drained_writer(task_id: str) -> None:
    """Shared candidate paths are reusable only after the old child was reaped."""
    from services.state import ACTIVE_PROCESSES, unregister_process

    process = ACTIVE_PROCESSES.get(task_id)
    if process is None:
        return
    if isinstance(process, subprocess.Popen):
        if process.poll() is None:
            raise RuntimeError("The previous ingestion writer is still active.")
        process.wait()
    elif process.returncode is None:
        raise RuntimeError("The previous ingestion writer is still active.")
    if not unregister_process(task_id):
        raise RuntimeError("The previous ingestion writer could not be released.")
