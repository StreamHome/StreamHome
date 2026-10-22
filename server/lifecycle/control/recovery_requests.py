"""Protected, generation-bound recovery grants for the original operation."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import math
import time

from .. import management_journal as contract, management_recovery_store as recovery, management_store, state
from ..execution_state import read_active_execution
from .requests import ManagementRequest, REQUEST_ROOT, parse_management_request
from . import requests as request_contract
from .rpc import ControlError
from .tickets import read_protected_json, read_stop_epoch

ROOT = Path('/opt/streamhome')


def recovery_path(operation_id: str, generation: int):
    contract._uuid(operation_id, 'recovery operation')
    contract._integer(generation, 2, 'recovery generation')
    return REQUEST_ROOT / operation_id / f'recovery-{generation}.json'


def parse_grant(payload: dict) -> dict:
    if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'recovery_review_id', 'source_evidence_sha256', 'source_request_sha256', 'action', 'request', 'expires_at_monotonic'}
            or type(payload['schema_version']) is not int or payload['schema_version'] != 1
            or payload['action'] not in recovery.ACTIONS):
        raise ControlError('control_recovery_request_invalid')
    expiry = payload['expires_at_monotonic']
    if type(expiry) not in {int, float} or not math.isfinite(expiry) or not 0 < expiry < 10**12:
        raise ControlError('control_recovery_request_invalid')
    contract._uuid(payload['recovery_review_id'], 'recovery review')
    for name in ('source_evidence_sha256', 'source_request_sha256'):
        contract._text(payload[name], contract._SHA256, name)
    current = parse_management_request(payload['request'])
    original = request_contract.read_management_request(current.operation_id)
    if original.sha256 != payload['source_request_sha256']:
        raise ControlError('control_recovery_request_changed')
    mutable = {'expected_generation', 'supervisor', 'backend', 'boot_id', 'stop_epoch', 'previous_boot_enabled', 'boot_enabled'}
    before, after = original.payload(), current.payload()
    reversing = payload['action'] in {'rollback', 'safe_abort'}
    if (any(before[key] != after[key] for key in before.keys() - mutable)
            or current.expected_generation <= original.expected_generation
            or current.supervisor_invocation_id != original.supervisor_invocation_id
            or current.activation_mode is contract.ActivationMode.CUTOVER and (
                current.initial_service_state != 'running' or current.backend is not None
                or current.boot_enabled is not (original.previous_boot_enabled if reversing else current.previous_boot_enabled))
            or current.activation_mode is not contract.ActivationMode.CUTOVER and (
                reversing
                or current.boot_enabled is not original.boot_enabled
                or current.supervisor != original.supervisor or current.backend != original.backend
                or current.initial_service_state == 'running' and current.backend is None)):
        raise ControlError('control_recovery_request_changed')
    return payload


def read_grant(operation_id: str, generation: int, *, require_live: bool = True) -> dict:
    grant = parse_grant(read_protected_json(recovery_path(operation_id, generation)))
    retained = REQUEST_ROOT / operation_id / ('recovery-review-' + grant['recovery_review_id'] + '.json')
    if read_protected_json(retained) != grant:
        raise ControlError('control_recovery_request_changed')
    reviewed = parse_management_request(grant['request'])
    if reviewed.operation_id != operation_id or reviewed.expected_generation + 1 != generation:
        raise ControlError('control_recovery_request_changed')
    if require_live and (time.monotonic() >= grant['expires_at_monotonic'] or reviewed.boot_id != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or reviewed.stop_epoch != read_stop_epoch()):
        raise ControlError('control_recovery_permission_expired')
    return grant


def effective_request(journal: contract.ManagementJournal, *, original: ManagementRequest | None = None, require_live: bool = True) -> ManagementRequest:
    original = request_contract.read_management_request(journal.operation_id) if original is None else original
    if journal.generation == original.expected_generation + 1:
        return original
    grant = read_grant(journal.operation_id, journal.generation, require_live=require_live)
    intent = recovery.inspect_takeover(ROOT, journal.operation_id, journal.generation)
    if (intent.review_id != grant['recovery_review_id'] or intent.action != grant['action']
            or intent.target_journal.generation != journal.generation
            or intent.target_journal.acceptance is not None and intent.target_journal.acceptance != journal.acceptance):
        raise ControlError('control_recovery_request_changed')
    initial = replace(journal, stage=contract.ManagementStage.RECOVERY_REQUIRED, acceptance=intent.target_journal.acceptance)
    if initial != intent.target_journal:
        raise ControlError('control_recovery_request_changed')
    return parse_management_request(grant['request'])


def review(operation_id: str, *, control_manifest_sha256: str) -> dict:
    root = ROOT
    original = request_contract.read_management_request(operation_id)
    authority = state.inspect_state(root)
    plan = recovery.inspect_recovery(root, operation_id, authority.generation)
    journal = management_store.inspect_management_journal(root, operation_id)
    if (original.activation_mode is contract.ActivationMode.CUTOVER and original.initial_service_state != 'running'
            or original.previous != journal.previous or original.candidate != journal.candidate
            or original.review_id != journal.review_id
            or original.action is not journal.action or original.activation_mode is not journal.activation_mode
            or original.installation_id != journal.installation_id
            or original.initial_service_state != journal.initial_service_state
            or original.supervisor_invocation_id != journal.supervisor_invocation_id
            or original.controller_control_manifest_sha256 != control_manifest_sha256):
        raise ControlError('control_recovery_mode_unavailable')
    active = read_active_execution(root)
    effective, _ = recovery._effective_journal(root, authority, journal)
    selected = contract.selected_execution(effective)
    if (active.installation_id != journal.installation_id
            or active.execution not in {journal.previous, journal.candidate}
            or journal.acceptance is None and active.execution != journal.previous
            or effective.recovery_target == 'previous' and active.execution != selected):
        raise ControlError('control_recovery_execution_changed')
    return {**plan, 'installation_id': journal.installation_id,
            'activation_mode': original.activation_mode.value,
            'initial_service_state': original.initial_service_state,
            'boot_enabled': original.boot_enabled,
            'previous_boot_enabled': original.previous_boot_enabled,
            'recovery_target': effective.recovery_target,
            'supervisor_invocation_id': original.supervisor_invocation_id,
            'source_request_sha256': original.sha256,
            'control_manifest_sha256': control_manifest_sha256}


def prepare(payload: dict, *, control_manifest_sha256: str) -> dict:
    from .requests import _process
    fields = {'operation_id', 'expected_evidence_sha256', 'recovery_review_id', 'processes', 'boot_enabled', 'boot_id', 'stop_epoch'}
    if (not isinstance(payload, dict) or set(payload) not in (fields, fields | {'action'})
            or type(payload['boot_enabled']) is not bool):
        raise ControlError('control_recovery_request_invalid')
    snapshot = review(payload['operation_id'], control_manifest_sha256=control_manifest_sha256)
    processes = payload['processes']
    action = payload.get('action', 'continue_recovery')
    if (action not in snapshot.get('permitted_actions', ['continue_recovery'])
            or snapshot['evidence_sha256'] != payload['expected_evidence_sha256']
            or not isinstance(processes, dict) or set(processes) != {'initial_service_state', 'supervisor_invocation_id', 'supervisor', 'backend'}
            or processes['initial_service_state'] != snapshot['initial_service_state']
            or processes['supervisor_invocation_id'] != snapshot['supervisor_invocation_id']):
        raise ControlError('control_recovery_review_stale')
    original = request_contract.read_management_request(payload['operation_id'])
    if action in {'rollback', 'safe_abort'} or snapshot.get('recovery_target') == 'previous':
        from .reader_support import require_previous_reader
        require_previous_reader(original.previous)
    preserving = original.activation_mode is not contract.ActivationMode.CUTOVER
    target_boot = (original.previous_boot_enabled if action in {'rollback', 'safe_abort'}
                   else original.boot_enabled if preserving else payload['boot_enabled'])
    current = replace(original, expected_generation=snapshot['generation'], supervisor=_process(processes['supervisor']),
                      backend=_process(processes['backend']) if preserving else None,
                      previous_boot_enabled=payload['boot_enabled'], boot_enabled=target_boot,
                      boot_id=payload['boot_id'], stop_epoch=payload['stop_epoch'])
    value = {'schema_version': 1, 'recovery_review_id': payload['recovery_review_id'],
             'source_evidence_sha256': snapshot['evidence_sha256'], 'source_request_sha256': original.sha256,
             'action': action, 'request': current.payload(), 'expires_at_monotonic': time.monotonic() + 900}
    return parse_grant(value)
