"""Exact control effects under the existing UPDATE/RESTORE maintenance owners."""
from __future__ import annotations

import os
from pathlib import Path
import time

from . import bound_control_parent_review as review, bound_control_repair
from . import bound_parent_repair, bound_update_repair, parent_repair_intents, update_repair_intents
from . import restore_controller, state, update_controller
from .control import entry, parent_grants, update_grants
from .control.processes import controller_peer, require_service_quiescent
from .control.rpc import ControlError
from .control.stopped_repair import require_stopped
from .control.tickets import read_protected_json
from .layout import layout_for_root
from .update_recovery import digest


ACTION='repair_bound_control'


def require_takeover_permission(root: Path,operation_id: str,generation: int,kind: str):
    grants=update_grants if kind=='update' else parent_grants
    grant=grants.read_grant(operation_id,generation+1)
    if grant['schema_version']!=4 or grant['generation']!=generation or grant['action']!=ACTION:
        raise ControlError('control_reconstruction_review_changed')
    current=review.plan(root,operation_id,grant['control_artifact'],
        control_manifest_sha256=grant['control_manifest_sha256'])
    if current['operation_type']!=kind or current['evidence_sha256']!=grant['source_evidence_sha256']:
        raise ControlError('control_recovery_review_stale')
    return grant


def _intent(grant):
    return {'schema_version':1,'operation_id':grant['operation_id'],'generation':grant['generation']+1,
        'action':ACTION,'grant_sha256':digest(grant),'selection':grant['control_selection'],
        'artifact':grant['control_artifact'],'filesystem_sha256':grant['filesystem_sha256']}


def _directory(root,grant):
    if grant['operation_type']=='update':return update_repair_intents.directory(root,grant['operation_id'])
    return restore_controller.restore_paths(root,grant['operation_id']).operation_dir


def _persist_once(path,value):
    bound_control_repair._persist_once(path,value)


