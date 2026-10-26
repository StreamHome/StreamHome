"""Root OS custody for explicit recovery of the original RESTORE operation."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from .administration import operation_unit, process_snapshot, root_stop_epoch
from .privileged import descriptor, require_root, run_worker, systemctl
from .root_files import atomic_root_json, root_directory
from .rpc import ControlError
from .stopped_repair import StoppedRepairHandoff, current_scope, require_stopped


def _processes(snapshot: dict) -> dict:
    observed = process_snapshot()
    if snapshot['runtime_status'] == 'active':
        if (observed['initial_service_state'] != 'running'
                or observed['supervisor_invocation_id'] != snapshot['original_supervisor_invocation_id']):
            raise ControlError('control_recovery_supervisor_required')
    elif snapshot['runtime_status'] == 'offline':
        require_stopped()
        if observed['initial_service_state'] != 'stopped':
            raise ControlError('control_recovery_supervisor_required')
    else:
        raise ControlError('control_recovery_supervisor_required')
    return observed


def _run(grant: dict) -> None:
    from .parent_grants import ROOT, grant_path, parse_grant, require_live
    from .tickets import read_protected_json
    from .stop import _require_empty
    require_root();parse_grant(grant);require_live(grant)
    operation = grant['operation_id']
    unit = operation_unit(operation)
    if unit['ActiveState'] not in {'inactive', 'failed'} or int(unit['MainPID']) != 0:
        raise ControlError('control_controller_busy')
    name = 'streamhome-operation@'+operation+'.service'
    _require_empty(name)
    selected = descriptor()
    if selected.installation_id != grant['installation_id']:
        raise ControlError('control_recovery_review_stale')
    directory = root_directory(ROOT/operation, create=True, gid=selected.service_gid, mode=0o750)
    import os
    os.close(directory)
    retained = ROOT/operation/('recovery-review-'+grant['review_id']+'.json')
    if retained.exists() or retained.is_symlink():
        if read_protected_json(retained) != grant:
            raise ControlError('control_parent_recovery_request_changed')
    else:
        atomic_root_json(retained, grant, gid=selected.service_gid, mode=0o640)
    path = grant_path(operation, grant['generation']+1)
    if path.exists() or path.is_symlink():
        previous = parse_grant(read_protected_json(path))
        old_retained = ROOT/operation/('recovery-review-'+previous['review_id']+'.json')
        if read_protected_json(old_retained) != previous:
            raise ControlError('control_parent_recovery_request_changed')
    atomic_root_json(path, grant, gid=selected.service_gid, mode=0o640)
    require_live(grant)
    systemctl('start', name, timeout=30)
    while time.monotonic() < grant['expires_at_monotonic']:
        require_live(grant)
        if read_protected_json(path) != grant:
            raise ControlError('control_parent_recovery_request_changed')
        unit = operation_unit(operation)
        if unit['ActiveState'] in {'inactive', 'failed'}:
            result = (run_worker(['bound-repair-result',operation],timeout=120)
                if grant['schema_version']==3 else run_worker(['parent-recovery-result', operation], payload={
                    'controller_generation':grant['generation']+1}, timeout=120))
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
                    # A speculative reader may race the existing canonical
                    # state/lease/journal transfer. An inconclusive read never
                    # consumes a start; live grant/deadline checks still bound
                    # this wait. Dispatch repeats the complete proof itself.
                    if exc.code not in {'control_controller_changed','control_worker_failed'}:raise
                else:
                    start(grant, control_manifest_sha256=grant['control_manifest_sha256'])
        time.sleep(.2)
    raise ControlError('control_operation_timeout')


def recover(snapshot: dict, *, request_path: Path | None, control_manifest_sha256: str,
            review_only: bool, action: str, scope_epoch: str | None) -> int | StoppedRepairHandoff:
    from .recover import _consent
    from ..terminal import open_console
    require_root()
    epoch = root_stop_epoch()
    if scope_epoch is not None and epoch != scope_epoch:
        raise ControlError('control_operator_stopped')
    if review_only:
        print(json.dumps(snapshot, sort_keys=True, allow_nan=False));return 0
    if action not in snapshot['permitted_actions']:
        raise ControlError('control_recovery_action_unavailable')
    resuming = snapshot['runtime_status'] == 'offline' and action != 'safe_abort'
    processes = _processes(snapshot)
    operation = snapshot['operation_id']
    if scope_epoch is None:
        return StoppedRepairHandoff(snapshot['installation_id'], operation, epoch, request_path, action)
    scope = current_scope(snapshot['installation_id'], epoch)
    resumption = None
    if resuming:
        from . import parent_resumption_review, resumption_review
        resumption = parent_resumption_review.make_review(snapshot,action,
            control_manifest_sha256=control_manifest_sha256)
        approved = resumption_review.consent(resumption,request_path)
    elif request_path is not None:
        approved = _consent(request_path, snapshot, action=action)
    else:
        with open_console() as console:
            console.write('Recover the original database restore operation.')
            console.write('Operation: '+operation+'; generation: '+str(snapshot['generation']))
            console.write('Action: '+action+'; accepted: '+('yes' if snapshot['accepted'] else 'no'))
            console.write('Recovery review SHA-256: '+snapshot['evidence_sha256'])
            console.write('The original application/configuration execution remains selected.')
            console.write('Owned application children may be interrupted. Accepted database changes stay accepted.')
            approved = console.confirm('Perform this exact restore recovery action?')
    if not approved:
        print('Restore recovery was declined.');return 0
    if (root_stop_epoch() != epoch or _processes(snapshot) != processes
            or run_worker(['recovery-review',operation], timeout=120) != snapshot):
        raise ControlError('control_recovery_review_stale')
    payload = {'operation_id':operation,
        'review_id':str(uuid.uuid4()), 'expected_evidence_sha256':snapshot['evidence_sha256'],
        'action':action, 'issuer_scope':scope, 'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'stop_epoch':epoch}
    if resumption is not None:
        plan = parent_resumption_review.launch_plan(resumption,approved=True)
        payload.update(resumption_plan_id=plan['plan_id'],review_id=plan['review_id'])
    grant = run_worker(['prepare-parent-recovery'], payload=payload, timeout=120)
    if grant.get('control_manifest_sha256') != control_manifest_sha256 or root_stop_epoch() != epoch:
        raise ControlError('control_recovery_review_stale')
    if resumption is not None:
        from .resumption_store import publish
        publish(plan,resumption.issuer,control_manifest_sha256=control_manifest_sha256)
    _run(grant)
    print('StreamHome restore recovery completed.')
    return 0
