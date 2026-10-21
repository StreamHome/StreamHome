from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import management_journal as contract, management_store, management_recovery_store as recovery, state
from tests.lifecycle.test_management_journal import _journal_payload


class ManagementRecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'server').mkdir()
        for name in ('start.sh', 'update.sh'):
            (self.root / name).write_text('#!/bin/sh\n')
        self.paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(self.paths)
        state._write_state_unlocked(self.paths, state.LifecycleState(2, 0, None))
        payload = _journal_payload()
        self.lease, self.claim = state.claim_management_controller(self.root, operation_id=payload['operation_id'], expected_generation=0)
        payload['generation'] = self.claim.state.generation
        self.journal = contract.parse_management_journal(payload)
        management_store.create_management_journal(self.root, self.journal, lease=self.lease)
        self.addCleanup(lambda: self.lease.release())

    def take_over(self, action='continue_recovery', **kwargs):
        authority = state.inspect_state(self.root)
        plan = recovery.inspect_recovery(self.root, self.journal.operation_id, authority.generation)
        self.lease.release()
        lease, transfer, journal = recovery.acquire_recovery_lease(self.root,
            operation_id=self.journal.operation_id, generation=authority.generation,
            evidence_sha256=plan['evidence_sha256'], review_id=str(uuid.uuid4()), action=action,
            controller_invocation_id='2' * 32, **kwargs)
        self.lease = lease
        return transfer, journal

    def test_read_only_review_never_creates_intent_or_changes_authority(self):
        before = {path: path.read_bytes() for path in self.paths.lifecycle_dir.rglob('*') if path.is_file()}
        plan = recovery.inspect_recovery(self.root, self.journal.operation_id, 1)
        self.assertEqual(plan['permitted_actions'], ['continue_recovery', 'rollback', 'safe_abort'])
        self.assertFalse(plan['accepted'])
        self.assertEqual(before, {path: path.read_bytes() for path in self.paths.lifecycle_dir.rglob('*') if path.is_file()})
        self.assertEqual(state.inspect_state(self.root), self.claim.state)

    def test_takeover_retains_original_inputs_and_fences_old_capability(self):
        original = self.journal
        transfer, current = self.take_over()
        self.assertEqual(current.generation, 2)
        self.assertEqual(current.stage, contract.ManagementStage.RECOVERY_REQUIRED)
        self.assertNotEqual(transfer.owner_capability, self.claim.owner_capability)
        with self.assertRaises(state.LifecycleError):
            state.validate_operation_owner(self.root, operation_id=original.operation_id, generation=1,
                expected_phase='reserved', owner_capability=self.claim.owner_capability)
        intent = recovery.inspect_takeover(self.root, original.operation_id, 2)
        self.assertEqual(intent.source_journal, original)
        self.assertEqual(intent.target_journal, current)
        self.assertEqual(recovery.require_takeover_source(self.root, current), original)
        self.assertEqual(management_store.inspect_management_journal(self.root, original.operation_id), current)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        directory = self.paths.lifecycle_dir / 'management' / original.operation_id
        self.assertEqual((directory / ('review-' + intent.review_id + '.json')).stat().st_mode & 0o777, 0o600)

    def test_live_controller_and_stale_review_prevent_any_takeover_intent(self):
        plan = recovery.inspect_recovery(self.root, self.journal.operation_id, 1)
        for digest in ('f' * 64, plan['evidence_sha256']):
            with self.subTest(digest=digest), self.assertRaises((state.LifecycleError, recovery.ManagementRecoveryError)):
                recovery.acquire_recovery_lease(self.root, operation_id=self.journal.operation_id, generation=1,
                    evidence_sha256=digest, review_id=str(uuid.uuid4()), action='continue_recovery', controller_invocation_id='2' * 32)
        directory = self.paths.lifecycle_dir / 'management' / self.journal.operation_id
        self.assertEqual([path.name for path in directory.iterdir()], ['journal.json'])

    def test_stop_or_changed_process_proof_before_transfer_keeps_old_owner(self):
        def stopped():
            raise RuntimeError('operator stopped')
        with self.assertRaisesRegex(RuntimeError, 'operator stopped'):
            self.take_over(pre_transfer_check=stopped)
        self.assertEqual(state.inspect_state(self.root), self.claim.state)
        self.assertEqual(management_store.inspect_management_journal(self.root, self.journal.operation_id), self.journal)

    def test_accepted_work_cannot_offer_or_rebind_rollback(self):
        prepared = replace(self.journal, stage=contract.ManagementStage.PREPARED)
        management_store.advance_management_journal(self.root, self.journal, prepared, lease=self.lease)
        state.transition_operation_with_credential(self.root, operation_id=prepared.operation_id, generation=1, expected_phase='reserved', next_phase='exclusive')
        exclusive = replace(prepared, stage=contract.ManagementStage.EXCLUSIVE)
        management_store.advance_management_journal(self.root, prepared, exclusive, lease=self.lease)
        state.transition_operation_with_credential(self.root, operation_id=prepared.operation_id, generation=1, expected_phase='exclusive', next_phase='verifying')
        verifying = replace(exclusive, stage=contract.ManagementStage.VERIFYING)
        management_store.advance_management_journal(self.root, exclusive, verifying, lease=self.lease)
        acceptance = contract.AcceptanceEvidence(contract.execution_tuple_sha256(verifying.candidate), 'a' * 64)
        accepted = replace(verifying, stage=contract.ManagementStage.ACCEPTED, acceptance=acceptance)
        management_store.advance_management_journal(self.root, verifying, accepted, lease=self.lease)
        self.journal = accepted
        plan = recovery.inspect_recovery(self.root, accepted.operation_id, 1)
        self.assertEqual(plan['permitted_actions'], ['continue_recovery'])
        with self.assertRaises(recovery.ManagementRecoveryError):
            self.take_over(action='rollback')
        _, current = self.take_over()
        self.assertEqual(current.acceptance, acceptance)
        self.assertEqual(recovery.inspect_takeover(self.root, accepted.operation_id, 2).source_journal, accepted)

    def test_crashes_after_rotations_are_explained_without_reset_or_history_loss(self):
        original = self.journal
        # Two successive actors die after canonical transfer but before journal
        # publication, including the phase-to-recovery persistence gap.
        for expected in (2, 3):
            with patch.object(management_store, '_publish', side_effect=OSError('power-loss fixture')):
                with self.assertRaises(OSError):
                    self.take_over()
            self.assertEqual(state.inspect_state(self.root).generation, expected)
            self.assertEqual(management_store.inspect_management_journal(self.root, original.operation_id), original)
            self.assertEqual(recovery.inspect_takeover(self.root, original.operation_id, expected).source_journal, original)
        _, current = self.take_over()
        self.assertEqual(current.generation, 4)
        self.assertEqual(recovery.require_takeover_source(self.root, current), original)
        self.assertEqual(len(list((self.paths.lifecycle_dir / 'management' / original.operation_id).glob('review-*.json'))), 3)

    def test_failed_pretransfer_review_is_retained_when_replaced_by_a_new_review(self):
        original = state._transfer_operation_owner_with_credential
        with patch.object(state, '_transfer_operation_owner_with_credential', side_effect=OSError('before transfer')):
            with self.assertRaises(OSError):
                self.take_over()
        self.assertEqual(state.inspect_state(self.root).generation, 1)
        self.assertEqual(management_store.inspect_management_journal(self.root, self.journal.operation_id), self.journal)
        _, current = self.take_over()
        self.assertEqual(current.generation, 2)
        directory = self.paths.lifecycle_dir / 'management' / current.operation_id
        self.assertEqual(len(list(directory.glob('review-*.json'))), 2)

    def test_missing_or_tampered_gap_proof_never_reconstructs_from_runtime_paths(self):
        with patch.object(management_store, '_publish', side_effect=OSError('after transfer')):
            with self.assertRaises(OSError):
                self.take_over()
        path = self.paths.lifecycle_dir / 'management' / self.journal.operation_id / 'takeover-2.json'
        payload = json.loads(path.read_text())
        payload['target_journal']['acceptance'] = {'execution_tuple_sha256': contract.execution_tuple_sha256(self.journal.candidate), 'verification_sha256': 'a' * 64}
        path.write_text(json.dumps(payload))
        with self.assertRaises((ValueError, state.LifecycleError)):
            recovery.inspect_recovery(self.root, self.journal.operation_id, 2)
        self.assertEqual(state.inspect_state(self.root).generation, 2)

    def test_ordinary_progress_still_cannot_rebind_a_generation(self):
        with self.assertRaises(contract.ManagementJournalError):
            management_store.advance_management_journal(self.root, self.journal,
                replace(self.journal, generation=2, controller_invocation_id='2' * 32), lease=self.lease)

    def test_recovery_phase_gap_and_lost_verification_publish_reply_are_reconcilable(self):
        _, current = self.take_over()
        for before, after in (('recovery_required', 'exclusive'), ('exclusive', 'verifying')):
            state.transition_operation_with_credential(self.root, operation_id=current.operation_id, generation=2,
                expected_phase=before, next_phase=after)
            plan = recovery.inspect_recovery(self.root, current.operation_id, 2)
            self.assertIn('continue_recovery', plan['permitted_actions'])
        resumed = recovery.resume_candidate_verification(self.root, current, lease=self.lease)
        self.assertEqual(resumed.stage, contract.ManagementStage.VERIFYING)
        self.assertEqual(recovery.resume_candidate_verification(self.root, current, lease=self.lease), resumed)
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)


if __name__ == '__main__':
    unittest.main()
