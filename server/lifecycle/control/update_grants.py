"""Finite protected UPDATE permission metadata; never a second lifecycle owner.

Root parses metadata only. Canonical review/preparation and binding interpretation
are UID-only. Stopped starts require their separate one-use resumption plan.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import time

from . import resumption_contract as metadata
from .rpc import ControlError
from .stopped_repair import parse_scope, require_scope
from .tickets import read_protected_json, read_stop_epoch


ROOT = Path('/etc/streamhome/control/operations')


def parse_grant(value: dict) -> dict:
    fields = {'schema_version', 'review_id', 'installation_id', 'operation_id', 'operation_type',
        'generation', 'original_generation', 'parent_binding_sha256', 'control_manifest_sha256',
        'source_evidence_sha256', 'canonical_evidence_identity', 'active_record_sha256',
        'request_sha256', 'database_checkpoint_sha256',
        'previous_execution_sha256', 'candidate_execution_sha256', 'accepted_execution_sha256',
        'permitted_execution_sha256', 'runtime_status', 'action', 'issuer_scope', 'boot_id',
        'stop_epoch', 'expires_at_monotonic', 'source_lifecycle_phase', 'runtime_link_source_sha256'}
    resuming = isinstance(value, dict) and type(value.get('schema_version')) is int and value['schema_version'] == 2
    repairing = isinstance(value, dict) and type(value.get('schema_version')) is int and value['schema_version'] in {3,4}
    control_repair=repairing and value['schema_version']==4
    if resuming:
        fields.add('resumption_plan_id')
    if repairing:
        fields.remove('canonical_evidence_identity')
        fields.update({'filesystem_sha256', 'source_journal_sha256', 'source_journal_generation', 'repair_execution_sha256'})
    if control_repair:fields.update({'control_artifact','control_selection'})
    try:
        if (not isinstance(value, dict) or set(value) != fields
                or type(value['schema_version']) is not int or value['schema_version'] not in {1,2,3,4}
                or value['operation_type'] != 'update'
                or value['action'] not in ({'repair_bound_control'} if control_repair else {'repair_bound_environment'} if repairing else {'continue_recovery', 'safe_abort'})
                or value['runtime_status'] not in {'active', 'offline'}
                or value['source_lifecycle_phase'] not in {'reserved', 'recovery_required'}
                or value['action'] == 'safe_abort' and value['runtime_status'] != 'offline'
                or value['action'] == 'continue_recovery' and value['source_lifecycle_phase'] != 'recovery_required'
                or value['action'] == 'continue_recovery' and value['runtime_status'] != ('offline' if resuming else 'active')
                or resuming and value['action'] != 'continue_recovery'
                or repairing and (value['runtime_status'] != 'offline' or value['source_lifecycle_phase'] != 'recovery_required')):
            raise ValueError('invalid UPDATE recovery envelope')
        for name in ('review_id', 'installation_id', 'operation_id', 'boot_id'):
            metadata._uuid(value[name])
        if resuming:
            metadata._uuid(value['resumption_plan_id'])
        if (type(value['generation']) is not int or not 1 <= value['generation'] < 2**63-1
                or type(value['original_generation']) is not int
                or not 1 <= value['original_generation'] <= value['generation']):
            raise ValueError('invalid UPDATE recovery generation')
        for name in ('parent_binding_sha256', 'control_manifest_sha256', 'source_evidence_sha256',
                     'active_record_sha256', 'previous_execution_sha256', 'stop_epoch',
                     'request_sha256', 'database_checkpoint_sha256', 'runtime_link_source_sha256'):
            metadata._digest(value[name])
        for name in ('candidate_execution_sha256', 'accepted_execution_sha256'):
            if value[name] is not None:metadata._digest(value[name])
        previous, candidate, accepted = (value[name] for name in
            ('previous_execution_sha256', 'candidate_execution_sha256', 'accepted_execution_sha256'))
        if candidate == previous or accepted is not None and accepted not in {previous, candidate}:
            raise ValueError('invalid UPDATE recovery tuple selection')
        if repairing:
            if control_repair:
                from .artifact_stage import parse_repair_binding
                parse_repair_binding(value)
            for name in ('filesystem_sha256', 'source_journal_sha256', 'repair_execution_sha256'):
                metadata._digest(value[name])
            if (type(value['source_journal_generation']) is not int
                    or value['source_journal_generation'] not in {value['generation']-1, value['generation']}
                    or value['source_journal_generation'] < 1
                    or value['repair_execution_sha256'] not in ({accepted} if accepted is not None else {previous, candidate})):
                raise ValueError('invalid UPDATE bound repair selection')
            expected = [value['repair_execution_sha256']]
        elif value['action'] == 'safe_abort':
            if accepted is not None:raise ValueError('accepted UPDATE cannot be aborted')
            expected = [previous]
        else:
            if candidate is None:raise ValueError('UPDATE continuation lacks prepared runtime')
            expected = [accepted] if accepted is not None else ([previous] if resuming else sorted([previous, candidate]))
        if value['permitted_execution_sha256'] != expected:
            raise ValueError('invalid UPDATE finite runtime set')
        if not repairing and (not isinstance(value['canonical_evidence_identity'], str)
                or not re.fullmatch(r'plan-v1:[a-f0-9]{32}', value['canonical_evidence_identity'])):
            raise ValueError('invalid canonical UPDATE review')
        expiry = value['expires_at_monotonic']
        if type(expiry) not in {int, float} or not math.isfinite(expiry) or not 0 < expiry < 10**12:
            raise ValueError('invalid UPDATE permission deadline')
        parse_scope(value['issuer_scope'], value['installation_id'])
        return value
    except (KeyError, TypeError, ValueError, ControlError) as exc:
        raise ControlError('control_update_recovery_request_invalid') from exc


def require_live(value: dict) -> None:
    grant = parse_grant(value)
    if (time.monotonic() >= grant['expires_at_monotonic'] or read_stop_epoch() != grant['stop_epoch']
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != grant['boot_id']):
        raise ControlError('control_update_recovery_permission_expired')
    require_scope(grant['issuer_scope'], grant['installation_id'])


def require_live_identity(value: dict) -> None:
    """Cheap OS/epoch/deadline proof between repeated complete root-scope checks."""
    from .administration_lock import process_identity
    grant = parse_grant(value);scope = grant['issuer_scope']
    if (time.monotonic() >= grant['expires_at_monotonic'] or read_stop_epoch() != grant['stop_epoch']
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != grant['boot_id']):
        raise ControlError('control_update_recovery_permission_expired')
    try:
        start, status = process_identity(scope['pid']);process = Path('/proc')/str(scope['pid'])
        if (start != scope['start_identity'] or status in {'Z','X'} or process.stat().st_uid != 0
                or (process/'cgroup').read_text().splitlines() != ['0::/system.slice/'+scope['unit']]):
            raise ControlError('control_update_recovery_permission_expired')
    except OSError as exc:
        raise ControlError('control_update_recovery_permission_expired') from exc


def grant_path(operation_id: str, controller_generation: int) -> Path:
    metadata._uuid(operation_id)
    if type(controller_generation) is not int or not 2 <= controller_generation < 2**63:
        raise ControlError('control_update_recovery_request_invalid')
    return ROOT/operation_id/f'update-recovery-{controller_generation}.json'


def read_grant(operation_id: str, controller_generation: int, *, live=True) -> dict:
    value = parse_grant(read_protected_json(grant_path(operation_id, controller_generation)))
    if (value['operation_id'] != operation_id or value['generation']+1 != controller_generation
            or read_protected_json(ROOT/operation_id/('update-review-'+value['review_id']+'.json')) != value):
        raise ControlError('control_update_recovery_request_changed')
    if live:require_live(value)
    return value


def require_binding(grant: dict, bound, active, journal) -> None:
    """Permit only original reviewed pointer or this grant's exact publication."""
    require_live(grant)
    _require_binding(grant, bound, active, journal)
    if grant['schema_version'] == 2:
        require_resumption_plan(grant)


