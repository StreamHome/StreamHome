"""Original managed reservation abort review; actual claim with OS/Git read boundaries substituted."""
from dataclasses import replace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import recovery, state, update_checkpoint, update_controller, update_recovery
from lifecycle.execution_state import ExecutionRecord
from tests.lifecycle import test_update_continuation as reservation_fixture


class UpdateAbortReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = reservation_fixture.UpdateContinuationTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation;self.parent = fixture.parent
        self.layout = fixture.layout
        self.enterContext(patch.object(recovery, 'layout_for_root', return_value=self.layout))
        self.runtime = self.enterContext(patch.object(recovery, '_runtime_status', return_value='offline'))
        self.enterContext(patch.object(recovery, '_installed_commit', return_value=self.parent.execution.application_commit))
        self.enterContext(patch.object(recovery, '_git_commit_exists', return_value=True))
        self.enterContext(patch.object(update_recovery, 'read_stop_epoch', return_value='f'*64))
        self.enterContext(patch.object(update_recovery, 'read_active_execution', return_value=ExecutionRecord(
            self.parent.installation_id, self.parent.execution, str(uuid.uuid4()), 0)))

    def view(self):return update_recovery.review(self.root, self.operation,
        control_manifest_sha256=self.parent.execution.control_manifest_sha256)

    def test_stopped_original_reservation_offers_only_read_only_abort_without_claim_or_normalization(self):
        before = state.inspect_state(self.root), self.fixture.path.read_bytes()
        view = self.view()
        self.assertEqual(view['generation'], 1)
        self.assertEqual(view['canonical_plan']['lifecyclePhase'], 'reserved')
        self.assertEqual(view['permitted_actions'], ['safe_abort'])
        self.assertEqual(view['stop_epoch'], 'f'*64)
        self.assertEqual((state.inspect_state(self.root), self.fixture.path.read_bytes()), before)

    def test_living_owner_or_unproven_runtime_cannot_offer_abort(self):
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=1)
        lease = state.acquire_controller_lease(self.root, operation_id=self.operation, generation=1,
            expected_phase='reserved', owner_capability=capability)
        self.addCleanup(lease.release)
        self.assertEqual(self.view()['permitted_actions'], [])
        lease.release();self.runtime.return_value = 'unknown'
        self.assertEqual(self.view()['permitted_actions'], [])

    def test_unknown_operation_snapshot_is_preserved_and_blocks_abort(self):
        snapshot = update_checkpoint.path_for(self.root, self.operation, create=True)
        snapshot.write_bytes(b'Unattributed retained evidence');snapshot.chmod(0o600)
        self.assertEqual(self.view()['permitted_actions'], [])
        self.assertEqual(snapshot.read_bytes(), b'Unattributed retained evidence')

    def test_legacy_reserved_recovery_contract_does_not_borrow_managed_abort_review(self):
        with patch.object(recovery, 'layout_for_root', return_value=replace(self.layout, descriptor=None)):
            with self.assertRaises(recovery.RecoveryPlanningConflict):self.view()
