"""Process-local settled admission, with subordinate durable acknowledgements.

An old startup generation is never rewritten. A live runtime may acknowledge
one exact noninterrupting operation, then observe its exact terminal journal.
Disk receipts cannot admit another process or release canonical authority.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING

from . import management_journal as contract, management_store, state
from .layout import layout_for_root

if TYPE_CHECKING:
    from .bootstrap import StartupContext, StartupReadiness


class RuntimeAdmissionError(RuntimeError):
    code = "runtime_not_settled"


def _journal_identity(journal: contract.ManagementJournal) -> str:
    initial = replace(journal, stage=contract.ManagementStage.RESERVED, acceptance=None)
    raw = json.dumps(contract.management_journal_payload(initial), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class RuntimeAdmission:
    def __init__(
        self, context: StartupContext, installation_id: str, supervisor_invocation_id: str,
        execution: contract.ExecutionTuple,
    ) -> None:
        self.context = context
        self.installation_id = contract._uuid(installation_id, "runtime installation")
        self.invocation = contract._text(supervisor_invocation_id, contract._INVOCATION, "runtime invocation")
        if int(self.invocation, 16) == 0:
            raise RuntimeAdmissionError("The supervisor incarnation is invalid.")
        self.execution = contract.parse_execution_tuple(contract.execution_tuple_payload(execution))
        self.pid = os.getpid()
        self.start = state._process_start_identity(self.pid)
        self.generation = context.generation
        self._lock = threading.RLock()
        self._settled = False
        self._pending: contract.ManagementJournal | None = None

    def _same_process(self) -> bool:
        return os.getpid() == self.pid and state._process_start_identity(self.pid) == self.start

    def _receipt(self, *, generation: int, pending: contract.ManagementJournal | None) -> None:
        paths = state.lifecycle_paths(self.context.installation_root)
        directory = paths.lifecycle_dir / "settlement"
        state._ensure_private_directory(directory, "runtime settlement directory")
        state._atomic_write_json(directory / f"{self.pid}.{self.start}.json", {
            "schema_version": 1, "installation_id": self.installation_id,
            "supervisor_invocation_id": self.invocation, "child_pid": self.pid, "child_start_identity": self.start,
            "startup_generation": self.context.generation, "settled_generation": generation,
            "startup_operation_id": self.context.operation_id,
            "execution_tuple_sha256": contract.execution_tuple_sha256(self.execution),
            "pending_operation_id": pending.operation_id if pending else None,
            "pending_journal_sha256": _journal_identity(pending) if pending else None,
        })

    def _require_terminal_evidence(self) -> None:
        if self.context.operation_type == state.OperationType.UPDATE.value:
            from .update_controller import read_update_journal
            journal = read_update_journal(layout_for_root(self.context.installation_root).durable_runtime / "update-state.json")
            finished, committed = journal.get("finished_at"), journal.get("runtime_committed_at")
            if (
                journal.get("transaction_id") != self.context.operation_id
                or journal.get("lifecycle_generation") != self.context.generation
                or journal.get("phase") not in {"succeeded", "rolled_back"}
                or journal.get("runtime_committed") is not True
                or journal.get("current_commit") != self.execution.application_commit
                or type(finished) not in {int, float} or type(committed) not in {int, float}
                or not math.isfinite(finished) or not math.isfinite(committed) or not 0 < committed <= finished
            ):
                raise RuntimeAdmissionError("The exact accepted update has not published terminal evidence.")
        elif self.context.operation_type == state.OperationType.RESTORE.value:
            from .restore_controller import read_restore_journal
            journal = read_restore_journal(self.context.installation_root, self.context.operation_id)
            if (
                journal["generation"] != self.context.generation or not journal["accepted"]
                or journal["progress"] not in {"succeeded", "rolled_back"}
                or journal["outcome"] not in {"succeeded", "rolled_back"}
            ):
                raise RuntimeAdmissionError("The exact accepted restore has not published terminal evidence.")

    def settle(self, readiness: StartupReadiness) -> None:
        from .bootstrap import guarded_operation_completed
        projection = readiness.projection()
        with self._lock:
            if (
                readiness.context != self.context or not self._same_process()
                or not projection["generalReady"] or not projection["workersStarted"]
                or not projection["recoveryCompleted"] or not projection["recoverySucceeded"]
                or self.generation is None
            ):
                raise RuntimeAdmissionError("Mandatory recovery and worker acknowledgement are incomplete.")
            if self.context.guarded and not guarded_operation_completed(self.context):
                raise RuntimeAdmissionError("The guarded operation has not completed.")
            with state.hold_idle_authority(self.context.installation_root) as authority:
                if authority.generation != self.context.generation or authority.schema_version != state.MANAGEMENT_SCHEMA_VERSION:
                    raise RuntimeAdmissionError("Lifecycle authority changed before settlement.")
                self._require_terminal_evidence()
                if self.context.operation_type == state.OperationType.MANAGEMENT.value:
                    journal = management_store.inspect_management_journal(self.context.installation_root, self.context.operation_id)
                    if journal.candidate != self.execution or journal.installation_id != self.installation_id:
                        raise RuntimeAdmissionError("Terminal management evidence does not identify this execution tuple.")
                self._receipt(generation=authority.generation, pending=None)
                self._settled = True

    def acknowledge_continuation(self, operation_id: str, *, controller_pid: int, preparation: bool = False) -> dict:
        with self._lock:
            if not self._settled or not self._same_process() or self._pending is not None and self._pending.operation_id != operation_id:
                raise RuntimeAdmissionError("The live runtime is not available for a new continuation acknowledgement.")
            journal = management_store.inspect_management_journal(self.context.installation_root, operation_id)
            with state.hold_operation_with_credential(
                self.context.installation_root, operation_id=operation_id, generation=journal.generation,
                expected_phase=state.LifecyclePhase.RESERVED, expected_operation_type=state.OperationType.MANAGEMENT,
            ) as authority:
                contract.validate_active_journal(journal, authority)
                lease = state.read_controller_lease(self.context.installation_root)
                if (
                    journal.generation != self.generation + 1 or journal.installation_id != self.installation_id
                    or journal.previous != self.execution or journal.initial_service_state != "running"
                    or journal.supervisor_invocation_id != self.invocation
                    or journal.activation_mode not in ({contract.ActivationMode.CUTOVER} if preparation else {contract.ActivationMode.SAVE_ONLY, contract.ActivationMode.OS_ONLY})
                    or journal.stage not in {contract.ManagementStage.RESERVED, contract.ManagementStage.PREPARED}
                    or lease is None or not state.controller_lease_is_current(lease) or lease.pid != controller_pid
                    or lease.operation_id != operation_id or lease.generation != journal.generation
                    or lease.owner_kind is not state.OwnerKind.MANAGEMENT_CONTROLLER
                ):
                    raise RuntimeAdmissionError("The preserving operation does not match this admitted runtime.")
                self._receipt(generation=self.generation, pending=journal)
                self._pending = journal
                return {"acknowledged": True, "generation": journal.generation, "pid": self.pid,
                        "startIdentity": self.start, "executionTupleSha256": contract.execution_tuple_sha256(self.execution)}

    def admitted(self) -> bool:
        with self._lock:
            if not self._settled or not self._same_process():
                return False
            current = state.inspect_state(self.context.installation_root)
            operation = current.active_operation
            pending = self._pending
            if pending is None:
                return operation is None and current.generation == self.generation
            if current.generation != pending.generation:
                return False
            journal = management_store.inspect_management_journal(self.context.installation_root, pending.operation_id)
            if _journal_identity(journal) != _journal_identity(pending):
                return False
            terminal = (
                contract.ManagementStage.SAVED if pending.activation_mode is contract.ActivationMode.SAVE_ONLY
                else contract.ManagementStage.OS_COMPLETED if pending.activation_mode is contract.ActivationMode.OS_ONLY else None
            )
            if operation is not None:
                contract.validate_active_journal(journal, current)
                lease = state.read_controller_lease(self.context.installation_root)
                return (
                    journal.stage in {contract.ManagementStage.RESERVED, contract.ManagementStage.PREPARED, terminal}
                    and lease is not None and state.controller_lease_is_current(lease)
                    and lease.operation_id == pending.operation_id and lease.generation == pending.generation
                )
            if terminal is None or journal.stage is not terminal or journal.acceptance is not None:
                return False
            with state.hold_idle_authority(self.context.installation_root) as latest:
                if latest != current:
                    return False
                self._receipt(generation=current.generation, pending=None)
                self.generation, self._pending = current.generation, None
            return True


_runtime: RuntimeAdmission | None = None


def install_runtime_admission(admission: RuntimeAdmission) -> None:
    global _runtime
    if _runtime is not None:
        raise RuntimeAdmissionError("This process already has a settled runtime identity.")
    if not admission.admitted():
        raise RuntimeAdmissionError("The runtime has not acknowledged settlement.")
    _runtime = admission


def require_runtime_admission() -> RuntimeAdmission:
    if _runtime is None:
        raise RuntimeAdmissionError("The runtime has not acknowledged settlement.")
    return _runtime


def registered_runtime_allows_work() -> bool:
    if _runtime is None:
        return True  # Startup recovery is governed by its explicit effect fences.
    try:
        return _runtime.admitted()
    except (state.LifecycleError, ValueError, OSError):
        return False


def managed_runtime_admitted(context: StartupContext) -> bool | None:
    try:
        if not layout_for_root(context.installation_root).managed:
            return None
        if _runtime is None or _runtime.context != context:
            return False
        from .coordination import get_runtime_coordinator
        if get_runtime_coordinator().closed_effects():
            return False
        return _runtime.admitted()
    except (state.LifecycleError, ValueError, OSError):
        return False
