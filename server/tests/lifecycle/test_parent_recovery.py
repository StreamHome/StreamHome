import os
from pathlib import Path
import unittest
from unittest.mock import patch

from lifecycle import parent_recovery, recovery, restore_controller as restore, state
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_managed_restore as fixtures


class ParentRecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        fixtures.ManagedRestoreJournalTests.setUp(self)
        fixtures.ManagedRestoreJournalTests.create(self)
        # Exercise the real canonical transfer and response-EOF failure, rather
        # than synthesizing a recovery-required generation/journal by hand.
        gate_read,gate_write=os.pipe();os.close(gate_write)
        self.assertEqual(restore._run_controller(self.root,self.operation,1,self.claim.owner_capability,
            acknowledgement_fd=None,gate_fd=gate_read),1)
        self.journal=restore.inspect_restore_journal(self.root,self.operation)
        self.binding=self.enterContext(patch.object(parent_recovery,'bound_journal',
            return_value=(self.journal,self.bound)))
        self.endpoint=self.enterContext(patch.object(parent_recovery,'_endpoint'))
        self.epoch=self.enterContext(patch.object(parent_recovery,'read_stop_epoch',return_value='b'*64))
        self.runtime=self.enterContext(patch.object(recovery,'_runtime_status',return_value='active'))

    def review(self):
        return parent_recovery.review(self.root,self.operation,
            control_manifest_sha256=self.bound.execution.control_manifest_sha256)

    def snapshot(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_review_delegates_to_canonical_planner_without_claim_or_journal_mutation(self):
        before=self.snapshot();authority=state.inspect_state(self.root)
        with patch.object(recovery,'plan_recovery',wraps=recovery.plan_recovery) as planner:
            first=self.review();second=self.review()
        self.assertEqual(planner.call_count,2)
        self.assertEqual(first,second)
        self.assertTrue(first['read_only'])
        self.assertEqual(first['canonical_plan']['evidenceIdentity'],
            recovery.plan_recovery(self.root,operation_id=self.operation,generation=2)['evidenceIdentity'])
        self.assertEqual(first['generation'],2)
        self.assertEqual(first['original_generation'],1)
        self.assertEqual(first['permitted_actions'],first['canonical_plan']['permittedActions'])
        self.assertEqual(state.inspect_state(self.root),authority)
        self.assertEqual(self.snapshot(),before)

    def test_current_os_intent_changes_evidence_without_rewriting_original_binding(self):
        before=self.review();self.epoch.return_value='c'*64;after=self.review()
        self.assertNotEqual(before['evidence_sha256'],after['evidence_sha256'])
        self.assertEqual(self.journal['managed_execution'],self.bound.payload())
        self.epoch.side_effect=['c'*64,'d'*64]
        with self.assertRaises(ControlError):self.review()

    def test_replaced_supervisor_or_wrong_control_reader_is_refused(self):
        self.endpoint.side_effect=ControlError('control_parent_binding_changed')
        with self.assertRaises(ControlError):self.review()
        self.endpoint.side_effect=None
        with self.assertRaises(ControlError):
            parent_recovery.review(self.root,self.operation,control_manifest_sha256='f'*64)

    def test_offline_review_never_requires_or_launches_the_old_supervisor(self):
        self.runtime.return_value='offline'
        review=self.review()
        self.assertEqual(review['runtime_status'],'offline')
        self.endpoint.assert_not_called()
        self.assertEqual(state.inspect_state(self.root).generation,2)


if __name__=='__main__':unittest.main()
