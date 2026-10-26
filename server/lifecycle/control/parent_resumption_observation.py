"""Independent UID proof before a stopped RESTORE parent's one OS start."""
from __future__ import annotations

import os
from pathlib import Path
import re

from .. import recovery,state
from ..managed_restore import bound_journal
from ..management_journal import execution_tuple_sha256
from ..runtime_build import verify_runtime
from ..runtime_links import verify_links
from . import parent_grants,resumption_contract
from .administration import operation_unit
from .processes import controller_peer,require_service_quiescent
from .rpc import ControlError


ROOT=Path('/opt/streamhome')


def observe(payload: dict) -> dict:
    fields={'plan','kind','controller_pid','controller_invocation_id'}
    if (not isinstance(payload,dict) or set(payload) not in (fields,fields|{'startup_hook'})
            or type(payload['controller_pid']) is not int or payload['controller_pid']<=1
            or not isinstance(payload['controller_invocation_id'],str)
            or not re.fullmatch(r'[a-f0-9]{32}',payload['controller_invocation_id'])
            or not int(payload['controller_invocation_id'],16)):
        raise ControlError('control_resumption_review_changed')
    plan=resumption_contract.parse_plan(payload['plan'])
    if (plan['schema_version']!=2 or plan['owner_type']!='restore'
            or plan['action']!='restore_recovery' or payload['kind']!='candidate_start'):
        raise ControlError('control_resumption_mode_unavailable')
    journal,bound=bound_journal(ROOT,plan['operation_id'])
    authority=state.inspect_state(ROOT);operation=authority.active_operation
    actor=operation_unit(plan['operation_id'])
    controller_peer(payload['controller_pid'],plan['operation_id'])
    if (actor['ActiveState']!='active' or int(actor['MainPID'])!=payload['controller_pid']
            or actor['InvocationID']!=payload['controller_invocation_id']):
        raise ControlError('control_controller_changed')
    grant=parent_grants.read_grant(plan['operation_id'],plan['generation'])
    parent_grants.require_binding(grant,bound)
    if (grant['schema_version']!=2 or grant['resumption_plan_id']!=plan['plan_id']
            or grant['review_id']!=plan['review_id'] or grant['action']!=plan['recovery_action']
            or bound.installation_id!=plan['installation_id']
            or bound.execution.control_manifest_sha256!=plan['control_manifest_sha256']
            or parent_grants.binding_sha256(bound)!=plan['parent_binding_sha256']
            or plan['stages'][0]['execution_sha256']!=execution_tuple_sha256(bound.execution)):
        raise ControlError('control_resumption_review_changed')
    if (operation is not None and operation.operation_id==plan['operation_id']
            and operation.operation_type is state.OperationType.RESTORE
            and operation.owner_kind is state.OwnerKind.RESTORE_CONTROLLER
            and authority.generation in {grant['generation'],plan['generation']}
            and journal['generation']==authority.generation
            and operation.phase in {state.LifecyclePhase.RECOVERY_REQUIRED,state.LifecyclePhase.EXCLUSIVE}):
        if (state.inspect_state(ROOT)!=authority
                or bound_journal(ROOT,operation.operation_id)!=(journal,bound)
                or operation_unit(operation.operation_id)!=actor):
            raise ControlError('control_resumption_review_changed')
        return {'pending':True}
    lease=state.read_controller_lease(ROOT)
    if (operation is None or operation.operation_id!=plan['operation_id']
            or operation.operation_type is not state.OperationType.RESTORE
            or operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
            or operation.phase is not state.LifecyclePhase.VERIFYING
            or authority.generation!=plan['generation'] or journal['generation']!=authority.generation
            or journal['progress'] not in {'verifying_candidate','verifying_rollback','accepted','normal_recovery'}
            or lease is None or lease.pid!=payload['controller_pid']
            or lease.operation_id!=operation.operation_id or lease.generation!=authority.generation
            or lease.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
            or not state.controller_lease_is_current(lease)):
        raise ControlError('control_controller_changed')
    controller_peer(lease.pid,operation.operation_id)
    _,record=recovery._find_action_record(ROOT,operation.operation_id,authority.generation)
    if (record.status!='executing' or record.operation_type!='restore'
            or record.generation!=grant['generation'] or record.requested_action!=grant['action']
            or record.evidence_identity!=grant['canonical_evidence_identity']):
        raise ControlError('control_resumption_review_changed')
    hook=payload.get('startup_hook')
    if hook is not None and (not isinstance(hook,dict) or set(hook)!={'pid','start_identity'}):
        raise ControlError('control_unit_hook_forbidden')
    quiet={'inspections':True,'settle_timeout':2,'controller':(operation.operation_id,lease.pid,lease.process_start_identity)}
    if hook is not None:quiet['startup_hook']=(hook['pid'],hook['start_identity'])
    require_service_quiescent(**quiet)
    verify_runtime(ROOT,bound.execution);verify_links(ROOT,bound.execution)
    if (state.inspect_state(ROOT)!=authority or bound_journal(ROOT,operation.operation_id)!=(journal,bound)
            or not state.controller_lease_is_current(lease)
            or operation_unit(operation.operation_id)!=actor):
        raise ControlError('control_resumption_review_changed')
    return {'installation_id':bound.installation_id,'operation_id':operation.operation_id,
        'owner_type':'restore','action':'restore_recovery','generation':authority.generation,
        'execution_sha256':execution_tuple_sha256(bound.execution),'handoff_sha256':None,
        'accepted':journal['accepted'],'candidate_failed':False,'previous_restored':False,
        'parent_binding_sha256':parent_grants.binding_sha256(bound),'recovery_action':grant['action']}
