"""Read-only exact environment-repair planning for an unresolved original parent.

This projection cannot claim ownership, repair artifacts, admit a runtime or
complete database recovery. The reader must explicitly understand parent repair;
normal MANAGEMENT repair declarations are not sufficient.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from . import bound_runtime_repair as artifacts, recovery, restore_controller, state
from .execution_state import read_active_execution
from .layout import layout_for_root
from .management_journal import execution_tuple_sha256
from .parent_execution import parse_parent_execution
from .control.parent_grants import binding_sha256
from .control.resumption_contract import digest
from .control.rpc import ControlError
from .control.stopped_repair import require_stopped
from .control.processes import require_service_quiescent
from .control.tickets import read_stop_epoch


ACTION = 'repair_bound_environment'


def plan(root: Path, operation_id: str, *, control_manifest_sha256: str) -> dict:
    layout = layout_for_root(root)
    if (not layout.managed or os.geteuid() == 0
            or layout.descriptor.descriptor.service_uid != os.geteuid()):
        raise ControlError('control_worker_forbidden')
    require_stopped()
    require_service_quiescent(inspections=True, settle_timeout=2)
    observed = state.inspect_state(root)
    authority = state.validate_operation_with_credential(root, operation_id=operation_id,
        generation=observed.generation,
        expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
    operation = authority.active_operation
    if (authority.schema_version != 2 or operation is None
            or operation.operation_type is not state.OperationType.RESTORE
            or operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER):
        raise ControlError('control_parent_repair_unavailable')
    journal = restore_controller.inspect_restore_journal(root, operation_id)
    if journal['schema_version'] != 2 or journal['progress'] != 'recovery_required':
        raise ControlError('control_parent_binding_changed')
    gap = None
    if journal['generation'] != authority.generation:
        from .parent_repair_intents import gap_witness
        gap = gap_witness(root,authority,journal)
    if not _inactive_controller(root,operation_id,authority.generation,journal,gap):
        raise ControlError('control_parent_repair_unavailable')
    bound = parse_parent_execution(journal['managed_execution'])
    active = read_active_execution(root)
    selected = layout.descriptor.descriptor
    if (bound.operation_id != operation_id or bound.operation_type != 'restore'
            or bound.installation_id != selected.installation_id
            or active.installation_id != bound.installation_id or active.execution != bound.execution
            or bound.execution.control_manifest_sha256 != control_manifest_sha256):
        raise ControlError('control_parent_binding_changed')
    from .control.reader_support import require_parent_bound_repair_reader
    require_parent_bound_repair_reader(bound.execution)
    epoch = read_stop_epoch()
    inventory = artifacts.inspect(root, layout.cache/'runtime-recovery', bound.execution,
        checkpoint=lambda: None)
    value = {'schema_version':1, 'operation_type':'restore', 'operation_id':operation_id,
        'installation_id':bound.installation_id, 'generation':authority.generation,
        'phase':operation.phase.value, 'accepted':journal['accepted'],
        'selected_execution_sha256':execution_tuple_sha256(bound.execution),
        'control_manifest_sha256':control_manifest_sha256,
        'parent_binding_sha256':binding_sha256(bound), 'source_journal_sha256':digest(journal),
        'source_journal_generation':journal['generation'],
        'original_generation':bound.original_generation,
        'bound_bundle_id':bound.execution.runtime_bundle_id,
        'bound_manifest_sha256':bound.execution.runtime_manifest_sha256,
        'artifacts':inventory, 'stop_epoch':epoch, 'generation_gap':gap,
        'permitted_actions':[ACTION], 'read_only':True}
    require_stopped()
    require_service_quiescent(inspections=True, settle_timeout=2)
    if (state.inspect_state(root) != authority
            or restore_controller.inspect_restore_journal(root, operation_id) != journal
            or read_active_execution(root) != active or read_stop_epoch() != epoch
            or not _inactive_controller(root,operation_id,authority.generation,journal,gap)):
        raise ControlError('control_recovery_review_stale')
    if gap is not None:
        if gap_witness(root,authority,journal) != gap:
            raise ControlError('control_recovery_review_stale')
    return {**value, 'evidence_sha256':digest(value)}


def _inactive_controller(root: Path, operation_id: str, generation: int, journal: dict, gap) -> bool:
    status = recovery._controller_status(root,operation_id,generation)
    if status == 'inactive':
        return True
    if status != 'unknown' or gap is None:
        return False
    # The exact authorized state CAS may precede lease publication. Only its
    # retained intent can qualify checking the old matching, actually dead lease.
    lease = state.read_controller_lease(root)
    return (lease is not None and lease.operation_id == operation_id
        and lease.owner_kind is state.OwnerKind.RESTORE_CONTROLLER
        and lease.generation == journal['generation']
        and not state.controller_lease_is_current(lease)
        and recovery._controller_status(root,operation_id,lease.generation) == 'inactive')


def prepare(root: Path, payload: dict, *, control_manifest_sha256: str) -> dict:
    """Construct a finite permission; the protected root bridge alone persists it."""
    from .control import parent_grants
    fields = {'operation_id','expected_evidence_sha256','recovery_review_id',
              'issuer_scope','boot_id','stop_epoch'}
    if not isinstance(payload,dict) or set(payload) != fields:
        raise ControlError('control_parent_recovery_request_invalid')
    current = plan(root,payload['operation_id'],control_manifest_sha256=control_manifest_sha256)
    if (payload['expected_evidence_sha256'] != current['evidence_sha256']
            or payload['stop_epoch'] != current['stop_epoch']):
        raise ControlError('control_recovery_review_stale')
    value = {'schema_version':3, 'review_id':payload['recovery_review_id'],
        'installation_id':current['installation_id'], 'operation_id':current['operation_id'],
        'operation_type':'restore', 'generation':current['generation'], 'action':ACTION,
        'execution_sha256':current['selected_execution_sha256'],
        'parent_binding_sha256':current['parent_binding_sha256'],
        'control_manifest_sha256':control_manifest_sha256,
        'source_evidence_sha256':current['evidence_sha256'],
        'filesystem_sha256':current['artifacts']['filesystem_sha256'],
        'source_journal_sha256':current['source_journal_sha256'],
        'source_journal_generation':current['source_journal_generation'],
        'issuer_scope':payload['issuer_scope'], 'boot_id':payload['boot_id'],
        'stop_epoch':payload['stop_epoch'], 'expires_at_monotonic':time.monotonic()+900}
    parent_grants.require_live(value)
    if plan(root,payload['operation_id'],control_manifest_sha256=control_manifest_sha256) != current:
        raise ControlError('control_recovery_review_stale')
    return value


class BoundParentEnvironmentController:
    """Original RESTORE custody with finite, independently issued repair consent."""
    action=ACTION
    permission_version=3

    def review_plan(self,root,operation_id,control_manifest_sha256):
        return plan(root,operation_id,control_manifest_sha256=control_manifest_sha256)

    def review_fingerprint(self):
        return self.review['artifacts']['filesystem_sha256']

    def validate_source_inventory(self):
        current=artifacts.inspect(self.root,self.layout.cache/'runtime-recovery',self.bound.execution,
            checkpoint=self.checkpoint)
        if current!=self.review['artifacts']:
            raise ControlError('control_recovery_review_stale')

    def __init__(self, root: Path, operation_id: str, *, control_manifest_sha256: str):
        from .control.administration import operation_unit
        from .control.processes import controller_peer
        from .control import parent_grants
        self.root,self.operation_id=root,operation_id
        controller_peer(os.getpid(),operation_id)
        self.actor=operation_unit(operation_id)
        self.invocation=os.environ.get('INVOCATION_ID','')
        if (self.actor['ActiveState']!='active' or int(self.actor['MainPID'])!=os.getpid()
                or not self.invocation or self.actor['InvocationID']!=self.invocation):
            raise ControlError('control_controller_changed')
        self.authority=state.inspect_state(root)
        self.grant=parent_grants.read_grant(operation_id,self.authority.generation+1)
        if (self.grant['schema_version']!=self.permission_version or self.grant['action']!=self.action
                or self.grant['control_manifest_sha256']!=control_manifest_sha256
                or self.grant['generation']!=self.authority.generation):
            raise ControlError('control_parent_recovery_request_changed')
        self.review=self.review_plan(root,operation_id,control_manifest_sha256)
        if (self.review['evidence_sha256']!=self.grant['source_evidence_sha256']
                or self.review_fingerprint()!=self.grant['filesystem_sha256']
                or self.review['source_journal_sha256']!=self.grant['source_journal_sha256']):
            raise ControlError('control_recovery_review_stale')
        self.layout=layout_for_root(root)
        self.source_journal=restore_controller.inspect_restore_journal(root,operation_id)
        self.bound=parse_parent_execution(self.source_journal['managed_execution'])
        parent_grants.require_binding(self.grant,self.bound)
        self.active=read_active_execution(root)
        self.start=state._process_start_identity(os.getpid())
        self.lease=None
        self.intent=None
        self.target_journal=None
        self.checked_at=0.0

    def checkpoint(self, *, force: bool=False) -> None:
        from .control import parent_grants
        from .control.administration import operation_unit
        parent_grants.require_live_identity(self.grant)
        if self.lease is not None:
            self.lease.require_current()
        if force or time.monotonic()>=self.checked_at:
            if (parent_grants.read_grant(self.operation_id,self.grant['generation']+1)!=self.grant
                    or operation_unit(self.operation_id)!=self.actor or read_active_execution(self.root)!=self.active):
                raise ControlError('control_parent_recovery_request_changed')
            expected=self.target_journal or self.source_journal
            if restore_controller.inspect_restore_journal(self.root,self.operation_id)!=expected:
                raise ControlError('control_parent_binding_changed')
            self.checked_at=time.monotonic()+1

    def quiescent(self) -> None:
        self.checkpoint(force=True)
        require_stopped()
        require_service_quiescent(inspections=True,settle_timeout=2,
            controller=(self.operation_id,os.getpid(),self.start))

    def pre_transfer(self) -> None:
        from . import parent_repair_intents
        self.quiescent()
        if state.inspect_state(self.root)!=self.authority:
            raise ControlError('control_recovery_review_stale')
        prior=state.read_controller_lease(self.root)
        if prior is not None and (prior.operation_id!=self.operation_id
                or prior.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
                or prior.generation not in {self.authority.generation,self.source_journal['generation']}
                or state.controller_lease_is_current(prior)):
            raise ControlError('control_controller_changed')
        self.validate_source_inventory()
        self.checkpoint(force=True)
        self.intent=parent_repair_intents.prepare_transfer(self.root,self.grant,self.source_journal)

    def _intent_stage(self, stage: str) -> None:
        from . import parent_repair_intents
        if self.intent is None or parent_repair_intents.read(self.root,self.grant)!=self.intent:
            raise ControlError('control_parent_repair_intent_invalid')
        updated={**self.intent,'stage':stage}
        parent_repair_intents.parse(self.root,self.grant,updated)
        state._atomic_write_json(parent_repair_intents.path(self.root,self.grant),updated)
        self.intent=updated

    def on_transfer(self, generation: int) -> None:
        from . import parent_repair_intents
        if self.intent is None or generation!=self.grant['generation']+1:
            raise ControlError('control_parent_repair_intent_invalid')
        journal=restore_controller.inspect_restore_journal(self.root,self.operation_id)
        if not parent_repair_intents.same_parent_journal(self.intent['normalized_journal'],journal,generation):
            raise ControlError('control_parent_binding_changed')
        self.target_journal=journal
        self.checkpoint(force=True)
        self._intent_stage('transferred')

    def maintenance(self, lease: state.ControllerLeaseHandle, generation: int, journal: dict) -> None:
        self.lease=lease
        if journal!=self.target_journal:
            raise ControlError('control_parent_binding_changed')
        self.quiescent()
        directory=restore_controller.restore_paths(self.root,self.operation_id).operation_dir
        stem='environment-repair-'+str(generation)+'-'+self.grant['review_id']
        receipt=artifacts.reconstruct(self.root,self.layout.cache/'runtime-recovery',self.bound.execution,
            expected=self.review['artifacts'],receipt=directory/(stem+'.json'),
            checkpoint=self.checkpoint,quiescent=self.quiescent)
        self.quiescent()
        value={'schema_version':1,'operation_type':'restore','operation_id':self.operation_id,
            'generation':generation,'grant_sha256':digest(self.grant),
            'execution_sha256':self.grant['execution_sha256'],
            'parent_binding_sha256':self.grant['parent_binding_sha256'],
            'journal_sha256':digest(journal),'accepted':journal['accepted'],
            'receipt_sha256':digest(receipt),'stage':'environment_repaired_recovery_required'}
        state._atomic_write_json(directory/(stem+'-result.json'),value)
        self._intent_stage('repaired')

    def execute(self) -> int:
        self.checkpoint(force=True)
        capability=state.load_controller_capability(self.root,operation_id=self.operation_id,
            generation=self.authority.generation)
        return restore_controller._run_controller(self.root,self.operation_id,self.authority.generation,capability,
            acknowledgement_fd=None,gate_fd=None,operator_action=self.action,
            pre_transfer_check=self.pre_transfer,on_transfer=self.on_transfer,maintenance=self.maintenance)


def result(root: Path, operation_id: str, *, control_manifest_sha256: str) -> dict:
    """Prove exact finished artifact work without resolving the original parent."""
    from . import parent_repair_intents
    from .control import parent_grants
    from .control.provision_worker import _private_record
    from .runtime_build import verify_runtime
    layout=layout_for_root(root)
    if not layout.managed or os.geteuid()==0 or layout.descriptor.descriptor.service_uid!=os.geteuid():
        raise ControlError('control_worker_forbidden')
    authority=state.inspect_state(root);operation=authority.active_operation
    if (operation is None or operation.operation_id!=operation_id
            or operation.operation_type is not state.OperationType.RESTORE
            or operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
            or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED):
        raise ControlError('control_authority_changed')
    grant=parent_grants.read_grant(operation_id,authority.generation,live=False)
    if (grant['schema_version']!=3 or grant['control_manifest_sha256']!=control_manifest_sha256
            or layout.descriptor.descriptor.installation_id!=grant['installation_id']):
        raise ControlError('control_parent_recovery_request_changed')
    intent=parent_repair_intents.read(root,grant)
    if intent['stage']!='repaired':
        raise ControlError('control_parent_repair_incomplete')
    journal=restore_controller.inspect_restore_journal(root,operation_id)
    if (journal['generation']!=authority.generation
            or not parent_repair_intents.same_parent_journal(intent['normalized_journal'],journal,authority.generation)):
        raise ControlError('control_parent_binding_changed')
    bound=parse_parent_execution(journal['managed_execution']);parent_grants.require_binding(grant,bound)
    stem='environment-repair-'+str(authority.generation)+'-'+grant['review_id']
    directory=restore_controller.restore_paths(root,operation_id).operation_dir
    receipt=_private_record(directory/(stem+'.json'))
    recorded=_private_record(directory/(stem+'-result.json'))
    expected={'schema_version':1,'operation_type':'restore','operation_id':operation_id,
        'generation':authority.generation,'grant_sha256':digest(grant),
        'execution_sha256':grant['execution_sha256'],'parent_binding_sha256':grant['parent_binding_sha256'],
        'journal_sha256':digest(journal),'accepted':journal['accepted'],
        'receipt_sha256':digest(receipt),'stage':'environment_repaired_recovery_required'}
    active=read_active_execution(root)
    if (recorded!=expected or receipt.get('status')!='complete'
            or receipt.get('manifest_sha256')!=bound.execution.runtime_manifest_sha256
            or receipt.get('filesystem_sha256')!=grant['filesystem_sha256']
            or active.installation_id!=bound.installation_id or active.execution!=bound.execution):
        raise ControlError('control_parent_repair_result_invalid')
    require_stopped();require_service_quiescent(inspections=True,settle_timeout=2)
    verify_runtime(root,bound.execution)
    if (state.inspect_state(root)!=authority or restore_controller.inspect_restore_journal(root,operation_id)!=journal
            or read_active_execution(root)!=active or parent_repair_intents.read(root,grant)!=intent):
        raise ControlError('control_recovery_review_stale')
    return {'completed':True,'generation':authority.generation,'stage':expected['stage']}
