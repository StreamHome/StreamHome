"""Root metadata capsule for original UPDATE/RESTORE exact-control permission.

No application authority is imported or interpreted. The nested owner permission
is independently checked by its existing UID controller.
"""
from __future__ import annotations

from . import parent_grants,update_grants
from .rpc import ControlError


def parse(value: dict) -> dict:
    fields={'schema_version','action','request','source_request_sha256','source_evidence_sha256','recovery_review_id',
        'repair_review_sha256','filesystem_sha256','artifact','selection','issuer_scope','expires_at_monotonic','owner_grant'}
    try:
        if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
                or value['schema_version']!=6 or value['action']!='repair_bound_control'
                or not isinstance(value['owner_grant'],dict)):
            raise ValueError('invalid parent control capsule')
        native=value['owner_grant']
        grants={'update':update_grants,'restore':parent_grants}.get(native.get('operation_type'))
        if grants is None:raise ValueError('invalid parent owner')
        grants.parse_grant(native)
        if native['schema_version']!=4:raise ValueError('invalid parent permission')
        request=value['request']
        if (not isinstance(request,dict) or set(request)!={'installation_id','operation_id','expected_generation',
                'controller_control_manifest_sha256','boot_id','stop_epoch'}
                or type(request['expected_generation']) is not int
                or request!={'installation_id':native['installation_id'],'operation_id':native['operation_id'],
                    'expected_generation':native['generation'],'controller_control_manifest_sha256':native['control_manifest_sha256'],
                    'boot_id':native['boot_id'],'stop_epoch':native['stop_epoch']}
                or value['source_request_sha256']!=native['parent_binding_sha256']
                or value['source_evidence_sha256']!=native['source_evidence_sha256']
                or value['repair_review_sha256']!=native['source_evidence_sha256']
                or value['recovery_review_id']!=native['review_id'] or value['artifact']!=native['control_artifact']
                or not isinstance(value['selection'],dict) or type(value['selection'].get('schema_version')) is not int
                or value['selection']!=native['control_selection'] or value['filesystem_sha256']!=native['filesystem_sha256']
                or value['issuer_scope']!=native['issuer_scope']
                or type(value['expires_at_monotonic']) not in {int,float}
                or value['expires_at_monotonic']!=native['expires_at_monotonic']):
            raise ValueError('changed parent control permission')
        return value
    except (KeyError,TypeError,ValueError,ControlError) as exc:
        raise ControlError('control_recovery_request_invalid') from exc


def wrap(native: dict) -> dict:
    return parse({'schema_version':6,'action':'repair_bound_control','request':{
        'installation_id':native['installation_id'],'operation_id':native['operation_id'],
        'expected_generation':native['generation'],'controller_control_manifest_sha256':native['control_manifest_sha256'],
        'boot_id':native['boot_id'],'stop_epoch':native['stop_epoch']},
        'source_request_sha256':native['parent_binding_sha256'],'source_evidence_sha256':native['source_evidence_sha256'],
        'repair_review_sha256':native['source_evidence_sha256'],'recovery_review_id':native['review_id'],
        'filesystem_sha256':native['filesystem_sha256'],'artifact':native['control_artifact'],
        'selection':native['control_selection'],'issuer_scope':native['issuer_scope'],
        'expires_at_monotonic':native['expires_at_monotonic'],'owner_grant':native})
