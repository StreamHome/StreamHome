import os
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch
import uuid

from lifecycle import restore_controller as restore,state
from lifecycle.control import parent_grants, parent_resumption_runtime as runtime,resumption_contract as contract
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import execution_tuple_sha256
from tests.lifecycle import test_managed_restore as fixtures,test_restore_controller as database_fixture
from tests.lifecycle.test_resumption_contract import plan as make_plan


class ParentRuntimeEnrollmentTests(unittest.TestCase):
    def setUp(self):
        fixtures.ManagedRestoreJournalTests.setUp(self)
        self.operation_id=self.operation;self.paths=restore.restore_paths(self.root,self.operation)
        self.paths.backup_dir.mkdir(mode=0o700,parents=True)
        self.database=self.paths.database_path;self.backup=self.paths.backup_dir/self.candidate.filename
        database_fixture.create_database(self.database,'original')
        database_fixture.create_database(self.backup,'candidate')
        self.candidate=restore.prevalidate_candidate(self.root,self.backup.name)
        fixtures.ManagedRestoreJournalTests.create(self)
        first,transfer=state.acquire_transferred_controller_lease(self.root,operation_id=self.operation,generation=1,
            expected_phase='reserved',owner_capability=self.claim.owner_capability,next_owner_kind='restore_controller')
        restore.transfer_restore_journal_generation(self.root,self.operation,expected_generation=1,next_generation=2)
        self.generation=2
        self._prepare_artifacts=lambda:database_fixture.RestoreControllerTests._prepare_artifacts(self)
        database_fixture.RestoreControllerTests._accept_candidate(self)
        restore._mark_recovery_required(self.paths,2,'fixture_interruption');first.release()
        self.lease,transfer=state.acquire_transferred_controller_lease(self.root,operation_id=self.operation,generation=2,
            expected_phase='recovery_required',owner_capability=transfer.owner_capability,next_owner_kind='restore_controller')
        self.addCleanup(self.lease.release)
        restore.transfer_restore_journal_generation(self.root,self.operation,expected_generation=2,next_generation=3)
        restore.resume_restore_journal_for_operator(self.root,self.operation,generation=3,action='continue_recovery')
        for before,after in (('recovery_required','exclusive'),('exclusive','verifying')):
            state.transition_operation_with_credential(self.root,operation_id=self.operation,generation=3,
                expected_phase=before,next_phase=after)
        self.journal=restore.update_restore_journal(self.root,self.operation,progress='normal_recovery')
        self.invocation='c'*32;execution_sha=execution_tuple_sha256(self.bound.execution)
        self.plan={**make_plan(),'schema_version':2,'owner_type':'restore','action':'restore_recovery',
            'recovery_action':'continue_recovery','parent_binding_sha256':parent_grants.binding_sha256(self.bound),
            'installation_id':self.bound.installation_id,'operation_id':self.operation,'generation':3,
            'control_manifest_sha256':self.bound.execution.control_manifest_sha256,
            'stages':[{'kind':'candidate_start','execution_sha256':execution_sha,'generation':3,'handoff_sha256':None}]}
        receipt=contract.bind_invocation(self.plan,contract.consume(self.plan,self.plan['stages'][0],'1'*64,existing=None),self.invocation)
        scope={'unit':'streamhome-install-'+self.bound.installation_id+'-'+uuid.uuid4().hex+'.scope',
            'invocation':'f'*32,'pid':4321,'start_identity':'123'}
        self.grant={'schema_version':2,'resumption_plan_id':self.plan['plan_id'],'review_id':self.plan['review_id'],
            'installation_id':self.bound.installation_id,'operation_id':self.operation,'operation_type':'restore',
            'generation':2,'execution_sha256':execution_sha,'parent_binding_sha256':parent_grants.binding_sha256(self.bound),
            'control_manifest_sha256':self.bound.execution.control_manifest_sha256,'source_evidence_sha256':'f'*64,
            'canonical_evidence_identity':'plan-v1:'+'1'*32,'action':'continue_recovery','issuer_scope':scope,
            'boot_id':self.bound.boot_id,'stop_epoch':self.bound.stop_epoch,'expires_at_monotonic':time.monotonic()+900}
        self.enterContext(patch.object(runtime,'layout_for_root',return_value=self.layout))
        self.grant_reader=self.enterContext(patch.object(parent_grants,'read_grant',return_value=self.grant))
        self.ticket=self.enterContext(patch.object(runtime,'bound_ticket',return_value={'plan':self.plan,'receipt':receipt}))
        self.enterContext(patch.object(runtime,'controller_peer'))
        self.enterContext(patch.object(runtime,'process_cgroup',return_value=runtime.RUNTIME_CGROUP))
        self.unit=self.enterContext(patch.object(runtime,'unit_snapshot',return_value={
            'ActiveState':'activating','MainPID':str(os.getpid()),'InvocationID':self.invocation}))
        self.enterContext(patch.object(runtime,'read_active_execution',return_value=SimpleNamespace(
            installation_id=self.bound.installation_id,generation=0,operation_id=str(uuid.uuid4()),execution=self.bound.execution)))

    def bind(self):
        return runtime.require_binding(self.root,self.journal,self.invocation,
            control_manifest_sha256=self.bound.execution.control_manifest_sha256,own_supervisor=True)

    def complete(self):
        state.complete_operation_with_credential(self.root,operation_id=self.operation,generation=3,expected_phase='verifying')
        restore.update_restore_journal(self.root,self.operation,progress='succeeded',outcome='succeeded')

    def test_actual_parent_journal_and_acceptance_are_not_rewritten_for_replacement_supervisor(self):
        before=restore.inspect_restore_journal(self.root,self.operation)
        bound=self.bind();self.assertTrue(bound.require_current(self.root))
        self.assertEqual(restore.inspect_restore_journal(self.root,self.operation),before)
        self.assertNotEqual(bound.invocation,self.bound.supervisor_invocation_id)
        self.assertEqual(bound.parent_binding_sha256,parent_grants.binding_sha256(self.bound))

    def test_changed_permission_or_wrong_invocation_cannot_enroll_or_preserve_unresolved_runtime(self):
        binding=self.bind();self.grant_reader.side_effect=ControlError('control_resumption_issuer_changed')
        with self.assertRaises(ControlError):binding.require_current(self.root)
        self.grant_reader.side_effect=None
        self.unit.return_value['InvocationID']='d'*32
        with self.assertRaises(ControlError):self.bind()

    def test_exact_completed_ram_identity_survives_issuer_exit_without_blessing_another_process(self):
        binding=self.bind();self.complete()
        self.grant_reader.side_effect=ControlError('control_resumption_issuer_changed')
        self.assertFalse(binding.require_current(self.root))
        with patch.object(runtime.os,'getpid',return_value=binding.pid+1):
            with self.assertRaises(ControlError):binding.require_current(self.root)

    def test_completion_racing_live_controller_check_reconciles_only_exact_terminal_parent(self):
        binding=self.bind()
        def complete(*args,**kwargs):
            self.complete();raise ControlError('control_controller_changed')
        with patch.object(runtime,'require_binding',side_effect=complete):
            self.assertFalse(binding.require_current(self.root))

    def test_idle_without_terminal_acceptance_never_settles_a_different_runtime(self):
        binding=self.bind()
        with patch.object(runtime.state,'inspect_state',return_value=state.LifecycleState(2,3,None)):
            with self.assertRaises(ControlError):binding.require_current(self.root)


if __name__=='__main__':unittest.main()
