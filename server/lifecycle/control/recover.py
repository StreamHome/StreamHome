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


def _consent(path: Path, snapshot: dict, *, action: str = 'continue_recovery') -> bool:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32768:
            raise ControlError('control_request_invalid')
        value = parse_request_json(handle.read(32769))
    fields = {'schema_version', 'operation_id', 'expected_generation', 'expected_evidence_sha256', 'continue_recovery', 'interruption_approved'}
    if (type(value.get('schema_version')) is not int or value['schema_version'] not in {1, 2}
            or set(value) != (fields if value['schema_version'] == 1 else fields | {'action'})
            or value['schema_version'] == 1 and action != 'continue_recovery'
            or value['schema_version'] == 2 and value['action'] != action
            or type(value['expected_generation']) is not int
            or any(type(value[key]) is not bool for key in ('continue_recovery', 'interruption_approved'))):
        raise ControlError('control_request_invalid')
    if (value['operation_id'] != snapshot['operation_id'] or value['expected_generation'] != snapshot['generation']
            or value['expected_evidence_sha256'] != snapshot['evidence_sha256']):
        raise ControlError('control_recovery_review_stale')
    return value['continue_recovery'] and value['interruption_approved']


def _preserved_processes(snapshot: dict, *, resuming: bool = False) -> dict:
    preserving = snapshot['activation_mode'] != 'cutover'
    processes = process_snapshot(preserving_recovery=True) if preserving else process_snapshot()
    if resuming:
        if snapshot['initial_service_state'] != 'running' or preserving or processes['initial_service_state'] != 'stopped':
            raise ControlError('control_resumption_mode_unavailable')
        from .stopped_repair import require_stopped
        require_stopped()
        return processes
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


def recover(operation_id: str, *, request_path: Path | None, control_manifest_sha256: str, review_only: bool = False,
            action: str = 'continue_recovery', scope_epoch: str | None = None,
            expected_installation_id: str | None = None):
    require_root()
    _identity(operation_id)
    unit = operation_unit(operation_id)
    if unit['ActiveState'] not in {'inactive', 'failed'} or int(unit['MainPID']) != 0:
        raise ControlError('control_controller_busy')
    from .stop import _require_empty
    _require_empty(f'streamhome-operation@{operation_id}.service')
    epoch = root_stop_epoch()
    if scope_epoch is not None and epoch != scope_epoch:
        raise ControlError('control_operator_stopped')
    snapshot = run_worker(['recovery-review', operation_id], timeout=120)
    if expected_installation_id is not None and snapshot['installation_id'] != expected_installation_id:
        raise ControlError('control_recovery_review_stale')
    if action not in snapshot['permitted_actions']:
        raise ControlError('control_recovery_action_unavailable')
    from .privileged import unit_snapshot
    runtime = unit_snapshot()
    resuming = (snapshot['initial_service_state'] == 'running' and snapshot['activation_mode'] == 'cutover'
                and runtime['ActiveState'] in {'inactive', 'failed'} and int(runtime['MainPID']) == 0)
    processes = _preserved_processes(snapshot, resuming=resuming)
    if review_only:
        print(json.dumps(snapshot, sort_keys=True, allow_nan=False))
        return 0
    issuer_scope = None
    if snapshot['initial_service_state'] == 'stopped' or resuming:
        from .stopped_repair import StoppedRepairHandoff, current_scope
        if scope_epoch is None:
            return StoppedRepairHandoff(snapshot['installation_id'], operation_id, epoch, request_path, action)
        issuer_scope = current_scope(snapshot['installation_id'], epoch)
    resumption = None
    if resuming:
        from . import resumption_review
        resumption = resumption_review.make_review(snapshot, action, control_manifest_sha256=control_manifest_sha256)
        approved = resumption_review.consent(resumption, request_path)
    elif request_path is not None:
        approved = _consent(request_path, snapshot, action=action)
    else:
        with open_console() as console:
            console.write('StreamHome operation recovery')
            console.write('Operation: ' + operation_id)
            console.write('Generation: ' + str(snapshot['generation']))
            console.write('Recovery review SHA-256: ' + snapshot['evidence_sha256'])
            if snapshot['initial_service_state'] == 'stopped':
                console.write('The recorded inactive runtime will be verified. The service will stay stopped.')
                console.write('Start at boot: ' + ('enabled' if (snapshot['previous_boot_enabled']
                    if action in {'rollback', 'safe_abort'} else snapshot['boot_enabled']) else 'disabled'))
            elif action in {'rollback', 'safe_abort'}:
                console.write('The previous recorded runtime will be started under guarded verification.')
                console.write('Owned application processes will be interrupted. User database writes are preserved.')
                console.write('Start at boot: ' + ('enabled' if snapshot['previous_boot_enabled'] else 'disabled'))
            elif snapshot.get('recovery_target') == 'previous':
                console.write('The previously selected runtime will continue under guarded verification.')
                console.write('Owned application processes will be interrupted. The recorded previous-runtime selection is retained.')
            elif snapshot['activation_mode'] == 'cutover':
                console.write('The recorded application and configuration will be verified and continued.')
                console.write('Owned application processes will be interrupted. Accepted changes remain accepted.')
            else:
                console.write('The recorded settings and boot preference will be completed without restarting the application.')
                console.write('Start at boot: ' + ('enabled' if snapshot['boot_enabled'] else 'disabled'))
            approved = console.confirm('Continue this operation?')
    if not approved:
        print('Operation recovery was declined.')
        return 0
    if root_stop_epoch() != epoch or _preserved_processes(snapshot, resuming=resuming) != processes:
        raise ControlError('control_operator_stopped')
    fresh = run_worker(['recovery-review', operation_id], timeout=120)
    if fresh != snapshot:
        raise ControlError('control_recovery_review_stale')
    payload = {'operation_id': operation_id,
        'expected_evidence_sha256': snapshot['evidence_sha256'], 'recovery_review_id': str(uuid.uuid4()),
        'action': action,
        'processes': processes, 'boot_enabled': _boot_enabled(),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'stop_epoch': epoch}
    if issuer_scope is not None:
        payload['issuer_scope'] = issuer_scope
    if resumption is not None:
        plan = resumption_review.launch_plan(resumption, approved=True)
        payload.update(resumption_plan_id=plan['plan_id'], recovery_review_id=plan['review_id'])
    grant = run_worker(['prepare-recovery'], payload=payload)
    if (grant['request']['controller_control_manifest_sha256'] != control_manifest_sha256
            or root_stop_epoch() != epoch):
        raise ControlError('control_recovery_review_stale')
    if resumption is not None:
        from .resumption_store import publish
        publish(plan, resumption.issuer, control_manifest_sha256=control_manifest_sha256)
    run_operation(grant['request'], recovery_grant=grant)
    print('StreamHome operation recovery completed.')
    return 0
