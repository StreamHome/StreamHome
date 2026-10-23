"""Cancellable root issuer for preparation/repair that must remain stopped."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import uuid

from .administration_lock import process_identity
from .privileged import _OS_ENV, require_root, systemctl
from .rpc import ControlError


@dataclass(frozen=True)
class StoppedRepairHandoff:
    installation_id: str
    preparation_id: str
    stop_epoch: str
    request_path: Path | None
    recovery_action: str | None = None
    running_repair: bool = False


def _uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError as exc:
        raise ControlError('control_stopped_repair_invalid') from exc
    if parsed is None or not parsed.int or str(parsed) != value:
        raise ControlError('control_stopped_repair_invalid')
    return value


def parse_scope(value: dict, installation_id: str) -> dict:
    _uuid(installation_id)
    if (not isinstance(value, dict) or set(value) != {'unit', 'invocation', 'pid', 'start_identity'}
            or not isinstance(value['unit'], str)
            or not re.fullmatch('streamhome-install-' + re.escape(installation_id) + r'-[0-9a-f]{32}\.scope', value['unit'])
            or not isinstance(value['invocation'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['invocation'])
            or not int(value['invocation'], 16) or type(value['pid']) is not int or not 1 < value['pid'] < 2**31
            or not isinstance(value['start_identity'], str) or not re.fullmatch(r'[0-9]{1,20}', value['start_identity'])):
        raise ControlError('control_stopped_repair_invalid')
    return value


def require_scope(value: dict, installation_id: str) -> None:
    parse_scope(value, installation_id)
    process = Path('/proc') / str(value['pid'])
    start, status = process_identity(value['pid'])
    if (start != value['start_identity'] or status in {'Z', 'X'} or process.stat().st_uid != 0
            or (process / 'cgroup').read_text().splitlines() != ['0::/system.slice/' + value['unit']]):
        raise ControlError('control_stopped_repair_issuer_changed')
    raw = systemctl('show', value['unit'], '--property=ActiveState,Transient,ControlGroup,InvocationID')
    observed = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    if observed != {'ActiveState': 'active', 'Transient': 'yes', 'ControlGroup': '/system.slice/' + value['unit'], 'InvocationID': value['invocation']}:
        raise ControlError('control_stopped_repair_issuer_changed')


def current_scope(installation_id: str, epoch: str) -> dict:
    require_root()
    from .provisioning import _require_install_scope
    value = _require_install_scope(installation_id, epoch)
    start, status = process_identity(os.getpid())
    if status in {'Z', 'X'}:
        raise ControlError('control_stopped_repair_issuer_changed')
    return parse_scope({**value, 'pid': os.getpid(), 'start_identity': start}, installation_id)


def require_stopped() -> None:
    raw = systemctl('show', 'streamhome.service', '--property=ActiveState,MainPID,Job')
    values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    if (set(values) != {'ActiveState', 'MainPID', 'Job'} or values['ActiveState'] not in {'inactive', 'failed'}
            or values['MainPID'] != '0' or values['Job'] not in {'', '0'}):
        raise ControlError('control_stopped_repair_service_changed')
    from .stop import _require_empty
    _require_empty('streamhome.service')


def _latest_recovery_scope(directory: Path) -> str | None:
    from .tickets import read_protected_json
    paths = ([path for path in directory.glob('recovery-*.json')
        if re.fullmatch(r'recovery-[1-9][0-9]{0,18}\.json', path.name)] if directory.exists() else [])
    if len(paths) > 128:
        raise ControlError('control_recovery_request_invalid')
    if not paths:
        return None
    latest = max(paths, key=lambda path: int(path.stem.removeprefix('recovery-')))
    return read_protected_json(latest).get('issuer_scope', {}).get('unit')


def _cleanup(handoff: StoppedRepairHandoff, name: str) -> None:
    from .provisioning import administrative_lock
    from .stop import _require_empty
    from .tickets import read_protected_json
    if systemctl('show', name, '--property=LoadState', '--value') != 'not-found':
        systemctl('stop', name, timeout=30)
    _require_empty(name)
    try:
        with administrative_lock():
            if handoff.recovery_action is None:
                path = Path('/etc/streamhome/control/preparations') / handoff.preparation_id / 'request.json'
                if not path.exists() and not path.is_symlink():
                    return
                grant = read_protected_json(path)
                scope = grant.get('issuer_scope' if grant.get('schema_version') == 3 else 'offline_scope', {})
                if scope.get('unit') != name:
                    raise ControlError('control_stopped_repair_issuer_changed')
                roles = ('prepare', 'operation')
            else:
                directory = Path('/etc/streamhome/control/operations') / handoff.preparation_id
                if _latest_recovery_scope(directory) != name:
                    return
                roles = ('operation',)
            for role in roles:
                if role == 'operation' and handoff.recovery_action is None:
                    directory = Path('/etc/streamhome/control/operations') / handoff.preparation_id
                    if directory.exists() and any(directory.glob('recovery-*.json')):
                        continue  # A later explicit owner has adopted this operation.
                unit = f'streamhome-{role}@{handoff.preparation_id}.service'
                raw = systemctl('show', unit, '--property=LoadState,FragmentPath,User')
                observed = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
                if observed.get('LoadState') == 'not-found':
                    _require_empty(unit)
                    continue
                if observed != {'LoadState': 'loaded', 'FragmentPath': f'/etc/systemd/system/streamhome-{role}@.service', 'User': 'streamhome'}:
                    raise ControlError('control_stop_unit_unqualified')
                systemctl('stop', unit, timeout=30)
                _require_empty(unit)
    except ControlError as exc:
        if exc.code != 'control_administrator_busy':
            raise


def run_scoped(handoff: StoppedRepairHandoff) -> int:
    require_root()
    _uuid(handoff.installation_id)
    _uuid(handoff.preparation_id)
    if not isinstance(handoff.stop_epoch, str) or not re.fullmatch(r'[0-9a-f]{64}', handoff.stop_epoch):
        raise ControlError('control_stopped_repair_invalid')
    if handoff.recovery_action not in {None, 'continue_recovery', 'rollback', 'safe_abort'}:
        raise ControlError('control_stopped_repair_invalid')
    if type(handoff.running_repair) is not bool or handoff.running_repair and handoff.recovery_action is not None:
        raise ControlError('control_stopped_repair_invalid')
    from . import privileged
    name = 'streamhome-install-' + handoff.installation_id + '-' + uuid.uuid4().hex + '.scope'
    arguments = ['/usr/bin/systemd-run', '--scope', '--quiet', '--collect', '--slice=system.slice', '--unit=' + name,
        '--description=StreamHome installation ' + handoff.installation_id, '--property=StopPropagatedFrom=streamhome.service',
        '--property=RuntimeMaxSec=3600', '/usr/bin/python3', '-I', '/usr/local/libexec/streamhome/provision',
        *(['--rescue'] if privileged._RESCUE_MODE else []),
        ('--recover-stopped-scoped' if handoff.recovery_action is not None else
         '--repair-running-scoped' if handoff.running_repair else '--repair-stopped-scoped'), handoff.installation_id,
        handoff.preparation_id, handoff.stop_epoch,
        *(['--action', handoff.recovery_action] if handoff.recovery_action is not None else []),
        *(['--request', os.path.abspath(handoff.request_path)] if handoff.request_path is not None else [])]
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, cwd='/', env=_OS_ENV, timeout=3630)
    except BaseException:
        _cleanup(handoff, name)
        raise
    if result.returncode:
        _cleanup(handoff, name)
        if handoff.running_repair:
            from .administration import _sha
            from .tickets import read_protected_json
            directory = Path('/etc/streamhome/control/operations') / handoff.preparation_id
            marker = directory / 'repair-stop.json'
            if marker.exists() or marker.is_symlink():
                receipt = read_protected_json(marker)
                if (receipt.get('schema_version') == 1 and receipt.get('operation_id') == handoff.preparation_id
                        and receipt.get('request_sha256') == _sha(read_protected_json(directory / 'request.json'))
                        and receipt.get('issuer_scope', {}).get('unit') == name):
                    print('Runtime activation requires a fresh recovery review. Run:', flush=True)
                    print('  sudo streamhome recover --operation ' + handoff.preparation_id, flush=True)
        raise ControlError('control_stopped_repair_scope_stopped')
    return 0
