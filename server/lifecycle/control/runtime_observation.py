"""Read managed process custody without importing replaceable application code."""
from __future__ import annotations

import os
from pathlib import Path

from .. import state
from ..execution_state import read_active_execution
from ..layout import layout_for_root
from ..update_controller import UpdateControllerError
from ..management_journal import execution_tuple_sha256
from .administration_lock import process_identity
from .processes import RUNTIME_CGROUP, process_cgroup, require_service_quiescent
from .privileged import supervisor_snapshot, unit_snapshot
from .rpc import ControlError
from .stopped_repair import require_stopped


def _execution_matches(root: Path, snapshot: dict, active, actor: dict) -> bool:
    authority = state.inspect_state(root)
    operation = authority.active_operation
    if operation is None or operation.operation_type is not state.OperationType.UPDATE:
        return snapshot.get('execution_sha256') == execution_tuple_sha256(active.execution)
    from ..update_recovery_observation import read_binding
    from .tickets import read_stop_epoch
    current, journal, bound, witness = read_binding(root, operation.operation_id)
    parent = bound.parent
    if (current != authority or parent.supervisor_pid != int(actor['MainPID'])
            or parent.supervisor_invocation_id != actor['InvocationID']
            or snapshot.get('pid') != parent.supervisor_pid
            or snapshot.get('invocation') != parent.supervisor_invocation_id
            or snapshot.get('start_identity') != parent.supervisor_start_identity
            or parent.boot_id != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or parent.stop_epoch != read_stop_epoch()):
        return False
    # Before acceptance, the candidate can be running while the durable active
    # record still selects the previous tuple. This is custody, never admission.
    allowed = (bound.accepted,) if bound.accepted is not None else (parent.execution, bound.candidate)
    if snapshot.get('execution_sha256') not in {execution_tuple_sha256(value) for value in allowed if value is not None}:
        return False
    previous = active.execution == parent.execution and active.generation < parent.original_generation
    accepted = (bound.accepted is not None and active.execution == bound.accepted
        and active.operation_id == parent.operation_id
        and parent.original_generation < active.generation <= authority.generation)
    if not (previous or accepted):
        return False
    return (read_binding(root, operation.operation_id) == (authority, journal, bound, witness)
        and read_stop_epoch() == parent.stop_epoch)


def runtime_status(root: Path) -> str:
    """A surviving supervisor is active even with no application children.

    In particular, safe abort must not release canonical authority while a
    surviving backend/supervisor retains process-local fenced admission. Missing
    runtime records or unused ports alone never establish an offline runtime.
    """
    try:
        layout = layout_for_root(root)
        if (not layout.managed or os.geteuid() == 0
                or layout.descriptor.descriptor.service_uid != os.geteuid()):
            return 'unknown'
        selected = layout.descriptor.descriptor
        before = unit_snapshot()
        if before['ActiveState'] in {'inactive', 'failed'} and int(before['MainPID']) == 0:
            require_stopped()
            require_service_quiescent(inspections=True, settle_timeout=2)
            return 'offline' if unit_snapshot() == before else 'unknown'
        if before['ActiveState'] != 'active' or int(before['MainPID']) <= 1:
            return 'unknown'
        pid = int(before['MainPID'])
        snapshot = supervisor_snapshot(selected, before)
        start, status = process_identity(pid)
        active = read_active_execution(root)
        if (status in {'Z', 'X'} or snapshot.get('start_identity') != start
                or snapshot.get('launches_open') is not True
                or process_cgroup(pid) != RUNTIME_CGROUP
                or Path(f'/proc/{pid}').stat().st_uid != selected.service_uid
                or active.installation_id != selected.installation_id
                or not _execution_matches(root, snapshot, active, before)
                or unit_snapshot() != before):
            return 'unknown'
        return 'active'
    except (state.LifecycleError, UpdateControllerError, ControlError, OSError, ValueError, TypeError, KeyError):
        return 'unknown'
