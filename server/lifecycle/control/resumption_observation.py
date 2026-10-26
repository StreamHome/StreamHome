"""Independent UID-only canonical verification before one OS resumption stage."""
from __future__ import annotations

import os
from pathlib import Path

from .. import management_journal as journal_contract, management_store, management_recovery_store, state
from ..layout import layout_for_root
from ..runtime_build import verify_runtime
from ..runtime_links import verify_links
from . import resumption_contract
from .processes import controller_peer, require_service_quiescent
from .recovery_requests import effective_request, read_grant
from .rpc import ControlError


ROOT = Path('/opt/streamhome')


def observe(payload: dict) -> dict:
    fields = {'plan', 'kind', 'controller_pid', 'controller_invocation_id'}
    if (not isinstance(payload, dict) or set(payload) not in (fields, fields | {'startup_hook'})
            or type(payload['controller_pid']) is not int or payload['controller_pid'] <= 1):
        raise ControlError('control_resumption_review_changed')
    plan = resumption_contract.parse_plan(payload['plan'])
    layout = layout_for_root(ROOT)
    selected = layout.descriptor.descriptor
    if not layout.managed or os.geteuid() == 0 or selected.service_uid != os.geteuid() or selected.installation_id != plan['installation_id']:
        raise ControlError('control_worker_forbidden')
    # Other parents and fallback restoration require their own exact handoff
    # adapter. They cannot borrow this MANAGEMENT candidate observer.
    if plan['owner_type'] != 'management' or payload['kind'] != 'candidate_start' or plan['action'] == 'repair_bound_environment':
        raise ControlError('control_resumption_mode_unavailable')
    authority = state.inspect_state(ROOT)
    operation = authority.active_operation
    lease = state.read_controller_lease(ROOT)
    if (operation is None or operation.operation_type is not state.OperationType.MANAGEMENT
            or operation.owner_kind is not state.OwnerKind.MANAGEMENT_CONTROLLER
            or operation.operation_id != plan['operation_id'] or authority.generation != plan['generation']
            or operation.phase is not state.LifecyclePhase.VERIFYING
            or lease is None or not state.controller_lease_is_current(lease)
            or lease.pid != payload['controller_pid'] or lease.operation_id != operation.operation_id
            or lease.generation != authority.generation):
        raise ControlError('control_resumption_review_changed')
    controller_peer(lease.pid, operation.operation_id)
    journal = management_store.inspect_management_journal(ROOT, operation.operation_id)
    journal_contract.validate_active_journal(journal, authority)
    if (journal.initial_service_state != 'running' or journal.activation_mode is not journal_contract.ActivationMode.CUTOVER
            or journal.action.value != plan['action'] or journal.installation_id != plan['installation_id']
            or journal.controller_invocation_id != payload['controller_invocation_id']
            or journal.stage not in {journal_contract.ManagementStage.VERIFYING, journal_contract.ManagementStage.ACCEPTED}):
        raise ControlError('control_resumption_review_changed')
    # A fresh post-stop review is required on the supported conservative systemd
    # path. Original requests and old supervisor identities remain immutable.
    management_recovery_store.require_takeover_source(ROOT, journal)
    takeover = management_recovery_store.inspect_takeover(ROOT, operation.operation_id, authority.generation)
    if takeover.review_id != plan['review_id']:
        raise ControlError('control_resumption_review_changed')
    reviewed = effective_request(journal)
    grant = read_grant(operation.operation_id, authority.generation)
    if (grant.get('resumption_plan_id') != plan['plan_id'] or grant['recovery_review_id'] != plan['review_id']
            or reviewed.expected_generation + 1 != authority.generation
            or reviewed.controller_control_manifest_sha256 != plan['control_manifest_sha256']
            or reviewed.previous != journal.previous or reviewed.candidate != journal.candidate
            or reviewed.review_id != journal.review_id or reviewed.action is not journal.action
            or reviewed.supervisor_invocation_id != journal.supervisor_invocation_id):
        raise ControlError('control_resumption_review_changed')
    execution = journal_contract.selected_execution(journal)
    if (plan['schema_version']==3
            and execution.control_manifest_sha256 != resumption_contract.stage_reader(plan,payload['kind'])):
        raise ControlError('control_resumption_review_changed')
    observed = {'installation_id': plan['installation_id'], 'operation_id': operation.operation_id,
        'owner_type': 'management', 'action': journal.action.value, 'generation': authority.generation,
        'execution_sha256': journal_contract.execution_tuple_sha256(execution), 'handoff_sha256': None,
        'accepted': journal.acceptance is not None, 'candidate_failed': False, 'previous_restored': False}
    if plan['schema_version']==3:
        observed['control_manifest_sha256']=execution.control_manifest_sha256
    stage = plan['stages'][0]
    if (observed['execution_sha256'] != stage['execution_sha256'] or stage['generation'] != authority.generation
            or stage['handoff_sha256'] is not None):
        raise ControlError('control_resumption_review_changed')
    hook = payload.get('startup_hook')
    if hook is not None and (not isinstance(hook, dict) or set(hook) != {'pid', 'start_identity'}):
        raise ControlError('control_unit_hook_forbidden')
    quiet = {'inspections': True, 'controller': (operation.operation_id, lease.pid, lease.process_start_identity)}
    if hook is not None:
        quiet['startup_hook'] = (hook['pid'], hook['start_identity'])
    require_service_quiescent(**quiet)
    verify_runtime(ROOT, execution)
    verify_links(ROOT, execution)
    if (state.inspect_state(ROOT) != authority or management_store.inspect_management_journal(ROOT, operation.operation_id) != journal
            or not state.controller_lease_is_current(lease)):
        raise ControlError('control_resumption_review_changed')
    return observed
