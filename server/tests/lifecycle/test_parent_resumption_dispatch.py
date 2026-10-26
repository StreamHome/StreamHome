"""Single-use OS dispatch retains exact parent permission and independent proof."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from lifecycle.control import parent_grants, resumption_dispatch as dispatch
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_resumption_dispatch as dispatch_fixture


class ParentResumptionDispatchTests(unittest.TestCase):
    def setUp(self):
        fixture = dispatch_fixture.ResumptionDispatchTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.plan = fixture.plan
        self.plan.update(schema_version=2, owner_type='restore', action='restore_recovery',
            parent_binding_sha256='9'*64, recovery_action='continue_recovery')
        self.plan['stages'] = self.plan['stages'][:1]
        # Replace only the disposable published fixture before dispatch; the
        # production store never overwrites a published or consumed permission.
        (fixture.fixture.root/self.plan['plan_id']/'plan.json').write_text(json.dumps(
            {'plan':self.plan, 'issuer':fixture.fixture.evidence}))
        self.grant = {'schema_version':2, 'review_id':self.plan['review_id'],
            'resumption_plan_id':self.plan['plan_id'], 'installation_id':self.plan['installation_id'],
            'operation_id':self.plan['operation_id'], 'operation_type':'restore',
            'generation':self.plan['generation']-1,
            'execution_sha256':self.plan['stages'][0]['execution_sha256'],
            'parent_binding_sha256':self.plan['parent_binding_sha256'],
            'control_manifest_sha256':fixture.manifest, 'source_evidence_sha256':'8'*64,
            'canonical_evidence_identity':'plan-v1:'+'7'*32, 'action':'continue_recovery',
            'issuer_scope':fixture.fixture.evidence['scope'], 'boot_id':self.plan['boot_id'],
            'stop_epoch':self.plan['stop_epoch'], 'expires_at_monotonic':self.plan['expires_at_monotonic']}
        fixture.grant = self.grant
        fixture.worker.return_value = {**fixture.fixture.observation(),
            'parent_binding_sha256':self.plan['parent_binding_sha256'],
            'recovery_action':self.plan['recovery_action']}
        # Scope process mechanics are isolated here; exact live scope and OS
        # proof have dedicated tests. All parent-to-plan associations remain real.
        self.enterContext(patch.object(parent_grants, 'parse_scope'))
        self.enterContext(patch.object(parent_grants, 'require_live'))
        self.enterContext(patch('lifecycle.control.resumption_ticket.reviewed_plan',
            return_value={'plan':self.plan, 'issuer':fixture.fixture.evidence}))

    def start(self):return dispatch.start(self.grant, control_manifest_sha256=self.fixture.manifest)

    def test_parent_consumes_one_start_and_observes_same_bound_invocation(self):
        self.assertTrue(self.start());self.assertTrue(self.start())
        self.assertEqual(self.fixture.systemctl.call_count,1)
        self.assertEqual(self.fixture.worker.call_count,1)
        receipt=json.loads(self.fixture.fixture.receipt_path().read_text())
        self.assertEqual(receipt['status'],'bound')

    def test_unrelated_action_parent_binding_or_review_cannot_consume_start(self):
        for changes in ({'action':'rollback'},{'parent_binding_sha256':'f'*64},
                        {'review_id':self.plan['installation_id']}, {'generation':True},
                        {'schema_version':1}):
            with self.subTest(changes=changes):
                original=dict(self.grant);self.grant.update(changes)
                with self.assertRaises(ControlError):self.start()
                self.grant.clear();self.grant.update(original)
        self.fixture.worker.assert_not_called();self.fixture.systemctl.assert_not_called()
        self.assertFalse(self.fixture.fixture.receipt_path().exists())

    def test_verified_parent_pending_controller_cannot_consume_or_dispatch_start(self):
        self.fixture.worker.return_value={'pending':True}
        # A speculative not-ready observation is pending only after the live
        # issuer and unconsumed stage are proved again; it grants no OS effect.
        with patch.object(dispatch,'qualify',wraps=dispatch.qualify) as qualify:
            self.assertFalse(self.start())
        self.assertEqual(qualify.call_count,2)
        self.fixture.systemctl.assert_not_called()
        self.assertFalse(self.fixture.fixture.receipt_path().exists())

    def test_lost_reply_cannot_retry_parent_start(self):
        self.fixture.systemctl.side_effect=subprocess.TimeoutExpired('fixed start',30)
        with self.assertRaises(ControlError):self.start()
        with self.assertRaises(ControlError):self.start()
        self.assertEqual(self.fixture.systemctl.call_count,1)

    def test_prestart_requires_second_independent_observation_and_binds_once(self):
        from lifecycle.control import resumption_store
        resumption_store.consume_stage(self.plan['plan_id'],'candidate_start',
            self.fixture.worker.return_value,control_manifest_sha256=self.fixture.manifest)
        dispatch.before_start(control_manifest_sha256=self.fixture.manifest)
        self.assertEqual(self.fixture.worker.call_args.kwargs['payload']['startup_hook']['pid'],os.getpid())
        with self.assertRaises(ControlError):dispatch.before_start(control_manifest_sha256=self.fixture.manifest)

    def test_root_parent_grant_reader_imports_no_application_or_canonical_authority(self):
        script="import sys;import lifecycle.control.parent_grants;assert not {'lifecycle.state','lifecycle.management_journal','config','database','application'}.intersection(sys.modules)"
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
