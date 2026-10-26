"""Read-only planning retains the actual original RESTORE and immutable cache."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_parent_repair as repair, layout, restore_controller as restore, runtime_build, state
from lifecycle.parent_execution import ParentExecution
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_runtime_recovery_cache as cache_fixture, test_restore_controller as database_fixture


class BoundParentRepairPlanTests(unittest.TestCase):
    def setUp(self):
        fixture=cache_fixture.BoundRecoveryCacheTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.root=fixture.root
        (self.root/'server').mkdir()
        for name in ('start.sh','update.sh'):(self.root/name).write_text('#!/bin/sh\n')
        asset=fixture.trees['web']/'assets.json';asset.write_bytes(b'{}\n');asset.chmod(0o600)
        fixture.manifest['web']=runtime_build.tree_inventory(fixture.trees['web'])
        fixture.manifest['source']={'server/requirements.lock':hashlib.sha256(b'locked input').hexdigest()}
        raw=runtime_build.canonical_bytes(fixture.manifest);(fixture.bundle/'manifest.json').write_bytes(raw)
        fixture.execution=replace(fixture.execution,runtime_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            web_asset_manifest_sha256=hashlib.sha256(asset.read_bytes()).hexdigest())
        installation=str(uuid.uuid4())
        self.layout=replace(layout.layout_for_root(self.root),cache=self.root/'cache',
            descriptor=SimpleNamespace(descriptor=SimpleNamespace(service_uid=os.geteuid(),installation_id=installation)))
        self.enterContext(patch.object(layout,'layout_for_root',return_value=self.layout))
        self.enterContext(patch.object(repair,'layout_for_root',return_value=self.layout))
        paths=state.lifecycle_paths(self.root);state._ensure_lifecycle_directories(paths)
        state._write_state_unlocked(paths,state.LifecycleState(2,0,None))
        claim=state.claim_operation_with_credential(self.root,operation_type='restore',owner_kind='restore_controller')
        self.operation=claim.state.active_operation.operation_id
        self.operation_id=self.operation;self.generation=2
        self.paths=restore.restore_paths(self.root,self.operation)
        self.database=self.paths.database_path
        database_fixture.create_database(self.database,'original')
        self.paths.backup_dir.mkdir(mode=0o700,parents=True)
        self.backup=self.paths.backup_dir/'backup_20260101_000000.db'
        database_fixture.create_database(self.backup,'candidate')
        self.candidate=restore.prevalidate_candidate(self.root,self.backup.name)
        self.bound=ParentExecution(installation,self.operation,'restore',1,fixture.execution,'a'*32,
            os.getpid(),state._process_start_identity(os.getpid()),str(uuid.uuid4()),'b'*64)
        with patch('lifecycle.parent_execution.capture',return_value=self.bound),patch(
            'lifecycle.control.reader_support.require_managed_restore_reader'):
            restore.create_restore_journal(self.root,operation_id=self.operation,generation=1,
                candidate=self.candidate)
        read,write=os.pipe();os.close(write)
        self.assertEqual(restore._run_controller(self.root,self.operation,1,claim.owner_capability,
            acknowledgement_fd=None,gate_fd=read),1)
        for name in ('python','node','web','bundles'):(fixture.runtime/name).chmod(0o700)
        self.layout.cache.mkdir(mode=0o700)
        fixture.cache=self.layout.cache/'runtime-recovery';fixture.capture()
        self.enterContext(patch.object(runtime_build,'source_inputs',return_value={'server/requirements.lock':b'locked input'}))
        self.reader=self.enterContext(patch('lifecycle.control.reader_support.require_parent_bound_repair_reader'))
        self.enterContext(patch.object(repair,'require_stopped'))
        self.quiet=self.enterContext(patch.object(repair,'require_service_quiescent'))
        self.enterContext(patch.object(repair,'read_active_execution',return_value=SimpleNamespace(
            installation_id=installation,execution=fixture.execution)))
        self.enterContext(patch.object(repair,'read_stop_epoch',return_value='b'*64))
        (fixture.trees['python']/'bin/program').write_bytes(b'broken original environment')

    def plan(self):return repair.plan(self.root,self.operation,
        control_manifest_sha256=self.bound.execution.control_manifest_sha256)

    def snapshot(self):return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_exact_corrupt_environment_review_does_not_repair_claim_or_touch_database(self):
        before=self.snapshot();first=self.plan();second=self.plan()
        self.assertEqual(first,second);self.assertEqual(self.snapshot(),before)
        self.assertEqual(first['generation'],2);self.assertFalse(first['accepted'])
        self.assertEqual(first['phase'],'recovery_required')
        self.assertEqual(first['permitted_actions'],['repair_bound_environment'])
        self.assertEqual(first['bound_manifest_sha256'],self.bound.execution.runtime_manifest_sha256)
        self.reader.assert_called_with(self.bound.execution)

    def accept_parent(self):
        # Prepare an actual checkpoint, candidate activation and accepted
        # canonical parent before observing an interrupted accepted operation.
        restore.resume_restore_journal_for_operator(self.root,self.operation,generation=2,action="continue_recovery")
        restore.update_restore_journal(self.root,self.operation,progress="runtime_stopped")
        journal=database_fixture.RestoreControllerTests._prepare_artifacts(self)
        restore.activate_staged_candidate(self.paths,journal)
        for before,after in (('recovery_required','exclusive'),('exclusive','verifying')):
            state.transition_operation_with_credential(self.root,operation_id=self.operation,generation=2,
                expected_phase=before,next_phase=after)
        restore.update_restore_journal(self.root,self.operation,progress='verifying_candidate')
        restore.update_restore_journal(self.root,self.operation,progress='accepted',accepted=True,outcome='accepted')
        restore._mark_recovery_required(self.paths,2,'fixture_interruption')

    def test_accepted_parent_and_original_binding_remain_immutable_in_review(self):
        self.accept_parent()
        before=self.snapshot();value=self.plan()
        self.assertTrue(value['accepted']);self.assertEqual(self.snapshot(),before)
        self.assertEqual(restore.inspect_restore_journal(self.root,self.operation)['managed_execution'],self.bound.payload())

    def test_missing_reader_permission_or_live_work_cannot_produce_a_repair_plan(self):
        before=self.snapshot()
        self.reader.side_effect=ControlError('control_bound_reader_incompatible')
        with self.assertRaises(ControlError):self.plan()
        self.reader.side_effect=None;self.quiet.side_effect=ControlError('control_foreign_service_process')
        with self.assertRaises(ControlError):self.plan()
        self.assertEqual(self.snapshot(),before)

    def test_finite_repair_permission_binds_exact_plan_journal_and_original_filesystem(self):
        from lifecycle.control import parent_grants
        current=self.plan();before=self.snapshot()
        scope={'unit':'streamhome-install-'+self.bound.installation_id+'-'+uuid.uuid4().hex+'.scope',
            'invocation':'a'*32,'pid':4321,'start_identity':'123'}
        payload={'operation_id':self.operation,'expected_evidence_sha256':current['evidence_sha256'],
            'recovery_review_id':str(uuid.uuid4()),'issuer_scope':scope,
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'stop_epoch':'b'*64}
        with patch.object(parent_grants,'require_scope'),patch.object(parent_grants,'read_stop_epoch',return_value='b'*64):
            value=repair.prepare(self.root,payload,control_manifest_sha256=self.bound.execution.control_manifest_sha256)
            self.assertEqual(parent_grants.parse_grant(value),value)
            parent_grants.require_binding(value,self.bound)
        self.assertEqual(value['schema_version'],3)
        self.assertNotIn('resumption_plan_id',value);self.assertNotIn('canonical_evidence_identity',value)
        self.assertEqual(value['source_journal_sha256'],current['source_journal_sha256'])
        self.assertEqual(value['filesystem_sha256'],current['artifacts']['filesystem_sha256'])
        self.assertEqual(self.snapshot(),before)
        for change in ({'action':'continue_recovery'},{'source_journal_generation':True},
                {'source_journal_generation':0},{'filesystem_sha256':None},{'resumption_plan_id':str(uuid.uuid4())}):
            with self.subTest(change=change),self.assertRaises(ControlError):
                parent_grants.parse_grant({**value,**change})

    def test_aliased_runtime_ancestor_cannot_redirect_repair_review_into_another_tree(self):
        before=self.snapshot()
        runtime=self.root/'runtime';preserved=self.root/'preserved-runtime-tree'
        runtime.rename(preserved);runtime.symlink_to(preserved,target_is_directory=True)
        with self.assertRaises(runtime_build.RuntimeBuildError):self.plan()
        self.assertTrue(runtime.is_symlink())
        for relative,data in before.items():
            target=preserved/relative.removeprefix('runtime/') if relative.startswith('runtime/') else self.root/relative
            self.assertEqual(target.read_bytes(),data)

    def test_changed_filesystem_changes_review_but_canonical_authority_does_not_change(self):
        first=self.plan();authority=state.inspect_state(self.root)
        (self.fixture.trees['node']/'bin/program').write_bytes(b'new observed corruption')
        second=self.plan()
        self.assertNotEqual(first['evidence_sha256'],second['evidence_sha256'])
        self.assertEqual(state.inspect_state(self.root),authority)


if __name__=='__main__':unittest.main()
