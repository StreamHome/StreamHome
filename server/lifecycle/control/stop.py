"""Global stop of exact publisher-owned runtime and auxiliary unit instances."""
from __future__ import annotations

from pathlib import Path
import re
import uuid

from .privileged import require_root, systemctl, unit_snapshot
from .rpc import ControlError


_ROLES = ("operation", "prepare", "provision", "startup")
_INSTANCE = re.compile(r"streamhome-(operation|prepare|provision|startup)@([0-9a-f-]+)\.service$")


def installation_scopes() -> tuple[str, ...]:
    raw = systemctl("list-units", "--all", "--plain", "--no-legend", "--full", "--type=scope", "streamhome-install-*.scope")
    result = set()
    for line in raw.splitlines():
        fields = line.split()
        if not fields:
            continue
        from .provisioning import read_intent
        intent = read_intent()
        name = "streamhome-install-" + intent.installation_id + ".scope"
        if fields[0] != name:
            raise ControlError("control_stop_unit_unqualified")
        observed = systemctl("show", name, "--property=Transient,ControlGroup,Description")
        values = dict(field.split("=", 1) for field in observed.splitlines() if "=" in field)
        if (set(values) != {"Transient", "ControlGroup", "Description"} or values["Transient"] != "yes"
                or values["ControlGroup"] not in {"", "/system.slice/" + name}
                or values["Description"] != "StreamHome installation " + intent.installation_id):
            raise ControlError("control_stop_unit_unqualified")
        result.add(name)
    return tuple(sorted(result))


def auxiliary_units() -> tuple[str, ...]:
    raw = systemctl("list-units", "--all", "--plain", "--no-legend", "--full", "--type=service",
        *("streamhome-" + role + "@*.service" for role in _ROLES))
    units = set()
    for line in raw.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0]
        match = _INSTANCE.fullmatch(name)
        if match is None:
            raise ControlError("control_stop_unit_unqualified")
        role, identity = match.groups()
        if role == "startup":
            valid = re.fullmatch(r"[0-9a-f]{32}", identity) is not None and identity != "0" * 32
        else:
            try:
                parsed = uuid.UUID(identity)
                valid = str(parsed) == identity and parsed.int != 0
            except ValueError:
                valid = False
        if not valid or len(units) >= 128:
            raise ControlError("control_stop_unit_unqualified")
        expected = {"FragmentPath": "/etc/systemd/system/streamhome-" + role + "@.service",
            "User": "root" if role == "startup" else "streamhome"}
        metadata = systemctl("show", name, "--property=FragmentPath,User,ControlGroup")
        observed = dict(field.split("=", 1) for field in metadata.splitlines() if "=" in field)
        if (set(observed) != {*expected, "ControlGroup"}
                or any(observed[key] != value for key, value in expected.items())
                or observed["ControlGroup"] not in {"", "/system.slice/" + name}):
            raise ControlError("control_stop_unit_unqualified")
        units.add(name)
    return tuple(sorted(units | set(installation_scopes())))


def _require_empty(name: str) -> None:
    path = Path("/sys/fs/cgroup/system.slice") / name / "cgroup.procs"
    try:
        if path.read_text().strip():
            raise ControlError("control_stop_children_busy")
    except FileNotFoundError:
        pass


def stop_units(*, runtime_registered: bool) -> None:
    """Return only after systemd jobs and known owned cgroups are stopped.

    The caller records stop intent and closes supervisor launch admission first.
    This function never stops an unrelated matching-name unit, kills by name or
    treats a dead unit leader as proof that its descendants are gone.
    """
    require_root()
    before = auxiliary_units()
    targets = (("streamhome.service",) if runtime_registered else ()) + before
    if targets:
        systemctl("stop", *targets, timeout=75)
    after = auxiliary_units()
    if set(after) - set(before):
        raise ControlError("control_stop_unit_changed")
    for name in targets:
        _require_empty(name)
    if runtime_registered:
        runtime = unit_snapshot()
        if runtime["ActiveState"] not in {"inactive", "failed"} or int(runtime["MainPID"]) != 0:
            raise ControlError("control_stop_runtime_busy")
