"""Fixed OS start/ExecStartPre bridge for one reviewed MANAGEMENT resumption."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .administration import OPERATIONS, _verify_owned_units, operation_unit
from .administration_lock import process_identity
from .notification import STARTUP_RECOVERY_SECONDS
from .privileged import descriptor, require_root, run_worker, supervisor_snapshot, systemctl, unit_snapshot
from .processes import RUNTIME_CGROUP, process_cgroup
from . import resumption_contract as contract, resumption_store as store
from .rpc import ControlError
from .tickets import read_protected_json


def qualify(grant: dict, *, control_manifest_sha256: str) -> dict:
    require_root()
    if (not isinstance(grant, dict) or type(grant.get('schema_version')) is not int or grant['schema_version'] != 3
            or not isinstance(grant.get('request'), dict)):
        raise ControlError('control_resumption_review_changed')
    value = store.live_state(grant.get('resumption_plan_id'), 'candidate_start', control_manifest_sha256=control_manifest_sha256)
    plan, request = value['plan'], grant['request']
    if (plan['owner_type'] != 'management' or plan['action'] == 'repair_bound_environment'
            or plan['operation_id'] != request.get('operation_id') or plan['installation_id'] != request.get('installation_id')
            or type(request.get('expected_generation')) is not int or plan['generation'] != request['expected_generation'] + 1
            or plan['review_id'] != grant.get('recovery_review_id') or plan['action'] != request.get('action')
            or request.get('initial_service_state') != 'running' or request.get('activation_mode') != 'cutover'
            or request.get('controller_control_manifest_sha256') != control_manifest_sha256
            or grant.get('issuer_scope') != value['issuer']['scope']):
        raise ControlError('control_resumption_review_changed')
    return value


def _observe(plan: dict, *, startup_hook: dict | None = None) -> dict:
    actor = operation_unit(plan['operation_id'])
    if actor['ActiveState'] != 'active' or int(actor['MainPID']) <= 0:
        raise ControlError('control_controller_changed')
    payload = {'plan': plan, 'kind': 'candidate_start', 'controller_pid': int(actor['MainPID']),
               'controller_invocation_id': actor['InvocationID']}
    if startup_hook is not None:
        payload['startup_hook'] = startup_hook
    return run_worker(['resumption-observe'], payload=payload, timeout=120)


def _observed_unit(receipt: dict) -> bool:
    if receipt['status'] != 'bound':
        raise ControlError('control_resumption_outcome_unknown')
    unit = unit_snapshot()
    if unit['InvocationID'] != receipt['invocation'] or unit['ActiveState'] not in {'active', 'activating'}:
        raise ControlError('control_resumption_outcome_unknown')
    if int(unit['MainPID']) <= 0:
        return False
    if process_cgroup(int(unit['MainPID'])) != RUNTIME_CGROUP:
        raise ControlError('control_incarnation_changed')
    value = supervisor_snapshot(descriptor(), unit)
    if value.get('launches_open') is not True:
        raise ControlError('control_operator_stopped')
    return True


def start(grant: dict, *, control_manifest_sha256: str) -> bool:
    value = qualify(grant, control_manifest_sha256=control_manifest_sha256)
    plan = value['plan']
    if value['issuer']['owner']['pid'] != os.getpid():
        raise ControlError('control_resumption_issuer_changed')
    if value['receipt'] is not None:
        # Reconciliation observes only this bound invocation; no branch retries
        # a consumed start, including when the original OS reply was lost.
        return _observed_unit(value['receipt'])
    _verify_owned_units()
    observed = _observe(plan)
    store.consume_stage(plan['plan_id'], 'candidate_start', observed, control_manifest_sha256=control_manifest_sha256)
    qualify(grant, control_manifest_sha256=control_manifest_sha256)
    try:
        systemctl('start', 'streamhome.service', timeout=STARTUP_RECOVERY_SECONDS + 30)
    except (ControlError, subprocess.TimeoutExpired) as exc:
        raise ControlError('control_resumption_outcome_unknown') from exc
    current = store.live_state(plan['plan_id'], 'candidate_start', control_manifest_sha256=control_manifest_sha256)
    if current['receipt'] is None:
        raise ControlError('control_resumption_outcome_unknown')
    return _observed_unit(current['receipt'])


def before_start(*, control_manifest_sha256: str) -> None:
    require_root()
    if Path('/proc/self/cgroup').read_text().splitlines() != ['0::' + RUNTIME_CGROUP]:
        raise ControlError('control_unit_hook_forbidden')
    intent = contract.parse_start_intent(read_protected_json(store.START_INTENT))
    if intent['kind'] != 'candidate_start':
        raise ControlError('control_resumption_mode_unavailable')
    value = store.live_state(intent['plan_id'], intent['kind'], control_manifest_sha256=control_manifest_sha256)
    plan = value['plan']
    grant = read_protected_json(OPERATIONS / plan['operation_id'] / f"recovery-{plan['generation']}.json")
    qualify(grant, control_manifest_sha256=control_manifest_sha256)
    if value['receipt'] is None or value['receipt']['status'] != 'consumed':
        raise ControlError('control_resumption_already_consumed')
    contract.require_start_intent(intent, plan, value['receipt'])
    _verify_owned_units()
    start, status = process_identity(os.getpid())
    if status in {'Z', 'X'}:
        raise ControlError('control_unit_hook_forbidden')
    _observe(plan, startup_hook={'pid': os.getpid(), 'start_identity': start})
    store.bind_current_start(control_manifest_sha256=control_manifest_sha256)
