"""Accepted managed execution selection shared by startup and controllers."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any

from . import management_journal as contract, management_store, state
from .configuration_store import _decode, _encode
from .installation import InstallationStatus
from .layout import layout_for_root


class ExecutionStateError(ValueError):
    code = "execution_identity_unavailable"


@dataclass(frozen=True)
class ExecutionRecord:
    installation_id: str
    execution: contract.ExecutionTuple
    operation_id: str
    generation: int

    def payload(self) -> dict[str, Any]:
        return {"schema_version": 1, "installation_id": self.installation_id,
                "execution": contract.execution_tuple_payload(self.execution),
                "operation_id": self.operation_id, "generation": self.generation}


def parse_execution_record(payload: Any) -> ExecutionRecord:
    if (
        not isinstance(payload, dict) or set(payload) != {"schema_version", "installation_id", "execution", "operation_id", "generation"}
        or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
        or type(payload["generation"]) is not int or not 0 <= payload["generation"] < 2**63
    ):
        raise ExecutionStateError("The active execution record is invalid.")
    return ExecutionRecord(
        contract._uuid(payload["installation_id"], "execution installation"),
        contract.parse_execution_tuple(payload["execution"]),
        contract._uuid(payload["operation_id"], "execution operation"), payload["generation"],
    )


def read_active_execution(root: Path) -> ExecutionRecord:
    layout = layout_for_root(root)
    if not layout.managed:
        raise ExecutionStateError("Legacy runtime selection is not a managed execution record.")
    state._validate_directory(layout.state, "installation state directory")
    path = layout.state / "active-runtime.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_mode & 0o7177 or before.st_nlink != 1 or before.st_size > 32768:
                raise ExecutionStateError("The active execution record is not private trusted evidence.")
            raw = handle.read(32769)
            after, linked = os.fstat(handle.fileno()), path.lstat()
            identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_uid, item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if len(raw) > 32768 or identity(before) != identity(after) or identity(before) != identity(linked):
                raise ExecutionStateError("The active execution record changed during inspection.")
        record = parse_execution_record(_decode(raw))
        if record.installation_id != layout.descriptor.descriptor.installation_id:
            raise ExecutionStateError("The active execution record belongs to another installation.")
        return record
    except OSError as exc:
        raise ExecutionStateError("The active execution record is unavailable.") from exc


def publish_initial_execution(root: Path, record: ExecutionRecord) -> None:
    record = parse_execution_record(record.payload())
    layout = layout_for_root(root)
    if (
        not layout.managed or layout.descriptor.descriptor.status is not InstallationStatus.PROVISIONING
        or record.installation_id != layout.descriptor.descriptor.installation_id or record.generation != 0
    ):
        raise ExecutionStateError("Initial execution publication requires the exact provisional installation.")
    with state.hold_idle_authority(root) as authority:
        if authority.schema_version != 2 or authority.generation != 0:
            raise ExecutionStateError("Initial execution publication cannot supersede existing authority.")
        path = layout.state / "active-runtime.json"
        if path.exists() or path.is_symlink():
            if read_active_execution(root) != record:
                raise ExecutionStateError("Initial execution already binds different artifacts.")
            state._fsync_directory(layout.state)
            return
        state._atomic_write_json(path, record.payload())


def publish_accepted_execution(
    root: Path, journal: contract.ManagementJournal, *, lease: state.ControllerLeaseHandle,
) -> ExecutionRecord:
    if journal.stage is not contract.ManagementStage.ACCEPTED or journal.acceptance is None:
        raise ExecutionStateError("Active execution requires durable candidate acceptance.")
    layout = layout_for_root(root)
    with state.hold_operation_with_credential(
        root, operation_id=journal.operation_id, generation=journal.generation,
        expected_phase=state.LifecyclePhase.VERIFYING, expected_operation_type=state.OperationType.MANAGEMENT,
    ) as authority:
        lease.require_current()
        contract.validate_active_journal(journal, authority)
        if (
            lease.paths.root != root or lease.lease.operation_id != journal.operation_id
            or lease.lease.generation != journal.generation
            or management_store.inspect_management_journal(root, journal.operation_id) != journal
        ):
            raise state.OwnershipError("Active execution does not match the current controller journal.")
        current = read_active_execution(root)
        selected = contract.selected_execution(journal)
        updated = ExecutionRecord(journal.installation_id, selected, journal.operation_id, journal.generation)
        if current == updated:
            state._fsync_directory(layout.state)
            return current
        if current.execution == selected and current.operation_id == journal.operation_id and current.generation < journal.generation:
            from .management_recovery_store import require_takeover_source
            source = require_takeover_source(root, journal)
            if source.acceptance != journal.acceptance or current.generation > source.generation:
                raise ExecutionStateError("The prior accepted execution has no exact recovery lineage.")
            state._atomic_write_json(layout.state / "active-runtime.json", updated.payload())
            return updated
        if current.execution != journal.previous or current.installation_id != journal.installation_id:
            raise ExecutionStateError("Active execution changed after review.")
        state._atomic_write_json(layout.state / "active-runtime.json", updated.payload())
        return updated
