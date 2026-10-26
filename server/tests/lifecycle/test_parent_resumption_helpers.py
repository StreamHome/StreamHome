"""Existing canonical parent ownership gates offline helpers and resumed RPCs."""
import os
import time
import unittest
from unittest.mock import patch

from lifecycle import managed_restore
from lifecycle.control import parent_resumption_runtime, supervisor
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_parent_resumption_runtime as fixtures


class ParentResumptionHelperTests(unittest.TestCase):
    def setUp(self):
        fixtures.ParentRuntimeEnrollmentTests.setUp(self)
        self.enterContext(patch.object(managed_restore,'bound_journal',return_value=(self.journal,self.bound)))
        self.enterContext(patch.object(managed_restore,'layout_for_root',return_value=self.layout))
        self.enterContext(patch('lifecycle.control.privileged.unit_snapshot',self.unit))
        self.stopped=self.enterContext(patch('lifecycle.control.stopped_repair.require_stopped'))
        self.quiet=self.enterContext(patch('lifecycle.control.processes.require_service_quiescent'))

    def test_offline_helper_requires_live_plan_and_exact_current_controller_without_launch(self):
        self.unit.return_value={'ActiveState':'inactive','MainPID':'0','InvocationID':''}
        self.assertTrue(managed_restore.resumption_offline(self.root))
        self.stopped.assert_called_once()
        self.quiet.assert_called_once_with(inspections=True,settle_timeout=2,controller=(
            self.operation,os.getpid(),self.lease.lease.process_start_identity))
        self.grant_reader.side_effect=ControlError('control_parent_recovery_permission_expired')
        with self.assertRaises(ControlError):managed_restore.resumption_offline(self.root)

    def test_same_supervisor_permission_cannot_bypass_stopped_runtime_proof(self):
        self.grant_reader.return_value={**self.grant,'schema_version':1}
        self.grant_reader.return_value.pop('resumption_plan_id')
        self.assertFalse(managed_restore.resumption_offline(self.root))
        self.stopped.assert_not_called();self.quiet.assert_not_called()

    def test_replacement_endpoint_requires_bound_current_unit_and_keeps_original_journal(self):
        before=dict(self.journal)
        self.unit.return_value['ActiveState']='active'
        identity=managed_restore.supervisor_identity(self.root,self.bound)
        self.assertEqual(identity,(os.getpid(),self.invocation))
        self.assertEqual(self.journal,before)
        self.unit.return_value['InvocationID']='d'*32
        self.ticket.side_effect=ControlError('control_resumption_invocation_changed')
        with self.assertRaises(ControlError):managed_restore.supervisor_identity(self.root,self.bound)

    def test_controller_wait_observes_only_root_start_and_cannot_outlive_permission(self):
        self.unit.return_value['ActiveState']='active'
        managed_restore.wait_for_resumed_backend(self.root)
        self.unit.return_value={'ActiveState':'inactive','MainPID':'0','InvocationID':''}
        with patch.object(time, 'sleep',side_effect=lambda delay:setattr(self.grant_reader,'side_effect',ControlError('expired'))):
            with self.assertRaisesRegex(ControlError,'expired'):managed_restore.wait_for_resumed_backend(self.root)

    def test_supervisor_enrollment_preserves_original_history_and_revalidates_permission(self):
        actor=supervisor.Supervisor.__new__(supervisor.Supervisor)
        actor.launches_open=True;actor.installation=self.bound.installation_id
        actor.invocation=self.invocation;actor.execution=self.bound.execution
        actor.resumption_binding=parent_resumption_runtime.require_binding(self.root,self.journal,
            self.invocation,control_manifest_sha256=self.bound.execution.control_manifest_sha256,own_supervisor=True)
        with patch.object(supervisor,'ROOT',self.root),patch.object(supervisor,'read_active_execution',
                parent_resumption_runtime.read_active_execution):
            journal,bound=actor._parent_journal(self.operation,3)
            self.assertEqual(bound,self.bound);self.assertEqual(journal,self.journal)
            self.grant_reader.side_effect=ControlError('expired')
            with self.assertRaises(ControlError):actor._parent_journal(self.operation,3)


if __name__=='__main__':unittest.main()
