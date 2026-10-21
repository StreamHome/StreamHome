import asyncio
from contextlib import nullcontext
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

from lifecycle.control import notification, startup, supervisor
from lifecycle.control.rpc import ControlError, Peer
from lifecycle.control.units import unit_files
from lifecycle import backend_control, management_journal as contract
from tests.lifecycle.test_management_requests import request_payload
from lifecycle.control.requests import parse_management_request


class StartupNotificationTests(unittest.TestCase):
    def test_startup_observer_never_retries_a_quarantined_desired_deployment(self):
        invocation = '1' * 32
        active, desired = str(uuid.uuid4()), str(uuid.uuid4())
        snapshot = {'lifecycle': {'active_operation': None}, 'configuration': {'deployment': {
            'active': {'revision': active, 'sha256': 'a' * 64}, 'desired': {'revision': desired, 'sha256': 'b' * 64},
            'quarantined': [desired]}}}
        selected = SimpleNamespace(status=startup.InstallationStatus.READY)
        with patch.object(startup, 'require_root'), patch.object(startup.Path, 'read_text', return_value=f'0::/system.slice/streamhome-startup@{invocation}.service\n'), patch.object(startup, 'administrative_lock', return_value=nullcontext()), patch.object(startup, 'descriptor', return_value=selected), patch.object(startup, 'root_stop_epoch', return_value='a' * 64), patch.object(startup, 'wait_supervisor', return_value={}), patch.object(startup, 'run_worker', return_value=snapshot) as worker, patch.object(startup, 'process_snapshot') as processes, patch.object(startup, 'run_operation') as claim:
            self.assertEqual(startup.activate_pending(invocation), 0)
        worker.assert_called_once_with(['inspect'], timeout=120)
        processes.assert_not_called()
        claim.assert_not_called()

    def test_extensions_continue_during_blocked_event_loop_but_expire_without_ready(self):
        values = []
        lock = threading.Lock()
        def notify(value):
            with lock:
                values.append(value)
        with patch.object(notification, 'STARTUP_RECOVERY_SECONDS', 0.10), patch.object(notification, 'STARTUP_EXTENSION_INTERVAL', 0.01):
            with notification.extending_startup(notify, lambda: True):
                # A synchronous verifier prevents asyncio callbacks, but the
                # service's own thread must keep systemd informed meanwhile.
                time.sleep(0.06)
                self.assertGreaterEqual(len(values), 2)
                time.sleep(0.10)
                count = len(values)
                time.sleep(0.04)
                self.assertEqual(len(values), count)
        self.assertTrue(all(value.startswith('EXTEND_TIMEOUT_USEC=') for value in values))
        self.assertNotIn('READY=1', '\n'.join(values))

    def test_stop_closes_extensions_without_minting_readiness(self):
        values = []
        with patch.object(notification, 'STARTUP_EXTENSION_INTERVAL', 0.01):
            with notification.extending_startup(values.append, lambda: False):
                time.sleep(0.04)
        self.assertEqual(len(values), 1)

    def test_pending_startup_observer_has_no_notify_dependency_cycle(self):
        files = unit_files()
        runtime = files['/etc/systemd/system/streamhome.service'].decode()
        observer = files['/etc/systemd/system/streamhome-startup@.service'].decode()
        self.assertIn('Type=notify', runtime)
        self.assertIn(' --before-start', runtime)
        self.assertNotIn('ExecStartPost=', runtime)
        self.assertIn('BindsTo=streamhome.service', observer)
        self.assertNotIn('After=streamhome.service', observer)

    def test_observer_accepts_exact_activating_supervisor_and_never_ignores_new_stop(self):
        invocation = '1' * 32
        unit = {'ActiveState': 'activating', 'MainPID': '200', 'InvocationID': invocation}
        with patch.object(startup, 'descriptor', return_value=object()), patch.object(startup, 'root_stop_epoch', return_value='a' * 64), patch.object(startup, 'unit_snapshot', return_value=unit), patch.object(startup, 'supervisor_snapshot', return_value={'launches_open': True}):
            self.assertEqual(startup.wait_supervisor(invocation, 'a' * 64), unit)
        with patch.object(startup, 'descriptor', return_value=object()), patch.object(startup, 'root_stop_epoch', return_value='b' * 64), patch.object(startup, 'unit_snapshot') as observe:
            with self.assertRaisesRegex(ControlError, 'control_operator_stopped'):
                startup.wait_supervisor(invocation, 'a' * 64)
            observe.assert_not_called()