class _ControlMaintenance:
    action=ACTION
    permission_version=4

    def review_plan(self,root,operation_id,control_manifest_sha256):
        source=review.plan(root,operation_id,self.grant['control_artifact'],
            control_manifest_sha256=control_manifest_sha256)
        selected=source['controller_selection']
        physical=entry.CONTROL/'rescue-versions'/selected['bundle_id']/'lib/lifecycle/bound_control_parent_repair.py'
        if Path(__file__)!=physical:
            raise ControlError('control_reconstruction_independent_reader_required')
        entry.verify_bundle(selected,rescue=True)
        return source

    def review_fingerprint(self):return self.review['filesystem_sha256']

    def validate_source_inventory(self):
        # The existing owner holds controller.lock here. Re-running the external
        # inactive-owner planner would mistake our transfer exclusion for a live
        # previous owner. Its pre_transfer_check already rechecks canonical
        # authority/journal/active execution and the actually dead prior lease.
        verified=bound_control_repair._artifact(self.grant['control_artifact'])
        selection={'schema_version':1,'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256}
        if (selection!=self.review['selection'] or digest(self.source_journal)!=self.grant['source_journal_sha256']
                or bound_control_repair.control_reconstruction.fingerprint(entry.CONTROL/'versions'/verified.bundle_id)
                    !=self.review['filesystem_sha256']):
            raise ControlError('control_recovery_review_stale')
        self.checkpoint(force=True)

    def maintenance(self,lease,generation,journal):
        self.lease=lease
        if generation!=self.grant['generation']+1 or journal!=self.target_journal:
            raise ControlError('control_reconstruction_review_changed')
        self.quiescent()
        intent=_intent(self.grant);directory=_directory(self.root,self.grant)
        _persist_once(directory/f'control-repair-intent-{generation}.json',intent)
        path=entry.OPERATIONS/self.operation_id/f'control-repair-effect-{generation}.json'
        deadline=time.monotonic()+90
        while True:
            self.quiescent()
            try:effect=read_protected_json(path)
            except ControlError as exc:
                if exc.code!='control_ticket_unavailable':raise
            else:
                _effect(effect,intent)
                if effect['effect']['stage']=='complete':break
            if time.monotonic()>=deadline:raise ControlError('control_reconstruction_bridge_timeout')
            time.sleep(.1)
        self.quiescent();entry.verify_bundle(self.grant['control_selection'])
        _persist_once(directory/f'control-repair-{generation}.json',
            {**intent,'effect_sha256':digest(effect),'status':'complete'})
        if self.grant['operation_type']=='update':self.intent_stage('repaired')
        else:self._intent_stage('repaired')


class BoundUpdateControlController(_ControlMaintenance,bound_update_repair.BoundUpdateEnvironmentController):
    def permission(self,root,operation_id,generation):
        return require_takeover_permission(root,operation_id,generation,'update')


class BoundRestoreControlController(_ControlMaintenance,bound_parent_repair.BoundParentEnvironmentController):
    pass


def _effect(effect,intent):
    if (not isinstance(effect,dict) or set(effect)!={'schema_version','intent_sha256','effect'}
            or type(effect['schema_version']) is not int or effect['schema_version']!=1
            or effect['intent_sha256']!=digest(intent) or not isinstance(effect['effect'],dict)):
        raise ControlError('control_reconstruction_review_changed')
    details=effect['effect']
    if (details.get('selection')!=intent['selection'] or details.get('artifact_sha256')!=intent['artifact']['artifact_sha256']
            or details.get('filesystem_sha256')!=intent['filesystem_sha256']
            or not isinstance(details.get('stage'),str) or details['stage'] not in {'rename_planned','reconstructing','complete'}):
        raise ControlError('control_reconstruction_review_changed')


def _current(root,operation_id,control_manifest_sha256):
    layout=layout_for_root(root)
    if not layout.managed or os.geteuid()==0 or layout.descriptor.descriptor.service_uid!=os.geteuid():
        raise ControlError('control_worker_forbidden')
    authority=state.inspect_state(root);operation=authority.active_operation
    if (operation is None or operation.operation_id!=operation_id
            or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or operation.operation_type not in {state.OperationType.UPDATE,state.OperationType.RESTORE}):
        raise ControlError('control_reconstruction_parent_unavailable')
    kind=operation.operation_type.value
    grants=update_grants if kind=='update' else parent_grants
    grant=grants.read_grant(operation_id,authority.generation)
    if (grant['schema_version']!=4 or grant['control_manifest_sha256']!=control_manifest_sha256
            or grant['installation_id']!=layout.descriptor.descriptor.installation_id):
        raise ControlError('control_operation_reader_changed')
    if kind=='update':
        path,_,_=update_controller._update_paths(root,operation_id,authority.generation)
        journal=update_controller.read_update_journal(path)
        transfer=update_repair_intents.read(root,grant)
        same=update_repair_intents.same_journal
    else:
        journal=restore_controller.inspect_restore_journal(root,operation_id)
        transfer=parent_repair_intents.read(root,grant)
        same=parent_repair_intents.same_parent_journal
    if (transfer['stage'] not in {'transferred','repaired'}
            or not same(transfer['normalized_journal'],journal,authority.generation)):
        raise ControlError('control_reconstruction_review_changed')
    bound_control_repair._artifact(grant['control_artifact'])
    intent=_intent(grant);directory=_directory(root,grant)
    from .control.provision_worker import _private_record
    if _private_record(directory/f'control-repair-intent-{authority.generation}.json')!=intent:
        raise ControlError('control_reconstruction_review_changed')
    state.validate_operation_with_credential(root,operation_id=operation_id,generation=authority.generation,
        expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
    return authority,journal,grant,intent,directory,transfer


def observe(root,payload,*,control_manifest_sha256):
    if (not isinstance(payload,dict) or set(payload)!={'operation_id','controller_pid','controller_invocation_id'}
            or type(payload['controller_pid']) is not int or payload['controller_pid']<=1):
        raise ControlError('control_request_invalid')
    authority,journal,grant,intent,_,_=_current(root,payload['operation_id'],control_manifest_sha256)
    lease=state.read_controller_lease(root)
    if (lease is None or not state.controller_lease_is_current(lease)
            or lease.pid!=payload['controller_pid'] or lease.operation_id!=payload['operation_id']
            or lease.generation!=authority.generation):raise ControlError('control_controller_changed')
    controller_peer(lease.pid,payload['operation_id'])
    from .control.administration import operation_unit
    actor=operation_unit(payload['operation_id'])
    if (actor['ActiveState']!='active' or int(actor['MainPID'])!=lease.pid
            or actor['InvocationID']!=payload['controller_invocation_id']):raise ControlError('control_controller_changed')
    require_stopped();require_service_quiescent(inspections=True,settle_timeout=2,
        controller=(lease.operation_id,lease.pid,lease.process_start_identity))
    if (_current(root,payload['operation_id'],control_manifest_sha256)[:4]!=(authority,journal,grant,intent)
            or not state.controller_lease_is_current(lease) or state.read_controller_lease(root)!=lease
            or operation_unit(payload['operation_id'])!=actor):
        raise ControlError('control_controller_changed')
    return {'operation_id':payload['operation_id'],'generation':authority.generation,'intent':intent,
        'intent_sha256':digest(intent),'controller_pid':lease.pid,'controller_invocation_id':actor['InvocationID'],
        'stage':'control_repair_requested'}


def result(root,operation_id,*,control_manifest_sha256):
    authority,journal,grant,intent,directory,transfer=_current(root,operation_id,control_manifest_sha256)
    require_stopped();require_service_quiescent(inspections=True,settle_timeout=2)
    if transfer['stage']!='repaired':raise ControlError('control_reconstruction_review_changed')
    effect=read_protected_json(entry.OPERATIONS/operation_id/f'control-repair-effect-{authority.generation}.json')
    _effect(effect,intent)
    from .control.provision_worker import _private_record
    if (effect['effect']['stage']!='complete' or _private_record(directory/f'control-repair-{authority.generation}.json')
            !={**intent,'effect_sha256':digest(effect),'status':'complete'}):
        raise ControlError('control_reconstruction_review_changed')
    entry.verify_bundle(grant['control_selection'])
    if _current(root,operation_id,control_manifest_sha256)!=(authority,journal,grant,intent,directory,transfer):
        raise ControlError('control_reconstruction_review_changed')
    return {'completed':True,'stage':'control_repaired_recovery_required','generation':authority.generation,
        'grant_sha256':digest(grant),'selection':grant['control_selection']}
