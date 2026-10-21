"""Immutable, expiring OS permission bound to one live first-start issuer."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import time

from .administration_lock import OWNER, parse_owner, process_identity
from .privileged import require_root, systemctl
from .provisioning import _require_install_scope
from .provisioning_contract import intent_sha256
from .root_files import atomic_root_json, root_directory
from .rpc import ControlError
from .tickets import read_protected_json, read_stop_epoch


PLANS = Path('/etc/streamhome/control/first-start-attempts')


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _review(intent) -> str:
    return intent.start_attempts[-1].review_id if intent.start_attempts else intent.operation_id


def publish_start_plan(intent) -> None:
    require_root()
    if intent.stage != 'first_start_requested' or intent.start_permit != 'issued':
        raise ControlError('control_start_plan_invalid')
    scope = _require_install_scope(intent.installation_id, intent.start_stop_epoch)
    owner = parse_owner(read_protected_json(OWNER, maximum=4096))
    start, status = process_identity(os.getpid())
    if (owner['status'] != 'active' or owner['pid'] != os.getpid() or owner['start_identity'] != start
            or status in {'Z', 'X'} or owner['boot_id'] != intent.start_boot_id or read_stop_epoch() != intent.start_stop_epoch):
        raise ControlError('control_start_plan_invalid')
    parent = root_directory(PLANS, create=True, gid=intent.service_gid, mode=0o750)
    os.close(parent)
    review = _review(intent)
    path = PLANS / (review + '.json')
    if path.exists() or path.is_symlink():
        raise ControlError('control_start_plan_already_issued')
    atomic_root_json(path, {'schema_version': 1, 'action': 'first_start', 'installation_id': intent.installation_id,
        'operation_id': intent.operation_id, 'review_id': review, 'issued_intent_sha256': intent_sha256(intent),
        'execution_sha256': intent.execution_sha256, 'boot_id': intent.start_boot_id, 'stop_epoch': intent.start_stop_epoch,
        'scope_unit': scope['unit'], 'scope_invocation': scope['invocation'], 'owner_sha256': _hash(owner),
        'expires_at_monotonic': time.monotonic() + 900, 'nonce': secrets.token_hex(32)}, gid=intent.service_gid, mode=0o640)


def require_issued_plan(intent, *, issuer: bool = False) -> dict:
    require_root()
    value = read_protected_json(PLANS / (_review(intent) + '.json'))
    expected = {'schema_version': 1, 'action': 'first_start', 'installation_id': intent.installation_id,
        'operation_id': intent.operation_id, 'review_id': _review(intent), 'issued_intent_sha256': intent_sha256(intent),
        'execution_sha256': intent.execution_sha256, 'boot_id': intent.start_boot_id, 'stop_epoch': intent.start_stop_epoch}
    fields = {*expected, 'scope_unit', 'scope_invocation', 'owner_sha256', 'expires_at_monotonic', 'nonce'}
    if (intent.start_permit != 'issued' or set(value) != fields
            or type(value.get('schema_version')) is not int or any(value.get(key) != item for key, item in expected.items())
            or not isinstance(value['scope_unit'], str)
            or not re.fullmatch('streamhome-install-' + re.escape(intent.installation_id) + r'-[0-9a-f]{32}\.scope', value['scope_unit'])
            or not isinstance(value['scope_invocation'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['scope_invocation'])
            or not int(value['scope_invocation'], 16)
            or any(not isinstance(value[key], str) or not re.fullmatch(r'[0-9a-f]{64}', value[key]) for key in ('owner_sha256', 'nonce'))
            or type(value['expires_at_monotonic']) not in {int, float}
            or not 0 < value['expires_at_monotonic'] < 10**12 or not math.isfinite(value['expires_at_monotonic'])
            or time.monotonic() >= value['expires_at_monotonic']
            or value['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or value['stop_epoch'] != read_stop_epoch()):
        raise ControlError('control_start_plan_invalid')
    owner = parse_owner(read_protected_json(OWNER, maximum=4096))
    if owner['status'] != 'active' or owner['boot_id'] != value['boot_id'] or _hash(owner) != value['owner_sha256']:
        raise ControlError('control_start_plan_issuer_changed')
    start, status = process_identity(owner['pid'])
    if (start != owner['start_identity'] or status in {'Z', 'X'} or Path('/proc/' + str(owner['pid'])).stat().st_uid != 0
            or Path('/proc/' + str(owner['pid']) + '/cgroup').read_text().splitlines() != ['0::/system.slice/' + value['scope_unit']]):
        raise ControlError('control_start_plan_issuer_changed')
    raw = systemctl('show', value['scope_unit'], '--property=ActiveState,Transient,ControlGroup,InvocationID')
    observed = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    if observed != {'ActiveState': 'active', 'Transient': 'yes', 'ControlGroup': '/system.slice/' + value['scope_unit'], 'InvocationID': value['scope_invocation']}:
        raise ControlError('control_start_plan_issuer_changed')
    if issuer and (owner['pid'] != os.getpid() or _require_install_scope(intent.installation_id, intent.start_stop_epoch)
                   != {'unit': value['scope_unit'], 'invocation': value['scope_invocation']}):
        raise ControlError('control_start_plan_issuer_changed')
    return value
