"""Root command orchestration around the installed unprivileged authority reader.

This module never imports state, journals, application code or an app environment
as root. Root performs only fixed systemd/file effects after a reader running as
the dedicated UID validates the exact live canonical controller.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import uuid

from ..configure_wizard import ConfigurationDraft, configure_wizard, parse_configuration_request, parse_request_json
from ..deployment_config import DeploymentConfigError, parse_deployment_payload
from ..terminal import open_console
from .privileged import descriptor, require_root, run_worker, supervisor_snapshot, systemctl, unit_snapshot
from .root_files import atomic_root_json, root_directory
from .rpc import ControlError, request
from .tickets import read_protected_json, read_stop_epoch


OPERATIONS = Path("/etc/streamhome/control/operations")
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def _identity(value: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value) or not uuid.UUID(value).int:
        raise ControlError("control_operation_invalid")
    return value


def _sha(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def root_stop_epoch() -> str:
    return read_stop_epoch()


def stop_record(invocation: str | None) -> dict:
    if invocation is not None and not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise ControlError("control_incarnation_changed")
    return {
        "schema_version": 1, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "invocation": invocation, "nonce": secrets.token_hex(32),
    }


def record_stop(invocation: str | None) -> None:
    require_root()
    selected = descriptor()
    atomic_root_json(Path("/etc/streamhome/control/stop-epoch.json"), stop_record(invocation), gid=selected.service_gid, mode=0o640)


def unit_stopped() -> int:
    require_root()
    if Path("/proc/self/cgroup").read_text().splitlines() != ["0::/system.slice/streamhome.service"]:
        raise ControlError("control_unit_hook_forbidden")
    invocation = os.environ.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise ControlError("control_incarnation_changed")
    record_stop(invocation)
    return 0


def process_snapshot(*, preserving_recovery: bool = False) -> dict:
    selected = descriptor()
    unit = unit_snapshot()
    result = {"initial_service_state": "stopped", "supervisor_invocation_id": None, "supervisor": None, "backend": None}
    if unit["ActiveState"] not in {"active", "activating"} or int(unit["MainPID"]) <= 0:
        return result
    supervisor = supervisor_snapshot(selected, unit)
    if supervisor.get("launches_open") is not True:
        raise ControlError("control_operator_stopped")
    result.update(initial_service_state="running", supervisor_invocation_id=unit["InvocationID"],
                  supervisor={"pid": int(unit["MainPID"]), "start_identity": supervisor["start_identity"]})
    backend = supervisor.get("children", {}).get("backend")
    if isinstance(backend, dict):
        try:
            admitted = request(selected.paths.runtime / "backend.sock", expected_uid=selected.service_uid,
                expected_pid=backend["pid"], installation_id=selected.installation_id, invocation=unit["InvocationID"],
                action="snapshot", arguments={})
            readiness = admitted.get('readiness')
            recovering = (preserving_recovery and isinstance(readiness, dict)
                and readiness.get('generalReady') is True and readiness.get('workersStarted') is True
                and readiness.get('recoverySucceeded') is True)
            if ((admitted.get("admitted") is True or recovering) and admitted.get("pid") == backend["pid"]
                    and admitted.get("startIdentity") == backend["start_identity"]
                    and admitted.get('installationId') == selected.installation_id and admitted.get('invocation') == unit['InvocationID']):
                result["backend"] = {"pid": backend["pid"], "start_identity": backend["start_identity"]}
        except ControlError:
            pass  # Only a separately approved interrupting path may use this absence.
    return result


def operation_unit(operation_id: str) -> dict:
    _identity(operation_id)
    output = systemctl("show", f"streamhome-operation@{operation_id}.service", "--property=ActiveState,SubState,MainPID,InvocationID,Result")
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if set(fields) != {"ActiveState", "SubState", "MainPID", "InvocationID", "Result"} or not re.fullmatch(r"[0-9]+", fields["MainPID"]):
        raise ControlError("control_unit_invalid")
    if fields["InvocationID"] and not re.fullmatch(r"[0-9a-f]{32}", fields["InvocationID"]):
        raise ControlError("control_incarnation_changed")
    return fields


def _boot_enabled() -> bool:
    value = unit_snapshot()["UnitFileState"]
    if value not in {"enabled", "disabled"}:
        raise ControlError("control_boot_policy_unqualified")
    return value == "enabled"


def _verify_owned_units() -> None:
    from .units import unit_files
    from .root_files import root_directory
    for name, expected in unit_files().items():
        path = Path(name)
        parent = root_directory(path.parent)
        try:
            descriptor_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor_fd, "rb") as handle:
                import stat
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o7133 or metadata.st_nlink != 1 or handle.read(32769) != expected:
                    raise ControlError("control_units_changed")
        finally:
            os.close(parent)
    if systemctl("show", "streamhome.service", "--property=DropInPaths", "--value"):
        raise ControlError("control_unit_dropins_unqualified")


def publish_request(payload: dict) -> None:
    selected = descriptor()
    operation = _identity(payload.get("operation_id"))
    root = OPERATIONS / operation
    descriptor_fd = root_directory(root, create=True, gid=selected.service_gid, mode=0o750)
    os.close(descriptor_fd)
    path = root / "request.json"
    if path.exists() or path.is_symlink():
        if read_protected_json(path) != payload:
            raise ControlError("control_request_changed")
        return
    atomic_root_json(path, payload, gid=selected.service_gid, mode=0o640)


def _apply_fixed_boot_effect(payload: dict, observation: dict, *, recovery: bool = False) -> None:
    selected = descriptor()
    if (payload["stop_epoch"] != root_stop_epoch() or payload["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            or observation.get("request_sha256") != _sha(payload) or observation.get("generation") != payload["expected_generation"] + 1
            or observation.get("boot_enabled") is not payload["boot_enabled"] or observation.get("previous_boot_enabled") is not payload["previous_boot_enabled"]):
        raise ControlError("control_os_review_changed")
    controller = operation_unit(payload["operation_id"])
    if (controller["ActiveState"] != "active" or int(controller["MainPID"]) <= 0
            or int(controller["MainPID"]) != observation.get("controller_pid")
            or controller["InvocationID"] != observation.get("controller_invocation_id")):
        raise ControlError("control_controller_changed")
    if payload["initial_service_state"] == "running":
        unit = unit_snapshot()
        if (unit["ActiveState"] not in {"active", "activating"} or unit["InvocationID"] != payload["supervisor_invocation_id"]
                or int(unit["MainPID"]) != payload["supervisor"]["pid"]):
            raise ControlError("control_incarnation_changed")
    else:
        unit = unit_snapshot()
        if (unit['ActiveState'] not in {'inactive', 'failed'} or int(unit['MainPID']) != 0
                or systemctl('show', 'streamhome.service', '--property=Job') not in {'Job=', 'Job=0'}):
            raise ControlError('control_incarnation_changed')
        from .stop import _require_empty
        _require_empty('streamhome.service')
    _verify_owned_units()
    suffix = "-" + str(observation["generation"]) if recovery else ""
    path = OPERATIONS / payload["operation_id"] / ("bridge" + suffix + ".json")
    receipt = {"schema_version": 1, "request_sha256": _sha(payload), "generation": observation["generation"],
               "controller_invocation_id": observation["controller_invocation_id"], "boot_enabled": payload["boot_enabled"], "stage": "applying"}
    if path.exists():
        previous = read_protected_json(path)
        if previous not in (receipt, {**receipt, "stage": "complete"}):
            raise ControlError("control_os_receipt_changed")
    else:
        if _boot_enabled() is not payload["previous_boot_enabled"]:
            raise ControlError("control_boot_policy_changed")
        # The one-use effect intent is durable before the fixed OS command.
        atomic_root_json(path, receipt, mode=0o600)
    if _boot_enabled() is not payload["boot_enabled"]:
        systemctl("enable" if payload["boot_enabled"] else "disable", "streamhome.service")
    if _boot_enabled() is not payload["boot_enabled"]:
        raise ControlError("control_os_verification_failed")
    atomic_root_json(path, {**receipt, "stage": "complete"}, mode=0o600)
    atomic_root_json(OPERATIONS / payload["operation_id"] / ("os-receipt" + suffix + ".json"), {
        "schema_version": 1, "request_sha256": _sha(payload), "operation_id": payload["operation_id"],
        "generation": observation["generation"], "controller_invocation_id": observation["controller_invocation_id"],
        "boot_enabled": payload["boot_enabled"], "stage": "complete",
    }, gid=selected.service_gid, mode=0o640)


def run_operation(payload: dict, *, start: bool = True, recovery_grant: dict | None = None) -> dict:
    require_root()
    operation = _identity(payload.get("operation_id"))
    if recovery_grant is None:
        publish_request(payload)
    else:
        generation = payload.get("expected_generation")
        if (type(generation) is not int or not 1 <= generation < 2**63 - 1
                or recovery_grant.get("request") != payload):
            raise ControlError("control_recovery_request_invalid")
        selected = descriptor()
        unit = operation_unit(operation)
        if unit['ActiveState'] not in {'inactive', 'failed'} or int(unit['MainPID']) != 0:
            raise ControlError('control_controller_busy')
        from .stop import _require_empty
        _require_empty(f'streamhome-operation@{operation}.service')
        retained = OPERATIONS / operation / ('recovery-review-' + _identity(recovery_grant.get('recovery_review_id')) + '.json')
        if retained.exists() or retained.is_symlink():
            if read_protected_json(retained) != recovery_grant:
                raise ControlError('control_recovery_request_changed')
        else:
            atomic_root_json(retained, recovery_grant, gid=selected.service_gid, mode=0o640)
        path = OPERATIONS / operation / f"recovery-{generation + 1}.json"
        if path.exists() or path.is_symlink():
            previous = read_protected_json(path)
            previous_review = _identity(previous.get('recovery_review_id'))
            previous_retained = OPERATIONS / operation / ('recovery-review-' + previous_review + '.json')
            if read_protected_json(previous_retained) != previous:
                raise ControlError('control_recovery_request_changed')
        atomic_root_json(path, recovery_grant, gid=selected.service_gid, mode=0o640)
    if start:
        # Type=exec acknowledges the executable, not operation completion. This
        # avoids treating a merely queued start job as an exited controller.
        systemctl("start", f"streamhome-operation@{operation}.service", timeout=30)
    deadline = time.monotonic() + 300
    effect_done = False
    last_stage = None
    while time.monotonic() < deadline:
        unit = operation_unit(operation)
        if unit["ActiveState"] == "active" and int(unit["MainPID"]) > 0 and unit["InvocationID"]:
            try:
                observation = run_worker(["bridge-observe"], payload={"operation_id": operation, "controller_pid": int(unit["MainPID"]), "controller_invocation_id": unit["InvocationID"]})
            except (ControlError, subprocess.TimeoutExpired):
                observation = None
            if observation is not None:
                if observation.get("stage") != last_stage:
                    last_stage = observation.get("stage")
                    print("[StreamHome] Management: " + str(last_stage), flush=True)
                if observation.get("os_effect_requested") is True and not effect_done:
                    if recovery_grant is None:
                        _apply_fixed_boot_effect(payload, observation)
                    else:
                        _apply_fixed_boot_effect(payload, observation, recovery=True)
                    effect_done = True
        if unit["ActiveState"] in {"inactive", "failed"}:
            try:
                result = run_worker(["operation-result", operation])
            except ControlError:
                result = {"completed": False}
            if result.get("completed") is True:
                return result
            raise ControlError("control_operation_recovery_required")
        time.sleep(0.2)
    raise ControlError("control_operation_timeout")


def configure(*, request_path: Path | None = None) -> int:
    require_root()
    selected = descriptor()
    if selected.status.value != "ready":
        raise ControlError("control_installation_incomplete")
    snapshot = run_worker(["inspect"], timeout=120)
    if snapshot["lifecycle"]["active_operation"] is not None:
        raise ControlError("control_operation_busy")
    active = parse_deployment_payload(snapshot["active_deployment"])
    desired = parse_deployment_payload(snapshot["desired_deployment"])
    enabled = _boot_enabled()
    current = ConfigurationDraft(desired, enabled)
    processes = process_snapshot()
    def diagnostics(config):
        web, invocation = None, None
        if processes["initial_service_state"] == "running":
            unit = unit_snapshot()
            snapshot = supervisor_snapshot(selected, unit)
            web, invocation = snapshot.get("children", {}).get("web"), unit["InvocationID"]
        from .listener_review import check_requested_listener
        return check_requested_listener(config, web=web, invocation=invocation)
    if request_path is None:
        with open_console() as console:
            review = configure_wizard(console, current, running=processes["initial_service_state"] == "running",
                                      active_deployment=active, diagnostics=diagnostics)
    else:
        # Explicit noninteractive input is data only and carries its own CAS and
        # consent. No shell/source/environment file is evaluated.
        descriptor_fd = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor_fd, "rb") as handle:
            import stat
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ControlError("control_request_invalid")
            parsed = parse_configuration_request(parse_request_json(handle.read(32769)), current, active_deployment=active)
        if parsed.expected_desired_revision != snapshot["configuration"]["deployment"]["desired"]["revision"]:
            raise ControlError("control_review_stale")
        review = parsed.review
        if review.before.deployment != review.after.deployment or review.activate_deployment:
            diagnostics(review.after.deployment)
    if review is None:
        return 0
    if processes["initial_service_state"] == "running" and processes["backend"] is None:
        if not review.activate_deployment:
            raise ControlError("control_save_requires_settled_runtime")
        if request_path is None:
            with open_console() as console:
                console.write("The backend cannot acknowledge normal admission. Applying this change will interrupt the owned runtime.")
                if not console.confirm("Continue with the independent interrupting path?"):
                    return 0
    payload = {"purpose": "configure", "operation_id": str(uuid.uuid4()), "review_id": str(uuid.uuid4()),
               "expected_generation": snapshot["lifecycle"]["generation"], "expected_desired_revision": snapshot["configuration"]["deployment"]["desired"]["revision"],
               "deployment": review.after.deployment.payload(), "activate_deployment": review.activate_deployment,
               "boot_enabled": review.after.start_at_boot, "previous_boot_enabled": enabled,
               "interruption_approved": review.activate_deployment,
               "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "stop_epoch": root_stop_epoch(), **processes}
    prepared = run_worker(["prepare-request"], payload=payload)
    if prepared.get("no_changes") is True:
        print("No changes; configuration and services are unchanged.")
        return 0
    result = run_operation(prepared["request"])
    print("StreamHome configuration " + ("saved; the deployment remains pending." if result["stage"] == "saved" else "applied."))
    return 0