def require_historical_binding(grant: dict, bound, active, journal) -> None:
    """Read historical identity only. This never authorizes another mutation."""
    parse_grant(grant)
    _require_binding(grant, bound, active, journal)


def require_retained_abort_binding(grant: dict, bound, journal) -> None:
    """Check immutable aborted history only; this carries no mutation permission."""
    parse_grant(grant)
    if grant['action'] != 'safe_abort' or journal.get('error') != 'operator_safe_abort':
        raise ControlError('control_parent_binding_changed')
    _require_binding(grant, bound, None, journal, retained_abort=True)


def require_resumption_plan(grant: dict) -> dict:
    """Bind fresh stopped recovery to one independently reviewed OS start."""
    from .resumption_ticket import reviewed_plan
    parse_grant(grant)
    if grant['schema_version'] != 2:
        raise ControlError('control_resumption_review_changed')
    record = reviewed_plan(grant['resumption_plan_id'], installation_id=grant['installation_id'],
        control_manifest_sha256=grant['control_manifest_sha256'])
    plan = record['plan']
    if (plan['schema_version'] != 2 or plan['owner_type'] != 'update' or plan['action'] != 'update_recovery'
            or plan['operation_id'] != grant['operation_id'] or plan['generation'] != grant['generation']+1
            or plan['review_id'] != grant['review_id'] or plan['recovery_action'] != grant['action']
            or plan['parent_binding_sha256'] != grant['parent_binding_sha256']
            or [plan['stages'][0]['execution_sha256']] != grant['permitted_execution_sha256']
            or record['issuer']['scope'] != grant['issuer_scope']):
        raise ControlError('control_resumption_review_changed')
    return record


