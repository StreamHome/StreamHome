"""Root review of exact bound repair; all canonical/filesystem planning is UID-only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid

from ..configure_wizard import parse_request_json
from ..terminal import open_console
from .administration import _identity, operation_unit, root_stop_epoch, run_operation
from .privileged import require_root, run_worker
from .rpc import ControlError
from .stopped_repair import StoppedRepairHandoff, current_scope, require_stopped


ACTION = 'repair_bound_environment'


def _consent(path: Path, snapshot: dict) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32768:
            raise ControlError('control_request_invalid')
        value = parse_request_json(handle.read(32769))
    fields = {'schema_version', 'operation_id', 'expected_generation', 'expected_evidence_sha256', ACTION}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or type(value['expected_generation']) is not int
            or type(value[ACTION]) is not bool):
        raise ControlError('control_request_invalid')
    if (value['operation_id'] != snapshot['operation_id'] or value['expected_generation'] != snapshot['generation']
            or value['expected_evidence_sha256'] != snapshot['evidence_sha256']):
        raise ControlError('control_recovery_review_stale')
    return value[ACTION]


def repair(operation_id: str, *, request_path: Path | None, control_manifest_sha256: str,
           review_only: bool = False, scope_epoch: str | None = None, expected_installation_id: str | None = None):
    require_root();_identity(operation_id)
    actor = operation_unit(operation_id)
    if actor['ActiveState'] not in {'inactive', 'failed'} or int(actor['MainPID']) != 0:
        raise ControlError('control_controller_busy')
    from .stop import _require_empty
    _require_empty(f'streamhome-operation@{operation_id}.service')
    require_stopped()
    epoch = root_stop_epoch()
    if scope_epoch is not None and epoch != scope_epoch:
        raise ControlError('control_operator_stopped')
    snapshot = run_worker(['bound-repair-review', operation_id], timeout=180)
    if expected_installation_id is not None and snapshot['installation_id'] != expected_installation_id:
        raise ControlError('control_recovery_review_stale')
    if review_only:
        print(json.dumps(snapshot, sort_keys=True, allow_nan=False));return 0
    if scope_epoch is None:
        return StoppedRepairHandoff(snapshot['installation_id'], operation_id, epoch, request_path, ACTION)
    scope = current_scope(snapshot['installation_id'], epoch)
    if request_path is not None:
        approved = _consent(request_path, snapshot)
    else:
        with open_console() as console:
            console.write('Repair only the exact environments bound to this unresolved operation.')
            console.write('Operation: ' + operation_id)
            console.write('Phase: ' + snapshot['phase'] + '; accepted: ' + ('yes' if snapshot['accepted'] else 'no'))
            console.write('Bound bundle: ' + snapshot['bound_bundle_id'])
            console.write('Bound manifest SHA-256: ' + snapshot['bound_manifest_sha256'])
            console.write('Corrupt runtime trees will be preserved and reconstructed at their original paths.')
            console.write('The service stays stopped. The original operation remains unresolved.')
            approved = console.confirm('Repair these exact recorded environments?')
    if not approved:
        print('Bound environment repair was declined.');return 0
    require_stopped()
    if root_stop_epoch() != epoch or run_worker(['bound-repair-review', operation_id], timeout=180) != snapshot:
        raise ControlError('control_recovery_review_stale')
    grant = run_worker(['bound-repair-prepare'], payload={'operation_id': operation_id,
        'expected_evidence_sha256': snapshot['evidence_sha256'], 'recovery_review_id': str(uuid.uuid4()),
        'issuer_scope': scope, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'stop_epoch': epoch}, timeout=180)
    if snapshot.get('operation_type')=='restore':
        if (grant.get('schema_version')!=3 or grant.get('action')!=ACTION
                or grant.get('control_manifest_sha256')!=control_manifest_sha256 or root_stop_epoch()!=epoch):
            raise ControlError('control_recovery_review_stale')
        from .parent_recover import _run
        _run(grant)
        result={'stage':'environment_repaired_recovery_required'}
    elif snapshot.get('operation_type') == 'update':
        if (grant.get('schema_version') != 3 or grant.get('action') != ACTION
                or grant.get('control_manifest_sha256') != control_manifest_sha256 or root_stop_epoch() != epoch):
            raise ControlError('control_recovery_review_stale')
        from .update_recover import _run
        _run(grant)
        result = {'stage': 'environment_repaired_recovery_required'}
    elif (grant.get('schema_version') != 4 or grant.get('action') != ACTION
            or grant['request']['controller_control_manifest_sha256'] != control_manifest_sha256
            or root_stop_epoch() != epoch):
        raise ControlError('control_recovery_review_stale')
    else:
        result = run_operation(grant['request'], recovery_grant=grant)
    if result.get('stage') != 'environment_repaired_recovery_required':
        raise ControlError('control_operation_recovery_required')
    print('Exact dependencies were restored. The service is stopped and the original operation remains unresolved.')
    print('Request a fresh recovery review: sudo streamhome recover --operation ' + operation_id)
    return 0
