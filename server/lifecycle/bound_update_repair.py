"""Exact cached environment reconstruction under the original UPDATE controller."""
from __future__ import annotations

import os
from pathlib import Path
import time

from . import bound_runtime_repair as artifacts, recovery, state, update_controller, update_execution
from . import update_repair_intents as intents
from .execution_state import read_active_execution
from .layout import layout_for_root
from .management_journal import execution_tuple_sha256
from .update_recovery import digest, request_sha256, runtime_link_source
from .update_recovery_observation import read_binding
from .control import update_grants
from .control.rpc import ControlError
from .control.stopped_repair import require_stopped
from .control.processes import require_service_quiescent
from .control.tickets import read_stop_epoch


ACTION = 'repair_bound_environment'


def selected_execution(root: Path, bound):
    installed = recovery._installed_commit(root)
    if bound.accepted is not None:
        selected = bound.accepted
    elif bound.verification is not None and bound.verification.application_commit == installed:
        selected = bound.verification
    elif bound.parent.execution.application_commit == installed:
        selected = bound.parent.execution
    elif bound.candidate is not None and bound.candidate.application_commit == installed:
        selected = bound.candidate
    else:
        raise ControlError('control_update_repair_source_unavailable')
    if selected.application_commit != installed:
        raise ControlError('control_update_repair_source_unavailable')
    return selected


def plan(root: Path, operation_id: str, *, control_manifest_sha256: str) -> dict:
    layout = layout_for_root(root)
    if not layout.managed or os.geteuid() == 0 or layout.descriptor.descriptor.service_uid != os.geteuid():
        raise ControlError('control_worker_forbidden')
    require_stopped();require_service_quiescent(inspections=True, settle_timeout=2)
    authority, journal, bound, gap = read_binding(root, operation_id)
    if (authority.active_operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or recovery._controller_status(root, operation_id, authority.generation) != 'inactive'
            or bound.parent.installation_id != layout.descriptor.descriptor.installation_id
            or bound.parent.execution.control_manifest_sha256 != control_manifest_sha256):
        raise ControlError('control_update_repair_unavailable')
    from .control.reader_support import require_managed_update_reader
    require_managed_update_reader(bound.parent.execution)
    selected = selected_execution(root, bound);active = read_active_execution(root)
    previous = active.execution == bound.parent.execution and active.generation < bound.parent.original_generation
    accepted = (bound.accepted is not None and active.execution == bound.accepted
        and active.operation_id == operation_id and bound.parent.original_generation < active.generation <= authority.generation)
    if active.installation_id != bound.parent.installation_id or not (previous or accepted):
        raise ControlError('control_parent_binding_changed')
    epoch = read_stop_epoch()
    inventory = artifacts.inspect(root, layout.cache/'runtime-recovery', selected, checkpoint=lambda: None)
    value = {'schema_version': 1, 'operation_type': 'update', 'operation_id': operation_id,
        'installation_id': bound.parent.installation_id, 'generation': authority.generation,
        'phase': 'recovery_required', 'accepted': bound.accepted is not None,
        'selected_execution_sha256': execution_tuple_sha256(selected),
        'previous_execution_sha256': execution_tuple_sha256(bound.parent.execution),
        'candidate_execution_sha256': execution_tuple_sha256(bound.candidate) if bound.candidate else None,
        'accepted_execution_sha256': execution_tuple_sha256(bound.accepted) if bound.accepted else None,
        'parent_binding_sha256': digest(bound.parent.payload()), 'active_record_sha256': digest(active.payload()),
        'request_sha256': request_sha256(journal), 'database_checkpoint_sha256': digest(journal['managed_database_checkpoint']),
        'runtime_link_source_sha256': digest(runtime_link_source(root)),
        'source_journal_sha256': digest(journal), 'source_journal_generation': journal['lifecycle_generation'],
        'control_manifest_sha256': control_manifest_sha256, 'original_generation': bound.parent.original_generation,
        'bound_bundle_id': selected.runtime_bundle_id, 'bound_manifest_sha256': selected.runtime_manifest_sha256,
        'artifacts': inventory, 'stop_epoch': epoch, 'generation_gap': gap,
        'permitted_actions': [ACTION], 'read_only': True}
    require_stopped();require_service_quiescent(inspections=True, settle_timeout=2)
    if (read_binding(root, operation_id) != (authority, journal, bound, gap)
            or selected_execution(root, bound) != selected or read_active_execution(root) != active
            or read_stop_epoch() != epoch or digest(runtime_link_source(root)) != value['runtime_link_source_sha256']
            or recovery._controller_status(root, operation_id, authority.generation) != 'inactive'):
        raise ControlError('control_recovery_review_stale')
    return {**value, 'evidence_sha256': digest(value)}


