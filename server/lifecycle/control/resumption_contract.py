"""Finite OS launch permissions; never canonical ownership or application state.

This data contract is shared by the privileged issuer and independent UID reader.
It contains only reviewed identities/digests, never commands, paths or credentials.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid

from .rpc import ControlError


def _invalid():
    raise ControlError('control_resumption_plan_invalid')


def _uuid(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        _invalid()
    if parsed is None or not parsed.int or str(parsed) != value:
        _invalid()


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        _invalid()


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def parse_plan(value: dict) -> dict:
    fields = {'schema_version', 'plan_id', 'installation_id', 'operation_id', 'review_id', 'owner_type',
              'action', 'generation', 'boot_id', 'stop_epoch', 'control_manifest_sha256',
              'issuer_sha256', 'expires_at_monotonic', 'prior_running', 'interruption_approved', 'resumption_approved', 'stages'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['prior_running'] is not True
            or value['interruption_approved'] is not True or value['resumption_approved'] is not True
            or not isinstance(value['owner_type'], str) or value['owner_type'] not in {'management', 'update', 'restore'}
            or not isinstance(value['action'], str) or value['action'] not in {'configure_apply', 'repair_environment', 'provision_control', 'repair_bound_environment'}
            or value['action'] != 'repair_bound_environment' and value['owner_type'] != 'management'
            or type(value['generation']) is not int or not 1 <= value['generation'] < 2**63):
        _invalid()
    for name in ('plan_id', 'installation_id', 'operation_id', 'review_id', 'boot_id'):
        _uuid(value[name])
    for name in ('stop_epoch', 'control_manifest_sha256', 'issuer_sha256'):
        _digest(value[name])
    expiry = value['expires_at_monotonic']
    if type(expiry) not in {int, float} or not math.isfinite(expiry) or not 0 < expiry < 10**12:
        _invalid()
    stages = value['stages']
    if not isinstance(stages, list) or len(stages) not in {1, 2}:
        _invalid()
    for index, stage in enumerate(stages):
        if (not isinstance(stage, dict) or set(stage) != {'kind', 'execution_sha256', 'generation', 'handoff_sha256'}
                or stage['kind'] != ('candidate_start' if index == 0 else 'previous_start')
                or type(stage['generation']) is not int
                or not 1 <= stage['generation'] < 2**63
                or stage['generation'] not in {value['generation'], value['generation'] + 1}):
            _invalid()
        _digest(stage['execution_sha256'])
        if stage['generation'] == value['generation']:
            if stage['handoff_sha256'] is not None:
                _invalid()
        else:
            _digest(stage['handoff_sha256'])
    if stages[0]['generation'] != value['generation'] or len(stages) == 2 and stages[0]['execution_sha256'] == stages[1]['execution_sha256']:
        _invalid()
    return value


def authorize_stage(plan: dict, kind: str, observation: dict, *, boot_id: str, stop_epoch: str,
                    issuer_sha256: str, now: float, candidate_consumed: bool) -> dict:
    """Match a separately validated canonical observation to one reviewed stage.

    The caller must prove the live issuer and canonical/controller observation.
    This function performs no I/O, grants no start by itself and cannot substitute
    a stored receipt for those live checks.
    """
    parse_plan(plan)
    fields = {'installation_id', 'operation_id', 'owner_type', 'action', 'generation',
              'execution_sha256', 'handoff_sha256', 'accepted', 'candidate_failed', 'previous_restored'}
    if (not isinstance(observation, dict) or set(observation) != fields
            or any(type(observation[name]) is not bool for name in ('accepted', 'candidate_failed', 'previous_restored'))
            or type(observation['generation']) is not int or type(candidate_consumed) is not bool
            or type(now) not in {int, float} or not math.isfinite(now) or now < 0
            or now >= plan['expires_at_monotonic'] or boot_id != plan['boot_id']
            or stop_epoch != plan['stop_epoch'] or issuer_sha256 != plan['issuer_sha256']
            or any(observation[name] != plan[name] for name in ('installation_id', 'operation_id', 'owner_type', 'action'))):
        raise ControlError('control_resumption_review_changed')
    stage = next((stage for stage in plan['stages'] if stage['kind'] == kind), None)
    if (stage is None or any(observation[name] != stage[name] for name in ('generation', 'execution_sha256', 'handoff_sha256'))
            or kind == 'candidate_start' and (candidate_consumed or observation['candidate_failed'] or observation['previous_restored'])
            or kind == 'previous_start' and (not candidate_consumed or observation['accepted']
                or not observation['candidate_failed'] or not observation['previous_restored'])):
        raise ControlError('control_resumption_stage_unavailable')
    return stage


def parse_receipt(value: dict, plan: dict) -> dict:
    """A consumed intent is never another permission, even without a reply."""
    parse_plan(plan)
    fields = {'schema_version', 'plan_sha256', 'stage_sha256', 'kind', 'nonce', 'status', 'invocation'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['plan_sha256'] != digest(plan)
            or not isinstance(value['kind'], str) or not isinstance(value['status'], str)
            or value['status'] not in {'consumed', 'bound'}):
        raise ControlError('control_resumption_receipt_invalid')
    _digest(value['nonce'])
    stage = next((stage for stage in plan['stages'] if stage['kind'] == value['kind']), None)
    if stage is None or value['stage_sha256'] != digest(stage):
        raise ControlError('control_resumption_receipt_invalid')
    invocation = value['invocation']
    if (value['status'] == 'consumed' and invocation is not None
            or value['status'] == 'bound' and (not isinstance(invocation, str)
                or not re.fullmatch(r'[0-9a-f]{32}', invocation) or not int(invocation, 16))):
        raise ControlError('control_resumption_receipt_invalid')
    return value


def consume(plan: dict, stage: dict, nonce: str, *, existing: dict | None) -> dict:
    """Create the durable launch intent before any fixed OS start is dispatched."""
    parse_plan(plan)
    if existing is not None:
        # A timeout/lost reply cannot reset a consumed intent or its invocation.
        raise ControlError('control_resumption_already_consumed')
    if stage not in plan['stages']:
        raise ControlError('control_resumption_stage_unavailable')
    return parse_receipt({'schema_version': 1, 'plan_sha256': digest(plan), 'stage_sha256': digest(stage),
        'kind': stage['kind'], 'nonce': nonce, 'status': 'consumed', 'invocation': None}, plan)


def bind_invocation(plan: dict, receipt: dict, invocation: str) -> dict:
    """Only ExecStartPre can bind the one newly assigned systemd invocation."""
    parse_receipt(receipt, plan)
    if receipt['status'] != 'consumed':
        raise ControlError('control_resumption_already_consumed')
    return parse_receipt({**receipt, 'status': 'bound', 'invocation': invocation}, plan)


def start_intent(plan: dict, receipt: dict) -> dict:
    parse_receipt(receipt, plan)
    return {'schema_version': 1, 'plan_id': plan['plan_id'], 'kind': receipt['kind'],
            'plan_sha256': receipt['plan_sha256'], 'stage_sha256': receipt['stage_sha256'], 'nonce': receipt['nonce']}


def parse_start_intent(value: dict) -> dict:
    fields = {'schema_version', 'plan_id', 'kind', 'plan_sha256', 'stage_sha256', 'nonce'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['kind'] not in ('candidate_start', 'previous_start')):
        raise ControlError('control_resumption_intent_invalid')
    _uuid(value['plan_id'])
    for name in ('plan_sha256', 'stage_sha256', 'nonce'):
        _digest(value[name])
    return value


def require_start_intent(value: dict, plan: dict, receipt: dict) -> None:
    if parse_start_intent(value) != start_intent(plan, receipt):
        raise ControlError('control_resumption_intent_changed')
