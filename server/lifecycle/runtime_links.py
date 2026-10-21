"""Journaled publication of the paired web dependency and asset links."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
from typing import Callable

from . import management_journal as contract, management_store, state
from .configuration_store import _decode
from .layout import layout_for_root
from .runtime_build import RuntimeBuildError, verify_runtime


def verify_links(root: Path, execution: contract.ExecutionTuple) -> None:
    for name, expected in (("node_modules", execution.web_dependency_target), ("dist", execution.web_dist_target)):
        path = root / "web" / name
        if not path.is_symlink() or os.readlink(path) != expected:
            raise RuntimeBuildError("The web dependency and asset links do not match the selected runtime.")


def _replace_links(root: Path, previous, candidate, *, operation_id: str, generation: int) -> None:
    layout = layout_for_root(root)
    receipt = layout.state / "runtime-links.json"
    intent = {
        "schema_version": 1, "operation_id": operation_id, "generation": generation,
        "previous": None if previous is None else contract.execution_tuple_sha256(previous),
        "candidate": contract.execution_tuple_sha256(candidate), "stage": "applying",
    }
    if receipt.exists() or receipt.is_symlink():
        if receipt.is_symlink() or receipt.stat().st_size > 8192:
            raise RuntimeBuildError("The web binding receipt is unsafe.")
        existing = _decode(receipt.read_bytes())
        if existing != intent and existing != {**intent, "stage": "complete"}:
            if not isinstance(existing, dict) or existing.get("stage") != "complete":
                raise RuntimeBuildError("A different web binding transaction requires recovery.")
    for name, target in (("node_modules", candidate.web_dependency_target), ("dist", candidate.web_dist_target)):
        path = root / "web" / name
        if path.exists() or path.is_symlink():
            old = None if previous is None else getattr(previous, "web_dependency_target" if name == "node_modules" else "web_dist_target")
            if not path.is_symlink() or os.readlink(path) not in {old, target}:
                raise RuntimeBuildError("An unrelated web directory or link cannot be replaced.")
        elif previous is not None:
            raise RuntimeBuildError("A previously active web binding is missing; repair is required.")
    state._atomic_write_json(receipt, intent)
    for name, target in (("node_modules", candidate.web_dependency_target), ("dist", candidate.web_dist_target)):
        temporary = root / "web" / (".binding-" + secrets.token_hex(16))
        try:
            temporary.symlink_to(target, target_is_directory=True)
            os.replace(temporary, root / "web" / name)
            state._fsync_directory(root / "web")
        finally:
            temporary.unlink(missing_ok=True)
    verify_links(root, candidate)
    state._atomic_write_json(receipt, {**intent, "stage": "complete"})


def bind_initial_runtime(root: Path, execution: contract.ExecutionTuple, *, installation_operation_id: str, assert_children_stopped: Callable[[], None]) -> None:
    from .installation import InstallationStatus
    layout = layout_for_root(root)
    if not layout.managed or layout.descriptor.descriptor.status is not InstallationStatus.PROVISIONING:
        raise RuntimeBuildError("Initial web bindings require the provisional installation.")
    contract._uuid(installation_operation_id, "installation operation")
    with state.hold_idle_authority(root) as authority:
        if authority.schema_version != 2 or authority.generation != 0:
            raise RuntimeBuildError("Initial web bindings cannot supersede existing authority.")
        assert_children_stopped()
        verify_runtime(root, execution)
        _replace_links(root, None, execution, operation_id=installation_operation_id, generation=0)


def bind_management_runtime(root: Path, journal: contract.ManagementJournal, *, lease: state.ControllerLeaseHandle, assert_children_stopped: Callable[[], None]) -> None:
    if journal.activation_mode is not contract.ActivationMode.CUTOVER or journal.stage not in {contract.ManagementStage.EXCLUSIVE, contract.ManagementStage.VERIFYING}:
        raise RuntimeBuildError("Web binding publication requires an exclusive unaccepted cutover.")
    with state.hold_operation_with_credential(root, operation_id=journal.operation_id, generation=journal.generation,
            expected_operation_type=state.OperationType.MANAGEMENT, expected_phase=state.LifecyclePhase.EXCLUSIVE if journal.stage is contract.ManagementStage.EXCLUSIVE else state.LifecyclePhase.VERIFYING) as authority:
        lease.require_current()
        contract.validate_active_journal(journal, authority)
        if lease.paths.root != root or lease.lease.operation_id != journal.operation_id or lease.lease.generation != journal.generation or management_store.inspect_management_journal(root, journal.operation_id) != journal:
            raise state.OwnershipError("Web binding publication requires the exact live management controller.")
        assert_children_stopped()
        verify_runtime(root, journal.candidate)
        _replace_links(root, journal.previous, journal.candidate, operation_id=journal.operation_id, generation=journal.generation)
