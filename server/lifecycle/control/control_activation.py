"""Fixed control-file cutover subordinate to the original MANAGEMENT recovery.

No canonical/application imports, service start or application-data rollback.
Owned unit bytes are read as signed data and preserve the qualified unit ABI.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import time
import uuid

from . import artifact_stage, artifacts, entry
from .administration import OPERATIONS, _sha, _verify_owned_units, operation_unit
from .privileged import require_root, run_worker, systemctl
from .resumption_dispatch import qualify
from .root_files import atomic_root_bytes, atomic_root_json, root_directory
from .rpc import ControlError
from .stopped_repair import require_stopped
from .tickets import read_protected_json


def bind_prepared(request: dict, prepared: artifact_stage.PreparedControl) -> None:
    """Retain the exact signed preparation before the original controller claim."""
    require_root()
    if (not isinstance(request,dict) or request.get('action') != 'provision_control'
            or request.get('activation_mode') != 'cutover' or request.get('interruption_approved') is not True):
        raise ControlError('control_cutover_review_changed')
    operation=request.get('operation_id')
    pin=entry.operation_selection(operation)
    if pin is None or pin['manifest_sha256'] != request.get('controller_control_manifest_sha256'):
        raise ControlError('control_operation_reader_changed')
    verified=artifact_stage.require_prepared(prepared)
    candidate=request.get('candidate')
    if (not isinstance(candidate,dict) or candidate.get('control_bundle_id') != verified.bundle_id
            or candidate.get('control_manifest_sha256') != verified.manifest_sha256
            or candidate.get('control_protocol_version') != 1
            or read_protected_json(OPERATIONS/operation/'request.json') != request):
        raise ControlError('control_cutover_review_changed')
    value={'schema_version':1,'request_sha256':_sha(request),'prepared':asdict(prepared)}
    path=OPERATIONS/operation/'control-artifact.json'
    if path.exists() or path.is_symlink():
        if read_protected_json(path) != value:
            raise ControlError('control_prepared_version_changed')
        return
    atomic_root_json(path,value,mode=0o600)


def prepared_for(request: dict) -> artifact_stage.PreparedControl:
    """Resolve only the original root-published operation-bound artifact intent."""
    require_root()
    if not isinstance(request,dict):
        raise ControlError('control_cutover_review_changed')
    operation=request.get('operation_id')
    # Validate identity before composing any protected path.
    if entry.operation_selection(operation) is None:
        raise ControlError('control_operation_reader_changed')
    original=read_protected_json(OPERATIONS/operation/'request.json')
    intent=read_protected_json(OPERATIONS/operation/'control-artifact.json')
    if (not isinstance(intent,dict) or set(intent) != {'schema_version','request_sha256','prepared'}
            or type(intent['schema_version']) is not int or intent['schema_version'] != 1
            or intent['request_sha256'] != _sha(original)
            or not isinstance(intent['prepared'],dict)
            or set(intent['prepared']) != set(artifact_stage.PreparedControl.__dataclass_fields__)):
        raise ControlError('control_cutover_review_changed')
    prepared=artifact_stage.PreparedControl(**intent['prepared'])
    artifact_stage.require_cached(prepared)
    return prepared


def _write(name: str, raw: bytes) -> None:
    modes={'provision':0o755,'selected.json':0o644}
    if name not in modes or not isinstance(raw,bytes) or not 0 < len(raw) <= 2*1024*1024:
        raise ControlError('control_cutover_destination_invalid')
    parent=root_directory(entry.CONTROL)
    temporary='.control-cutover-'+uuid.uuid4().hex
    try:
        entry._protected(entry.CONTROL/name)
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
        with os.fdopen(descriptor,'wb') as output:
            os.fchmod(output.fileno(),modes[name])
            output.write(raw);output.flush();os.fsync(output.fileno())
        os.replace(temporary,name,src_dir_fd=parent,dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        # Interrupted temporary evidence stays protected; no unrelated cleanup.
        os.close(parent)


def activate(prepared: artifact_stage.PreparedControl, grant: dict) -> dict:
    """Publish only the reviewed control identity while the service stays stopped.

    Requires the root-published artifact intent, original pinned UID reader,
    current canonical controller and live single-use resumption issuer. Root
    consumes only that reader's finite OS decision, never canonical state files.
    """
    require_root()
    if (not isinstance(grant,dict) or type(grant.get('schema_version')) is not int
            or grant['schema_version'] != 3 or not isinstance(grant.get('request'),dict)):
        raise ControlError('control_cutover_review_changed')
    request=grant['request']
    if request.get('initial_service_state') != 'running':
        raise ControlError('control_cutover_review_changed')
    return _publish(prepared,request,grant.get('source_request_sha256'),
        lambda old:qualify(grant,control_manifest_sha256=old['manifest_sha256']),
        expected_stage='recovery_required',allow_previous=True)


def activate_stopped(prepared: artifact_stage.PreparedControl, request: dict) -> dict:
    """Original stopped maintenance has scope permission, never start permission."""
    require_root()
    if not isinstance(request,dict) or request.get('initial_service_state') != 'stopped':
        raise ControlError('control_cutover_review_changed')
    from .control_permission import require_initial
    return _publish(prepared,request,_sha(request),lambda old:require_initial(request),
        expected_stage='prepared',allow_previous=False)


def activate_stopped_recovery(prepared: artifact_stage.PreparedControl, grant: dict) -> dict:
    """Rebound originally-stopped maintenance still grants no service start."""
    require_root()
    if not isinstance(grant,dict) or not isinstance(grant.get('request'),dict):
        raise ControlError('control_cutover_review_changed')
    from .control_permission import require_stopped_recovery
    return _publish(prepared,grant['request'],grant.get('source_request_sha256'),
        lambda old:require_stopped_recovery(grant),expected_stage='recovery_required',allow_previous=True)


def _publish(prepared,request,source_request_sha256,authorize,*,expected_stage,allow_previous):
    """Shared fixed file effect; public wrappers choose the live proof protocol."""
    require_root()
    operation=request.get('operation_id')
    if (request.get('action') != 'provision_control' or request.get('activation_mode') != 'cutover'
            or request.get('initial_service_state') not in {'running','stopped'}):
        raise ControlError('control_cutover_review_changed')
    pin=entry.operation_selection(operation)
    if pin is None or pin['manifest_sha256'] != request.get('controller_control_manifest_sha256'):
        raise ControlError('control_operation_reader_changed')
    old={name:pin[name] for name in ('schema_version','bundle_id','manifest_sha256')}
    previous=request.get('previous',{});candidate=request.get('candidate',{})
    if (previous.get('control_bundle_id') != old['bundle_id']
            or previous.get('control_manifest_sha256') != old['manifest_sha256']):
        raise ControlError('control_operation_reader_changed')
    old_bundle,old_manifest,_=entry.verify_bundle(old)
    verified=artifact_stage.require_cached(prepared)
    old_units_raw=entry._read(old_bundle/'lib/lifecycle/control/units.py',32768)
    artifacts.require_upgrade_support(old_manifest,verified,previous_units=old_units_raw)
    if (candidate.get('control_bundle_id') != verified.bundle_id
            or candidate.get('control_manifest_sha256') != verified.manifest_sha256
            or candidate.get('control_protocol_version') != 1):
        raise ControlError('control_prepared_version_changed')
    intent=read_protected_json(OPERATIONS/operation/'control-artifact.json')
    original=read_protected_json(OPERATIONS/operation/'request.json')
    if (_sha(original) != source_request_sha256
            or intent != {'schema_version':1,'request_sha256':_sha(original),'prepared':asdict(prepared)}):
        raise ControlError('control_cutover_review_changed')
    prefix=artifacts.CONTROL+'versions/'+verified.bundle_id+'/'
    from .unit_payload import parse, compatible
    old_units=parse(old_units_raw)
    new_units=parse(verified.files[prefix+'lib/lifecycle/control/units.py'])
    compatible(old_units,new_units)
    old_entry=entry._read(old_bundle/'lib/lifecycle/control/entry.py')
    new_entry=verified.files[artifacts.CONTROL+'provision']
    new={'schema_version':1,'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256}

    def observe_once():
        permission=authorize(old)
        if permission['issuer']['owner']['pid'] != os.getpid():
            raise ControlError('control_resumption_issuer_changed')
        require_stopped();_verify_owned_units(expected=old_units,alternate=new_units)
        actor=operation_unit(operation)
        if actor.get('ActiveState') != 'active' or int(actor.get('MainPID','0')) <= 1:
            raise ControlError('control_controller_changed')
        result=run_worker(['bridge-observe'],payload={'operation_id':operation,
            'controller_pid':int(actor['MainPID']),'controller_invocation_id':actor['InvocationID']},
            reader_operation_id=operation)
        plan=permission.get('plan')
        if (result.get('request_sha256') != _sha(request)
                or result.get('generation') != request.get('expected_generation',-1)+1
                or result.get('controller_pid') != int(actor['MainPID'])
                or result.get('controller_invocation_id') != actor['InvocationID']
                or result.get('stage') != expected_stage or result.get('os_effect_requested') is not True):
            raise ControlError('control_cutover_review_changed')
        if plan is not None and (plan['schema_version'] != 3 or plan['action'] != 'provision_control'
                or result.get('generation') != plan['generation']
                or result.get('selected_execution_sha256') != plan['stages'][0]['execution_sha256']):
            raise ControlError('control_cutover_review_changed')
        target=result.get('selected_control')
        allowed=[{name:value.get(name) for name in ('control_bundle_id','control_manifest_sha256','control_protocol_version')}
            for value in (previous,candidate)]
        if (target not in allowed or not allow_previous and target != allowed[1]
                or plan is not None and target['control_manifest_sha256'] != plan['stages'][0]['control_manifest_sha256']):
            raise ControlError('control_cutover_review_changed')
        return result, new if target==allowed[1] else old

    def observe():
        # A concurrent controller checkpoint can invalidate a speculative UID
        # read. Repeat the entire live issuer/OS/UID proof, never just a failed
        # component or the file write. The same reviewed permission is retained;
        # this bounded observation grants no renewal or service start.
        deadline=time.monotonic()+2
        while True:
            try:return observe_once()
            except ControlError as exc:
                if exc.code not in {'control_worker_failed','control_controller_changed'} or time.monotonic()>=deadline:
                    raise
                time.sleep(.05)

    observation,target=observe()
    if target==new:
        artifact_stage.require_prepared(prepared)
    pointer=entry._decode(entry._read(entry.CONTROL/'selected.json',4096))
    if pointer not in (old,new) or entry._read(entry.CONTROL/'provision') not in (old_entry,new_entry):
        raise ControlError('control_cutover_files_changed')
    receipt={'schema_version':1,'operation_id':operation,'generation':observation['generation'],
        'request_sha256':_sha(request),'controller_invocation_id':observation['controller_invocation_id'],
        'previous':old,'candidate':new,'target':target,'prepared':asdict(prepared),'stage':'applying'}
    path=OPERATIONS/operation/('control-cutover-'+str(observation['generation'])+'.json')
    if path.exists() or path.is_symlink():
        if read_protected_json(path) not in (receipt,{**receipt,'stage':'complete'}):
            raise ControlError('control_cutover_receipt_changed')
    else:atomic_root_json(path,receipt,mode=0o600)
    target_units=new_units if target==new else old_units
    # Durable intent precedes all unit changes. A fresh canonical recovery may
    # reconcile any finite old/new mixture, but no unknown or missing bytes.
    for name,raw in sorted(target_units.items()):
        if old_units[name] == new_units[name]:
            continue
        if observe() != (observation,target):
            raise ControlError('control_cutover_review_changed')
        if entry._read(Path(name),32768) != raw:
            atomic_root_bytes(Path(name),raw,mode=0o644)
    # Reload after a replay as well: a crash may have published every file but
    # occurred before the manager reloaded it. No-op maintenance needs no reload.
    if old_units != new_units:
        if observe() != (observation,target):
            raise ControlError('control_cutover_review_changed')
        systemctl('daemon-reload')
    _verify_owned_units(expected=target_units)
    for name,raw in (('provision',new_entry if target==new else old_entry),
            ('selected.json',(json.dumps(target,sort_keys=True,separators=(',',':'))+'\n').encode())):
        if observe() != (observation,target):
            raise ControlError('control_cutover_review_changed')
        _write(name,raw)
    if observe() != (observation,target):
        raise ControlError('control_cutover_review_changed')
    entry.verify_bundle(target)
    atomic_root_json(path,{**receipt,'stage':'complete'},mode=0o600)
    return target
