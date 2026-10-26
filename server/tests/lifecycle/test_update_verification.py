"""Actual UPDATE authority/selection, substituted OS custody and bundle checks."""
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import managed_update, state, update_controller, update_execution
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_update_delegation import UpdateDelegateFixture


class UpdateVerificationTests(UpdateDelegateFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        payload = update_controller.read_update_journal(self.path)
        self.candidate = managed_update.prepared_execution(self.parent.execution, payload['target_commit'], self.built)
        for old, new in (('reserved', 'exclusive'), ('exclusive', 'verifying')):
            state.transition_operation_with_credential(self.root, operation_id=self.operation,
                generation=2, expected_phase=old, next_phase=new)
        payload.update(managed_candidate=execution_tuple_payload(self.candidate),
            lifecycle_phase='verifying', phase='starting')
        self.path.write_text(json.dumps(payload))
        self.links = self.enterContext(patch('lifecycle.runtime_links.verify_links'))
        self.rpc = self.enterContext(patch.object(managed_update, 'supervisor_command', return_value={'stopped': True}))
        # Actual database/checkpoint semantics are covered by test_update_checkpoint.
        self.database_launch = self.enterContext(patch('lifecycle.update_checkpoint.require_launch_database'))

    def select(self, target='candidate'):
        return managed_update.select_verification(self.root, self.proof(), target)

    def test_selection_is_in_original_journal_and_never_accepts_or_completes_authority(self):
        before = state.inspect_state(self.root)
        self.assertEqual(self.select(), self.candidate)
        self.assertEqual(self.select(), self.candidate)
        selected = update_execution.parse(update_controller.read_update_journal(self.path))
        self.assertEqual(selected.verification, self.candidate)
        self.assertIsNone(selected.accepted)
        self.assertEqual(state.inspect_state(self.root), before)
        self.rpc.assert_called_with(self.root, self.proof(), 'update_assert_stopped', authority_locked=True)
        self.links.assert_called_with(self.root, self.candidate)

    def test_busy_children_or_changed_journal_preserve_original_selection(self):
        original = self.path.read_bytes()
        self.rpc.return_value = {'stopped': False}
        with self.assertRaisesRegex(ControlError, 'control_children_busy'):self.select()
        self.assertEqual(self.path.read_bytes(), original)
        self.rpc.return_value = {'stopped': True}
        def changed(*args):
            payload = json.loads(original);payload['message'] = 'Concurrent evidence'
            self.path.write_text(json.dumps(payload))
        self.links.side_effect = changed
        with self.assertRaisesRegex(ControlError, 'control_parent_binding_changed'):self.select()
        self.assertIsNone(update_execution.parse(update_controller.read_update_journal(self.path)).verification)

    def test_unaccepted_candidate_can_select_previous_only_on_original_rollback_path(self):
        self.select()
        original = self.path.read_bytes()
        with self.assertRaises(ControlError):self.select('previous')
        self.assertEqual(self.path.read_bytes(), original)
        payload = json.loads(original)
        payload.update(phase='failed', current_commit=self.parent.execution.application_commit,
            failed_target=payload['target_commit'])
        self.path.write_text(json.dumps(payload))
        self.assertEqual(self.select('previous'), self.parent.execution)
        payload = update_controller.read_update_journal(self.path)
        payload['phase'] = 'starting';self.path.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):self.select('candidate')

    def test_acceptance_irreversibly_fixes_verification_even_if_detail_phase_changes(self):
        self.select()
        payload = update_controller.read_update_journal(self.path)
        payload.update(runtime_committed=True, runtime_committed_at=1,
            current_commit=self.candidate.application_commit,
            managed_accepted_execution_sha256=execution_tuple_sha256(self.candidate))
        self.path.write_text(json.dumps(payload))
        self.assertEqual(self.select(), self.candidate)
        payload.update(phase='failed', failed_target=payload['target_commit'])
        self.path.write_text(json.dumps(payload));original = self.path.read_bytes()
        with self.assertRaises(ControlError):self.select('previous')
        self.assertEqual(self.path.read_bytes(), original)

    def test_private_commit_token_rejects_aliases_hardlinks_permissions_and_malformed_bytes(self):
        path = self.path.parent/('update-commit.'+self.operation+'.token')
        path.write_text('a'*43+'\n');path.chmod(0o600)
        self.assertEqual(managed_update.read_commit_token(self.root, self.operation), 'a'*43)
        path.chmod(0o644)
        with self.assertRaises(ControlError):managed_update.read_commit_token(self.root, self.operation)
        path.chmod(0o600)
        link = path.with_name('retained-token');os.link(path, link)
        with self.assertRaises(ControlError):managed_update.read_commit_token(self.root, self.operation)
        link.unlink()
        path.rename(link);path.symlink_to(link)
        with self.assertRaises(OSError):managed_update.read_commit_token(self.root, self.operation)
        path.unlink();link.rename(path)
        for raw in ('a'*42, 'a'*44, 'a'*43+'\n\n', 'a'*42+'!'):
            path.write_text(raw)
            with self.subTest(length=len(raw)), self.assertRaises(ControlError):
                managed_update.read_commit_token(self.root, self.operation)

    def test_backend_selection_requires_exact_original_parent_invocation_and_stop_epoch(self):
        self.select()
        parent = self.parent
        environment = {'STREAMHOME_LIFECYCLE_OPERATION_ID': self.operation,
            'STREAMHOME_LIFECYCLE_GENERATION': '2', 'INVOCATION_ID': parent.supervisor_invocation_id}
        unit = {'ActiveState': 'active', 'MainPID': str(parent.supervisor_pid),
            'InvocationID': parent.supervisor_invocation_id}
        with patch('lifecycle.control.privileged.unit_snapshot', return_value=unit), patch.object(
                managed_update.os, 'getppid', return_value=parent.supervisor_pid):
            self.assertEqual(managed_update.verification_execution(self.root, environment), self.candidate)
            for field, invalid in (('STREAMHOME_LIFECYCLE_GENERATION', '3'), ('INVOCATION_ID', 'f'*32)):
                with self.subTest(field=field), self.assertRaises(ControlError):
                    managed_update.verification_execution(self.root, {**environment, field: invalid})
            with patch('lifecycle.control.tickets.read_stop_epoch', return_value='f'*64):
                with self.assertRaises(ControlError):managed_update.verification_execution(self.root, environment)
            with patch.object(managed_update.os, 'getppid', return_value=parent.supervisor_pid+1):
                with self.assertRaises(ControlError):managed_update.verification_execution(self.root, environment)


    def acceptance_boundaries(self):
        from dataclasses import replace
        from lifecycle.execution_state import ExecutionRecord, parse_execution_record
        directory = self.root.parent/'accepted-state';directory.mkdir(mode=0o700)
        self.enterContext(patch.object(managed_update, 'layout_for_root', return_value=replace(self.layout, state=directory)))
        self.enterContext(patch.object(managed_update, '_require_acceptance_ready'))
        self.enterContext(patch.object(managed_update, 'verification_execution', return_value=self.candidate))
        old = ExecutionRecord(self.parent.installation_id, self.parent.execution, self.operation, 0)
        path = directory/'active-runtime.json'
        def active(root):
            return parse_execution_record(json.loads(path.read_text())) if path.exists() else old
        self.enterContext(patch.object(managed_update, 'read_active_execution', side_effect=active))
        return path

    def accept(self):
        return managed_update.accept_verification(self.root, operation_id=self.operation,
            generation=2, expected_detail_phase='starting')

    def test_acceptance_precedes_pointer_publication_and_retry_preserves_original_timestamp(self):
        self.select();path = self.acceptance_boundaries()
        original_authority = state.inspect_state(self.root)
        write = state._atomic_write_json
        observed = []
        def publish(destination, payload):
            if destination == path:
                journal = update_controller.read_update_journal(self.path)
                observed.append(update_execution.parse(journal).accepted)
            return write(destination, payload)
        with patch.object(state, '_atomic_write_json', side_effect=publish):
            accepted = self.accept()
            repeated = self.accept()
        self.assertEqual(observed, [self.candidate, self.candidate])
        self.assertEqual(accepted['runtime_committed_at'], repeated['runtime_committed_at'])
        self.assertEqual(json.loads(path.read_text())['execution'], execution_tuple_payload(self.candidate))
        self.assertEqual(state.inspect_state(self.root), original_authority)

    def test_pointer_failure_keeps_irreversible_acceptance_and_requires_same_runtime_on_retry(self):
        self.select();path = self.acceptance_boundaries()
        with patch.object(state, '_atomic_write_json', side_effect=OSError('Injected pointer write failure')):
            with self.assertRaises(OSError):self.accept()
        self.assertFalse(path.exists())
        accepted = update_execution.parse(update_controller.read_update_journal(self.path))
        self.assertEqual(accepted.accepted, self.candidate)
        before = self.path.read_bytes()
        with patch.object(managed_update, 'verification_execution', return_value=self.parent.execution):
            with self.assertRaises(ControlError):self.accept()
        self.assertEqual(self.path.read_bytes(), before)
        self.accept()
        self.assertTrue(path.exists())

    def test_failed_journal_publication_cannot_publish_an_active_candidate(self):
        self.select();path = self.acceptance_boundaries();original = self.path.read_bytes()
        with patch.object(update_controller, '_private_atomic_write', side_effect=OSError('Injected journal failure')):
            with self.assertRaises(OSError):self.accept()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(path.exists())

    def test_missing_guarded_readiness_or_unrelated_active_runtime_cannot_accept(self):
        from lifecycle.execution_state import ExecutionRecord
        from dataclasses import replace
        self.select();path = self.acceptance_boundaries();original = self.path.read_bytes()
        with patch.object(managed_update, '_require_acceptance_ready', side_effect=ControlError('control_guarded_recovery_incomplete')):
            with self.assertRaises(ControlError):self.accept()
        active = ExecutionRecord(self.parent.installation_id,
            replace(self.candidate, application_commit='f'*40), self.operation, 2)
        with patch.object(managed_update, 'read_active_execution', return_value=active):
            with self.assertRaises(ControlError):self.accept()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(path.exists())


    def test_settlement_requires_exact_terminal_retention_and_full_active_identity(self):
        from lifecycle import runtime_admission, update_history
        self.select();path = self.acceptance_boundaries();payload = self.accept()
        payload.update(phase='succeeded', finished_at=payload['runtime_committed_at']+1,
            error='', failed_target='')
        self.path.write_text(json.dumps(payload))
        record = update_history._record(payload)
        history = update_history._path(self.root, self.operation, 2, create=True)
        state._atomic_write_json(history, record)
        admission = runtime_admission.RuntimeAdmission.__new__(runtime_admission.RuntimeAdmission)
        admission.context = SimpleNamespace(operation_type='update', operation_id=self.operation,
            generation=2, installation_root=self.root)
        admission.execution = self.candidate;admission.installation_id = self.parent.installation_id
        from dataclasses import replace
        from lifecycle.execution_state import parse_execution_record
        with patch.object(runtime_admission, 'layout_for_root', return_value=replace(self.layout, state=path.parent)), patch(
                'lifecycle.execution_state.read_active_execution', side_effect=lambda root: parse_execution_record(json.loads(path.read_text()))):
            admission._require_terminal_evidence()
            history.write_text(json.dumps({**record, 'journal_sha256': 'f'*64}))
            with self.assertRaises(runtime_admission.RuntimeAdmissionError):admission._require_terminal_evidence()
            state._atomic_write_json(history, record)
            active = json.loads(path.read_text());active['generation'] = 1;path.write_text(json.dumps(active))
            with self.assertRaises(runtime_admission.RuntimeAdmissionError):admission._require_terminal_evidence()


    def test_future_or_foreign_same_tuple_active_record_cannot_be_relabelled_as_original(self):
        from lifecycle.execution_state import ExecutionRecord
        self.select();path = self.acceptance_boundaries();original = self.path.read_bytes()
        for generation in (self.parent.original_generation, 999):
            active = ExecutionRecord(self.parent.installation_id, self.parent.execution, self.operation, generation)
            with patch.object(managed_update, 'read_active_execution', return_value=active):
                with self.subTest(generation=generation), self.assertRaises(ControlError):self.accept()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(path.exists())

    def test_fresh_generation_publication_requires_its_reviewed_source_and_preserves_acceptance(self):
        from lifecycle import update_recovery
        self.select();path = self.acceptance_boundaries();accepted = self.accept()
        active = managed_update.read_active_execution(self.root)
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=2)
        self.lease.release()
        with patch.object(managed_update.os, 'getpid', return_value=self.main):
            lease, transferred = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation,
                generation=2, expected_phase='verifying', owner_capability=capability, next_owner_kind='updater')
        self.addCleanup(lease.release)
        with patch.object(update_recovery, 'require_controller_permission'):
            update_controller._record_transferred_generation(self.root, operation_id=self.operation,
                previous_generation=2, generation=3, lifecycle_phase=state.LifecyclePhase.VERIFYING,
                target_commit=self.candidate.application_commit, previous_commit=self.parent.execution.application_commit)
        before = self.path.read_bytes(), path.read_bytes()
        def accept():return managed_update.accept_verification(self.root, operation_id=self.operation,
            generation=3, expected_detail_phase='starting')
        with self.assertRaises(ControlError):accept()
        with self.assertRaises(ControlError):
            update_controller.mutate_update_journal(self.root, operation_id=self.operation, generation=3,
                expected_lifecycle_phase='verifying', expected_detail_phases=frozenset({'starting'}),
                changes={'phase': 'recovering'})
        self.assertEqual((self.path.read_bytes(), path.read_bytes()), before)
        # Full grant validation is qualified separately; this boundary test
        # exercises the real canonical transfer, acceptance and pointer writes.
        with patch.object(update_recovery, 'require_controller_permission',
                return_value={'active_record_sha256': update_recovery.digest(active.payload())}):
            repeated = accept()
        self.assertEqual(repeated['runtime_committed_at'], accepted['runtime_committed_at'])
        self.assertEqual(update_execution.parse(repeated).accepted, self.candidate)
        self.assertEqual(json.loads(path.read_text())['generation'], transferred.state.generation)

    def test_unconfirmed_transport_cannot_finalize_or_retain_accepted_recovery(self):
        self.select();self.acceptance_boundaries();payload = self.accept()
        payload['phase'] = 'recovering';self.path.write_text(json.dumps(payload))
        original = self.path.read_bytes();authority = state.inspect_state(self.root)
        with patch.object(managed_update, 'runtime_action', return_value={'ready': False}):
            with self.assertRaisesRegex(ControlError, 'control_runtime_not_ready'):
                managed_update.retain_completed_from_delegate(self.root, self.proof())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(state.inspect_state(self.root), authority)
        self.assertFalse((state.lifecycle_paths(self.root).lifecycle_dir/'update-history').exists())


if __name__ == '__main__':unittest.main()
