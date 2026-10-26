"""Original live-runtime evidence while its managed UPDATE stays reserved.

Only process-local acknowledgement can admit this continuation. Retained journals
and receipts cannot admit a replacement backend or authorize a controller claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path

from . import state, update_execution
from .execution_state import read_active_execution
from .parent_execution import ParentExecution
from .runtime_build import canonical_bytes
from .control.rpc import ControlError


@dataclass(frozen=True)
class UpdateReservation:
    parent: ParentExecution
    target_commit: str
    automatic: bool
    install_mode: str
    update_branch: str

    @property
    def operation_id(self) -> str:
        return self.parent.operation_id

    @property
    def sha256(self) -> str:
        return sha256(canonical_bytes({'parent': self.parent.payload(),
            'target_commit': self.target_commit, 'automatic': self.automatic,
            'install_mode': self.install_mode, 'update_branch': self.update_branch})).hexdigest()


def reservation(payload: dict) -> UpdateReservation:
    bound = update_execution.parse(payload)
    if bound is None:
        raise ControlError('control_update_continuation_unbound')
    return UpdateReservation(bound.parent, payload['target_commit'], payload['automatic'],
        payload['install_mode'], payload['update_branch'])


def _require_original_runtime(root: Path, pending: UpdateReservation, baseline: int,
                              installation_id: str, invocation: str, execution) -> None:
    from .control.privileged import unit_snapshot
    from .control.processes import RUNTIME_CGROUP, process_cgroup
    from .control.tickets import read_stop_epoch
    parent = pending.parent
    active = read_active_execution(root)
    actor = unit_snapshot()
    if (parent.original_generation != baseline+1 or parent.installation_id != installation_id
            or parent.supervisor_invocation_id != invocation or parent.execution != execution
            or os.getppid() != parent.supervisor_pid
            or state._process_start_identity(parent.supervisor_pid) != parent.supervisor_start_identity
            or process_cgroup(os.getpid()) != RUNTIME_CGROUP
            or actor['ActiveState'] != 'active' or int(actor['MainPID']) != parent.supervisor_pid
            or actor['InvocationID'] != invocation or read_stop_epoch() != parent.stop_epoch
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != parent.boot_id
            or active.installation_id != installation_id or active.execution != execution
            or active.generation >= parent.original_generation):
        raise ControlError('control_update_continuation_changed')


def require_live(root: Path, pending: UpdateReservation, baseline: int, installation_id: str,
                 invocation: str, execution):
    from .managed_update import bound_journal, require_owner_peer
    authority, payload, bound = bound_journal(root, pending.operation_id)
    if (reservation(payload) != pending or bound.accepted is not None or bound.verification is not None
            or authority.active_operation.phase is not state.LifecyclePhase.RESERVED
            or payload['phase'] not in {'queued', 'preflight', 'waiting_for_idle'}
            or authority.generation not in {pending.parent.original_generation, pending.parent.original_generation+1}):
        raise ControlError('control_update_continuation_changed')
    _require_original_runtime(root, pending, baseline, installation_id, invocation, execution)
    lease = state.read_controller_lease(root)
    if authority.generation == pending.parent.original_generation:
        if payload['phase'] != 'queued' or lease is not None and lease.generation >= authority.generation:
            raise ControlError('control_controller_changed')
    elif lease is None or require_owner_peer(root, lease, lease.pid) != (authority, payload, bound):
        raise ControlError('control_controller_changed')
    if bound_journal(root, pending.operation_id) != (authority, payload, bound):
        raise ControlError('control_update_continuation_changed')
    return authority


def require_completed(root: Path, pending: UpdateReservation, baseline: int, installation_id: str,
                      invocation: str, execution, authority: state.LifecycleState) -> None:
    from .update_history import _path, _record, require_retained
    from .control.provision_worker import _private_record
    record = _private_record(_path(root, pending.operation_id, authority.generation))
    payload = record.get('journal')
    if not isinstance(payload, dict):
        raise ControlError('control_update_history_changed')
    bound = update_execution.parse(payload)
    if (bound is None or authority.schema_version != 2 or authority.active_operation is not None or authority.generation not in {
                pending.parent.original_generation, pending.parent.original_generation+1}
            or payload.get('lifecycle_generation') != authority.generation
            or record != _record(payload) or reservation(payload) != pending
            or bound.accepted is not None or bound.verification is not None):
        raise ControlError('control_update_continuation_changed')
    _require_original_runtime(root, pending, baseline, installation_id, invocation, execution)
    require_retained(root, payload, authority)


@dataclass(frozen=True)
class UnadoptedUpdateClaim:
    authority: state.LifecycleState
    parent: ParentExecution


def capture_claim(root: Path, authority: state.LifecycleState, baseline: int,
                  installation_id: str, invocation: str, execution) -> UnadoptedUpdateClaim:
    """Record original settled-process evidence without a supervisor RPC cycle."""
    from .control.tickets import read_stop_epoch
    from .parent_execution import parse_parent_execution
    from .management_journal import execution_tuple_payload
    operation = authority.active_operation
    if (authority.schema_version != 2 or authority.generation != baseline+1 or operation is None
            or operation.operation_type is not state.OperationType.UPDATE
            or operation.owner_kind is not state.OwnerKind.UPDATER
            or operation.phase is not state.LifecyclePhase.RESERVED):
        raise ControlError('control_update_claim_changed')
    parent_pid = os.getppid()
    parent = parse_parent_execution({'schema_version': 1, 'installation_id': installation_id,
        'operation_id': operation.operation_id, 'operation_type': 'update',
        'original_generation': authority.generation, 'execution': execution_tuple_payload(execution),
        'supervisor_invocation_id': invocation, 'supervisor_pid': parent_pid,
        'supervisor_start_identity': state._process_start_identity(parent_pid),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'stop_epoch': read_stop_epoch()})
    intent = UnadoptedUpdateClaim(authority, parent)
    _require_original_runtime(root, intent, baseline, installation_id, invocation, execution)
    if state.inspect_state(root) != authority:
        raise ControlError('control_update_claim_changed')
    return intent


def require_unjournaled(root: Path, intent: UnadoptedUpdateClaim) -> None:
    """Unknown or adopted evidence is never a failed-reservation shortcut."""
    from .update_controller import _update_paths
    from .control.provision_worker import _private_record
    path = _update_paths(root, intent.parent.operation_id, intent.authority.generation)[0]
    if path.exists() or path.is_symlink():
        payload = _private_record(path)
        update_execution.parse(payload)
        if payload.get('transaction_id') == intent.parent.operation_id:
            raise ControlError('control_update_claim_adopted')
    lease = state.read_controller_lease(root)
    if lease is not None and lease.operation_id == intent.parent.operation_id:
        raise ControlError('control_update_claim_adopted')


def require_completed_claim(root: Path, intent: UnadoptedUpdateClaim, completed: state.CompletionResult,
                            baseline: int, installation_id: str, invocation: str, execution,
                            current: state.LifecycleState) -> None:
    expected = state.LifecycleState(2, intent.authority.generation, None)
    if (completed.state != expected or current.schema_version != 2
            or current.generation < expected.generation
            or current.active_operation is not None and current.active_operation.operation_id == intent.parent.operation_id):
        raise ControlError('control_update_claim_changed')
    _require_original_runtime(root, intent, baseline, installation_id, invocation, execution)
