from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import parent_recover as control
from lifecycle.control import recover as dispatch
from lifecycle.control.rpc import ControlError
from lifecycle.control.stopped_repair import StoppedRepairHandoff


class ParentRecoveryControlTests(unittest.TestCase):
    def setUp(self):
        self.installation=str(uuid.uuid4());self.operation=str(uuid.uuid4())
        self.snapshot={'operation_type':'restore','installation_id':self.installation,
            'operation_id':self.operation,'generation':7,'evidence_sha256':'a'*64,
            'permitted_actions':['continue_recovery'],'runtime_status':'active',
            'original_supervisor_invocation_id':'b'*32,'accepted':False}
        self.processes={'initial_service_state':'running','supervisor_invocation_id':'b'*32,
            'supervisor':{'pid':4321,'start_identity':'987'},'backend':None}
        self.enterContext(patch.object(control,'require_root'))
        self.epoch=self.enterContext(patch.object(control,'root_stop_epoch',return_value='c'*64))
        self.process_reader=self.enterContext(patch.object(control,'process_snapshot',return_value=self.processes))
        self.enterContext(patch.object(control,'current_scope',return_value={'unit':'synthetic-qualified-root-scope'}))
        self.scope=self.enterContext(patch.object(control,'require_stopped'))
        self.worker=self.enterContext(patch.object(control,'run_worker',return_value=self.snapshot))
        self.execute=self.enterContext(patch.object(control,'_run'))
        self.output=self.enterContext(redirect_stdout(StringIO()))

    def recover(self,**changes):
        args={'request_path':None,'control_manifest_sha256':'d'*64,'review_only':False,
            'action':'continue_recovery','scope_epoch':None}
        return control.recover(self.snapshot,**{**args,**changes})

    def test_review_only_is_read_only_even_when_no_default_action_is_available(self):
        self.snapshot['permitted_actions']=[]
        self.assertEqual(self.recover(review_only=True),0)
        self.worker.assert_not_called();self.execute.assert_not_called();self.process_reader.assert_not_called()

    def test_running_parent_also_uses_a_cancellable_root_issuer_scope(self):
        result=self.recover()
        self.assertIsInstance(result,StoppedRepairHandoff)
        self.assertEqual(result.preparation_id,self.operation)
        self.assertEqual(result.recovery_action,'continue_recovery')
        self.worker.assert_not_called();self.execute.assert_not_called()

    def test_decline_and_direct_stop_never_prepare_or_dispatch(self):
        with patch.object(dispatch,'_consent',return_value=False):
            self.assertEqual(self.recover(scope_epoch='c'*64,request_path=Path('/synthetic-request')),0)
        self.worker.assert_not_called();self.execute.assert_not_called()
        self.epoch.return_value='e'*64
        with self.assertRaises(ControlError):self.recover(scope_epoch='c'*64)
        self.worker.assert_not_called();self.execute.assert_not_called()

    def test_replaced_supervisor_or_stale_evidence_refuses_effect(self):
        self.process_reader.return_value={**self.processes,'supervisor_invocation_id':'e'*32}
        with self.assertRaises(ControlError):self.recover()
        self.process_reader.return_value=self.processes
        self.worker.return_value={**self.snapshot,'evidence_sha256':'e'*64}
        with patch.object(dispatch,'_consent',return_value=True),self.assertRaises(ControlError):
            self.recover(scope_epoch='c'*64,request_path=Path('/synthetic-request'))
        self.execute.assert_not_called()

    def test_fresh_consent_prepares_exact_grant_then_dispatches_it(self):
        grant={'control_manifest_sha256':'d'*64}
        self.worker.side_effect=[self.snapshot,grant]
        with patch.object(dispatch,'_consent',return_value=True):
            self.assertEqual(self.recover(scope_epoch='c'*64,request_path=Path('/synthetic-request')),0)
        self.assertEqual(self.worker.call_args_list[0].args[0],['recovery-review',self.operation])
        request=self.worker.call_args_list[1].kwargs['payload']
        self.assertEqual(request['operation_id'],self.operation)
        self.assertEqual(request['expected_evidence_sha256'],self.snapshot['evidence_sha256'])
        self.assertEqual(request['action'],'continue_recovery')
        self.execute.assert_called_once_with(grant)

    def test_offline_forward_recovery_requires_a_new_cancellable_issuer(self):
        self.snapshot['runtime_status']='offline'
        self.process_reader.return_value={'initial_service_state':'stopped'}
        result=self.recover()
        self.assertIsInstance(result,StoppedRepairHandoff)
        self.scope.assert_called_once()
        self.worker.assert_not_called();self.execute.assert_not_called()

    def test_offline_decline_uses_fresh_resumption_consent_never_legacy_consent(self):
        self.snapshot['runtime_status']='offline'
        self.process_reader.return_value={'initial_service_state':'stopped'}
        with patch('lifecycle.control.parent_resumption_review.make_review',return_value=object()),patch(
            'lifecycle.control.resumption_review.consent',return_value=False),patch.object(dispatch,'_consent') as old:
            self.assertEqual(self.recover(scope_epoch='c'*64,request_path=Path('/synthetic-request')),0)
        old.assert_not_called();self.worker.assert_not_called();self.execute.assert_not_called()

    def test_offline_plan_is_prepared_then_published_before_operation_dispatch(self):
        from lifecycle.control import parent_resumption_review,resumption_review,resumption_store
        self.snapshot['runtime_status']='offline'
        self.process_reader.return_value={'initial_service_state':'stopped'}
        review=SimpleNamespace(issuer={'scope':'owned scope'})
        plan={'plan_id':str(uuid.uuid4()),'review_id':str(uuid.uuid4())}
        grant={'schema_version':2,'control_manifest_sha256':'d'*64}
        events=[]
        def worker(arguments,**kwargs):
            if arguments[0]=='recovery-review':return self.snapshot
            payload=kwargs['payload']
            self.assertEqual(payload['resumption_plan_id'],plan['plan_id'])
            self.assertEqual(payload['review_id'],plan['review_id'])
            events.append('prepared');return grant
        self.worker.side_effect=worker
        self.execute.side_effect=lambda value:events.append('dispatched')
        with patch.object(parent_resumption_review,'make_review',return_value=review),patch.object(
            resumption_review,'consent',return_value=True),patch.object(
            parent_resumption_review,'launch_plan',return_value=plan),patch.object(
            resumption_store,'publish',side_effect=lambda *a,**kw:events.append('published')):
            self.assertEqual(self.recover(scope_epoch='c'*64,request_path=Path('/synthetic-request')),0)
        self.assertEqual(events,['prepared','published','dispatched'])


if __name__=='__main__':unittest.main()
