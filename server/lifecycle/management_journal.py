"""Read-only management protocol contracts; no activation or authority grants.

These validators describe subordinate evidence. Parsing, hashes and an authority
snapshot never authorize a mutation, controller takeover, launch or completion.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping
import uuid

from . import state


JOURNAL_SCHEMA_VERSION = 1
MAX_JOURNAL_BYTES = 32 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BUILD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_INVOCATION = re.compile(r"^[0-9a-f]{32}$")
_DOMAIN = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


class ManagementJournalError(ValueError):
    code = "invalid_management_journal"


class ManagementAction(str, Enum):
    CONFIGURE_APPLY = "configure_apply"
    REPAIR_ENVIRONMENT = "repair_environment"
    PROVISION_CONTROL = "provision_control"


class ActivationMode(str, Enum):
    CUTOVER = "cutover"
    SAVE_ONLY = "save_only"
    OS_ONLY = "os_only"


class ManagementStage(str, Enum):
    RESERVED = "reserved"
    PREPARED = "prepared"
    EXCLUSIVE = "exclusive"
    VERIFYING = "verifying"
    ACCEPTED = "accepted"
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    SAVED = "saved"
    OS_COMPLETED = "os_completed"


@dataclass(frozen=True)
class ExecutionTuple:
    application_commit: str
    application_build_id: str
    deployment_revision: str
    runtime_bundle_id: str
    runtime_manifest_sha256: str
    python_environment_id: str
    python_executable: str
    node_environment_id: str
    node_executable: str
    web_bundle_id: str
    web_dependency_target: str
    web_dist_target: str
    web_asset_manifest_sha256: str
    control_bundle_id: str
    control_manifest_sha256: str
    control_protocol_version: int
    policy_revisions: tuple[tuple[str, int], ...]
    provider_revisions: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class AcceptanceEvidence:
    execution_tuple_sha256: str
    verification_sha256: str


@dataclass(frozen=True)
class ManagementJournal:
    installation_id: str
    operation_id: str
    generation: int
    action: ManagementAction
    activation_mode: ActivationMode
    review_id: str
    previous: ExecutionTuple
    candidate: ExecutionTuple
    initial_service_state: str
    controller_invocation_id: str
    supervisor_invocation_id: str | None
    stage: ManagementStage
    acceptance: AcceptanceEvidence | None


def _object(value: Any, keys: set[str], description: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ManagementJournalError(f"{description} contains missing or unknown fields")
    return value


def _text(value: Any, pattern: re.Pattern[str], description: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ManagementJournalError(f"{description} is invalid")
    return value


def _uuid(value: Any, description: str) -> str:
    if not isinstance(value, str):
        raise ManagementJournalError(f"{description} is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ManagementJournalError(f"{description} is invalid") from exc
    if str(parsed) != value or parsed.int == 0:
        raise ManagementJournalError(f"{description} is not a canonical nonzero UUID")
    return value


def _integer(value: Any, minimum: int, description: str) -> int:
    if type(value) is not int or not minimum <= value <= (1 << 63) - 1:
        raise ManagementJournalError(f"{description} is invalid")
    return value


def _revisions(value: Any) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict) or len(value) > 16:
        raise ManagementJournalError("domain revisions are invalid")
    return tuple(sorted(
        (_text(name, _DOMAIN, "revision domain"), _integer(revision, 0, "domain revision"))
        for name, revision in value.items()
    ))


def execution_tuple_payload(execution: ExecutionTuple) -> dict[str, Any]:
    payload = asdict(execution)
    payload["policy_revisions"] = dict(execution.policy_revisions)
    payload["provider_revisions"] = dict(execution.provider_revisions)
    return payload


def execution_tuple_sha256(execution: ExecutionTuple) -> str:
    payload = execution_tuple_payload(execution)
    if parse_execution_tuple(payload) != execution:
        raise ManagementJournalError("execution tuple is not canonical and immutable")
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def parse_execution_tuple(payload: Any) -> ExecutionTuple:
    fields = set(ExecutionTuple.__dataclass_fields__)
    value = dict(_object(payload, fields, "execution tuple"))
    for name in ("deployment_revision", "runtime_bundle_id", "python_environment_id",
                 "node_environment_id", "web_bundle_id", "control_bundle_id"):
        value[name] = _uuid(value[name], name)
    for name in ("runtime_manifest_sha256", "web_asset_manifest_sha256", "control_manifest_sha256"):
        value[name] = _text(value[name], _SHA256, name)
    value["application_commit"] = _text(value["application_commit"], _COMMIT, "application commit")
    value["application_build_id"] = _text(value["application_build_id"], _BUILD, "application build")
    if type(value["control_protocol_version"]) is not int or value["control_protocol_version"] != 1:
        raise ManagementJournalError("control protocol is unsupported")
    runtime = PurePosixPath("/opt/streamhome/runtime")
    paths = {
        "python_executable": runtime / "python" / value["python_environment_id"] / "venv/bin/python",
        "node_executable": runtime / "node" / value["node_environment_id"] / "bin/node",
        "web_dependency_target": runtime / "web" / value["web_bundle_id"] / "node_modules",
        "web_dist_target": runtime / "web" / value["web_bundle_id"] / "dist",
    }
    for name, expected in paths.items():
        if value[name] != str(expected):
            raise ManagementJournalError("execution path does not match its bound generation")
    for name in ("policy_revisions", "provider_revisions"):
        value[name] = _revisions(value[name])
    return ExecutionTuple(**value)


def parse_management_journal(payload: Any) -> ManagementJournal:
    fields = set(ManagementJournal.__dataclass_fields__) | {"schema_version"}
    value = dict(_object(payload, fields, "management journal"))
    version = value.pop("schema_version")
    if type(version) is not int or version != JOURNAL_SCHEMA_VERSION:
        raise ManagementJournalError("management journal schema is unsupported")
    for name in ("installation_id", "operation_id", "review_id"):
        value[name] = _uuid(value[name], name)
    value["generation"] = _integer(value["generation"], 1, "operation generation")
    try:
        value["action"] = ManagementAction(value["action"])
        value["activation_mode"] = ActivationMode(value["activation_mode"])
        value["stage"] = ManagementStage(value["stage"])
    except (TypeError, ValueError) as exc:
        raise ManagementJournalError("management action, activation mode or stage is unsupported") from exc
    for name in ("previous", "candidate"):
        value[name] = parse_execution_tuple(value[name])
    if value["initial_service_state"] not in ("running", "stopped"):
        raise ManagementJournalError("initial service state is invalid")
    value["controller_invocation_id"] = _text(value["controller_invocation_id"], _INVOCATION, "controller invocation")
    supervisor = value["supervisor_invocation_id"]
    if supervisor is not None:
        value["supervisor_invocation_id"] = _text(supervisor, _INVOCATION, "supervisor invocation")
    if value["controller_invocation_id"] == "0" * 32 or supervisor == "0" * 32:
        raise ManagementJournalError("invocation identity must be nonzero")
    if (value["initial_service_state"] == "running") != (supervisor is not None):
        raise ManagementJournalError("supervisor invocation does not match initial service state")
    acceptance = value["acceptance"]
    if acceptance is not None:
        acceptance = _object(acceptance, {"execution_tuple_sha256", "verification_sha256"}, "acceptance")
        value["acceptance"] = AcceptanceEvidence(**{
            name: _text(digest, _SHA256, name) for name, digest in acceptance.items()
        })
    journal = ManagementJournal(**value)
    _validate_action(journal)
    return journal


def _validate_action(journal: ManagementJournal) -> None:
    action, mode, stage = journal.action, journal.activation_mode, journal.stage
    if (
        mode is ActivationMode.SAVE_ONLY and action is not ManagementAction.CONFIGURE_APPLY
        or mode is ActivationMode.OS_ONLY and action is not ManagementAction.PROVISION_CONTROL
    ):
        raise ManagementJournalError("action cannot use the selected activation mode")
    previous = execution_tuple_payload(journal.previous)
    candidate = execution_tuple_payload(journal.candidate)
    if mode is ActivationMode.OS_ONLY:
        allowed_changes: set[str] = set()
    elif action is ManagementAction.CONFIGURE_APPLY:
        allowed_changes = {"deployment_revision"}
    elif action is ManagementAction.REPAIR_ENVIRONMENT:
        allowed_changes = {
            "runtime_bundle_id", "runtime_manifest_sha256", "python_environment_id", "python_executable",
            "node_environment_id", "node_executable", "web_bundle_id", "web_dependency_target", "web_dist_target",
        }
    else:
        allowed_changes = {"control_bundle_id", "control_manifest_sha256", "control_protocol_version"}
    if any(previous[name] != candidate[name] for name in previous.keys() - allowed_changes):
        raise ManagementJournalError("action changes an unrelated execution identity")
    common = {ManagementStage.RESERVED, ManagementStage.PREPARED, ManagementStage.RECOVERY_REQUIRED}
    if mode is ActivationMode.CUTOVER:
        allowed_stages = common | {
            ManagementStage.EXCLUSIVE, ManagementStage.VERIFYING, ManagementStage.ACCEPTED,
            ManagementStage.SUCCEEDED, ManagementStage.ROLLED_BACK,
        }
    elif mode is ActivationMode.SAVE_ONLY:
        allowed_stages = common | {ManagementStage.SAVED}
    else:
        allowed_stages = common | {ManagementStage.OS_COMPLETED}
    if stage not in allowed_stages:
        raise ManagementJournalError("stage is incompatible with activation mode")
    acceptance = journal.acceptance
    if mode is not ActivationMode.CUTOVER and acceptance is not None:
        raise ManagementJournalError("non-cutover work cannot accept a candidate runtime")
    if stage in {ManagementStage.ACCEPTED, ManagementStage.SUCCEEDED} and acceptance is None:
        raise ManagementJournalError("accepted runtime requires acceptance evidence")
    if acceptance is not None:
        if stage not in {ManagementStage.ACCEPTED, ManagementStage.SUCCEEDED, ManagementStage.RECOVERY_REQUIRED}:
            raise ManagementJournalError("acceptance cannot precede verification or permit rollback")
        if acceptance.execution_tuple_sha256 != execution_tuple_sha256(journal.candidate):
            raise ManagementJournalError("acceptance does not identify the exact candidate")


def management_journal_payload(journal: ManagementJournal) -> dict[str, Any]:
    payload = asdict(journal)
    payload["schema_version"] = JOURNAL_SCHEMA_VERSION
    for name in ("previous", "candidate"):
        payload[name] = execution_tuple_payload(getattr(journal, name))
    for name in ("action", "activation_mode", "stage"):
        payload[name] = getattr(journal, name).value
    return payload


def parse_management_journal_json(raw: str | bytes) -> ManagementJournal:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, item in pairs:
            if name in value:
                raise ManagementJournalError("management journal contains duplicate fields")
            value[name] = item
        return value

    def invalid_constant(value: str) -> Any:
        raise ManagementJournalError("management journal contains a nonstandard number")

    try:
        if isinstance(raw, str) and len(raw) > MAX_JOURNAL_BYTES:
            raise ManagementJournalError("management journal exceeds its input bound")
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(encoded, bytes) or len(encoded) > MAX_JOURNAL_BYTES:
            raise ManagementJournalError("management journal exceeds its input bound")
        payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=invalid_constant)
        return parse_management_journal(payload)
    except ManagementJournalError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ManagementJournalError("management journal JSON is invalid") from exc


def validate_journal_transition(previous: ManagementJournal, updated: ManagementJournal) -> None:
    """Validate ordinary progress; recovery/takeover need their own exact protocol."""
    before = management_journal_payload(previous)
    after = management_journal_payload(updated)
    parse_management_journal(before)
    parse_management_journal(after)
    if any(before[name] != after[name] for name in before.keys() - {"stage", "acceptance"}):
        raise ManagementJournalError("journal progress cannot change reviewed operation inputs")
    if previous.acceptance is not None and previous.acceptance != updated.acceptance:
        raise ManagementJournalError("journal acceptance is irreversible")
    if previous == updated:
        return
    legal = {
        ManagementStage.RESERVED: {ManagementStage.PREPARED, ManagementStage.RECOVERY_REQUIRED},
        ManagementStage.PREPARED: {
            ManagementStage.EXCLUSIVE, ManagementStage.SAVED, ManagementStage.OS_COMPLETED,
            ManagementStage.RECOVERY_REQUIRED,
        },
        ManagementStage.EXCLUSIVE: {ManagementStage.VERIFYING, ManagementStage.RECOVERY_REQUIRED},
        ManagementStage.VERIFYING: {
            ManagementStage.ACCEPTED, ManagementStage.ROLLED_BACK, ManagementStage.RECOVERY_REQUIRED,
        },
        ManagementStage.ACCEPTED: {ManagementStage.SUCCEEDED, ManagementStage.RECOVERY_REQUIRED},
    }
    if updated.stage not in legal.get(previous.stage, set()):
        raise ManagementJournalError("journal progress requires an explicit recovery protocol")


def validate_active_journal(journal: ManagementJournal, authority: state.LifecycleState) -> None:
    """Compare a snapshot only; callers still need live lock/capability/lease proof."""
    parse_management_journal(management_journal_payload(journal))
    operation = authority.active_operation
    if (
        authority.schema_version != state.MANAGEMENT_SCHEMA_VERSION
        or operation is None
        or operation.operation_type is not state.OperationType.MANAGEMENT
        or operation.owner_kind is not state.OwnerKind.MANAGEMENT_CONTROLLER
        or operation.operation_id != journal.operation_id
        or authority.generation != journal.generation
    ):
        raise ManagementJournalError("management journal does not match canonical authority")
    if operation.phase is not journal_phase(journal):
        raise ManagementJournalError("management journal stage does not match canonical phase")


def journal_phase(journal: ManagementJournal) -> state.LifecyclePhase:
    return {
        ManagementStage.RESERVED: state.LifecyclePhase.RESERVED,
        ManagementStage.PREPARED: state.LifecyclePhase.RESERVED,
        ManagementStage.SAVED: state.LifecyclePhase.RESERVED,
        ManagementStage.OS_COMPLETED: state.LifecyclePhase.RESERVED,
        ManagementStage.EXCLUSIVE: state.LifecyclePhase.EXCLUSIVE,
        ManagementStage.VERIFYING: state.LifecyclePhase.VERIFYING,
        ManagementStage.ACCEPTED: state.LifecyclePhase.VERIFYING,
        ManagementStage.SUCCEEDED: state.LifecyclePhase.VERIFYING,
        ManagementStage.ROLLED_BACK: state.LifecyclePhase.VERIFYING,
        ManagementStage.RECOVERY_REQUIRED: state.LifecyclePhase.RECOVERY_REQUIRED,
    }[journal.stage]