class SupervisorReadinessTests(unittest.IsolatedAsyncioTestCase):
    def waiting_supervisor(self):
        instance = supervisor.Supervisor.__new__(supervisor.Supervisor)
        instance.launches_open = True
        instance.stopping = asyncio.Event()
        instance.lock = asyncio.Lock()
        instance.children = {}
        return instance

    async def test_initial_start_waits_through_recovery_without_early_ready(self):
        instance = self.waiting_supervisor()
        instance.selected_readiness = Mock(side_effect=[None, None, 'general'])
        messages = []
        with patch.object(supervisor, 'notify', side_effect=messages.append), patch.object(supervisor.asyncio, 'sleep', new=AsyncMock()):
            self.assertEqual(await instance.wait_initial_readiness(time.monotonic() + 10), 'general')
        self.assertEqual(instance.selected_readiness.call_count, 3)
        self.assertFalse(any('READY=1' in value for value in messages))

    async def test_stop_during_readiness_probe_never_acknowledges_ready(self):
        instance = self.waiting_supervisor()
        def stop():
            instance.launches_open = False
            return 'general'
        instance.selected_readiness = stop
        with patch.object(supervisor, 'notify') as notify:
            self.assertIsNone(await instance.wait_initial_readiness(time.monotonic() + 10))
        notify.assert_not_called()

    async def test_initial_recovery_has_a_finite_deadline(self):
        instance = self.waiting_supervisor()
        instance.selected_readiness = Mock()
        with self.assertRaisesRegex(ControlError, 'control_startup_recovery_timeout'):
            await instance.wait_initial_readiness(time.monotonic() - 1)
        instance.selected_readiness.assert_not_called()

    async def test_run_emits_ready_only_after_selected_readiness_and_always_cleans_up(self):
        instance = self.waiting_supervisor()
        instance.server = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        instance.layout = SimpleNamespace(runtime=Path('/disposable-runtime'))
        instance._deployment = Mock(return_value=object())
        instance.launch = AsyncMock()
        instance.stop_children = AsyncMock()
        instance.close_maintenance = AsyncMock()
        messages = []
        async def ready(deadline):
            self.assertFalse(any('READY=1' in value for value in messages))
            self.assertGreater(deadline, time.monotonic())
            instance.stopping.set()
            return 'guarded'
        instance.wait_initial_readiness = ready
        instance.snapshot = Mock(return_value={})
        with patch.object(supervisor, 'extending_startup', return_value=nullcontext(time.monotonic() + 10)), patch.object(supervisor, 'notify', side_effect=messages.append), patch.object(supervisor, 'read_active_execution', return_value=SimpleNamespace(execution=object())), patch.object(supervisor.state, '_atomic_write_json'):
            await instance.run_owned()
        self.assertEqual(sum('READY=1' in value for value in messages), 1)
        self.assertIn('guarded runtime is ready', '\n'.join(messages))
        instance.server.close.assert_awaited_once()
        instance.stop_children.assert_awaited_once()

    async def test_failed_initial_launch_does_not_report_healthy_supervisor(self):
        instance = self.waiting_supervisor()
        instance.server = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        instance._deployment = Mock(return_value=object())
        instance.launch = AsyncMock(side_effect=ControlError('control_bundle_mismatch'))
        instance.stop_children = AsyncMock()
        instance.close_maintenance = AsyncMock()
        instance.start_maintenance = AsyncMock()
        instance.wait_initial_readiness = AsyncMock()
        with patch.object(supervisor, 'extending_startup', return_value=nullcontext(time.monotonic() + 10)), patch.object(supervisor, 'notify') as notify, patch.object(supervisor, 'read_active_execution', return_value=SimpleNamespace(execution=object())):
            with self.assertRaisesRegex(ControlError, 'control_bundle_mismatch'):
                await instance.run_owned()
        self.assertFalse(any('READY=1' in call.args[0] for call in notify.call_args_list))
        instance.wait_initial_readiness.assert_not_awaited()
        instance.start_maintenance.assert_not_awaited()


class BackendReadinessPeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_actual_parent_in_runtime_unit_can_read_narrow_projection(self):
        instance = backend_control.BackendControl.__new__(backend_control.BackendControl)
        execution = parse_management_request(request_payload()).previous
        admission = SimpleNamespace(execution=execution)
        projection = {'generalReady': False, 'guardedReady': True}
        with patch.object(backend_control.os, 'getppid', return_value=200), patch('lifecycle.control.processes.process_cgroup', return_value='/system.slice/streamhome.service'), patch.object(backend_control, 'require_runtime_admission', return_value=admission), patch.object(backend_control, 'get_startup_readiness', return_value=object()), patch.object(backend_control, 'current_readiness_projection', return_value=projection):
            result = await instance.handle(Peer(200, os.geteuid(), os.getegid()), 'readiness', {})
            self.assertEqual(result['execution_sha256'], contract.execution_tuple_sha256(execution))
            self.assertEqual(result['readiness'], projection)
            for peer, arguments in ((Peer(201, os.geteuid(), os.getegid()), {}), (Peer(200, os.geteuid() + 1, os.getegid()), {}), (Peer(200, os.geteuid(), os.getegid()), {'renew': True})):
                with self.subTest(peer=peer, arguments=arguments), self.assertRaisesRegex(ControlError, 'control_peer_forbidden'):
                    await instance.handle(peer, 'readiness', arguments)


if __name__ == '__main__':
    unittest.main()
