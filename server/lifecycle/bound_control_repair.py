"""UID exact-control review and original MANAGEMENT maintenance custody."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time

from . import management_journal as contract, management_recovery_store as recovery, management_store, state
from .layout import layout_for_root
from .control import artifact_stage, artifacts, control_reconstruction, entry, recovery_requests
from .control.processes import controller_peer, require_service_quiescent
from .control.requests import read_management_request
from .control.rpc import ControlError
from .control.stopped_repair import require_stopped


ROOT=Path('/opt/streamhome')


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _artifact(value: dict) -> artifacts.VerifiedArtifact:
    if not isinstance(value,dict) or set(value)!=set(artifact_stage.PreparedControl.__dataclass_fields__):
        raise ControlError('control_reconstruction_identity_changed')
    contract._uuid(value['bundle_id'],'bound control')
    for name in ('manifest_sha256','artifact_sha256','entry_sha256'):
        contract._text(value[name],contract._SHA256,name)
    cache=entry.CONTROL/'artifacts'/value['artifact_sha256']
    current=Path('/')
    entry._protected(current,directory=True)
    for part in cache.parts[1:]:
        current/=part;entry._protected(current,directory=True)
    verified=artifacts.verify_local(cache/'streamhome-control-bootstrap.deb')
    if value!={'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256,
            'artifact_sha256':verified.artifact_sha256,'entry_sha256':verified.entry_sha256}:
        raise ControlError('control_reconstruction_identity_changed')
    selection={'schema_version':1,'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256}
    rescue,_,_=entry.verify_bundle(selection,rescue=True)
    prefix=artifacts.CONTROL+'versions/'+verified.bundle_id+'/'
    if entry._read(rescue/'manifest.json')!=verified.files[prefix+'manifest.json']:
        raise ControlError('control_reconstruction_identity_changed')
    return verified


def plan(operation_id: str, artifact: dict, *, control_manifest_sha256: str) -> dict:
    layout=layout_for_root(ROOT)
    if (os.geteuid()==0 or not layout.managed
            or layout.descriptor.descriptor.service_uid!=os.geteuid()):
        raise ControlError('control_worker_forbidden')
    contract._uuid(operation_id,'bound operation')
    require_stopped();require_service_quiescent(inspections=True)
    authority=state.inspect_state(ROOT)
    operation=authority.active_operation
    if (operation is not None and operation.operation_id==operation_id
            and operation.operation_type in {state.OperationType.UPDATE,state.OperationType.RESTORE}):
        from .bound_control_parent_review import plan as parent_plan
        return parent_plan(ROOT,operation_id,artifact,control_manifest_sha256=control_manifest_sha256)
    if (operation is None or operation.operation_id!=operation_id
            or operation.operation_type is not state.OperationType.MANAGEMENT):
        raise ControlError('control_reconstruction_parent_unavailable')
    base=recovery_requests.review(operation_id,control_manifest_sha256=control_manifest_sha256)
    original=read_management_request(operation_id)
    journal=management_store.inspect_management_journal(ROOT,operation_id)
    if journal.installation_id!=layout.descriptor.descriptor.installation_id:
        raise ControlError('control_reconstruction_parent_unavailable')
    effective,_=recovery._effective_journal(ROOT,authority,journal)
    selected=contract.selected_execution(effective)
    if original.previous.control_manifest_sha256!=control_manifest_sha256:
        raise ControlError('control_operation_reader_changed')
    verified=_artifact(artifact)
    selection={'schema_version':1,'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256}
    roles=[name for name,execution in (('controller',original.previous),('runtime',selected))
        if execution.control_bundle_id==verified.bundle_id and execution.control_manifest_sha256==verified.manifest_sha256
            and execution.control_protocol_version==1]
    if not roles:
        raise ControlError('control_reconstruction_identity_changed')
    maintenance=recovery.inspect_recovery(ROOT,operation_id,authority.generation,
        maintenance_action=recovery.CONTROL_MAINTENANCE_ACTION)
    filesystem=control_reconstruction.fingerprint(entry.CONTROL/'versions'/verified.bundle_id)
    value={**base,'permitted_actions':[recovery.CONTROL_MAINTENANCE_ACTION],
        'source_evidence_sha256':maintenance['evidence_sha256'],'artifact':dict(artifact),
        'selection':selection,'roles':roles,'filesystem_sha256':filesystem,'read_only':True,
        'controller_selection':{'schema_version':1,'bundle_id':original.previous.control_bundle_id,
            'manifest_sha256':original.previous.control_manifest_sha256}}
    value.pop('evidence_sha256')
    require_stopped();require_service_quiescent(inspections=True)
    if (state.inspect_state(ROOT)!=authority
            or management_store.inspect_management_journal(ROOT,operation_id)!=journal
            or recovery_requests.review(operation_id,control_manifest_sha256=control_manifest_sha256)!=base):
        raise ControlError('control_recovery_review_stale')
    return {**value,'evidence_sha256':_digest(value)}


def _persist_once(path: Path, value: dict) -> None:
    if path.exists() or path.is_symlink():
        if recovery._read(path)!=value:
            raise ControlError('control_reconstruction_review_changed')
        return
    state._atomic_write_json(path,value)


def prepare(payload: dict,*,control_manifest_sha256: str) -> dict:
    fields={'operation_id','artifact','expected_evidence_sha256','recovery_review_id','issuer_scope','boot_id','stop_epoch'}
    if not isinstance(payload,dict) or set(payload)!=fields:
        raise ControlError('control_request_invalid')
    current=plan(payload['operation_id'],payload['artifact'],control_manifest_sha256=control_manifest_sha256)
    if current['evidence_sha256']!=payload['expected_evidence_sha256']:
        raise ControlError('control_recovery_review_stale')
    if current.get('operation_type') in {'update','restore'}:
        from .bound_control_parent_review import prepare as parent_prepare
        from .control.control_parent_requests import wrap
        return wrap(parent_prepare(ROOT,payload,control_manifest_sha256=control_manifest_sha256))
    original=read_management_request(payload['operation_id'])
    request=replace(original,expected_generation=current['generation'],backend=None,
        boot_id=payload['boot_id'],stop_epoch=payload['stop_epoch'])
    return recovery_requests.parse_grant({'schema_version':5,'action':recovery.CONTROL_MAINTENANCE_ACTION,
        'request':request.payload(),'source_request_sha256':original.sha256,
        'source_evidence_sha256':current['source_evidence_sha256'],'recovery_review_id':payload['recovery_review_id'],
        'repair_review_sha256':current['evidence_sha256'],'filesystem_sha256':current['filesystem_sha256'],
        'artifact':dict(payload['artifact']),'selection':current['selection'],'issuer_scope':payload['issuer_scope'],
        'expires_at_monotonic':time.monotonic()+900})


def _intent(grant: dict,generation: int) -> dict:
    request=grant['request']
    if generation!=request['expected_generation']+1:
        raise ControlError('control_reconstruction_review_changed')
    return {'schema_version':1,'operation_id':request['operation_id'],'generation':generation,
        'action':recovery.CONTROL_MAINTENANCE_ACTION,'grant_sha256':_digest(grant),
        'selection':grant['selection'],'artifact':grant['artifact'],'filesystem_sha256':grant['filesystem_sha256']}


def _current(operation_id: str,*,control_manifest_sha256: str):
    layout=layout_for_root(ROOT)
    if not layout.managed or os.geteuid()==0 or layout.descriptor.descriptor.service_uid!=os.geteuid():
        raise ControlError('control_worker_forbidden')
    authority=state.inspect_state(ROOT);operation=authority.active_operation
    if (operation is None or operation.operation_id!=operation_id
            or operation.operation_type is not state.OperationType.MANAGEMENT
            or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED):
        raise ControlError('control_reconstruction_parent_unavailable')
    journal=management_store.inspect_management_journal(ROOT,operation_id)
    if journal.installation_id!=layout.descriptor.descriptor.installation_id:
        raise ControlError('control_reconstruction_parent_unavailable')
    contract.validate_active_journal(journal,authority)
    grant=recovery_requests.read_grant(operation_id,authority.generation)
    if (grant['schema_version']!=5 or grant['action']!=recovery.CONTROL_MAINTENANCE_ACTION
            or grant['request']['controller_control_manifest_sha256']!=control_manifest_sha256):
        raise ControlError('control_operation_reader_changed')
    intent=recovery.inspect_takeover(ROOT,operation_id,authority.generation)
    if (intent.action!=recovery.CONTROL_MAINTENANCE_ACTION or intent.target_journal!=journal
            or intent.review_id!=grant['recovery_review_id']):
        raise ControlError('control_reconstruction_review_changed')
    original=read_management_request(operation_id)
    target=grant['selection']
    if not any(target=={'schema_version':1,'bundle_id':item.control_bundle_id,'manifest_sha256':item.control_manifest_sha256}
            for item in (original.previous,contract.selected_execution(journal))):
        raise ControlError('control_reconstruction_identity_changed')
    _artifact(grant['artifact'])
    _,path=management_store._journal_path(ROOT,operation_id)
    planned=_intent(grant,authority.generation)
    if recovery._read(path.parent/f'control-repair-intent-{authority.generation}.json')!=planned:
        raise ControlError('control_reconstruction_review_changed')
    return authority,journal,grant,planned,path.parent


def observe(payload: dict,*,control_manifest_sha256: str) -> dict:
    fields={'operation_id','controller_pid','controller_invocation_id'}
    if (not isinstance(payload,dict) or set(payload)!=fields or type(payload['controller_pid']) is not int
            or payload['controller_pid']<=1):raise ControlError('control_request_invalid')
    if _parent_owner(payload['operation_id']):
        from .bound_control_parent_repair import observe as parent_observe
        return parent_observe(ROOT,payload,control_manifest_sha256=control_manifest_sha256)
    authority,journal,grant,intent,_=_current(payload['operation_id'],control_manifest_sha256=control_manifest_sha256)
    lease=state.read_controller_lease(ROOT)
    if (lease is None or not state.controller_lease_is_current(lease) or lease.pid!=payload['controller_pid']
            or lease.operation_id!=journal.operation_id or lease.generation!=journal.generation
            or journal.controller_invocation_id!=payload['controller_invocation_id']):
        raise ControlError('control_controller_changed')
    controller_peer(lease.pid,journal.operation_id)
    state.validate_operation_with_credential(ROOT,operation_id=journal.operation_id,generation=journal.generation,
        expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
    require_stopped();require_service_quiescent(inspections=True,
        controller=(journal.operation_id,lease.pid,lease.process_start_identity))
    if (state.inspect_state(ROOT)!=authority or management_store.inspect_management_journal(ROOT,journal.operation_id)!=journal
            or not state.controller_lease_is_current(lease)):
        raise ControlError('control_controller_changed')
    return {'operation_id':journal.operation_id,'generation':journal.generation,'intent':intent,
        'intent_sha256':_digest(intent),'controller_pid':lease.pid,'controller_invocation_id':journal.controller_invocation_id,
        'stage':'control_repair_requested'}


def result(operation_id: str,*,control_manifest_sha256: str) -> dict:
    if _parent_owner(operation_id):
        from .bound_control_parent_repair import result as parent_result
        return parent_result(ROOT,operation_id,control_manifest_sha256=control_manifest_sha256)
    from .control.tickets import read_protected_json
    authority,journal,grant,intent,directory=_current(operation_id,control_manifest_sha256=control_manifest_sha256)
    require_stopped();require_service_quiescent(inspections=True)
    effect=read_protected_json(entry.OPERATIONS/operation_id/f'control-repair-effect-{journal.generation}.json')
    if (not isinstance(effect,dict) or set(effect)!={'schema_version','intent_sha256','effect'}
            or type(effect['schema_version']) is not int or effect['schema_version']!=1
            or effect['intent_sha256']!=_digest(intent) or not isinstance(effect['effect'],dict)
            or effect['effect'].get('stage')!='complete'
            or effect['effect'].get('selection')!=intent['selection']
            or effect['effect'].get('artifact_sha256')!=intent['artifact']['artifact_sha256']
            or effect['effect'].get('filesystem_sha256')!=intent['filesystem_sha256']):
        raise ControlError('control_reconstruction_review_changed')
    expected={**intent,'effect_sha256':_digest(effect),'status':'complete'}
    if recovery._read(directory/f'control-repair-{journal.generation}.json')!=expected:
        raise ControlError('control_reconstruction_review_changed')
    entry.verify_bundle(grant['selection'])
    if state.inspect_state(ROOT)!=authority or management_store.inspect_management_journal(ROOT,operation_id)!=journal:
        raise ControlError('control_reconstruction_review_changed')
    return {'completed':True,'stage':'control_repaired_recovery_required','generation':journal.generation,
        'grant_sha256':_digest(grant),'selection':grant['selection']}


def _parent_owner(operation_id):
    """Route only after the same early UID gate as the management projection."""
    layout=layout_for_root(ROOT)
    if not layout.managed or os.geteuid()==0 or layout.descriptor.descriptor.service_uid!=os.geteuid():
        raise ControlError('control_worker_forbidden')
    operation=state.inspect_state(ROOT).active_operation
    return (operation is not None and operation.operation_id==operation_id
        and operation.operation_type in {state.OperationType.UPDATE,state.OperationType.RESTORE})


class BoundControlController:
    """Original MANAGEMENT maintenance fence while root restores exact code.

    The fixed rescue entry consumes a finite protected control-repair grant.
    Capability advertisement remains closed until all required owners qualify.
    """
    def __init__(self,grant: dict,*,control_manifest_sha256: str,checkpoint):
        layout=layout_for_root(ROOT)
        if not layout.managed or os.geteuid()==0 or layout.descriptor.descriptor.service_uid!=os.geteuid():
            raise ControlError('control_controller_forbidden')
        self.grant=recovery_requests.parse_grant(grant)
        from .control.requests import parse_management_request
        self.request=parse_management_request(self.grant['request'])
        if (layout.descriptor.descriptor.installation_id!=self.request.installation_id
                or self.request.controller_control_manifest_sha256!=control_manifest_sha256):
            raise ControlError('control_controller_forbidden')
        reader=entry.CONTROL/'rescue-versions'/self.request.previous.control_bundle_id/'lib/lifecycle/bound_control_repair.py'
        if (self.request.previous.control_manifest_sha256!=control_manifest_sha256 or Path(__file__)!=reader):
            raise ControlError('control_reconstruction_independent_reader_required')
        controller_peer(os.getpid(),self.request.operation_id)
        self.invocation=contract._text(os.environ.get('INVOCATION_ID',''),contract._INVOCATION,'controller invocation')
        if self.invocation=='0'*32:raise ControlError('control_controller_forbidden')
        self.permission_checkpoint=checkpoint
        self.lease=None

    def checkpoint(self):
        self.permission_checkpoint()
        if recovery_requests.read_grant(self.request.operation_id,self.request.expected_generation+1)!=self.grant:
            raise ControlError('control_reconstruction_review_changed')
        if self.lease is not None:self.lease.require_current()

    def execute(self):
        from .control.requests import parse_management_request
        from .control.tickets import read_protected_json
        if (self.grant.get('schema_version')!=5 or type(self.grant['schema_version']) is not int
                or self.grant.get('action')!=recovery.CONTROL_MAINTENANCE_ACTION):
            raise ControlError('control_recovery_request_invalid')
        request=parse_management_request(self.grant['request'])
        self.checkpoint()
        source=plan(request.operation_id,self.grant['artifact'],
            control_manifest_sha256=request.controller_control_manifest_sha256)
        if (source['evidence_sha256']!=self.grant['repair_review_sha256']
                or source['filesystem_sha256']!=self.grant['filesystem_sha256']
                or source['selection']!=self.grant['selection']
                or source['source_evidence_sha256']!=self.grant['source_evidence_sha256']):
            raise ControlError('control_reconstruction_review_changed')
        def recheck():
            self.checkpoint()
            if plan(request.operation_id,self.grant['artifact'],
                    control_manifest_sha256=request.controller_control_manifest_sha256)!=source:
                raise ControlError('control_reconstruction_review_changed')
        self.lease,_,journal=recovery.acquire_recovery_lease(ROOT,operation_id=request.operation_id,
            generation=request.expected_generation,evidence_sha256=self.grant['source_evidence_sha256'],
            review_id=self.grant['recovery_review_id'],action=recovery.CONTROL_MAINTENANCE_ACTION,
            controller_invocation_id=self.invocation,pre_transfer_check=recheck)
        try:
            _,journal_path=management_store._journal_path(ROOT,request.operation_id)
            intent=_intent(self.grant,journal.generation)
            _persist_once(journal_path.parent/f'control-repair-intent-{journal.generation}.json',intent)
            effect_path=entry.OPERATIONS/request.operation_id/f'control-repair-effect-{journal.generation}.json'
            deadline=time.monotonic()+90
            while True:
                self.checkpoint()
                try:effect=read_protected_json(effect_path)
                except ControlError as exc:
                    if exc.code!='control_ticket_unavailable':raise
                else:
                    if (not isinstance(effect,dict) or set(effect)!={'schema_version','intent_sha256','effect'}
                            or type(effect['schema_version']) is not int or effect['schema_version']!=1
                            or effect['intent_sha256']!=_digest(intent) or not isinstance(effect['effect'],dict)):
                        raise ControlError('control_reconstruction_review_changed')
                    details=effect['effect']
                    if (details.get('selection')!=source['selection'] or details.get('artifact_sha256')!=source['artifact']['artifact_sha256']
                            or details.get('filesystem_sha256')!=source['filesystem_sha256']):
                        raise ControlError('control_reconstruction_review_changed')
                    if details.get('stage')=='complete':break
                    if details.get('stage') not in {'rename_planned','reconstructing'}:
                        raise ControlError('control_reconstruction_review_changed')
                if time.monotonic()>=deadline:raise ControlError('control_reconstruction_bridge_timeout')
                time.sleep(.1)
            require_stopped();require_service_quiescent(inspections=True);self.checkpoint()
            entry.verify_bundle(source['selection'])
            if (state.inspect_state(ROOT).active_operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
                    or management_store.inspect_management_journal(ROOT,request.operation_id)!=journal):
                raise ControlError('control_reconstruction_review_changed')
            result={**intent,'effect_sha256':_digest(effect),'status':'complete'}
            _persist_once(journal_path.parent/f'control-repair-{journal.generation}.json',result)
            return result
        finally:self.lease.release()


def run_controller(grant: dict,*,control_manifest_sha256: str) -> int:
    """Fixed operation entry; success intentionally leaves the parent unresolved."""
    cancelled=False
    def checkpoint():
        if cancelled:raise ControlError('control_operator_stopped')
    def cancel(_signal,_frame):
        nonlocal cancelled
        cancelled=True
    previous={value:signal.getsignal(value) for value in (signal.SIGTERM,signal.SIGINT)}
    try:
        for value in previous:signal.signal(value,cancel)
        actor=BoundControlController(grant,control_manifest_sha256=control_manifest_sha256,checkpoint=checkpoint)
        actor.execute()
        print('Exact control files were restored. The original operation still requires a fresh recovery review.')
        return 0
    except Exception as exc:
        code=getattr(exc,'code','control_reconstruction_failed')
        if not isinstance(code,str) or not re.fullmatch(r'[a-z_]{1,64}',code):code='control_reconstruction_failed'
        print('Exact control repair requires a fresh review: '+code)
        return 2
    finally:
        for value,handler in previous.items():signal.signal(value,handler)
