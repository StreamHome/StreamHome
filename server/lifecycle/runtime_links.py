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


def recover_management_links(
    root: Path, journal: contract.ManagementJournal, *, lease: state.ControllerLeaseHandle,
    target: str, assert_children_stopped: Callable[[], None],
) -> None:
    """Recover an exact old/candidate/mixed pair without rewriting its receipt.

    Recovery is limited to the original operation and its retained takeover
    generations. Accepted work can only reconstruct candidate links. This does
    not launch a runtime, accept it, roll back data or complete the operation.
    """
    from .management_recovery_store import inspect_takeover, require_takeover_source, _read, _sha
    if (journal.activation_mode is not contract.ActivationMode.CUTOVER
            or journal.stage is not contract.ManagementStage.RECOVERY_REQUIRED
            or target not in {"previous", "candidate"} or target == "previous" and journal.acceptance is not None):
        raise RuntimeBuildError("Web binding recovery does not permit the requested target.")
    with state.hold_operation_with_credential(root, operation_id=journal.operation_id, generation=journal.generation,
            expected_operation_type=state.OperationType.MANAGEMENT, expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED) as authority:
        management_store._require_lease(lease.paths, lease, journal, authority)
        if lease.paths.root != root or management_store.inspect_management_journal(root, journal.operation_id) != journal:
            raise state.OwnershipError("Web binding recovery requires its exact live journal.")
        intent = inspect_takeover(root, journal.operation_id, journal.generation)
        source = require_takeover_source(root, journal)
        if target == "previous" and intent.action not in {"rollback", "safe_abort"} or target == "candidate" and intent.action != "continue_recovery":
            raise RuntimeBuildError("The reviewed recovery selected another web binding target.")
        generations = {journal.generation, source.generation}
        for _ in range(128):
            if source.generation < 2:
                break
            try:
                ancestor = inspect_takeover(root, journal.operation_id, source.generation)
            except FileNotFoundError:
                break
            source = ancestor.source_journal
            if source.generation in generations:
                break
            generations.add(source.generation)
        else:
            raise RuntimeBuildError("The web binding lineage exceeds its recovery bound.")
        layout = layout_for_root(root)
        state._validate_directory(layout.state, "runtime binding state")
        original_path = layout.state / "runtime-links.json"
        original = _read(original_path)
        fields = {"schema_version", "operation_id", "generation", "previous", "candidate", "stage"}
        if (not isinstance(original, dict) or set(original) != fields or type(original["schema_version"]) is not int
                or original["schema_version"] != 1 or type(original["generation"]) is not int
                or not 0 <= original["generation"] < 2**63 or not isinstance(original["stage"], str)
                or original["stage"] not in {"applying", "complete"}):
            raise RuntimeBuildError("The original web binding receipt is invalid.")
        contract._uuid(original['operation_id'], 'binding operation')
        contract._text(original['candidate'], contract._SHA256, 'binding candidate')
        if original['previous'] is not None:
            contract._text(original['previous'], contract._SHA256, 'binding previous')
        previous_sha = contract.execution_tuple_sha256(journal.previous)
        candidate_sha = contract.execution_tuple_sha256(journal.candidate)
        own = (original["operation_id"] == journal.operation_id and original["generation"] in generations
               and original["previous"] == previous_sha and original["candidate"] == candidate_sha)
        old_complete = (original["stage"] == "complete" and original["candidate"] == previous_sha)
        if not own and not old_complete:
            raise RuntimeBuildError("The original binding receipt does not explain this operation.")
        def recovery_transaction(selected_intent, selected_target):
            return {"schema_version": 1, "operation_id": journal.operation_id, "generation": selected_intent.target_journal.generation,
                    "review_id": selected_intent.review_id, "previous": previous_sha, "candidate": candidate_sha,
                    "target": selected_target, "source_receipt_sha256": _sha(original), "stage": "applying"}
        receipt = layout.state / f"runtime-link-recovery-{journal.operation_id}-{journal.generation}.json"
        transaction = recovery_transaction(intent, target)
        recovery_started = False
        for generation in sorted(generations):
            earlier = layout.state / f"runtime-link-recovery-{journal.operation_id}-{generation}.json"
            if not earlier.exists() and not earlier.is_symlink():
                continue
            earlier_intent = inspect_takeover(root, journal.operation_id, generation)
            selected_target = "candidate" if earlier_intent.action == "continue_recovery" else "previous"
            expected = recovery_transaction(earlier_intent, selected_target)
            if _read(earlier) not in (expected, {**expected, "stage": "complete"}):
                raise RuntimeBuildError("The retained binding recovery names another transaction.")
            if generation == journal.generation and selected_target != target:
                raise RuntimeBuildError("The binding recovery selected another target.")
            recovery_started = True
        chosen = journal.previous if target == "previous" else journal.candidate
        assert_children_stopped()
        verify_runtime(root, chosen)
        for name, attribute in (("node_modules", "web_dependency_target"), ("dist", "web_dist_target")):
            path = root / "web" / name
            allowed = {getattr(journal.previous, attribute)}
            if own or recovery_started:
                allowed.add(getattr(journal.candidate, attribute))
            if not path.is_symlink() or os.readlink(path) not in allowed:
                raise RuntimeBuildError("Unrelated or unexplained web bindings cannot be recovered.")
        if receipt.exists() or receipt.is_symlink():
            if _read(receipt) not in (transaction, {**transaction, "stage": "complete"}):
                raise RuntimeBuildError("The binding recovery already names another transaction.")
        else:
            state._atomic_write_json(receipt, transaction)
        for name, destination in (("node_modules", chosen.web_dependency_target), ("dist", chosen.web_dist_target)):
            assert_children_stopped()
            temporary = root / "web" / (".recovery-binding-" + secrets.token_hex(16))
            try:
                temporary.symlink_to(destination, target_is_directory=True)
                os.replace(temporary, root / "web" / name)
                state._fsync_directory(root / "web")
            finally:
                temporary.unlink(missing_ok=True)
        verify_links(root, chosen)
        state._atomic_write_json(receipt, {**transaction, "stage": "complete"})
