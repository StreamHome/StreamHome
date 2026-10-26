"""Actual updater lease death/lock fencing; original supervisor OS proof substituted."""
import json
import select
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from lifecycle import managed_update, state, update_controller, update_execution
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from tests.lifecycle.test_managed_update_preparation import ManagedUpdatePreparationFixture


class DeadUpdateControllerTests(ManagedUpdatePreparationFixture, unittest.TestCase):
    def phase_gap(self):
        payload=update_controller.read_update_journal(self.path)
        candidate=managed_update.prepared_execution(self.parent.execution,payload['target_commit'],self.built)
        state.transition_operation_with_credential(self.root,operation_id=self.operation,generation=2,
            expected_phase='reserved',next_phase='exclusive')
        payload.update(managed_candidate=execution_tuple_payload(candidate),phase='starting',lifecycle_phase='exclusive')
        self.path.write_text(json.dumps(payload))
        state.transition_operation_with_credential(self.root,operation_id=self.operation,generation=2,
            expected_phase='exclusive',next_phase='verifying')
        return payload

    def test_original_dead_owner_between_verifying_cas_and_journal_publication_is_fenced(self):
        actor=self.actor();original=self.phase_gap()
        with self.assertRaises(update_controller.UpdateControllerConflict):managed_update.bound_journal(self.root,self.operation)
        self.authorize.side_effect=lambda operation,generation:managed_update.read_fence_binding(self.root,operation)
        self.assertFalse(self.fence());self.authorize.assert_not_called()
        actor.terminate();actor.wait(timeout=5)
        capability=state.load_controller_capability(self.root,operation_id=self.operation,generation=2)
        self.assertTrue(self.fence())
        after=update_controller.read_update_journal(self.path)
        self.assertEqual(state.inspect_state(self.root).generation,2)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(state.load_controller_capability(self.root,operation_id=self.operation,generation=2),capability)
        self.assertEqual({k:v for k,v in original.items() if k not in {'lifecycle_phase','updated_at'}},
            {k:v for k,v in after.items() if k not in {'lifecycle_phase','updated_at'}})

    def setUp(self):
        super().setUp()
        self.lease.release()
        self.authorize = Mock(side_effect=lambda operation, generation:
            managed_update.bound_journal(self.root, operation))

    def actor(self):
        child = subprocess.Popen([sys.executable, '-c',
            "import json,sys;from lifecycle import state;v=json.loads(sys.stdin.readline());"
            "lease=state.acquire_controller_lease(v['root'],operation_id=v['operation'],generation=2,"
            "expected_phase='reserved',owner_capability=v['capability']);"
            "print('owned',flush=True);sys.stdin.read(1);lease.release()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:child.terminate()
            child.wait(timeout=5)
            child.stdin.close();child.stdout.close();child.stderr.close()
        self.addCleanup(cleanup)
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=2)
        child.stdin.write((json.dumps({'root': str(self.root), 'operation': self.operation,
            'capability': capability})+'\n').encode());child.stdin.flush()
        self.assertTrue(select.select([child.stdout], [], [], 5)[0])
        self.assertEqual(child.stdout.readline(), b'owned\n')
        return child

    def fence(self):return managed_update.fence_dead_controller(self.root, authorize=self.authorize)

    def accept(self):
        payload = update_controller.read_update_journal(self.path)
        bound = update_execution.parse(payload)
        candidate = managed_update.prepared_execution(bound.parent.execution, payload['target_commit'], self.built)
        for old, new in (('reserved', 'exclusive'), ('exclusive', 'verifying')):
            state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
                expected_phase=old, next_phase=new)
        payload.update(managed_candidate=execution_tuple_payload(candidate), phase='starting', lifecycle_phase='verifying',
            runtime_committed=True, runtime_committed_at=1, current_commit=candidate.application_commit,
            managed_verification_execution_sha256=execution_tuple_sha256(candidate),
            managed_accepted_execution_sha256=execution_tuple_sha256(candidate))
        self.path.write_text(json.dumps(payload))

    def test_actual_dead_owner_is_fenced_without_rotating_generation_capability_or_request(self):
        actor = self.actor();before = state.inspect_state(self.root), self.path.read_bytes()
        self.assertFalse(self.fence());self.authorize.assert_not_called()
        self.assertEqual((state.inspect_state(self.root), self.path.read_bytes()), before)
        actor.terminate();actor.wait(timeout=5)
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=2)
        original = update_controller.read_update_journal(self.path)
        self.assertTrue(self.fence())
        current = state.inspect_state(self.root)
        self.assertEqual(current.generation, 2)
        self.assertEqual(current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        changed = update_controller.read_update_journal(self.path)
        self.assertEqual({k: v for k, v in changed.items() if k != 'lifecycle_phase'},
            {k: v for k, v in original.items() if k != 'lifecycle_phase'})
        self.assertEqual(state.load_controller_capability(self.root, operation_id=self.operation, generation=2), capability)
        self.assertFalse(self.fence())

    def test_accepted_tuple_is_irreversible_when_actual_updater_dies(self):
        actor = self.actor();self.accept()
        accepted = update_execution.parse(update_controller.read_update_journal(self.path))
        actor.terminate();actor.wait(timeout=5)
        self.assertTrue(self.fence())
        after = update_execution.parse(update_controller.read_update_journal(self.path))
        self.assertEqual(after, accepted)

    def test_missing_or_ambiguous_ownership_never_authorizes_fencing(self):
        self.assertFalse(self.fence());self.authorize.assert_not_called()
        actor = self.actor();actor.terminate();actor.wait(timeout=5)
        self.authorize.side_effect = ControlError('control_parent_binding_changed')
        before = state.inspect_state(self.root), self.path.read_bytes()
        self.assertFalse(self.fence())
        self.assertEqual((state.inspect_state(self.root), self.path.read_bytes()), before)

    def test_failed_subordinate_publication_still_requires_supervisor_child_stop(self):
        actor = self.actor();actor.terminate();actor.wait(timeout=5)
        original = self.path.read_bytes()
        with patch.object(update_controller, '_private_atomic_write', side_effect=OSError('Injected journal failure')):
            self.assertTrue(self.fence())
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(self.path.read_bytes(), original)

    def test_concurrent_failed_owner_fence_does_not_escape_or_repeat_the_cas(self):
        actor = self.actor();actor.terminate();actor.wait(timeout=5)
        before = state.inspect_state(self.root), self.path.read_bytes()
        with patch.object(state, 'transition_operation_with_credential',
                side_effect=state.LifecycleConflictError('competing phase fence')) as transition:
            self.assertFalse(self.fence())
        transition.assert_called_once()
        self.assertEqual((state.inspect_state(self.root), self.path.read_bytes()), before)

    def test_caught_failed_owner_without_lease_requires_exact_rr_and_empty_inactive_fixed_unit(self):
        from lifecycle.control import administration, stop
        def quiescent():
            return managed_update.failed_controller_is_quiescent(self.root, authorize=self.authorize)
        actor = {'ActiveState': 'inactive', 'MainPID': '0'}
        with patch.object(administration, 'operation_unit', return_value=actor), patch.object(stop, '_require_empty') as empty:
            self.assertFalse(quiescent())  # A missing lease alone cannot fence authority.
            state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
                expected_phase='reserved', next_phase='recovery_required')
            update_controller.reconcile_operator_journal_recovery_required(self.root,
                operation_id=self.operation, generation=2, current_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
                pre_cutover_baseline_rejected=True)
            before = state.inspect_state(self.root), self.path.read_bytes()
            self.assertTrue(quiescent());empty.assert_called_once_with('streamhome-operation@'+self.operation+'.service')
            self.assertEqual((state.inspect_state(self.root), self.path.read_bytes()), before)
            actor['MainPID'] = '12345';self.assertFalse(quiescent())
            actor['MainPID'] = '0';actor['ActiveState'] = 'active';self.assertFalse(quiescent())
            actor['ActiveState'] = 'failed'
            with patch.object(stop, '_require_empty', side_effect=ControlError('descendant alive')):
                self.assertFalse(quiescent())
            self.authorize.side_effect = ControlError('wrong original supervisor')
            self.assertFalse(quiescent())

    def test_rr_with_living_updater_never_supplies_failed_owner_quiescence(self):
        actor = self.actor()
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='reserved', next_phase='recovery_required')
        update_controller.reconcile_operator_journal_recovery_required(self.root,
            operation_id=self.operation, generation=2, current_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
            pre_cutover_baseline_rejected=True)
        from lifecycle.control import administration, stop
        with (patch.object(administration, 'operation_unit', return_value={'ActiveState': 'inactive', 'MainPID': '0'}),
                patch.object(stop, '_require_empty')):
            self.assertFalse(managed_update.failed_controller_is_quiescent(self.root, authorize=self.authorize))
        actor.terminate();actor.wait(timeout=5)
