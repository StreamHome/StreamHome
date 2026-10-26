"""Acknowledged continuation can explain only its own terminal phase fence."""
import json
import unittest
from unittest.mock import patch

from lifecycle import state, update_controller, update_recovery, update_recovery_observation
from lifecycle.update_recovery import review as canonical_review
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_resumption_runtime as runtime_fixture


class UpdateRecoveryFencePhaseTests(unittest.TestCase):
    def setUp(self):
        fixture = runtime_fixture.UpdateResumptionRuntimeTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation
        self.canonical = fixture.canonical
        self.enterContext(patch.object(update_recovery_observation, 'layout_for_root', return_value=self.canonical.layout))
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=3,
            expected_phase='verifying', next_phase='recovery_required')
        fixture.fixture.lease.release()
        fixture.fixture.grant['expires_at_monotonic'] = .001

    def test_expired_permission_is_only_historical_phase_proof_and_new_readonly_review_remains_reachable(self):
        before = self.canonical.path.read_bytes(), state.inspect_state(self.root)
        authority, journal, bound, witness = update_recovery_observation.read_binding(self.root, self.operation)
        self.assertEqual(authority.active_operation.phase.value, 'recovery_required')
        self.assertEqual(journal['lifecycle_phase'], 'verifying')
        self.assertIs(witness['phase_fence'], True)
        with self.assertRaises(ControlError):
            update_recovery.require_controller_permission(self.root, journal, bound, 3)
        view = canonical_review(self.root, self.operation,
            control_manifest_sha256=self.fixture.fixture.grant['control_manifest_sha256'])
        self.assertEqual(view['permitted_actions'], ['continue_recovery'])
        self.assertEqual((self.canonical.path.read_bytes(), state.inspect_state(self.root)), before)

    def test_changed_action_or_missing_transfer_acknowledgement_cannot_explain_phase_fence(self):
        paths = self.fixture.fixture.paths
        original = paths.record_path.read_bytes();action = json.loads(original)
        for changes in ({'controller_generation': None}, {'requested_action': 'safe_abort'},
                {'evidence_identity': 'plan-v1:'+'f'*32}):
            paths.record_path.write_text(json.dumps({**action, **changes}))
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                update_recovery_observation.read_binding(self.root, self.operation)
        paths.record_path.write_bytes(original)
