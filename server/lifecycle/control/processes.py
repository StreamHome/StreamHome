"""Exact managed-unit process ownership; no name or port based termination."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import stat

from .rpc import ControlError


RUNTIME_CGROUP = "/system.slice/streamhome.service"


def process_cgroup(pid: int) -> str:
    lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise ControlError("control_cgroup_unsupported")
    return lines[0][3:]


def require_runtime_supervisor() -> None:
    if os.geteuid() == 0 or process_cgroup(os.getpid()) != RUNTIME_CGROUP:
        raise ControlError("control_supervisor_forbidden")


def controller_peer(pid: int, operation_id: str) -> None:
    from ..management_journal import _uuid
    _uuid(operation_id, "controller operation")
    if process_cgroup(pid) != f"/system.slice/streamhome-operation@{operation_id}.service":
        raise ControlError("control_controller_forbidden")


def runtime_children() -> set[int]:
    require_runtime_supervisor()
    path = Path("/sys/fs/cgroup" + RUNTIME_CGROUP + "/cgroup.procs")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ControlError("control_cgroup_unavailable")
    return {int(raw) for raw in path.read_text().split()} - {os.getpid()}


def signal_owned(pid: int, value: signal.Signals) -> None:
    """Hold a pidfd before checking membership; never signal a recycled PID."""
    try:
        descriptor = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return
    try:
        if process_cgroup(pid) != RUNTIME_CGROUP or Path(f"/proc/{pid}").stat().st_uid != os.geteuid():
            raise ControlError("control_process_changed")
        signal.pidfd_send_signal(descriptor, value)
    except (ProcessLookupError, FileNotFoundError):
        pass
    finally:
        os.close(descriptor)


async def stop_runtime_children(*, graceful_seconds: float = 45.0) -> None:
    """Stop all unit-owned descendants, including FFmpeg/Rclone new sessions.

    Controllers have their own unit/cgroup and are never included. The supervisor
    remains alive during internal cutover; systemd retains final stop custody.
    """
    require_runtime_supervisor()
    if not 0 < graceful_seconds <= 45:
        raise ControlError("control_timeout_invalid")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + graceful_seconds
    signaled = set()
    while children := runtime_children():
        for pid in children - signaled:
            signal_owned(pid, signal.SIGTERM)
            signaled.add(pid)
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.1)
    if not runtime_children():
        return
    deadline = loop.time() + 5.0
    while children := runtime_children():
        for pid in children:
            signal_owned(pid, signal.SIGKILL)
        if loop.time() >= deadline:
            raise ControlError("control_children_busy")
        await asyncio.sleep(0.1)
