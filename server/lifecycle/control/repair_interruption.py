"""Fixed whole-service stop after an independent repair candidate is complete."""
from __future__ import annotations

import os
from pathlib import Path
import time

from .administration import OPERATIONS, _identity, _sha, _verify_owned_units, operation_unit, root_stop_epoch
from .administration_lock import process_identity
from .privileged import descriptor, require_root, supervisor_snapshot, systemctl, unit_snapshot
from .processes import RUNTIME_CGROUP, process_cgroup
from .root_files import atomic_root_json
from .rpc import ControlError
from .stopped_repair import require_scope
from .tickets import read_protected_json


def stop_prepared(payload: dict, observation: dict) -> None:
    require_root()
    operation = _identity(payload.get('operation_id'))
    directory = OPERATIONS / operation
    selected = descriptor()
    grant = read_protected_json(Path('/etc/streamhome/control/preparations') / operation / 'request.json')
    if (payload.get('action') != 'repair_environment' or payload.get('activation_mode') != 'cutover'
            or payload.get('initial_service_state') != 'running' or payload.get('backend') is not None
            or payload.get('interruption_approved') is not True or payload.get('installation_id') != selected.installation_id
            or read_protected_json(directory / 'request.json') != payload
            or grant.get('schema_version') != 3 or grant.get('preparation_id') != operation
            or any(grant.get(name) != payload.get(name) for name in ('installation_id', 'expected_generation',
                'previous', 'supervisor', 'supervisor_invocation_id', 'boot_id', 'stop_epoch'))
            or grant.get('control_manifest_sha256') != payload.get('controller_control_manifest_sha256')
            or grant.get('backend') is not None or observation.get('stage') != 'prepared'
            or time.monotonic() >= grant.get('expires_at_monotonic', 0)
            or observation.get('request_sha256') != _sha(payload)
            or observation.get('generation') != payload['expected_generation'] + 1
            or root_stop_epoch() != payload['stop_epoch']
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != payload['boot_id']):
        raise ControlError('control_repair_interruption_changed')
    scope = grant.get('issuer_scope')
    _stop_owned(payload,observation,scope,selected,marker='repair-stop.json')


def stop_control_prepared(payload: dict, observation: dict) -> None:
    """Stop only after the original control scope and signed preparation verify."""
    from .control_activation import prepared_for
    from .artifact_stage import require_prepared
    from .control_permission import require_initial
    require_root()
    selected=descriptor()
    if (payload.get('action') != 'provision_control' or payload.get('activation_mode') != 'cutover'
            or payload.get('initial_service_state') != 'running'):
        raise ControlError('control_repair_interruption_changed')
    permission=require_initial(payload)
    prepared=prepared_for(payload);require_prepared(prepared)
    candidate=payload.get('candidate',{})
    if (candidate.get('control_bundle_id') != prepared.bundle_id
            or candidate.get('control_manifest_sha256') != prepared.manifest_sha256):
        raise ControlError('control_prepared_version_changed')
    _stop_owned(payload,observation,permission['issuer']['scope'],selected,marker='control-stop.json',
        before_stop=lambda:require_initial(payload))


def _stop_owned(payload,observation,scope,selected,*,marker,before_stop=None):
    """Shared exact unit/kernel proof; wrappers provide distinct root permissions."""
    require_root()
    operation=_identity(payload.get('operation_id'));directory=OPERATIONS/operation
    if (marker not in {'repair-stop.json','control-stop.json'} or payload.get('initial_service_state') != 'running'
            or payload.get('interruption_approved') is not True or payload.get('installation_id') != selected.installation_id
            or read_protected_json(directory/'request.json') != payload
            or observation.get('stage') != 'prepared' or observation.get('request_sha256') != _sha(payload)
            or observation.get('generation') != payload['expected_generation']+1
            or root_stop_epoch() != payload['stop_epoch']
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != payload['boot_id']):
        raise ControlError('control_repair_interruption_changed')
    require_scope(scope, selected.installation_id)
    if scope['pid'] != os.getpid():
        raise ControlError('control_repair_interruption_changed')
    actor = operation_unit(operation)
    if (actor['ActiveState'] != 'active' or int(actor['MainPID']) != observation.get('controller_pid')
            or actor['InvocationID'] != observation.get('controller_invocation_id')):
        raise ControlError('control_controller_changed')
    unit = unit_snapshot()
    peer = payload['supervisor']
    start, status = process_identity(peer['pid'])
    if (unit['ActiveState'] not in {'active', 'activating'} or int(unit['MainPID']) != peer['pid']
            or unit['InvocationID'] != payload['supervisor_invocation_id'] or start != peer['start_identity']
            or status in {'Z', 'X'} or process_cgroup(peer['pid']) != RUNTIME_CGROUP
            or Path(f"/proc/{peer['pid']}").stat().st_uid != selected.service_uid
            or systemctl('show', 'streamhome.service', '--property=Job') not in {'Job=', 'Job=0'}):
        raise ControlError('control_incarnation_changed')
    supervisor = supervisor_snapshot(selected, unit)
    if supervisor.get('launches_open') is not True or supervisor.get('start_identity') != start:
        raise ControlError('control_operator_stopped')
    receipt = read_protected_json(directory / 'os-receipt.json')
    if (receipt.get('stage') != 'complete' or receipt.get('request_sha256') != _sha(payload)
            or receipt.get('generation') != observation['generation']
            or receipt.get('controller_invocation_id') != actor['InvocationID']):
        raise ControlError('control_os_receipt_changed')
    _verify_owned_units()
    path = directory / marker
    if path.exists() or path.is_symlink():
        raise ControlError('control_repair_interruption_consumed')
    # This receipt authorizes no start. Stop propagation cancels this issuer
    # and controller; only a new post-stop owner review can continue the work.
    atomic_root_json(path, {'schema_version': 1, 'operation_id': operation,
        'request_sha256': _sha(payload), 'generation': observation['generation'],
        'issuer_scope': scope, 'supervisor_invocation_id': unit['InvocationID']}, mode=0o600)
    require_scope(scope, selected.installation_id)
    if root_stop_epoch() != payload['stop_epoch']:
        raise ControlError('control_operator_stopped')
    if before_stop is not None:before_stop()
    systemctl('stop', 'streamhome.service', timeout=90)
    raise ControlError('control_operation_recovery_required')
