"""Close an unaccepted managed UPDATE through its existing controller lease."""
from __future__ import annotations

from pathlib import Path

from . import state, update_checkpoint, update_controller, update_history
from .control.rpc import ControlError
from .control.stopped_repair import require_stopped
from .execution_state import read_active_execution
from .layout import layout_for_root


def require_no_effects(root: Path, journal: dict, bound) -> None:
    """Preserve unknown artifacts rather than treating them as abandoned work."""
    if (bound.accepted is not None or bound.verification is not None or bound.checkpoint is not None
            or journal.get('runtime_committed') is not False
            or journal.get('lifecycle_phase') not in {'reserved', 'recovery_required'}
            or journal.get('phase') not in {'queued', 'preflight', 'waiting_for_idle', 'failed'}
            or journal.get('web_artifacts_swapped') or journal.get('web_dependencies_swapped')):
        raise ControlError('control_update_abort_effects_unresolved')
    checkpoint = update_checkpoint.path_for(root, bound.parent.operation_id)
    # Even an unattributed or dangling entry is evidence, not cleanup permission.
    try:
        checkpoint.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ControlError('control_update_abort_effects_unresolved')
    from .control.provision_worker import _private_record
    from .management_journal import execution_tuple_sha256
    from .runtime_links import verify_links
    receipt = layout_for_root(root).state/'runtime-links.json'
    record = _private_record(receipt)
    if (not isinstance(record, dict)
            or set(record) != {'schema_version', 'operation_id', 'generation', 'previous', 'candidate', 'stage'}
            or type(record['schema_version']) is not int or record['schema_version'] != 1
            or record['stage'] != 'complete' or record['operation_id'] == bound.parent.operation_id
            or record['candidate'] != execution_tuple_sha256(bound.parent.execution)):
        raise ControlError('control_update_abort_effects_unresolved')
    verify_links(root, bound.parent.execution)


def complete(root: Path, lease: state.ControllerLeaseHandle) -> None:
    """Retain terminal evidence before closing the exact original reservation."""
    from .managed_update import require_owner_peer
    import os
    authority, journal, bound = require_owner_peer(root, lease.lease, os.getpid())
    from .update_recovery import require_controller_permission
    grant = require_controller_permission(root, journal, bound, authority.generation)
    if grant is None or grant['action'] != 'safe_abort' or grant['runtime_status'] != 'offline':
        raise ControlError('control_update_abort_permission_required')
    require_stopped()
    require_no_effects(root, journal, bound)
    phase = authority.active_operation.phase
    update_controller.mutate_update_journal(root, operation_id=bound.parent.operation_id,
        generation=authority.generation, expected_lifecycle_phase=phase,
        expected_detail_phases=frozenset({'queued', 'preflight', 'waiting_for_idle', 'failed'}),
        changes={'phase': 'failed', 'message': 'The unresolved update was safely abandoned by explicit operator action.',
            'error': 'operator_safe_abort', 'queued_at': None})
    require_owner_peer(root, lease.lease, os.getpid())
    update_history.retain_terminal(root, lease)
    authority, journal, bound = require_owner_peer(root, lease.lease, os.getpid())
    require_no_effects(root, journal, bound)
    require_stopped()
    if read_active_execution(root).execution != bound.parent.execution:
        raise ControlError('control_parent_binding_changed')
    state.complete_operation_with_credential(root, operation_id=bound.parent.operation_id,
        generation=authority.generation, expected_phase=phase, expected_operation_type=state.OperationType.UPDATE)
