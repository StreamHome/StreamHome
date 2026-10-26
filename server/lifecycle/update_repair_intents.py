"""Exact UPDATE maintenance transfer evidence, never another recovery engine."""
from __future__ import annotations

from dataclasses import replace
import fcntl
import os
from pathlib import Path
import time

from . import state, update_controller, update_execution
from .execution_state import read_active_execution
from .control import update_grants
from .control.provision_worker import _private_record
from .control.rpc import ControlError
from .update_recovery import digest


def directory(root: Path, operation: str, *, create=False) -> Path:
    update_grants.metadata._uuid(operation)
    base = state.lifecycle_paths(root).lifecycle_dir/'update-repairs'
    selected = base/operation
    if create:
        state._ensure_private_directory(base, 'update repair evidence')
        state._ensure_private_directory(selected, 'update repair operation')
    else:
        state._validate_directory(base, 'update repair evidence')
        state._validate_directory(selected, 'update repair operation')
    return selected


def path(root: Path, grant: dict, *, create=False) -> Path:
    update_grants.parse_grant(grant)
    if grant['schema_version'] not in {3,4}:
        raise ControlError('control_update_repair_intent_invalid')
    return directory(root, grant['operation_id'], create=create)/(
        'intent-'+str(grant['generation']+1)+'-'+grant['review_id']+'.json')


def same_journal(source: dict, current: dict, generation: int) -> bool:
    return (set(source) == set(current) and type(current['lifecycle_generation']) is int
        and current['lifecycle_generation'] == generation and current['lifecycle_phase'] == 'recovery_required'
        and type(current['updated_at']) in {int, float} and current['updated_at'] >= source['updated_at']
        and all(current[name] == source[name] for name in source
            if name not in {'lifecycle_generation', 'lifecycle_phase', 'updated_at'}))


def _journal(root: Path, grant: dict, value: dict) -> None:
    try:
        bound = update_execution.parse(value)
        authority = state.inspect_state(root)
        if bound is None or authority.active_operation is None:
            raise ValueError('missing original update')
        virtual = state.LifecycleState(authority.schema_version, value['lifecycle_generation'],
            replace(authority.active_operation, phase=state.LifecyclePhase(value['lifecycle_phase'])))
        update_controller._validate_update_journal_semantics(value, authority=virtual)
        update_grants.require_historical_binding(grant, bound, read_active_execution(root), value)
        if digest(read_active_execution(root).payload()) != grant['active_record_sha256']:
            raise ValueError('repair cannot publish another active pointer')
    except (ValueError, KeyError, TypeError, update_controller.UpdateControllerError) as exc:
        raise ControlError('control_update_repair_intent_invalid') from exc


def parse(root: Path, grant: dict, value: dict) -> dict:
    update_grants.parse_grant(grant)
    fields = {'schema_version', 'installation_id', 'operation_id', 'controller_generation',
        'grant_sha256', 'source_journal', 'normalized_journal', 'stage'}
    if (grant['schema_version'] not in {3,4} or not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['installation_id'] != grant['installation_id'] or value['operation_id'] != grant['operation_id']
            or type(value['controller_generation']) is not int or value['controller_generation'] != grant['generation']+1
            or value['grant_sha256'] != digest(grant)
            or not isinstance(value['stage'], str)
            or value['stage'] not in {'planned', 'normalization_planned', 'normalized', 'transferred', 'repaired'}):
        raise ControlError('control_update_repair_intent_invalid')
    original = value['source_journal'];_journal(root, grant, original)
    if (digest(original) != grant['source_journal_sha256']
            or original['lifecycle_generation'] != grant['source_journal_generation']):
        raise ControlError('control_update_repair_intent_invalid')
    normalized = value['normalized_journal']
    if normalized is not None:
        _journal(root, grant, normalized)
        if not same_journal(original, normalized, grant['generation']):
            raise ControlError('control_update_repair_intent_invalid')
    if value['stage'] != 'planned' and normalized is None:
        raise ControlError('control_update_repair_intent_invalid')
    return value


def read(root: Path, grant: dict) -> dict:
    return parse(root, grant, _private_record(path(root, grant)))


def gap_witness(root: Path, authority, journal: dict) -> dict:
    operation = authority.active_operation
    if (operation is None or operation.operation_type is not state.OperationType.UPDATE
            or operation.owner_kind is not state.OwnerKind.UPDATER
            or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or journal['lifecycle_generation']+1 != authority.generation or journal['lifecycle_phase'] != 'recovery_required'):
        raise ControlError('control_update_repair_gap_unproven')
    grant = update_grants.read_grant(operation.operation_id, authority.generation, live=False)
    intent = read(root, grant)
    if (grant['schema_version'] not in {3,4} or intent['stage'] != 'normalized'
            or intent['normalized_journal'] != journal or grant['generation'] != journal['lifecycle_generation']):
        raise ControlError('control_update_repair_gap_unproven')
    return {'source_generation': grant['generation'], 'controller_generation': authority.generation,
        'grant_sha256': digest(grant), 'intent_sha256': digest(intent), 'journal_sha256': digest(journal),
        'repair_transfer': True}


def prepare_transfer(root: Path, grant: dict, journal: dict) -> dict:
    """Write ahead before the existing controller's ownership CAS."""
    update_grants.require_live(grant);_journal(root, grant, journal)
    from .update_recovery_observation import read_binding
    authority, observed, bound, _ = read_binding(root, grant['operation_id'])
    if (authority.generation != grant['generation'] or authority.active_operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or observed != journal or digest(journal) != grant['source_journal_sha256']):
        raise ControlError('control_recovery_review_stale')
    destination = path(root, grant, create=True)
    if destination.exists() or destination.is_symlink():
        raise ControlError('control_update_repair_intent_exists')
    intent = {'schema_version': 1, 'installation_id': grant['installation_id'], 'operation_id': grant['operation_id'],
        'controller_generation': grant['generation']+1, 'grant_sha256': digest(grant),
        'source_journal': journal, 'normalized_journal': None, 'stage': 'planned'}
    state._atomic_write_json(destination, parse(root, grant, intent))
    normalized = {**journal, 'lifecycle_generation': grant['generation'], 'lifecycle_phase': 'recovery_required',
        'updated_at': max(time.time(), journal['updated_at'])}
    update_execution.require_transition(journal, normalized)
    intent.update(normalized_journal=normalized, stage='normalization_planned')
    state._atomic_write_json(destination, parse(root, grant, intent))
    journal_path, lock, _ = update_controller._update_paths(root, grant['operation_id'], grant['generation'])
    descriptor = state._open_locked_file(lock)
    try:
        update_grants.require_live(grant)
        with state.hold_operation_with_credential(root, operation_id=grant['operation_id'], generation=grant['generation'],
                expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED, expected_operation_type=state.OperationType.UPDATE) as current:
            if current != authority or update_controller.read_update_journal(journal_path) != journal:
                raise ControlError('control_recovery_review_stale')
            update_controller._validate_update_journal_semantics(normalized, authority=current)
            from .runtime_build import canonical_bytes
            update_controller._private_atomic_write(journal_path, canonical_bytes(normalized))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)
    intent['stage'] = 'normalized';state._atomic_write_json(destination, parse(root, grant, intent))
    return intent
