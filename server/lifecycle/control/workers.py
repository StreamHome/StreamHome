"""Installed unprivileged lifecycle readers; no application configuration import."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from dataclasses import replace
import uuid

from .. import state
from ..configuration_store import ConfigurationStore
from ..execution_state import read_active_execution
from ..installation import inspect_installation
from ..layout import layout_for_root
from ..runtime_build import RuntimeBuildError, verify_runtime
from ..runtime_links import verify_links
from .rpc import ControlError


def prepare_management_request(payload: dict, *, control_manifest_sha256: str) -> dict:
    from .. import management_journal as contract
    from ..deployment_config import parse_deployment_payload
    from .requests import parse_management_request
    fields = {"purpose", "operation_id", "review_id", "expected_generation", "expected_desired_revision",
              "deployment", "activate_deployment", "boot_enabled", "previous_boot_enabled", "initial_service_state",
              "supervisor_invocation_id", "supervisor", "backend", "interruption_approved", "boot_id", "stop_epoch"}
    if isinstance(payload,dict) and payload.get('purpose') == 'maintain_control':
        fields.add('control_candidate')
    if (not isinstance(payload, dict) or set(payload) != fields or payload["purpose"] not in {"configure", "complete_install", "activate_pending", "maintain_control"}
            or any(type(payload[name]) is not bool for name in ("activate_deployment", "boot_enabled", "previous_boot_enabled", "interruption_approved"))):
        raise ControlError("control_request_invalid")
    from ..cloudflare_connector import require_deployment
    require_deployment(parse_deployment_payload(payload['deployment']))
    layout = layout_for_root(ROOT)
    authority = state.inspect_state(ROOT)
    record = read_active_execution(ROOT)
    descriptor = os.open(layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        with ConfigurationStore(descriptor, record.installation_id) as store:
            manifest = store.inspect()
            active = store.read_revision(manifest.active)
            desired = store.read_revision(manifest.desired)
            if payload["purpose"] == "activate_pending":
                from ..pending_deployment import eligible_saved_deployment
                if eligible_saved_deployment(ROOT, store, authority, record.execution) is None:
                    raise ControlError("control_pending_deployment_ineligible")
    finally:
        os.close(descriptor)
    if (authority.schema_version != 2 or authority.active_operation is not None
            or type(payload["expected_generation"]) is not int or authority.generation != payload["expected_generation"]
            or manifest.desired.revision != payload["expected_desired_revision"]):
        raise ControlError("control_review_stale")
    deployment = parse_deployment_payload(payload["deployment"])
    candidate = record.execution
    action, mode = contract.ManagementAction.PROVISION_CONTROL, contract.ActivationMode.OS_ONLY
    changes_deployment = deployment != desired.settings
    applies_pending = payload["activate_deployment"] and manifest.active != manifest.desired
    if payload['purpose'] == 'maintain_control':
        # Root supplies only the authenticated inactive control identity. Keep
        # canonical/application interpretation here, under the installation UID.
        control = payload['control_candidate']
        if (not isinstance(control,dict) or set(control) != {'control_bundle_id','control_manifest_sha256','control_protocol_version'}
                or deployment != active.settings or payload['boot_enabled'] != payload['previous_boot_enabled']
                or not payload['activate_deployment'] or not payload['interruption_approved']
                or record.execution.control_manifest_sha256 != control_manifest_sha256
                or manifest.active.revision != record.execution.deployment_revision
                or manifest.policy_revisions != record.execution.policy_revisions
                or manifest.provider_revisions != record.execution.provider_revisions):
            raise ControlError('control_review_stale')
        candidate = contract.parse_execution_tuple({**contract.execution_tuple_payload(record.execution),**control})
        if candidate == record.execution:
            return {'no_changes':True}
        mode = contract.ActivationMode.CUTOVER
        deployment = active.settings
    elif payload["purpose"] != "complete_install" and (changes_deployment or applies_pending):
        if deployment == active.settings:
            revision = manifest.active.revision
        elif deployment == desired.settings and desired.revision not in manifest.quarantined:
            revision = manifest.desired.revision
        else:
            revision = str(uuid.uuid4())
        candidate = replace(record.execution, deployment_revision=revision)
        action = contract.ManagementAction.CONFIGURE_APPLY
        mode = contract.ActivationMode.CUTOVER if payload["activate_deployment"] and payload["initial_service_state"] == "running" and candidate != record.execution else contract.ActivationMode.SAVE_ONLY
    else:
        deployment = active.settings
        if payload["purpose"] != "complete_install" and payload["boot_enabled"] == payload["previous_boot_enabled"]:
            return {"no_changes": True}
    if payload["purpose"] == "activate_pending" and (deployment != desired.settings or manifest.desired.revision in manifest.quarantined):
        raise ControlError("control_pending_deployment_ineligible")
    value = {name: payload[name] for name in ("operation_id", "review_id", "expected_generation", "boot_enabled", "previous_boot_enabled", "initial_service_state", "supervisor_invocation_id", "supervisor", "backend", "boot_id", "stop_epoch")}
    value.update(schema_version=1, installation_id=record.installation_id, action=action.value, activation_mode=mode.value,
                 previous=contract.execution_tuple_payload(record.execution), candidate=contract.execution_tuple_payload(candidate),
                 expected_desired=manifest.desired.payload(), deployment=deployment.payload(),
                 interruption_approved=payload["interruption_approved"] if mode is contract.ActivationMode.CUTOVER else False,
                 controller_control_manifest_sha256=control_manifest_sha256)
    return {"no_changes": False, "request": parse_management_request(value).payload()}


def bridge_observe(payload: dict) -> dict:
    from .. import management_journal as contract, management_store
    from .processes import controller_peer
    from .recovery_requests import effective_request
    from .requests import read_management_request
    if not isinstance(payload, dict) or set(payload) != {"operation_id", "controller_pid", "controller_invocation_id"} or type(payload["controller_pid"]) is not int:
        raise ControlError("control_request_invalid")
    journal = management_store.inspect_management_journal(ROOT, payload["operation_id"])
    reviewed = effective_request(journal, original=read_management_request(journal.operation_id))
    authority = state.inspect_state(ROOT)
    lease = state.read_controller_lease(ROOT)
    if (lease is None or not state.controller_lease_is_current(lease) or lease.pid != payload["controller_pid"]
            or lease.operation_id != reviewed.operation_id or lease.generation != journal.generation
            or journal.generation != reviewed.expected_generation + 1 or journal.controller_invocation_id != payload["controller_invocation_id"]
            or journal.review_id != reviewed.review_id or journal.installation_id != reviewed.installation_id
            or journal.previous != reviewed.previous or journal.candidate != reviewed.candidate
            or journal.action != reviewed.action or journal.activation_mode != reviewed.activation_mode):
        raise ControlError("control_controller_changed")
    controller_peer(lease.pid, reviewed.operation_id)
    contract.validate_active_journal(journal, authority)
    recovering = journal.generation != read_management_request(journal.operation_id).expected_generation + 1
    effect_requested = journal.stage is contract.ManagementStage.PREPARED or recovering and journal.stage is contract.ManagementStage.RECOVERY_REQUIRED
    if effect_requested:
        # Original PREPARED work and an explicitly rebound recovery can request
        # their own OS receipt. Other observations are progress and cannot wait
        # on the controller's long exclusive runtime verification lock.
        state.validate_operation_with_credential(ROOT, operation_id=journal.operation_id, generation=journal.generation, expected_phase=authority.active_operation.phase)
    result = {"request_sha256": reviewed.sha256, "generation": journal.generation,
            "stage": journal.stage.value, "os_effect_requested": effect_requested,
            "previous_boot_enabled": reviewed.previous_boot_enabled, "boot_enabled": reviewed.boot_enabled,
            "controller_invocation_id": journal.controller_invocation_id, "controller_pid": lease.pid,
            "accepted": journal.acceptance is not None}
    if journal.action is contract.ManagementAction.PROVISION_CONTROL and journal.activation_mode is contract.ActivationMode.CUTOVER:
        if effect_requested and (reviewed.initial_service_state=='stopped' or recovering):
            from .processes import require_service_quiescent
            require_service_quiescent(inspections=True,
                controller=(journal.operation_id,lease.pid,lease.process_start_identity))
            if (state.inspect_state(ROOT) != authority
                    or management_store.inspect_management_journal(ROOT,journal.operation_id) != journal
                    or not state.controller_lease_is_current(lease)):
                raise ControlError('control_controller_changed')
        selected=contract.selected_execution(journal)
        result.update(selected_execution_sha256=contract.execution_tuple_sha256(selected),
            selected_control={'control_bundle_id':selected.control_bundle_id,
                'control_manifest_sha256':selected.control_manifest_sha256,
                'control_protocol_version':selected.control_protocol_version})
    return result


def operation_result(operation_id: str) -> dict:
    from .. import management_journal as contract, management_store
    from .recovery_requests import effective_request
    journal = management_store.inspect_management_journal(ROOT, operation_id)
    reviewed = effective_request(journal, require_live=False)
    authority = state.inspect_state(ROOT)
    if (journal.review_id != reviewed.review_id or journal.installation_id != reviewed.installation_id
            or journal.previous != reviewed.previous or journal.candidate != reviewed.candidate
            or journal.generation != reviewed.expected_generation + 1):
        raise ControlError("control_result_changed")
    terminal = journal.stage in {contract.ManagementStage.SAVED, contract.ManagementStage.OS_COMPLETED, contract.ManagementStage.SUCCEEDED, contract.ManagementStage.ROLLED_BACK}
    released = authority.generation >= journal.generation and (authority.active_operation is None or authority.active_operation.operation_id != operation_id)
    return {"completed": terminal and released, "stage": journal.stage.value,
            "accepted": journal.acceptance is not None, "generation": journal.generation}


def check_listener(payload: dict) -> dict:
    from ..deployment_config import parse_deployment_payload
    from ..port_check import ClosingConnectionsError, ListenerCheckError, OwnedWebListener, check_web_port
    if not isinstance(payload, dict) or set(payload) != {"deployment", "web", "invocation"}:
        raise ControlError("control_request_invalid")
    selected = layout_for_root(ROOT).descriptor.descriptor
    config = parse_deployment_payload(payload["deployment"])
    owner = None
    if payload["web"] is not None:
        web = payload["web"]
        if not isinstance(web, dict) or set(web) != {"pid", "start_identity"} or not isinstance(payload["invocation"], str):
            raise ControlError("control_request_invalid")
        owner = OwnedWebListener(selected.installation_id, selected.service_uid, payload["invocation"],
            web["pid"], web["start_identity"], config.web_bind, config.web_port)
    try:
        result = check_web_port(config.web_bind, config.web_port, owner=owner, installation=selected)
    except ListenerCheckError as exc:
        return {'available': False, 'reason': 'closing_connections' if isinstance(exc, ClosingConnectionsError) else 'listener_conflict',
                'bind': config.web_bind, 'port': config.web_port}
    return {"available": True, "owned": result.reusable_owned_listener, "bind": result.bind, "port": result.port}


ROOT = Path("/opt/streamhome")


def inspect_managed() -> dict:
    layout = layout_for_root(ROOT)
    selected = layout.descriptor.descriptor
    if os.geteuid() != selected.service_uid:
        raise ControlError("control_worker_forbidden")
    authority = state.inspect_state(ROOT)
    record = read_active_execution(ROOT)
    descriptor = os.open(layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        with ConfigurationStore(descriptor, selected.installation_id) as store:
            manifest = store.inspect()
            active, desired = store.read_revision(manifest.active), store.read_revision(manifest.desired)
    finally:
        os.close(descriptor)
    runtime_verified, runtime_error = False, None
    try:
        verify_runtime(ROOT, record.execution)
        verify_links(ROOT, record.execution)
        runtime_verified = True
    except (RuntimeError, OSError, ValueError):
        runtime_error = "runtime_verification_failed"
    return {
        "installation_id": selected.installation_id, "lifecycle": state.public_projection(authority),
        "execution": record.payload(), "configuration": manifest.payload(),
        "active_deployment": active.settings.payload(), "desired_deployment": desired.settings.payload(),
        "runtime_verified": runtime_verified, "runtime_error": runtime_error,
    }


def main(arguments: list[str], *, bundle: Path, manifest: dict, manifest_sha256: str) -> int:
    parser = argparse.ArgumentParser(prog="streamhome internal reader")
    parser.add_argument("action", choices=("inspect", "prepare-request", "bridge-observe", "operation-result", "select-source", "initial-result", "initial-progress", "initial-listener", "initial-authority", "initial-retry-check", "check-listener", "identity", "release-check", "validate-preparation", "runtime-progress", "runtime-result", "prepare-runtime-request", "recovery-review", "prepare-recovery", "resumption-observe", "resumption-required", "bound-control-review", "bound-control-prepare", "bound-control-observe", "bound-control-result", "bound-repair-review", "bound-repair-prepare", "bound-repair-result", "prepare-parent-recovery", "parent-recovery-result", "retain-update", "update-runtime", "update-context", "prepare-update-recovery", "update-recovery-result"))
    parser.add_argument("operation_id", nargs="?")
    parsed = parser.parse_args(arguments)
    snapshot = inspect_installation()
    if snapshot is None or snapshot.descriptor.service_uid != os.geteuid() or os.geteuid() == 0:
        raise ControlError("control_worker_forbidden")
    if parsed.action == 'update-context':
        from ..configure_wizard import parse_request_json
        from ..managed_update import bound_journal, delegated_controller, execution_context
        payload = parse_request_json(sys.stdin.buffer.read(32769))
        if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'generation'}
                or type(payload['schema_version']) is not int or payload['schema_version'] != 1
                or type(payload['generation']) is not int or not 0 < payload['generation'] < 2**63):
            raise ControlError('control_request_invalid')
        _, _, bound = bound_journal(ROOT, parsed.operation_id)
        if bound.parent.execution.control_manifest_sha256 != manifest_sha256:
            raise ControlError('control_parent_binding_changed')
        proof = delegated_controller(ROOT, parsed.operation_id, payload['generation'])
        print(json.dumps(execution_context(ROOT, proof), sort_keys=True, allow_nan=False))
        return 0
    if parsed.action == 'update-runtime':
        from ..configure_wizard import parse_request_json
        from ..managed_update import bound_journal, delegated_controller, runtime_action
        payload = parse_request_json(sys.stdin.buffer.read(32769))
        if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'generation', 'action'}
                or type(payload['schema_version']) is not int or payload['schema_version'] != 1
                or type(payload['generation']) is not int or not 0 < payload['generation'] < 2**63
                or not isinstance(payload['action'], str)
                or payload['action'] not in {'stop', 'assert_stopped', 'maintenance', 'bind_candidate', 'bind_previous', 'start_candidate', 'start_previous', 'readiness', 'start', 'verify_previous', 'prepared', 'assert_rollback', 'create_checkpoint', 'restore_checkpoint', 'cleanup_checkpoint'}):
            raise ControlError('control_request_invalid')
        _, _, bound = bound_journal(ROOT, parsed.operation_id)
        if bound.parent.execution.control_manifest_sha256 != manifest_sha256:
            raise ControlError('control_parent_binding_changed')
        proof = delegated_controller(ROOT, parsed.operation_id, payload['generation'])
        print(json.dumps(runtime_action(ROOT, proof, payload['action']), sort_keys=True, allow_nan=False))
        return 0
    if parsed.action == 'retain-update':
        from ..configure_wizard import parse_request_json
        from ..managed_update import bound_journal, delegated_controller, retain_completed_from_delegate
        payload = parse_request_json(sys.stdin.buffer.read(32769))
        if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'generation'}
                or type(payload['schema_version']) is not int or payload['schema_version'] != 1
                or type(payload['generation']) is not int or not 0 < payload['generation'] < 2**63):
            raise ControlError('control_request_invalid')
        _, _, bound = bound_journal(ROOT, parsed.operation_id)
        if bound.parent.execution.control_manifest_sha256 != manifest_sha256:
            raise ControlError('control_parent_binding_changed')
        proof = delegated_controller(ROOT, parsed.operation_id, payload['generation'])
        retained = retain_completed_from_delegate(ROOT, proof)
        print(json.dumps({'retained': True, 'generation': retained['generation'],
            'journal_sha256': retained['journal_sha256']}, sort_keys=True, allow_nan=False))
        return 0
    if parsed.action == "identity":
        record = read_active_execution(ROOT)
        print(json.dumps({"installation_id":record.installation_id,"application_commit":record.execution.application_commit,
            "application_build_id":record.execution.application_build_id,"control_bundle_id":record.execution.control_bundle_id}, sort_keys=True))
        return 0
    if parsed.action == "release-check":
        from ..release_inspection import inspect_release
        result = inspect_release(ROOT)
        print(json.dumps({"status":result.status.value,"installed_commit":result.installed_commit,
            "target_commit":result.target_commit,"reason":result.reason}, sort_keys=True))
        return 0
    if parsed.action == "initial-authority":
        from .provision_worker import initial_authority
        print(json.dumps(initial_authority(), sort_keys=True))
        return 0
    if parsed.action == "initial-retry-check":
        from .provision_worker import initial_retry_check
        print(json.dumps(initial_retry_check(), sort_keys=True))
        return 0
    if parsed.action in {"select-source", "initial-result", "initial-progress"}:
        from .provision_worker import initial_progress, initial_result, select_source
        if snapshot.descriptor.status.value != "provisioning":
            raise ControlError("control_initial_preparation_forbidden")
        value = select_source() if parsed.action == "select-source" else initial_progress(parsed.operation_id) if parsed.action == "initial-progress" else initial_result(parsed.operation_id)
        print(json.dumps(value, sort_keys=True, allow_nan=False))
        return 0
    if parsed.action in {"initial-listener", "check-listener"}:
        from ..configure_wizard import parse_request_json
        from .provision_worker import initial_listener
        payload = parse_request_json(sys.stdin.buffer.read(32769))
        value = initial_listener(payload) if parsed.action == "initial-listener" else check_listener(payload)
        print(json.dumps(value, sort_keys=True, allow_nan=False))
        return 0
    if parsed.action == "inspect":
        print(json.dumps(inspect_managed(), sort_keys=True, allow_nan=False))
        return 0
    if parsed.action == 'resumption-required':
        active = state.inspect_state(ROOT).active_operation
        print(json.dumps({'required': active is not None and active.operation_type in {
            state.OperationType.MANAGEMENT, state.OperationType.RESTORE, state.OperationType.UPDATE}}))
        return 0
    if parsed.action in {'bound-control-review','bound-control-prepare','bound-control-observe','bound-control-result'}:
        from .. import bound_control_repair
        from ..configure_wizard import parse_request_json
        if parsed.action=='bound-control-result':
            value=bound_control_repair.result(parsed.operation_id,control_manifest_sha256=manifest_sha256)
        else:
            payload=parse_request_json(sys.stdin.buffer.read(32769))
            if parsed.action=='bound-control-review':
                if not isinstance(payload,dict) or set(payload)!={'artifact'}:
                    raise ControlError('control_request_invalid')
                value=bound_control_repair.plan(parsed.operation_id,payload['artifact'],control_manifest_sha256=manifest_sha256)
            else:
                if not isinstance(payload,dict) or payload.get('operation_id')!=parsed.operation_id:
                    raise ControlError('control_request_invalid')
                method=bound_control_repair.prepare if parsed.action=='bound-control-prepare' else bound_control_repair.observe
                value=method(payload,control_manifest_sha256=manifest_sha256)
        print(json.dumps(value,sort_keys=True,allow_nan=False));return 0
    if parsed.action in {'bound-repair-review', 'bound-repair-prepare', 'bound-repair-result'}:
        from .. import bound_management_repair
        from ..configure_wizard import parse_request_json
        active=state.inspect_state(ROOT).active_operation
        if active is not None and active.operation_type is state.OperationType.UPDATE:
            from .. import bound_update_repair
            result = (bound_update_repair.plan(ROOT, parsed.operation_id, control_manifest_sha256=manifest_sha256)
                if parsed.action == 'bound-repair-review' else bound_update_repair.result(
                    ROOT, parsed.operation_id, control_manifest_sha256=manifest_sha256)
                if parsed.action == 'bound-repair-result' else bound_update_repair.prepare(
                    ROOT, parse_request_json(sys.stdin.buffer.read(32769)), control_manifest_sha256=manifest_sha256))
            print(json.dumps(result, sort_keys=True, allow_nan=False));return 0
        if active is not None and active.operation_type is state.OperationType.RESTORE:
            from .. import bound_parent_repair
            result=(bound_parent_repair.plan(ROOT,parsed.operation_id,control_manifest_sha256=manifest_sha256)
                if parsed.action=='bound-repair-review' else bound_parent_repair.result(
                    ROOT,parsed.operation_id,control_manifest_sha256=manifest_sha256)
                if parsed.action=='bound-repair-result' else bound_parent_repair.prepare(
                    ROOT,parse_request_json(sys.stdin.buffer.read(32769)),control_manifest_sha256=manifest_sha256))
            print(json.dumps(result,sort_keys=True,allow_nan=False));return 0
        result = (bound_management_repair.plan(parsed.operation_id, control_manifest_sha256=manifest_sha256)
                  if parsed.action == 'bound-repair-review' else bound_management_repair.result(parsed.operation_id)
                  if parsed.action == 'bound-repair-result' else bound_management_repair.prepare(
                      parse_request_json(sys.stdin.buffer.read(32769)), control_manifest_sha256=manifest_sha256))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    if parsed.action == 'resumption-observe':
        from .resumption_observation import observe
        from ..configure_wizard import parse_request_json
        payload = parse_request_json(sys.stdin.buffer.read(32769))
        if (not isinstance(payload, dict) or not isinstance(payload.get('plan'), dict)
                or payload['plan'].get('control_manifest_sha256') != manifest_sha256):
            raise ControlError('control_bundle_mismatch')
        if payload['plan'].get('owner_type') == 'restore':
            from .parent_resumption_observation import observe
        elif payload['plan'].get('owner_type') == 'update':
            from .update_resumption_observation import observe
        print(json.dumps(observe(payload), sort_keys=True, allow_nan=False))
        return 0
    if parsed.action in {'prepare-update-recovery', 'update-recovery-result'}:
        from ..configure_wizard import parse_request_json
        from .. import update_recovery
        from . import update_grants
        payload = parse_request_json(sys.stdin.buffer.read(32769))
        if parsed.action == 'update-recovery-result':
            if (parsed.operation_id is None or not isinstance(payload, dict) or set(payload) != {'controller_generation'}
                    or type(payload['controller_generation']) is not int or not 2 <= payload['controller_generation'] < 2**63):
                raise ControlError('control_request_invalid')
            value = update_recovery.result(ROOT, parsed.operation_id, payload['controller_generation'],
                control_manifest_sha256=manifest_sha256)
        else:
            if parsed.operation_id is not None:
                raise ControlError('control_request_invalid')
            value = update_grants.prepare(ROOT, payload, control_manifest_sha256=manifest_sha256)
        print(json.dumps(value, sort_keys=True, allow_nan=False));return 0
    if parsed.action in {'prepare-parent-recovery', 'parent-recovery-result'}:
        from ..configure_wizard import parse_request_json
        from .. import parent_recovery
        from . import parent_grants
        payload = parse_request_json(sys.stdin.buffer.read(32769))
        if parsed.action == 'parent-recovery-result':
            if (not isinstance(payload, dict) or set(payload) != {'controller_generation'}
                    or type(payload['controller_generation']) is not int or payload['controller_generation'] < 2):
                raise ControlError('control_request_invalid')
            value = parent_recovery.result(ROOT, parsed.operation_id, payload['controller_generation'])
        else:
            value = parent_grants.prepare(ROOT, payload, control_manifest_sha256=manifest_sha256)
        print(json.dumps(value, sort_keys=True, allow_nan=False));return 0
    if parsed.action in {"recovery-review", "prepare-recovery"}:
        from . import recovery_requests
        from ..configure_wizard import parse_request_json
        active = state.inspect_state(ROOT).active_operation if parsed.action == 'recovery-review' else None
        if active is not None and active.operation_type is state.OperationType.UPDATE:
            from ..update_recovery import review
            value = review(ROOT, parsed.operation_id, control_manifest_sha256=manifest_sha256)
            print(json.dumps(value, sort_keys=True, allow_nan=False));return 0
        if active is not None and active.operation_type is state.OperationType.RESTORE:
            from ..parent_recovery import review
            value = review(ROOT, parsed.operation_id, control_manifest_sha256=manifest_sha256)
            print(json.dumps(value, sort_keys=True, allow_nan=False));return 0
        value = (recovery_requests.review(parsed.operation_id, control_manifest_sha256=manifest_sha256)
                 if parsed.action == "recovery-review" else recovery_requests.prepare(
                     parse_request_json(sys.stdin.buffer.read(32769)), control_manifest_sha256=manifest_sha256))
        print(json.dumps(value, sort_keys=True, allow_nan=False))
        return 0
    if parsed.action in {"validate-preparation", "runtime-progress", "runtime-result", "prepare-runtime-request"}:
        from . import runtime_preparation as preparation
        if parsed.action == "validate-preparation":
            from ..configure_wizard import parse_request_json
            value = preparation.parse_grant(parse_request_json(sys.stdin.buffer.read(32769)))
            if value['control_manifest_sha256'] != manifest_sha256:
                raise ControlError('control_bundle_mismatch')
        elif parsed.action == "runtime-progress":
            grant = preparation.read_grant(parsed.operation_id)
            preparation.require_current(grant)
            from .provision_worker import _private_record
            value = _private_record(layout_for_root(ROOT).cache / 'preparation' / parsed.operation_id / 'progress.json')
            from .support import PREPARATION_STAGES
            if (set(value) != {'stage', 'status'} or value['stage'] not in PREPARATION_STAGES
                    or value['status'] not in {'running', 'done'}):
                raise ControlError('control_preparation_invalid')
        elif parsed.action == "runtime-result":
            value = preparation.result(parsed.operation_id)
        else:
            from ..configure_wizard import parse_request_json
            from .. import management_journal as contract
            from .requests import parse_management_request
            payload = parse_request_json(sys.stdin.buffer.read(32769))
            if (set(payload) != {'preparation_id', 'review_id', 'boot_enabled'} or type(payload['boot_enabled']) is not bool):
                raise ControlError('control_request_invalid')
            grant = preparation.read_grant(payload['preparation_id'])
            if grant['control_manifest_sha256'] != manifest_sha256:
                raise ControlError('control_bundle_mismatch')
            built = preparation.result(payload['preparation_id'])
            active = inspect_managed()
            value = {'schema_version': 1, 'installation_id': grant['installation_id'],
                'operation_id': grant['preparation_id'], 'review_id': payload['review_id'],
                'expected_generation': grant['expected_generation'], 'action': contract.ManagementAction.REPAIR_ENVIRONMENT.value,
                'activation_mode': contract.ActivationMode.CUTOVER.value, 'previous': grant['previous'], 'candidate': built['execution'],
                'expected_desired': grant['expected_desired'], 'deployment': active['active_deployment'],
                'initial_service_state': 'stopped' if grant['schema_version'] == 2 else 'running', 'supervisor_invocation_id': grant['supervisor_invocation_id'],
                'supervisor': grant['supervisor'], 'backend': grant['backend'], 'interruption_approved': True,
                'boot_enabled': payload['boot_enabled'], 'previous_boot_enabled': payload['boot_enabled'],
                'boot_id': grant['boot_id'], 'stop_epoch': grant['stop_epoch'], 'controller_control_manifest_sha256': manifest_sha256}
            value = parse_management_request(value).payload()
        print(json.dumps(value, sort_keys=True, allow_nan=False))
        return 0
    if parsed.action == "operation-result":
        print(json.dumps(operation_result(parsed.operation_id), sort_keys=True))
        return 0
    if parsed.action in {"prepare-request", "bridge-observe"}:
        from ..configure_wizard import parse_request_json
        raw = sys.stdin.buffer.read(32769)
        payload = parse_request_json(raw)
        result = prepare_management_request(payload, control_manifest_sha256=manifest_sha256) if parsed.action == "prepare-request" else bridge_observe(payload)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    raise ControlError("control_action_unsupported")
