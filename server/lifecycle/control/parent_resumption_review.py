"""Fresh root-scope consent for one stopped RESTORE runtime resumption.

This constructs finite OS metadata only. The canonical controller/observer must
separately establish the selected database and exact parent ownership.
"""
from __future__ import annotations

from pathlib import Path
import time
import uuid

from . import resumption_contract as contract, resumption_review as shared
from .rpc import ControlError


def make_review(snapshot: dict, action: str, *, control_manifest_sha256: str):
    shared.require_root()
    owner = snapshot.get('operation_type')
    if (owner not in {'restore', 'update'} or snapshot.get('runtime_status') != 'offline'
            or not isinstance(action,str) or action not in ({'continue_recovery'} if owner=='update'
                else {'continue_recovery','rollback'})
            or action not in snapshot['permitted_actions']
            or snapshot['control_manifest_sha256'] != control_manifest_sha256
            or type(snapshot['generation']) is not int or not 1 <= snapshot['generation'] < 2**63-1):
        raise ControlError('control_resumption_mode_unavailable')
    for name in ('installation_id','operation_id'):contract._uuid(snapshot[name])
    for name in ('execution_sha256','parent_binding_sha256','evidence_sha256','control_manifest_sha256'):
        contract._digest(snapshot[name])
    shared.require_stopped()
    epoch=shared.root_stop_epoch()
    if epoch != snapshot['stop_epoch']:
        raise ControlError('control_resumption_review_changed')
    issuer=shared.current_issuer(snapshot['installation_id'],epoch)
    enabled=shared._boot_enabled()
    if type(enabled) is not bool:raise ControlError('control_resumption_review_changed')
    payload={'schema_version':2,'owner_type':owner,'review_id':str(uuid.uuid4()),'plan_id':str(uuid.uuid4()),
        'installation_id':snapshot['installation_id'],'operation_id':snapshot['operation_id'],
        'generation':snapshot['generation'],'canonical_evidence_sha256':snapshot['evidence_sha256'],
        'recovery_action':action,'parent_binding_sha256':snapshot['parent_binding_sha256'],
        'execution_sha256':snapshot['execution_sha256'],'previous_boot_enabled':enabled,'boot_enabled':enabled,
        'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'stop_epoch':epoch,
        'issuer_sha256':contract.digest(issuer),'control_manifest_sha256':control_manifest_sha256}
    return shared.ResumptionReview(dict(snapshot),payload,issuer)


def launch_plan(review, *, approved: bool) -> dict:
    shared.require_live(review)
    value=review.payload
    if approved is not True or value.get('schema_version')!=2 or value.get('owner_type') not in {'restore','update'}:
        raise ControlError('control_resumption_not_approved')
    return contract.parse_plan({'schema_version':2,'plan_id':value['plan_id'],
        'installation_id':value['installation_id'],'operation_id':value['operation_id'],'review_id':value['review_id'],
        'owner_type':value['owner_type'],'action':value['owner_type']+'_recovery','recovery_action':value['recovery_action'],
        'parent_binding_sha256':value['parent_binding_sha256'],'generation':value['generation']+1,
        'boot_id':value['boot_id'],'stop_epoch':value['stop_epoch'],'control_manifest_sha256':value['control_manifest_sha256'],
        'issuer_sha256':value['issuer_sha256'],'expires_at_monotonic':time.monotonic()+900,
        'prior_running':True,'interruption_approved':True,'resumption_approved':True,
        'stages':[{'kind':'candidate_start','execution_sha256':value['execution_sha256'],
                   'generation':value['generation']+1,'handoff_sha256':None}]})
