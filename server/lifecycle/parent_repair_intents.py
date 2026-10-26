"""Exact UID repair intent and original-journal generation gap evidence.

A retained root grant can explain a previous transfer, never authorize another
one. New work still requires a fresh live root grant and canonical controller
exclusion. These records never authorize database activation or runtime starts.
"""
from __future__ import annotations

from pathlib import Path
import time

from . import restore_controller as restore, state
from .parent_execution import parse_parent_execution
from .control import parent_grants
from .control.provision_worker import _private_record
from .control.resumption_contract import digest
from .control.rpc import ControlError


def path(root: Path, grant: dict) -> Path:
    parent_grants.parse_grant(grant)
    if grant['schema_version'] not in {3,4}:
        raise ControlError('control_parent_repair_intent_invalid')
    return restore.restore_paths(root,grant['operation_id']).operation_dir / (
        ('control-repair-intent-' if grant['schema_version']==4 else 'environment-repair-intent-')+str(grant['generation']+1)+'-'+grant['review_id']+'.json')


def _journal(root: Path, grant: dict, value: dict) -> dict:
    try:
        parsed=restore._validate_journal(value,restore.restore_paths(root,grant['operation_id']))
        if parsed['schema_version'] != 2 or parsed['progress'] != 'recovery_required':
            raise ControlError('control_parent_repair_intent_invalid')
        bound=parse_parent_execution(parsed['managed_execution'])
        parent_grants.require_binding(grant,bound)
        return parsed
    except (restore.RestoreError,KeyError,TypeError,ValueError) as exc:
        raise ControlError('control_parent_repair_intent_invalid') from exc


def same_parent_journal(source: dict, current: dict, generation: int) -> bool:
    """Only the generation and its monotone observation timestamp may change."""
    return (set(source)==set(current) and type(current['generation']) is int
        and current['generation']==generation
        and type(current['updated_at']) in {int,float}
        and current['updated_at']>=source['updated_at']
        and all(current[name]==source[name] for name in source if name not in {'generation','updated_at'}))


def parse(root: Path, grant: dict, value: dict) -> dict:
    parent_grants.parse_grant(grant)
    fields={'schema_version','installation_id','operation_id','controller_generation','grant_sha256',
        'source_journal','normalized_journal','stage'}
    if (grant['schema_version'] not in {3,4} or not isinstance(value,dict) or set(value)!=fields
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['installation_id']!=grant['installation_id'] or value['operation_id']!=grant['operation_id']
            or type(value['controller_generation']) is not int
            or value['controller_generation']!=grant['generation']+1
            or value['grant_sha256']!=digest(grant) or not isinstance(value['stage'],str)
            or value['stage'] not in {'planned','normalization_planned','normalized','transferred','repaired'}):
        raise ControlError('control_parent_repair_intent_invalid')
    original=_journal(root,grant,value['source_journal'])
    if (digest(original)!=grant['source_journal_sha256']
            or original['generation']!=grant['source_journal_generation']):
        raise ControlError('control_parent_repair_intent_invalid')
    normalized=value['normalized_journal']
    if normalized is not None:
        _journal(root,grant,normalized)
        if not same_parent_journal(original,normalized,grant['generation']):
            raise ControlError('control_parent_repair_intent_invalid')
    if value['stage']!='planned' and normalized is None:
        raise ControlError('control_parent_repair_intent_invalid')
    return value


def read(root: Path, grant: dict) -> dict:
    return parse(root,grant,_private_record(path(root,grant)))


def gap_witness(root: Path, authority: state.LifecycleState, journal: dict) -> dict:
    """Read-only proof of the exact transfer-before-journal gap, even after expiry."""
    operation=authority.active_operation
    if (operation is None or operation.operation_type is not state.OperationType.RESTORE
            or operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
            or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
            or journal['generation']+1!=authority.generation):
        raise ControlError('control_parent_repair_gap_unproven')
    old=parent_grants.read_grant(operation.operation_id,authority.generation,live=False)
    if old['schema_version'] not in {3,4}:
        raise ControlError('control_parent_repair_gap_unproven')
    intent=read(root,old)
    before=intent['normalized_journal'] or intent['source_journal']
    if before['generation']!=old['generation'] or before!=journal:
        raise ControlError('control_parent_repair_gap_unproven')
    return {'grant_sha256':digest(old),'intent_sha256':digest(intent),
        'review_id':old['review_id'],'source_generation':old['generation'],
        'controller_generation':authority.generation,'journal_sha256':digest(journal)}


def prepare_transfer(root: Path, grant: dict, journal: dict) -> dict:
    """Persist exact intent and normalize a proven prior gap before another CAS.

    Called only by the existing controller's pre_transfer_check while holding
    controller.lock. The caller separately rechecks plan/artifacts/quiescence,
    credential ownership, inactive prior lease and exact operation-unit custody.
    Every normalization is write-ahead, so repeated interrupted retries retain
    a single explainable generation gap rather than accumulating journal lag.
    """
    parent_grants.require_live(grant)
    original=_journal(root,grant,journal)
    if digest(original)!=grant['source_journal_sha256']:
        raise ControlError('control_recovery_review_stale')
    authority=state.validate_operation_with_credential(root,operation_id=grant['operation_id'],
        generation=grant['generation'],expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
    if journal['generation']!=grant['generation']:
        gap_witness(root,authority,journal)
    destination=path(root,grant)
    if destination.exists() or destination.is_symlink():
        raise ControlError('control_parent_repair_intent_exists')
    intent={'schema_version':1,'installation_id':grant['installation_id'],
        'operation_id':grant['operation_id'],'controller_generation':grant['generation']+1,
        'grant_sha256':digest(grant),'source_journal':original,
        'normalized_journal':None,'stage':'planned'}
    state._atomic_write_json(destination,parse(root,grant,intent))
    normalized={**original,'generation':grant['generation'],
        'updated_at':max(time.time(),original['updated_at'])} if original['generation']!=grant['generation'] else original
    intent.update(normalized_journal=normalized,stage='normalization_planned')
    state._atomic_write_json(destination,parse(root,grant,intent))
    if normalized!=original:
        paths=restore.restore_paths(root,grant['operation_id'])
        descriptor=restore._journal_lock(paths)
        try:
            parent_grants.require_live(grant)
            if (state.inspect_state(root)!=authority
                    or restore._validate_journal(restore._read_json(paths.journal_path),paths)!=original):
                raise ControlError('control_recovery_review_stale')
            restore._atomic_write_json(paths.journal_path,normalized)
        finally:
            import fcntl,os
            fcntl.flock(descriptor,fcntl.LOCK_UN);os.close(descriptor)
    intent['stage']='normalized'
    state._atomic_write_json(destination,parse(root,grant,intent))
    return intent
