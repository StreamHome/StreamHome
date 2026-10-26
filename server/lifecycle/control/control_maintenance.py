"""Root control-maintenance review/producer; canonical work stays in UID readers.

The entry must declare qualified maintenance support before exposing this mode.
Preparation is inactive; a running service's stop ends the original scope and
requires a separate fresh recovery review before any replacement runtime start.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import time
import uuid

from ..configure_wizard import parse_request_json
from ..terminal import open_console
from . import artifact_stage, artifacts, entry
from .administration import (_boot_enabled, _identity, _sha, _verify_owned_units,
    process_snapshot, publish_operation_reader, publish_request, root_stop_epoch, run_operation)
from .control_activation import bind_prepared
from .control_permission import issue
from .privileged import descriptor, require_root, run_worker
from .rpc import ControlError
from .stopped_repair import StoppedRepairHandoff, current_scope, require_scope, require_stopped


def _consent(request: dict, prepared, *, request_path: Path | None, require_live) -> bool:
    review={'schema_version':1,'request':request,'prepared':asdict(prepared)}
    digest=_sha(review)
    require_live()
    if request_path is None:
        with open_console() as console:
            console.write('StreamHome control maintenance')
            console.write('Current control: '+request['previous']['control_bundle_id'])
            console.write('Prepared control: '+prepared.bundle_id)
            console.write('Review SHA-256: '+digest)
            console.write('The service will stop. Starting the replacement requires a separate fresh recovery review.'
                if request['initial_service_state']=='running' else 'The service will stay stopped.')
            return console.confirm('Maintain the reviewed control components?')
    print(json.dumps({'control_review':review,'control_review_sha256':digest,'request_schema_version':1},sort_keys=True),flush=True)
    deadline=time.monotonic()+900
    while True:
        require_live()
        try:
            descriptor_fd=os.open(request_path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise ControlError('control_maintenance_review_timeout')
            time.sleep(.2);continue
        with os.fdopen(descriptor_fd,'rb') as source:
            metadata=os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32768:
                raise ControlError('control_request_invalid')
            value=parse_request_json(source.read(32769))
        if (not isinstance(value,dict) or set(value) != {'schema_version','expected_review_sha256','maintain_control','interruption_approved'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or any(type(value[name]) is not bool for name in ('maintain_control','interruption_approved'))):
            raise ControlError('control_request_invalid')
        if value['expected_review_sha256'] != digest:
            raise ControlError('control_maintenance_review_changed')
        return value['maintain_control'] and value['interruption_approved']


def maintain(package: Path, *, manifest: dict, manifest_sha256: str, request_path: Path | None = None,
             scope_epoch: str | None = None, operation_id: str | None = None,
             expected_installation_id: str | None = None):
    require_root()
    support=manifest.get('support',{})
    if (support.get('control_maintenance_versions') != [1]
            or any(type(number) is not int for number in support['control_maintenance_versions'])):
        raise ControlError('control_maintenance_not_available')
    selected=descriptor()
    if selected.status.value != 'ready':
        raise ControlError('control_installation_incomplete')
    snapshot=run_worker(['inspect'],timeout=120)
    if snapshot['lifecycle']['active_operation'] is not None:
        raise ControlError('control_operation_busy')
    previous=snapshot['execution']['execution']
    if (snapshot['installation_id'] != selected.installation_id
            or previous['control_bundle_id'] != manifest['bundle_id']
            or previous['control_manifest_sha256'] != manifest_sha256 or snapshot['runtime_verified'] is not True):
        raise ControlError('control_bundle_mismatch')
    old={'schema_version':1,'bundle_id':manifest['bundle_id'],'manifest_sha256':manifest_sha256}
    bundle,old_manifest,_=entry.verify_bundle(old)
    verified=artifacts.verify_local(package)
    old_units=entry._read(bundle/'lib/lifecycle/control/units.py',32768)
    artifacts.require_upgrade_support(old_manifest,verified,previous_units=old_units)
    if verified.bundle_id==old['bundle_id'] and verified.manifest_sha256==manifest_sha256:
        print('The verified control version is already selected. No maintenance is needed.',flush=True)
        return 0
    epoch=root_stop_epoch()
    if scope_epoch is not None and (scope_epoch != epoch or expected_installation_id != selected.installation_id):
        raise ControlError('control_operator_stopped')
    if scope_epoch is None:
        return StoppedRepairHandoff(selected.installation_id,str(uuid.uuid4()),epoch,request_path,control_artifact=package)
    operation=_identity(operation_id)
    scope=current_scope(selected.installation_id,epoch)
    processes=process_snapshot();enabled=_boot_enabled()
    if processes['initial_service_state']=='stopped':require_stopped()
    _verify_owned_units()
    prepared=artifact_stage.prepare_local(package,previous_manifest=old_manifest,previous_units=old_units)
    if asdict(prepared) != {'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256,
            'artifact_sha256':verified.artifact_sha256,'entry_sha256':verified.entry_sha256}:
        raise ControlError('control_artifact_changed')
    payload={**processes,'purpose':'maintain_control','operation_id':operation,'review_id':str(uuid.uuid4()),
        'expected_generation':snapshot['lifecycle']['generation'],
        'expected_desired_revision':snapshot['configuration']['deployment']['desired']['revision'],
        'deployment':snapshot['active_deployment'],'activate_deployment':True,
        'boot_enabled':enabled,'previous_boot_enabled':enabled,'interruption_approved':True,
        'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'stop_epoch':epoch,
        'control_candidate':{'control_bundle_id':prepared.bundle_id,'control_manifest_sha256':prepared.manifest_sha256,
            'control_protocol_version':1}}
    proposal=run_worker(['prepare-request'],payload=payload,timeout=120)
    if proposal.get('no_changes') is True:
        return 0
    request=proposal.get('request')
    expected_candidate={**previous,**payload['control_candidate']}
    if (not isinstance(request,dict) or request.get('operation_id') != operation
            or request.get('installation_id') != selected.installation_id or request.get('action') != 'provision_control'
            or request.get('activation_mode') != 'cutover' or request.get('candidate') != expected_candidate
            or request.get('previous') != previous or request.get('controller_control_manifest_sha256') != manifest_sha256
            or any(request.get(name) != payload[name] for name in ('expected_generation','boot_id','stop_epoch',
                'initial_service_state','supervisor','supervisor_invocation_id','backend','boot_enabled','previous_boot_enabled'))):
        raise ControlError('control_maintenance_review_changed')

    def live():
        require_scope(scope,selected.installation_id)
        if root_stop_epoch()!=epoch or _boot_enabled() is not enabled:
            raise ControlError('control_operator_stopped')

    if not _consent(request,prepared,request_path=request_path,require_live=live):
        print('Control maintenance was declined. The verified inactive package is retained.',flush=True)
        return 0
    live()
    if (run_worker(['inspect'],timeout=120) != snapshot or process_snapshot() != processes
            or entry._decode(entry._read(entry.CONTROL/'selected.json',4096)) != old):
        raise ControlError('control_maintenance_review_changed')
    artifact_stage.require_prepared(prepared)
    publish_request(request)
    publish_operation_reader(operation,bundle=bundle,manifest_sha256=manifest_sha256)
    bind_prepared(request,prepared)
    issue(request)
    run_operation(request,reader_operation_id=operation)
    return 0
