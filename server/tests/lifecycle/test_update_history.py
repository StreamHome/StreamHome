import json
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from lifecycle import managed_update, state, update_controller, update_execution, update_history
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_managed_update_preparation import ManagedUpdatePreparationFixture


class UpdateHistoryTests(ManagedUpdatePreparationFixture, unittest.TestCase):
    def test_completed_update_rollback_proves_older_links_to_a_following_management_owner(self):
        from lifecycle import runtime_links,execution_state,management_store
        from lifecycle.update_recovery import digest
        payload=self.terminal(accepted=True,previous=True)
        update_history.retain_terminal(self.root,self.lease)
        self.complete('verifying');self.lease.release()
        operation=str(uuid.uuid4())
        lease,claim=state.claim_management_controller(self.root,operation_id=operation,expected_generation=2)
        self.addCleanup(lease.release)
        following=SimpleNamespace(previous=self.parent.execution,installation_id=self.parent.installation_id,
            operation_id=operation,generation=claim.state.generation)
        bound=update_execution.parse(payload)
        original={'schema_version':1,'operation_id':self.operation,'generation':2,
            'previous':execution_tuple_sha256(bound.parent.execution),
            'candidate':execution_tuple_sha256(bound.candidate),'stage':'complete'}
        receipt={'schema_version':1,'operation_id':self.operation,'generation':2,'source_generation':2,
            'source_receipt_sha256':digest(original),'target_execution_sha256':execution_tuple_sha256(self.parent.execution),
            'stage':'complete'}
        path=self.layout.state/f'runtime-link-update-recovery-{self.operation}-2.json'
        state._atomic_write_json(path,receipt)
        with patch.object(runtime_links,'layout_for_root',return_value=self.layout), \
                patch.object(execution_state,'read_active_execution',return_value=self.active), \
                patch.object(management_store,'inspect_management_journal',side_effect=AssertionError('UPDATE has its own history')):
            self.assertTrue(runtime_links._completed_previous_binding(self.root,following,original))
            for change in ({'stage':'applying'},{'source_receipt_sha256':'f'*64},
                    {'target_execution_sha256':execution_tuple_sha256(bound.candidate)},{'schema_version':True}):
                state._atomic_write_json(path,{**receipt,**change})
                with self.subTest(change=change),self.assertRaises(runtime_links.RuntimeBuildError):
                    runtime_links._completed_update_previous_binding(self.root,following,original,self.active)
            state._atomic_write_json(path,receipt)
            retained=update_history._path(self.root,self.operation,2)
            history=json.loads(retained.read_bytes());history['journal_sha256']='f'*64
            state._atomic_write_json(retained,history)
            with self.assertRaises(runtime_links.RuntimeBuildError):
                runtime_links._completed_update_previous_binding(self.root,following,original,self.active)

    def terminal(self, *, accepted=False, previous=False):
        payload = update_controller.read_update_journal(self.path)
        selected = self.parent.execution
        if accepted:
            for old, new in ((state.LifecyclePhase.RESERVED, state.LifecyclePhase.EXCLUSIVE),
                    (state.LifecyclePhase.EXCLUSIVE, state.LifecyclePhase.VERIFYING)):
                state.transition_operation_with_credential(self.root, operation_id=self.operation,
                    generation=2, expected_phase=old, next_phase=new)
            candidate = managed_update.prepared_execution(self.parent.execution, payload['target_commit'], self.built)
            selected = self.parent.execution if previous else candidate
            payload.update(managed_candidate=execution_tuple_payload(candidate), phase='rolled_back' if previous else 'succeeded',
                lifecycle_phase='verifying', runtime_committed=True, runtime_committed_at=1,
                managed_verification_execution_sha256=execution_tuple_sha256(selected),
                managed_accepted_execution_sha256=execution_tuple_sha256(selected),
                current_commit=selected.application_commit, finished_at=2)
            if previous:payload['failed_target'] = payload['target_commit']
        else:
            payload.update(phase='failed')
        self.path.write_text(json.dumps(payload))
        self.active = SimpleNamespace(installation_id=self.parent.installation_id, execution=selected,
            operation_id=self.operation, generation=2)
        self.enterContext(patch.object(update_history, 'read_active_execution', return_value=self.active))
        return payload

    def complete(self, phase):
        state.complete_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase=phase, expected_operation_type=state.OperationType.UPDATE)

    def test_exact_terminal_record_is_retained_before_completion_and_cannot_retire_live_owner(self):
        payload = self.terminal()
        record = update_history.retain_terminal(self.root, self.lease)
        self.assertEqual(record['journal'], payload)
        self.assertEqual(update_history.retain_terminal(self.root, self.lease), record)
        original = self.path.read_bytes()
        self.complete('reserved')
        with self.assertRaisesRegex(ControlError, 'control_controller_busy'):
            update_history.require_retained(self.root, payload, state.inspect_state(self.root))
        self.lease.release()
        update_history.require_retained(self.root, payload, state.inspect_state(self.root))
        self.assertEqual(self.path.read_bytes(), original)
        changed = {**payload, 'message': 'Unrecorded later change'}
        with self.assertRaisesRegex(ControlError, 'control_update_history_changed'):
            update_history.require_retained(self.root, changed, state.inspect_state(self.root))

    def test_accepted_previous_exit_contract_requires_retained_history_and_exact_active_generation(self):
        from lifecycle import recovery
        payload = self.terminal(accepted=True, previous=True)
        self.assertTrue(recovery._terminal_update_evidence(payload, self.operation))
        update_history.retain_terminal(self.root, self.lease)
        self.complete('verifying');self.lease.release()
        with patch.object(recovery, 'layout_for_root', return_value=self.layout), patch.object(
                recovery, '_controller_status', return_value='inactive'), patch.object(
                recovery, '_installed_commit', return_value=self.parent.execution.application_commit), patch(
                'lifecycle.execution_state.read_active_execution', return_value=self.active):
            self.assertTrue(recovery._verified_update_exit_twelve(self.root, self.operation, 2))
            self.path.write_text(json.dumps({'phase': 'checking'}))
            self.assertTrue(recovery._verified_update_exit_twelve(self.root, self.operation, 2))
            self.active.generation = 3
            self.assertFalse(recovery._verified_update_exit_twelve(self.root, self.operation, 2))
            self.active.generation = 2
            update_history._path(self.root, self.operation, 2).unlink()
            self.assertFalse(recovery._verified_update_exit_twelve(self.root, self.operation, 2))
            legacy = self.layout.durable_runtime/'pre-update-database.db'
            legacy.write_bytes(b'Unrelated retained evidence')
            self.assertFalse(recovery._cleanup_update_resolution(self.root, self.operation, 2, 'continue_recovery'))
            self.assertEqual(legacy.read_bytes(), b'Unrelated retained evidence')

    def test_retention_before_completion_can_retry_in_fresh_generation_without_replacing_prior_evidence(self):
        from lifecycle import update_recovery
        original = self.terminal(accepted=True)
        update_history.retain_terminal(self.root, self.lease)
        first = update_history._path(self.root, self.operation, 2)
        first_bytes = first.read_bytes()
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=2)
        self.lease.release()
        lease, _ = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation, generation=2,
            expected_phase='verifying', owner_capability=capability, next_owner_kind='updater')
        self.addCleanup(lease.release)
        with patch.object(update_recovery, 'require_controller_permission'):
            update_controller._record_transferred_generation(self.root, operation_id=self.operation,
                previous_generation=2, generation=3, lifecycle_phase=state.LifecyclePhase.VERIFYING,
                target_commit=original['target_commit'], previous_commit=original['previous_commit'])
        self.active.generation = 3
        current = update_controller.read_update_journal(self.path)
        retained = update_history.retain_terminal(self.root, lease)
        self.assertEqual(retained['generation'], 3)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertNotEqual(update_history._path(self.root, self.operation, 3), first)
        self.assertEqual(current['runtime_committed_at'], original['runtime_committed_at'])
        state.complete_operation_with_credential(self.root, operation_id=self.operation, generation=3,
            expected_phase='verifying', expected_operation_type='update')
        lease.release()
        update_history.require_retained(self.root, current, state.inspect_state(self.root))
        self.assertEqual(first.read_bytes(), first_bytes)

    def test_nonterminal_owner_cannot_fabricate_completed_history(self):
        self.terminal()
        payload = update_controller.read_update_journal(self.path);payload['phase'] = 'preflight'
        self.path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ControlError, 'control_update_history_nonterminal'):
            update_history.retain_terminal(self.root, self.lease)
        self.assertFalse((state.lifecycle_paths(self.root).lifecycle_dir/'update-history').exists())

    def test_accepted_history_requires_matching_active_tuple_and_cannot_be_replaced(self):
        payload = self.terminal(accepted=True)
        candidate = self.active.execution
        self.active.execution = self.parent.execution
        with self.assertRaisesRegex(ControlError, 'control_parent_binding_changed'):
            update_history.retain_terminal(self.root, self.lease)
        self.active.execution = candidate
        record = update_history.retain_terminal(self.root, self.lease)
        self.assertEqual(update_execution.parse(record['journal']).accepted, candidate)
        payload['message'] = 'Different terminal record'
        self.path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ControlError, 'control_update_history_changed'):
            update_history.retain_terminal(self.root, self.lease)

    def writer(self):
        from services import update
        for name, value in {'WORKSPACE_ROOT': self.root, 'RUN_DIR': self.path.parent,
                'STATUS_PATH': self.path, 'STATUS_LOCK_PATH': self.path.with_name('update-state.lock')}.items():
            self.enterContext(patch.object(update, name, value))
        return update

    def test_next_discovery_keeps_original_accepted_history_and_uses_unbound_status(self):
        payload = self.terminal(accepted=True)
        original = update_history.retain_terminal(self.root, self.lease)
        self.complete('verifying');self.lease.release()
        writer = self.writer()
        result = writer.write_update_state(phase='checking', target_commit='f'*40,
            current_commit=self.active.execution.application_commit,
            runtime_committed=False, runtime_committed_at=None)
        self.assertIsNone(update_execution.parse(result))
        self.assertEqual(result['phase'], 'checking')
        self.assertEqual(result['target_commit'], 'f'*40)
        self.assertFalse(result['runtime_committed'])
        from lifecycle.control.provision_worker import _private_record
        self.assertEqual(_private_record(update_history._path(self.root, self.operation, 2)), original)
        self.assertEqual(update_execution.parse(original['journal']).accepted, self.active.execution)

    def test_missing_history_blocks_idle_rewrite_without_losing_original_record(self):
        self.terminal();original = self.path.read_bytes()
        self.complete('reserved');self.lease.release()
        writer = self.writer()
        with self.assertRaises(writer.UpdateJournalReadError):writer.write_update_state(phase='checking')
        self.assertEqual(self.path.read_bytes(), original)

    def test_new_reservation_does_not_inherit_the_prior_managed_parent_or_acceptance(self):
        self.terminal(accepted=True)
        update_history.retain_terminal(self.root, self.lease)
        self.complete('verifying');self.lease.release()
        claim = state.claim_operation_with_credential(self.root,
            operation_type=state.OperationType.UPDATE, owner_kind=state.OwnerKind.UPDATER)
        operation = claim.state.active_operation
        writer = self.writer()
        result = writer.write_update_state(phase='queued', transaction_id=operation.operation_id,
            lifecycle_generation=claim.state.generation, lifecycle_phase='reserved',
            previous_commit=self.active.execution.application_commit,
            current_commit=self.active.execution.application_commit, target_commit='f'*40,
            runtime_committed=False, runtime_committed_at=None)
        self.assertIsNone(update_execution.parse(result))
        self.assertEqual(result['transaction_id'], operation.operation_id)
        self.assertEqual(result['lifecycle_generation'], claim.state.generation)
        self.assertFalse(result['runtime_committed'])

    def test_completed_history_does_not_authorize_recovery_or_a_different_active_operation(self):
        payload = self.terminal();update_history.retain_terminal(self.root, self.lease)
        self.complete('reserved');self.lease.release()
        claimed = state.claim_operation_with_credential(self.root,
            operation_type=state.OperationType.UPDATE, owner_kind=state.OwnerKind.UPDATER)
        update_history.require_retained(self.root, payload, claimed.state)
        operation = claimed.state.active_operation
        wrong = state.LifecycleState(2, claimed.state.generation,
            state.ActiveOperation(operation.operation_id, state.OperationType.RESTORE, operation.owner_kind,
                operation.phase, operation.owner_nonce_hash))
        with self.assertRaises(ControlError):update_history.require_retained(self.root, payload, wrong)


if __name__ == '__main__':unittest.main()
