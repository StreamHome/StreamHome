"""Read a narrowly witnessed UPDATE transfer gap without normalizing any state.

The existing canonical action intent and retained protected root grant explain one
transfer only. An expired issuer can be historical evidence, never new permission.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from . import recovery, state, update_controller, update_execution
from .execution_state import read_active_execution
from .layout import layout_for_root
from . import managed_update
from .update_recovery import digest
from .control import update_grants
from .control.rpc import ControlError


def read_binding(root: Path, operation_id: str):
    try:
        authority, journal, bound = managed_update.bound_journal(root, operation_id)
        return authority, journal, bound, None
    except update_controller.UpdateControllerConflict:
        pass
    authority = state.inspect_state(root)
    operation = authority.active_operation
    if (operation is None or operation.operation_id != operation_id
            or operation.operation_type is not state.OperationType.UPDATE
            or operation.owner_kind is not state.OwnerKind.UPDATER
            or operation.phase not in {state.LifecyclePhase.RESERVED, state.LifecyclePhase.RECOVERY_REQUIRED}):
        raise ControlError('control_update_transfer_gap_unproven')
    path, _, _ = update_controller._update_paths(root, operation_id, authority.generation)
    journal = update_controller.read_update_journal(path)
    bound = update_execution.parse(journal)
    layout = layout_for_root(root)
    if (bound is None or not layout.managed or os.geteuid() == 0
            or layout.descriptor.descriptor.service_uid != os.geteuid()
            or layout.descriptor.descriptor.installation_id != bound.parent.installation_id
            or journal['lifecycle_generation'] not in {authority.generation-1, authority.generation}):
        raise ControlError('control_update_transfer_gap_unproven')
    old = update_grants.read_grant(operation_id, authority.generation, live=False)
    if old['schema_version'] in {3,4}:
        from .update_repair_intents import gap_witness
        virtual = state.LifecycleState(authority.schema_version, journal['lifecycle_generation'], operation)
        update_controller._validate_update_journal_semantics(journal, authority=virtual)
        witness = gap_witness(root, authority, journal)
        if (state.inspect_state(root) != authority or update_controller.read_update_journal(path) != journal
                or update_grants.read_grant(operation_id, authority.generation, live=False) != old):
            raise ControlError('control_update_repair_gap_unproven')
        return authority, journal, bound, witness
    phase_only = journal['lifecycle_generation'] == authority.generation
    if (not phase_only and (journal['lifecycle_phase'] != old['source_lifecycle_phase']
            or operation.phase.value != old['source_lifecycle_phase'] and not (
                old['action'] == 'safe_abort' and old['source_lifecycle_phase'] == 'reserved'
                and operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED))
            or old['source_lifecycle_phase'] == 'reserved' and old['action'] != 'safe_abort'):
        raise ControlError('control_update_transfer_gap_unproven')
    virtual = state.LifecycleState(authority.schema_version, journal['lifecycle_generation'],
        replace(operation, phase=state.LifecyclePhase(journal['lifecycle_phase'])))
    update_controller._validate_update_journal_semantics(journal, authority=virtual)
    active = read_active_execution(root)
    update_grants.require_historical_binding(old, bound, active, journal)
    if journal['lifecycle_generation'] == authority.generation:
        # A caught abort may fence canonical authority before its subordinate
        # phase write succeeds. The acknowledged original action proves this
        # metadata-only gap; an expired issuer grants no continuation rights.
        abort = old['action'] == 'safe_abort'
        if (operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
                or journal['lifecycle_phase'] not in ({'reserved'} if abort else {'exclusive', 'verifying'})):
            raise ControlError('control_update_transfer_gap_unproven')
        paths = recovery._new_action_paths(root, operation_id, old['generation'], action_id=old['review_id'])
        try:
            record = recovery.read_operator_action_record(paths.record_path)
        except (recovery.RecoveryPlanningError, OSError) as exc:
            raise ControlError('control_update_transfer_gap_unproven') from exc
        if abort:
            from .update_abort import require_no_effects
            require_no_effects(root, journal, bound)
        if (record.operation_id != operation_id or record.operation_type != 'update'
                or record.generation != old['generation'] or record.controller_generation != authority.generation
                or record.status not in {'executing', 'failed'} or record.requested_action != old['action']
                or record.evidence_identity != old['canonical_evidence_identity']
                or abort and digest(active.payload()) != old['active_record_sha256']):
            raise ControlError('control_update_transfer_gap_unproven')
        witness = {'source_generation': old['generation'], 'controller_generation': authority.generation,
            'grant_sha256': digest(old), 'action_sha256': digest(recovery._action_record_payload(record)),
            'journal_sha256': digest(journal), 'phase_fence': True}
        if (state.inspect_state(root) != authority or update_controller.read_update_journal(path) != journal
                or read_active_execution(root) != active
                or update_grants.read_grant(operation_id, authority.generation, live=False) != old):
            raise ControlError('control_update_transfer_gap_unproven')
        return authority, journal, bound, witness
    record, ambiguous = recovery._interrupted_planned_action(root, operation_id, authority.generation, 'update')
    # Reconstruct only the historical canonical projection, using its recorded
    # runtime status. Never substitute that status for current OS observation.
    artifacts, committed, reason, actions = recovery._update_plan(root, operation_id,
        old['generation'], virtual, controller_status='inactive', runtime_status=old['runtime_status'])
    prior = recovery._projection_with_evidence(operation_id=operation_id, operation_type='update',
        generation=old['generation'], controller_status='inactive', runtime_status=old['runtime_status'],
        reason=reason, boundary_name='runtimeCommitted', boundary=committed, artifacts=artifacts,
        actions=actions, durable_evidence=recovery._durable_recovery_evidence(root, operation_id,
            state.OperationType.UPDATE, normalized_generation=old['generation']),
        lifecycle_phase=old['source_lifecycle_phase'])
    if (ambiguous or record is None or record.generation != old['generation']
            or old['generation'] != journal['lifecycle_generation']
            or record.evidence_identity != old['canonical_evidence_identity']
            or prior['evidenceIdentity'] != old['canonical_evidence_identity']
            or record.requested_action != old['action'] or digest(active.payload()) != old['active_record_sha256']
            or state.inspect_state(root) != authority or update_controller.read_update_journal(path) != journal
            or read_active_execution(root) != active
            or update_grants.read_grant(operation_id, authority.generation, live=False) != old):
        raise ControlError('control_update_transfer_gap_unproven')
    witness = {'source_generation': old['generation'], 'controller_generation': authority.generation,
        'grant_sha256': digest(old), 'action_sha256': digest(recovery._action_record_payload(record)),
        'journal_sha256': digest(journal)}
    return authority, journal, bound, witness
