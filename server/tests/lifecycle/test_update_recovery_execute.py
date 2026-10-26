"""Root UPDATE adapter delegates to canonical recovery; OS/engine boundaries explicit."""
import unittest
from unittest.mock import patch

from lifecycle import managed_update, recovery, state, update_recovery
from lifecycle.control import update_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_grants as grant_fixture


class UpdateRecoveryExecutionTests(unittest.TestCase):
    def setUp(self):
        fixture = grant_fixture.UpdateGrantTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.fixture.operation
        self.grant = fixture.prepare()
        self.reader = self.enterContext(patch.object(update_grants, 'read_grant', return_value=self.grant))
        self.peer = self.enterContext(patch.object(managed_update, 'require_operation_entry'))

    def execute(self):return update_recovery.execute(self.root, self.operation,
        control_manifest_sha256=self.fixture.fixture.digest)

    def test_adapter_uses_original_canonical_action_evidence_and_checks_current_grant(self):
        before = state.inspect_state(self.root), self.fixture.fixture.fixture.path.read_bytes()
        def engine(root, **kwargs):
            self.assertEqual(root, self.root)
            self.assertEqual(kwargs['operation_id'], self.operation)
            self.assertEqual(kwargs['generation'], 2)
            self.assertEqual(kwargs['action'], 'continue_recovery')
            self.assertEqual(kwargs['evidence_identity'], self.grant['canonical_evidence_identity'])
            kwargs['execution_guard']()
            self.fixture.stop.return_value = 'f'*64
            with self.assertRaises(ControlError):kwargs['execution_guard']()
            self.fixture.stop.return_value = self.grant['stop_epoch']
            return {'status': 'succeeded'}
        with patch.object(recovery, 'execute_recovery', side_effect=engine) as execute:
            self.assertEqual(self.execute(), 0)
        execute.assert_called_once()
        self.assertEqual((state.inspect_state(self.root), self.fixture.fixture.fixture.path.read_bytes()), before)
        self.peer.assert_called_once()

    def test_stale_review_or_incompatible_reader_cannot_reach_canonical_transfer(self):
        with patch.object(recovery, 'execute_recovery') as execute:
            self.fixture.snapshot['evidence_sha256'] = 'f'*64
            with self.assertRaises(ControlError):self.execute()
            execute.assert_not_called()
            self.fixture.snapshot['evidence_sha256'] = self.grant['source_evidence_sha256']
            self.fixture.reader.side_effect = ControlError('control_bound_reader_incompatible')
            with self.assertRaises(ControlError):self.execute()
            execute.assert_not_called()

    def test_fixed_operation_dispatch_uses_root_adapter_for_recovery_without_initial_claim(self):
        with patch.object(update_recovery, 'execute', return_value=0) as execute, patch.object(
                managed_update, 'require_initial_dispatch') as initial:
            self.assertEqual(managed_update.execute(self.root, self.operation,
                control_manifest_sha256=self.fixture.fixture.digest), 0)
        initial.assert_not_called()
        execute.assert_called_once_with(self.root, self.operation,
            control_manifest_sha256=self.fixture.fixture.digest)

    def test_exact_root_intent_cannot_be_replayed_or_overwrite_a_prior_attempt(self):
        snapshot = self.fixture.snapshot['canonical_plan']
        before = state.inspect_state(self.root), self.fixture.fixture.fixture.path.read_bytes()
        def execute():return recovery.execute_recovery(self.root, operation_id=self.operation,
            generation=2, action='continue_recovery', evidence_identity=snapshot['evidenceIdentity'],
            action_id=self.grant['review_id'])
        with patch('lifecycle.update_controller.run_operator_recovery', side_effect=RuntimeError('Injected before transfer')):
            with self.assertRaisesRegex(recovery.RecoveryExecutionError, 'failed closed'):execute()
        record = recovery._new_action_paths(self.root, self.operation, 2,
            action_id=self.grant['review_id']).record_path
        retained = record.read_bytes()
        with self.assertRaisesRegex(recovery.RecoveryExecutionError, 'intent already exists'):execute()
        self.assertEqual(record.read_bytes(), retained)
        self.assertEqual((state.inspect_state(self.root), self.fixture.fixture.fixture.path.read_bytes()), before)
