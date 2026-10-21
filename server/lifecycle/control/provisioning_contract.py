"""First-install root intent contracts, independent of application authority.

This record binds fixed OS effects and the provisional identity. It never replaces
the unprivileged lifecycle authority after registration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
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
class StartAttempt:
    review_id: str
    previous_intent_sha256: str
    permit: str
    invocation: str | None
    boot_id: str
    stop_epoch: str


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
    start_attempts: tuple[StartAttempt, ...] = ()

    def payload(self) -> dict:
        value = asdict(self)
        value["schema_version"] = 2 if self.start_attempts else 1
        if not self.start_attempts:
            value.pop("start_attempts")
        else:
            value["start_attempts"] = [asdict(attempt) for attempt in self.start_attempts]
        value["directories"] = {name: {"device": device, "inode": inode} for name, device, inode in self.directories}
        value["mirror_directories"] = {name: {"device": device, "inode": inode} for name, device, inode in self.mirror_directories}
        return value


def parse_intent(value: Any) -> ProvisioningIntent:
    if not isinstance(value, dict):
        raise ControlError("control_installation_journal_invalid")
    value = dict(value)
    version = value.pop("schema_version", None)
    if type(version) is not int or version not in {1, 2}:
        raise ControlError("control_installation_journal_unsupported")
    expected = set(ProvisioningIntent.__dataclass_fields__)
    if version == 1:
        expected.remove("start_attempts")
    if set(value) != expected:
        raise ControlError("control_installation_journal_invalid")
    attempts = value.get("start_attempts", [])
    if (not isinstance(attempts, list) or len(attempts) > 32 or version == 2 and not attempts):
        raise ControlError("control_installation_journal_invalid")
    parsed_attempts = []
    for attempt in attempts:
        if not isinstance(attempt, dict) or set(attempt) != set(StartAttempt.__dataclass_fields__):
            raise ControlError("control_installation_journal_invalid")
        identity(attempt["review_id"])
        identity(attempt["boot_id"])
        if (not isinstance(attempt["permit"], str) or attempt["permit"] not in {"issued", "consumed"}
                or any(not isinstance(attempt[key], str) or not _SHA.fullmatch(attempt[key]) for key in ("previous_intent_sha256", "stop_epoch"))
                or attempt["permit"] == "issued" and attempt["invocation"] is not None
                or attempt["permit"] == "consumed" and (not isinstance(attempt["invocation"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", attempt["invocation"]) or not int(attempt["invocation"], 16))):
            raise ControlError("control_installation_journal_invalid")
        parsed_attempts.append(StartAttempt(**attempt))
    value["start_attempts"] = tuple(parsed_attempts)
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
    if value["start_attempts"] and (stage < STAGES.index("first_start_requested") or value["start_permit"] == "absent"):
        raise ControlError("control_installation_journal_invalid")
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
    intent = ProvisioningIntent(**value)
    if len({attempt.review_id for attempt in intent.start_attempts}) != len(intent.start_attempts):
        raise ControlError("control_installation_journal_invalid")
    for index, attempt in enumerate(intent.start_attempts):
        previous = replace(intent, stage="first_start_requested", start_attempts=intent.start_attempts[:index],
            start_permit=attempt.permit, start_invocation=attempt.invocation,
            start_boot_id=attempt.boot_id, start_stop_epoch=attempt.stop_epoch)
        if intent_sha256(previous) != attempt.previous_intent_sha256:
            raise ControlError("control_installation_journal_invalid")
    return intent


def intent_sha256(intent: ProvisioningIntent) -> str:
    return hashlib.sha256(json.dumps(intent.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def reviewed_start_attempt(current: ProvisioningIntent, *, review_id: str, boot_id: str, stop_epoch: str) -> ProvisioningIntent:
    """Describe a distinct explicit attempt; parsing grants no launch authority."""
    parse_intent(current.payload())
    identity(review_id)
    identity(boot_id)
    if (current.stage != "first_start_requested" or current.start_permit not in {"issued", "consumed"}
            or not isinstance(stop_epoch, str) or not _SHA.fullmatch(stop_epoch)):
        raise ControlError("control_start_retry_unavailable")
    if len(current.start_attempts) >= 32:
        raise ControlError("control_start_history_full")
    archived = StartAttempt(review_id, intent_sha256(current), current.start_permit, current.start_invocation,
        current.start_boot_id, current.start_stop_epoch)
    candidate = replace(current, start_attempts=(*current.start_attempts, archived), start_permit="issued",
        start_invocation=None, start_boot_id=boot_id, start_stop_epoch=stop_epoch)
    return parse_intent(candidate.payload())


def advance_intent(current: ProvisioningIntent, **changes) -> ProvisioningIntent:
    candidate = parse_intent(replace(current, **changes).payload())
    immutable = ("installation_id", "operation_id", "revision_id", "service_uid", "service_gid", "deployment",
                 "start_at_boot", "control_bundle_id", "control_manifest_sha256", "start_attempts")
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
