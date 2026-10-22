"""Review one bounded continuation of the original MANAGEMENT operation."""
from __future__ import annotations

import os
import json
from pathlib import Path
import stat
import uuid

from ..configure_wizard import parse_request_json
from ..terminal import open_console
from .administration import (_boot_enabled, _identity, operation_unit, process_snapshot,
                             root_stop_epoch, run_operation)
from .privileged import require_root, run_worker
from .rpc import ControlError


def _consent(path: Path, snapshot: dict) -> bool:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32768:
            raise ControlError('control_request_invalid')
        value = parse_request_json(handle.read(32769))
    if (set(value) != {'schema_version', 'operation_id', 'expected_generation', 'expected_evidence_sha256', 'continue_recovery', 'interruption_approved'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['expected_generation']) is not int
            or any(type(value[key]) is not bool for key in ('continue_recovery', 'interruption_approved'))):
        raise ControlError('control_request_invalid')
    if (value['operation_id'] != snapshot['operation_id'] or value['expected_generation'] != snapshot['generation']
            or value['expected_evidence_sha256'] != snapshot['evidence_sha256']):
        raise ControlError('control_recovery_review_stale')
    return value['continue_recovery'] and value['interruption_approved']


def _preserved_processes(snapshot: dict) -> dict:
    preserving = snapshot['activation_mode'] != 'cutover'
    processes = process_snapshot(preserving_recovery=True) if preserving else process_snapshot()
    if (processes['initial_service_state'] != snapshot['initial_service_state']
            or processes['supervisor_invocation_id'] != snapshot['supervisor_invocation_id']):
        raise ControlError('control_recovery_supervisor_required')
    if preserving and snapshot['initial_service_state'] == 'running' and processes['backend'] is None:
        raise ControlError('control_runtime_not_settled')
    if snapshot['initial_service_state'] == 'stopped':
        from .privileged import systemctl, unit_snapshot
        from .stop import _require_empty
        unit = unit_snapshot()
        if (unit['ActiveState'] not in {'inactive', 'failed'} or int(unit['MainPID']) != 0
                or systemctl('show', 'streamhome.service', '--property=Job') not in {'Job=', 'Job=0'}):
            raise ControlError('control_recovery_supervisor_required')
        _require_empty('streamhome.service')
    return processes


def recover(operation_id: str, *, request_path: Path | None, control_manifest_sha256: str, review_only: bool = False) -> int:
    require_root()
    _identity(operation_id)
    unit = operation_unit(operation_id)
    if unit['ActiveState'] not in {'inactive', 'failed'} or int(unit['MainPID']) != 0:
        raise ControlError('control_controller_busy')
    from .stop import _require_empty
    _require_empty(f'streamhome-operation@{operation_id}.service')
    epoch = root_stop_epoch()
    snapshot = run_worker(['recovery-review', operation_id], timeout=120)
    processes = _preserved_processes(snapshot)
    if review_only:
        print(json.dumps(snapshot, sort_keys=True, allow_nan=False))
        return 0
    if request_path is not None:
        approved = _consent(request_path, snapshot)
    else:
        with open_console() as console:
            console.write('StreamHome operation recovery')
            console.write('Operation: ' + operation_id)
            console.write('Generation: ' + str(snapshot['generation']))
            console.write('Recovery review SHA-256: ' + snapshot['evidence_sha256'])
            if snapshot['activation_mode'] == 'cutover':
                console.write('The recorded application and configuration will be verified and continued.')
                console.write('Owned application processes will be interrupted. Accepted changes remain accepted.')
            else:
                console.write('The recorded settings and boot preference will be completed without restarting the application.')
                console.write('Start at boot: ' + ('enabled' if snapshot['boot_enabled'] else 'disabled'))
            approved = console.confirm('Continue this operation?')
    if not approved:
        print('Operation recovery was declined.')
        return 0
    if root_stop_epoch() != epoch or _preserved_processes(snapshot) != processes:
        raise ControlError('control_operator_stopped')
    fresh = run_worker(['recovery-review', operation_id], timeout=120)
    if fresh != snapshot:
        raise ControlError('control_recovery_review_stale')
    grant = run_worker(['prepare-recovery'], payload={'operation_id': operation_id,
        'expected_evidence_sha256': snapshot['evidence_sha256'], 'recovery_review_id': str(uuid.uuid4()),
        'processes': processes, 'boot_enabled': _boot_enabled(),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'stop_epoch': epoch})
    if (grant['request']['controller_control_manifest_sha256'] != control_manifest_sha256
            or root_stop_epoch() != epoch):
        raise ControlError('control_recovery_review_stale')
    run_operation(grant['request'], recovery_grant=grant)
    print('StreamHome operation recovery completed.')
    return 0
