"""Protected finite launch intents; no service dispatch or canonical imports."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import stat
import time

from . import resumption_contract as contract
from .privileged import descriptor, require_root
from .resumption_issuer import require_issuer
from .root_files import atomic_root_json, root_directory
from .rpc import ControlError
from .stopped_repair import require_stopped
from .tickets import read_protected_json, read_stop_epoch


ROOT = Path('/etc/streamhome/control/resumption')
START_INTENT = Path('/etc/streamhome/control/resumption-start.json')


def _directory(plan_id: str) -> Path:
    contract._uuid(plan_id)
    return ROOT / plan_id


@contextmanager
def _locked(plan_id: str, *, create: bool = False):
    require_root()
    directory = _directory(plan_id)
    parent = root_directory(directory, create=create, mode=0o750, gid=descriptor().service_gid)
    handle = None
    try:
        handle = os.open('permission.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        value = os.fstat(handle)
        if not stat.S_ISREG(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o7177 or value.st_nlink != 1:
            raise ControlError('control_resumption_lock_untrusted')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControlError('control_resumption_busy') from exc
        yield directory
    finally:
        if handle is not None:
            os.close(handle)
        os.close(parent)


def _selected(plan: dict, control_manifest_sha256: str):
    selected = descriptor()
    if (selected.status.value != 'ready' or selected.installation_id != plan['installation_id']
            or control_manifest_sha256 != plan['control_manifest_sha256']):
        raise ControlError('control_resumption_review_changed')
    return selected


def _read(directory: Path, control_manifest_sha256: str) -> dict:
    value = read_protected_json(directory / 'plan.json')
    if not isinstance(value, dict) or set(value) != {'plan', 'issuer'}:
        raise ControlError('control_resumption_plan_invalid')
    plan = contract.parse_plan(value['plan'])
    if plan['plan_id'] != directory.name:
        raise ControlError('control_resumption_review_changed')
    _selected(plan, control_manifest_sha256)
    require_issuer(value['issuer'], plan)
    return value


def _receipt(directory: Path, kind: str, plan: dict) -> dict:
    value = contract.parse_receipt(read_protected_json(directory / (kind + '.json')), plan)
    if value['kind'] != kind:
        raise ControlError('control_resumption_receipt_invalid')
    return value


def publish(plan: dict, evidence: dict, *, control_manifest_sha256: str) -> None:
    require_root()
    contract.parse_plan(plan)
    selected = _selected(plan, control_manifest_sha256)
    require_issuer(evidence, plan)
    if evidence['owner']['pid'] != os.getpid():
        raise ControlError('control_resumption_issuer_changed')
    # Publication cannot extend a reviewed launch permission indefinitely.
    if not time.monotonic() < plan['expires_at_monotonic'] <= time.monotonic() + 900:
        raise ControlError('control_resumption_plan_invalid')
    with _locked(plan['plan_id'], create=True) as directory:
        if any(directory.glob('*.json')):
            raise ControlError('control_resumption_already_issued')
        require_issuer(evidence, plan)
        atomic_root_json(directory / 'plan.json', {'plan': plan, 'issuer': evidence}, gid=selected.service_gid, mode=0o640)


def consume_stage(plan_id: str, kind: str, observation: dict, *, control_manifest_sha256: str) -> dict:
    """Persist consumption before the caller dispatches its fixed OS start.

    Only the root dispatcher may call this with a fresh independent UID-reader
    observation. The store never treats caller data as canonical ownership.
    """
    require_root()
    if kind not in ('candidate_start', 'previous_start'):
        raise ControlError('control_resumption_stage_unavailable')
    with _locked(plan_id) as directory:
        record = _read(directory, control_manifest_sha256)
        plan = record['plan']
        if record['issuer']['owner']['pid'] != os.getpid():
            raise ControlError('control_resumption_issuer_changed')
        candidate_path = directory / 'candidate_start.json'
        candidate_consumed = candidate_path.exists() or candidate_path.is_symlink()
        if candidate_consumed:
            _receipt(directory, 'candidate_start', plan)
        stage = contract.authorize_stage(plan, kind, observation,
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), stop_epoch=read_stop_epoch(),
            issuer_sha256=contract.digest(record['issuer']), now=time.monotonic(), candidate_consumed=candidate_consumed)
        path = directory / (kind + '.json')
        existing = _receipt(directory, kind, plan) if path.exists() or path.is_symlink() else None
        receipt = contract.consume(plan, stage, secrets.token_hex(32), existing=existing)
        require_issuer(record['issuer'], plan)
        require_stopped()
        atomic_root_json(path, receipt, gid=descriptor().service_gid, mode=0o640)
        # A failed second publication leaves a consumed permission, never a retry.
        atomic_root_json(START_INTENT, contract.start_intent(plan, receipt), gid=descriptor().service_gid, mode=0o640)
        return receipt


def bind_start(plan_id: str, kind: str, invocation: str, *, control_manifest_sha256: str) -> dict:
    """Bind a consumed intent once, from the fixed runtime ExecStartPre hook."""
    require_root()
    if (Path('/proc/self/cgroup').read_text().splitlines() != ['0::/system.slice/streamhome.service']
            or os.environ.get('INVOCATION_ID') != invocation):
        raise ControlError('control_unit_hook_forbidden')
    if kind not in ('candidate_start', 'previous_start'):
        raise ControlError('control_resumption_stage_unavailable')
    with _locked(plan_id) as directory:
        record = _read(directory, control_manifest_sha256)
        path = directory / (kind + '.json')
        current = _receipt(directory, kind, record['plan'])
        contract.require_start_intent(read_protected_json(START_INTENT), record['plan'], current)
        receipt = contract.bind_invocation(record['plan'], current, invocation)
        require_issuer(record['issuer'], record['plan'])
        atomic_root_json(path, receipt, gid=descriptor().service_gid, mode=0o640)
        return receipt


def bind_current_start(*, control_manifest_sha256: str) -> dict:
    require_root()
    intent = contract.parse_start_intent(read_protected_json(START_INTENT))
    return bind_start(intent['plan_id'], intent['kind'], os.environ.get('INVOCATION_ID', ''),
                      control_manifest_sha256=control_manifest_sha256)


def live_state(plan_id: str, kind: str, *, control_manifest_sha256: str) -> dict:
    require_root()
    if kind not in ('candidate_start', 'previous_start'):
        raise ControlError('control_resumption_stage_unavailable')
    with _locked(plan_id) as directory:
        record = _read(directory, control_manifest_sha256)
        path = directory / (kind + '.json')
        receipt = _receipt(directory, kind, record['plan']) if path.exists() or path.is_symlink() else None
        return {**record, 'receipt': receipt}
