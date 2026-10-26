"""UPDATE controller waits for a reviewed root start; it never dispatches one."""
from __future__ import annotations

import os
from pathlib import Path
import time

from .. import state
from ..managed_update import bound_journal, require_owner_peer
from ..execution_state import read_active_execution
from . import update_grants
from .privileged import unit_snapshot
from .processes import require_service_quiescent
from .stopped_repair import require_stopped
from .rpc import ControlError


def offline(root: Path, proof) -> bool:
    authority, journal, bound = bound_journal(root, proof.lease.operation_id)
    from ..update_recovery import require_controller_permission
    grant = require_controller_permission(root, journal, bound, authority.generation)
    if grant is None or grant['schema_version'] != 2:
        return False
    actor = unit_snapshot()
    if actor['ActiveState'] not in {'inactive', 'failed'}:
        return False
    require_owner_peer(root, proof.lease, proof.lease.pid)
    require_stopped()
    from ..managed_update import quiescent_delegate
    require_service_quiescent(inspections=True, settle_timeout=2,
        controller=(proof.lease.operation_id, proof.lease.pid, proof.lease.process_start_identity),
        update_delegate=quiescent_delegate(root, proof.lease, helper_pid=os.getpid()))
    update_grants.require_binding(grant, bound, read_active_execution(root), journal)
    if (bound_journal(root, proof.lease.operation_id) != (authority, journal, bound)
            or unit_snapshot() != actor):
        raise ControlError('control_resumption_review_changed')
    return True


def wait(root: Path, proof) -> None:
    authority, journal, bound = bound_journal(root, proof.lease.operation_id)
    from ..update_recovery import require_controller_permission
    grant = require_controller_permission(root, journal, bound, authority.generation)
    if grant is None or grant['schema_version'] != 2:
        return
    while True:
        proof.require_current()
        if update_grants.read_grant(bound.parent.operation_id, authority.generation) != grant:
            raise ControlError('control_update_recovery_request_changed')
        actor = unit_snapshot()
        if actor['ActiveState'] == 'active':
            from .update_resumption_runtime import require_binding
            require_binding(root, actor['InvocationID'], control_manifest_sha256=grant['control_manifest_sha256'])
            return
        if actor['ActiveState'] not in {'inactive', 'failed', 'activating'}:
            raise ControlError('control_resumption_outcome_unknown')
        time.sleep(.2)
