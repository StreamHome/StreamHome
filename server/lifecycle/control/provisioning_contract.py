"""First-install root intent contracts, independent of application authority.

This record binds fixed OS effects and the provisional identity. It never replaces
the unprivileged lifecycle authority after registration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import re
from typing import Any
import uuid

from ..deployment_config import parse_deployment_payload
from .rpc import ControlError


JOURNAL_PATH = "/etc/streamhome/control/installation.json"
SERVICE_DIRECTORIES = (
    "/opt/streamhome", "/etc/streamhome/application", "/etc/streamhome/domains",
    "/var/lib/streamhome", "/var/cache/streamhome",
)
STAGES = ("intent", "account_ready", "layout_ready", "source_selected", "preparing_application",
          "application_prepared", "first_start_requested", "registered", "complete")
_SHA = re.compile(r"[0-9a-f]{64}")


def identity(value: Any) -> str:
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError as exc:
        raise ControlError("control_installation_journal_invalid") from exc
    if parsed is None or not parsed.int or str(parsed) != value:
        raise ControlError("control_installation_journal_invalid")
    return value


@dataclass(frozen=True)
class ProvisioningIntent:
    installation_id: str
    operation_id: str
    revision_id: str
    service_uid: int
    service_gid: int
    deployment: dict
    start_at_boot: bool
    control_bundle_id: str
    control_manifest_sha256: str
    stage: str = "intent"
    directories: tuple[tuple[str, int, int], ...] = ()
    mirror_directories: tuple[tuple[str, int, int], ...] = ()
    application_commit: str | None = None
    execution_sha256: str | None = None
    start_permit: str = "absent"
    start_invocation: str | None = None
    start_boot_id: str | None = None
    start_stop_epoch: str | None = None

    def payload(self) -> dict:
        value = asdict(self)
        value["schema_version"] = 1
        value["directories"] = {name: {"device": device, "inode": inode} for name, device, inode in self.directories}
        value["mirror_directories"] = {name: {"device": device, "inode": inode} for name, device, inode in self.mirror_directories}
        return value


def parse_intent(value: Any) -> ProvisioningIntent:
    if not isinstance(value, dict) or set(value) != set(ProvisioningIntent.__dataclass_fields__) | {"schema_version"}:
        raise ControlError("control_installation_journal_invalid")
    value = dict(value)
    version = value.pop("schema_version")
    if type(version) is not int or version != 1:
        raise ControlError("control_installation_journal_unsupported")
    for name in ("installation_id", "operation_id", "revision_id", "control_bundle_id"):
        identity(value[name])
    if (type(value["start_at_boot"]) is not bool or value["stage"] not in STAGES
            or any(type(value[name]) is not int or not 100 <= value[name] <= 999 for name in ("service_uid", "service_gid"))):
        raise ControlError("control_installation_journal_invalid")
    deployment = parse_deployment_payload(value["deployment"])
    if deployment.payload() != value["deployment"]:
        raise ControlError("control_installation_journal_invalid")
    if not isinstance(value["control_manifest_sha256"], str) or not _SHA.fullmatch(value["control_manifest_sha256"]):
        raise ControlError("control_installation_journal_invalid")
    directories = value["directories"]
    if not isinstance(directories, dict) or set(directories) - set(SERVICE_DIRECTORIES):
        raise ControlError("control_installation_journal_invalid")
    bindings = []
    for name, binding in directories.items():
        if (not isinstance(binding, dict) or set(binding) != {"device", "inode"}
                or any(type(binding[key]) is not int or not 0 < binding[key] < 2**64 for key in binding)):
            raise ControlError("control_installation_journal_invalid")
        bindings.append((name, binding["device"], binding["inode"]))
    value["directories"] = tuple(sorted(bindings))
    mirrors = value["mirror_directories"]
    if not isinstance(mirrors, dict) or set(mirrors) - {"provisioning", "provisioning/" + value["installation_id"]}:
        raise ControlError("control_installation_journal_invalid")
    mirror_bindings = []
    for name, binding in mirrors.items():
        if (not isinstance(binding, dict) or set(binding) != {"device", "inode"}
                or any(type(binding[key]) is not int or not 0 < binding[key] < 2**64 for key in binding)):
            raise ControlError("control_installation_journal_invalid")
        mirror_bindings.append((name, binding["device"], binding["inode"]))
    value["mirror_directories"] = tuple(sorted(mirror_bindings))
    stage = STAGES.index(value["stage"])
    commit, execution = value["application_commit"], value["execution_sha256"]
    if (commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit))
            or execution is not None and (not isinstance(execution, str) or not _SHA.fullmatch(execution))
            or stage >= STAGES.index("layout_ready") and set(directories) != set(SERVICE_DIRECTORIES)
            or stage >= STAGES.index("layout_ready") and len(mirrors) != 2
            or stage >= STAGES.index("source_selected") and commit is None
            or stage >= STAGES.index("application_prepared") and execution is None):
        raise ControlError("control_installation_journal_invalid")
    if not isinstance(value["start_permit"], str) or value["start_permit"] not in {"absent", "issued", "consumed"}:
        raise ControlError("control_installation_journal_invalid")
    invocation, boot, epoch = value["start_invocation"], value["start_boot_id"], value["start_stop_epoch"]
    if value["start_permit"] == "absent":
        if any(item is not None for item in (invocation, boot, epoch)) or stage >= STAGES.index("first_start_requested"):
            raise ControlError("control_installation_journal_invalid")
    else:
        identity(boot)
        if (not isinstance(epoch, str) or not _SHA.fullmatch(epoch)
                or stage < STAGES.index("first_start_requested")
                or value["start_permit"] == "issued" and invocation is not None
                or value["start_permit"] == "consumed" and (not isinstance(invocation, str) or not re.fullmatch(r"[0-9a-f]{32}", invocation) or not int(invocation, 16))):
            raise ControlError("control_installation_journal_invalid")
    if stage >= STAGES.index("registered") and value["start_permit"] != "consumed":
        raise ControlError("control_installation_journal_invalid")
    return ProvisioningIntent(**value)


def advance_intent(current: ProvisioningIntent, **changes) -> ProvisioningIntent:
    candidate = parse_intent(replace(current, **changes).payload())
    immutable = ("installation_id", "operation_id", "revision_id", "service_uid", "service_gid", "deployment",
                 "start_at_boot", "control_bundle_id", "control_manifest_sha256")
    if any(getattr(current, name) != getattr(candidate, name) for name in immutable):
        raise ControlError("control_installation_review_changed")
    old_stage, next_stage = STAGES.index(current.stage), STAGES.index(candidate.stage)
    if not old_stage <= next_stage <= old_stage + 1:
        raise ControlError("control_installation_stage_invalid")
    old_dirs = {item[0]: item[1:] for item in current.directories}
    new_dirs = {item[0]: item[1:] for item in candidate.directories}
    if any(new_dirs.get(name) != binding for name, binding in old_dirs.items()):
        raise ControlError("control_installation_directory_changed")
    old_mirrors = {item[0]: item[1:] for item in current.mirror_directories}
    new_mirrors = {item[0]: item[1:] for item in candidate.mirror_directories}
    if any(new_mirrors.get(name) != binding for name, binding in old_mirrors.items()):
        raise ControlError("control_installation_directory_changed")
    for name in ("application_commit", "execution_sha256", "start_boot_id", "start_stop_epoch", "start_invocation"):
        if getattr(current, name) is not None and getattr(candidate, name) != getattr(current, name):
            raise ControlError("control_installation_review_changed")
    if (current.start_permit, candidate.start_permit) not in {
        ("absent", "absent"), ("absent", "issued"), ("issued", "issued"), ("issued", "consumed"), ("consumed", "consumed"),
    }:
        raise ControlError("control_start_permit_consumed")
    return candidate