def _require_binding(grant: dict, bound, active, journal, *, retained_abort=False) -> None:
    from ..management_journal import execution_tuple_sha256
    from ..update_execution import parse as parse_update
    from ..update_recovery import digest, request_sha256
    try:
        current = parse_update(journal)
        request = request_sha256(journal)
        checkpoint = digest(journal['managed_database_checkpoint'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlError('control_parent_binding_changed') from exc
    parent = bound.parent
    accepted = execution_tuple_sha256(bound.accepted) if bound.accepted else None
    publication = (active is not None and active.installation_id == parent.installation_id and active.operation_id == parent.operation_id
        and active.generation == grant['generation']+1 and bound.accepted is not None
        and active.execution == bound.accepted and accepted in grant['permitted_execution_sha256'])
    if (current != bound
            or grant['action'] == 'safe_abort' and (bound.accepted is not None
                or bound.verification is not None or bound.checkpoint is not None
                or journal.get('lifecycle_phase') not in {'reserved', 'recovery_required'}
                or journal.get('phase') not in {'queued', 'preflight', 'waiting_for_idle', 'failed'}
                or journal.get('web_artifacts_swapped') or journal.get('web_dependencies_swapped'))
            or parent.installation_id != grant['installation_id'] or parent.operation_id != grant['operation_id']
            or parent.original_generation != grant['original_generation']
            or digest(parent.payload()) != grant['parent_binding_sha256']
            or parent.execution.control_manifest_sha256 != grant['control_manifest_sha256']
            or request != grant['request_sha256']
            or checkpoint != grant['database_checkpoint_sha256']
            or execution_tuple_sha256(parent.execution) != grant['previous_execution_sha256']
            or (execution_tuple_sha256(bound.candidate) if bound.candidate else None) != grant['candidate_execution_sha256']
            or grant['accepted_execution_sha256'] is not None and accepted != grant['accepted_execution_sha256']
            or accepted is not None and accepted not in grant['permitted_execution_sha256']
            or not retained_abort and (digest(active.payload()) != grant['active_record_sha256'] and not publication)):
        raise ControlError('control_parent_binding_changed')


def prepare(root: Path, payload: dict, *, control_manifest_sha256: str) -> dict:
    """UID-only preparation after exact review; publishing still belongs to root."""
    from ..update_recovery_observation import read_binding
    from ..update_recovery import review
    from .reader_support import require_managed_update_reader
    fields = {'operation_id', 'review_id', 'expected_evidence_sha256', 'action',
        'issuer_scope', 'boot_id', 'stop_epoch'}
    resuming = isinstance(payload, dict) and 'resumption_plan_id' in payload
    if resuming:
        fields.add('resumption_plan_id')
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ControlError('control_update_recovery_request_invalid')
    current = review(root, payload['operation_id'], control_manifest_sha256=control_manifest_sha256)
    if (payload['expected_evidence_sha256'] != current['evidence_sha256']
            or payload['action'] not in current['permitted_actions']
            or payload['boot_id'] != current['boot_id'] or payload['stop_epoch'] != current['stop_epoch']):
        raise ControlError('control_recovery_review_stale')
    authority, journal, bound, witness = read_binding(root, payload['operation_id'])
    if (authority.generation != current['generation'] or bound.parent.installation_id != current['installation_id']
            or bound.parent.execution.control_manifest_sha256 != control_manifest_sha256):
        raise ControlError('control_recovery_review_stale')
    require_managed_update_reader(bound.parent.execution)
    if payload['action'] == 'safe_abort':
        permitted = [current['previous_execution_sha256']]
    elif current['accepted_execution_sha256'] is not None:
        permitted = [current['accepted_execution_sha256']]
    elif resuming:
        permitted = [current['previous_execution_sha256']]
    else:
        permitted = sorted([current['previous_execution_sha256'], current['candidate_execution_sha256']])
    value = {name: current[name] for name in ('installation_id', 'operation_id', 'operation_type',
        'generation', 'original_generation', 'parent_binding_sha256', 'control_manifest_sha256',
        'active_record_sha256', 'previous_execution_sha256', 'candidate_execution_sha256',
        'request_sha256', 'database_checkpoint_sha256', 'runtime_link_source_sha256',
        'accepted_execution_sha256', 'runtime_status', 'boot_id', 'stop_epoch')}
    value.update(schema_version=2 if resuming else 1, review_id=payload['review_id'],
        source_lifecycle_phase=current['canonical_plan']['lifecyclePhase'],
        source_evidence_sha256=current['evidence_sha256'],
        canonical_evidence_identity=current['canonical_plan']['evidenceIdentity'],
        permitted_execution_sha256=permitted, action=payload['action'], issuer_scope=payload['issuer_scope'],
        expires_at_monotonic=time.monotonic()+900)
    if resuming:
        value['resumption_plan_id'] = payload['resumption_plan_id']
    require_live(value)
    if (review(root, payload['operation_id'], control_manifest_sha256=control_manifest_sha256) != current
            or read_binding(root, payload['operation_id']) != (authority, journal, bound, witness)):
        raise ControlError('control_recovery_review_stale')
    return value
