"""Actual abort claim/lease/journal/history; protected OS effects are substituted."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from lifecycle import managed_update, recovery, state, update_abort, update_checkpoint
from lifecycle import update_controller, update_execution, update_history, update_recovery
from lifecycle import update_recovery_observation
from lifecycle.control import update_grants
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import execution_tuple_sha256
from tests.lifecycle import test_update_abort_review as review_fixture


class UpdateAbortTests(unittest.TestCase):
    def setUp(self):
        fixture = review_fixture.UpdateAbortReviewTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation;self.layout = fixture.layout
        self.active = update_recovery.read_active_execution(self.root)
        for module in (update_abort, update_controller, update_checkpoint, update_recovery_observation):
            self.enterContext(patch.object(module, 'layout_for_root', return_value=self.layout))
        self.enterContext(patch.object(update_abort, 'read_active_execution', return_value=self.active))
        self.enterContext(patch.object(update_history, 'read_active_execution', return_value=self.active))
        self.enterContext(patch.object(update_recovery_observation, 'read_active_execution', return_value=self.active))
        web = self.root/'web';web.mkdir(mode=0o700)
        for name, target in (('node_modules', fixture.parent.execution.web_dependency_target),
                ('dist', fixture.parent.execution.web_dist_target)):
            (web/name).symlink_to(target)
        self.receipt = self.layout.state/'runtime-links.json'
        state._atomic_write_json(self.receipt, {'schema_version': 1, 'operation_id': self.active.operation_id,
            'generation': 0, 'previous': None, 'candidate': execution_tuple_sha256(self.active.execution),
            'stage': 'complete'})
        self.enterContext(patch.object(update_grants, 'require_scope'))
        self.stopped = self.enterContext(patch.object(update_abort, 'require_stopped'))
        self.enterContext(patch.object(update_grants, 'read_stop_epoch', return_value='f'*64))
        self.enterContext(patch.object(managed_update, 'require_operation_entry'))
        self.snapshot = fixture.view()
        self.grant = update_grants.prepare(self.root, {'operation_id': self.operation,
            'review_id': str(uuid.uuid4()), 'expected_evidence_sha256': self.snapshot['evidence_sha256'],
            'action': 'safe_abort', 'issuer_scope': {
                'unit': 'streamhome-install-'+fixture.parent.installation_id+'-'+uuid.uuid4().hex+'.scope',
                'invocation': 'a'*32, 'pid': os.getpid(), 'start_identity': '123'},
            'boot_id': self.snapshot['boot_id'], 'stop_epoch': 'f'*64},
            control_manifest_sha256=fixture.parent.execution.control_manifest_sha256)
        self.grants = {2: self.grant}
        self.enterContext(patch.object(update_grants, 'read_grant',
            side_effect=lambda operation, generation, **kwargs: self.grants[generation]))
        # Substitute protected path access; retain real first-generation lstat selection.
        self.enterContext(patch.object(update_grants, 'grant_path', return_value=self.receipt))

    def execute(self):
        return update_recovery.execute(self.root, self.operation,
            control_manifest_sha256=self.fixture.parent.execution.control_manifest_sha256)

    def test_abort_transfers_original_reservation_retains_history_and_never_runs_shell_or_baseline_reset(self):
        database = self.layout.database
        database.write_bytes(b'Canonical bytes must remain untouched')
        before = database.read_bytes(), self.receipt.read_bytes()
        popen = update_controller.subprocess.Popen
        def read_only_process(command, *args, **kwargs):
            self.assertNotIn(Path(str(command[0])).name, {'bash', 'start.sh', 'update.sh'})
            return popen(command, *args, **kwargs)
        with (patch.object(update_controller, 'validate_pre_cutover_baseline', side_effect=AssertionError('No Git reset')),
                patch.object(update_controller.subprocess, 'Popen', side_effect=read_only_process)):
            self.assertEqual(self.execute(), 0)
        authority = state.inspect_state(self.root)
        self.assertIsNone(authority.active_operation);self.assertEqual(authority.generation, 2)
        retained = json.loads(update_history._path(self.root, self.operation, 2).read_text())
        self.assertEqual(retained['journal']['error'], 'operator_safe_abort')
        self.assertIsNone(retained['journal']['managed_accepted_execution_sha256'])
        self.assertEqual((database.read_bytes(), self.receipt.read_bytes()), before)
        with patch('lifecycle.layout.layout_for_root', return_value=self.layout):
            result = update_recovery.result(self.root, self.operation, 2,
                control_manifest_sha256=self.fixture.parent.execution.control_manifest_sha256)
        self.assertIs(result['completed'], True)

    def test_unattributed_checkpoint_and_applying_or_current_receipt_cannot_be_abandoned(self):
        original = self.receipt.read_bytes()
        payload = json.loads(original)
        journal = update_controller.read_update_journal(self.fixture.fixture.path)
        bound = update_execution.parse(journal)
        for changes in ({'stage': 'applying'}, {'operation_id': self.operation}, {'candidate': '0'*64}):
            state._atomic_write_json(self.receipt, {**payload, **changes})
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                update_abort.require_no_effects(self.root, journal, bound)
        self.receipt.write_bytes(original)
        checkpoint = update_checkpoint.path_for(self.root, self.operation, create=True)
        checkpoint.symlink_to('/missing-unattributed-snapshot')
        with self.assertRaises(ControlError):update_abort.require_no_effects(self.root, journal, bound)
        self.assertTrue(checkpoint.is_symlink())

    def test_expired_permission_never_transfers_or_changes_the_original_claim(self):
        before = state.inspect_state(self.root), self.fixture.fixture.path.read_bytes()
        with patch.object(update_grants, 'require_scope', side_effect=ControlError('issuer gone')):
            with self.assertRaises(ControlError):self.execute()
        self.assertEqual((state.inspect_state(self.root), self.fixture.fixture.path.read_bytes()), before)

    def test_fixed_unit_dispatch_does_not_misclassify_reserved_root_abort_as_initial_handoff(self):
        with patch.object(managed_update, 'require_initial_dispatch', side_effect=AssertionError('Original Sup is stopped')):
            self.assertEqual(managed_update.execute(self.root, self.operation,
                control_manifest_sha256=self.fixture.parent.execution.control_manifest_sha256), 0)
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def fresh_permission(self):
        snapshot = self.fixture.view()
        value = update_grants.prepare(self.root, {'operation_id': self.operation,
            'review_id': str(uuid.uuid4()), 'expected_evidence_sha256': snapshot['evidence_sha256'],
            'action': 'safe_abort', 'issuer_scope': self.grant['issuer_scope'],
            'boot_id': snapshot['boot_id'], 'stop_epoch': 'f'*64},
            control_manifest_sha256=self.fixture.parent.execution.control_manifest_sha256)
        self.grants[snapshot['generation']+1] = value
        return snapshot

    def test_caught_abort_failure_keeps_exact_phase_witness_and_fresh_retry_closes_same_operation(self):
        before = self.receipt.read_bytes()
        with (patch.object(update_abort, 'complete', side_effect=OSError('Injected before abort terminal publication')),
                patch.object(update_controller, 'reconcile_operator_journal_recovery_required',
                    side_effect=update_controller.UpdateControllerPersistenceError('Injected subordinate fence failure'))):
            with self.assertRaises(recovery.RecoveryExecutionError):self.execute()
        authority, journal, _, witness = update_recovery_observation.read_binding(self.root, self.operation)
        self.assertEqual(authority.generation, 2)
        self.assertEqual(authority.active_operation.phase.value, 'recovery_required')
        self.assertEqual(journal['lifecycle_phase'], 'reserved')
        self.assertIs(witness['phase_fence'], True)
        self.grant['expires_at_monotonic'] = .001
        # Old permission is historical evidence only; the fresh review is read-only.
        snapshot = self.fresh_permission()
        self.assertEqual(snapshot['permitted_actions'], ['safe_abort'])
        self.assertEqual(self.execute(), 0)
        self.assertEqual(state.inspect_state(self.root).generation, 3)
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_transfer_before_journal_failure_remains_one_witnessed_generation_and_retries(self):
        with patch.object(update_controller, '_record_transferred_generation',
                side_effect=OSError('Injected after ownership CAS')):
            with self.assertRaises(recovery.RecoveryExecutionError):self.execute()
        authority, journal, _, witness = update_recovery_observation.read_binding(self.root, self.operation)
        self.assertEqual(authority.generation, 2);self.assertEqual(journal['lifecycle_generation'], 1)
        self.assertEqual(witness['source_generation'], 1)
        self.fresh_permission()
        self.assertEqual(self.execute(), 0)
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_second_stopped_proof_failure_keeps_terminal_history_and_can_retry_without_database_work(self):
        self.stopped.side_effect = [None, ControlError('A service start invalidated stopped proof')]
        with self.assertRaises(recovery.RecoveryExecutionError):self.execute()
        retained = update_history._path(self.root, self.operation, 2).read_bytes()
        self.stopped.side_effect = None
        self.fresh_permission()
        self.assertEqual(self.execute(), 0)
        self.assertEqual(update_history._path(self.root, self.operation, 2).read_bytes(), retained)
        self.assertTrue(update_history._path(self.root, self.operation, 3).exists())

    def test_phase_fence_refuses_changed_request_or_missing_acknowledged_action(self):
        with (patch.object(update_abort, 'complete', side_effect=OSError('Injected abort interruption')),
                patch.object(update_controller, 'reconcile_operator_journal_recovery_required',
                    side_effect=update_controller.UpdateControllerPersistenceError('Injected subordinate fence failure'))):
            with self.assertRaises(recovery.RecoveryExecutionError):self.execute()
        path = self.fixture.fixture.path
        original = path.read_bytes();payload = json.loads(original)
        payload['update_branch'] = 'foreign'
        path.write_text(json.dumps(payload))
        with self.assertRaises(ControlError):update_recovery_observation.read_binding(self.root, self.operation)
        path.write_bytes(original)
        paths = recovery._new_action_paths(self.root, self.operation, 1, action_id=self.grant['review_id'])
        paths.record_path.unlink()
        with self.assertRaises(ControlError):
            update_recovery_observation.read_binding(self.root, self.operation)
