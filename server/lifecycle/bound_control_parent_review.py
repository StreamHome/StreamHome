"""Read-only exact control identity projection for original UPDATE/RESTORE owners.

Reviews and finite native control-repair grants retain the original owner's
binding. Ownership transfer, filesystem repair and runtime admission belong to
their separately verified execution adapters.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from . import bound_parent_repair, bound_update_repair, recovery, restore_controller, state
from .control import control_reconstruction, entry
from .control.rpc import ControlError
from .control.stopped_repair import require_stopped
from .control.processes import require_service_quiescent
from .control.tickets import read_stop_epoch
from .layout import layout_for_root
from .parent_execution import parse_parent_execution
from .update_recovery import digest, request_sha256, runtime_link_source
from .update_recovery_observation import read_binding


ACTION='repair_bound_control'


def _update(root: Path, operation_id: str):
    authority,journal,bound,gap=read_binding(root,operation_id)
    if (authority.active_operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or recovery._controller_status(root,operation_id,authority.generation)!='inactive'):
        raise ControlError('control_reconstruction_parent_unavailable')
    selected=bound_update_repair.selected_execution(root,bound)
    active=bound_update_repair.read_active_execution(root)
    previous=active.execution==bound.parent.execution and active.generation<bound.parent.original_generation
    accepted=(bound.accepted is not None and active.execution==bound.accepted
        and active.operation_id==operation_id and bound.parent.original_generation<active.generation<=authority.generation)
    if active.installation_id!=bound.parent.installation_id or not (previous or accepted):
        raise ControlError('control_parent_binding_changed')
    return authority,journal,bound.parent,selected,active,gap,bound.accepted is not None


def _restore(root: Path, operation_id: str):
    observed=state.inspect_state(root)
    authority=state.validate_operation_with_credential(root,operation_id=operation_id,
        generation=observed.generation,expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
    operation=authority.active_operation
    if (authority.schema_version!=2 or operation.operation_type is not state.OperationType.RESTORE
            or operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER):
        raise ControlError('control_reconstruction_parent_unavailable')
    journal=restore_controller.inspect_restore_journal(root,operation_id)
    if journal['schema_version']!=2 or journal['progress']!='recovery_required':
        raise ControlError('control_parent_binding_changed')
    gap=None
    if journal['generation']!=authority.generation:
        from .parent_repair_intents import gap_witness
        gap=gap_witness(root,authority,journal)
    if not bound_parent_repair._inactive_controller(root,operation_id,authority.generation,journal,gap):
        raise ControlError('control_reconstruction_parent_unavailable')
    bound=parse_parent_execution(journal['managed_execution'])
    active=bound_parent_repair.read_active_execution(root)
    if (bound.operation_id!=operation_id or bound.operation_type!='restore'
            or active.installation_id!=bound.installation_id or active.execution!=bound.execution):
        raise ControlError('control_parent_binding_changed')
    return authority,journal,bound,bound.execution,active,gap,journal['accepted']


def plan(root: Path, operation_id: str, artifact: dict, *, control_manifest_sha256: str) -> dict:
    """Bind a signed exact copy to the original controller or selected runtime."""
    from .bound_control_repair import _artifact
    layout=layout_for_root(root)
    if not layout.managed or os.geteuid()==0 or layout.descriptor.descriptor.service_uid!=os.geteuid():
        raise ControlError('control_worker_forbidden')
    from .management_journal import _uuid
    _uuid(operation_id,'bound operation')
    require_stopped();require_service_quiescent(inspections=True,settle_timeout=2)
    operation=state.inspect_state(root).active_operation
    if operation is None or operation.operation_id!=operation_id:
        raise ControlError('control_reconstruction_parent_unavailable')
    project={state.OperationType.UPDATE:_update,state.OperationType.RESTORE:_restore}.get(operation.operation_type)
    if project is None:raise ControlError('control_reconstruction_parent_unavailable')
    source=project(root,operation_id)
    authority,journal,parent,selected,active,gap,accepted=source
    if (parent.installation_id!=layout.descriptor.descriptor.installation_id
            or parent.execution.control_manifest_sha256!=control_manifest_sha256):
        raise ControlError('control_operation_reader_changed')
    epoch=read_stop_epoch();verified=_artifact(artifact)
    selection={'schema_version':1,'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256}
    roles=[name for name,execution in (('controller',parent.execution),('runtime',selected))
        if execution.control_bundle_id==verified.bundle_id and execution.control_manifest_sha256==verified.manifest_sha256
            and execution.control_protocol_version==1]
    if not roles:raise ControlError('control_reconstruction_identity_changed')
    value={'schema_version':1,'operation_type':operation.operation_type.value,'operation_id':operation_id,
        'installation_id':parent.installation_id,'generation':authority.generation,'accepted':accepted,
        'parent_binding_sha256':digest(parent.payload()),'source_request_sha256':digest(parent.payload()),
        'source_journal_sha256':digest(journal),
        'active_record_sha256':digest(active.payload()),'generation_gap':gap,'stop_epoch':epoch,
        'artifact':dict(artifact),'selection':selection,'roles':roles,'read_only':True,
        'permitted_actions':[ACTION],'controller_selection':{'schema_version':1,
            'bundle_id':parent.execution.control_bundle_id,'manifest_sha256':parent.execution.control_manifest_sha256},
        'filesystem_sha256':control_reconstruction.fingerprint(entry.CONTROL/'versions'/verified.bundle_id)}
    if operation.operation_type is state.OperationType.UPDATE:
        value['runtime_link_source_sha256']=digest(runtime_link_source(root))
    require_stopped();require_service_quiescent(inspections=True,settle_timeout=2)
    if (project(root,operation_id)!=source or read_stop_epoch()!=epoch
            or operation.operation_type is state.OperationType.UPDATE
            and digest(runtime_link_source(root))!=value['runtime_link_source_sha256']):
        raise ControlError('control_recovery_review_stale')
    evidence=digest(value)
    return {**value,'evidence_sha256':evidence,'source_evidence_sha256':evidence}


def prepare(root: Path, payload: dict, *, control_manifest_sha256: str) -> dict:
    """Prepare only the original owner's finite, non-starting control permission."""
    from .control import parent_grants,update_grants
    from .management_journal import execution_tuple_sha256
    fields={'operation_id','artifact','expected_evidence_sha256','recovery_review_id','issuer_scope','boot_id','stop_epoch'}
    if not isinstance(payload,dict) or set(payload)!=fields:raise ControlError('control_request_invalid')
    current=plan(root,payload['operation_id'],payload['artifact'],control_manifest_sha256=control_manifest_sha256)
    if (current['evidence_sha256']!=payload['expected_evidence_sha256'] or current['stop_epoch']!=payload['stop_epoch']):
        raise ControlError('control_recovery_review_stale')
    project=_update if current['operation_type']=='update' else _restore
    authority,journal,parent,selected,active,gap,accepted=project(root,payload['operation_id'])
    if (authority.generation!=current['generation'] or digest(journal)!=current['source_journal_sha256']
            or digest(parent.payload())!=current['parent_binding_sha256'] or digest(active.payload())!=current['active_record_sha256']):
        raise ControlError('control_recovery_review_stale')
    value={'schema_version':4,'operation_type':current['operation_type'],'operation_id':payload['operation_id'],
        'installation_id':current['installation_id'],'generation':current['generation'],
        'review_id':payload['recovery_review_id'],'action':ACTION,
        'parent_binding_sha256':current['parent_binding_sha256'],'control_manifest_sha256':control_manifest_sha256,
        'source_evidence_sha256':current['evidence_sha256'],'filesystem_sha256':current['filesystem_sha256'],
        'source_journal_sha256':current['source_journal_sha256'],
        'control_artifact':dict(payload['artifact']),'control_selection':current['selection'],
        'issuer_scope':payload['issuer_scope'],'boot_id':payload['boot_id'],'stop_epoch':payload['stop_epoch'],
        'expires_at_monotonic':time.monotonic()+900}
    if current['operation_type']=='update':
        bound=read_binding(root,payload['operation_id'])[2]
        value.update(original_generation=parent.original_generation,active_record_sha256=current['active_record_sha256'],
            request_sha256=request_sha256(journal),database_checkpoint_sha256=digest(journal['managed_database_checkpoint']),
            previous_execution_sha256=execution_tuple_sha256(parent.execution),
            candidate_execution_sha256=execution_tuple_sha256(bound.candidate) if bound.candidate else None,
            accepted_execution_sha256=execution_tuple_sha256(bound.accepted) if bound.accepted else None,
            permitted_execution_sha256=[execution_tuple_sha256(selected)],runtime_status='offline',
            source_lifecycle_phase='recovery_required',runtime_link_source_sha256=current['runtime_link_source_sha256'],
            source_journal_generation=journal['lifecycle_generation'],repair_execution_sha256=execution_tuple_sha256(selected))
        grants=update_grants
    else:
        value.update(execution_sha256=execution_tuple_sha256(parent.execution),source_journal_generation=journal['generation'])
        grants=parent_grants
    grants.require_live(value)
    if plan(root,payload['operation_id'],payload['artifact'],control_manifest_sha256=control_manifest_sha256)!=current:
        raise ControlError('control_recovery_review_stale')
    return value
