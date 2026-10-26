"""Exact managed-unit process ownership; no name or port based termination."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import stat
import math
import time

from .rpc import ControlError
from . import entry


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
        rescue=False;operation_rescue=False
        if tail[:1] == [b'--rescue']:
            tail = tail[1:];rescue=True
        if tail[:1]==[b'--control-repair-review-worker']:
            if rescue or len(tail)!=2:return False
            pin=entry.operation_selection(tail[1].decode('ascii'))
            selection=pin or entry._decode(entry._read(entry.CONTROL/'selected.json',4096))
            entry.verify_bundle(selection,rescue=True)
            tail=[b'--worker',b'bound-control-review',tail[1]];operation_rescue=True
        if tail[:1] in ([b'--operation-worker'],[b'--operation-rescue-worker']):
            if rescue or len(tail) not in {3,4}:
                return False
            operation_rescue=tail[0]==b'--operation-rescue-worker'
            if operation_rescue and tail[2] not in {b'bound-control-review',b'bound-control-observe',b'bound-control-result'}:
                return False
            pin=entry.operation_selection(tail[1].decode('ascii'))
            if pin is None:
                return False
            # Keep the same read-only exemption, but prove the full immutable
            # pinned tree instead of trusting a new argv spelling by itself.
            entry.verify_bundle(pin,rescue=operation_rescue)
            tail=[b'--worker',*tail[2:]]
        return (len(tail) in {2, 3} and tail[0] == b'--worker'
            and (operation_rescue and tail[1] in {b'bound-control-review',b'bound-control-observe',b'bound-control-result'}
                or not operation_rescue and tail[1] in {b'inspect', b'identity', b'release-check', b'check-listener',
                            b'runtime-progress', b'runtime-result', b'bridge-observe', b'operation-result',
                            b'resumption-observe', b'resumption-required', b'bound-repair-review', b'bound-repair-result',
                            b'recovery-review', b'parent-recovery-result', b'update-recovery-result'}))
    except (entry.EntryError,OSError,IndexError,ValueError,TypeError):
        return False


def require_service_quiescent(*, preparation_id: str | None = None, inspections: bool = False,
                              controller: tuple[str, int, str] | None = None,
                              startup_hook: tuple[int, str] | None = None,
                              runtime_supervisor: tuple[int, str, str] | None = None,
                              update_delegate: tuple[int, str, int, str] | None = None,
                              settle_timeout: float = 0) -> None:
    """Require a fresh full proof, optionally waiting for transient readers to exit.

    No live process is exempted by this wait. Every attempt repeats the complete
    unit/cgroup/controller/UID scan; unknown identities and metadata errors still
    fail immediately. Persistent foreign work remains a refusal.
    """
    if (type(settle_timeout) not in {int,float} or not math.isfinite(settle_timeout)
            or not 0 <= settle_timeout <= 2):
        raise ControlError('control_quiescence_unproven')
    deadline = time.monotonic() + settle_timeout
    while True:
        try:
            _require_service_quiescent(preparation_id=preparation_id, inspections=inspections,
                controller=controller, startup_hook=startup_hook, runtime_supervisor=runtime_supervisor,
                update_delegate=update_delegate)
            return
        except ControlError as exc:
            if exc.code != 'control_foreign_service_process' or time.monotonic() >= deadline:
                raise
            time.sleep(min(.02, max(0, deadline-time.monotonic())))


def _require_service_quiescent(*, preparation_id: str | None, inspections: bool,
                               controller: tuple[str, int, str] | None,
                               startup_hook: tuple[int, str] | None,
                               runtime_supervisor: tuple[int, str, str] | None,
                               update_delegate: tuple[int, str, int, str] | None = None) -> None:
    """Prove absence of live service-UID work beyond this independent reader."""
    if os.geteuid() == 0:
        raise ControlError("control_worker_forbidden")
    running_members = None
    if runtime_supervisor is not None:
        if controller is not None or startup_hook is not None or preparation_id is None:
            raise ControlError('control_preparation_invalid')
        running_members = _running_preparation_members(runtime_supervisor)
    allowed = None
    controller_pid = None
    delegated_members = set()
    def prove_delegate():
        from .. import managed_update, state
        if (controller is None or not isinstance(update_delegate, tuple) or len(update_delegate) != 4
                or any(type(update_delegate[index]) is not int or update_delegate[index] <= 1 for index in (0, 2))):
            raise ControlError('control_controller_changed')
        root = Path('/opt/streamhome')
        lease = state.read_controller_lease(root)
        if (lease is None or (lease.operation_id, lease.pid, lease.process_start_identity) != controller
                or managed_update.quiescent_delegate(root, lease, helper_pid=update_delegate[2]) != update_delegate):
            raise ControlError('control_controller_changed')
    if update_delegate is not None:
        prove_delegate()
        delegated_members = {update_delegate[0], update_delegate[2]}
    if controller is not None:
        from ..management_journal import _uuid
        from .administration_lock import process_identity
        if not isinstance(controller, tuple) or len(controller) != 3 or type(controller[1]) is not int or controller[1] <= 1:
            raise ControlError('control_controller_changed')
        operation, controller_pid, expected_start = controller
        _uuid(operation, 'owned controller')
        try:
            observed_start, observed_status = process_identity(controller_pid)
            if (observed_start != expected_start or observed_status in {'Z', 'X'}
                    or process_cgroup(controller_pid) != f'/system.slice/streamhome-operation@{operation}.service'
                    or Path(f'/proc/{controller_pid}').stat().st_uid != os.geteuid()):
                raise ControlError('control_controller_changed')
        except FileNotFoundError as exc:
            raise ControlError('control_controller_changed') from exc
    if preparation_id is not None:
        from ..management_journal import _uuid
        _uuid(preparation_id, 'owned preparation')
        allowed = f'/system.slice/streamhome-prepare@{preparation_id}.service'
    startup_members = set()
    if startup_hook is not None:
        from .administration_lock import process_identity
        if (not isinstance(startup_hook, tuple) or len(startup_hook) != 2
                or type(startup_hook[0]) is not int or startup_hook[0] <= 1 or startup_hook[0] != os.getppid()):
            raise ControlError('control_unit_hook_forbidden')
        hook_pid, hook_start = startup_hook
        try:
            observed_start, observed_status = process_identity(hook_pid)
            hook = Path(f'/proc/{hook_pid}')
            arguments = (hook / 'cmdline').read_bytes().split(b'\0')
            if arguments[-1:] == [b'']:arguments.pop()
            expected = [b'/usr/bin/python3', b'-I', b'/usr/local/libexec/streamhome/provision']
            if (observed_start != hook_start or observed_status in {'Z', 'X'} or hook.stat().st_uid != 0
                    or process_cgroup(hook_pid) != RUNTIME_CGROUP or process_cgroup(os.getpid()) != RUNTIME_CGROUP
                    or arguments not in (expected + [b'--before-start'], expected + [b'--rescue', b'--before-start'])):
                raise ControlError('control_unit_hook_forbidden')
        except FileNotFoundError as exc:
            raise ControlError('control_unit_hook_forbidden') from exc
        startup_members = {hook_pid, os.getpid()}
    runtime = Path("/sys/fs/cgroup" + RUNTIME_CGROUP + "/cgroup.procs")
    try:
        members = {int(value) for value in runtime.read_text().split()}
        if ((running_members is not None and members != running_members)
                or running_members is None and ((startup_members and members != startup_members) or (not startup_members and members))):
            raise ControlError("control_children_busy")
    except FileNotFoundError:
        if startup_members or running_members is not None:
            raise ControlError('control_unit_hook_forbidden')
    except ValueError as exc:
        raise ControlError('control_quiescence_unproven') from exc
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
            if ((controller_pid is not None and int(path.name) == controller_pid)
                    or int(path.name) in delegated_members
                    or (running_members is not None and int(path.name) in running_members
                        and process_cgroup(int(path.name)) == RUNTIME_CGROUP)
                    or (allowed is not None and process_cgroup(int(path.name)) == allowed)
                    or (allowed is not None or inspections) and _protected_reader(path, fields)):
                continue
            if fields[0] not in {"Z", "X"}:
                raise ControlError("control_foreign_service_process")
        except FileNotFoundError:
            continue
        except (OSError, IndexError) as exc:
            raise ControlError("control_quiescence_unproven") from exc
    if runtime_supervisor is not None and _running_preparation_members(runtime_supervisor) != running_members:
        raise ControlError('control_incarnation_changed')
    if update_delegate is not None:
        prove_delegate()


def _running_preparation_members(reference: tuple[int, str, str]) -> set[int]:
    """Prove exact owned runtime custody; never authorize a launch or app IO."""
    from .administration_lock import process_identity
    from .privileged import descriptor, supervisor_snapshot, systemctl, unit_snapshot
    if (not isinstance(reference, tuple) or len(reference) != 3 or type(reference[0]) is not int
            or reference[0] <= 1 or not isinstance(reference[1], str) or not isinstance(reference[2], str)):
        raise ControlError('control_preparation_invalid')
    pid, start, invocation = reference
    selected = descriptor()
    unit = unit_snapshot()
    observed_start, status = process_identity(pid)
    if (selected.service_uid != os.geteuid() or unit['ActiveState'] not in {'active', 'activating'}
            or int(unit['MainPID']) != pid or unit['InvocationID'] != invocation
            or observed_start != start or status in {'Z', 'X'} or Path(f'/proc/{pid}').stat().st_uid != os.geteuid()
            or process_cgroup(pid) != RUNTIME_CGROUP
            or systemctl('show', 'streamhome.service', '--property=Job') not in {'Job=', 'Job=0'}):
        raise ControlError('control_incarnation_changed')
    snapshot = supervisor_snapshot(selected, unit)
    if snapshot.get('launches_open') is not True or snapshot.get('start_identity') != start:
        raise ControlError('control_operator_stopped')
    try:
        members = {int(value) for value in Path('/sys/fs/cgroup' + RUNTIME_CGROUP + '/cgroup.procs').read_text().split()}
    except (FileNotFoundError, ValueError) as exc:
        raise ControlError('control_quiescence_unproven') from exc
    if pid not in members:
        raise ControlError('control_incarnation_changed')
    return members


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
