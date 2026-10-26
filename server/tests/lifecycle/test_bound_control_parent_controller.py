"""Real original-owner transfers; explicit artifact/root-effect/OS substitutes."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_control_parent_repair as repair, bound_control_parent_review as review
from lifecycle import state,restore_controller,update_controller
from lifecycle.control import entry,parent_grants,update_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_bound_control_parent_review as fixtures


class ParentControlControllerTests(unittest.TestCase):
    def setup_owner(self,kind):
        case=fixtures.ParentControlReviewTests();case.setUp();self.addCleanup(case.doCleanups)
        fixture=case.fixture(kind);self.case=case;self.fixture=fixture
        self.root=case.root;self.operation=case.operation;self.execution=case.execution
        self.kind=kind;self.grants=update_grants if kind=='update' else parent_grants
        if kind=='update':self.enterContext(patch.object(os,'getpid',return_value=fixture.canonical.main))
        self.enterContext(patch.object(repair,'layout_for_root',return_value=case.layout))
        self.enterContext(patch.object(repair,'require_stopped'))
        self.enterContext(patch.object(repair,'require_service_quiescent'))
        self.enterContext(patch.object(repair,'controller_peer'))
        self.enterContext(patch('lifecycle.control.processes.controller_peer'))
        self.enterContext(patch.dict(os.environ,INVOCATION_ID='a'*32))
        self.enterContext(patch('lifecycle.control.administration.operation_unit',return_value={
            'ActiveState':'active','MainPID':str(os.getpid()),'InvocationID':'a'*32}))
        self.enterContext(patch.object(self.grants,'require_scope'))
        self.enterContext(patch.object(self.grants,'require_live_identity'))
        self.enterContext(patch.object(self.grants,'read_stop_epoch',return_value='e'*64))
        physical=entry.CONTROL/'rescue-versions'/self.execution.control_bundle_id/'lib/lifecycle/bound_control_parent_repair.py'
        self.enterContext(patch.object(repair,'__file__',str(physical)))
        self.enterContext(patch.object(entry,'verify_bundle',return_value=(physical.parent,{},self.execution.control_manifest_sha256)))
        self.issued={}
        self.enterContext(patch.object(self.grants,'read_grant',side_effect=lambda op,generation,**kw:self.issued[generation]))
        self.issue()

    def issue(self):
        view=self.case.plan()
        scope={'unit':'streamhome-install-'+view['installation_id']+'-'+uuid.uuid4().hex+'.scope',
            'invocation':'a'*32,'pid':4321,'start_identity':'123'}
        self.grant=review.prepare(self.root,{'operation_id':self.operation,'artifact':self.case.artifact,
            'expected_evidence_sha256':view['evidence_sha256'],'recovery_review_id':str(uuid.uuid4()),
            'issuer_scope':scope,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'stop_epoch':'e'*64},
            control_manifest_sha256=self.execution.control_manifest_sha256)
        self.issued[self.grant['generation']+1]=self.grant

    def controller(self):
        actor=repair.BoundUpdateControlController if self.kind=='update' else repair.BoundRestoreControlController
        return actor(self.root,self.operation,control_manifest_sha256=self.execution.control_manifest_sha256)

    def journal(self):
        if self.kind=='restore':return restore_controller.inspect_restore_journal(self.root,self.operation)
        return update_controller.read_update_journal(self.fixture.canonical.path)

    def effect(self,path):
        intent=repair._intent(self.grant)
        observation=repair.observe(self.root,{'operation_id':self.operation,'controller_pid':os.getpid(),
            'controller_invocation_id':'a'*32},control_manifest_sha256=self.execution.control_manifest_sha256)
        self.assertEqual(observation['intent'],intent)
        return {'schema_version':1,'intent_sha256':repair.digest(intent),'effect':{
            'stage':'complete','selection':intent['selection'],'artifact_sha256':intent['artifact']['artifact_sha256'],
            'filesystem_sha256':intent['filesystem_sha256']}}

    def exercise(self,kind):
        self.setup_owner(kind)
        database=self.fixture.canonical.database if kind=='update' else self.fixture.database
        before=database.read_bytes();original=self.journal();generation=self.grant['generation']
        with patch.object(repair,'read_protected_json',side_effect=self.effect), \
                patch.object(update_controller,'_materialize_target_controller',side_effect=AssertionError('No update engine')):
            self.assertEqual(self.controller().execute(),0)
        current=state.inspect_state(self.root)
        self.assertEqual(current.generation,generation+1)
        self.assertEqual(current.active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(current.active_operation.operation_id,self.operation)
        after=self.journal()
        mutable={'generation','updated_at'} if kind=='restore' else {'lifecycle_generation','updated_at'}
        self.assertEqual({k:v for k,v in original.items() if k not in mutable},
            {k:v for k,v in after.items() if k not in mutable})
        self.assertEqual(database.read_bytes(),before)
        with patch.object(repair,'read_protected_json',return_value={'schema_version':1,
                'intent_sha256':repair.digest(repair._intent(self.grant)),'effect':{
                    'stage':'complete','selection':self.grant['control_selection'],
                    'artifact_sha256':self.grant['control_artifact']['artifact_sha256'],
                    'filesystem_sha256':self.grant['filesystem_sha256']}}):
            self.assertTrue(repair.result(self.root,self.operation,
                control_manifest_sha256=self.execution.control_manifest_sha256)['completed'])

    def test_update_rotates_original_owner_without_engine_data_acceptance_or_runtime_effects(self):self.exercise('update')

    def test_restore_rotates_original_owner_without_database_activation_or_runtime_effects(self):self.exercise('restore')

    def test_wrong_physical_reader_is_refused_before_transfer(self):
        self.setup_owner('update');before=state.inspect_state(self.root)
        with patch.object(repair,'__file__','/unqualified/normal.py'), \
                self.assertRaisesRegex(ControlError,'control_reconstruction_independent_reader_required'):
            self.controller()
        self.assertEqual(state.inspect_state(self.root),before)

    def test_accepted_restore_keeps_database_and_irreversible_acceptance_after_control_repair(self):
        self.setup_owner('restore');self.fixture.accept_parent();self.issue()
        before=self.fixture.database.read_bytes()
        with patch.object(repair,'read_protected_json',side_effect=self.effect):
            self.assertEqual(self.controller().execute(),0)
        self.assertTrue(self.journal()['accepted']);self.assertEqual(self.fixture.database.read_bytes(),before)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)

    def test_update_control_permission_cannot_authorize_update_shell_progress(self):
        self.setup_owner('update')
        with patch.object(repair,'read_protected_json',side_effect=self.effect):
            self.assertEqual(self.controller().execute(),0)
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._require_managed_progress_permission(self.root,self.journal())

    def test_interrupted_root_effect_keeps_original_owner_unresolved_and_needs_fresh_permission(self):
        self.setup_owner('update');database=self.fixture.canonical.database.read_bytes();original=self.journal()
        with patch.object(repair,'read_protected_json',side_effect=ControlError('control_operator_stopped')), \
                self.assertRaisesRegex(ControlError,'control_operator_stopped'):
            self.controller().execute()
        self.assertEqual(state.inspect_state(self.root).generation,3)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(self.fixture.canonical.database.read_bytes(),database)
        self.assertEqual(self.journal()['managed_accepted_execution_sha256'],original['managed_accepted_execution_sha256'])
        with self.assertRaises(ControlError):
            repair.result(self.root,self.operation,control_manifest_sha256=self.execution.control_manifest_sha256)
        retained=repair.update_repair_intents.path(self.root,self.grant).read_bytes()
        self.issue()
        with patch.object(repair,'read_protected_json',side_effect=self.effect):
            self.assertEqual(self.controller().execute(),0)
        self.assertEqual(state.inspect_state(self.root).generation,4)
        self.assertEqual(self.fixture.canonical.database.read_bytes(),database)
        old=list(repair.update_repair_intents.directory(self.root,self.operation).glob('intent-3-*.json'))
        self.assertEqual(len(old),1);self.assertEqual(old[0].read_bytes(),retained)


if __name__=='__main__':unittest.main()
