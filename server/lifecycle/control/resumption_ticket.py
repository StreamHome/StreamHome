"""Read a bound OS start identity without importing canonical or app state.

This ticket is subordinate to the reader's separate canonical startup decision.
It proves one systemd invocation, never application admission or another start.
"""
from __future__ import annotations

from pathlib import Path
import time

from .administration_lock import parse_owner
from . import resumption_contract as contract
from .resumption_store import ROOT, START_INTENT
from .rpc import ControlError
from .stopped_repair import parse_scope, require_scope
from .tickets import read_protected_json, read_stop_epoch


def reviewed_plan(plan_id: str, *, installation_id: str, control_manifest_sha256: str) -> dict:
    contract._uuid(plan_id)
    directory = ROOT / plan_id
    record = read_protected_json(directory / 'plan.json')
    if not isinstance(record, dict) or set(record) != {'plan', 'issuer'}:
        raise ControlError('control_resumption_plan_invalid')
    plan = contract.parse_plan(record['plan'])
    evidence = record['issuer']
    if not isinstance(evidence, dict) or set(evidence) != {'owner', 'scope'}:
        raise ControlError('control_resumption_issuer_changed')
    owner = parse_owner(evidence['owner'])
    scope = parse_scope(evidence['scope'], installation_id)
    if (plan['plan_id'] != plan_id or plan['installation_id'] != installation_id
            or plan['control_manifest_sha256'] != control_manifest_sha256
            or plan['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or plan['stop_epoch'] != read_stop_epoch() or time.monotonic() >= plan['expires_at_monotonic']
            or contract.digest(evidence) != plan['issuer_sha256'] or owner['status'] != 'active'
            or owner['boot_id'] != plan['boot_id'] or owner['pid'] != scope['pid']
            or owner['start_identity'] != scope['start_identity']):
        raise ControlError('control_resumption_review_changed')
    require_scope(scope, installation_id)
    return record


def bound_ticket(plan_id: str, kind: str, invocation: str, *, installation_id: str,
                 control_manifest_sha256: str) -> dict:
    if kind not in ('candidate_start', 'previous_start'):
        raise ControlError('control_resumption_stage_unavailable')
    record = reviewed_plan(plan_id, installation_id=installation_id, control_manifest_sha256=control_manifest_sha256)
    plan, evidence = record['plan'], record['issuer']
    directory = ROOT / plan_id
    receipt = contract.parse_receipt(read_protected_json(directory / (kind + '.json')), plan)
    if receipt['status'] != 'bound' or receipt['kind'] != kind or receipt['invocation'] != invocation:
        raise ControlError('control_resumption_invocation_changed')
    require_scope(evidence['scope'], installation_id)
    return {'plan': plan, 'receipt': receipt}


def current_ticket(invocation: str, *, installation_id: str, control_manifest_sha256: str) -> dict:
    intent = contract.parse_start_intent(read_protected_json(START_INTENT))
    value = bound_ticket(intent['plan_id'], intent['kind'], invocation,
        installation_id=installation_id, control_manifest_sha256=control_manifest_sha256)
    contract.require_start_intent(intent, value['plan'], value['receipt'])
    return value
