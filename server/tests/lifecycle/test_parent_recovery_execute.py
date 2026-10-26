import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import parent_recovery,recovery,restore_controller as restore,state
from lifecycle.control import parent_grants as grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_parent_recovery as fixtures


class ParentRecoveryExecutionTests(unittest.TestCase):
    def setUp(self):
        fixtures.ParentRecoveryReviewTests.setUp(self)
        self.runtime.return_value='offline'
        self.enterContext(patch('lifecycle.control.processes.controller_peer'))
        self.enterContext(patch('lifecycle.control.reader_support.require_parent_recovery_reader'))
        self.enterContext(patch('lifecycle.managed_restore.bound_journal',return_value=(self.journal,self.bound)))
        self.enterContext(patch('lifecycle.managed_restore.deployment',return_value=SimpleNamespace(web_port=3000)))
        self.enterContext(patch.object(grants,'require_scope'))
        self.enterContext(patch.object(grants,'read_stop_epoch',return_value='b'*64))
        plan=parent_recovery.review(self.root,self.operation,
            control_manifest_sha256=self.bound.execution.control_manifest_sha256)
        self.assertIn('safe_abort',plan['permitted_actions'])
        scope={'unit':'streamhome-install-'+self.bound.installation_id+'-'+uuid.uuid4().hex+'.scope',
            'invocation':'a'*32,'pid':4321,'start_identity':'123'}
        self.grant=grants.prepare(self.root,{'operation_id':self.operation,'review_id':str(uuid.uuid4()),
            'expected_evidence_sha256':plan['evidence_sha256'],'action':'safe_abort','issuer_scope':scope,
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'stop_epoch':'b'*64},
            control_manifest_sha256=self.bound.execution.control_manifest_sha256)

    def execute(self):
        return parent_recovery.execute(self.root,self.operation,
            control_manifest_sha256=self.bound.execution.control_manifest_sha256)

    def test_actual_canonical_abort_rotates_parent_and_preserves_operator_receipt(self):
        old=state.load_controller_capability(self.root,operation_id=self.operation,generation=2)
        with patch.object(grants,'read_grant',return_value=self.grant) as reader,patch.object(
            restore,'_stop_supported_runtime') as stop:
            self.assertEqual(self.execute(),0)
        stop.assert_not_called()
        self.assertGreaterEqual(reader.call_count,3)
        current=state.inspect_state(self.root)
        self.assertEqual(current.generation,3);self.assertIsNone(current.active_operation)
        path,record=recovery._find_action_record(self.root,self.operation,3)
        self.assertEqual(record.status,'succeeded');self.assertEqual(record.requested_action,'safe_abort')
        self.assertEqual(record.result,'operator_safe_abort_completed')
        self.assertNotIn(old,path.read_text())
        self.assertFalse(restore.restore_paths(self.root,self.operation).journal_path.exists())

    def test_canceled_permission_before_transfer_leaves_original_generation_and_database(self):
        calls=0
        def read(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls>=3:raise ControlError('control_parent_recovery_permission_expired')
            return self.grant
        with patch.object(grants,'read_grant',side_effect=read),patch.object(
            restore,'_stop_supported_runtime') as stop,self.assertRaises(recovery.RecoveryExecutionError):
            self.execute()
        stop.assert_not_called()
        current=state.inspect_state(self.root)
        self.assertEqual(current.generation,2)
        self.assertEqual(current.active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        journal=restore.inspect_restore_journal(self.root,self.operation)
        self.assertFalse(journal['accepted']);self.assertEqual(journal['managed_execution'],self.bound.payload())

    def test_fresh_takeover_does_not_require_permission_from_an_expired_prior_issuer(self):
        capability=state.load_controller_capability(self.root,operation_id=self.operation,generation=2)
        lease,transfer=state.acquire_transferred_controller_lease(self.root,operation_id=self.operation,
            generation=2,expected_phase='recovery_required',owner_capability=capability,
            next_owner_kind='restore_controller')
        self.journal=restore.transfer_restore_journal_generation(self.root,self.operation,
            expected_generation=2,next_generation=3)
        lease.release()
        self.binding.return_value=(self.journal,self.bound)
        with patch('lifecycle.managed_restore.bound_journal',return_value=(self.journal,self.bound)):
            reviewed=parent_recovery.review(self.root,self.operation,
                control_manifest_sha256=self.bound.execution.control_manifest_sha256)
            fresh=grants.prepare(self.root,{'operation_id':self.operation,'review_id':str(uuid.uuid4()),
                'expected_evidence_sha256':reviewed['evidence_sha256'],'action':'safe_abort',
                'issuer_scope':self.grant['issuer_scope'],'boot_id':self.grant['boot_id'],'stop_epoch':'b'*64},
                control_manifest_sha256=self.bound.execution.control_manifest_sha256)
            self.assertEqual(fresh['generation'],3)
            def read(operation,generation,**kwargs):
                self.assertEqual(operation,self.operation)
                if generation==3:raise ControlError('control_parent_recovery_permission_expired')
                self.assertEqual(generation,4);return fresh
            with patch.object(grants,'read_grant',side_effect=read) as reader:
                self.assertEqual(self.execute(),0)
        self.assertTrue(all(call.args[1]==4 for call in reader.call_args_list))
        authority=state.inspect_state(self.root)
        self.assertEqual(authority.generation,4);self.assertIsNone(authority.active_operation)
        _,record=recovery._find_action_record(self.root,self.operation,4)
        self.assertEqual(record.status,'succeeded')

    def test_stale_grant_is_refused_before_operator_intent(self):
        bad={**self.grant,'source_evidence_sha256':'f'*64}
        with patch.object(grants,'read_grant',return_value=bad),self.assertRaises(ControlError):self.execute()
        self.assertFalse((state.lifecycle_paths(self.root).lifecycle_dir/'operator-actions').exists())
        self.assertEqual(state.inspect_state(self.root).generation,2)


if __name__=='__main__':unittest.main()
