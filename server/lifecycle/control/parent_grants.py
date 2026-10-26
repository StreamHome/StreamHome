"""Finite protected permission for the existing managed RESTORE parent.

This is a permission envelope, not a management request or a replacement
recovery engine. Only the UID canonical planner interprets parent evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time

from . import resumption_contract as metadata
from .rpc import ControlError
from .stopped_repair import parse_scope, require_scope
from .tickets import read_protected_json, read_stop_epoch


ROOT = Path('/etc/streamhome/control/operations')
ACTIONS = frozenset({'continue_recovery', 'rollback', 'safe_abort'})


def binding_sha256(bound) -> str:
    return hashlib.sha256(json.dumps(bound.payload(), sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def parse_grant(value: dict) -> dict:
    fields = {'schema_version', 'review_id', 'installation_id', 'operation_id',
        'operation_type', 'generation', 'execution_sha256', 'parent_binding_sha256',
        'control_manifest_sha256', 'source_evidence_sha256', 'canonical_evidence_identity',
        'action', 'issuer_scope', 'boot_id', 'stop_epoch', 'expires_at_monotonic'}
    resuming = isinstance(value,dict) and type(value.get('schema_version')) is int and value['schema_version']==2
    repairing = isinstance(value,dict) and type(value.get('schema_version')) is int and value['schema_version'] in {3,4}
    control_repair=repairing and value['schema_version']==4
    if resuming:
        fields.add('resumption_plan_id')
    if repairing:
        fields.remove('canonical_evidence_identity')
        fields.update({'filesystem_sha256','source_journal_sha256','source_journal_generation'})
    if control_repair:fields.update({'control_artifact','control_selection'})
    try:
        if (not isinstance(value, dict) or set(value) != fields
                or type(value['schema_version']) is not int or value['schema_version'] not in {1,2,3,4}
                or value['operation_type'] != 'restore'
                or not isinstance(value['action'], str)
                or value['action'] not in ({'repair_bound_control'} if control_repair else {'repair_bound_environment'} if repairing else ACTIONS)
                or resuming and value['action']=='safe_abort'):
            raise ControlError('control_parent_recovery_request_invalid')
        if resuming:
            metadata._uuid(value['resumption_plan_id'])
        for name in ('review_id', 'installation_id', 'operation_id', 'boot_id'):
            metadata._uuid(value[name])
        if type(value['generation']) is not int or not 1 <= value['generation'] < 2**63-1:
            raise ControlError('control_parent_recovery_request_invalid')
        for name in ('execution_sha256', 'parent_binding_sha256', 'control_manifest_sha256',
                     'source_evidence_sha256', 'stop_epoch'):
            metadata._digest(value[name])
        if repairing:
            if control_repair:
                from .artifact_stage import parse_repair_binding
                parse_repair_binding(value)
            for name in ('filesystem_sha256','source_journal_sha256'):metadata._digest(value[name])
            if (type(value['source_journal_generation']) is not int
                    or value['source_journal_generation'] not in {value['generation'],value['generation']-1}
                    or value['source_journal_generation'] < 1):
                raise ControlError('control_parent_recovery_request_invalid')
        elif (not isinstance(value['canonical_evidence_identity'], str)
              or not re.fullmatch(r'plan-v1:[a-f0-9]{32}', value['canonical_evidence_identity'])):
            raise ControlError('control_parent_recovery_request_invalid')
        expiry = value['expires_at_monotonic']
        if type(expiry) not in {int, float} or not math.isfinite(expiry) or not 0 < expiry < 10**12:
            raise ControlError('control_parent_recovery_request_invalid')
        parse_scope(value['issuer_scope'], value['installation_id'])
        return value
    except (ValueError, TypeError, KeyError, ControlError) as exc:
        raise ControlError('control_parent_recovery_request_invalid') from exc


def grant_path(operation_id: str, controller_generation: int) -> Path:
    metadata._uuid(operation_id)
    if type(controller_generation) is not int or not 2 <= controller_generation < 2**63:
        raise ControlError('control_parent_recovery_request_invalid')
    return ROOT / operation_id / f'recovery-{controller_generation}.json'


def require_live(value: dict) -> None:
    grant = parse_grant(value)
    if (time.monotonic() >= grant['expires_at_monotonic']
            or grant['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or grant['stop_epoch'] != read_stop_epoch()):
        raise ControlError('control_parent_recovery_permission_expired')
    require_scope(grant['issuer_scope'], grant['installation_id'])


def require_live_identity(value: dict) -> None:
    """Cheap kernel/epoch checks between separately repeated full scope proofs."""
    from .administration_lock import process_identity
    grant=parse_grant(value)
    scope=grant['issuer_scope']
    if (time.monotonic()>=grant['expires_at_monotonic'] or read_stop_epoch()!=grant['stop_epoch']
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=grant['boot_id']):
        raise ControlError('control_parent_recovery_permission_expired')
    try:
        start,status=process_identity(scope['pid'])
        process=Path('/proc')/str(scope['pid'])
        if (start!=scope['start_identity'] or status in {'Z','X'} or process.stat().st_uid!=0
                or (process/'cgroup').read_text().splitlines()!=['0::/system.slice/'+scope['unit']]):
            raise ControlError('control_parent_recovery_permission_expired')
    except OSError as exc:
        raise ControlError('control_parent_recovery_permission_expired') from exc


def read_grant(operation_id: str, controller_generation: int, *, live: bool = True) -> dict:
    capsule=read_protected_json(grant_path(operation_id,controller_generation))
    wrapped=isinstance(capsule,dict) and type(capsule.get('schema_version')) is int and capsule['schema_version']==6
    if wrapped:
        from .control_parent_requests import parse
        value=parse(capsule)['owner_grant']
        if value['operation_type']!='restore':raise ControlError('control_parent_recovery_request_changed')
    else:value=parse_grant(capsule)
    if (value['operation_id'] != operation_id or value['generation']+1 != controller_generation
            or read_protected_json(ROOT / operation_id / ('recovery-review-'+value['review_id']+'.json')) != capsule):
        raise ControlError('control_parent_recovery_request_changed')
    if live:
        require_live(value)
        if value['schema_version']==2:
            require_resumption_plan(value)
    return value


def require_resumption_plan(grant: dict) -> dict:
    """Bind only the parent's separately reviewed single-start permission."""
    from .resumption_ticket import reviewed_plan
    parse_grant(grant)
    if grant['schema_version']!=2:
        raise ControlError('control_resumption_review_changed')
    record=reviewed_plan(grant['resumption_plan_id'],installation_id=grant['installation_id'],
        control_manifest_sha256=grant['control_manifest_sha256'])
    plan=record['plan']
    if (plan['schema_version']!=2 or plan['operation_id']!=grant['operation_id']
            or plan['owner_type']!='restore' or plan['action']!='restore_recovery'
            or plan['generation']!=grant['generation']+1 or plan['review_id']!=grant['review_id']
            or plan['recovery_action']!=grant['action']
            or plan['parent_binding_sha256']!=grant['parent_binding_sha256']
            or plan['stages'][0]['execution_sha256']!=grant['execution_sha256']
            or record['issuer']['scope']!=grant['issuer_scope']):
        raise ControlError('control_resumption_review_changed')
    return record


