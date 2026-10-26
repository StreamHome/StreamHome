"""Actual one-generation canonical gap and action intent; protected metadata read substituted."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from lifecycle import execution_state, managed_update, recovery, state, update_controller
from lifecycle import update_recovery_observation as observation
from lifecycle.update_recovery import review as canonical_review
from lifecycle.control import update_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_grants as grant_fixture


class UpdateTransferGapTests(unittest.TestCase):
    def setUp(self):
        fixture = grant_fixture.UpdateGrantTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.fixture.operation
        self.canonical = fixture.fixture.fixture
        self.original_permit = update_controller.create_cutover_permit(self.root, self.operation, 2)
        # Qualify a consistent canonical source plan. Only OS runtime observation
        # is substituted; the prior evidence SHA is reconstructed by production code.
        with patch.object(recovery, '_runtime_status', return_value='active'):
            fixture.snapshot.update(canonical_review(self.root, self.operation,
                control_manifest_sha256=fixture.fixture.digest))
        fixture.payload['expected_evidence_sha256'] = fixture.snapshot['evidence_sha256']
        self.grant = fixture.prepare()
        self.enterContext(patch.object(observation, 'layout_for_root', return_value=self.canonical.layout))
        self.reader = self.enterContext(patch.object(update_grants, 'read_grant', return_value=self.grant))
        paths = recovery._new_action_paths(self.root, self.operation, 2, action_id=self.grant['review_id'])
        self.action_path = paths.record_path
        record = recovery.OperatorActionRecord(1, self.operation, 2, 'update', 'continue_recovery',
            recovery._timestamp(), self.grant['canonical_evidence_identity'], 'planned', None, None)
        recovery._write_action_record(paths.record_path, record)

    def transfer(self, generation=2):
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=generation)
        with patch.object(managed_update.os, 'getpid', return_value=self.canonical.main):
            lease, _ = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation,
                generation=generation, expected_phase='recovery_required', owner_capability=capability,
                next_owner_kind='updater')
        lease.release()

    def test_exact_existing_action_and_historical_grant_explain_only_one_transfer_without_mutation(self):
        self.transfer();before = state.inspect_state(self.root), self.canonical.path.read_bytes()
        authority, journal, bound, witness = observation.read_binding(self.root, self.operation)
        self.assertEqual(authority.generation, 3);self.assertEqual(journal['lifecycle_generation'], 2)
        self.assertEqual(witness['source_generation'], 2);self.assertEqual(witness['controller_generation'], 3)
        self.assertEqual(bound.accepted, self.canonical.parent.execution)
        self.assertEqual((state.inspect_state(self.root), self.canonical.path.read_bytes()), before)
        self.reader.assert_any_call(self.operation, 3, live=False)

    def test_expired_permission_is_history_only_and_cannot_authorize_current_work(self):
        self.transfer();self.grant['expires_at_monotonic'] = .001
        self.assertIsNotNone(observation.read_binding(self.root, self.operation)[3])
        with self.assertRaises(ControlError):
            update_grants.require_binding(self.grant, self.canonical.bound(), self.fixture.active, self.fixture.journal)

    def test_missing_or_foreign_action_intent_cannot_explain_a_gap(self):
        self.transfer();original = self.action_path.read_bytes()
        self.action_path.unlink()
        with self.assertRaises(ControlError):observation.read_binding(self.root, self.operation)
        value = json.loads(original);value['evidence_identity'] = 'plan-v1:'+'f'*32
        state._atomic_write_json(self.action_path, value)
        with self.assertRaises(ControlError):observation.read_binding(self.root, self.operation)

    def test_two_transfers_or_changed_request_never_become_a_single_witnessed_gap(self):
        self.transfer()
        raw = update_controller.read_update_journal(self.canonical.path)
        self.canonical.path.write_text(json.dumps({**raw, 'automatic': True}))
        with self.assertRaises(ControlError):observation.read_binding(self.root, self.operation)
        self.canonical.path.write_text(json.dumps(raw));self.transfer(3)
        with self.assertRaises(ControlError):observation.read_binding(self.root, self.operation)

    def test_published_new_generation_pointer_is_not_a_transfer_before_journal_gap(self):
        self.transfer()
        active = execution_state.read_active_execution(self.root)
        self.fixture.fixture.write_active(replace(active, generation=3))
        with self.assertRaises(ControlError):observation.read_binding(self.root, self.operation)

    def test_same_tuple_with_changed_acceptance_timestamp_does_not_match_original_plan(self):
        self.transfer()
        raw = update_controller.read_update_journal(self.canonical.path)
        self.canonical.path.write_text(json.dumps({**raw, 'runtime_committed_at': 1.5}))
        with self.assertRaises(ControlError):observation.read_binding(self.root, self.operation)

    def test_earlier_valid_attempt_does_not_replace_the_protected_reviews_selected_intent(self):
        old = recovery._new_action_paths(self.root, self.operation, 2)
        recovery._write_action_record(old.record_path, recovery.read_operator_action_record(self.action_path))
        self.transfer()
        self.assertIsNotNone(observation.read_binding(self.root, self.operation)[3])
        self.assertTrue(old.record_path.exists())
        self.action_path.unlink()
        with self.assertRaises(ControlError):observation.read_binding(self.root, self.operation)

    def test_current_offline_review_keeps_historical_runtime_status_out_of_current_observation(self):
        self.transfer();before = state.inspect_state(self.root), self.canonical.path.read_bytes()
        view = canonical_review(self.root, self.operation,
            control_manifest_sha256=self.fixture.fixture.digest)
        self.assertEqual(view['generation'], 3)
        self.assertEqual(view['runtime_status'], 'offline')
        self.assertEqual(view['canonical_plan']['runtimeStatus'], 'offline')
        self.assertEqual(view['permitted_actions'], ['continue_recovery'])
        self.assertTrue(view['read_only'])
        self.assertEqual((state.inspect_state(self.root), self.canonical.path.read_bytes()), before)

    def test_old_dead_lease_is_inactive_only_with_free_lock_and_exact_transfer_witness(self):
        self.transfer()
        # Kernel death is substituted here; actual child death/lock behavior is
        # covered by test_update_dead_controller and canonical transfer is real.
        old = self.canonical.lease.lease
        with patch.object(state, 'read_controller_lease', return_value=old), patch.object(
                state, 'controller_lease_is_current', return_value=False):
            self.assertEqual(recovery._controller_status(self.root, self.operation, 3), 'inactive')
            with patch.object(state, 'controller_lease_is_current', return_value=True):
                self.assertEqual(recovery._controller_status(self.root, self.operation, 3), 'unknown')
            with patch.object(update_grants, 'read_grant', side_effect=ControlError('missing historical grant')):
                self.assertEqual(recovery._controller_status(self.root, self.operation, 3), 'unknown')
