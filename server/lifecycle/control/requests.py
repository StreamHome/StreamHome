"""Root-reviewed operation requests shared with unprivileged controllers.

The JSON is data, never a command or path. Canonical authority and the current
controller lease remain mandatory; a reviewed request alone authorizes no app IO.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .. import management_journal as contract
from ..configuration_store import DeploymentReference
from ..deployment_config import DeploymentConfig, parse_deployment_payload
from .rpc import ControlError
from .tickets import read_protected_json


REQUEST_ROOT = Path("/etc/streamhome/control/operations")


@dataclass(frozen=True)
class ProcessReference:
    pid: int
    start_identity: str

    def payload(self) -> dict:
        return {"pid": self.pid, "start_identity": self.start_identity}


def _process(value: Any) -> ProcessReference | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"pid", "start_identity"}
            or type(value["pid"]) is not int or value["pid"] <= 0
            or not isinstance(value["start_identity"], str) or not re.fullmatch(r"[0-9]{1,24}", value["start_identity"])):
        raise ControlError("control_process_invalid")
    return ProcessReference(**value)


@dataclass(frozen=True)
class ManagementRequest:
    installation_id: str
    operation_id: str
    review_id: str
    expected_generation: int
    action: contract.ManagementAction
    activation_mode: contract.ActivationMode
    previous: contract.ExecutionTuple
    candidate: contract.ExecutionTuple
    expected_desired: DeploymentReference
    deployment: DeploymentConfig
    initial_service_state: str
    supervisor_invocation_id: str | None
    supervisor: ProcessReference | None
    backend: ProcessReference | None
    interruption_approved: bool
    previous_boot_enabled: bool
    boot_enabled: bool
    boot_id: str
    stop_epoch: str
    controller_control_manifest_sha256: str

    def payload(self) -> dict:
        return {
            "schema_version": 1, "installation_id": self.installation_id, "operation_id": self.operation_id,
            "review_id": self.review_id, "expected_generation": self.expected_generation,
            "action": self.action.value, "activation_mode": self.activation_mode.value,
            "previous": contract.execution_tuple_payload(self.previous), "candidate": contract.execution_tuple_payload(self.candidate),
            "expected_desired": self.expected_desired.payload(), "deployment": self.deployment.payload(),
            "initial_service_state": self.initial_service_state, "supervisor_invocation_id": self.supervisor_invocation_id,
            "supervisor": self.supervisor.payload() if self.supervisor else None,
            "backend": self.backend.payload() if self.backend else None,
            "interruption_approved": self.interruption_approved, "previous_boot_enabled": self.previous_boot_enabled,
            "boot_enabled": self.boot_enabled, "boot_id": self.boot_id, "stop_epoch": self.stop_epoch,
            "controller_control_manifest_sha256": self.controller_control_manifest_sha256,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def parse_management_request(payload: Any) -> ManagementRequest:
    fields = set(ManagementRequest.__dataclass_fields__) | {"schema_version"}
    if not isinstance(payload, dict) or set(payload) != fields or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ControlError("control_request_invalid")
    value = dict(payload)
    value.pop("schema_version")
    for name in ("installation_id", "operation_id", "review_id", "boot_id"):
        value[name] = contract._uuid(value[name], name)
    value["expected_generation"] = contract._integer(value["expected_generation"], 0, "reviewed generation")
    for name in ("previous", "candidate"):
        value[name] = contract.parse_execution_tuple(value[name])
    for name in ("interruption_approved", "previous_boot_enabled", "boot_enabled"):
        if type(value[name]) is not bool:
            raise ControlError("control_request_invalid")
    value["supervisor"], value["backend"] = _process(value["supervisor"]), _process(value["backend"])
    if value["initial_service_state"] not in {"running", "stopped"}:
        raise ControlError("control_request_invalid")
    running = value["initial_service_state"] == "running"
    if running != (value["supervisor"] is not None) or (not running and value["backend"] is not None):
        raise ControlError("control_request_invalid")
    if running:
        contract._text(value["supervisor_invocation_id"], contract._INVOCATION, "reviewed supervisor")
        if value["supervisor_invocation_id"] == "0" * 32:
            raise ControlError("control_request_invalid")
    elif value["supervisor_invocation_id"] is not None:
        raise ControlError("control_request_invalid")
    reference = value["expected_desired"]
    if not isinstance(reference, dict) or set(reference) != {"revision", "sha256"}:
        raise ControlError("control_request_invalid")
    value["expected_desired"] = DeploymentReference(contract._uuid(reference["revision"], "desired revision"), contract._text(reference["sha256"], contract._SHA256, "desired digest"))
    for name in ("stop_epoch", "controller_control_manifest_sha256"):
        value[name] = contract._text(value[name], contract._SHA256, name)
    value["deployment"] = parse_deployment_payload(value["deployment"])
    try:
        value["action"] = contract.ManagementAction(value["action"])
        value["activation_mode"] = contract.ActivationMode(value["activation_mode"])
    except (ValueError, TypeError) as exc:
        raise ControlError("control_request_invalid") from exc
    result = ManagementRequest(**value)
    # Reuse the actual journal contract rather than a second action matrix.
    contract.parse_management_journal(contract.management_journal_payload(contract.ManagementJournal(
        result.installation_id, result.operation_id, result.expected_generation + 1, result.action,
        result.activation_mode, result.review_id, result.previous, result.candidate,
        result.initial_service_state, "1" * 32, result.supervisor_invocation_id,
        contract.ManagementStage.RESERVED, None,
    )))
    if result.activation_mode is contract.ActivationMode.CUTOVER and not result.interruption_approved:
        raise ControlError("control_interruption_not_approved")
    if result.activation_mode is not contract.ActivationMode.CUTOVER and result.interruption_approved:
        raise ControlError("control_request_invalid")
    return result


def read_management_request(operation_id: str) -> ManagementRequest:
    contract._uuid(operation_id, "operation request")
    result = parse_management_request(read_protected_json(REQUEST_ROOT / operation_id / "request.json"))
    if result.operation_id != operation_id:
        raise ControlError("control_request_invalid")
    return result


def read_os_receipt(request: ManagementRequest, generation: int, controller_invocation_id: str) -> dict:
    payload = read_protected_json(os_receipt_path(request, generation))
    if (set(payload) != {"schema_version", "request_sha256", "operation_id", "generation", "controller_invocation_id", "boot_enabled", "stage"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["request_sha256"] != request.sha256 or payload["operation_id"] != request.operation_id
            or type(payload["generation"]) is not int or payload["generation"] != generation
            or payload["controller_invocation_id"] != controller_invocation_id
            or payload["boot_enabled"] is not request.boot_enabled or payload["stage"] != "complete"):
        raise ControlError("control_os_receipt_changed")
    return payload


def os_receipt_path(request: ManagementRequest, generation: int) -> Path:
    grant_path = REQUEST_ROOT / request.operation_id / f"recovery-{generation}.json"
    name = f"os-receipt-{generation}.json" if grant_path.exists() or grant_path.is_symlink() else "os-receipt.json"
    return REQUEST_ROOT / request.operation_id / name
