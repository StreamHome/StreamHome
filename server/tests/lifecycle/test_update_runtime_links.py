"""Actual UPDATE lease/paired links; runtime inventory and Sup RPC substituted."""
from dataclasses import replace
import json
import io
from contextlib import redirect_stdout
import os
import unittest
from unittest.mock import patch

from lifecycle import managed_update, runtime_links, state, update_controller
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from lifecycle.runtime_build import RuntimeBuildError
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_update_delegation import UpdateDelegateFixture


class UpdateRuntimeLinkTests(UpdateDelegateFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = self.root.parent/'links-state';directory.mkdir(mode=0o700)
        self.enterContext(patch.object(runtime_links, 'layout_for_root', return_value=replace(self.layout, state=directory)))
        self.runtime_verify = self.enterContext(patch.object(runtime_links, 'verify_runtime'))
        self.web = self.root/'web';self.web.mkdir(mode=0o700)
        old = self.parent.execution
        for name, target in (('node_modules', old.web_dependency_target), ('dist', old.web_dist_target)):
            (self.web/name).symlink_to(target, target_is_directory=True)
        self.receipt = directory/'runtime-links.json'
        state._atomic_write_json(self.receipt, {'schema_version': 1, 'operation_id': self.operation,
            'generation': 0, 'previous': None, 'candidate': execution_tuple_sha256(old), 'stage': 'complete'})
        payload = update_controller.read_update_journal(self.path)
        self.candidate = managed_update.prepared_execution(old, payload['target_commit'], self.built)
        payload['managed_candidate'] = execution_tuple_payload(self.candidate)
        self.path.write_text(json.dumps(payload))
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='reserved', next_phase='exclusive')
        payload.update(phase='installing', lifecycle_phase='exclusive')
        self.path.write_text(json.dumps(payload))
        self.stopped = []

    def bind(self, target):
        runtime_links.bind_update_runtime(self.root, proof=self.proof(), target=target,
            assert_children_stopped=lambda: self.stopped.append(True))

    def links(self):return tuple(os.readlink(self.web/name) for name in ('node_modules', 'dist'))

    def test_fixed_worker_uses_original_supervisor_and_does_not_reacquire_held_state_in_callback(self):
        from types import SimpleNamespace
        from lifecycle.control import workers
        from lifecycle.control.rpc import ControlError
        unit = {'ActiveState': 'active', 'MainPID': str(self.parent.supervisor_pid),
            'InvocationID': self.parent.supervisor_invocation_id}
        payload = {'schema_version': 1, 'generation': 2, 'action': 'bind_candidate'}
        source = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
        stdout = io.StringIO()
        with patch.object(workers, 'ROOT', self.root), patch.object(workers, 'inspect_installation',
                return_value=self.layout.descriptor), patch.object(workers.sys, 'stdin', source), patch(
                'lifecycle.control.privileged.unit_snapshot', return_value=unit), patch(
                'lifecycle.control.rpc.request', return_value={'stopped': True}) as request, redirect_stdout(stdout):
            self.assertEqual(workers.main(['update-runtime', self.operation], bundle=self.root, manifest={},
                manifest_sha256=self.parent.execution.control_manifest_sha256), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {'bound': True, 'target': 'candidate', 'generation': 2})
        runtime_links.verify_links(self.root, self.candidate)
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertEqual(call.kwargs['expected_pid'], self.parent.supervisor_pid)
            self.assertEqual(call.kwargs['invocation'], self.parent.supervisor_invocation_id)
            self.assertEqual(call.kwargs['action'], 'update_assert_stopped')
            self.assertEqual(call.kwargs['arguments'], {'operation_id': self.operation, 'generation': 2})
        before = self.links(), self.receipt.read_bytes()
        source.buffer = io.BytesIO(json.dumps({**payload, 'action': {}}).encode())
        with patch.object(workers, 'ROOT', self.root), patch.object(workers, 'inspect_installation',
                return_value=self.layout.descriptor), patch.object(workers.sys, 'stdin', source):
            with self.assertRaisesRegex(ControlError, 'control_request_invalid'):
                workers.main(['update-runtime', self.operation], bundle=self.root, manifest={},
                    manifest_sha256=self.parent.execution.control_manifest_sha256)
        self.assertEqual((self.links(), self.receipt.read_bytes()), before)

    def test_exact_prepared_pair_can_activate_then_restore_before_acceptance_under_same_owner(self):
        self.bind('candidate')
        runtime_links.verify_links(self.root, self.candidate)
        payload = update_controller.read_update_journal(self.path)
        payload.update(phase='rolling_back', failed_target=payload['target_commit'])
        self.path.write_text(json.dumps(payload))
        self.bind('previous')
        runtime_links.verify_links(self.root, self.parent.execution)
        self.assertEqual(len(self.stopped), 4)
        self.assertEqual(state.inspect_state(self.root).generation, 2)
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(json.loads(self.receipt.read_text())['candidate'], execution_tuple_sha256(self.parent.execution))

    def test_existing_engine_rollback_permission_requires_exact_unaccepted_stopped_parent(self):
        original = self.path.read_bytes()
        with patch.object(managed_update, 'supervisor_command', return_value={'stopped': True}) as command, patch(
                'lifecycle.update_checkpoint.require_checkpoint'):
            with self.assertRaises(ControlError):managed_update.runtime_action(self.root, self.proof(), 'assert_rollback')
            command.assert_not_called()
            payload = json.loads(original);payload.update(phase='rolling_back', failed_target=payload['target_commit'])
            self.path.write_text(json.dumps(payload));before = self.path.read_bytes()
            self.assertEqual(managed_update.runtime_action(self.root, self.proof(), 'assert_rollback'), {'rollback_allowed': True})
            self.assertEqual(self.path.read_bytes(), before)
            command.return_value = {'stopped': False}
            with self.assertRaises(ControlError):managed_update.runtime_action(self.root, self.proof(), 'assert_rollback')
            self.assertEqual(self.path.read_bytes(), before)

    def test_failed_guarded_candidate_can_restore_original_pair_before_acceptance(self):
        self.bind('candidate')
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='exclusive', next_phase='verifying')
        payload = update_controller.read_update_journal(self.path)
        payload.update(phase='rolling_back', lifecycle_phase='verifying', failed_target=payload['target_commit'])
        self.path.write_text(json.dumps(payload))
        self.bind('previous')
        runtime_links.verify_links(self.root, self.parent.execution)
        self.assertFalse(update_controller.read_update_journal(self.path)['runtime_committed'])
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.VERIFYING)

    def test_accepted_candidate_cannot_select_previous_or_change_existing_link_evidence(self):
        self.bind('candidate')
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='exclusive', next_phase='verifying')
        payload = update_controller.read_update_journal(self.path)
        payload.update(phase='recovering', lifecycle_phase='verifying', runtime_committed=True,
            runtime_committed_at=1, current_commit=self.candidate.application_commit,
            managed_verification_execution_sha256=execution_tuple_sha256(self.candidate),
            managed_accepted_execution_sha256=execution_tuple_sha256(self.candidate))
        self.path.write_text(json.dumps(payload))
        before = self.links(), self.receipt.read_bytes()
        with self.assertRaises(RuntimeBuildError):self.bind('previous')
        self.assertEqual((self.links(), self.receipt.read_bytes()), before)
        self.bind('candidate');runtime_links.verify_links(self.root, self.candidate)

    def test_interrupted_pair_retains_exact_applying_intent_and_same_owner_can_finish_it(self):
        original = os.replace
        def interrupt(source, destination):
            if destination == self.web/'dist':raise OSError('synthetic second-link interruption')
            return original(source, destination)
        with patch.object(runtime_links.os, 'replace', side_effect=interrupt):
            with self.assertRaises(OSError):self.bind('candidate')
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt['stage'], 'applying')
        self.assertEqual(receipt['operation_id'], self.operation)
        self.assertEqual(receipt['generation'], 2)
        self.assertEqual(self.links(), (self.candidate.web_dependency_target, self.parent.execution.web_dist_target))
        self.bind('candidate');runtime_links.verify_links(self.root, self.candidate)
        self.assertEqual(json.loads(self.receipt.read_text())['stage'], 'complete')

    def test_foreign_pair_and_aliased_source_are_refused_without_replacing_preserved_links(self):
        (self.web/'dist').unlink();(self.web/'dist').symlink_to('/unrelated/assets')
        before = self.links(), self.receipt.read_bytes()
        with self.assertRaises(RuntimeBuildError):self.bind('candidate')
        self.assertEqual((self.links(), self.receipt.read_bytes()), before)
        preserved = self.root/'preserved-web'
        self.web.rename(preserved);self.web.symlink_to(preserved, target_is_directory=True)
        with self.assertRaises(RuntimeBuildError):self.bind('candidate')
        self.assertEqual((self.links(), self.receipt.read_bytes()), before)

    def test_replaced_real_web_directory_after_verification_cannot_receive_bindings(self):
        before = self.links(), self.receipt.read_bytes()
        preserved = self.root/'original-web'
        calls = []
        def stopped():
            calls.append(True)
            if len(calls) == 1:
                self.web.rename(preserved);self.web.mkdir(mode=0o700)
                for name, target in zip(('node_modules', 'dist'), before[0]):
                    (self.web/name).symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeBuildError, 'changed before publication'):
            runtime_links.bind_update_runtime(self.root, proof=self.proof(), target='candidate',
                assert_children_stopped=stopped)
        self.assertEqual((self.links(), self.receipt.read_bytes()), before)
        self.assertEqual(tuple(os.readlink(preserved/name) for name in ('node_modules', 'dist')), before[0])

    def test_lost_quiescence_before_publication_preserves_both_links_and_receipt(self):
        before = self.links(), self.receipt.read_bytes()
        calls = []
        def stopped():
            calls.append(True)
            if len(calls) == 2:raise RuntimeError('quiescence lost')
        with self.assertRaisesRegex(RuntimeError, 'quiescence lost'):
            runtime_links.bind_update_runtime(self.root, proof=self.proof(), target='candidate',
                assert_children_stopped=stopped)
        self.assertEqual((self.links(), self.receipt.read_bytes()), before)


if __name__ == '__main__':unittest.main()
