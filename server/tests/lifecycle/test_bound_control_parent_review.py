"""Original parent control identity review without grants or ownership changes."""
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_control_parent_review as review, bound_control_repair, state
from lifecycle.control.rpc import ControlError
from lifecycle.control import parent_grants,update_grants
from tests.lifecycle import test_bound_parent_repair as restore_fixtures
from tests.lifecycle import test_bound_update_repair as update_fixtures


class ParentControlReviewTests(unittest.TestCase):
    def fixture(self,kind):
        fixture=(restore_fixtures.BoundParentRepairPlanTests() if kind=='restore'
            else update_fixtures.BoundUpdateRepairTests())
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.root=fixture.root
        if kind=='restore':
            self.operation=fixture.operation;self.execution=fixture.bound.execution
            self.layout=fixture.layout
            active=review.bound_parent_repair.read_active_execution(self.root)
            active.payload=lambda:{'installation_id':active.installation_id,
                'execution':review.bound_parent_repair.execution_tuple_sha256(active.execution)}
        else:
            self.operation=fixture.operation;self.execution=fixture.canonical.parent.execution
            self.layout=fixture.canonical.layout
        self.enterContext(patch.object(review,'layout_for_root',return_value=self.layout))
        self.enterContext(patch.object(review,'require_stopped'))
        self.enterContext(patch.object(review,'require_service_quiescent'))
        self.enterContext(patch.object(review,'read_stop_epoch',return_value='e'*64))
        self.artifact={'bundle_id':self.execution.control_bundle_id,'manifest_sha256':self.execution.control_manifest_sha256,
            'artifact_sha256':'b'*64,'entry_sha256':'c'*64}
        self.verify=self.enterContext(patch.object(bound_control_repair,'_artifact',return_value=SimpleNamespace(
            bundle_id=self.execution.control_bundle_id,manifest_sha256=self.execution.control_manifest_sha256)))
        self.enterContext(patch.object(review.control_reconstruction,'fingerprint',return_value='d'*64))
        return fixture

    def plan(self):
        return review.plan(self.root,self.operation,self.artifact,control_manifest_sha256=self.execution.control_manifest_sha256)

    def test_restore_exact_control_review_preserves_canonical_files_and_acceptance(self):
        fixture=self.fixture('restore')
        for accepted in (False,True):
            if accepted:fixture.accept_parent()
            before=fixture.snapshot();authority=state.inspect_state(self.root)
            value=self.plan()
            self.assertEqual(value,self.plan());self.assertEqual(value['accepted'],accepted)
            self.assertEqual(value['roles'],['controller','runtime'])
            self.assertEqual(value['permitted_actions'],['repair_bound_control'])
            self.assertEqual(fixture.snapshot(),before);self.assertEqual(state.inspect_state(self.root),authority)

    def test_update_exact_control_review_preserves_original_owner_and_journal(self):
        fixture=self.fixture('update')
        before=state.inspect_state(self.root),fixture.canonical.path.read_bytes()
        value=self.plan()
        self.assertEqual(value['operation_type'],'update');self.assertTrue(value['read_only'])
        self.assertEqual(value['roles'],['controller','runtime'])
        self.assertEqual((state.inspect_state(self.root),fixture.canonical.path.read_bytes()),before)

    def test_valid_but_unbound_artifact_or_wrong_controller_reader_cannot_choose_a_version(self):
        self.fixture('restore')
        self.verify.return_value=SimpleNamespace(bundle_id=str(uuid.uuid4()),manifest_sha256='f'*64)
        with self.assertRaisesRegex(ControlError,'control_reconstruction_identity_changed'):self.plan()
        with self.assertRaisesRegex(ControlError,'control_operation_reader_changed'):
            review.plan(self.root,self.operation,self.artifact,control_manifest_sha256='f'*64)

    def test_root_is_denied_before_canonical_or_signed_artifact_read(self):
        self.fixture('update');self.verify.reset_mock()
        with patch.object(review.os,'geteuid',return_value=0),patch.object(state,'inspect_state') as inspect, \
                self.assertRaisesRegex(ControlError,'control_worker_forbidden'):self.plan()
        inspect.assert_not_called();self.verify.assert_not_called()

    def test_stop_epoch_change_during_signature_work_invalidates_the_review(self):
        self.fixture('update')
        with patch.object(review,'read_stop_epoch',side_effect=['a'*64,'b'*64]), \
                self.assertRaisesRegex(ControlError,'control_recovery_review_stale'):self.plan()

    def test_restore_acceptance_during_artifact_verification_invalidates_original_review(self):
        fixture=self.fixture('restore')
        verified=self.verify.return_value
        def accepted(_artifact):
            fixture.accept_parent()
            return verified
        self.verify.side_effect=accepted
        with self.assertRaisesRegex(ControlError,'control_recovery_review_stale'):self.plan()
        journal=review.restore_controller.inspect_restore_journal(self.root,self.operation)
        self.assertTrue(journal['accepted'])
        self.assertEqual(state.inspect_state(self.root).active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)

    def test_live_original_controller_blocks_even_a_valid_bound_artifact(self):
        self.fixture('update');self.verify.reset_mock()
        with patch.object(review.recovery,'_controller_status',return_value='active'), \
                self.assertRaisesRegex(ControlError,'control_reconstruction_parent_unavailable'):self.plan()
        self.verify.assert_not_called()

    def test_each_owner_prepares_only_exact_control_permission_without_writes_or_start_plan(self):
        # Use separate fixture lifetimes because both owners patch canonical
        # adapters. Releasing each stack also removes every temporary state file.
        for kind in ('update','restore'):
            with self.subTest(kind=kind):
                case=ParentControlReviewTests();case.setUp()
                try:
                    fixture=case.fixture(kind);view=case.plan()
                    scope={'unit':'streamhome-install-'+view['installation_id']+'-'+uuid.uuid4().hex+'.scope',
                        'invocation':'a'*32,'pid':1234,'start_identity':'5678'}
                    payload={'operation_id':case.operation,'artifact':case.artifact,
                        'expected_evidence_sha256':view['evidence_sha256'],'recovery_review_id':str(uuid.uuid4()),
                        'issuer_scope':scope,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                        'stop_epoch':view['stop_epoch']}
                    grants=update_grants if kind=='update' else parent_grants
                    before=state.inspect_state(case.root)
                    journal=review._update(case.root,case.operation)[1] if kind=='update' else review._restore(case.root,case.operation)[1]
                    with patch.object(grants,'require_scope'),patch.object(grants,'read_stop_epoch',return_value=view['stop_epoch']):
                        grant=review.prepare(case.root,payload,control_manifest_sha256=case.execution.control_manifest_sha256)
                    self.assertEqual(grants.parse_grant(grant),grant)
                    self.assertEqual(grant['schema_version'],4);self.assertEqual(grant['action'],'repair_bound_control')
                    self.assertNotIn('resumption_plan_id',grant)
                    self.assertEqual(grant['control_artifact'],case.artifact)
                    self.assertEqual(state.inspect_state(case.root),before)
                    current=review._update(case.root,case.operation)[1] if kind=='update' else review._restore(case.root,case.operation)[1]
                    self.assertEqual(current,journal)
                    for changes in ({'schema_version':3},{'action':'repair_bound_environment'},
                            {'control_selection':{**grant['control_selection'],'bundle_id':str(uuid.uuid4())}},
                            {'resumption_plan_id':str(uuid.uuid4())}):
                        with self.subTest(changes=changes),self.assertRaises(ControlError):
                            grants.parse_grant({**grant,**changes})
                finally:case.doCleanups()


if __name__=='__main__':unittest.main()
