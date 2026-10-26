"""Actual retained records/canonical completion; OS scope and runtime build substituted."""
import json
import unittest
from unittest.mock import patch

from lifecycle import execution_state, managed_update, recovery, state, update_controller, update_history, update_recovery
from lifecycle.control import update_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_grants as grant_fixture


class UpdateRecoveryResultTests(unittest.TestCase):
    def setUp(self):
        fixture = grant_fixture.UpdateGrantTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.fixture.operation;self.canonical = fixture.fixture.fixture
        self.grant = fixture.prepare();self.digest = fixture.fixture.digest
        self.enterContext(patch.object(update_grants, 'read_grant', return_value=self.grant))
        self.enterContext(patch('lifecycle.layout.layout_for_root', return_value=self.canonical.layout))
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=2)
        with patch.object(managed_update.os, 'getpid', return_value=self.canonical.main):
            self.lease, _ = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation,
                generation=2, expected_phase='recovery_required', owner_capability=capability, next_owner_kind='updater')
        self.addCleanup(self.lease.release)
        update_controller._record_transferred_generation(self.root, operation_id=self.operation,
            previous_generation=2, generation=3, lifecycle_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
            target_commit=self.canonical.bound().candidate.application_commit,
            previous_commit=self.canonical.parent.execution.application_commit)
        for old, new in (('recovery_required', 'exclusive'), ('exclusive', 'verifying')):
            state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=3,
                expected_phase=old, next_phase=new)
        payload = update_controller.read_update_journal(self.canonical.path)
        payload.update(lifecycle_phase='verifying', phase='rolled_back', finished_at=2,
            failed_target=payload['target_commit'])
        self.canonical.path.write_text(json.dumps(payload))
        state._atomic_write_json(self.canonical.layout.state/'active-runtime.json', execution_state.ExecutionRecord(
            self.canonical.parent.installation_id, self.canonical.parent.execution, self.operation, 3).payload())
        with patch.object(managed_update.os, 'getpid', return_value=self.canonical.main):
            update_history.retain_terminal(self.root, self.lease)
        self.action = recovery._new_action_paths(self.root, self.operation, 2,
            action_id=self.grant['review_id']).record_path
        self.write_action('executing', None)

    def write_action(self, status, result):
        recovery._write_action_record(self.action, recovery.OperatorActionRecord(1, self.operation, 2,
            'update', 'continue_recovery', recovery._timestamp(), self.grant['canonical_evidence_identity'], status, 3, result))

    def finish(self, *, confirm=True):
        state.complete_operation_with_credential(self.root, operation_id=self.operation, generation=3,
            expected_phase='verifying', expected_operation_type='update')
        self.lease.release()
        if confirm:self.write_action('succeeded', 'operator_cleanup_pending')

    def result(self):return update_recovery.result(self.root, self.operation, 3, control_manifest_sha256=self.digest)

    def test_confirmed_result_survives_status_rewrite_and_later_different_operation(self):
        self.finish();self.canonical.path.write_text(json.dumps({'phase': 'checking'}))
        self.assertTrue(self.result()['completed'])
        state.claim_operation_with_credential(self.root, operation_type='management', owner_kind='management_controller')
        self.assertTrue(self.result()['completed'])

    def test_completion_before_action_receipt_is_a_read_only_reconciled_fact(self):
        self.finish(confirm=False)
        before = state.inspect_state(self.root), self.action.read_bytes(), self.canonical.database.read_bytes()
        result = self.result()
        self.assertTrue(result['completed']);self.assertTrue(result['completion_reconciled'])
        self.assertEqual((state.inspect_state(self.root), self.action.read_bytes(), self.canonical.database.read_bytes()), before)

    def test_retention_before_canonical_completion_cannot_report_success(self):
        self.assertFalse(self.result()['completed'])
        self.assertEqual(state.inspect_state(self.root).active_operation.operation_id, self.operation)

    def test_other_generation_content_cannot_be_attributed_by_its_filename(self):
        self.finish()
        path = update_history._path(self.root, self.operation, 3)
        journal = json.loads(path.read_text())['journal'];journal['lifecycle_generation'] = 2
        state._atomic_write_json(path, update_history._record(journal))
        with self.assertRaises(ControlError):self.result()
        self.assertFalse(recovery._verified_update_exit_twelve(self.root, self.operation, 3))
