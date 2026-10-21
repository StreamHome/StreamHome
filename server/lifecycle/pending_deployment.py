"""Read-only eligibility of a saved deployment at the next ordinary start."""
from __future__ import annotations

import os
import re

from . import management_journal as contract, management_store, state
from .configuration_store import ConfigurationStore, _journal_identity
from .control.requests import read_management_request
from .control.rpc import ControlError


_RECEIPT = re.compile(r"apply-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})-([1-9][0-9]{0,18})\.json")


def eligible_saved_deployment(root, store: ConfigurationStore, authority, execution) -> dict | None:
    manifest = store.inspect()
    if manifest.active == manifest.desired:
        return None
    if (authority.schema_version != 2 or authority.active_operation is not None
            or manifest.active.revision != execution.deployment_revision
            or manifest.desired.revision in manifest.quarantined
            or manifest.policy_revisions != execution.policy_revisions or manifest.provider_revisions != execution.provider_revisions):
        raise ControlError("control_pending_deployment_ineligible")
    selected = None
    with os.scandir(store.fd) as files:
        for count, item in enumerate(files, start=1):
            if count > 4096:
                raise ControlError("control_configuration_history_exceeds_bound")
            match = _RECEIPT.fullmatch(item.name)
            if match is None:
                continue
            operation, generation = match.group(1), int(match.group(2))
            if generation > authority.generation:
                continue
            receipt = store.inspect_apply_receipt(operation, generation)
            if receipt.stage != "saved" or receipt.target != manifest.desired or receipt.previous_active != manifest.active:
                continue
            journal = management_store.inspect_management_journal(root, operation)
            reviewed = read_management_request(operation)
            if (journal.generation != generation or journal.stage is not contract.ManagementStage.SAVED
                    or journal.installation_id != store.installation_id or journal.acceptance is not None
                    or journal.action is not contract.ManagementAction.CONFIGURE_APPLY
                    or journal.activation_mode is not contract.ActivationMode.SAVE_ONLY
                    or journal.candidate.deployment_revision != manifest.desired.revision
                    or receipt.journal_identity_sha256 != _journal_identity(journal)
                    or journal.review_id != reviewed.review_id or journal.previous != reviewed.previous or journal.candidate != reviewed.candidate
                    or journal.generation != reviewed.expected_generation + 1 or reviewed.expected_desired != receipt.expected_desired
                    or reviewed.deployment != store.read_revision(manifest.desired).settings):
                raise ControlError("control_pending_deployment_ineligible")
            proof = {"operation_id": operation, "generation": generation, "request_sha256": reviewed.sha256}
            if selected is None or selected["generation"] < generation:
                selected = proof
    if selected is None:
        raise ControlError("control_pending_deployment_ineligible")
    # Eligibility is data evidence only. The caller must still claim a fresh
    # MANAGEMENT generation and perform guarded verification before acceptance.
    if state.inspect_state(root) != authority or store.inspect() != manifest:
        raise ControlError("control_review_stale")
    return selected