def prepare(root: Path, payload: dict, *, control_manifest_sha256: str) -> dict:
    fields = {'operation_id', 'expected_evidence_sha256', 'recovery_review_id', 'issuer_scope', 'boot_id', 'stop_epoch'}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ControlError('control_update_recovery_request_invalid')
    current = plan(root, payload['operation_id'], control_manifest_sha256=control_manifest_sha256)
    if payload['expected_evidence_sha256'] != current['evidence_sha256'] or payload['stop_epoch'] != current['stop_epoch']:
        raise ControlError('control_recovery_review_stale')
    value = {name: current[name] for name in ('operation_type', 'operation_id', 'installation_id', 'generation',
        'previous_execution_sha256', 'candidate_execution_sha256', 'accepted_execution_sha256',
        'parent_binding_sha256', 'active_record_sha256', 'request_sha256', 'database_checkpoint_sha256',
        'runtime_link_source_sha256', 'source_journal_sha256', 'source_journal_generation',
        'control_manifest_sha256', 'original_generation')}
    value.update(schema_version=3, review_id=payload['recovery_review_id'], action=ACTION,
        runtime_status='offline', source_lifecycle_phase='recovery_required',
        repair_execution_sha256=current['selected_execution_sha256'],
        permitted_execution_sha256=[current['selected_execution_sha256']],
        filesystem_sha256=current['artifacts']['filesystem_sha256'], issuer_scope=payload['issuer_scope'],
        boot_id=payload['boot_id'], stop_epoch=payload['stop_epoch'], expires_at_monotonic=time.monotonic()+900,
        source_evidence_sha256=current['evidence_sha256'])
    update_grants.require_live(value)
    if plan(root, payload['operation_id'], control_manifest_sha256=control_manifest_sha256) != current:
        raise ControlError('control_recovery_review_stale')
    return value


def require_takeover_permission(root: Path, operation_id: str, generation: int) -> dict:
    grant = update_grants.read_grant(operation_id, generation+1)
    authority, journal, bound, _ = read_binding(root, operation_id)
    if (grant['schema_version'] != 3 or grant['generation'] != generation or authority.generation != generation
            or authority.active_operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or digest(journal) != grant['source_journal_sha256']
            or digest(read_active_execution(root).payload()) != grant['active_record_sha256']
            or execution_tuple_sha256(selected_execution(root, bound)) != grant['repair_execution_sha256']):
        raise ControlError('control_update_recovery_request_changed')
    update_grants.require_binding(grant, bound, read_active_execution(root), journal)
    return grant


