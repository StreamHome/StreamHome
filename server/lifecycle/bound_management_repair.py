"""Independent MANAGEMENT dependency repair that never completes its parent."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import time

from . import bound_runtime_repair as artifacts, management_journal as contract, management_recovery_store as recovery, management_store, state
from .control import recovery_requests
from .control.requests import parse_management_request, read_management_request
from .control.rpc import ControlError
from .control.stopped_repair import require_stopped
from .control.processes import require_service_quiescent
from .layout import layout_for_root
from .management_controller import RecoveredManagementController
from .runtime_build import canonical_bytes, verify_runtime


ROOT = Path('/opt/streamhome')


def _digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def plan(operation_id: str, *, control_manifest_sha256: str) -> dict:
    layout = layout_for_root(ROOT)
    if os.geteuid() == 0 or not layout.managed or layout.descriptor.descriptor.service_uid != os.geteuid():
        raise ControlError('control_worker_forbidden')
    require_stopped();require_service_quiescent(inspections=True)
    base = recovery_requests.review(operation_id, control_manifest_sha256=control_manifest_sha256)
    authority = state.inspect_state(ROOT)
    maintenance = recovery.inspect_recovery(ROOT, operation_id, authority.generation, maintenance=True)
    journal = management_store.inspect_management_journal(ROOT, operation_id)
    effective, _ = recovery._effective_journal(ROOT, authority, journal)
    execution = contract.selected_execution(effective)
    from .control.reader_support import require_bound_repair_reader
    require_bound_repair_reader(execution)
    bound = artifacts.inspect(ROOT, layout.cache / 'runtime-recovery', execution, checkpoint=lambda: None)
    value = {**base, 'permitted_actions': [recovery.MAINTENANCE_ACTION],
        'source_evidence_sha256': maintenance['evidence_sha256'], 'artifacts': bound,
        'bound_bundle_id': execution.runtime_bundle_id, 'bound_manifest_sha256': execution.runtime_manifest_sha256}
    value.pop('evidence_sha256')
    if state.inspect_state(ROOT) != authority or management_store.inspect_management_journal(ROOT, operation_id) != journal:
        raise ControlError('control_recovery_review_stale')
    require_stopped();require_service_quiescent(inspections=True)
    return {**value, 'evidence_sha256': _digest(value)}


def prepare(payload: dict, *, control_manifest_sha256: str) -> dict:
    fields = {'operation_id', 'expected_evidence_sha256', 'recovery_review_id', 'issuer_scope', 'boot_id', 'stop_epoch'}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ControlError('control_request_invalid')
    current = plan(payload['operation_id'], control_manifest_sha256=control_manifest_sha256)
    if current['evidence_sha256'] != payload['expected_evidence_sha256']:
        raise ControlError('control_recovery_review_stale')
    original = read_management_request(payload['operation_id'])
    request = replace(original, expected_generation=current['generation'], backend=None,
        boot_id=payload['boot_id'], stop_epoch=payload['stop_epoch'])
    return recovery_requests.parse_grant({'schema_version': 4, 'action': recovery.MAINTENANCE_ACTION,
        'recovery_review_id': payload['recovery_review_id'], 'source_evidence_sha256': current['source_evidence_sha256'],
        'source_request_sha256': original.sha256, 'request': request.payload(), 'issuer_scope': payload['issuer_scope'],
        'repair_review_sha256': current['evidence_sha256'], 'execution_sha256': current['selected_execution_sha256'],
        'filesystem_sha256': current['artifacts']['filesystem_sha256'], 'expires_at_monotonic': time.monotonic() + 900})


class BoundEnvironmentController(RecoveredManagementController):
    def __init__(self, grant, *, control_manifest_sha256):
        self._permission_check_at = 0.0
        super().__init__(grant, control_manifest_sha256=control_manifest_sha256)
        if self.grant['schema_version'] != 4:
            raise ControlError('control_recovery_request_invalid')

    def checkpoint(self) -> None:
        from .control.administration_lock import process_identity
        from .control.tickets import read_stop_epoch
        scope = self.grant['issuer_scope']
        identity, status = process_identity(scope['pid'])
        if (self.cancelled or status in {'Z', 'X'} or identity != scope['start_identity']
                or time.monotonic() >= self.grant['expires_at_monotonic']
                or read_stop_epoch() != self.review.stop_epoch):
            raise ControlError('control_operator_stopped')
        if self.lease is not None:
            self.lease.require_current()
        if time.monotonic() >= self._permission_check_at:
            super().checkpoint()
            self._permission_check_at = time.monotonic() + 1

    def execute(self) -> None:
        self.checkpoint()
        source = plan(self.review.operation_id, control_manifest_sha256=self.review.controller_control_manifest_sha256)
        if (source['evidence_sha256'] != self.grant['repair_review_sha256']
                or source['selected_execution_sha256'] != self.grant['execution_sha256']
                or source['artifacts']['filesystem_sha256'] != self.grant['filesystem_sha256']):
            raise ControlError('control_recovery_review_stale')
        def recheck():
            self.checkpoint()
            if plan(self.review.operation_id, control_manifest_sha256=self.review.controller_control_manifest_sha256) != source:
                raise ControlError('control_recovery_review_stale')
        self.lease, self.claim, self.journal = recovery.acquire_recovery_lease(ROOT,
            operation_id=self.review.operation_id, generation=self.review.expected_generation,
            evidence_sha256=self.grant['source_evidence_sha256'], review_id=self.grant['recovery_review_id'],
            action=recovery.MAINTENANCE_ACTION, controller_invocation_id=self.invocation, pre_transfer_check=recheck)
        execution = contract.selected_execution(self.journal)
        _, path = management_store._journal_path(ROOT, self.journal.operation_id)
        receipt = path.parent / f'environment-repair-{self.journal.generation}.json'
        def quiescent():
            self.checkpoint();require_stopped();require_service_quiescent(inspections=True)
        result = artifacts.reconstruct(ROOT, self.layout.cache / 'runtime-recovery', execution,
            expected=source['artifacts'], receipt=receipt, checkpoint=self.checkpoint, quiescent=quiescent)
        quiescent()
        state._atomic_write_json(path.parent / f'environment-repair-result-{self.journal.generation}.json', {
            'schema_version': 1, 'operation_id': self.journal.operation_id, 'generation': self.journal.generation,
            'action': recovery.MAINTENANCE_ACTION, 'grant_sha256': _digest(self.grant),
            'execution_sha256': contract.execution_tuple_sha256(execution), 'receipt_sha256': _digest(result)})
        # No acceptance, configuration/link publication, start or parent
        # completion is permitted. The next continuation needs a fresh review.


def result(operation_id: str) -> dict:
    authority = state.inspect_state(ROOT)
    operation = authority.active_operation
    if (operation is None or operation.operation_id != operation_id or operation.operation_type is not state.OperationType.MANAGEMENT
            or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED):
        raise ControlError('control_authority_changed')
    journal = management_store.inspect_management_journal(ROOT, operation_id)
    contract.validate_active_journal(journal, authority)
    grant = recovery_requests.read_grant(operation_id, authority.generation)
    if grant['schema_version'] != 4:
        raise ControlError('control_recovery_request_invalid')
    from .control.provision_worker import _private_record
    _, path = management_store._journal_path(ROOT, operation_id)
    receipt = _private_record(path.parent / f'environment-repair-{authority.generation}.json')
    value = _private_record(path.parent / f'environment-repair-result-{authority.generation}.json')
    execution = contract.selected_execution(journal)
    expected = {'schema_version': 1, 'operation_id': operation_id, 'generation': authority.generation,
        'action': recovery.MAINTENANCE_ACTION, 'grant_sha256': _digest(grant),
        'execution_sha256': contract.execution_tuple_sha256(execution), 'receipt_sha256': _digest(receipt)}
    if value != expected or receipt.get('status') != 'complete' or grant['execution_sha256'] != expected['execution_sha256']:
        raise ControlError('control_recovery_request_changed')
    require_stopped();verify_runtime(ROOT, execution)
    return {'completed': True, 'stage': 'environment_repaired_recovery_required', 'generation': authority.generation}
