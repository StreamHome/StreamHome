"""Replacement UPDATE RAM binding with real journal/authority/retained completion."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from lifecycle import managed_update, state, update_controller, update_history
from lifecycle.control import update_grants, resumption_contract
from lifecycle.control import update_resumption_runtime as runtime
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_resumption_observation as observation_fixture


class UpdateResumptionRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture = observation_fixture.UpdateResumptionObservationTests();fixture.setUp();fixture.ready()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation
        self.canonical = fixture.fixture.fixture.fixture
        self.invocation = 'c'*32;self.main = fixture.main
        self.enterContext(patch.object(runtime, 'layout_for_root', return_value=self.canonical.layout))
        self.enterContext(patch.object(runtime, 'require_owner_peer', side_effect=lambda root, lease, pid:
            managed_update.bound_journal(root, lease.operation_id)))
        self.enterContext(patch.object(runtime, 'process_cgroup', return_value='/system.slice/streamhome.service'))
        self.enterContext(patch.object(runtime, 'process_identity', return_value=('123456', 'S')))
        self.unit = {'ActiveState': 'activating', 'MainPID': str(self.main), 'InvocationID': self.invocation}
        self.enterContext(patch.object(runtime, 'unit_snapshot', side_effect=lambda: self.unit))
        receipt = resumption_contract.consume(fixture.plan, fixture.plan['stages'][0], '1'*64, existing=None)
        receipt = resumption_contract.bind_invocation(fixture.plan, receipt, self.invocation)
        self.ticket = {'plan': fixture.plan, 'receipt': receipt}
        self.enterContext(patch.object(runtime, 'bound_ticket', side_effect=lambda *args, **kwargs: self.ticket))

    def binding(self):
        return runtime.require_binding(self.root, self.invocation,
            control_manifest_sha256=self.fixture.grant['control_manifest_sha256'])

    def test_binding_keeps_original_history_and_requires_current_grant_controller_and_invocation(self):
        before = self.canonical.path.read_bytes(), state.inspect_state(self.root)
        bound = self.binding()
        self.assertEqual(bound.execution_sha256, self.fixture.grant['accepted_execution_sha256'])
        with patch.object(managed_update.os, 'getpid', return_value=self.main):
            self.assertIs(bound.require_current(self.root), True)
        self.assertEqual((self.canonical.path.read_bytes(), state.inspect_state(self.root)), before)
        with patch.object(update_grants, 'require_scope', side_effect=ControlError('scope ended')):
            with self.assertRaises(ControlError):self.binding()
        self.unit['InvocationID'] = 'd'*32
        with self.assertRaises(ControlError):self.binding()

    def test_same_owner_progress_publication_is_reobserved_without_changing_ram_identity(self):
        expected = self.binding()
        original_state = state.inspect_state(self.root)
        changed = False
        def unit():
            nonlocal changed
            if not changed:
                journal = update_controller.read_update_journal(self.canonical.path)
                journal.update(phase='rolled_back', failed_target=journal['target_commit'])
                self.canonical.path.write_text(json.dumps(journal));changed = True
            return self.unit
        with patch.object(runtime, 'unit_snapshot', side_effect=unit) as observed:
            self.assertEqual(self.binding(), expected)
        self.assertEqual(observed.call_count, 2)
        self.assertEqual(state.inspect_state(self.root), original_state)

    def test_observation_retry_is_bounded_and_never_retries_os_identity_failure(self):
        with patch.object(runtime, '_binding_once', side_effect=ControlError('control_update_observation_changed')) as once:
            with self.assertRaisesRegex(ControlError, 'control_update_observation_changed'):self.binding()
            self.assertEqual(once.call_count, 8)
        with patch.object(runtime, '_binding_once', side_effect=ControlError('control_incarnation_changed')) as once:
            with self.assertRaisesRegex(ControlError, 'control_incarnation_changed'):self.binding()
            once.assert_called_once()

    def test_same_owner_progress_during_owner_projection_is_reobserved(self):
        expected = self.binding()
        changed = False
        def owner(root, lease, pid):
            nonlocal changed
            if not changed:
                journal = update_controller.read_update_journal(self.canonical.path)
                journal.update(phase='rolled_back', failed_target=journal['target_commit'])
                self.canonical.path.write_text(json.dumps(journal));changed = True
            return managed_update.bound_journal(root, lease.operation_id)
        with patch.object(runtime, 'require_owner_peer', side_effect=owner) as observed:
            self.assertEqual(self.binding(), expected)
        self.assertEqual(observed.call_count, 2)

    def test_consumed_or_foreign_ticket_cannot_enroll_or_fall_back_to_original_supervisor(self):
        # bound_ticket is the independent protected receipt/OS boundary.
        with patch.object(runtime, 'bound_ticket', side_effect=ControlError('not bound')):
            with self.assertRaises(ControlError):self.binding()
        with self.assertRaises(ControlError):runtime.require_binding(self.root,
            self.canonical.parent.supervisor_invocation_id,
            control_manifest_sha256=self.fixture.grant['control_manifest_sha256'])
        changed = {**self.fixture.plan, 'generation': 4}
        self.ticket = {**self.ticket, 'plan': changed}
        with self.assertRaises(ControlError):self.binding()

    def test_backend_preimport_uses_exact_new_os_invocation_and_parent_without_rewriting_original_history(self):
        before = self.canonical.path.read_bytes()
        environ = {'STREAMHOME_LIFECYCLE_OPERATION_ID': self.operation,
            'STREAMHOME_LIFECYCLE_GENERATION': '3', 'INVOCATION_ID': self.invocation}
        with (patch('lifecycle.control.privileged.unit_snapshot', return_value=self.unit),
                patch.object(managed_update.os, 'getppid', return_value=self.main)):
            self.assertEqual(managed_update.verification_execution(self.root, environ), self.fixture.fixture.bound.accepted)
            with self.assertRaises(ControlError):managed_update.verification_execution(self.root,
                {**environ, 'INVOCATION_ID': self.canonical.parent.supervisor_invocation_id})
        self.assertEqual(self.canonical.path.read_bytes(), before)

    def test_exact_retained_completion_settles_binding_after_scope_exit_and_global_status_rewrite(self):
        bound = self.binding()
        payload = update_controller.read_update_journal(self.canonical.path)
        payload.update(phase='rolled_back', finished_at=2, failed_target=payload['target_commit'])
        self.canonical.path.write_text(json.dumps(payload))
        self.fixture.fixture.fixture.write_active(replace(self.fixture.fixture.fixture.active(), generation=3))
        with patch.object(managed_update.os, 'getpid', return_value=self.main):
            update_history.retain_terminal(self.root, self.fixture.lease)
            state.complete_operation_with_credential(self.root, operation_id=self.operation, generation=3,
                expected_phase='verifying', expected_operation_type='update')
            self.fixture.lease.release()
            self.canonical.path.write_text(json.dumps({'phase': 'checking'}))
            with patch.object(update_grants, 'require_scope', side_effect=ControlError('scope ended')):
                self.assertIs(bound.require_current(self.root), False)
            path = update_history._path(self.root, self.operation, 3)
            payload = json.loads(path.read_bytes());payload['generation'] = 4
            path.write_text(json.dumps(payload))
            with self.assertRaises(ControlError):bound.require_current(self.root)

    def test_completion_during_live_binding_read_uses_exact_retained_history(self):
        bound = self.binding()
        payload = update_controller.read_update_journal(self.canonical.path)
        payload.update(phase='rolled_back', finished_at=2, failed_target=payload['target_commit'])
        self.canonical.path.write_text(json.dumps(payload))
        self.fixture.fixture.fixture.write_active(replace(self.fixture.fixture.fixture.active(), generation=3))
        def completed(*args, **kwargs):
            update_history.retain_terminal(self.root, self.fixture.lease)
            state.complete_operation_with_credential(self.root, operation_id=self.operation, generation=3,
                expected_phase='verifying', expected_operation_type='update')
            self.fixture.lease.release()
            raise update_controller.UpdateControllerConflict('completed during observation')
        with patch.object(managed_update.os, 'getpid', return_value=self.main), patch.object(
                runtime, 'require_binding', side_effect=completed):
            self.assertIs(bound.require_current(self.root), False)
