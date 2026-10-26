"""Retained completed UPDATE evidence, never an alternative recovery authority."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from . import state, update_controller, update_execution
from .execution_state import read_active_execution
from .runtime_build import canonical_bytes
from .control.provision_worker import _private_record
from .control.rpc import ControlError

if TYPE_CHECKING:
    from .managed_update import UpdateDelegate


def _record(payload: dict) -> dict:
    bound = update_execution.parse(payload)
    if bound is None:
        raise ControlError('control_update_history_unbound')
    phase = payload.get('phase')
    if bound.accepted is not None:
        if phase not in {'succeeded', 'rolled_back'} or payload.get('lifecycle_phase') != 'verifying':
            raise ControlError('control_update_history_nonterminal')
        if phase == 'rolled_back' and bound.accepted != bound.parent.execution:
            raise ControlError('control_update_history_nonterminal')
    elif (phase not in {'failed', 'update_available'}
            or payload.get('lifecycle_phase') != 'reserved' and not (
                phase == 'failed' and payload.get('lifecycle_phase') == 'recovery_required'
                and payload.get('error') == 'operator_safe_abort')
            or bound.checkpoint is not None or bound.verification is not None
            or payload.get('web_artifacts_swapped') or payload.get('web_dependencies_swapped')):
        raise ControlError('control_update_history_nonterminal')
    from hashlib import sha256
    record = {'schema_version': 1, 'operation_id': bound.parent.operation_id,
        'generation': payload['lifecycle_generation'], 'journal_sha256': sha256(canonical_bytes(payload)).hexdigest(),
        'journal': payload}
    if len(canonical_bytes(record)) > 32768:
        raise ControlError('control_update_history_too_large')
    return record


def _path(root: Path, operation: str, generation: int, *, create=False) -> Path:
    if (str(uuid.UUID(operation)) != operation or type(generation) is not int
            or not 1 <= generation < 2**63):
        raise ControlError('control_update_history_unbound')
    directory = state.lifecycle_paths(root).lifecycle_dir/'update-history'
    if create:
        state._ensure_private_directory(directory, 'retained update history')
    else:
        state._validate_directory(directory, 'retained update history')
    return directory/(operation+'.'+str(generation)+'.json')


def retain_terminal(root: Path, lease: state.ControllerLeaseHandle | UpdateDelegate) -> dict:
    """Retain the exact final journal before the original owner completes."""
    from .managed_update import UpdateDelegate, bound_journal
    lease.require_current()
    authority, payload, bound = bound_journal(root, lease.lease.operation_id)
    if (lease.paths.root != root or lease.lease.generation != authority.generation
            or lease.lease.owner_kind is not state.OwnerKind.UPDATER
            or lease.lease.pid != os.getpid() and not isinstance(lease, UpdateDelegate)):
        raise ControlError('control_controller_changed')
    record = _record(payload)
    active = read_active_execution(root)
    if (active.installation_id != bound.parent.installation_id
            or active.execution != (bound.accepted or bound.parent.execution)
            or bound.accepted is not None and (active.operation_id != bound.parent.operation_id
                or active.generation != authority.generation)):
        raise ControlError('control_parent_binding_changed')
    path = _path(root, bound.parent.operation_id, authority.generation, create=True)
    lease.require_current()
    with state.hold_operation_with_credential(root, operation_id=bound.parent.operation_id,
            generation=authority.generation, expected_operation_type=state.OperationType.UPDATE,
            expected_phase=authority.active_operation.phase) as current:
        journal_path, _, _ = update_controller._update_paths(root, bound.parent.operation_id, authority.generation)
        if current != authority or update_controller.read_update_journal(journal_path) != payload:
            raise ControlError('control_parent_binding_changed')
        if path.exists() or path.is_symlink():
            if _private_record(path) != record:
                raise ControlError('control_update_history_changed')
        else:
            state._atomic_write_json(path, record)
    return record


def require_retained(root: Path, payload: dict, authority: state.LifecycleState) -> None:
    """Read-only proof for a caller already holding idle/new-reservation authority."""
    record = _record(payload)
    generation = record['generation']
    operation = authority.active_operation
    if (authority.schema_version != 2 or authority.generation < generation
            or operation is not None and (authority.generation <= generation
                or operation.operation_type is not state.OperationType.UPDATE
                or operation.owner_kind is not state.OwnerKind.UPDATER
                or operation.phase is not state.LifecyclePhase.RESERVED
                or operation.operation_id == record['operation_id'])):
        raise ControlError('control_update_history_unresolved')
    if _private_record(_path(root, record['operation_id'], generation)) != record:
        raise ControlError('control_update_history_changed')
    lease = state.read_controller_lease(root)
    if lease is not None and lease.operation_id == record['operation_id'] and state.controller_lease_is_current(lease):
        raise ControlError('control_controller_busy')


def retain_unstarted(root: Path, pending, baseline: int, installation_id: str,
                     invocation: str, execution) -> dict:
    """Retain the original backend's terminal queued reservation, without a lease."""
    from .managed_update import bound_journal
    from .update_continuation import reservation, _require_original_runtime
    import fcntl
    operation = pending.parent.operation_id
    path, lock_path, _ = update_controller._update_paths(root, operation, pending.parent.original_generation)
    descriptor = state._open_locked_file(lock_path)
    try:
        authority, payload, bound = bound_journal(root, operation)
        if (authority.generation != pending.parent.original_generation
                or authority.active_operation.phase is not state.LifecyclePhase.RESERVED
                or reservation(payload) != pending or bound.candidate is not None
                or bound.accepted is not None or bound.verification is not None
                or payload.get('controller_pid') is not None or payload.get('staging_dir')):
            raise ControlError('control_update_continuation_changed')
        record = _record(payload)
        target = _path(root, operation, authority.generation, create=True)
        with state.hold_operation_with_credential(root, operation_id=operation,
                generation=authority.generation, expected_phase=state.LifecyclePhase.RESERVED,
                expected_operation_type=state.OperationType.UPDATE) as current:
            _require_original_runtime(root, pending, baseline, installation_id, invocation, execution)
            lease = state.read_controller_lease(root)
            if (current != authority or update_controller.read_update_journal(path) != payload
                    or lease is not None and lease.operation_id == operation):
                raise ControlError('control_update_continuation_changed')
            if target.exists() or target.is_symlink():
                if _private_record(target) != record:
                    raise ControlError('control_update_history_changed')
            else:
                state._atomic_write_json(target, record)
        return record
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)
