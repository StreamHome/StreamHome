"""Pure-root exact control repair bridge; canonical interpretation stays in UID workers."""
from __future__ import annotations

from dataclasses import asdict
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from ..configure_wizard import parse_request_json
from ..terminal import open_console
from . import artifact_stage, artifacts, control_reconstruction, entry, unit_payload
from .administration import OPERATIONS, _boot_enabled, _identity, _sha, _verify_owned_units, operation_unit, publish_operation_reader, root_stop_epoch
from .privileged import descriptor, require_root, run_worker, systemctl
from .resumption_issuer import current_issuer
from .root_files import atomic_root_json
from .rpc import ControlError
from .stopped_repair import StoppedRepairHandoff, current_scope, require_stopped
from .tickets import read_protected_json


ACTION='repair_bound_control'


def _inactive(operation):
    value=operation_unit(operation)
    if value['ActiveState'] not in {'inactive','failed'} or int(value['MainPID'])!=0:
        raise ControlError('control_controller_busy')
    from .stop import _require_empty
    _require_empty(f'streamhome-operation@{operation}.service')
    raw=systemctl('show',f'streamhome-operation@{operation}.service',
        '--property=LoadState,FragmentPath,User,DropInPaths,NeedDaemonReload')
    definition=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    if definition!={'LoadState':'loaded','FragmentPath':'/etc/systemd/system/streamhome-operation@.service',
            'User':'streamhome','DropInPaths':'','NeedDaemonReload':'no'}:
        raise ControlError('control_stop_unit_unqualified')


def _root_readers(operation):
    """Other fixed root readers must finish; never signal or name-kill them."""
    require_root()
    rows=list(Path('/proc').iterdir())
    if len(rows)>8192:raise ControlError('control_quiescence_unproven')
    prefix=[b'/usr/bin/python3',b'-I',b'/usr/local/libexec/streamhome/provision']
    for path in rows:
        if not path.name.isdigit() or int(path.name)==os.getpid():continue
        try:
            if path.stat().st_uid!=0:continue
            fields=(path/'stat').read_bytes().rsplit(b')',1)[1].split()
            if fields[0] in {b'Z',b'X'}:continue
            with (path/'cmdline').open('rb') as source:raw=source.read(8193)
            if len(raw)>8192:raise ControlError('control_quiescence_unproven')
            arguments=raw.rstrip(b'\0').split(b'\0')
            if prefix[2] not in arguments:continue
            if arguments[:3]!=prefix and arguments[:1]==[b'/usr/bin/systemd-run']:
                offset=arguments.index(prefix[2])
                command=arguments[offset-2:]
                if (str((path/'exe').readlink())=='/usr/bin/systemd-run'
                        and arguments[1:5]==[b'--scope',b'--quiet',b'--collect',b'--slice=system.slice']
                        and command[:4]==[*prefix,b'--repair-control-scoped'] and len(command)>=7
                        and command[5]==operation.encode()):
                    continue  # OS launcher holds no normal Python modules.
            tail=arguments[3:]
            normalized=[]
            for token in tail:
                key,separator,value=token.partition(b'=')
                normalized.extend([key,value] if separator and key in {b'--operation',b'--control-artifact',b'--request'} else [token])
            tail=normalized
            # Same-operation outer/inner repair dispatchers load physical rescue.
            outer=(tail[:1]==[b'--repair-control'] and tail.count(b'--operation')==1
                and tail.index(b'--operation')+1<len(tail) and tail[tail.index(b'--operation')+1]==operation.encode())
            inner=len(tail)>=4 and tail[0]==b'--repair-control-scoped' and tail[2]==operation.encode()
            public=(tail[:2]==[b'--cli',b'repair'] and b'--control-artifact' in tail and tail.count(b'--operation')==1
                and tail.index(b'--operation')+1<len(tail) and tail[tail.index(b'--operation')+1]==operation.encode())
            if arguments[:3]==prefix and (outer or inner or public):continue
            raise ControlError('control_root_reader_busy')
        except FileNotFoundError:continue
        except (OSError,IndexError) as exc:raise ControlError('control_quiescence_unproven') from exc


