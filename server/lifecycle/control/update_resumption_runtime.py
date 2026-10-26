"""RAM enrollment for one reviewed replacement UPDATE supervisor."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .. import state, update_controller, update_execution, update_history
from ..execution_state import read_active_execution
from ..layout import layout_for_root
from ..managed_update import bound_journal, require_owner_peer
from ..management_journal import execution_tuple_sha256
from ..update_recovery import digest, request_sha256
from . import update_grants
from .administration_lock import process_identity
from .privileged import unit_snapshot
from .processes import RUNTIME_CGROUP, process_cgroup
from .provision_worker import _private_record
from .resumption_ticket import bound_ticket
from .rpc import ControlError


def identity(journal: dict) -> str:
    return digest({'parent': journal['managed_execution'], 'candidate': journal['managed_candidate'],
        'request': request_sha256(journal)})


@dataclass(frozen=True)
class UpdateRuntimeBinding:
    installation_id: str
    operation_id: str
    generation: int
    execution_sha256: str
    journal_sha256: str
    control_manifest_sha256: str
    plan_sha256: str
    receipt_sha256: str
    pid: int
    start_identity: str
    invocation: str

    def _completed(self, root: Path, authority) -> bool:
        operation = authority.active_operation
        if (authority.generation < self.generation
                or operation is not None and operation.operation_id == self.operation_id):
            return False
        record = _private_record(update_history._path(root, self.operation_id, self.generation))
        journal = record.get('journal')
        if (not isinstance(journal, dict) or record != update_history._record(journal)
                or record['operation_id'] != self.operation_id or record['generation'] != self.generation
                or identity(journal) != self.journal_sha256):
            raise ControlError('control_resumption_review_changed')
        bound = update_execution.parse(journal)
        active = read_active_execution(root)
        if (bound.accepted is None or execution_tuple_sha256(bound.accepted) != self.execution_sha256
                or active.installation_id != self.installation_id
                or not self.generation <= active.generation <= authority.generation
                or active.generation == self.generation and (active.operation_id != self.operation_id
                    or execution_tuple_sha256(active.execution) != self.execution_sha256)
                or active.generation > self.generation and active.operation_id == self.operation_id):
            raise ControlError('control_resumption_review_changed')
        return state.inspect_state(root) == authority

    def require_current(self, root: Path) -> bool:
        if (os.getpid() != self.pid or process_identity(self.pid)[0] != self.start_identity
                or process_cgroup(self.pid) != RUNTIME_CGROUP):
            raise ControlError('control_incarnation_changed')
        authority = state.inspect_state(root)
        if self._completed(root, authority):
            return False
        try:
            current = require_binding(root, self.invocation, control_manifest_sha256=self.control_manifest_sha256,
                own_supervisor=True)
            if current != self:
                raise ControlError('control_resumption_review_changed')
            return True
        except (ControlError, OSError, ValueError, update_controller.UpdateControllerError, state.LifecycleError):
            if self._completed(root, state.inspect_state(root)):
                return False
            raise


def require_binding(root: Path, invocation: str, *, control_manifest_sha256: str,
                    own_supervisor=False) -> UpdateRuntimeBinding:
    # Acceptance/progress can publish between two independent read-only
    # observations. Retry only that positively identified same-owner race.
    # Changed OS identity, permission, tuple or canonical owner still fails.
    for _ in range(8):
        try:
            return _binding_once(root, invocation, control_manifest_sha256=control_manifest_sha256,
                own_supervisor=own_supervisor)
        except ControlError as exc:
            if exc.code != 'control_update_observation_changed':
                raise
    raise ControlError('control_update_observation_changed')


def _binding_once(root: Path, invocation: str, *, control_manifest_sha256: str,
                  own_supervisor=False) -> UpdateRuntimeBinding:
    layout = layout_for_root(root)
    if not layout.managed or os.geteuid() == 0 or layout.descriptor.descriptor.service_uid != os.geteuid():
        raise ControlError('control_worker_forbidden')
    authority = state.inspect_state(root);operation = authority.active_operation
    if operation is None or operation.operation_type is not state.OperationType.UPDATE:
        raise ControlError('control_resumption_review_changed')
    observed, journal, bound = bound_journal(root, operation.operation_id)
    selected = bound.accepted or bound.parent.execution
    if (observed != authority or operation.owner_kind is not state.OwnerKind.UPDATER
            or operation.phase not in {state.LifecyclePhase.VERIFYING, state.LifecyclePhase.RECOVERY_REQUIRED}
            or bound.parent.installation_id != layout.descriptor.descriptor.installation_id
            or bound.parent.execution.control_manifest_sha256 != control_manifest_sha256
            or invocation == bound.parent.supervisor_invocation_id or bound.verification != selected):
        raise ControlError('control_resumption_review_changed')
    grant = update_grants.read_grant(operation.operation_id, authority.generation)
    update_grants.require_binding(grant, bound, read_active_execution(root), journal)
    if grant['schema_version'] != 2:
        raise ControlError('control_resumption_mode_unavailable')
    ticket = bound_ticket(grant['resumption_plan_id'], 'candidate_start', invocation,
        installation_id=bound.parent.installation_id, control_manifest_sha256=control_manifest_sha256)
    plan, receipt = ticket['plan'], ticket['receipt']
    reviewed = update_grants.require_resumption_plan(grant)
    if (reviewed['plan'] != plan or plan['generation'] != authority.generation
            or plan['stages'][0]['execution_sha256'] != execution_tuple_sha256(selected)):
        raise ControlError('control_resumption_review_changed')
    lease = state.read_controller_lease(root)
    def same_progress(record):
        current_authority, current_journal, current_bound = record
        return (current_authority == authority and current_journal != journal
            and identity(current_journal) == identity(journal)
            and current_journal.get('managed_database_checkpoint') == journal.get('managed_database_checkpoint')
            and current_bound.verification == selected
            and (current_bound.accepted or current_bound.parent.execution) == selected
            and (bound.accepted is None or current_bound.accepted == bound.accepted))
    if lease is None:
        raise ControlError('control_controller_changed')
    try:
        owner = require_owner_peer(root, lease, lease.pid)
    except ControlError as exc:
        if (exc.code == 'control_controller_changed' and state.read_controller_lease(root) == lease
                and state.controller_lease_is_current(lease)
                and same_progress(bound_journal(root, operation.operation_id))):
            raise ControlError('control_update_observation_changed') from exc
        raise
    if owner != (authority, journal, bound):
        if same_progress(owner):
            raise ControlError('control_update_observation_changed')
        raise ControlError('control_controller_changed')
    unit = unit_snapshot();pid = int(unit['MainPID'])
    if (unit['ActiveState'] not in {'active', 'activating'} or unit['InvocationID'] != invocation or pid <= 1
            or process_cgroup(pid) != RUNTIME_CGROUP or Path(f'/proc/{pid}').stat().st_uid != os.geteuid()
            or own_supervisor and pid != os.getpid()):
        raise ControlError('control_incarnation_changed')
    start, status = process_identity(pid)
    if status in {'Z','X'} or state.inspect_state(root) != authority:
        raise ControlError('control_incarnation_changed')
    fresh_authority, fresh_journal, fresh_bound = bound_journal(root, operation.operation_id)
    if (fresh_authority, fresh_journal, fresh_bound) != (authority, journal, bound):
        if same_progress((fresh_authority, fresh_journal, fresh_bound)):
            raise ControlError('control_update_observation_changed')
        raise ControlError('control_resumption_review_changed')
    return UpdateRuntimeBinding(bound.parent.installation_id, operation.operation_id, authority.generation,
        execution_tuple_sha256(selected), identity(journal), control_manifest_sha256,
        receipt['plan_sha256'], digest(receipt), pid, start, invocation)


def initial_binding(root: Path, invocation: str, *, control_manifest_sha256: str):
    authority = state.inspect_state(root);operation = authority.active_operation
    if operation is None or operation.operation_type is not state.OperationType.UPDATE:
        return None
    binding = require_binding(root, invocation, control_manifest_sha256=control_manifest_sha256, own_supervisor=True)
    observed, journal, bound = bound_journal(root, operation.operation_id)
    if observed != authority:
        raise ControlError('control_resumption_review_changed')
    return journal, bound, binding
