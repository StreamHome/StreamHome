import copy
import math
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import parent_grants as grants
from lifecycle.control.rpc import ControlError
from lifecycle import parent_recovery
from tests.lifecycle import test_parent_recovery as fixtures


class ParentGrantTests(unittest.TestCase):
    def setUp(self):
        fixtures.ParentRecoveryReviewTests.setUp(self)
        self.plan=parent_recovery.review(self.root,self.operation,
            control_manifest_sha256=self.bound.execution.control_manifest_sha256)
        # This test isolates grant construction from recovery action policy,
        # which is exercised by the existing actual canonical planner tests.
        self.plan['permitted_actions']=['continue_recovery']
        self.review_reader=self.enterContext(patch.object(parent_recovery,'review',return_value=self.plan))
        self.enterContext(patch('lifecycle.managed_restore.bound_journal',return_value=(self.journal,self.bound)))
        self.scope={'unit':'streamhome-install-'+self.bound.installation_id+'-'+uuid.uuid4().hex+'.scope',
            'invocation':'a'*32,'pid':4321,'start_identity':'123'}
        self.scope_check=self.enterContext(patch.object(grants,'require_scope'))
        self.reader=self.enterContext(patch('lifecycle.control.reader_support.require_parent_recovery_reader'))
        self.stop=self.enterContext(patch.object(grants,'read_stop_epoch',return_value=self.plan['stop_epoch']))
        self.payload={'operation_id':self.operation,'review_id':str(uuid.uuid4()),
            'expected_evidence_sha256':self.plan['evidence_sha256'],'action':'continue_recovery',
            'issuer_scope':self.scope,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'stop_epoch':self.plan['stop_epoch']}

    def prepare(self):
        return grants.prepare(self.root,self.payload,
            control_manifest_sha256=self.bound.execution.control_manifest_sha256)

    def test_finite_grant_binds_original_parent_and_existing_canonical_plan(self):
        before=self.snapshot();grant=self.prepare()
        grants.require_binding(grant,self.bound)
        self.assertEqual(grant['generation'],2)
        self.assertEqual(grant['canonical_evidence_identity'],self.plan['canonical_plan']['evidenceIdentity'])
        self.assertLessEqual(grant['expires_at_monotonic']-time.monotonic(),900)
        self.assertGreater(grant['expires_at_monotonic']-time.monotonic(),895)
        self.assertEqual(self.snapshot(),before)
        self.scope_check.assert_called_once_with(self.scope,self.bound.installation_id)

    def snapshot(self):return fixtures.ParentRecoveryReviewTests.snapshot(self)

    def test_unknown_fields_booleans_nonfinite_expiry_and_other_action_are_rejected(self):
        original=self.prepare()
        changes=({'schema_version':True},{'generation':True},{'generation':2**63-1},
            {'expires_at_monotonic':True},{'expires_at_monotonic':math.nan},
            {'expires_at_monotonic':math.inf},{'operation_type':'update'},
            {'action':'repair_bound_environment'},{'unknown':1},{'canonical_evidence_identity':'plan-v1:'+'0'*31})
        for change in changes:
            with self.subTest(change=list(change)),self.assertRaises(ControlError):
                grants.parse_grant({**original,**change})

    def test_stale_review_and_action_never_publish_or_rotate_authority(self):
        self.payload['expected_evidence_sha256']='f'*64
        with self.assertRaises(ControlError):self.prepare()
        self.payload['expected_evidence_sha256']=self.plan['evidence_sha256']
        self.payload['action']='rollback'
        with self.assertRaises(ControlError):self.prepare()
        self.assertEqual(self.journal['generation'],2)

    def test_scope_stop_and_expiry_are_live_requirements(self):
        grant=self.prepare()
        self.stop.return_value='f'*64
        with self.assertRaises(ControlError):grants.require_live(grant)
        self.stop.return_value=grant['stop_epoch']
        with patch.object(grants.time,'monotonic',return_value=grant['expires_at_monotonic']):
            with self.assertRaises(ControlError):grants.require_live(grant)
        self.scope_check.side_effect=ControlError('control_stopped_repair_issuer_changed')
        with self.assertRaises(ControlError):grants.require_live(grant)

    def test_fast_checks_reject_reused_dead_foreign_or_moved_issuer_between_full_proofs(self):
        grant=self.prepare();scope=grant['issuer_scope'];uid=[0];group=['0::/system.slice/'+scope['unit']]
        original=grants.Path
        class Process:
            def stat(self):return SimpleNamespace(st_uid=uid[0])
            def __truediv__(self,name):return SimpleNamespace(read_text=lambda:group[0])
        class ProcRoot:
            def __truediv__(self,name):return Process()
        def path(value):return ProcRoot() if value=='/proc' else original(value)
        with patch.object(grants,'Path',side_effect=path),patch(
            'lifecycle.control.administration_lock.process_identity',return_value=(scope['start_identity'],'S')) as identity:
            grants.require_live_identity(grant)
            identity.return_value=('999','S')
            with self.assertRaises(ControlError):grants.require_live_identity(grant)
            identity.return_value=(scope['start_identity'],'Z')
            with self.assertRaises(ControlError):grants.require_live_identity(grant)
            identity.return_value=(scope['start_identity'],'S');uid[0]=1000
            with self.assertRaises(ControlError):grants.require_live_identity(grant)
            uid[0]=0;group[0]='0::/system.slice/foreign.scope'
            with self.assertRaises(ControlError):grants.require_live_identity(grant)

    def test_wrong_retained_review_or_generation_and_binding_refused(self):
        grant=self.prepare()
        with patch.object(grants,'read_protected_json',side_effect=[grant,grant]):
            self.assertEqual(grants.read_grant(self.operation,3),grant)
        with patch.object(grants,'read_protected_json',side_effect=[grant,{**grant,'action':'rollback'}]):
            with self.assertRaises(ControlError):grants.read_grant(self.operation,3)
        with patch.object(grants,'read_protected_json',return_value=grant):
            with self.assertRaises(ControlError):grants.read_grant(self.operation,4)
        changed=copy.deepcopy(grant);changed['parent_binding_sha256']='f'*64
        with self.assertRaises(ControlError):grants.require_binding(changed,self.bound)

    def test_resumption_grant_requires_its_own_parent_plan_action_tuple_and_issuer(self):
        from tests.lifecycle.test_resumption_contract import plan as make_plan
        grant={**self.prepare(),'schema_version':2,'resumption_plan_id':str(uuid.uuid4())}
        self.assertEqual(grants.parse_grant(grant),grant)
        plan={**make_plan(),'schema_version':2,'plan_id':grant['resumption_plan_id'],
            'installation_id':grant['installation_id'],'operation_id':grant['operation_id'],
            'review_id':grant['review_id'],'owner_type':'restore','action':'restore_recovery',
            'recovery_action':grant['action'],'parent_binding_sha256':grant['parent_binding_sha256'],
            'generation':grant['generation']+1,
            'stages':[{'kind':'candidate_start','generation':grant['generation']+1,
                'execution_sha256':grant['execution_sha256'],'handoff_sha256':None}]}
        record={'plan':plan,'issuer':{'scope':grant['issuer_scope']}}
        with patch('lifecycle.control.resumption_ticket.reviewed_plan',return_value=record):
            self.assertEqual(grants.require_resumption_plan(grant),record)
            for change in ({'owner_type':'management'},{'recovery_action':'rollback'},
                           {'parent_binding_sha256':'f'*64},{'generation':grant['generation']+2}):
                original=dict(plan);plan.update(change)
                with self.subTest(change=change),self.assertRaises(ControlError):grants.require_resumption_plan(grant)
                plan.clear();plan.update(original)
        with self.assertRaises(ControlError):grants.parse_grant({**grant,'action':'safe_abort'})
        with self.assertRaises(ControlError):grants.parse_grant({**grant,'resumption_plan_id':None})


if __name__=='__main__':unittest.main()
