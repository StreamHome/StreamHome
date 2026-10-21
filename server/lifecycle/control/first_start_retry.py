"""Explicit finite first-start recovery; root never imports application state."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import uuid

from ..configure_wizard import parse_request_json
from ..installation import InstallationStatus
from ..terminal import open_console
from .administration import root_stop_epoch
from .privileged import descriptor, require_root, run_worker, systemctl, unit_snapshot
from .provisioning import (administrative_lock, read_intent, _persist, _first_start, _finish_registration,
    _require_install_scope, _run_install_scope, complete_install)
from .provisioning_contract import identity, intent_sha256, reviewed_start_attempt
from .rpc import ControlError
from .support import require_supported_platform
from .first_start_plan import publish_start_plan


def _qualify(intent, *, manifest: dict, manifest_sha256: str) -> None:
    selected = descriptor()
    if (selected.status is not InstallationStatus.PROVISIONING or selected.installation_id != intent.installation_id
            or intent.control_bundle_id != manifest['bundle_id'] or intent.control_manifest_sha256 != manifest_sha256
            or intent.stage != 'first_start_requested' or intent.start_permit not in {'issued', 'consumed'}):
        raise ControlError('control_start_retry_unavailable')


def _inactive() -> None:
    from .stop import _require_empty
    unit = unit_snapshot()
    if (unit['LoadState'] != 'loaded' or unit['ActiveState'] not in {'inactive', 'failed'}
            or int(unit['MainPID']) != 0 or systemctl('show', 'streamhome.service', '--property=Job') not in {'Job=', 'Job=0'}):
        raise ControlError('control_stop_before_first_start_retry')
    _require_empty('streamhome.service')


def _consent(path: Path, intent) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32768:
            raise ControlError('control_request_invalid')
        value = parse_request_json(handle.read(32769))
    if (set(value) != {'schema_version', 'installation_id', 'expected_intent_sha256', 'retry_first_start', 'start_approved'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['retry_first_start']) is not bool or type(value['start_approved']) is not bool):
        raise ControlError('control_request_invalid')
    if value['installation_id'] != intent.installation_id or value['expected_intent_sha256'] != intent_sha256(intent):
        raise ControlError('control_review_stale')
    return value['retry_first_start'] and value['start_approved']


def retry_scoped(installation_id: str, epoch: str, *, manifest: dict, manifest_sha256: str, request_path: Path | None) -> int:
    require_root()
    require_supported_platform()
    _require_install_scope(installation_id, epoch)
    with administrative_lock():
        intent = read_intent()
        if intent.installation_id != identity(installation_id):
            raise ControlError('control_installation_review_changed')
        _qualify(intent, manifest=manifest, manifest_sha256=manifest_sha256)
        from .provisioning_mirror import publish_mirror
        publish_mirror(intent)
        _inactive()
        expected = {'installation_id': intent.installation_id, 'intent_sha256': intent_sha256(intent),
            'generation': 0, 'execution_sha256': intent.execution_sha256}
        if run_worker(['initial-retry-check'], timeout=120) != expected:
            raise ControlError('control_initial_result_changed')
        if request_path is not None:
            approved = _consent(request_path, intent)
        else:
            with open_console() as console:
                console.write('StreamHome first-start recovery')
                console.write('Application commit: ' + intent.application_commit)
                console.write('Recorded attempt: ' + intent.start_permit)
                console.write('One new start will use the recorded application and configuration.')
                approved = console.confirm('Authorize this first-start attempt?')
        if not approved:
            print('First-start recovery was declined.')
            return 0
        _inactive()
        if root_stop_epoch() != epoch or read_intent() != intent:
            raise ControlError('control_operator_stopped')
        if run_worker(['initial-retry-check'], timeout=120) != expected:
            raise ControlError('control_initial_result_changed')
        _inactive()
        if root_stop_epoch() != epoch or read_intent() != intent:
            raise ControlError('control_operator_stopped')
        candidate = reviewed_start_attempt(intent, review_id=str(uuid.uuid4()),
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), stop_epoch=epoch)
        # Retired permission/history is durable in both root anchors before
        # resetting this exact unit's OS failure counter or requesting a start.
        _persist(candidate)
        publish_start_plan(candidate)
        if root_stop_epoch() != epoch:
            raise ControlError('control_operator_stopped')
        systemctl('reset-failed', 'streamhome.service')
        registered = _first_start(candidate, expected_stop_epoch=epoch)
        _finish_registration(registered)
    return 0


def retry_installation(installation_id: str, *, manifest: dict, manifest_sha256: str, request_path: Path | None = None) -> int:
    require_root()
    require_supported_platform()
    identity(installation_id)
    with administrative_lock():
        intent = read_intent()
        if intent.installation_id != installation_id:
            raise ControlError('control_installation_review_changed')
        _qualify(intent, manifest=manifest, manifest_sha256=manifest_sha256)
        _inactive()
        epoch = root_stop_epoch()
    result = _run_install_scope(installation_id, epoch, retry=True, request_path=request_path)
    if result == 0 and descriptor().status is InstallationStatus.READY:
        return complete_install(installation_id, manifest=manifest, manifest_sha256=manifest_sha256, automatic=True)
    return result
