import json
import os
import select
import subprocess
import sys
import unittest
from unittest.mock import Mock

from lifecycle import managed_restore,restore_controller as restore,state
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_parent_recovery as fixtures


class DeadParentControllerTests(unittest.TestCase):
    def setUp(self):
        fixtures.ParentRecoveryReviewTests.setUp(self)
        restore.resume_restore_journal_for_operator(self.root,self.operation,generation=2,action='continue_recovery')
        state.transition_operation_with_credential(self.root,operation_id=self.operation,generation=2,
            expected_phase='recovery_required',next_phase='exclusive')
        self.authorize=Mock(side_effect=lambda operation,generation:(
            restore.inspect_restore_journal(self.root,operation),self.bound))

    def actor(self):
        child=subprocess.Popen([sys.executable,'-c',
            "import json,sys;from lifecycle import state;v=json.loads(sys.stdin.readline());"
            "lease=state.acquire_controller_lease(v['root'],operation_id=v['operation'],generation=2,"
            "expected_phase='exclusive',owner_capability=v['capability']);"
            "print('owned',flush=True);sys.stdin.read(1);lease.release()"],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:child.terminate()
            child.wait(timeout=5)
            child.stdin.close();child.stdout.close();child.stderr.close()
        self.addCleanup(cleanup)
        capability=state.load_controller_capability(self.root,operation_id=self.operation,generation=2)
        child.stdin.write((json.dumps({'root':str(self.root),'operation':self.operation,'capability':capability})+'\n').encode())
        child.stdin.flush()
        self.assertTrue(select.select([child.stdout],[],[],5)[0]);self.assertEqual(child.stdout.readline(),b'owned\n')
        return child

    def test_living_lock_owner_is_never_fenced_and_actual_dead_owner_only_marks_recovery(self):
        actor=self.actor();before=state.inspect_state(self.root)
        self.assertFalse(managed_restore.fence_dead_controller(self.root,authorize=self.authorize))
        self.authorize.assert_not_called();self.assertEqual(state.inspect_state(self.root),before)
        actor.terminate();actor.wait(timeout=5)
        capability=state.load_controller_capability(self.root,operation_id=self.operation,generation=2)
        self.assertTrue(managed_restore.fence_dead_controller(self.root,authorize=self.authorize))
        after=state.inspect_state(self.root)
        self.assertEqual(after.generation,2);self.assertEqual(after.active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        journal=restore.inspect_restore_journal(self.root,self.operation)
        self.assertEqual(journal['error_code'],'restore_controller_interrupted');self.assertFalse(journal['accepted'])
        self.assertEqual(journal['managed_execution'],self.bound.payload())
        self.assertEqual(state.load_controller_capability(self.root,operation_id=self.operation,generation=2),capability)
        self.assertFalse(managed_restore.fence_dead_controller(self.root,authorize=self.authorize))

    def test_living_lease_without_lock_and_ambiguous_binding_are_not_dead_proof(self):
        # The real earlier lease belongs to this still-living test process.
        self.assertFalse(managed_restore.fence_dead_controller(self.root,authorize=self.authorize))
        self.authorize.assert_not_called()
        actor=self.actor();actor.terminate();actor.wait(timeout=5)
        self.authorize.side_effect=ControlError('control_parent_binding_changed')
        before=state.inspect_state(self.root)
        self.assertFalse(managed_restore.fence_dead_controller(self.root,authorize=self.authorize))
        self.assertEqual(state.inspect_state(self.root),before)


if __name__=='__main__':unittest.main()
