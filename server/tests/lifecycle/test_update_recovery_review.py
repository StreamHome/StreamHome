"""Actual managed journal/active-pointer/SQLite review with OS liveness substituted."""
from dataclasses import replace
import json
import unittest
import uuid
from unittest.mock import patch

from lifecycle import execution_state, recovery, state, update_checkpoint, update_controller, update_recovery
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import execution_tuple_sha256
from tests.lifecycle import test_update_checkpoint as checkpoint_fixture


class UpdateRecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = checkpoint_fixture.ManagedUpdateCheckpointTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation
        fixture.create();fixture.accept(previous=True);fixture.lease.release()
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='verifying', next_phase='recovery_required')
        update_controller.reconcile_operator_journal_recovery_required(self.root, operation_id=self.operation,
            generation=2, current_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
        self.enterContext(patch.object(recovery, 'layout_for_root', return_value=fixture.layout))
        self.enterContext(patch.object(recovery, '_runtime_status', return_value='offline'))
        self.enterContext(patch.object(update_recovery, 'read_stop_epoch', return_value=fixture.parent.stop_epoch))
        self.digest = fixture.parent.execution.control_manifest_sha256

    def review(self):return update_recovery.review(self.root, self.operation, control_manifest_sha256=self.digest)

    def active(self):return execution_state.read_active_execution(self.root)

    def write_active(self, value):
        state._atomic_write_json(self.fixture.layout.state/'active-runtime.json', value.payload())

    def test_accepted_previous_review_is_read_only_and_never_offers_rollback_or_abort(self):
        before = self.fixture.path.read_bytes(), state.inspect_state(self.root), self.active()
        result = self.review()
        self.assertTrue(result['read_only'])
        self.assertEqual(result['permitted_actions'], ['continue_recovery'])
        self.assertEqual(result['accepted_execution_sha256'], execution_tuple_sha256(self.fixture.parent.execution))
        self.assertEqual(result['active_record_sha256'], update_recovery.digest(self.active().payload()))
        self.assertEqual((self.fixture.path.read_bytes(), state.inspect_state(self.root), self.active()), before)

    def test_foreign_or_future_same_tuple_pointer_cannot_approve_recovery(self):
        original = self.active()
        for changed in (replace(original, generation=3), replace(original, operation_id=str(uuid.uuid4()))):
            self.write_active(changed)
            with self.subTest(changed=changed), self.assertRaises(ControlError):self.review()
        self.write_active(original)

    def test_original_pointer_publication_gap_remains_explicit_exact_review_evidence(self):
        published = self.review()
        self.write_active(replace(self.active(), generation=0))
        gap = self.review()
        self.assertEqual(gap['permitted_actions'], ['continue_recovery'])
        self.assertNotEqual(published['evidence_sha256'], gap['evidence_sha256'])
        self.assertNotEqual(published['active_record_sha256'], gap['active_record_sha256'])

    def test_active_pointer_change_during_review_cannot_publish_a_stale_challenge(self):
        original = recovery.plan_recovery
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.write_active(replace(self.active(), generation=0))
            return result
        with patch.object(recovery, 'plan_recovery', side_effect=changed):
            with self.assertRaisesRegex(ControlError, 'control_recovery_review_stale'):self.review()

    def test_wrong_bound_reader_cannot_produce_a_review(self):
        before = self.fixture.path.read_bytes(), state.inspect_state(self.root)
        with self.assertRaises(ControlError):
            update_recovery.review(self.root, self.operation, control_manifest_sha256='f'*64)
        self.assertEqual((self.fixture.path.read_bytes(), state.inspect_state(self.root)), before)

    def test_unaccepted_previous_guarded_recovery_requires_its_original_checkpoint_and_exact_tuple(self):
        payload = update_controller.read_update_journal(self.fixture.path)
        payload.update(phase='recovering', runtime_committed=False, runtime_committed_at=None,
            managed_accepted_execution_sha256=None)
        self.fixture.path.write_text(json.dumps(payload))
        self.write_active(replace(self.active(), generation=0))
        result = self.review()
        self.assertEqual(result['permitted_actions'], ['continue_recovery'])
        self.assertEqual(result['execution_sha256'], execution_tuple_sha256(self.fixture.parent.execution))
        update_checkpoint.path_for(self.root, self.operation).unlink()
        self.assertEqual(self.review()['permitted_actions'], [])
