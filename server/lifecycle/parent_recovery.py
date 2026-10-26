"""UID-only exact restore review using the existing canonical recovery planner.

No claim, normalization, journal write, dependency repair or service start is
authorized by this read-only projection. Legacy unbound journals stay unbound.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import recovery, state
from .managed_restore import bound_journal, _endpoint
from .management_journal import execution_tuple_sha256
from .control.rpc import ControlError
from .control.tickets import read_stop_epoch


def review(root: Path, operation_id: str, *, control_manifest_sha256: str) -> dict:
    journal, bound = bound_journal(root, operation_id)
    authority = state.inspect_state(root)
    if (authority.generation != journal['generation'] or authority.active_operation is None
            or authority.active_operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or bound.execution.control_manifest_sha256 != control_manifest_sha256):
        raise ControlError('control_parent_recovery_unavailable')
    epoch = read_stop_epoch()
    plan = recovery.plan_recovery(root, operation_id=operation_id, generation=authority.generation)
    if plan['runtimeStatus'] == 'active':
        _endpoint(root, bound)
    fresh_journal, fresh_bound = bound_journal(root, operation_id)
    if (fresh_journal != journal or fresh_bound != bound or state.inspect_state(root) != authority
            or read_stop_epoch() != epoch):
        raise ControlError('control_recovery_review_stale')
    # Keep generic action/evidence names unchanged for execute_recovery. The
    # outer SHA also binds original managed execution, current OS intent and the
    # entire journal rather than inferring any parent identity from active links.
    evidence = {'canonical_plan': plan, 'journal': journal,
        'managed_execution': bound.payload(), 'stop_epoch': epoch,
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()
    from .control.parent_grants import binding_sha256
    return {'schema_version': 1, 'installation_id': bound.installation_id,
        'operation_id': operation_id, 'operation_type': 'restore',
        'generation': authority.generation, 'accepted': journal['accepted'],
        'execution_sha256': execution_tuple_sha256(bound.execution),
        'parent_binding_sha256': binding_sha256(bound),
        'control_manifest_sha256': control_manifest_sha256,
        'original_generation': bound.original_generation,
        'original_supervisor_invocation_id': bound.supervisor_invocation_id,
        'runtime_status': plan['runtimeStatus'], 'stop_epoch': epoch,
        'permitted_actions': plan['permittedActions'], 'canonical_plan': plan,
        'evidence_sha256': digest, 'read_only': True}


def require_controller_permission(root: Path, operation_id: str, generation: int) -> None:
    from .restore_controller import inspect_restore_journal
    from .parent_execution import parse_parent_execution
    from .control import parent_grants
    journal = inspect_restore_journal(root, operation_id)
    if journal['schema_version'] != 2:
        raise ControlError('control_parent_binding_changed')
    bound = parse_parent_execution(journal['managed_execution'])
    if generation <= bound.original_generation+1:
        return  # Original HTTP-gated takeover uses its independent pipe custody.
    grant = parent_grants.read_grant(operation_id, generation)
    if grant['schema_version'] not in {1,2}:
        raise ControlError('control_parent_recovery_action_unavailable')
    parent_grants.require_binding(grant, bound)


def require_takeover_permission(root: Path, operation_id: str, generation: int, action: str) -> None:
    """A fresh takeover uses its new grant, never an expired prior issuer."""
    from .control import parent_grants
    if action=='repair_bound_control':
        from .bound_control_parent_repair import require_takeover_permission as control_permission
        control_permission(root,operation_id,generation,'restore')
        return
    if action == 'repair_bound_environment':
        from . import bound_parent_repair
        grant=parent_grants.read_grant(operation_id,generation+1)
        current=bound_parent_repair.plan(root,operation_id,
            control_manifest_sha256=grant['control_manifest_sha256'])
        if (grant['schema_version']!=3 or grant['generation']!=generation
                or current['generation']!=generation or current['evidence_sha256']!=grant['source_evidence_sha256']):
            raise ControlError('control_parent_recovery_request_changed')
        return
    journal, bound = bound_journal(root, operation_id)
    grant = parent_grants.read_grant(operation_id, generation+1)
    parent_grants.require_binding(grant, bound)
    if (journal['generation'] != generation or grant['generation'] != generation
            or grant['action'] != action):
        raise ControlError('control_parent_recovery_request_changed')


def execute(root: Path, operation_id: str, *, control_manifest_sha256: str) -> int:
    from .control import parent_grants
    from .control.processes import controller_peer
    controller_peer(os.getpid(), operation_id)
    journal, bound = bound_journal(root, operation_id)
    grant = parent_grants.read_grant(operation_id, journal['generation']+1)
    parent_grants.require_binding(grant, bound)
    current = review(root, operation_id, control_manifest_sha256=control_manifest_sha256)
    if (grant['source_evidence_sha256'] != current['evidence_sha256']
            or grant['canonical_evidence_identity'] != current['canonical_plan']['evidenceIdentity']
            or grant['action'] not in current['permitted_actions']):
        raise ControlError('control_recovery_review_stale')

    def guard():
        # Reread the immutable root permission on every guarded boundary. A
        # replacement review, dead issuer, timeout or direct Stop fences it.
        if parent_grants.read_grant(operation_id, grant['generation']+1) != grant:
            raise ControlError('control_parent_recovery_request_changed')
        parent_grants.require_binding(grant, bound)

    result = recovery.execute_recovery(root, operation_id=operation_id, generation=journal['generation'],
        action=grant['action'], evidence_identity=grant['canonical_evidence_identity'], execution_guard=guard)
    return 0 if result['status'] == 'succeeded' else 1


def result(root: Path, operation_id: str, controller_generation: int) -> dict:
    from .control import parent_grants
    from .layout import layout_for_root
    from .execution_state import read_active_execution
    grant = parent_grants.read_grant(operation_id, controller_generation, live=False)
    layout = layout_for_root(root)
    active = read_active_execution(root)
    if (not layout.managed or os.geteuid() == 0
            or layout.descriptor.descriptor.service_uid != os.geteuid()
            or active.installation_id != grant['installation_id']
            or execution_tuple_sha256(active.execution) != grant['execution_sha256']):
        raise ControlError('control_parent_binding_changed')
    _, record = recovery._find_action_record(root, operation_id, controller_generation)
    authority = state.inspect_state(root)
    exact = (record.operation_type == 'restore' and record.generation == grant['generation']
        and record.requested_action == grant['action']
        and record.evidence_identity == grant['canonical_evidence_identity']
        and record.status == 'succeeded' and authority.generation >= controller_generation
        and authority.active_operation is None)
    if exact and record.result == 'operator_cleanup_pending':
        exact = recovery._resolution_is_exact(root, record, controller_generation)
    elif exact:
        exact = record.result in {'operator_recovery_completed', 'operator_safe_abort_completed'}
    return {'completed': exact, 'generation': controller_generation,
        'action': grant['action'], 'result_code': record.result}