def _units(snapshot,reader):
    bundle,manifest,_=entry.verify_bundle(reader,rescue=True)
    raw=entry._read(bundle/'lib/lifecycle/control/units.py',32768)
    before=unit_payload.parse(raw);after=None
    allowed=[reader]
    if snapshot.get('management_action')=='provision_control' and snapshot.get('activation_mode')=='cutover':
        operation=snapshot['operation_id']
        original=read_protected_json(OPERATIONS/operation/'request.json')
        bound=read_protected_json(OPERATIONS/operation/'control-artifact.json')
        if (_sha(original)!=snapshot.get('source_request_sha256') or not isinstance(bound,dict)
                or set(bound)!={'schema_version','request_sha256','prepared'} or type(bound['schema_version']) is not int
                or bound['schema_version']!=1 or bound['request_sha256']!=_sha(original)
                or not isinstance(bound['prepared'],dict)
                or set(bound['prepared'])!=set(artifact_stage.PreparedControl.__dataclass_fields__)):
            raise ControlError('control_reconstruction_review_changed')
        prepared=artifact_stage.PreparedControl(**bound['prepared'])
        verified=artifact_stage.require_cached(prepared)
        artifacts.require_upgrade_support(manifest,verified,previous_units=raw)
        prefix=artifacts.CONTROL+'versions/'+verified.bundle_id+'/'
        after=unit_payload.parse(verified.files[prefix+'lib/lifecycle/control/units.py'])
        unit_payload.compatible(before,after)
        allowed.append({'schema_version':1,'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256})
    pointer=entry._decode(entry._read(entry.CONTROL/'selected.json',4096))
    if type(pointer.get('schema_version')) is not int or pointer not in allowed:
        raise ControlError('control_selected_reader_changed')
    _verify_owned_units(expected=before,alternate=after)


def _consent(snapshot,prepared,issuer,enabled,path,live):
    review={'schema_version':1,'review':snapshot,'prepared':asdict(prepared),
        'issuer_sha256':_sha(issuer),'boot_enabled':enabled}
    digest=_sha(review)
    live()
    if path is None:
        with open_console() as console:
            console.write('Repair only exact control files bound to operation '+snapshot['operation_id'])
            console.write('Control bundle: '+prepared.bundle_id)
            console.write('Manifest SHA-256: '+prepared.manifest_sha256)
            console.write('Review SHA-256: '+digest)
            console.write('Damaged code is preserved. The service stays stopped; the original operation remains unresolved.')
            return console.confirm('Restore these exact control files?')
    import json
    print(json.dumps({'control_repair_review':review,'control_repair_review_sha256':digest,'request_schema_version':1},sort_keys=True),flush=True)
    deadline=time.monotonic()+900
    while True:
        live()
        try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        except FileNotFoundError:
            if time.monotonic()>=deadline:raise ControlError('control_reconstruction_review_timeout')
            time.sleep(.2);continue
        with os.fdopen(fd,'rb') as source:
            metadata=os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>32768:raise ControlError('control_request_invalid')
            value=parse_request_json(source.read(32769))
        if (not isinstance(value,dict) or set(value)!={'schema_version','expected_review_sha256',ACTION}
                or type(value['schema_version']) is not int or value['schema_version']!=1 or type(value[ACTION]) is not bool):
            raise ControlError('control_request_invalid')
        if value['expected_review_sha256']!=digest:raise ControlError('control_reconstruction_review_changed')
        return value[ACTION]


def repair(operation_id: str,package: Path,*,bundle: Path,manifest: dict,manifest_sha256: str,
           request_path: Path | None=None,scope_epoch: str | None=None,expected_installation_id: str | None=None):
    require_root();operation=_identity(operation_id)
    support=manifest.get('support',{})
    if (support.get('bound_control_repair_versions')!=[1]
            or any(type(item) is not int for item in support['bound_control_repair_versions'])
            or ACTION not in support.get('maintenance_actions',[])):
        raise ControlError('control_bound_control_repair_unavailable')
    selected=descriptor()
    if selected.status.value!='ready':raise ControlError('control_installation_incomplete')
    reader={'schema_version':1,'bundle_id':manifest['bundle_id'],'manifest_sha256':manifest_sha256}
    actual,_,_=entry.verify_bundle(reader,rescue=True)
    if actual!=bundle:raise ControlError('control_reconstruction_independent_reader_required')
    require_stopped();_inactive(operation)
    epoch=root_stop_epoch()
    if scope_epoch is not None and (scope_epoch!=epoch or expected_installation_id!=selected.installation_id):
        raise ControlError('control_operator_stopped')
    if scope_epoch is None:
        return StoppedRepairHandoff(selected.installation_id,operation,epoch,request_path,
            recovery_action=ACTION,control_artifact=package)
    scope=current_scope(selected.installation_id,epoch)
    issuer=current_issuer(selected.installation_id,epoch);enabled=_boot_enabled()
    prepared=artifact_stage.cache_local(package)
    metadata=asdict(prepared)
    def review():
        value=run_worker(['bound-control-review',operation],payload={'artifact':metadata},timeout=180,control_repair_review=True)
        if (value.get('operation_id')!=operation or value.get('installation_id')!=selected.installation_id
                or value.get('artifact')!=metadata or value.get('controller_selection')!=reader
                or value.get('read_only') is not True or value.get('permitted_actions')!=[ACTION]
                or type(value.get('generation')) is not int or not 1<=value['generation']<2**63-1
                or value.get('selection')!={'schema_version':1,'bundle_id':prepared.bundle_id,'manifest_sha256':prepared.manifest_sha256}):
            raise ControlError('control_reconstruction_review_changed')
        return value
    snapshot=review()
    def live():
        require_stopped()
        if (root_stop_epoch()!=epoch or _boot_enabled() is not enabled
                or current_issuer(selected.installation_id,epoch)!=issuer):
            raise ControlError('control_operator_stopped')
        _units(snapshot,reader)
    live();_root_readers(operation)
    if not _consent(snapshot,prepared,issuer,enabled,request_path,live):
        print('Exact control repair was declined.');return 0
    live();_inactive(operation)
    if review()!=snapshot:raise ControlError('control_reconstruction_review_changed')
    publish_operation_reader(operation,bundle=actual,manifest_sha256=manifest_sha256,rescue=True)
    review_id=str(uuid.uuid4())
    grant=run_worker(['bound-control-prepare',operation],payload={'operation_id':operation,'artifact':metadata,
        'expected_evidence_sha256':snapshot['evidence_sha256'],'recovery_review_id':review_id,
        'issuer_scope':scope,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'stop_epoch':epoch},
        timeout=180,reader_operation_id=operation,reader_rescue=True)
    fields={'schema_version','action','request','source_request_sha256','source_evidence_sha256','recovery_review_id',
        'repair_review_sha256','filesystem_sha256','artifact','selection','issuer_scope','expires_at_monotonic'}
    if type(grant.get('schema_version')) is int and grant['schema_version']==6:
        from .control_parent_requests import parse
        parse(grant)
        fields.add('owner_grant')
    request=grant.get('request');expiry=grant.get('expires_at_monotonic')
    if (set(grant)!=fields or type(grant.get('schema_version')) is not int or grant['schema_version'] not in {5,6}
            or grant.get('action')!=ACTION or not isinstance(request,dict) or request.get('operation_id')!=operation
            or request.get('installation_id')!=selected.installation_id or type(request.get('expected_generation')) is not int
            or request['expected_generation']!=snapshot['generation'] or request.get('controller_control_manifest_sha256')!=manifest_sha256
            or request.get('stop_epoch')!=epoch or request.get('boot_id')!=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or grant.get('source_request_sha256')!=snapshot.get('source_request_sha256')
            or grant.get('source_evidence_sha256')!=snapshot['source_evidence_sha256']
            or grant.get('repair_review_sha256')!=snapshot['evidence_sha256'] or grant.get('artifact')!=metadata
            or grant.get('selection')!=snapshot['selection'] or grant.get('filesystem_sha256')!=snapshot['filesystem_sha256']
            or grant.get('recovery_review_id')!=review_id or grant.get('issuer_scope')!=scope
            or type(expiry) not in {int,float} or not math.isfinite(expiry) or not time.monotonic()<expiry<=time.monotonic()+900):
        raise ControlError('control_reconstruction_review_changed')
    live();_inactive(operation)
    directory=OPERATIONS/operation;generation=snapshot['generation']+1
    role_path=directory/'control-repair-reader.json'
    if role_path.exists() or role_path.is_symlink():
        old=read_protected_json(role_path)
        if (not isinstance(old,dict) or set(old)!={'schema_version','operation_id','selection','generation','grant_sha256','stage'}
                or type(old.get('schema_version')) is not int or old['schema_version']!=1
                or old.get('operation_id')!=operation or not isinstance(old.get('selection'),dict)
                or type(old['selection'].get('schema_version')) is not int or old['selection']!=reader
                or type(old.get('generation')) is not int or not 2<=old['generation']<=generation
                or not isinstance(old.get('grant_sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',old['grant_sha256'])
                or not isinstance(old.get('stage'),str) or old['stage'] not in {'active','complete'}):
            raise ControlError('control_reconstruction_review_changed')
    retained=directory/('recovery-review-'+review_id+'.json')
    if retained.exists() or retained.is_symlink():raise ControlError('control_recovery_request_changed')
    native=None
    if grant['schema_version']==6 and grant['owner_grant']['operation_type']=='update':
        from .update_grants import parse_grant
        native=grant['owner_grant']
        native_retained=directory/('update-review-'+native['review_id']+'.json')
        if native_retained.exists() or native_retained.is_symlink():raise ControlError('control_recovery_request_changed')
        native_pointer=directory/f'update-recovery-{generation}.json'
        if native_pointer.exists() or native_pointer.is_symlink():
            previous=parse_grant(read_protected_json(native_pointer))
            if (previous['operation_id']!=operation or previous['generation']+1!=generation
                    or read_protected_json(directory/('update-review-'+_identity(previous['review_id'])+'.json'))!=previous):
                raise ControlError('control_recovery_request_changed')
    atomic_root_json(retained,grant,gid=selected.service_gid,mode=0o640)
    pointer=directory/f'recovery-{generation}.json'
    if pointer.exists() or pointer.is_symlink():
        previous=read_protected_json(pointer);prior=_identity(previous.get('recovery_review_id'))
        if read_protected_json(directory/('recovery-review-'+prior+'.json'))!=previous:
            raise ControlError('control_recovery_request_changed')
    atomic_root_json(pointer,grant,gid=selected.service_gid,mode=0o640)
    if native is not None:
        atomic_root_json(native_retained,native,gid=selected.service_gid,mode=0o640)
        atomic_root_json(native_pointer,native,gid=selected.service_gid,mode=0o640)
    role={'schema_version':1,'operation_id':operation,'selection':reader,'generation':generation,'grant_sha256':_sha(grant),'stage':'active'}
    atomic_root_json(role_path,role,mode=0o644)
    live();systemctl('start',f'streamhome-operation@{operation}.service',timeout=30)
    effect_permission_sha256=_sha(grant['owner_grant'] if grant['schema_version']==6 else grant)
    expected_intent={'schema_version':1,'operation_id':operation,'generation':generation,'action':ACTION,
        'grant_sha256':effect_permission_sha256,'selection':snapshot['selection'],'artifact':metadata,'filesystem_sha256':snapshot['filesystem_sha256']}
    deadline=time.monotonic()+180;effect_done=False
    def observation():
        stop=time.monotonic()+2
        while True:
            live();_root_readers(operation)
            actor=operation_unit(operation)
            if actor['ActiveState']!='active' or int(actor['MainPID'])<=1:raise ControlError('control_controller_changed')
            try:
                value=run_worker(['bound-control-observe',operation],payload={'operation_id':operation,
                    'controller_pid':int(actor['MainPID']),'controller_invocation_id':actor['InvocationID']},timeout=120,
                    reader_operation_id=operation,reader_rescue=True)
            except ControlError as exc:
                if exc.code not in {'control_worker_failed','control_controller_changed'} or time.monotonic()>=stop:raise
                time.sleep(.05);continue
            if (value.get('operation_id')!=operation or value.get('generation')!=generation or value.get('intent')!=expected_intent
                    or value.get('intent_sha256')!=_sha(expected_intent) or value.get('controller_pid')!=int(actor['MainPID'])
                    or value.get('controller_invocation_id')!=actor['InvocationID'] or value.get('stage')!='control_repair_requested'):
                raise ControlError('control_reconstruction_review_changed')
            return value
    while time.monotonic()<deadline:
        live();actor=operation_unit(operation)
        if actor['ActiveState']=='active' and int(actor['MainPID'])>1 and not effect_done:
            try:baseline=observation()
            except ControlError as exc:
                if exc.code not in {'control_worker_failed','control_controller_changed'}:raise
            else:
                def checkpoint():
                    if observation()!=baseline:raise ControlError('control_reconstruction_review_changed')
                def record(value):
                    checkpoint()
                    atomic_root_json(directory/f'control-repair-effect-{generation}.json',
                        {'schema_version':1,'intent_sha256':_sha(expected_intent),'effect':value},gid=selected.service_gid,mode=0o640)
                cached=entry.CONTROL/'artifacts'/prepared.artifact_sha256/'streamhome-control-bootstrap.deb'
                artifact_stage.require_cached(prepared)
                control_reconstruction.reconstruct(cached,snapshot['selection'],expected_artifact_sha256=prepared.artifact_sha256,
                    expected_fingerprint=snapshot['filesystem_sha256'],checkpoint=checkpoint,quiescent=checkpoint,record=record)
                effect_done=True
        if actor['ActiveState'] in {'inactive','failed'}:
            _inactive(operation)
            result=run_worker(['bound-control-result',operation],timeout=180,reader_operation_id=operation,reader_rescue=True)
            if (not effect_done or result!={'completed':True,'stage':'control_repaired_recovery_required',
                    'generation':generation,'grant_sha256':effect_permission_sha256,'selection':snapshot['selection']}):
                raise ControlError('control_operation_recovery_required')
            live();atomic_root_json(role_path,{**role,'stage':'complete'},mode=0o644)
            print('Exact control files were restored. The service is stopped and the original operation remains unresolved.')
            print('Request a fresh recovery review: sudo streamhome recover --operation '+operation)
            return 0
        time.sleep(.1)
    raise ControlError('control_reconstruction_bridge_timeout')
