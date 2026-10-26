"""Guarded UPDATE recovery cannot depend on its own future settled admission."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import backend_control, bootstrap, managed_update, runtime_admission, state
from lifecycle.control.rpc import ControlError, Peer
from lifecycle.management_journal import parse_execution_tuple, execution_tuple_sha256
from tests.lifecycle.test_management_journal import _execution_payload
from tests.lifecycle import test_restore_web_readiness as restore_readiness


class UpdateProxyRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = restore_readiness.RestoreTransportReadinessTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture = fixture;self.actor = fixture.actor
        fixture.projection['mode'] = fixture.body['mode'] = 'update_verify'

    def test_guarded_start_uses_private_update_projection_before_runtime_admission_exists(self):
        self.actor.resumption_binding = None
        self.fixture.projection.update(guardedReady=True, workersStarted=False, errorCode=None)
        self.fixture.body.update(readyKind='guarded')
        self.assertEqual(self.actor.selected_readiness(guarded_update=True), 'guarded')
        self.assertEqual(self.fixture.request.call_args.kwargs['action'], 'update_guarded_readiness')
        self.fixture.opener.open.assert_called_once_with('http://127.0.0.1:3000/api/health', timeout=2)
        self.fixture.projection['mode'] = 'normal'
        with self.assertRaises(ControlError):self.actor.selected_readiness(guarded_update=True)

    def test_actual_backend_and_web_projection_can_confirm_recovery_before_settlement(self):
        self.assertTrue(self.actor.update_transport_ready())
        self.assertEqual(self.fixture.request.call_args.kwargs['action'], 'update_transport_readiness')
        self.assertFalse(self.fixture.projection['generalReady'])
        self.fixture.opener.open.assert_called_once_with('http://127.0.0.1:3000/api/health/live', timeout=2)

    def test_other_guarded_mode_or_recovery_failure_cannot_confirm_update(self):
        self.fixture.projection['mode'] = 'restore_verify'
        self.assertFalse(self.actor.update_transport_ready())
        self.fixture.opener.open.assert_not_called()
        self.fixture.projection['mode'] = 'update_verify'
        self.fixture.body['mode'] = 'restore_verify'
        with self.assertRaises(ControlError):self.actor.update_transport_ready()
        self.fixture.body.update(mode='update_verify', recoverySucceeded=False)
        self.assertFalse(self.actor.update_transport_ready())


class UpdateBackendRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_accepted_recovering_update_and_actual_parent_can_bypass_settlement_projection(self):
        actor = backend_control.BackendControl.__new__(backend_control.BackendControl)
        actor.root = Path('/opt/streamhome');actor.layout = object()
        execution = parse_execution_tuple(_execution_payload())
        context = SimpleNamespace(mode=bootstrap.StartupMode.UPDATE_VERIFY,
            operation_id=str(uuid.uuid4()), generation=3)
        authority = SimpleNamespace(generation=3, active_operation=SimpleNamespace(phase=state.LifecyclePhase.VERIFYING))
        bound = SimpleNamespace(accepted=execution, verification=execution)
        journal = {'phase': 'recovering'}
        projection = {'mode': 'update_verify', 'generalReady': False, 'workersStarted': True,
            'recoveryCompleted': True, 'recoverySucceeded': True, 'errorCode': 'runtime_not_settled'}
        with patch.object(backend_control.os, 'getppid', return_value=200), patch(
                'lifecycle.control.processes.process_cgroup', return_value='/system.slice/streamhome.service'), patch.object(
                bootstrap, 'require_startup_context', return_value=context), patch.object(
                bootstrap, 'require_current_guarded_authority'), patch.object(
                managed_update, 'bound_journal', return_value=(authority, journal, bound)), patch.object(
                managed_update, 'verification_execution', return_value=execution), patch.object(
                backend_control, 'get_startup_readiness', return_value=object()), patch.object(
                backend_control, 'current_readiness_projection', return_value=projection), patch.object(
                backend_control, 'require_runtime_admission', side_effect=runtime_admission.RuntimeAdmissionError('not settled')) as admission:
            peer = Peer(200, os.geteuid(), os.getegid())
            result = await actor.handle(peer, 'update_transport_readiness', {})
            self.assertEqual(result['execution_sha256'], execution_tuple_sha256(execution))
            admission.assert_not_called()
            bound.accepted = None
            with self.assertRaises(ControlError):await actor.handle(peer, 'update_transport_readiness', {})
            journal['phase'] = 'starting'
            value = await actor.handle(peer, 'update_guarded_readiness', {})
            self.assertEqual(value['execution_sha256'], execution_tuple_sha256(execution))
            admission.assert_not_called()
            for phase in ('starting', 'recovering', 'failed', 'rolled_back'):
                journal['phase'] = phase
                await actor.handle(peer, 'update_guarded_readiness', {})
            for phase in ('preflight', 'rollback_failed', 'succeeded'):
                journal['phase'] = phase
                with self.assertRaises(ControlError):await actor.handle(peer, 'update_guarded_readiness', {})
            journal['phase'] = 'starting';bound.verification = None
            with self.assertRaises(ControlError):await actor.handle(peer, 'update_guarded_readiness', {})
            bound.verification = execution
            bound.accepted = execution;journal['phase'] = 'starting'
            with self.assertRaises(ControlError):await actor.handle(peer, 'update_transport_readiness', {})
            journal['phase'] = 'recovering'
            for invalid in (Peer(201, os.geteuid(), os.getegid()), Peer(200, os.geteuid()+1, os.getegid())):
                with self.assertRaises(ControlError):await actor.handle(invalid, 'update_transport_readiness', {})
            with self.assertRaises(runtime_admission.RuntimeAdmissionError):await actor.handle(peer, 'readiness', {})
