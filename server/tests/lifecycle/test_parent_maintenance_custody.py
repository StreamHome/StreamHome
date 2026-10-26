"""Exact repair reuses original restore custody without any database resume."""
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_parent_repair, bound_runtime_repair, parent_recovery, restore_controller as restore, state
from lifecycle.control import parent_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_bound_parent_repair as fixtures


class ParentMaintenanceCustodyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.BoundParentRepairPlanTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def execute(self,*,accepted=False,fail=False):
        f=self.fixture
        if accepted:f.accept_parent()
        reviewed=f.plan()
        journal=restore.inspect_restore_journal(f.root,f.operation)
        database=f.database.read_bytes()
        scope={'unit':'streamhome-install-'+f.bound.installation_id+'-'+uuid.uuid4().hex+'.scope',
            'invocation':'a'*32,'pid':4321,'start_identity':'123'}
        with patch.object(parent_grants,'require_scope'),patch.object(parent_grants,'read_stop_epoch',return_value='b'*64):
            grant=bound_parent_repair.prepare(f.root,{'operation_id':f.operation,
                'expected_evidence_sha256':reviewed['evidence_sha256'],'recovery_review_id':str(uuid.uuid4()),
                'issuer_scope':scope,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'stop_epoch':'b'*64},control_manifest_sha256=f.bound.execution.control_manifest_sha256)
        capability=state.load_controller_capability(f.root,operation_id=f.operation,generation=2)
        transfers=[]
        def maintenance(lease,generation,current):
            lease.require_current()
            self.assertEqual(generation,3);self.assertEqual(current['accepted'],accepted)
            self.assertEqual(state.inspect_state(f.root).active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
            if fail:raise ControlError('control_operator_stopped')
            bound_runtime_repair.reconstruct(f.root,f.layout.cache/'runtime-recovery',f.bound.execution,
                expected=reviewed['artifacts'],receipt=f.paths.operation_dir/'environment-repair-3.json',
                checkpoint=lease.require_current,quiescent=lambda:None)
        with patch.object(parent_grants,'read_grant',return_value=grant),patch.object(
            parent_recovery,'bound_journal',return_value=(journal,f.bound)),patch.object(
            restore,'resume_restore_journal_for_operator') as resume,patch.object(
            restore,'_start_backend') as backend,patch.object(restore,'_web_port') as web:
            arguments=dict(acknowledgement_fd=None,gate_fd=None,operator_action='repair_bound_environment',
                pre_transfer_check=lambda:None,on_transfer=transfers.append,maintenance=maintenance)
            if fail:
                with self.assertRaisesRegex(ControlError,'control_operator_stopped'):
                    restore._run_controller(f.root,f.operation,2,capability,**arguments)
            else:self.assertEqual(restore._run_controller(f.root,f.operation,2,capability,**arguments),0)
            resume.assert_not_called();backend.assert_not_called();web.assert_not_called()
        self.assertEqual(transfers,[3]);self.assertEqual(f.database.read_bytes(),database)
        authority=state.inspect_state(f.root)
        self.assertEqual(authority.generation,3)
        self.assertEqual(authority.active_operation.operation_id,f.operation)
        self.assertEqual(authority.active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        after=restore.inspect_restore_journal(f.root,f.operation)
        self.assertEqual(after['accepted'],accepted)
        self.assertEqual(after['managed_execution'],journal['managed_execution'])
        self.assertEqual(after['activation_state'],journal['activation_state'])
        if not fail:
            self.assertEqual((f.fixture.trees['python']/'bin/program').read_bytes(),b'exact immutable bytes')
            preserved=list(f.fixture.trees['python'].parent.glob('.repair-*'))
            self.assertEqual(len(preserved),1)
            self.assertEqual((preserved[0]/'bin/program').read_bytes(),b'broken original environment')

    def test_unaccepted_parent_repairs_under_real_transferred_lease_and_remains_unresolved(self):
        self.execute()

    def test_actual_accepted_parent_database_and_original_binding_survive_real_reconstruction(self):
        self.execute(accepted=True)

    def test_canceled_maintenance_keeps_new_parent_generation_and_acceptance(self):
        self.execute(accepted=True,fail=True)


if __name__=='__main__':unittest.main()
