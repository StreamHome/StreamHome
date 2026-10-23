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
    if (not isinstance(payload, dict) or set(payload) != fields or payload["purpose"] not in {"configure", "complete_install", "activate_pending"}
            or any(type(payload[name]) is not bool for name in ("activate_deployment", "boot_enabled", "previous_boot_enabled", "interruption_approved"))):
        raise ControlError("control_request_invalid")
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
    if payload["purpose"] != "complete_install" and (changes_deployment or applies_pending):
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
    return {"request_sha256": reviewed.sha256, "generation": journal.generation,
            "stage": journal.stage.value, "os_effect_requested": effect_requested,
            "previous_boot_enabled": reviewed.previous_boot_enabled, "boot_enabled": reviewed.boot_enabled,
            "controller_invocation_id": journal.controller_invocation_id, "controller_pid": lease.pid,
            "accepted": journal.acceptance is not None}


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
    parser.add_argument("action", choices=("inspect", "prepare-request", "bridge-observe", "operation-result", "select-source", "initial-result", "initial-progress", "initial-listener", "initial-authority", "initial-retry-check", "check-listener", "identity", "release-check", "validate-preparation", "runtime-progress", "runtime-result", "prepare-runtime-request", "recovery-review", "prepare-recovery", "resumption-observe", "resumption-required"))
    parser.add_argument("operation_id", nargs="?")
    parsed = parser.parse_args(arguments)
    snapshot = inspect_installation()
    if snapshot is None or snapshot.descriptor.service_uid != os.geteuid() or os.geteuid() == 0:
        raise ControlError("control_worker_forbidden")
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
        print(json.dumps({'required': active is not None and active.operation_type is state.OperationType.MANAGEMENT}))
        return 0
    if parsed.action == 'resumption-observe':
        from .resumption_observation import observe
        from ..configure_wizard import parse_request_json
        payload = parse_request_json(sys.stdin.buffer.read(32769))
        if (not isinstance(payload, dict) or not isinstance(payload.get('plan'), dict)
                or payload['plan'].get('control_manifest_sha256') != manifest_sha256):
            raise ControlError('control_bundle_mismatch')
        print(json.dumps(observe(payload), sort_keys=True, allow_nan=False))
        return 0
    if parsed.action in {"recovery-review", "prepare-recovery"}:
        from . import recovery_requests
        from ..configure_wizard import parse_request_json
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
