"""Root OS orchestration; runtime/canonical interpretation stays in UID readers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

from ..configure_wizard import parse_request_json
from ..terminal import Progress, open_console
from .administration import _boot_enabled, process_snapshot, root_stop_epoch, run_operation
from .privileged import descriptor, require_root, run_worker, systemctl
from .root_files import atomic_root_json, root_directory
from .rpc import ControlError
from .support import PREPARATION_STAGES


def sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _request(path: Path, snapshot: dict) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32768:
            raise ControlError('control_request_invalid')
        value = parse_request_json(handle.read(32769))
    fields = {'schema_version', 'expected_generation', 'expected_execution_sha256', 'expected_desired_revision',
              'prepare', 'activate', 'interruption_approved'}
    if (set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['expected_generation']) is not int
            or any(type(value[name]) is not bool for name in ('prepare', 'activate', 'interruption_approved'))
            or value['prepare'] is not True or value['activate'] != value['interruption_approved']):
        raise ControlError('control_request_invalid')
    if (value['expected_generation'] != snapshot['lifecycle']['generation']
            or value['expected_execution_sha256'] != sha256(snapshot['execution']['execution'])
            or value['expected_desired_revision'] != snapshot['configuration']['deployment']['desired']['revision']):
        raise ControlError('control_review_stale')
    return value


def _prepare(grant: dict) -> dict:
    selected = descriptor()
    preparation = grant['preparation_id']
    if str(uuid.UUID(preparation)) != preparation or not uuid.UUID(preparation).int:
        raise ControlError('control_preparation_invalid')
    directory = Path('/etc/streamhome/control/preparations') / preparation
    fd = root_directory(directory, create=True, gid=selected.service_gid, mode=0o750)
    os.close(fd)
    if (directory / 'request.json').exists() or (directory / 'request.json').is_symlink():
        raise ControlError('control_preparation_changed')
    atomic_root_json(directory / 'request.json', grant, gid=selected.service_gid, mode=0o640)
    unit = f'streamhome-prepare@{preparation}.service'
    deadline, current_stage, context = time.monotonic() + 3600, None, None
    progress = Progress(sys.stdout)
    try:
        if root_stop_epoch() != grant['stop_epoch']:
            raise ControlError('control_operator_stopped')
        systemctl('start', unit, timeout=30)
        while time.monotonic() < deadline:
            if root_stop_epoch() != grant['stop_epoch']:
                raise ControlError('control_operator_stopped')
            raw = systemctl('show', unit, '--property=ActiveState,MainPID,Result')
            fields = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
            if set(fields) != {'ActiveState', 'MainPID', 'Result'} or not fields['MainPID'].isdigit():
                raise ControlError('control_unit_invalid')
            if fields['ActiveState'] in {'inactive', 'failed'}:
                if fields['Result'] != 'success' or int(fields['MainPID']) != 0:
                    raise ControlError('control_preparation_failed')
                cgroup = Path(f'/sys/fs/cgroup/system.slice/{unit}/cgroup.procs')
                try:
                    if cgroup.read_text().strip():
                        raise ControlError('control_preparation_busy')
                except FileNotFoundError:
                    pass
                result = run_worker(['runtime-result', preparation], timeout=120)
                if context is not None:
                    context.__exit__(None, None, None)
                    context = None
                return result
            from .progress import read_display_progress
            value = read_display_progress('repair', selected.service_uid, preparation)
            if (isinstance(value, dict) and value.get('stage') in PREPARATION_STAGES
                    and value.get('status') in {'running', 'done'}):
                if value['stage'] != current_stage:
                    if context is not None:
                        context.__exit__(None, None, None)
                    current_stage = value['stage']
                    context = progress.stage(current_stage)
                    context.__enter__()
                if value['status'] == 'done' and context is not None:
                    context.__exit__(None, None, None)
                    context = None
            time.sleep(1)
        raise ControlError('control_preparation_timeout')
    except BaseException:
        if context is not None:
            context.__exit__(*sys.exc_info())
        systemctl('stop', unit, timeout=30)
        raise


def repair(*, request_path: Path | None, control_manifest_sha256: str) -> int:
    require_root()
    selected = descriptor()
    if selected.status.value != 'ready':
        raise ControlError('control_installation_incomplete')
    epoch = root_stop_epoch()
    snapshot = run_worker(['inspect'], timeout=120)
    if snapshot['lifecycle']['active_operation'] is not None:
        raise ControlError('control_operation_recovery_required')
    processes = process_snapshot()
    if processes['initial_service_state'] != 'running' or processes['backend'] is None:
        raise ControlError('control_interrupting_repair_required')
    if snapshot['execution']['execution']['control_manifest_sha256'] != control_manifest_sha256:
        raise ControlError('control_bundle_mismatch')
    enabled = _boot_enabled()
    accepted = _request(request_path, snapshot) if request_path is not None else None
    if accepted is None:
        with open_console() as console:
            console.write('Repair prepares private Python, Node.js and web dependency generations for the accepted build.')
            console.write('Application build: ' + snapshot['execution']['execution']['application_build_id'])
            console.write('The current environment stays in use during preparation. Activation pauses owned application work.')
            if not console.confirm('Prepare the replacement runtime?'):
                return 0
    if root_stop_epoch() != epoch:
        raise ControlError('control_operator_stopped')
    preparation = str(uuid.uuid4())
    grant = {'schema_version': 1, 'installation_id': selected.installation_id, 'preparation_id': preparation,
        'expected_generation': snapshot['lifecycle']['generation'], 'previous': snapshot['execution']['execution'],
        'expected_desired': snapshot['configuration']['deployment']['desired'],
        'supervisor': processes['supervisor'], 'backend': processes['backend'],
        'supervisor_invocation_id': processes['supervisor_invocation_id'],
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'stop_epoch': epoch,
        'control_manifest_sha256': control_manifest_sha256, 'expires_at_monotonic': time.monotonic() + 3600}
    if run_worker(['validate-preparation'], payload=grant) != grant:
        raise ControlError('control_preparation_changed')
    built = _prepare(grant)
    if (not isinstance(built, dict) or set(built) != {'schema_version', 'grant_sha256', 'execution'}
            or built['grant_sha256'] != sha256(grant)):
        raise ControlError('control_preparation_result_invalid')
    print('Verified replacement execution SHA-256: ' + sha256(built['execution']), flush=True)
    if accepted is None:
        with open_console() as console:
            activate = console.confirm('Activate this verified runtime and pause owned application work?')
    else:
        activate = accepted['activate']
    if not activate:
        print('The inactive verified generation is retained. The current runtime was not changed.')
        return 0
    if _boot_enabled() is not enabled:
        raise ControlError('control_boot_policy_changed')
    reviewed = run_worker(['prepare-runtime-request'], payload={'preparation_id': preparation,
        'review_id': str(uuid.uuid4()), 'boot_enabled': enabled}, timeout=120)
    if (reviewed.get('candidate') != built['execution'] or reviewed.get('previous') != grant['previous']
            or reviewed.get('expected_generation') != grant['expected_generation']):
        raise ControlError('control_preparation_changed')
    result = run_operation(reviewed)
    if result.get('completed') is not True or result.get('stage') != 'succeeded':
        raise ControlError('control_operation_recovery_required')
    print('StreamHome runtime repair completed.')
    return 0
