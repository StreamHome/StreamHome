"""Explicit MANAGEMENT takeover with durable, retained generation bindings.

This is controller infrastructure, not a second lifecycle authority. A recovery
review is persisted before credential rotation. Missing journals, live competing
controllers and unexplained generation gaps never authorize reconstruction.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable

from . import management_journal as contract, management_store, state


MAX_INTENT_BYTES = 96 * 1024
ACTIONS = frozenset({"safe_abort", "continue_recovery", "rollback"})
MAINTENANCE_ACTION = 'repair_bound_environment'


class ManagementRecoveryError(ValueError):
    code = "management_recovery_unavailable"


def _sha(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def journal_sha256(journal: contract.ManagementJournal) -> str:
    payload = contract.management_journal_payload(journal)
    contract.parse_management_journal(payload)
    return _sha(payload)


@dataclass(frozen=True)
class TakeoverIntent:
    review_id: str
    source_generation: int
    source_phase: state.LifecyclePhase
    source_journal: contract.ManagementJournal
    parent_intent_sha256: str | None
    action: str
    target_journal: contract.ManagementJournal

    def payload(self) -> dict:
        return {"schema_version": 1, "review_id": self.review_id,
                "source_generation": self.source_generation, "source_phase": self.source_phase.value,
                "source_journal": contract.management_journal_payload(self.source_journal),
                "parent_intent_sha256": self.parent_intent_sha256, "action": self.action,
                "target_journal": contract.management_journal_payload(self.target_journal)}


def parse_takeover_intent(payload: dict) -> TakeoverIntent:
    fields = set(TakeoverIntent.__dataclass_fields__) | {"schema_version"}
    if (not isinstance(payload, dict) or set(payload) != fields
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1):
        raise ManagementRecoveryError("The recovery intent format is unsupported.")
    value = dict(payload)
    value.pop("schema_version")
    value["review_id"] = contract._uuid(value["review_id"], "recovery review")
    value["source_generation"] = contract._integer(value["source_generation"], 1, "recovery generation")
    for name in ("source_journal", "target_journal"):
        value[name] = contract.parse_management_journal(value[name])
    try:
        value["source_phase"] = state.LifecyclePhase(value["source_phase"])
    except (ValueError, TypeError) as exc:
        raise ManagementRecoveryError("The recovery source phase is invalid.") from exc
    if not isinstance(value["action"], str) or value["action"] not in ACTIONS | {MAINTENANCE_ACTION}:
        raise ManagementRecoveryError("The recovery action is unavailable.")
    parent = value["parent_intent_sha256"]
    if parent is not None:
        contract._text(parent, contract._SHA256, "parent takeover")
    result = TakeoverIntent(**value)
    previous, target = result.source_journal, result.target_journal
    if (previous.generation > result.source_generation or target.generation != result.source_generation + 1
            or (previous.generation < result.source_generation) != (parent is not None)
            or target.stage is not contract.ManagementStage.RECOVERY_REQUIRED
            or previous.stage is contract.ManagementStage.ROLLED_BACK and previous.recovery_target != 'previous'
            or previous.stage is contract.ManagementStage.SUCCEEDED and previous.acceptance is None
            or result.action in {"rollback", "safe_abort"} and previous.acceptance is not None):
        raise ManagementRecoveryError("The recovery intent does not bind a recoverable generation.")
    before, after = contract.management_journal_payload(previous), contract.management_journal_payload(target)
    if any(before[key] != after[key] for key in before.keys() - {"generation", "controller_invocation_id", "stage", "schema_version", "recovery_target"}):
        raise ManagementRecoveryError("Recovery cannot alter reviewed inputs or acceptance.")
    expected_target = 'previous' if result.action in {'rollback', 'safe_abort'} else previous.recovery_target
    if target.recovery_target != expected_target:
        raise ManagementRecoveryError('The recovery cannot switch its selected runtime without an unaccepted rollback review.')
    return result


def _paths(root: Path, operation_id: str) -> tuple[state.LifecyclePaths, Path]:
    paths, journal_path = management_store._journal_path(root, operation_id)
    management_store._validate_parents(paths, journal_path)
    return paths, journal_path.parent


def _read(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o7177 or before.st_nlink != 1 or before.st_size > MAX_INTENT_BYTES):
            raise ManagementRecoveryError("The recovery intent is not private trusted evidence.")
        raw = handle.read(MAX_INTENT_BYTES + 1)
        if (len(raw) > MAX_INTENT_BYTES or management_store._identity(before) != management_store._identity(os.fstat(handle.fileno()))
                or management_store._identity(before) != management_store._identity(path.lstat())):
            raise ManagementRecoveryError("The recovery intent changed during inspection.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ManagementRecoveryError("The recovery intent contains duplicate fields.")
            result[key] = value
        return result
    def invalid(_value):
        raise ManagementRecoveryError("The recovery intent contains a non-finite value.")
    return json.loads(raw.decode(), object_pairs_hook=pairs, parse_constant=invalid)


def inspect_takeover(root: Path, operation_id: str, generation: int) -> TakeoverIntent:
    contract._integer(generation, 2, "takeover generation")
    _, directory = _paths(root, operation_id)
    payload = _read(directory / f"takeover-{generation}.json")
    intent = parse_takeover_intent(payload)
    if intent.target_journal.operation_id != operation_id or intent.target_journal.generation != generation:
        raise ManagementRecoveryError("The recovery intent path names another generation.")
    retained = _read(directory / ("review-" + intent.review_id + ".json"))
    if retained != payload:
        raise ManagementRecoveryError("The recovery intent has no identical retained review.")
    return intent


def _effective_journal(root: Path, authority, journal):
    if journal.generation == authority.generation:
        return journal, None
    if not 0 < journal.generation < authority.generation:
        raise ManagementRecoveryError("The journal generation has no canonical recovery lineage.")
    parent = inspect_takeover(root, journal.operation_id, authority.generation)
    # A crash may leave either the pre-transfer journal or the exact rebound
    # journal. No third shape is normalized or guessed from the execution paths.
    if journal != parent.source_journal:
        raise ManagementRecoveryError("The interrupted takeover does not explain the journal.")
    ancestor = parent
    depth = 0
    while ancestor.parent_intent_sha256 is not None:
        depth += 1
        if depth > 128:
            raise ManagementRecoveryError("The interrupted takeover lineage exceeds its review bound.")
        grandparent = inspect_takeover(root, journal.operation_id, ancestor.source_generation)
        if _sha(grandparent.payload()) != ancestor.parent_intent_sha256 or grandparent.source_journal != journal:
            raise ManagementRecoveryError("The interrupted takeover lineage changed.")
        ancestor = grandparent
    return parent.target_journal, _sha(parent.payload())


def inspect_recovery(root: Path, operation_id: str, generation: int, *, maintenance: bool = False) -> dict:
    """Bounded read-only review; a returned plan grants no takeover authority."""
    authority = state.inspect_state(root)
    operation = authority.active_operation
    if (authority.schema_version != 2 or authority.generation != generation or operation is None
            or operation.operation_id != operation_id or operation.operation_type is not state.OperationType.MANAGEMENT
            or operation.owner_kind is not state.OwnerKind.MANAGEMENT_CONTROLLER):
        raise ManagementRecoveryError("Exact MANAGEMENT authority is unavailable.")
    journal = management_store.inspect_management_journal(root, operation_id)
    effective, parent_hash = _effective_journal(root, authority, journal)
    expected_phase = contract.journal_phase(journal)
    phase_gap = ((journal.stage is contract.ManagementStage.PREPARED and operation.phase is state.LifecyclePhase.EXCLUSIVE)
                 or (journal.stage is contract.ManagementStage.EXCLUSIVE and operation.phase is state.LifecyclePhase.VERIFYING))
    if journal.stage is contract.ManagementStage.RECOVERY_REQUIRED and operation.phase in {state.LifecyclePhase.EXCLUSIVE, state.LifecyclePhase.VERIFYING}:
        intent = inspect_takeover(root, operation_id, journal.generation)
        phase_gap = intent.action in ACTIONS | {MAINTENANCE_ACTION} and intent.target_journal == journal
    if operation.phase is not expected_phase and operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED and not phase_gap:
        raise ManagementRecoveryError("The journal does not explain the canonical phase.")
    if effective.stage is contract.ManagementStage.ROLLED_BACK and effective.recovery_target != 'previous':
        raise ManagementRecoveryError("Terminal acknowledgement requires its completion protocol.")
    actions = ["continue_recovery"]
    if maintenance:
        if effective.activation_mode is not contract.ActivationMode.CUTOVER:
            raise ManagementRecoveryError('Bound environment repair requires a cutover journal.')
        actions = [MAINTENANCE_ACTION]
    if not maintenance and effective.acceptance is None and effective.activation_mode is contract.ActivationMode.CUTOVER and effective.recovery_target == 'candidate':
        actions.append("rollback")
        if operation.phase is state.LifecyclePhase.RESERVED and journal.stage in {contract.ManagementStage.RESERVED, contract.ManagementStage.PREPARED}:
            actions.append("safe_abort")
    payload = {"operation_id": operation_id, "generation": generation, "phase": operation.phase.value,
               "journal_sha256": journal_sha256(journal), "parent_intent_sha256": parent_hash,
               "accepted": effective.acceptance is not None, "recovery_target": effective.recovery_target,
               "permitted_actions": sorted(actions)}
    return {**payload, "evidence_sha256": _sha(payload)}


def _persist_intent(root: Path, intent: TakeoverIntent) -> None:
    parse_takeover_intent(intent.payload())
    _, directory = _paths(root, intent.target_journal.operation_id)
    payload = intent.payload()
    if len(json.dumps(payload, sort_keys=True).encode()) > MAX_INTENT_BYTES:
        raise ManagementRecoveryError("The recovery intent exceeds its persistence bound.")
    retained = directory / ("review-" + intent.review_id + ".json")
    if retained.exists() or retained.is_symlink():
        if _read(retained) != payload:
            raise ManagementRecoveryError("The recovery review already binds different inputs.")
    else:
        state._atomic_write_json(retained, payload)
    # controller.lock and the still-old canonical generation prove that a prior
    # pointer here is an unexecuted review. Its unique review file is retained.
    pointer = directory / f"takeover-{intent.target_journal.generation}.json"
    if pointer.exists() or pointer.is_symlink():
        previous = inspect_takeover(root, intent.target_journal.operation_id, intent.target_journal.generation)
        if previous.source_generation != intent.source_generation:
            raise ManagementRecoveryError("An existing takeover pointer names another source generation.")
    state._atomic_write_json(pointer, payload)
    state._fsync_directory(directory)


def acquire_recovery_lease(
    root: Path, *, operation_id: str, generation: int, evidence_sha256: str,
    review_id: str, action: str, controller_invocation_id: str,
    pre_transfer_check: Callable[[], None] | None = None,
) -> tuple[state.ControllerLeaseHandle, state.OwnershipTransferResult, contract.ManagementJournal]:
    """Rotate ownership only after a fresh exact review, retaining crash gaps."""
    contract._uuid(review_id, "recovery review")
    contract._text(evidence_sha256, contract._SHA256, "reviewed recovery evidence")
    contract._text(controller_invocation_id, contract._INVOCATION, "recovery invocation")
    if int(controller_invocation_id, 16) == 0:
        raise ManagementRecoveryError("The recovery invocation is invalid.")
    plan = inspect_recovery(root, operation_id, generation, maintenance=action == MAINTENANCE_ACTION)
    if plan["evidence_sha256"] != evidence_sha256 or action not in plan["permitted_actions"]:
        raise ManagementRecoveryError("The reviewed recovery evidence changed.")
    intent = None
    def check():
        nonlocal intent
        fresh = inspect_recovery(root, operation_id, generation, maintenance=action == MAINTENANCE_ACTION)
        if fresh != plan:
            raise ManagementRecoveryError("Recovery evidence changed before ownership transfer.")
        if pre_transfer_check is not None:
            pre_transfer_check()
        authority = state.inspect_state(root)
        source = management_store.inspect_management_journal(root, operation_id)
        effective, parent = _effective_journal(root, authority, source)
        target = replace(effective, generation=generation + 1, controller_invocation_id=controller_invocation_id,
                         stage=contract.ManagementStage.RECOVERY_REQUIRED,
                         recovery_target='previous' if action in {'rollback', 'safe_abort'} else effective.recovery_target)
        intent = TakeoverIntent(review_id, generation, authority.active_operation.phase, source, parent, action, target)
        _persist_intent(root, intent)
    capability = state.load_controller_capability(root, operation_id=operation_id, generation=generation)
    lease, transfer = state.acquire_transferred_controller_lease(root, operation_id=operation_id, generation=generation,
        expected_phase=plan["phase"], owner_capability=capability, next_owner_kind=state.OwnerKind.MANAGEMENT_CONTROLLER,
        pre_transfer_check=check)
    try:
        assert intent is not None
        phase = transfer.state.active_operation.phase
        if phase is not state.LifecyclePhase.RECOVERY_REQUIRED:
            state.transition_operation_with_credential(root, operation_id=operation_id, generation=generation + 1,
                expected_phase=phase, next_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
        with state.hold_operation_with_credential(root, operation_id=operation_id, generation=generation + 1,
                expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED, expected_operation_type=state.OperationType.MANAGEMENT) as authority:
            lease.require_current()
            management_store._require_lease(lease.paths, lease, intent.target_journal, authority)
            if (inspect_takeover(root, operation_id, generation + 1) != intent
                    or management_store.inspect_management_journal(root, operation_id) != intent.source_journal):
                raise ManagementRecoveryError("The retained takeover inputs changed.")
            _, path = management_store._journal_path(root, operation_id)
            management_store._publish(path, intent.target_journal)
        return lease, transfer, intent.target_journal
    except BaseException:
        lease.release()
        raise


def require_takeover_source(root: Path, journal: contract.ManagementJournal) -> contract.ManagementJournal:
    """Prove exact rebound immutable inputs before reattaching old receipts."""
    intent = inspect_takeover(root, journal.operation_id, journal.generation)
    expected = replace(journal, stage=contract.ManagementStage.RECOVERY_REQUIRED)
    if expected != intent.target_journal:
        raise ManagementRecoveryError("The current journal does not match its retained takeover.")
    return intent.source_journal


def resume_candidate_verification(root: Path, journal: contract.ManagementJournal, *, lease) -> contract.ManagementJournal:
    """Reenter guarded verification after explicit recovery and child quiescence.

    Canonical phase progression and runtime reconstruction precede this CAS.
    Acceptance, when already durable, survives unchanged. No rollback, service
    start or authority release is implied by this publication.
    """
    if journal.stage is not contract.ManagementStage.RECOVERY_REQUIRED or journal.activation_mode is not contract.ActivationMode.CUTOVER:
        raise ManagementRecoveryError("Only cutover recovery can resume candidate verification.")
    intent = inspect_takeover(root, journal.operation_id, journal.generation)
    if intent.action not in ACTIONS or intent.target_journal != journal:
        raise ManagementRecoveryError("The recovery review does not authorize candidate verification.")
    updated = replace(journal, stage=contract.ManagementStage.ACCEPTED if journal.acceptance else contract.ManagementStage.VERIFYING)
    contract.parse_management_journal(contract.management_journal_payload(updated))
    with state.hold_operation_with_credential(root, operation_id=journal.operation_id, generation=journal.generation,
            expected_phase=state.LifecyclePhase.VERIFYING, expected_operation_type=state.OperationType.MANAGEMENT) as authority:
        management_store._require_lease(lease.paths, lease, updated, authority)
        _, path = management_store._journal_path(root, journal.operation_id)
        current = management_store.inspect_management_journal(root, journal.operation_id)
        if current == updated:
            management_store._acknowledge_existing(path)
        elif current != journal:
            raise ManagementRecoveryError("The recovery journal changed before verification.")
        else:
            management_store._publish(path, updated)
    return updated


def preserving_continuation_source(root: Path, journal: contract.ManagementJournal, generation: int) -> contract.ManagementJournal:
    """Prove an unbroken same-operation chain back to the live runtime's claim.

    A disk settlement receipt is insufficient: the caller must own the original
    process-local settled identity. No CUTOVER, execution change or skipped claim
    is admitted by this helper.
    """
    if (journal.activation_mode is contract.ActivationMode.CUTOVER or journal.acceptance is not None
            or journal.stage is not contract.ManagementStage.RECOVERY_REQUIRED):
        raise ManagementRecoveryError("Only preserving recovery can acknowledge a live runtime.")
    current = journal
    for _ in range(128):
        intent = inspect_takeover(root, current.operation_id, current.generation)
        if intent.action != 'continue_recovery' or intent.target_journal != replace(current, stage=contract.ManagementStage.RECOVERY_REQUIRED):
            raise ManagementRecoveryError("The preserving continuation changed its reviewed inputs.")
        current = intent.source_journal
        if current.generation == generation + 1:
            return current
        if current.generation <= generation + 1:
            break
    raise ManagementRecoveryError("The preserving continuation has no exact process-local lineage.")


def finish_preserving_recovery(root: Path, journal: contract.ManagementJournal, *, lease) -> contract.ManagementJournal:
    """Publish terminal evidence without inventing a reserved/exclusive cutover.

    The controller must first verify desired publication, its fresh OS receipt
    and the unchanged running/stopped outcome. Canonical authority remains in
    recovery_required until its separate credential-fenced completion. A crash
    in that gap is reviewed again, never inferred from the terminal file alone.
    """
    if journal.activation_mode is contract.ActivationMode.CUTOVER or journal.acceptance is not None:
        raise ManagementRecoveryError("Cutover work cannot use preserving completion.")
    intent = inspect_takeover(root, journal.operation_id, journal.generation)
    if intent.action != 'continue_recovery' or intent.target_journal != journal:
        raise ManagementRecoveryError("The review does not authorize preserving completion.")
    terminal = (contract.ManagementStage.SAVED if journal.activation_mode is contract.ActivationMode.SAVE_ONLY
                else contract.ManagementStage.OS_COMPLETED)
    updated = replace(journal, stage=terminal)
    contract.parse_management_journal(contract.management_journal_payload(updated))
    with state.hold_operation_with_credential(root, operation_id=journal.operation_id, generation=journal.generation,
            expected_phase=state.LifecyclePhase.RECOVERY_REQUIRED, expected_operation_type=state.OperationType.MANAGEMENT) as authority:
        # Terminal preserving journals normally map to RESERVED. Here the exact
        # retained takeover proves the deliberate RECOVERY_REQUIRED completion.
        management_store._require_lease(lease.paths, lease, journal, authority)
        _, path = management_store._journal_path(root, journal.operation_id)
        current = management_store.inspect_management_journal(root, journal.operation_id)
        if current == updated:
            management_store._acknowledge_existing(path)
        elif current != journal:
            raise ManagementRecoveryError("The preserving journal changed before completion.")
        else:
            management_store._publish(path, updated)
    return updated