class BoundUpdateEnvironmentController:
    action=ACTION
    permission_version=3

    def permission(self,root,operation_id,generation):
        return require_takeover_permission(root,operation_id,generation)

    def review_plan(self,root,operation_id,control_manifest_sha256):
        return plan(root,operation_id,control_manifest_sha256=control_manifest_sha256)

    def review_fingerprint(self):
        return self.review['artifacts']['filesystem_sha256']

    def validate_source_inventory(self):
        if artifacts.inspect(self.root,self.layout.cache/'runtime-recovery',self.selected,
                checkpoint=self.checkpoint)!=self.review['artifacts']:
            raise ControlError('control_recovery_review_stale')

    def __init__(self, root: Path, operation_id: str, *, control_manifest_sha256: str):
        from .managed_update import require_operation_entry
        from .control.administration import operation_unit
        require_operation_entry(operation_id)
        self.root = root;self.operation_id = operation_id;self.layout = layout_for_root(root)
        self.authority = state.inspect_state(root)
        self.grant = self.permission(root, operation_id, self.authority.generation)
        if (self.grant['schema_version']!=self.permission_version or self.grant['action']!=self.action
                or self.grant['control_manifest_sha256'] != control_manifest_sha256):
            raise ControlError('control_parent_binding_changed')
        self.review = self.review_plan(root, operation_id, control_manifest_sha256)
        if (self.review['evidence_sha256'] != self.grant['source_evidence_sha256']
                or self.review_fingerprint() != self.grant['filesystem_sha256']):
            raise ControlError('control_recovery_review_stale')
        _, self.source_journal, self.bound, _ = read_binding(root, operation_id)
        self.selected = selected_execution(root, self.bound);self.active = read_active_execution(root)
        self.actor = operation_unit(operation_id);self.start = state._process_start_identity(os.getpid())
        self.lease = None;self.intent = None;self.target_journal = None;self.checked_at = 0.0

    def checkpoint(self, *, force=False):
        from .control.administration import operation_unit
        update_grants.require_live_identity(self.grant)
        if self.lease is not None:
            self.lease.require_current()
        if force or time.monotonic() >= self.checked_at:
            path, _, _ = update_controller._update_paths(self.root, self.operation_id, self.grant['generation'])
            if (update_grants.read_grant(self.operation_id, self.grant['generation']+1) != self.grant
                    or operation_unit(self.operation_id) != self.actor or read_active_execution(self.root) != self.active
                    or update_controller.read_update_journal(path) != (self.target_journal or self.source_journal)
                    or selected_execution(self.root, self.bound) != self.selected):
                raise ControlError('control_update_recovery_request_changed')
            self.checked_at = time.monotonic()+1

    def quiescent(self):
        self.checkpoint(force=True);require_stopped()
        require_service_quiescent(inspections=True, settle_timeout=2,
            controller=(self.operation_id, os.getpid(), self.start))

    def pre_transfer(self):
        self.quiescent()
        if state.inspect_state(self.root) != self.authority:
            raise ControlError('control_recovery_review_stale')
        old = state.read_controller_lease(self.root)
        if old is not None and (old.operation_id != self.operation_id or old.owner_kind is not state.OwnerKind.UPDATER
                or old.generation not in {self.authority.generation, self.source_journal['lifecycle_generation']}
                or state.controller_lease_is_current(old)):
            raise ControlError('control_controller_changed')
        self.validate_source_inventory()
        self.intent = intents.prepare_transfer(self.root, self.grant, self.source_journal)

    def intent_stage(self, stage):
        if self.intent is None or intents.read(self.root, self.grant) != self.intent:
            raise ControlError('control_update_repair_intent_invalid')
        self.intent = {**self.intent, 'stage': stage}
        state._atomic_write_json(intents.path(self.root, self.grant), intents.parse(self.root, self.grant, self.intent))

    def on_transfer(self, generation):
        path, _, _ = update_controller._update_paths(self.root, self.operation_id, generation)
        journal = update_controller.read_update_journal(path)
        if (self.intent is None or generation != self.grant['generation']+1
                or not intents.same_journal(self.intent['normalized_journal'], journal, generation)):
            raise ControlError('control_update_repair_intent_invalid')
        self.target_journal = journal;self.checkpoint(force=True);self.intent_stage('transferred')

    def maintenance(self, lease, generation, journal):
        self.lease = lease
        if journal != self.target_journal:
            raise ControlError('control_parent_binding_changed')
        self.quiescent()
        directory = intents.directory(self.root, self.operation_id)
        stem = 'environment-'+str(generation)+'-'+self.grant['review_id']
        receipt = artifacts.reconstruct(self.root, self.layout.cache/'runtime-recovery', self.selected,
            expected=self.review['artifacts'], receipt=directory/(stem+'.json'),
            checkpoint=self.checkpoint, quiescent=self.quiescent)
        self.quiescent()
        value = {'schema_version': 1, 'operation_id': self.operation_id, 'generation': generation,
            'grant_sha256': digest(self.grant), 'execution_sha256': execution_tuple_sha256(self.selected),
            'journal_sha256': digest(journal), 'accepted': self.bound.accepted is not None,
            'receipt_sha256': digest(receipt), 'stage': 'environment_repaired_recovery_required'}
        state._atomic_write_json(directory/(stem+'-result.json'), value);self.intent_stage('repaired')

    def execute(self):
        self.checkpoint(force=True)
        journal = self.source_journal
        return update_controller._run_controller(self.root, operation_id=self.operation_id,
            generation=self.authority.generation, target_commit=journal['target_commit'],
            previous_commit=journal['previous_commit'], automatic=journal['automatic'],
            install_mode=journal['install_mode'], update_branch=journal['update_branch'],
            acknowledgement_fd=None, recovery=True, operator_action=self.action, managed_recovery=True,
            pre_transfer_check=self.pre_transfer, on_transfer=self.on_transfer, maintenance=self.maintenance)


