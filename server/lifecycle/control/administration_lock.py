"""Root administrator ownership evidence subordinate to the held OS flock."""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import uuid

from .root_files import atomic_root_json
from .rpc import ControlError
from .tickets import read_protected_json


OWNER = Path("/etc/streamhome/control/administration-owner.json")


def process_identity(pid: int) -> tuple[str, str]:
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields = raw.rsplit(")", 1)[1].split()
    value = fields[19]
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise ControlError("control_administrator_identity_unavailable")
    return value, fields[0]


def process_start(pid: int) -> str:
    return process_identity(pid)[0]


def parse_owner(value: dict) -> dict:
    fields = {"schema_version", "pid", "start_identity", "boot_id", "nonce", "status"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["pid"]) is not int or not 1 < value["pid"] < 2**31
            or not isinstance(value["start_identity"], str) or not re.fullmatch(r"[0-9]{1,20}", value["start_identity"])
            or not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["nonce"])
            or not isinstance(value["status"], str) or value["status"] not in {"active", "released"}):
        raise ControlError("control_administrator_owner_untrusted")
    try:
        boot = uuid.UUID(value["boot_id"]) if isinstance(value["boot_id"], str) else None
    except ValueError as exc:
        raise ControlError("control_administrator_owner_untrusted") from exc
    if boot is None or not boot.int or str(boot) != value["boot_id"]:
        raise ControlError("control_administrator_owner_untrusted")
    return value


def previous_owner_released(value: dict, boot_id: str) -> bool:
    value = parse_owner(value)
    if value["status"] == "released" or value["boot_id"] != boot_id:
        return True
    try:
        start, status = process_identity(value["pid"])
        return start != value["start_identity"] or status in {"Z", "X"}
    except FileNotFoundError:
        return True
    # Permission/format/I/O errors remain ambiguous; they never prove death.


def enter_owner() -> dict:
    if os.geteuid() != 0:
        raise ControlError("control_root_required")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if OWNER.exists() or OWNER.is_symlink():
        previous = read_protected_json(OWNER, maximum=4096)
        if not previous_owner_released(previous, boot_id):
            raise ControlError("control_administrator_handoff_required")
    elif (Path("/etc/streamhome/install.json").exists() or Path("/etc/streamhome/control/installation.json").exists()):
        raise ControlError("control_administrator_owner_missing")
    value = parse_owner({"schema_version":1,"pid":os.getpid(),"start_identity":process_start(os.getpid()),
        "boot_id":boot_id,"nonce":secrets.token_hex(32),"status":"active"})
    atomic_root_json(OWNER, value, mode=0o600)
    return value


def release_owner(expected: dict) -> None:
    if read_protected_json(OWNER, maximum=4096) != expected:
        raise ControlError("control_administrator_owner_changed")
    atomic_root_json(OWNER, {**expected, "status":"released"}, mode=0o600)
