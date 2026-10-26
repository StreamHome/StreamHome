from copy import deepcopy
import unittest

from lifecycle.control import resumption_contract as contract
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_resumption_contract import plan


class ParentResumptionContractTests(unittest.TestCase):
    def setUp(self):
        self.plan={**plan(),'schema_version':2,'owner_type':'restore','action':'restore_recovery',
            'recovery_action':'continue_recovery','parent_binding_sha256':'9'*64}
        self.plan['stages']=self.plan['stages'][:1]
        stage=self.plan['stages'][0]
        self.observed={**{name:self.plan[name] for name in ('installation_id','operation_id','owner_type','action',
            'parent_binding_sha256','recovery_action')},**{name:stage[name] for name in
            ('generation','execution_sha256','handoff_sha256')},
            'accepted':True,'candidate_failed':False,'previous_restored':False}

    def authorize(self,**changes):
        return contract.authorize_stage(self.plan,'candidate_start',{**self.observed,**changes},
            boot_id=self.plan['boot_id'],stop_epoch=self.plan['stop_epoch'],
            issuer_sha256=self.plan['issuer_sha256'],now=100,candidate_consumed=False)

    def test_exact_accepted_parent_allows_only_its_single_reviewed_os_start(self):
        self.assertEqual(self.authorize(),self.plan['stages'][0])
        receipt=contract.consume(self.plan,self.plan['stages'][0],'1'*64,existing=None)
        with self.assertRaises(ControlError):
            contract.consume(self.plan,self.plan['stages'][0],'2'*64,existing=receipt)

    def test_original_binding_or_requested_parent_action_cannot_be_retargeted(self):
        for changed in ({'parent_binding_sha256':'8'*64},{'recovery_action':'rollback'}):
            with self.subTest(changed=changed),self.assertRaises(ControlError):self.authorize(**changed)

    def test_unaccepted_rollback_reuses_same_execution_and_acceptance_forbids_it(self):
        self.plan['recovery_action']='rollback';self.observed['recovery_action']='rollback'
        with self.assertRaises(ControlError):self.authorize()
        self.assertEqual(self.authorize(accepted=False),self.plan['stages'][0])

    def test_no_unreviewed_fallback_legacy_downgrade_or_other_parent_borrows_permission(self):
        for changed in ({'schema_version':1},{'owner_type':'update'},{'action':'repair_environment'},
                        {'recovery_action':'safe_abort'},{'parent_binding_sha256':None}):
            with self.subTest(changed=changed),self.assertRaises(ControlError):
                contract.parse_plan({**self.plan,**changed})
        changed=deepcopy(self.plan);changed['stages'].append(plan()['stages'][1])
        with self.assertRaises(ControlError):contract.parse_plan(changed)

    def test_update_forward_stage_is_consumed_once_and_cannot_be_borrowed_by_restore_or_rollback(self):
        self.plan.update(owner_type='update', action='update_recovery')
        self.observed.update(owner_type='update', action='update_recovery')
        self.assertEqual(self.authorize(), self.plan['stages'][0])
        receipt = contract.consume(self.plan, self.plan['stages'][0], '1'*64, existing=None)
        with self.assertRaises(ControlError):
            contract.consume(self.plan, self.plan['stages'][0], '2'*64, existing=receipt)
        with self.assertRaises(ControlError):self.authorize(owner_type='restore', action='restore_recovery')
        with self.assertRaises(ControlError):contract.parse_plan({**self.plan, 'recovery_action': 'rollback'})


if __name__=='__main__':unittest.main()