def prepare(root: Path, payload: dict, *, control_manifest_sha256: str) -> dict:
    from ..parent_recovery import review
    from ..managed_restore import bound_journal
    fields = {'operation_id', 'review_id', 'expected_evidence_sha256', 'action',
              'issuer_scope', 'boot_id', 'stop_epoch'}
    resuming=isinstance(payload,dict) and 'resumption_plan_id' in payload
    if resuming:fields.add('resumption_plan_id')
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ControlError('control_parent_recovery_request_invalid')
    current = review(root, payload['operation_id'], control_manifest_sha256=control_manifest_sha256)
    if (payload['expected_evidence_sha256'] != current['evidence_sha256']
            or payload['action'] not in current['permitted_actions']
            or payload['stop_epoch'] != current['stop_epoch']):
        raise ControlError('control_recovery_review_stale')
    _, bound = bound_journal(root, payload['operation_id'])
    from .reader_support import require_parent_recovery_reader, require_parent_resumption_reader
    if resuming:
        if current['runtime_status']!='offline' or payload['action'] not in {'continue_recovery','rollback'}:
            raise ControlError('control_resumption_mode_unavailable')
        require_parent_resumption_reader(bound.execution)
    else:
        require_parent_recovery_reader(bound.execution)
    value = {'schema_version': 1, 'review_id': payload['review_id'],
        'installation_id': current['installation_id'], 'operation_id': current['operation_id'],
        'operation_type': 'restore', 'generation': current['generation'],
        'execution_sha256': current['execution_sha256'], 'parent_binding_sha256': binding_sha256(bound),
        'control_manifest_sha256': control_manifest_sha256,
        'source_evidence_sha256': current['evidence_sha256'],
        'canonical_evidence_identity': current['canonical_plan']['evidenceIdentity'],
        'action': payload['action'], 'issuer_scope': payload['issuer_scope'],
        'boot_id': payload['boot_id'], 'stop_epoch': payload['stop_epoch'],
        'expires_at_monotonic': time.monotonic()+900}
    if resuming:
        value.update(schema_version=2,resumption_plan_id=payload['resumption_plan_id'])
    require_live(value)
    if review(root, payload['operation_id'], control_manifest_sha256=control_manifest_sha256) != current:
        raise ControlError('control_recovery_review_stale')
    return value


def require_binding(grant: dict, bound) -> None:
    from .. import management_journal as contract
    parse_grant(grant)
    if (grant['installation_id'] != bound.installation_id or grant['operation_id'] != bound.operation_id
            or grant['operation_type'] != bound.operation_type
            or grant['parent_binding_sha256'] != binding_sha256(bound)
            or grant['execution_sha256'] != contract.execution_tuple_sha256(bound.execution)
            or grant['control_manifest_sha256'] != bound.execution.control_manifest_sha256
            or grant['generation'] < bound.original_generation
            or grant['schema_version'] not in {3,4} and grant['generation'] == bound.original_generation):
        raise ControlError('control_parent_recovery_request_changed')
