"""Bound root startup observer; pending deployment still uses MANAGEMENT."""
from __future__ import annotations

import os
from pathlib import Path
import re
import time
import uuid

from ..installation import InstallationStatus
from .administration import _boot_enabled, process_snapshot, root_stop_epoch, run_operation
from .privileged import descriptor, require_root, run_worker, systemctl, unit_snapshot
from .privileged import supervisor_snapshot
from .provisioning import administrative_lock
from .rpc import ControlError


def _invocation(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value) or not int(value, 16):
        raise ControlError("control_incarnation_changed")
    return value


def schedule_pending() -> int:
    require_root()
    if Path("/proc/self/cgroup").read_text().splitlines() != ["0::/system.slice/streamhome.service"]:
        raise ControlError("control_unit_hook_forbidden")
    if descriptor().status is InstallationStatus.PROVISIONING:
        return 0
    invocation = _invocation(os.environ.get("INVOCATION_ID", ""))
    systemctl("start", "--no-block", f"streamhome-startup@{invocation}.service")
    return 0


def after_start() -> int:
    # Retained only as an installed-control compatibility entry. New unit files
    # schedule from ExecStartPre and never depend on ExecStartPost for readiness.
    return schedule_pending()


def wait_supervisor(invocation: str, stop_epoch: str) -> dict:
    deadline = time.monotonic() + 120
    selected = descriptor()
    while time.monotonic() < deadline:
        if root_stop_epoch() != stop_epoch:
            raise ControlError("control_operator_stopped")
        unit = unit_snapshot()
        if unit["InvocationID"] != invocation or unit["ActiveState"] not in {"active", "activating"}:
            raise ControlError("control_incarnation_changed")
        if int(unit["MainPID"]) > 0:
            try:
                snapshot = supervisor_snapshot(selected, unit)
                if snapshot.get("launches_open") is not True:
                    raise ControlError("control_operator_stopped")
                return unit
            except ControlError as exc:
                if exc.code not in {"control_unavailable", "control_reply_incomplete", "control_timeout"}:
                    raise
        time.sleep(0.25)
    raise ControlError("control_supervisor_start_timeout")


def activate_pending(invocation: str) -> int:
    require_root()
    _invocation(invocation)
    if Path("/proc/self/cgroup").read_text().splitlines() != [f"0::/system.slice/streamhome-startup@{invocation}.service"]:
        raise ControlError("control_unit_hook_forbidden")
    with administrative_lock():
        if descriptor().status is not InstallationStatus.READY:
            raise ControlError("control_installation_incomplete")
        epoch = root_stop_epoch()
        unit = wait_supervisor(invocation, epoch)
        snapshot = run_worker(["inspect"], timeout=120)
        if snapshot["lifecycle"]["active_operation"] is not None:
            raise ControlError("control_operation_recovery_required")
        if snapshot["configuration"]["deployment"]["active"] == snapshot["configuration"]["deployment"]["desired"]:
            return 0
        processes = process_snapshot()
        if processes["initial_service_state"] != "running" or processes["supervisor_invocation_id"] != invocation:
            raise ControlError("control_incarnation_changed")
        enabled = _boot_enabled()
        payload = {"purpose": "activate_pending", "operation_id": str(uuid.uuid4()), "review_id": str(uuid.uuid4()),
            "expected_generation": snapshot["lifecycle"]["generation"],
            "expected_desired_revision": snapshot["configuration"]["deployment"]["desired"]["revision"],
            "deployment": snapshot["desired_deployment"], "activate_deployment": True,
            "boot_enabled": enabled, "previous_boot_enabled": enabled, "interruption_approved": True,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "stop_epoch": epoch, **processes}
        prepared = run_worker(["prepare-request"], payload=payload)
        if prepared.get("no_changes") is not False:
            raise ControlError("control_pending_deployment_ineligible")
        result = run_operation(prepared["request"])
        if result.get("completed") is not True or result.get("stage") != "succeeded":
            raise ControlError("control_operation_recovery_required")
    return 0
