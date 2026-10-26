import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch
import uuid

from lifecycle.control import supervisor
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import parse_execution_tuple,execution_tuple_sha256
from tests.lifecycle.test_management_journal import _execution_payload


class RestoreTransportReadinessTests(unittest.TestCase):
    def setUp(self):
        self.actor=supervisor.Supervisor.__new__(supervisor.Supervisor)
        self.actor.installation=str(uuid.uuid4());self.actor.invocation='a'*32
        self.actor.execution=parse_execution_tuple(_execution_payload())
        self.actor.layout=SimpleNamespace(runtime=Path('/run/streamhome'))
        self.actor.config=SimpleNamespace(web_port=3000)
        self.actor.children={'backend':SimpleNamespace(process=SimpleNamespace(pid=4321),start='101'),
                             'web':SimpleNamespace(process=SimpleNamespace(pid=4322),start='102')}
        self.enterContext(patch.object(supervisor.state,'_process_start_identity',side_effect=lambda pid:str(pid-4220)))
        self.projection={'mode':'restore_verify','generalReady':False,'errorCode':'runtime_not_settled',
            'workersStarted':True,'recoveryCompleted':True,'recoverySucceeded':True}
        self.reply={'pid':4321,'start_identity':'101','execution_sha256':execution_tuple_sha256(self.actor.execution),
                    'readiness':self.projection}
        self.request=self.enterContext(patch.object(supervisor,'request',return_value=self.reply))
        self.response=MagicMock()
        self.response.headers={'X-StreamHome-Web-Build':self.actor.execution.application_build_id}
        self.body={**self.projection,'buildId':self.actor.execution.application_build_id}
        self.response.read.side_effect=lambda limit:json.dumps(self.body).encode()
        self.opener=MagicMock();self.opener.open.return_value.__enter__.return_value=self.response
        self.enterContext(patch.object(supervisor.urllib.request,'build_opener',return_value=self.opener))

    def test_transport_can_be_ready_before_settled_admission_without_completion_deadlock(self):
        self.assertTrue(self.actor.restore_transport_ready())
        self.assertFalse(self.projection['generalReady'])
        self.opener.open.assert_called_once_with('http://127.0.0.1:3000/api/health/live',timeout=2)
        self.assertEqual(self.request.call_args.kwargs['expected_pid'],4321)
        self.assertEqual(self.request.call_args.kwargs['expected_uid'],os.geteuid())

    def test_backend_recovery_or_workers_incomplete_never_confirm_proxy(self):
        self.projection['recoverySucceeded']=False
        self.assertFalse(self.actor.restore_transport_ready());self.opener.open.assert_not_called()

    def test_wrong_backend_pid_or_proxy_build_cannot_confirm_transport(self):
        self.reply['pid']=4323
        with self.assertRaises(ControlError):self.actor.restore_transport_ready()
        self.reply['pid']=4321;self.response.headers['X-StreamHome-Web-Build']='other-build'
        with self.assertRaises(ControlError):self.actor.restore_transport_ready()

    def test_wrong_mode_or_failed_proxy_recovery_never_confirms_completion(self):
        self.projection['mode']='normal'
        self.assertFalse(self.actor.restore_transport_ready())
        self.projection['mode']='restore_verify';self.body['recoverySucceeded']=False
        self.assertFalse(self.actor.restore_transport_ready())


class GuardedBackendTransportProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_parent_can_prove_transport_before_any_runtime_admission_exists(self):
        from lifecycle import backend_control,bootstrap,runtime_admission
        from lifecycle.control.rpc import Peer
        actor=backend_control.BackendControl.__new__(backend_control.BackendControl)
        actor.root=Path('/opt/streamhome');actor.layout=object()
        execution=parse_execution_tuple(_execution_payload())
        context=SimpleNamespace(mode=bootstrap.StartupMode.RESTORE_VERIFY,operation_id=str(uuid.uuid4()),generation=3)
        journal={'generation':3,'accepted':True,'progress':'normal_recovery'}
        projection={'mode':'restore_verify','generalReady':False,'workersStarted':True,
                    'recoveryCompleted':True,'recoverySucceeded':True,'errorCode':'runtime_not_settled'}
        with patch.object(backend_control.os,'getppid',return_value=200),patch(
            'lifecycle.control.processes.process_cgroup',return_value='/system.slice/streamhome.service'),patch.object(
            backend_control,'require_runtime_admission',side_effect=runtime_admission.RuntimeAdmissionError('not settled')) as admission,patch.object(
            bootstrap,'require_startup_context',return_value=context),patch.object(
            bootstrap,'require_current_guarded_authority') as authority,patch(
            'lifecycle.deployment_startup.execution_for_start',return_value=execution),patch(
            'lifecycle.restore_controller.inspect_restore_journal',return_value=journal),patch.object(
            backend_control,'get_startup_readiness',return_value=object()),patch.object(
            backend_control,'current_readiness_projection',return_value=projection):
            peer=Peer(200,os.geteuid(),os.getegid())
            value=await actor.handle(peer,'restore_transport_readiness',{})
            self.assertEqual(value['execution_sha256'],execution_tuple_sha256(execution))
            self.assertEqual(value['readiness'],projection);admission.assert_not_called()
            authority.assert_called_once_with(context)
            with self.assertRaises(runtime_admission.RuntimeAdmissionError):await actor.handle(peer,'readiness',{})
            for invalid in ({'accepted':False},{'generation':4},{'progress':'verifying_candidate'}):
                before=dict(journal);journal.update(invalid)
                with self.subTest(invalid=invalid),self.assertRaises(ControlError):
                    await actor.handle(peer,'restore_transport_readiness',{})
                journal.clear();journal.update(before)
            for stage in ('verifying_candidate','verifying_rollback','accepted','normal_recovery'):
                journal.update(accepted=False,progress=stage)
                value=await actor.handle(peer,'restore_guarded_readiness',{})
                self.assertEqual(value['execution_sha256'],execution_tuple_sha256(execution))
            journal['progress']='recovery_required'
            with self.assertRaises(ControlError):await actor.handle(peer,'restore_guarded_readiness',{})
            admission.assert_called_once()  # Only the ordinary readiness request above.
            for peer in (Peer(201,os.geteuid(),os.getegid()),Peer(200,os.geteuid()+1,os.getegid())):
                with self.assertRaises(ControlError):await actor.handle(peer,'restore_transport_readiness',{})
class ParentGuardedReadinessTests(unittest.TestCase):
    def setUp(self):
        fixture=RestoreTransportReadinessTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.actor=fixture.actor
        del self.actor.children['web']
        self.actor.resumption_binding=SimpleNamespace(operation_id=str(uuid.uuid4()),generation=3)
        bound=SimpleNamespace(execution=self.actor.execution)
        self.authority=self.enterContext(patch.object(self.actor,'_parent_journal',return_value=({},bound)))
        self.fixture.projection.update(guardedReady=True,workersStarted=False)

    def test_only_owned_guarded_backend_releases_os_start_without_web_or_general_admission(self):
        self.assertEqual(self.actor.restore_guarded_readiness(),'guarded')
        self.assertEqual(self.fixture.request.call_args.kwargs['action'],'restore_guarded_readiness')
        self.fixture.opener.open.assert_not_called()
        self.assertEqual(self.authority.call_count,2)

    def test_workers_started_or_non_guarded_projection_cannot_release_initial_start(self):
        for change in ({'workersStarted':True},{'guardedReady':False},{'mode':'normal'}):
            before=dict(self.fixture.projection);self.fixture.projection.update(change)
            with self.subTest(change=change):self.assertIsNone(self.actor.restore_guarded_readiness())
            self.fixture.projection.clear();self.fixture.projection.update(before)

    def test_wrong_process_tuple_or_changed_parent_cannot_release_start(self):
        self.fixture.reply['execution_sha256']='0'*64
        with self.assertRaises(ControlError):self.actor.restore_guarded_readiness()
        self.fixture.reply['execution_sha256']=execution_tuple_sha256(self.actor.execution)
        self.authority.side_effect=[self.authority.return_value,ControlError('control_controller_changed')]
        with self.assertRaises(ControlError):self.actor.restore_guarded_readiness()


if __name__=='__main__':unittest.main()