def result(root: Path, operation_id: str, *, control_manifest_sha256: str) -> dict:
    from .control.provision_worker import _private_record
    from .runtime_build import verify_runtime
    layout = layout_for_root(root)
    if not layout.managed or os.geteuid() == 0 or layout.descriptor.descriptor.service_uid != os.geteuid():
        raise ControlError('control_worker_forbidden')
    authority, journal, bound, witness = read_binding(root, operation_id)
    if authority.active_operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED or witness is not None:
        raise ControlError('control_authority_changed')
    grant = update_grants.read_grant(operation_id, authority.generation, live=False)
    if grant['schema_version'] != 3 or grant['control_manifest_sha256'] != control_manifest_sha256:
        raise ControlError('control_update_recovery_request_changed')
    intent = intents.read(root, grant)
    if intent['stage'] != 'repaired' or not intents.same_journal(intent['normalized_journal'], journal, authority.generation):
        raise ControlError('control_update_repair_incomplete')
    selected = selected_execution(root, bound);active = read_active_execution(root)
    stem = 'environment-'+str(authority.generation)+'-'+grant['review_id'];directory = intents.directory(root, operation_id)
    receipt = _private_record(directory/(stem+'.json'));record = _private_record(directory/(stem+'-result.json'))
    expected = {'schema_version': 1, 'operation_id': operation_id, 'generation': authority.generation,
        'grant_sha256': digest(grant), 'execution_sha256': execution_tuple_sha256(selected),
        'journal_sha256': digest(journal), 'accepted': bound.accepted is not None,
        'receipt_sha256': digest(receipt), 'stage': 'environment_repaired_recovery_required'}
    if (record != expected or receipt.get('status') != 'complete'
            or receipt.get('manifest_sha256') != selected.runtime_manifest_sha256
            or receipt.get('filesystem_sha256') != grant['filesystem_sha256']
            or digest(active.payload()) != grant['active_record_sha256']):
        raise ControlError('control_update_repair_result_invalid')
    require_stopped();require_service_quiescent(inspections=True, settle_timeout=2)
    verify_runtime(root, selected)
    if (read_binding(root, operation_id) != (authority, journal, bound, witness)
            or read_active_execution(root) != active or intents.read(root, grant) != intent):
        raise ControlError('control_recovery_review_stale')
    return {'completed': True, 'generation': authority.generation, 'stage': expected['stage']}
