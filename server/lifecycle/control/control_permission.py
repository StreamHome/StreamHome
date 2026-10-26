"""Bounded root scope for original control maintenance; no canonical authority.

The installed UID controller may read the protected record and reprove its root
scope. Issuing or retaining it cannot select code, claim an operation or start a
service. Recovery uses its separate fresh generation-bound grant instead.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import time

from .administration_lock import OWNER, parse_owner
from .privileged import descriptor, require_root
from .resumption_contract import _digest, _uuid, digest
from .resumption_issuer import current_issuer
from .root_files import atomic_root_json
from .rpc import ControlError
from .stopped_repair import parse_scope, require_scope
from .tickets import read_protected_json, read_stop_epoch


OPERATIONS=Path('/etc/streamhome/control/operations')


def _path(request: dict) -> Path:
    if (not isinstance(request,dict) or request.get('action') != 'provision_control'
            or request.get('activation_mode') != 'cutover' or request.get('interruption_approved') is not True
            or not isinstance(request.get('initial_service_state'),str)
            or request['initial_service_state'] not in {'running','stopped'}):
        raise ControlError('control_maintenance_permission_invalid')
    for name in ('installation_id','operation_id','boot_id'):_uuid(request.get(name))
    for name in ('stop_epoch','controller_control_manifest_sha256'):_digest(request.get(name))
    return OPERATIONS/request['operation_id']/'control-permission.json'


def require_initial(request: dict) -> dict:
    """Root checks its actual lock owner; UID checks the protected live scope."""
    path=_path(request)
    value=read_protected_json(path)
    fields={'schema_version','operation_id','installation_id','request_sha256','control_manifest_sha256',
        'boot_id','stop_epoch','issuer','issued_at_monotonic','expires_at_monotonic'}
    if (not isinstance(value,dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['operation_id'] != request['operation_id']
            or value['installation_id'] != request['installation_id'] or value['request_sha256'] != digest(request)
            or value['control_manifest_sha256'] != request['controller_control_manifest_sha256']
            or value['boot_id'] != request['boot_id'] or value['stop_epoch'] != request['stop_epoch']
            or value['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or value['stop_epoch'] != read_stop_epoch()):
        raise ControlError('control_maintenance_permission_changed')
    issued,expiry=value['issued_at_monotonic'],value['expires_at_monotonic']
    if (any(type(number) not in {int,float} or not math.isfinite(number) for number in (issued,expiry))
            or not 0 <= issued <= time.monotonic() < expiry <= issued+900):
        raise ControlError('control_maintenance_permission_expired')
    evidence=value['issuer']
    if not isinstance(evidence,dict) or set(evidence) != {'owner','scope'}:
        raise ControlError('control_maintenance_permission_changed')
    owner=parse_owner(evidence['owner']);scope=parse_scope(evidence['scope'],request['installation_id'])
    if (owner['status'] != 'active' or owner['boot_id'] != value['boot_id'] or owner['pid'] != scope['pid']
            or owner['start_identity'] != scope['start_identity']
            or os.geteuid()==0 and read_protected_json(OWNER,maximum=4096) != owner):
        raise ControlError('control_maintenance_permission_changed')
    require_scope(scope,request['installation_id'])
    return value


def issue(request: dict) -> dict:
    """Publish once under the existing root administrative lock and scope."""
    require_root()
    path=_path(request)
    selected=descriptor()
    if selected.installation_id != request['installation_id']:
        raise ControlError('control_maintenance_permission_changed')
    if (request['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or request['stop_epoch'] != read_stop_epoch()):
        raise ControlError('control_maintenance_permission_changed')
    if path.exists() or path.is_symlink():
        value=require_initial(request)
        if value['issuer']['owner']['pid'] != os.getpid():
            raise ControlError('control_maintenance_permission_changed')
        return value  # Never renew a prior issuance or extend its deadline.
    issuer=current_issuer(request['installation_id'],request['stop_epoch'])
    if issuer['owner']['pid'] != os.getpid():
        raise ControlError('control_maintenance_permission_changed')
    now=time.monotonic()
    if type(now) not in {int,float} or not math.isfinite(now) or now < 0:
        raise ControlError('control_maintenance_permission_invalid')
    value={'schema_version':1,'operation_id':request['operation_id'],'installation_id':request['installation_id'],
        'request_sha256':digest(request),'control_manifest_sha256':request['controller_control_manifest_sha256'],
        'boot_id':request['boot_id'],'stop_epoch':request['stop_epoch'],'issuer':issuer,
        'issued_at_monotonic':now,'expires_at_monotonic':now+900}
    atomic_root_json(path,value,gid=selected.service_gid,mode=0o640)
    return require_initial(request)


def require_stopped_recovery(grant: dict) -> dict:
    """A fresh stopped recovery grant has scope custody and no launch intent."""
    require_root()
    if (not isinstance(grant,dict) or type(grant.get('schema_version')) is not int
            or grant['schema_version'] != 2 or not isinstance(grant.get('request'),dict)):
        raise ControlError('control_maintenance_permission_invalid')
    request=grant['request'];_path(request)
    if request['initial_service_state'] != 'stopped':
        raise ControlError('control_maintenance_permission_invalid')
    generation=request.get('expected_generation')
    if type(generation) is not int or not 1 <= generation < 2**63-1:
        raise ControlError('control_maintenance_permission_invalid')
    _uuid(grant.get('recovery_review_id'));_digest(grant.get('source_request_sha256'))
    expiry=grant.get('expires_at_monotonic');now=time.monotonic()
    if (type(expiry) not in {int,float} or not math.isfinite(expiry) or not now < expiry <= now+900
            or request['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or request['stop_epoch'] != read_stop_epoch()):
        raise ControlError('control_maintenance_permission_expired')
    directory=OPERATIONS/request['operation_id']
    if (read_protected_json(directory/('recovery-'+str(generation+1)+'.json')) != grant
            or read_protected_json(directory/('recovery-review-'+grant['recovery_review_id']+'.json')) != grant):
        raise ControlError('control_maintenance_permission_changed')
    issuer=current_issuer(request['installation_id'],request['stop_epoch'])
    if grant.get('issuer_scope') != issuer['scope'] or issuer['owner']['pid'] != os.getpid():
        raise ControlError('control_maintenance_permission_changed')
    return {'issuer':issuer}
