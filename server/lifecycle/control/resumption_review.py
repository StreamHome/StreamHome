"""Fresh scoped owner review after an owned runtime has already stopped.

An old noninteractive request cannot approve this start. Automated callers must
answer the newly emitted review challenge while its cancellable issuer is alive.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import time
import uuid

from ..configure_wizard import parse_request_json
from ..terminal import open_console
from .administration import _boot_enabled, root_stop_epoch
from .privileged import require_root
from . import resumption_contract as contract
from .resumption_issuer import current_issuer
from .rpc import ControlError
from .stopped_repair import require_stopped


@dataclass(frozen=True)
class ResumptionReview:
    source: dict
    payload: dict
    issuer: dict

    @property
    def sha256(self) -> str:
        return contract.digest(self.payload)


def make_review(snapshot: dict, action: str, *, control_manifest_sha256: str) -> ResumptionReview:
    require_root()
    if (not isinstance(action, str) or snapshot['initial_service_state'] != 'running' or snapshot['activation_mode'] != 'cutover'
            or action not in snapshot['permitted_actions'] or snapshot['control_manifest_sha256'] != control_manifest_sha256):
        raise ControlError('control_resumption_mode_unavailable')
    if snapshot['management_action'] not in ('configure_apply', 'repair_environment', 'provision_control'):
        raise ControlError('control_resumption_mode_unavailable')
    for name in ('installation_id', 'operation_id'):
        contract._uuid(snapshot[name])
    if type(snapshot['generation']) is not int or not 1 <= snapshot['generation'] < 2**63 - 1:
        raise ControlError('control_resumption_review_changed')
    require_stopped()
    epoch = root_stop_epoch()
    issuer = current_issuer(snapshot['installation_id'], epoch)
    previous = action in {'rollback', 'safe_abort'}
    execution = snapshot['previous_execution_sha256'] if previous else snapshot['selected_execution_sha256']
    for value in (execution, snapshot['evidence_sha256'], control_manifest_sha256):
        contract._digest(value)
    enabled = _boot_enabled()
    if type(enabled) is not bool or type(snapshot['previous_boot_enabled']) is not bool:
        raise ControlError('control_resumption_review_changed')
    payload = {'schema_version': 1, 'review_id': str(uuid.uuid4()), 'plan_id': str(uuid.uuid4()),
        'installation_id': snapshot['installation_id'], 'operation_id': snapshot['operation_id'],
        'generation': snapshot['generation'], 'canonical_evidence_sha256': snapshot['evidence_sha256'],
        'recovery_action': action, 'management_action': snapshot['management_action'],
        'execution_sha256': execution, 'previous_boot_enabled': enabled,
        'boot_enabled': snapshot['previous_boot_enabled'] if previous else enabled,
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'stop_epoch': epoch,
        'issuer_sha256': contract.digest(issuer), 'control_manifest_sha256': control_manifest_sha256}
    if snapshot['management_action']=='provision_control':
        runtime_reader=snapshot.get('previous_control_manifest_sha256' if previous else 'selected_control_manifest_sha256')
        contract._digest(runtime_reader)
        payload['runtime_control_manifest_sha256']=runtime_reader
    return ResumptionReview(dict(snapshot), payload, issuer)


def require_live(review: ResumptionReview) -> None:
    require_root()
    payload = review.payload
    if (root_stop_epoch() != payload['stop_epoch'] or _boot_enabled() is not payload['previous_boot_enabled']
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != payload['boot_id']
            or current_issuer(payload['installation_id'], payload['stop_epoch']) != review.issuer):
        raise ControlError('control_resumption_review_changed')
    require_stopped()


def _request(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as handle:
        value = os.fstat(handle.fileno())
        if not stat.S_ISREG(value.st_mode) or value.st_size > 32768:
            raise ControlError('control_request_invalid')
        return parse_request_json(handle.read(32769))


def consent(review: ResumptionReview, request_path: Path | None) -> bool:
    require_live(review)
    if request_path is None:
        with open_console() as console:
            console.write('StreamHome is stopped. This fresh review authorizes one guarded runtime start.')
            console.write('Operation: ' + review.payload['operation_id'])
            console.write('Runtime SHA-256: ' + review.payload['execution_sha256'])
            console.write('Resumption review SHA-256: ' + review.sha256)
            console.write('Start at boot: ' + ('enabled' if review.payload['boot_enabled'] else 'disabled'))
            return console.confirm('Start the reviewed runtime and continue this operation?')
    print(json.dumps({'resumption_review': review.payload, 'resumption_review_sha256': review.sha256,
                      'request_schema_version': 3}, sort_keys=True), flush=True)
    deadline = time.monotonic() + 900
    while True:
        require_live(review)
        try:
            value = _request(request_path)
            break
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise ControlError('control_resumption_review_timeout')
            time.sleep(0.2)
    fields = {'schema_version', 'operation_id', 'expected_generation', 'expected_evidence_sha256', 'action',
              'continue_recovery', 'interruption_approved', 'resume_service', 'expected_resumption_sha256'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 3 or type(value['expected_generation']) is not int
            or any(type(value[name]) is not bool for name in ('continue_recovery', 'interruption_approved', 'resume_service'))):
        raise ControlError('control_request_invalid')
    if (value['operation_id'] != review.payload['operation_id'] or value['expected_generation'] != review.payload['generation']
            or value['expected_evidence_sha256'] != review.payload['canonical_evidence_sha256']
            or value['action'] != review.payload['recovery_action'] or value['expected_resumption_sha256'] != review.sha256):
        raise ControlError('control_resumption_review_changed')
    return value['continue_recovery'] and value['interruption_approved'] and value['resume_service']


def launch_plan(review: ResumptionReview, *, approved: bool) -> dict:
    require_live(review)
    if approved is not True:
        raise ControlError('control_resumption_not_approved')
    value = review.payload
    plan = {'schema_version': 1, 'plan_id': value['plan_id'],
        'installation_id': value['installation_id'], 'operation_id': value['operation_id'], 'review_id': value['review_id'],
        'owner_type': 'management', 'action': value['management_action'], 'generation': value['generation'] + 1,
        'boot_id': value['boot_id'], 'stop_epoch': value['stop_epoch'], 'control_manifest_sha256': value['control_manifest_sha256'],
        'issuer_sha256': value['issuer_sha256'], 'expires_at_monotonic': time.monotonic() + 900,
        'prior_running': True, 'interruption_approved': True, 'resumption_approved': True,
        'stages': [{'kind': 'candidate_start', 'execution_sha256': value['execution_sha256'],
                    'generation': value['generation'] + 1, 'handoff_sha256': None}]}
    if value['management_action']=='provision_control':
        plan['schema_version']=3
        plan['stages'][0]['control_manifest_sha256']=value['runtime_control_manifest_sha256']
    return contract.parse_plan(plan)
