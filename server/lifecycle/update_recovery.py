"""Read-only managed UPDATE review for the existing canonical recovery engine.

No permission, claim, normalization, service start or repair is issued here.
Original journal tuples and active-pointer lineage remain part of the exact review.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import recovery, state
from .execution_state import read_active_execution
from .management_journal import execution_tuple_sha256
from .control.rpc import ControlError
from .control.tickets import read_stop_epoch


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def request_sha256(journal) -> str:
    return digest({name: journal[name] for name in
        ('target_commit', 'previous_commit', 'automatic', 'install_mode', 'update_branch')})


def runtime_link_source(root: Path):
    """Read the retained paired-link receipt as evidence, never publication authority."""
    from .layout import layout_for_root
    from .control.provision_worker import _private_record
    path = layout_for_root(root).state/'runtime-links.json'
    try:
        return _private_record(path)
    except FileNotFoundError:
        return None


def require_controller_permission(root: Path, journal, bound, generation: int):
    """A later generation requires its own live root permission, never an old issuer."""
    if type(generation) is not int or journal['lifecycle_generation'] != generation:
        raise ControlError('control_controller_changed')
    from .control import update_grants
    if generation == bound.parent.original_generation+1:
        try:
            update_grants.grant_path(bound.parent.operation_id, generation).lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ControlError('control_update_recovery_request_changed') from exc
    if generation <= bound.parent.original_generation:
        raise ControlError('control_controller_changed')
    grant = update_grants.read_grant(bound.parent.operation_id, generation)
    if grant['operation_id'] != bound.parent.operation_id or grant['generation']+1 != generation:
        raise ControlError('control_update_recovery_request_changed')
    update_grants.require_binding(grant, bound, read_active_execution(root), journal)
    return grant


def require_takeover_permission(root: Path, operation_id: str, generation: int, action: str):
    """Validate the fresh review before the existing canonical ownership transfer."""
    from .control import update_grants
    from .managed_update import require_operation_entry
    require_operation_entry(operation_id)
    if action=='repair_bound_control':
        from .bound_control_parent_repair import require_takeover_permission as control_permission
        return control_permission(root,operation_id,generation,'update')
    if action == 'repair_bound_environment':
        from .bound_update_repair import require_takeover_permission as repair_permission
        return repair_permission(root, operation_id, generation)
    from .update_recovery_observation import read_binding
    authority, journal, bound, _ = read_binding(root, operation_id)
    grant = update_grants.read_grant(operation_id, generation+1)
    if (authority.generation != generation
            or authority.active_operation.phase.value != grant['source_lifecycle_phase']
            or grant['generation'] != generation or grant['action'] != action):
        raise ControlError('control_update_recovery_request_changed')
    update_grants.require_binding(grant, bound, read_active_execution(root), journal)
    return grant


def prepare_transfer(root: Path, operation_id: str, generation: int, action: str) -> None:
    """Normalize only a witnessed subordinate gap under a fresh root permission.

    The existing engine calls this after its canonical intent/check, while it
    holds controller.lock, before its original ownership CAS. Its selected
    canonical intent is the write-ahead record; no new authority is introduced.
    """
    import fcntl
    import os
    import time
    from . import update_controller, update_execution
    from .control import update_grants
    from .runtime_build import canonical_bytes
    from .update_recovery_observation import read_binding
    grant = require_takeover_permission(root, operation_id, generation, action)
    authority, journal, bound, witness = read_binding(root, operation_id)
    if witness is None:
        return
    paths = recovery._new_action_paths(root, operation_id, generation, action_id=grant['review_id'])
    intent = recovery.read_operator_action_record(paths.record_path)
    if (intent.operation_id != operation_id or intent.operation_type != 'update'
            or intent.generation != generation or intent.status != 'planned' or intent.controller_generation is not None
            or intent.requested_action != action or intent.evidence_identity != grant['canonical_evidence_identity']):
        raise ControlError('control_update_recovery_request_changed')
    path, lock, _ = update_controller._update_paths(root, operation_id, generation)
    descriptor = state._open_locked_file(lock)
    try:
        if read_binding(root, operation_id) != (authority, journal, bound, witness):
            raise ControlError('control_recovery_review_stale')
        normalized = {**journal, 'lifecycle_generation': generation,
            'lifecycle_phase': authority.active_operation.phase.value,
            'updated_at': max(time.time(), journal.get('updated_at', 0))}
        update_execution.require_transition(journal, normalized)
        selected = update_execution.parse(normalized)
        with state.hold_operation_with_credential(root, operation_id=operation_id, generation=generation,
                expected_phase=authority.active_operation.phase,
                expected_operation_type=state.OperationType.UPDATE) as current:
            if (current != authority or update_controller.read_update_journal(path) != journal
                    or recovery.read_operator_action_record(paths.record_path) != intent
                    or update_grants.read_grant(operation_id, generation+1) != grant):
                raise ControlError('control_recovery_review_stale')
            update_grants.require_binding(grant, selected, read_active_execution(root), normalized)
            update_controller._validate_update_journal_semantics(normalized, authority=current)
            update_controller._private_atomic_write(path, canonical_bytes(normalized))
        update_grants.require_live(grant)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)


def review(root: Path, operation_id: str, *, control_manifest_sha256: str) -> dict:
    from .update_recovery_observation import read_binding
    authority, journal, bound, witness = read_binding(root, operation_id)
    parent = bound.parent
    if (authority.active_operation.phase not in {state.LifecyclePhase.RESERVED, state.LifecyclePhase.RECOVERY_REQUIRED}
            or parent.execution.control_manifest_sha256 != control_manifest_sha256):
        raise ControlError('control_update_recovery_unavailable')
    active = read_active_execution(root)
    original_pointer = (active.execution == parent.execution and active.generation < parent.original_generation)
    accepted_pointer = (bound.accepted is not None and active.execution == bound.accepted
        and active.operation_id == operation_id and parent.original_generation < active.generation <= authority.generation)
    if active.installation_id != parent.installation_id or not (original_pointer or accepted_pointer):
        raise ControlError('control_parent_binding_changed')
    epoch = read_stop_epoch()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    plan = recovery.plan_recovery(root, operation_id=operation_id, generation=authority.generation)
    links = runtime_link_source(root)
    actions = set(plan['permittedActions']).intersection({'safe_abort', 'continue_recovery'})
    if bound.accepted is not None:
        actions.discard('safe_abort')
    elif bound.checkpoint is None or plan['artifacts']['databaseCheckpoint'] != 'valid':
        # A continuation may execute the original rollback. Its snapshot must
        # already be attributable before any root permission is prepared.
        actions.discard('continue_recovery')
    if (read_binding(root, operation_id) != (authority, journal, bound, witness)
            or read_active_execution(root) != active or read_stop_epoch() != epoch
            or runtime_link_source(root) != links
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot):
        raise ControlError('control_recovery_review_stale')
    evidence = {'canonical_plan': plan, 'journal': journal,
        'active_execution': active.payload(), 'stop_epoch': epoch, 'boot_id': boot,
        'transfer_witness': witness, 'runtime_link_source': links}
    return {'schema_version': 1, 'installation_id': parent.installation_id,
        'operation_id': operation_id, 'operation_type': 'update', 'generation': authority.generation,
        'accepted_execution_sha256': execution_tuple_sha256(bound.accepted) if bound.accepted else None,
        'previous_execution_sha256': execution_tuple_sha256(parent.execution),
        'candidate_execution_sha256': execution_tuple_sha256(bound.candidate) if bound.candidate else None,
        'execution_sha256': execution_tuple_sha256(bound.accepted or parent.execution),
        'active_record_sha256': digest(active.payload()), 'parent_binding_sha256': digest(parent.payload()),
        'request_sha256': request_sha256(journal),
        'database_checkpoint_sha256': digest(journal['managed_database_checkpoint']),
        'runtime_link_source_sha256': digest(links),
        'control_manifest_sha256': control_manifest_sha256, 'original_generation': parent.original_generation,
        'original_supervisor_invocation_id': parent.supervisor_invocation_id,
        'runtime_status': plan['runtimeStatus'], 'stop_epoch': epoch, 'boot_id': boot,
        'permitted_actions': sorted(actions), 'canonical_plan': plan,
        'evidence_sha256': digest(evidence), 'read_only': True}


def execute(root: Path, operation_id: str, *, control_manifest_sha256: str) -> int:
    """Run the existing canonical continuation under its fresh protected grant."""
    from .control import update_grants
    from .control.reader_support import require_managed_update_reader
    from .update_recovery_observation import read_binding
    authority, journal, bound, _ = read_binding(root, operation_id)
    require_managed_update_reader(bound.parent.execution)
    selected_grant = update_grants.read_grant(operation_id, authority.generation+1)
    grant = require_takeover_permission(root, operation_id, authority.generation, selected_grant['action'])
    current = review(root, operation_id, control_manifest_sha256=control_manifest_sha256)
    if (grant['control_manifest_sha256'] != control_manifest_sha256
            or grant['source_evidence_sha256'] != current['evidence_sha256']
            or grant['canonical_evidence_identity'] != current['canonical_plan']['evidenceIdentity']
            or grant['action'] not in current['permitted_actions']):
        raise ControlError('control_recovery_review_stale')

    def guard():
        if update_grants.read_grant(operation_id, grant['generation']+1) != grant:
            raise ControlError('control_update_recovery_request_changed')
        observed, payload, selected, _ = read_binding(root, operation_id)
        if observed.generation not in {grant['generation'], grant['generation']+1}:
            raise ControlError('control_controller_changed')
        update_grants.require_binding(grant, selected, read_active_execution(root), payload)

    result = recovery.execute_recovery(root, operation_id=operation_id, generation=authority.generation,
        action=grant['action'], evidence_identity=grant['canonical_evidence_identity'], execution_guard=guard,
        action_id=grant['review_id'])
    return 0 if result['status'] == 'succeeded' else 1


def result(root: Path, operation_id: str, controller_generation: int, *, control_manifest_sha256: str) -> dict:
    """Read the exact completed attempt; normal status rewrites are irrelevant."""
    import os
    from . import update_execution, update_history
    from .control import update_grants
    from .control.provision_worker import _private_record
    from .execution_state import ExecutionRecord
    from .layout import layout_for_root
    grant = update_grants.read_grant(operation_id, controller_generation, live=False)
    layout = layout_for_root(root)
    if (not layout.managed or os.geteuid() == 0 or layout.descriptor.descriptor.service_uid != os.geteuid()
            or layout.descriptor.descriptor.installation_id != grant['installation_id']
            or grant['control_manifest_sha256'] != control_manifest_sha256):
        raise ControlError('control_parent_binding_changed')
    retained = _private_record(update_history._path(root, operation_id, controller_generation))
    journal = retained.get('journal')
    if (not isinstance(journal, dict) or retained != update_history._record(journal)
            or retained['generation'] != controller_generation or retained['operation_id'] != operation_id):
        raise ControlError('control_update_history_changed')
    bound = update_execution.parse(journal)
    if bound is None:
        raise ControlError('control_update_history_nonterminal')
    aborted = grant['action'] == 'safe_abort'
    if aborted:
        update_grants.require_retained_abort_binding(grant, bound, journal)
    else:
        if bound.accepted is None:
            raise ControlError('control_update_history_nonterminal')
        accepted = ExecutionRecord(bound.parent.installation_id, bound.accepted, operation_id, controller_generation)
        update_grants.require_historical_binding(grant, bound, accepted, journal)
    path = state.lifecycle_paths(root).lifecycle_dir/'operator-actions'/operation_id/(
        f"action.{grant['generation']}.{grant['review_id'].replace('-', '')}.json")
    record = recovery.read_operator_action_record(path)
    authority = state.inspect_state(root)
    exact = (record.operation_id == operation_id and record.operation_type == 'update'
        and record.generation == grant['generation'] and record.controller_generation == controller_generation
        and record.requested_action == grant['action']
        and record.evidence_identity == grant['canonical_evidence_identity']
        and authority.generation >= controller_generation
        and (authority.active_operation is None or authority.active_operation.operation_id != operation_id))
    confirmed = (record.status == 'succeeded'
        and record.result in ({'operator_safe_abort_completed', 'operator_cleanup_pending'} if aborted
            else {'operator_recovery_completed', 'operator_cleanup_pending'}))
    # A crash after canonical completion but before the action receipt finished
    # is a read-only completion fact, not permission to run another recovery.
    if not exact or confirmed:
        reconcile_proof = False
    elif aborted:
        lease = state.read_controller_lease(root)
        reconcile_proof = (authority.generation == controller_generation and authority.active_operation is None
            and digest(read_active_execution(root).payload()) == grant['active_record_sha256']
            and not (lease is not None and lease.operation_id == operation_id and state.controller_lease_is_current(lease)))
    else:
        reconcile_proof = recovery._verified_update_exit_twelve(root, operation_id, controller_generation)
    reconciled = exact and not confirmed and reconcile_proof
    return {'completed': bool(exact and (confirmed or reconciled)), 'generation': controller_generation,
        'action': grant['action'], 'result_code': record.result, 'completion_reconciled': bool(reconciled)}
