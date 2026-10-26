"""UID-only binding of a replacement supervisor to one guarded operation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path

from .. import management_journal as contract, management_store, state
from ..execution_state import read_active_execution
from ..layout import layout_for_root
from .administration_lock import process_identity
from .privileged import unit_snapshot
from .processes import RUNTIME_CGROUP, controller_peer, process_cgroup
from .recovery_requests import effective_request, read_grant
from .resumption_contract import digest, stage_reader
from .resumption_ticket import bound_ticket
from .rpc import ControlError


def _journal_identity(journal) -> str:
    return digest(contract.management_journal_payload(replace(journal, stage=contract.ManagementStage.RESERVED, acceptance=None)))


@dataclass(frozen=True)
class RuntimeBinding:
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

    def require_current(self, root: Path) -> bool:
        """Return False only after this exact operation has completed.

        Completed RAM enrollment survives issuer expiry; a stored receipt cannot
        enroll a different process. Unresolved work continues to require its live
        root permission and current controller.
        """
        if (os.getpid() != self.pid or process_identity(self.pid)[0] != self.start_identity
                or process_cgroup(self.pid) != RUNTIME_CGROUP):
            raise ControlError('control_incarnation_changed')
        try:
            return self._require_current(root)
        except (ControlError, contract.ManagementJournalError):
            # Completion can release the lease between the first authority
            # observation and the live-controller proof. Only independently
            # reobserved exact terminal acceptance can settle that race.
            authority = state.inspect_state(root)
            journal = management_store.inspect_management_journal(root, self.operation_id)
            if self._completed(root, authority, journal):
                return False
            raise

    def _completed(self, root, authority, journal) -> bool:
        if _journal_identity(journal) != self.journal_sha256:
            raise ControlError('control_resumption_review_changed')
        operation = authority.active_operation
        terminal = contract.ManagementStage.ROLLED_BACK if journal.recovery_target == 'previous' else contract.ManagementStage.SUCCEEDED
        if ((operation is not None and operation.operation_id == self.operation_id)
                or authority.generation < self.generation or journal.stage is not terminal or journal.acceptance is None):
            return False
        record = read_active_execution(root)
        if (record.installation_id != self.installation_id or not self.generation <= record.generation <= authority.generation
                or record.generation == self.generation and (record.operation_id != self.operation_id
                    or contract.execution_tuple_sha256(record.execution) != self.execution_sha256)
                or record.generation > self.generation and record.operation_id == self.operation_id):
            raise ControlError('control_resumption_review_changed')
        return (state.inspect_state(root) == authority
                and management_store.inspect_management_journal(root, self.operation_id) == journal)

    def _require_current(self, root) -> bool:
        authority = state.inspect_state(root)
        journal = management_store.inspect_management_journal(root, self.operation_id)
        if _journal_identity(journal) != self.journal_sha256:
            raise ControlError('control_resumption_review_changed')
        operation = authority.active_operation
        own_active = operation is not None and operation.operation_id == self.operation_id
        if self._completed(root, authority, journal):
            return False
        if authority.generation != self.generation or not own_active:
            raise ControlError('control_resumption_review_changed')
        current = require_binding(root, journal, self.invocation, control_manifest_sha256=self.control_manifest_sha256,
                                  own_supervisor=True, allow_original=False)
        if current != self:
            raise ControlError('control_resumption_review_changed')
        return True


def require_binding(root: Path, journal, invocation: str, *, control_manifest_sha256: str,
                    own_supervisor: bool = False, allow_original: bool = True) -> RuntimeBinding | None:
    layout = layout_for_root(root)
    selected = layout.descriptor.descriptor
    if not layout.managed or os.geteuid() == 0 or selected.service_uid != os.geteuid():
        raise ControlError('control_worker_forbidden')
    authority = state.inspect_state(root)
    contract.validate_active_journal(journal, authority)
    if journal.installation_id != selected.installation_id:
        raise ControlError('control_incarnation_changed')
    if allow_original and invocation == journal.supervisor_invocation_id:
        return None
    if journal.initial_service_state != 'running' or journal.activation_mode is not contract.ActivationMode.CUTOVER:
        raise ControlError('control_resumption_mode_unavailable')
    reviewed = effective_request(journal)
    grant = read_grant(journal.operation_id, journal.generation)
    if (grant['schema_version'] != 3 or reviewed.previous != journal.previous or reviewed.candidate != journal.candidate):
        raise ControlError('control_resumption_review_changed')
    ticket = bound_ticket(grant['resumption_plan_id'], 'candidate_start', invocation,
        installation_id=journal.installation_id, control_manifest_sha256=control_manifest_sha256)
    plan, receipt = ticket['plan'], ticket['receipt']
    execution = contract.selected_execution(journal)
    if (reviewed.controller_control_manifest_sha256 != plan['control_manifest_sha256']
            or plan['schema_version'] != 3 and reviewed.controller_control_manifest_sha256 != control_manifest_sha256
            or plan['schema_version'] == 3 and (journal.action is not contract.ManagementAction.PROVISION_CONTROL
                or execution.control_manifest_sha256 != control_manifest_sha256
                or stage_reader(plan,'candidate_start') != control_manifest_sha256)
            or plan['operation_id'] != journal.operation_id or plan['generation'] != journal.generation
            or plan['review_id'] != grant['recovery_review_id'] or plan['action'] != journal.action.value
            or plan['stages'][0]['execution_sha256'] != contract.execution_tuple_sha256(execution)):
        raise ControlError('control_resumption_review_changed')
    lease = state.read_controller_lease(root)
    if (lease is None or not state.controller_lease_is_current(lease) or lease.operation_id != journal.operation_id
            or lease.generation != journal.generation or lease.owner_kind is not state.OwnerKind.MANAGEMENT_CONTROLLER):
        raise ControlError('control_controller_changed')
    controller_peer(lease.pid, journal.operation_id)
    unit = unit_snapshot()
    pid = int(unit['MainPID'])
    if (unit['ActiveState'] not in {'active', 'activating'} or unit['InvocationID'] != invocation or pid <= 1
            or process_cgroup(pid) != RUNTIME_CGROUP or Path(f'/proc/{pid}').stat().st_uid != selected.service_uid
            or own_supervisor and pid != os.getpid()):
        raise ControlError('control_incarnation_changed')
    start, status = process_identity(pid)
    if status in {'Z', 'X'} or state.inspect_state(root) != authority:
        raise ControlError('control_incarnation_changed')
    return RuntimeBinding(journal.installation_id, journal.operation_id, journal.generation,
        contract.execution_tuple_sha256(execution), _journal_identity(journal), control_manifest_sha256,
        receipt['plan_sha256'], digest(receipt), pid, start, invocation)


def initial_binding(root: Path, invocation: str, *, control_manifest_sha256: str):
    authority = state.inspect_state(root)
    operation = authority.active_operation
    if operation is None or operation.operation_type is not state.OperationType.MANAGEMENT:
        return None
    journal = management_store.inspect_management_journal(root, operation.operation_id)
    if journal.supervisor_invocation_id == invocation:
        return None
    binding = require_binding(root, journal, invocation, control_manifest_sha256=control_manifest_sha256,
                              own_supervisor=True, allow_original=False)
    return journal, binding
