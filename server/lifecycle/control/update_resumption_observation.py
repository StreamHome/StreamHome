"""UID proof for one stopped UPDATE start, subordinate to the original engine."""
from __future__ import annotations

from pathlib import Path
import re

from .. import recovery, state
from ..managed_update import bound_journal, require_owner_peer
from ..management_journal import execution_tuple_sha256
from ..runtime_build import verify_runtime
from ..runtime_links import verify_links
from ..update_checkpoint import require_launch_database
from ..update_recovery import digest
from . import resumption_contract, update_grants
from .administration import operation_unit
from .processes import require_service_quiescent
from .rpc import ControlError


ROOT = Path('/opt/streamhome')


def observe(payload: dict) -> dict:
    fields = {'plan', 'kind', 'controller_pid', 'controller_invocation_id'}
    if (not isinstance(payload, dict) or set(payload) not in (fields, fields|{'startup_hook'})
            or type(payload['controller_pid']) is not int or payload['controller_pid'] <= 1
            or not isinstance(payload['controller_invocation_id'], str)
            or not re.fullmatch(r'[a-f0-9]{32}', payload['controller_invocation_id'])
            or not int(payload['controller_invocation_id'], 16)):
        raise ControlError('control_resumption_review_changed')
    plan = resumption_contract.parse_plan(payload['plan'])
    if (plan['schema_version'] != 2 or plan['owner_type'] != 'update'
            or plan['action'] != 'update_recovery' or payload['kind'] != 'candidate_start'):
        raise ControlError('control_resumption_mode_unavailable')
    authority, journal, bound = bound_journal(ROOT, plan['operation_id'])
    grant = update_grants.read_grant(plan['operation_id'], plan['generation'])
    record = update_grants.require_resumption_plan(grant)
    actor = operation_unit(plan['operation_id'])
    selected = bound.accepted or bound.parent.execution
    if (grant['schema_version'] != 2 or record['plan'] != plan
            or actor['ActiveState'] != 'active' or int(actor['MainPID']) != payload['controller_pid']
            or actor['InvocationID'] != payload['controller_invocation_id']
            or bound.parent.installation_id != plan['installation_id']
            or bound.parent.execution.control_manifest_sha256 != plan['control_manifest_sha256']
            or digest(bound.parent.payload()) != plan['parent_binding_sha256']
            or plan['stages'][0]['execution_sha256'] != execution_tuple_sha256(selected)):
        raise ControlError('control_resumption_review_changed')
    operation = authority.active_operation
    if (operation is None or operation.operation_id != plan['operation_id']
            or operation.operation_type is not state.OperationType.UPDATE
            or operation.owner_kind is not state.OwnerKind.UPDATER
            or authority.generation not in {grant['generation'], plan['generation']}):
        raise ControlError('control_controller_changed')
    from ..execution_state import read_active_execution
    update_grants.require_binding(grant, bound, read_active_execution(ROOT), journal)
    if operation.phase in {state.LifecyclePhase.RECOVERY_REQUIRED, state.LifecyclePhase.EXCLUSIVE}:
        if (bound_journal(ROOT, plan['operation_id']) != (authority, journal, bound)
                or operation_unit(plan['operation_id']) != actor):
            raise ControlError('control_resumption_review_changed')
        return {'pending': True}
    lease = state.read_controller_lease(ROOT)
    if (operation.phase is not state.LifecyclePhase.VERIFYING or authority.generation != plan['generation']
            or bound.verification != selected or journal['phase'] not in {'starting', 'recovering'}
            or lease is None or lease.pid != payload['controller_pid']
            or require_owner_peer(ROOT, lease, payload['controller_pid']) != (authority, journal, bound)):
        raise ControlError('control_controller_changed')
    paths = recovery._new_action_paths(ROOT, plan['operation_id'], grant['generation'], action_id=grant['review_id'])
    action = recovery.read_operator_action_record(paths.record_path)
    if (action.status != 'executing' or action.operation_type != 'update'
            or action.operation_id != plan['operation_id'] or action.controller_generation != authority.generation
            or action.generation != grant['generation'] or action.requested_action != grant['action']
            or action.evidence_identity != grant['canonical_evidence_identity']):
        raise ControlError('control_resumption_review_changed')
    hook = payload.get('startup_hook')
    if hook is not None and (not isinstance(hook, dict) or set(hook) != {'pid', 'start_identity'}):
        raise ControlError('control_unit_hook_forbidden')
    quiet = {'inspections': True, 'settle_timeout': 2,
        'controller': (plan['operation_id'], lease.pid, lease.process_start_identity)}
    from ..managed_update import quiescent_delegate
    quiet['update_delegate'] = quiescent_delegate(ROOT, lease)
    if hook is not None:
        quiet['startup_hook'] = (hook['pid'], hook['start_identity'])
    require_service_quiescent(**quiet)
    verify_runtime(ROOT, selected);verify_links(ROOT, selected);require_launch_database(ROOT, bound)
    if (bound_journal(ROOT, plan['operation_id']) != (authority, journal, bound)
            or require_owner_peer(ROOT, lease, payload['controller_pid']) != (authority, journal, bound)
            or operation_unit(plan['operation_id']) != actor):
        raise ControlError('control_resumption_review_changed')
    return {'installation_id': plan['installation_id'], 'operation_id': plan['operation_id'],
        'owner_type': 'update', 'action': 'update_recovery', 'generation': authority.generation,
        'execution_sha256': execution_tuple_sha256(selected), 'handoff_sha256': None,
        'accepted': bound.accepted is not None, 'candidate_failed': False, 'previous_restored': False,
        'parent_binding_sha256': plan['parent_binding_sha256'], 'recovery_action': grant['action']}
