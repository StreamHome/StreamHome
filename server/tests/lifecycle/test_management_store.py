from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import management_journal as contract, management_store as store, state
from tests.lifecycle.test_management_journal import _journal_payload


class ManagementStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "server").mkdir()
        for name in ("start.sh", "update.sh"):
            (self.root / name).write_text("#!/bin/sh\n")
        self.paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(self.paths)
        state._write_state_unlocked(self.paths, state.LifecycleState(2, 0, None))
        payload = _journal_payload()
        self.claim = state.claim_operation_with_credential(
            self.root, operation_type="management", owner_kind="management_controller",
            operation_id=payload["operation_id"],
        )
        payload["generation"] = self.claim.state.generation
        self.journal = contract.parse_management_journal(payload)
        self.lease = state.acquire_controller_lease(
            self.root, operation_id=self.journal.operation_id, generation=self.journal.generation,
            expected_phase="reserved", owner_capability=self.claim.owner_capability,
        )
        self.path = self.paths.lifecycle_dir / "management" / self.journal.operation_id / "journal.json"

    def tearDown(self) -> None:
        self.lease.release()
        self.temporary.cleanup()

    def _create(self) -> contract.ManagementJournal:
        return store.create_management_journal(self.root, self.journal, lease=self.lease)

    def _advance(self, previous: contract.ManagementJournal, stage: contract.ManagementStage) -> contract.ManagementJournal:
        updated = replace(previous, stage=stage)
        return store.advance_management_journal(self.root, previous, updated, lease=self.lease)

    def test_missing_inspection_never_creates_directories_or_state(self) -> None:
        before = set(self.root.rglob("*"))
        with self.assertRaises(store.ManagementJournalMissingError):
            store.inspect_management_journal(self.root, self.journal.operation_id)
        self.assertEqual(set(self.root.rglob("*")), before)
        self.assertEqual(state.inspect_state(self.root), self.claim.state)

    def test_create_is_private_exact_and_subordinate_to_authority(self) -> None:
        self.assertEqual(self._create(), self.journal)
        self.assertEqual(store.inspect_management_journal(self.root, self.journal.operation_id), self.journal)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(state.inspect_state(self.root), self.claim.state)
        self.assertNotIn(self.claim.owner_capability, self.path.read_text())

    def test_exact_creation_retry_reaffirms_durability_without_replacing_evidence(self) -> None:
        self._create()
        identity = self.path.stat().st_ino
        with patch.object(state, "_fsync_directory", wraps=state._fsync_directory) as fsync:
            self._create()
            fsync.assert_called_once_with(self.path.parent)
        self.assertEqual(self.path.stat().st_ino, identity)

    def test_different_review_cannot_replace_existing_operation_inputs(self) -> None:
        self._create()
        before = self.path.read_bytes()
        with self.assertRaises(state.LifecycleConflictError):
            store.create_management_journal(
                self.root, replace(self.journal, review_id=str(uuid.uuid4())), lease=self.lease,
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_released_or_foreign_process_lease_never_creates_a_journal(self) -> None:
        with patch.object(state.os, "getpid", return_value=os.getpid() + 1):
            with self.assertRaises(state.OwnershipError):
                self._create()
        self.assertFalse(self.path.exists())
        self.lease.release()
        with self.assertRaises(state.OwnershipError):
            self._create()
        self.assertFalse(self.path.exists())

    def test_stale_generation_or_unknown_operation_cannot_create_evidence(self) -> None:
        for journal in (replace(self.journal, generation=2), replace(self.journal, operation_id=str(uuid.uuid4()))):
            with self.subTest(journal=journal), self.assertRaises(state.LifecycleError):
                store.create_management_journal(self.root, journal, lease=self.lease)
        self.assertFalse((self.paths.lifecycle_dir / "management").exists())

    def test_canonical_update_claim_cannot_be_used_to_write_management_evidence(self) -> None:
        self.lease.release()
        state.complete_operation_with_credential(
            self.root, operation_id=self.journal.operation_id, generation=1, expected_phase="reserved",
        )
        claimed = state.claim_operation_with_credential(self.root, operation_type="update", owner_kind="updater")
        journal = replace(self.journal, operation_id=claimed.state.active_operation.operation_id, generation=2)
        with state.acquire_controller_lease(
            self.root, operation_id=journal.operation_id, generation=2,
            expected_phase="reserved", owner_capability=claimed.owner_capability,
        ) as lease:
            with self.assertRaises(state.LifecycleConflictError):
                store.create_management_journal(self.root, journal, lease=lease)
        self.assertFalse((self.paths.lifecycle_dir / "management").exists())

    def test_progress_is_cas_and_does_not_transition_canonical_phase(self) -> None:
        self._create()
        prepared = self._advance(self.journal, contract.ManagementStage.PREPARED)
        self.assertEqual(state.inspect_state(self.root), self.claim.state)
        with self.assertRaises(state.LifecycleConflictError):
            self._advance(prepared, contract.ManagementStage.EXCLUSIVE)
        self.assertEqual(store.inspect_management_journal(self.root, self.journal.operation_id), prepared)
        state.transition_operation_with_credential(
            self.root, operation_id=self.journal.operation_id, generation=1,
            expected_phase="reserved", next_phase="exclusive",
        )
        exclusive = self._advance(prepared, contract.ManagementStage.EXCLUSIVE)
        self.assertEqual(store.inspect_management_journal(self.root, self.journal.operation_id), exclusive)

    def test_retry_of_exact_advanced_state_is_durable_and_idempotent(self) -> None:
        self._create()
        prepared = self._advance(self.journal, contract.ManagementStage.PREPARED)
        before = self.path.stat().st_ino
        with patch.object(state, "_fsync_directory", wraps=state._fsync_directory) as fsync:
            store.advance_management_journal(self.root, self.journal, prepared, lease=self.lease)
            fsync.assert_called_once_with(self.path.parent)
        self.assertEqual(self.path.stat().st_ino, before)

    def test_changed_disk_inputs_fail_cas_and_are_not_overwritten(self) -> None:
        self._create()
        tampered = replace(self.journal, review_id=str(uuid.uuid4()))
        state._atomic_write_json(self.path, contract.management_journal_payload(tampered))
        before = self.path.read_bytes()
        with self.assertRaises(state.LifecycleConflictError):
            self._advance(self.journal, contract.ManagementStage.PREPARED)
        self.assertEqual(self.path.read_bytes(), before)

    def test_old_owner_cannot_write_after_generation_and_capability_transfer(self) -> None:
        self._create()
        self.lease.release()
        new_lease, transfer = state.acquire_transferred_controller_lease(
            self.root, operation_id=self.journal.operation_id, generation=1,
            expected_phase="reserved", owner_capability=self.claim.owner_capability,
            next_owner_kind="management_controller",
        )
        before = self.path.read_bytes()
        try:
            self.assertEqual(transfer.state.generation, 2)
            with self.assertRaises(state.LifecycleError):
                self._advance(self.journal, contract.ManagementStage.PREPARED)
            with self.assertRaises(state.LifecycleError):
                store.advance_management_journal(
                    self.root, self.journal, replace(self.journal, stage=contract.ManagementStage.PREPARED), lease=new_lease,
                )
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            new_lease.release()

    def test_read_only_inspection_survives_terminal_authority_and_removed_credentials(self) -> None:
        self._create()
        self.lease.release()
        state.complete_operation_with_credential(
            self.root, operation_id=self.journal.operation_id, generation=1, expected_phase="reserved",
        )
        self.assertEqual(store.inspect_management_journal(self.root, self.journal.operation_id), self.journal)
        with self.assertRaises(state.LifecycleError):
            self._advance(self.journal, contract.ManagementStage.PREPARED)

    def test_symlink_fifo_hardlink_and_public_file_are_rejected_without_blocking(self) -> None:
        self._create()
        original = self.path.read_bytes()
        other = self.root / "foreign.json"
        other.write_bytes(original)
        other.chmod(0o600)
        for kind in ("symlink", "fifo", "hardlink", "public", "executable"):
            with self.subTest(kind=kind):
                self.path.unlink()
                if kind == "symlink":
                    self.path.symlink_to(other)
                elif kind == "fifo":
                    os.mkfifo(self.path, 0o600)
                elif kind == "hardlink":
                    os.link(other, self.path)
                else:
                    self.path.write_bytes(original)
                    self.path.chmod(0o644 if kind == "public" else 0o700)
                with self.assertRaises(contract.ManagementJournalError):
                    store.inspect_management_journal(self.root, self.journal.operation_id)
                with self.assertRaises(contract.ManagementJournalError):
                    self._create()
                self.assertEqual(other.read_bytes(), original)

    def test_foreign_or_public_parent_is_never_followed_or_repaired(self) -> None:
        self._create()
        self.path.parent.chmod(0o755)
        with self.assertRaises(state.CorruptStateError):
            store.inspect_management_journal(self.root, self.journal.operation_id)
        with self.assertRaises(state.CorruptStateError):
            self._create()
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)

    def test_post_replace_durability_failure_retains_claim_and_can_be_reconciled(self) -> None:
        original = state._fsync_directory

        def fail_after_replace(directory):
            if directory == self.path.parent and self.path.exists():
                raise OSError("injected journal directory durability failure")
            return original(directory)

        with patch.object(state, "_fsync_directory", side_effect=fail_after_replace):
            with self.assertRaises(state.PersistenceError):
                self._create()
        self.assertEqual(state.inspect_state(self.root), self.claim.state)
        self.assertEqual(store.inspect_management_journal(self.root, self.journal.operation_id), self.journal)
        self._create()

    def test_repeated_durability_failure_cannot_return_success(self) -> None:
        self._create()
        with patch.object(state, "_fsync_directory", side_effect=OSError("injected retry durability failure")):
            with self.assertRaises(state.PersistenceError):
                self._create()
        self.assertEqual(state.inspect_state(self.root), self.claim.state)

    def test_changed_entry_during_read_is_not_accepted(self) -> None:
        self._create()
        original = os.read
        replacement = self.path.with_name("replacement.json")
        replacement.write_bytes(self.path.read_bytes())
        replacement.chmod(0o600)
        changed = False

        def replace_after_read(descriptor, size):
            nonlocal changed
            result = original(descriptor, size)
            if not changed:
                changed = True
                os.replace(replacement, self.path)
            return result

        with patch.object(store.os, "read", side_effect=replace_after_read):
            with self.assertRaises(contract.ManagementJournalError):
                store.inspect_management_journal(self.root, self.journal.operation_id)

    def test_accepted_progress_retains_evidence_and_never_releases_authority(self) -> None:
        self._create()
        current = self._advance(self.journal, contract.ManagementStage.PREPARED)
        for before, after, stage in (
            ("reserved", "exclusive", contract.ManagementStage.EXCLUSIVE),
            ("exclusive", "verifying", contract.ManagementStage.VERIFYING),
        ):
            state.transition_operation_with_credential(
                self.root, operation_id=self.journal.operation_id, generation=1,
                expected_phase=before, next_phase=after,
            )
            current = self._advance(current, stage)
        accepted = replace(
            current, stage=contract.ManagementStage.ACCEPTED,
            acceptance=contract.AcceptanceEvidence(contract.execution_tuple_sha256(current.candidate), "1" * 64),
        )
        store.advance_management_journal(self.root, current, accepted, lease=self.lease)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.VERIFYING)
        state.transition_operation_with_credential(
            self.root, operation_id=self.journal.operation_id, generation=1,
            expected_phase="verifying", next_phase="recovery_required",
        )
        failed = replace(accepted, stage=contract.ManagementStage.RECOVERY_REQUIRED)
        store.advance_management_journal(self.root, accepted, failed, lease=self.lease)
        with self.assertRaises(contract.ManagementJournalError):
            store.advance_management_journal(self.root, failed, replace(failed, acceptance=None), lease=self.lease)
        self.assertEqual(store.inspect_management_journal(self.root, self.journal.operation_id), failed)
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)


if __name__ == "__main__":
    unittest.main()
