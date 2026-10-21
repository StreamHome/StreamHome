from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import configuration_store as store, management_journal as contract, management_store, state
from lifecycle.deployment_config import default_deployment, patch_deployment
from lifecycle.pending_deployment import eligible_saved_deployment
from lifecycle.control.requests import parse_management_request
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_management_journal import _journal_payload


class ConfigurationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "server").mkdir()
        for name in ("start.sh", "update.sh"):
            (self.root / name).write_text("#!/bin/sh\n")
        self.paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(self.paths)
        state._write_state_unlocked(self.paths, state.LifecycleState(2, 0, None))
        self.directory = self.root / "configuration"
        self.directory.mkdir(mode=0o700)
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.identity = str(uuid.uuid4())
        self.store = store.ConfigurationStore(descriptor, self.identity)
        os.close(descriptor)
        self.initial = store.DeploymentRevision(self.identity, str(uuid.uuid4()), default_deployment())
        self.store.prepare_revision(self.initial)
        self.manifest = store.ConfigurationManifest(self.identity, self.initial.reference, self.initial.reference, (), (), ())
        self._fixture_manifest(self.manifest.payload())
        self.lease = None

    def tearDown(self) -> None:
        if self.lease is not None:
            self.lease.release()
        self.store.close()
        self.temporary.cleanup()

    def _fixture_manifest(self, payload) -> None:
        path = self.directory / "manifest.json"
        path.write_bytes(store._encode(payload))
        path.chmod(0o600)

    def _target(self, port: int = 4000):
        revision = store.DeploymentRevision(self.identity, str(uuid.uuid4()), patch_deployment(self.initial.settings, {"web_port": port}))
        self.store.prepare_revision(revision)
        return revision

    def _operation(self, target, mode="save_only"):
        payload = _journal_payload()
        payload.update(installation_id=self.identity, activation_mode=mode)
        payload["previous"]["deployment_revision"] = self.initial.revision
        payload["candidate"]["deployment_revision"] = target.revision
        manifest = self.store.inspect()
        for execution in (payload["previous"], payload["candidate"]):
            execution["policy_revisions"] = dict(manifest.policy_revisions)
            execution["provider_revisions"] = dict(manifest.provider_revisions)
        self.claim = state.claim_operation_with_credential(
            self.root, operation_type="management", owner_kind="management_controller", operation_id=payload["operation_id"],
        )
        payload["generation"] = self.claim.state.generation
        journal = contract.parse_management_journal(payload)
        self.lease = state.acquire_controller_lease(
            self.root, operation_id=journal.operation_id, generation=journal.generation,
            expected_phase="reserved", owner_capability=self.claim.owner_capability,
        )
        management_store.create_management_journal(self.root, journal, lease=self.lease)
        prepared = replace(journal, stage=contract.ManagementStage.PREPARED)
        management_store.advance_management_journal(self.root, journal, prepared, lease=self.lease)
        return prepared

    def _publish(self, journal, target, expected=None):
        return self.store.publish_desired(
            self.root, journal, lease=self.lease, expected=expected or self.initial.reference, target=target.reference,
        )

    def _phase(self, journal, phase, stage, *, acceptance=None):
        state.transition_operation_with_credential(
            self.root, operation_id=journal.operation_id, generation=journal.generation,
            expected_phase=contract.journal_phase(journal), next_phase=phase,
        )
        updated = replace(journal, stage=stage, acceptance=acceptance)
        management_store.advance_management_journal(self.root, journal, updated, lease=self.lease)
        return updated

    def test_missing_manifest_does_not_initialize_an_installation(self) -> None:
        (self.directory / "manifest.json").unlink()
        before = set(self.directory.iterdir())
        with self.assertRaises(store.ConfigurationStoreError):
            self.store.inspect()
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_inactive_revision_is_private_immutable_and_not_published(self) -> None:
        target = self._target()
        path = self.directory / f"deployment-{target.revision}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.read_revision(target.reference), target)
        self.assertEqual(self.store.inspect(), self.manifest)
        with self.assertRaises(store.ConfigurationConflictError):
            self.store.prepare_revision(replace(target, settings=default_deployment()))
        self.assertEqual(self.store.read_revision(target.reference), target)

    def test_save_only_changes_desired_and_never_active_or_domains(self) -> None:
        target = self._target()
        journal = self._operation(target)
        result = self._publish(journal, target)
        self.assertEqual(result.desired, target.reference)
        self.assertEqual(result.active, self.initial.reference)
        self.assertEqual(state.inspect_state(self.root), self.claim.state)
        self.assertEqual(management_store.inspect_management_journal(self.root, journal.operation_id), journal)
        with self.assertRaises(store.ConfigurationStoreError):
            self.store.promote_active(self.root, journal, lease=self.lease)

    def _saved_pending_review(self):
        from tests.lifecycle.test_management_requests import request_payload
        target = self._target()
        journal = self._operation(target)
        self._publish(journal, target)
        saved = replace(journal, stage=contract.ManagementStage.SAVED)
        management_store.advance_management_journal(self.root, journal, saved, lease=self.lease)
        state.complete_operation_with_credential(self.root, operation_id=saved.operation_id, generation=saved.generation, expected_phase='reserved')
        self.lease.release()
        self.lease = None
        payload = request_payload()
        payload.update(installation_id=self.identity, operation_id=saved.operation_id, review_id=saved.review_id,
            expected_generation=saved.generation - 1, activation_mode='save_only', interruption_approved=False,
            previous=contract.execution_tuple_payload(saved.previous), candidate=contract.execution_tuple_payload(saved.candidate),
            expected_desired=self.initial.reference.payload(), deployment=target.settings.payload())
        return saved, target, parse_management_request(payload)

    def test_pending_start_requires_completed_saved_receipt_and_protected_review(self):
        journal, target, reviewed = self._saved_pending_review()
        before = self.store.inspect()
        with patch('lifecycle.pending_deployment.read_management_request', return_value=reviewed):
            proof = eligible_saved_deployment(self.root, self.store, state.inspect_state(self.root), journal.previous)
        self.assertEqual(proof, {'operation_id': journal.operation_id, 'generation': journal.generation, 'request_sha256': reviewed.sha256})
        self.assertEqual(self.store.inspect(), before)
        self.assertEqual(before.active, self.initial.reference)
        self.assertEqual(before.desired, target.reference)

    def test_pending_start_rejects_completed_authority_without_terminal_saved_journal(self):
        target = self._target()
        journal = self._operation(target)
        self._publish(journal, target)
        state.complete_operation_with_credential(self.root, operation_id=journal.operation_id, generation=journal.generation, expected_phase='reserved')
        with self.assertRaises(ControlError):
            eligible_saved_deployment(self.root, self.store, state.inspect_state(self.root), journal.previous)

    def test_pending_start_rejects_missing_or_changed_root_review_and_quarantine(self):
        journal, target, reviewed = self._saved_pending_review()
        for replacement in (replace(reviewed, review_id=str(uuid.uuid4())), replace(reviewed, expected_generation=20)):
            with patch('lifecycle.pending_deployment.read_management_request', return_value=replacement), self.assertRaises(ControlError):
                eligible_saved_deployment(self.root, self.store, state.inspect_state(self.root), journal.previous)
        with patch('lifecycle.pending_deployment.read_management_request', side_effect=ControlError('missing')), self.assertRaises(ControlError):
            eligible_saved_deployment(self.root, self.store, state.inspect_state(self.root), journal.previous)
        self._fixture_manifest(replace(self.store.inspect(), quarantined=(target.revision,)).payload())
        with self.assertRaises(ControlError):
            eligible_saved_deployment(self.root, self.store, state.inspect_state(self.root), journal.previous)

    def test_stale_desired_cas_and_changed_journal_are_rejected(self) -> None:
        target = self._target()
        journal = self._operation(target)
        foreign = self._target(5000)
        self._fixture_manifest(replace(self.manifest, desired=foreign.reference).payload())
        before = (self.directory / "manifest.json").read_bytes()
        with self.assertRaises(store.ConfigurationConflictError):
            self._publish(journal, target)
        self.assertEqual((self.directory / "manifest.json").read_bytes(), before)
        with self.assertRaises(store.ConfigurationConflictError):
            self._publish(replace(journal, review_id=str(uuid.uuid4())), target, expected=foreign.reference)

    def test_exact_retry_reaffirms_durability_after_lost_publish_reply(self) -> None:
        target = self._target()
        journal = self._operation(target)
        original = os.fsync
        directory_syncs = 0

        def fail_reply(fd):
            nonlocal directory_syncs
            if fd == self.store.fd:
                directory_syncs += 1
                if directory_syncs == 2:
                    raise OSError("directory acknowledgement lost")
            return original(fd)

        with patch.object(store.os, "fsync", side_effect=fail_reply), self.assertRaises(store.ConfigurationStoreError):
            self._publish(journal, target)
        self.assertEqual(self.store.inspect().desired, target.reference)
        with patch.object(store.os, "fsync", wraps=original) as fsync:
            result = self._publish(journal, target)
            self.assertGreaterEqual(sum(call.args == (self.store.fd,) for call in fsync.call_args_list), 2)
        self.assertEqual(result.active, self.initial.reference)
        self.assertEqual(self.store.inspect_apply_receipt(journal.operation_id, journal.generation).stage, "saved")

    def test_missing_authority_released_lease_and_unbound_target_cannot_publish(self) -> None:
        target = self._target()
        journal = self._operation(target)
        with self.assertRaises(store.ConfigurationConflictError):
            self._publish(journal, self._target(5000))
        self.lease.release()
        with self.assertRaises(state.OwnershipError):
            self._publish(journal, target)
        self.assertEqual(self.store.inspect(), self.manifest)

    def test_policy_before_deployment_and_deployment_before_policy_preserve_domains(self) -> None:
        policy = self.store.publish_domain_reference(
            self.root, domain="policy", name="session", expected_revision=None, target_revision=1, expected_generation=0,
        )
        target = self._target()
        journal = self._operation(target)
        desired = self._publish(journal, target)
        self.assertEqual(desired.policy_revisions, policy.policy_revisions)
        self.lease.release()
        state.complete_operation_with_credential(
            self.root, operation_id=journal.operation_id, generation=journal.generation, expected_phase="reserved",
        )
        after = self.store.publish_domain_reference(
            self.root, domain="policy", name="session", expected_revision=1, target_revision=2,
            expected_generation=journal.generation,
        )
        self.assertEqual(after.desired, target.reference)
        self.assertEqual(after.active, self.initial.reference)
        self.assertEqual(after.policy_revisions, (("session", 2),))
        with self.assertRaises(store.ConfigurationConflictError):
            self.store.publish_domain_reference(
                self.root, domain="policy", name="session", expected_revision=1, target_revision=3,
                expected_generation=journal.generation,
            )

    def test_policy_cannot_race_an_active_management_claim_or_stale_generation(self) -> None:
        target = self._target()
        journal = self._operation(target)
        with self.assertRaises(state.LifecycleConflictError):
            self.store.publish_domain_reference(
                self.root, domain="policy", name="session", expected_revision=None,
                target_revision=1, expected_generation=journal.generation,
            )
        self.assertEqual(self.store.inspect(), self.manifest)

    def test_corrupt_or_foreign_revision_never_becomes_desired(self) -> None:
        target = self._target()
        journal = self._operation(target)
        path = self.directory / f"deployment-{target.revision}.json"
        payload = json.loads(path.read_text())
        payload["settings"]["web_port"] = 5000
        path.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):
            self._publish(journal, target)
        self.assertEqual(self.store.inspect(), self.manifest)
        with self.assertRaises(store.ConfigurationStoreError):
            self.store.prepare_revision(replace(target, installation_id=str(uuid.uuid4())))

    def test_symlinks_hardlinks_and_nonprivate_modes_are_rejected(self) -> None:
        path = self.directory / "manifest.json"
        path.chmod(0o644)
        with self.assertRaises(store.ConfigurationStoreError):
            self.store.inspect()
        path.chmod(0o600)
        other = self.directory / "other"
        os.link(path, other)
        with self.assertRaises(store.ConfigurationStoreError):
            self.store.inspect()
        other.unlink()
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(store.ConfigurationStoreError):
            self.store.inspect()

    def test_preacceptance_failure_quarantines_desired_without_reverting_database(self) -> None:
        database = self.root / "database.db"
        database.write_bytes(b"user state must remain unchanged")
        target = self._target()
        journal = self._operation(target, mode="cutover")
        self._publish(journal, target)
        failed = self._phase(journal, "recovery_required", contract.ManagementStage.RECOVERY_REQUIRED)
        result = self.store.quarantine_candidate(self.root, failed, lease=self.lease)
        self.assertEqual(result.active, self.initial.reference)
        self.assertEqual(result.desired, target.reference)
        self.assertEqual(result.quarantined, (target.revision,))
        self.assertEqual(database.read_bytes(), b"user state must remain unchanged")

    def test_only_durably_accepted_exact_candidate_can_become_active(self) -> None:
        target = self._target()
        journal = self._operation(target, mode="cutover")
        self._publish(journal, target)
        journal = self._phase(journal, "exclusive", contract.ManagementStage.EXCLUSIVE)
        journal = self._phase(journal, "verifying", contract.ManagementStage.VERIFYING)
        with self.assertRaises(store.ConfigurationStoreError):
            self.store.promote_active(self.root, journal, lease=self.lease)
        accepted = replace(journal, stage=contract.ManagementStage.ACCEPTED, acceptance=contract.AcceptanceEvidence(
            contract.execution_tuple_sha256(journal.candidate), "b" * 64,
        ))
        with self.assertRaises(store.ConfigurationConflictError):
            self.store.promote_active(self.root, accepted, lease=self.lease)
        management_store.advance_management_journal(self.root, journal, accepted, lease=self.lease)
        result = self.store.promote_active(self.root, accepted, lease=self.lease)
        self.assertEqual(result.active, target.reference)
        with self.assertRaises(store.ConfigurationStoreError):
            self.store.quarantine_candidate(self.root, accepted, lease=self.lease)

    def test_duplicate_and_unsupported_manifest_never_get_rewritten(self) -> None:
        path = self.directory / "manifest.json"
        original = path.read_text()
        for raw in (original.replace('"schema_version":1', '"schema_version":1,"schema_version":1'), original.replace('"schema_version":1', '"schema_version":2')):
            path.write_text(raw)
            with self.assertRaises(store.ConfigurationStoreError):
                self.store.inspect()
            self.assertEqual(path.read_text(), raw)

    def test_intent_is_durable_before_desired_and_retry_cannot_change_the_base(self) -> None:
        target = self._target()
        journal = self._operation(target)
        original = self.store._replace

        def interrupt(name, payload, **kwargs):
            if name == "manifest.json":
                raise store.ConfigurationStoreError("interrupted before manifest publication")
            return original(name, payload, **kwargs)

        with patch.object(self.store, "_replace", side_effect=interrupt), self.assertRaises(store.ConfigurationStoreError):
            self._publish(journal, target)
        receipt = self.store.inspect_apply_receipt(journal.operation_id, journal.generation)
        self.assertEqual(receipt.stage, "prepared")
        self.assertEqual(self.store.inspect(), self.manifest)
        with self.assertRaises(store.ConfigurationConflictError):
            self._publish(journal, target, expected=self._target(5000).reference)
        self._publish(journal, target)
        self.assertEqual(self.store.inspect_apply_receipt(journal.operation_id, journal.generation).stage, "saved")

    def test_accepted_manifest_reply_loss_reconciles_forward_without_rollback(self) -> None:
        target = self._target()
        journal = self._operation(target, mode="cutover")
        self._publish(journal, target)
        journal = self._phase(journal, "exclusive", contract.ManagementStage.EXCLUSIVE)
        journal = self._phase(journal, "verifying", contract.ManagementStage.VERIFYING)
        accepted = replace(journal, stage=contract.ManagementStage.ACCEPTED, acceptance=contract.AcceptanceEvidence(
            contract.execution_tuple_sha256(journal.candidate), "b" * 64,
        ))
        management_store.advance_management_journal(self.root, journal, accepted, lease=self.lease)
        with patch.object(self.store, "_write_receipt", side_effect=store.ConfigurationStoreError("receipt reply lost")), self.assertRaises(store.ConfigurationStoreError):
            self.store.promote_active(self.root, accepted, lease=self.lease)
        self.assertEqual(self.store.inspect().active, target.reference)
        self.assertEqual(self.store.inspect_apply_receipt(journal.operation_id, journal.generation).stage, "applying")
        self.store.promote_active(self.root, accepted, lease=self.lease)
        self.assertEqual(self.store.inspect_apply_receipt(journal.operation_id, journal.generation).stage, "accepted")

    def test_changed_policy_reference_requires_management_rereview(self) -> None:
        target = self._target()
        journal = self._operation(target)
        self._fixture_manifest(replace(self.manifest, policy_revisions=(("session", 1),)).payload())
        with self.assertRaises(store.ConfigurationConflictError):
            self._publish(journal, target)
        self.assertEqual(self.store.inspect().desired, self.initial.reference)


if __name__ == "__main__":
    unittest.main()
