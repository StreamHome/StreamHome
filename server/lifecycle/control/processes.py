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


def _protected_reader(path: Path, fields: list[str]) -> bool:
    """Allow only root-spawned, fixed read-only dispatcher inspections."""
    try:
        parent = Path('/proc') / fields[1]
        if int(fields[1]) <= 1 or parent.stat().st_uid != 0:
            return False
        arguments = (path / 'cmdline').read_bytes().split(b'\0')
        if arguments[-1:] == [b'']:
            arguments.pop()
        prefix = [b'/usr/bin/python3', b'-I', b'/usr/local/libexec/streamhome/provision']
        if arguments[:3] != prefix or (parent / 'cmdline').read_bytes().split(b'\0')[:3] != prefix:
            return False
        tail = arguments[3:]
        if tail[:1] == [b'--rescue']:
            tail = tail[1:]
        return (len(tail) in {2, 3} and tail[0] == b'--worker'
            and tail[1] in {b'inspect', b'identity', b'release-check', b'check-listener',
                            b'runtime-progress', b'runtime-result', b'bridge-observe', b'operation-result'})
    except (OSError, IndexError):
        return False


def require_service_quiescent(*, preparation_id: str | None = None, inspections: bool = False) -> None:
    """Prove absence of live service-UID work beyond this independent reader."""
    if os.geteuid() == 0:
        raise ControlError("control_worker_forbidden")
    allowed = None
    if preparation_id is not None:
        from ..management_journal import _uuid
        _uuid(preparation_id, 'owned preparation')
        allowed = f'/system.slice/streamhome-prepare@{preparation_id}.service'
    runtime = Path("/sys/fs/cgroup" + RUNTIME_CGROUP + "/cgroup.procs")
    try:
        if runtime.read_text().strip():
            raise ControlError("control_children_busy")
    except FileNotFoundError:
        pass
    for path in Path("/proc").iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        try:
            if path.stat().st_uid != os.geteuid():
                continue
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if not fields:
                raise ControlError('control_quiescence_unproven')
            if fields[0] in {'Z', 'X'}:
                continue
            if ((allowed is not None and process_cgroup(int(path.name)) == allowed)
                    or (allowed is not None or inspections) and _protected_reader(path, fields)):
                continue
            if fields[0] not in {"Z", "X"}:
                raise ControlError("control_foreign_service_process")
        except FileNotFoundError:
            continue
        except (OSError, IndexError) as exc:
            raise ControlError("control_quiescence_unproven") from exc


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
