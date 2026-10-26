"""Root OS custody for the original managed UPDATE continuation.

Only protected metadata and OS state are interpreted here. Canonical evidence,
permissions and outcomes are read by the existing service-UID workers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

from .administration import operation_unit, process_snapshot, root_stop_epoch
from .privileged import descriptor, require_root, run_worker, systemctl
from .root_files import atomic_root_json, root_directory
from .rpc import ControlError
from .stopped_repair import StoppedRepairHandoff, current_scope, require_stopped


def _processes(snapshot: dict, action: str) -> dict:
    observed = process_snapshot()
    if action in {'safe_abort', 'continue_recovery'} and snapshot['runtime_status'] == 'offline':
        require_stopped()
        if observed['initial_service_state'] != 'stopped':
            raise ControlError('control_recovery_supervisor_required')
    elif (action != 'continue_recovery' or snapshot['runtime_status'] != 'active' or observed['initial_service_state'] != 'running'
            or observed['supervisor_invocation_id'] != snapshot['original_supervisor_invocation_id']):
        raise ControlError('control_recovery_supervisor_required')
    return observed


def _run(grant: dict) -> None:
    from . import update_grants
    from .tickets import read_protected_json
    from .stop import _require_empty
    require_root();update_grants.parse_grant(grant);update_grants.require_live(grant)
    if grant['schema_version'] == 2:
        update_grants.require_resumption_plan(grant)
    operation = grant['operation_id']
    actor = operation_unit(operation)
    if actor['ActiveState'] not in {'inactive', 'failed'} or int(actor['MainPID']) != 0:
        raise ControlError('control_controller_busy')
    name = 'streamhome-operation@'+operation+'.service'
    _require_empty(name)
    selected = descriptor()
    if selected.installation_id != grant['installation_id']:
        raise ControlError('control_recovery_review_stale')
    directory = root_directory(update_grants.ROOT/operation, create=True, gid=selected.service_gid, mode=0o750)
    os.close(directory)
    retained = update_grants.ROOT/operation/('update-review-'+grant['review_id']+'.json')
    if retained.exists() or retained.is_symlink():
        if read_protected_json(retained) != grant:
            raise ControlError('control_update_recovery_request_changed')
    else:
        atomic_root_json(retained, grant, gid=selected.service_gid, mode=0o640)
    path = update_grants.grant_path(operation, grant['generation']+1)
    if path.exists() or path.is_symlink():
        old = update_grants.parse_grant(read_protected_json(path))
        if (old['operation_id'] != operation or old['installation_id'] != grant['installation_id']
                or old['generation'] != grant['generation']):
            raise ControlError('control_update_recovery_request_changed')
        previous = update_grants.ROOT/operation/('update-review-'+old['review_id']+'.json')
        if read_protected_json(previous) != old:
            raise ControlError('control_update_recovery_request_changed')
    atomic_root_json(path, grant, gid=selected.service_gid, mode=0o640)
    update_grants.require_live(grant)
    if grant['action'] in {'safe_abort', 'repair_bound_environment'}:
        require_stopped()
    systemctl('start', name, timeout=30)
    while time.monotonic() < grant['expires_at_monotonic']:
        update_grants.require_live(grant)
        if read_protected_json(path) != grant:
            raise ControlError('control_update_recovery_request_changed')
        actor = operation_unit(operation)
        if actor['ActiveState'] in {'inactive', 'failed'}:
            if int(actor['MainPID']) != 0:
                raise ControlError('control_controller_busy')
            _require_empty(name)
            result = (run_worker(['bound-repair-result', operation], timeout=180)
                if grant['schema_version'] == 3 else run_worker(['update-recovery-result', operation], payload={
                    'controller_generation': grant['generation']+1}, timeout=120))
            if result.get('completed') is not True:
                raise ControlError('control_operation_recovery_required')
            return
        if grant['schema_version'] == 2:
            from .resumption_dispatch import _observe, start
            from .resumption_store import live_state
            permission = live_state(grant['resumption_plan_id'], 'candidate_start',
                control_manifest_sha256=grant['control_manifest_sha256'])
            if permission['receipt'] is not None:
                start(grant, control_manifest_sha256=grant['control_manifest_sha256'])
            else:
                try:
                    _observe(permission['plan'])
                except ControlError as exc:
                    if exc.code not in {'control_controller_changed', 'control_worker_failed'}:
                        raise
                else:
                    start(grant, control_manifest_sha256=grant['control_manifest_sha256'])
        time.sleep(.2)
    raise ControlError('control_operation_timeout')


def recover(snapshot: dict, *, request_path: Path | None, control_manifest_sha256: str,
            review_only: bool, action: str, scope_epoch: str | None) -> int | StoppedRepairHandoff:
    from .recover import _consent
    from ..terminal import open_console
    require_root();epoch = root_stop_epoch()
    if scope_epoch is not None and epoch != scope_epoch:
        raise ControlError('control_operator_stopped')
    if review_only:
        print(json.dumps(snapshot, sort_keys=True, allow_nan=False));return 0
    if action not in {'safe_abort', 'continue_recovery'} or action not in snapshot['permitted_actions']:
        raise ControlError('control_recovery_action_unavailable')
    processes = _processes(snapshot, action)
    operation = snapshot['operation_id']
    if scope_epoch is None:
        return StoppedRepairHandoff(snapshot['installation_id'], operation, epoch, request_path, action)
    scope = current_scope(snapshot['installation_id'], epoch)
    resumption = None
    if snapshot['runtime_status'] == 'offline' and action == 'continue_recovery':
        from . import parent_resumption_review, resumption_review
        resumption = parent_resumption_review.make_review(snapshot, action,
            control_manifest_sha256=control_manifest_sha256)
        approved = resumption_review.consent(resumption, request_path)
    elif request_path is not None:
        approved = _consent(request_path, snapshot, action=action)
    else:
        with open_console() as console:
            console.write('Recover the interrupted StreamHome update.')
            console.write('Operation: '+operation+'; generation: '+str(snapshot['generation']))
            console.write('Accepted release: '+('yes' if snapshot['accepted_execution_sha256'] else 'no'))
            console.write('Recovery review SHA-256: '+snapshot['evidence_sha256'])
            if action == 'safe_abort':
                console.write('Abandon the unaccepted update reservation. The application remains stopped.')
            else:
                console.write('Owned application processes may be interrupted. Recorded configuration remains selected.')
                console.write('Accepted application and database changes remain accepted.')
            approved = console.confirm('Abandon this exact update?' if action == 'safe_abort'
                else 'Continue this exact update recovery?')
    if not approved:
        print('Update recovery was declined.');return 0
    if (root_stop_epoch() != epoch or _processes(snapshot, action) != processes
            or run_worker(['recovery-review', operation], timeout=120) != snapshot):
        raise ControlError('control_recovery_review_stale')
    payload = {'operation_id': operation,
        'review_id': str(uuid.uuid4()), 'expected_evidence_sha256': snapshot['evidence_sha256'],
        'action': action, 'issuer_scope': scope, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'stop_epoch': epoch}
    if resumption is not None:
        plan = parent_resumption_review.launch_plan(resumption, approved=True)
        payload.update(resumption_plan_id=plan['plan_id'], review_id=plan['review_id'])
    grant = run_worker(['prepare-update-recovery'], payload=payload, timeout=120)
    if grant.get('control_manifest_sha256') != control_manifest_sha256 or root_stop_epoch() != epoch:
        raise ControlError('control_recovery_review_stale')
    if resumption is not None:
        from .resumption_store import publish
        publish(plan, resumption.issuer, control_manifest_sha256=control_manifest_sha256)
    _run(grant)
    print('StreamHome update recovery completed.')
    return 0
