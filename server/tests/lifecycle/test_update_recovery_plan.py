"""Canonical/Git recovery planning selects accepted managed tuples, including previous."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import recovery, state, update_controller, update_execution
from lifecycle.layout import layout_for_root
from lifecycle.management_journal import execution_tuple_sha256
from tests.lifecycle import test_operator_recovery as recovery_fixture
from tests.lifecycle.test_update_execution import journal


class ManagedUpdateRecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        fixture = recovery_fixture.UpdateRecoveryPlannerTests();fixture.setUp()
        self.addCleanup(fixture.tearDown);self.fixture = fixture;self.root = fixture.root
        paths = state.lifecycle_paths(self.root);state._ensure_lifecycle_directories(paths)
        state._write_state_unlocked(paths, state.LifecycleState(2, 0, None))
        operation, generation, self.path = fixture._claim_update(committed=True)
        capability = state.load_controller_capability(self.root, operation_id=operation, generation=generation)
        lease, transferred = state.acquire_transferred_controller_lease(self.root, operation_id=operation,
            generation=generation, expected_phase='recovery_required', owner_capability=capability, next_owner_kind='updater')
        update_controller._record_transferred_generation(self.root, operation_id=operation,
            previous_generation=generation, generation=transferred.state.generation,
            lifecycle_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
            target_commit=fixture.target, previous_commit=fixture.previous)
        lease.release();self.operation = operation
        self.payload = journal(previous=fixture.previous, target=fixture.target)
        self.payload.update(transaction_id=operation, lifecycle_generation=2,
            lifecycle_phase='recovery_required', phase='recovering', runtime_committed=True,
            runtime_committed_at=1)
        self.payload['managed_execution']['operation_id'] = operation
        layout = layout_for_root(self.root)
        self.layout = replace(layout, descriptor=SimpleNamespace(descriptor=SimpleNamespace()))
        self.enterContext(patch.object(recovery, 'layout_for_root', return_value=self.layout))
        self.enterContext(patch.object(recovery, '_runtime_status', return_value='offline'))

    def select(self, previous=False):
        selected = update_execution.parse({**self.payload, 'runtime_committed': False,
            'managed_accepted_execution_sha256': None, 'managed_verification_execution_sha256': None})
        selected = selected.parent.execution if previous else selected.candidate
        self.payload.update(current_commit=selected.application_commit,
            managed_verification_execution_sha256=execution_tuple_sha256(selected),
            managed_accepted_execution_sha256=execution_tuple_sha256(selected))
        self.path.write_text(json.dumps(self.payload));self.path.chmod(0o600)
        self.fixture._git('checkout', '--detach', selected.application_commit)

    def plan(self):return recovery.plan_recovery(self.root, operation_id=self.operation, generation=2)

    def test_accepted_previous_execution_can_continue_and_never_offers_rollback_or_abort(self):
        self.select(previous=True);before = self.path.read_bytes(), state.inspect_state(self.root)
        plan = self.plan()
        self.assertEqual(plan['permittedActions'], ['continue_recovery'])
        self.assertEqual(plan['artifacts']['acceptedExecutionBinding'], 'valid')
        self.assertEqual(before, (self.path.read_bytes(), state.inspect_state(self.root)))

    def test_accepted_candidate_requires_its_actual_installed_commit(self):
        self.select();self.assertEqual(self.plan()['permittedActions'], ['continue_recovery'])
        self.fixture._git('checkout', '--detach', self.fixture.previous)
        self.assertEqual(self.plan()['permittedActions'], [])

    def test_same_commit_acceptance_still_changes_exact_recovery_evidence(self):
        self.payload['target_commit'] = self.fixture.previous
        self.payload['managed_candidate'].update(application_commit=self.fixture.previous,
            application_build_id=self.fixture.previous[:12])
        self.select(previous=True);old = self.plan()
        self.select();candidate = self.plan()
        self.assertEqual(old['permittedActions'], candidate['permittedActions'])
        self.assertNotEqual(old['evidenceIdentity'], candidate['evidenceIdentity'])

    def test_legacy_journal_does_not_infer_the_managed_previous_selection_contract(self):
        self.select(previous=True)
        legacy = {key: value for key, value in self.payload.items() if key not in update_execution.FIELDS}
        self.path.write_text(json.dumps(legacy))
        self.assertEqual(self.plan()['permittedActions'], [])

    def test_failed_managed_rollback_requires_valid_bound_checkpoint_and_installed_finite_source(self):
        for previous in (False, True):
            self.select(previous=previous)
            self.payload.update(runtime_committed=False, runtime_committed_at=None,
                managed_accepted_execution_sha256=None, phase='rollback_failed',
                failed_target=self.fixture.target)
            self.path.write_text(json.dumps(self.payload))
            for status in ('valid', 'invalid', 'absent'):
                with self.subTest(previous=previous, status=status), patch.object(
                        recovery, '_update_checkpoint_status', return_value=status):
                    self.assertEqual(self.plan()['permittedActions'],
                        ['continue_recovery'] if status == 'valid' else [])
            self.payload['managed_verification_execution_sha256'] = None
            self.path.write_text(json.dumps(self.payload))
            with patch.object(recovery, '_update_checkpoint_status', return_value='valid'):
                self.assertEqual(self.plan()['permittedActions'], ['continue_recovery'])
