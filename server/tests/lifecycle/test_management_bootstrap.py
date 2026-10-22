from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from lifecycle import bootstrap, management_journal as contract, management_store, management_recovery_store as recovery, state
import uuid
from tests.lifecycle.test_management_journal import _journal_payload


class ManagementBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'server').mkdir()
        for name in ('start.sh', 'update.sh'):
            (self.root/name).write_text('#!/bin/sh\n')
        paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(paths)
        state._write_state_unlocked(paths, state.LifecycleState(2, 0, None))
        payload = _journal_payload()
        claim = state.claim_operation_with_credential(self.root, operation_type='management', owner_kind='management_controller', operation_id=payload['operation_id'])
        payload['generation'] = claim.state.generation
        self.journal = contract.parse_management_journal(payload)
        self.lease = state.acquire_controller_lease(self.root, operation_id=self.journal.operation_id, generation=self.journal.generation, expected_phase='reserved', owner_capability=claim.owner_capability)
        self.addCleanup(self.lease.release)
        management_store.create_management_journal(self.root, self.journal, lease=self.lease)
        self.advance(contract.ManagementStage.PREPARED)
        self.advance(contract.ManagementStage.EXCLUSIVE, phase='exclusive')
        self.advance(contract.ManagementStage.VERIFYING, phase='verifying')
        self.environment = {
            bootstrap.STARTUP_MODE_ENV: 'management_verify',
            bootstrap.OPERATION_ID_ENV: self.journal.operation_id,
            bootstrap.GENERATION_ENV: str(self.journal.generation),
            'STREAMHOME_BUILD_ID': self.journal.candidate.application_build_id,
            'STREAMHOME_RUNTIME_TUPLE_SHA256': contract.execution_tuple_sha256(self.journal.candidate),
        }

    def advance(self, stage, phase=None, acceptance=None):
        if phase:
            state.transition_operation_with_credential(self.root, operation_id=self.journal.operation_id, generation=self.journal.generation, expected_phase=contract.journal_phase(self.journal), next_phase=phase)
        updated = replace(self.journal, stage=stage, acceptance=acceptance)
        management_store.advance_management_journal(self.root, self.journal, updated, lease=self.lease)
        self.journal = updated

    def test_exact_live_owner_and_candidate_authorize_only_guarded_local_verification(self):
        context = bootstrap.evaluate_startup(self.root, self.environment)
        self.assertIs(context.mode, bootstrap.StartupMode.MANAGEMENT_VERIFY)
        self.assertTrue(context.guarded)
        self.assertTrue(context.imports_application)
        self.assertEqual(context.allowed_effects, frozenset({'db_write'}))
        bootstrap.require_current_guarded_authority(context, self.environment)

    def test_missing_wrong_or_stale_tuple_and_generation_fail_before_application_import(self):
        for field, value in (('STREAMHOME_RUNTIME_TUPLE_SHA256', ''), ('STREAMHOME_BUILD_ID', 'wrong'), (bootstrap.GENERATION_ENV, '2'), (bootstrap.STARTUP_MODE_ENV, 'update_verify')):
            with self.subTest(field=field):
                context = bootstrap.evaluate_startup(self.root, {**self.environment, field: value})
                self.assertFalse(context.imports_application)
                self.assertEqual(context.allowed_effects, frozenset())

    def test_dead_or_released_controller_cannot_leave_a_guarded_launch_permission(self):
        self.lease.release()
        self.assertFalse(bootstrap.evaluate_startup(self.root, self.environment).imports_application)

    def test_canonical_completion_alone_is_not_management_terminal_evidence(self):
        context = bootstrap.evaluate_startup(self.root, self.environment)
        acceptance = contract.AcceptanceEvidence(contract.execution_tuple_sha256(self.journal.candidate), 'a'*64)
        self.advance(contract.ManagementStage.ACCEPTED, acceptance=acceptance)
        state.complete_operation_with_credential(self.root, operation_id=self.journal.operation_id, generation=self.journal.generation, expected_phase='verifying')
        self.assertFalse(bootstrap.guarded_operation_completed(context, self.environment))

    def test_exact_accepted_terminal_journal_and_idle_generation_prove_completion(self):
        context = bootstrap.evaluate_startup(self.root, self.environment)
        acceptance = contract.AcceptanceEvidence(contract.execution_tuple_sha256(self.journal.candidate), 'a'*64)
        self.advance(contract.ManagementStage.ACCEPTED, acceptance=acceptance)
        self.advance(contract.ManagementStage.SUCCEEDED, acceptance=acceptance)
        state.complete_operation_with_credential(self.root, operation_id=self.journal.operation_id, generation=self.journal.generation, expected_phase='verifying')
        self.assertTrue(bootstrap.guarded_operation_completed(context, self.environment))
        state.claim_operation_with_credential(self.root, operation_type='restore', owner_kind='restore_controller')
        self.assertFalse(bootstrap.guarded_operation_completed(context, self.environment))

    def test_guarded_previous_identity_acceptance_and_terminal_gap_preserve_the_same_context(self):
        plan = recovery.inspect_recovery(self.root, self.journal.operation_id, self.journal.generation)
        self.lease.release()
        self.lease, _, self.journal = recovery.acquire_recovery_lease(self.root,
            operation_id=self.journal.operation_id, generation=self.journal.generation,
            evidence_sha256=plan['evidence_sha256'], review_id=str(uuid.uuid4()), action='rollback',
            controller_invocation_id='1' * 32)
        self.addCleanup(self.lease.release)
        for before, after in (('recovery_required', 'exclusive'), ('exclusive', 'verifying')):
            state.transition_operation_with_credential(self.root, operation_id=self.journal.operation_id,
                generation=self.journal.generation, expected_phase=before, next_phase=after)
        self.journal = recovery.resume_candidate_verification(self.root, self.journal, lease=self.lease)
        self.environment.update({bootstrap.GENERATION_ENV: str(self.journal.generation),
            'STREAMHOME_RUNTIME_TUPLE_SHA256': contract.execution_tuple_sha256(self.journal.previous)})
        context = bootstrap.evaluate_startup(self.root, self.environment)
        self.assertTrue(context.imports_application)
        self.assertFalse(bootstrap.evaluate_startup(self.root, {**self.environment,
            'STREAMHOME_RUNTIME_TUPLE_SHA256': contract.execution_tuple_sha256(self.journal.candidate)}).imports_application)
        acceptance = contract.AcceptanceEvidence(contract.execution_tuple_sha256(self.journal.previous), 'a' * 64)
        self.advance(contract.ManagementStage.ACCEPTED, acceptance=acceptance)
        bootstrap.require_current_guarded_authority(context, self.environment)
        self.advance(contract.ManagementStage.ROLLED_BACK, acceptance=acceptance)
        bootstrap.require_current_guarded_authority(context, self.environment)
        self.assertFalse(bootstrap.guarded_operation_completed(context, self.environment))
        state.complete_operation_with_credential(self.root, operation_id=self.journal.operation_id,
            generation=self.journal.generation, expected_phase='verifying')
        self.assertTrue(bootstrap.guarded_operation_completed(context, self.environment))


if __name__ == '__main__':
    unittest.main()
