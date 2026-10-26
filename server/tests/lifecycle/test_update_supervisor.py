"""Actual canonical UPDATE state with explicit substituted kernel/unit records."""
import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from lifecycle import managed_update, state, update_controller
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from lifecycle.control import supervisor
from lifecycle.control.rpc import ControlError, Peer
from tests.lifecycle.test_update_delegation import UpdateDelegateFixture


class UpdateSupervisorFixture(UpdateDelegateFixture):
    cutover = True
    preflight = False
    def setUp(self):
        super().setUp()
        self.sup_pid = self.main+100002
        payload = update_controller.read_update_journal(self.path)
        payload['managed_execution'].update(supervisor_pid=self.sup_pid, supervisor_start_identity='98765')
        candidate = managed_update.prepared_execution(self.parent.execution, payload['target_commit'], self.built)
        payload['managed_candidate'] = execution_tuple_payload(candidate)
        self.path.write_text(json.dumps(payload))
        if self.preflight:
            payload.update(lifecycle_phase='reserved', phase='preflight', managed_candidate=None)
        elif self.cutover:
            state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
                expected_phase='reserved', next_phase='exclusive')
            payload.update(lifecycle_phase='exclusive', phase='installing')
        else:
            self.lease.release()
            payload.update(lifecycle_phase='reserved', phase='queued', managed_candidate=None)
            payload['managed_execution']['original_generation'] = 2
        self.path.write_text(json.dumps(payload))
        self.parent = managed_update.bound_journal(self.root, self.operation)[2].parent
        self.enterContext(patch.object(managed_update.os, 'getpid', return_value=self.sup_pid))
        real_start = state._process_start_identity
        self.enterContext(patch.object(state, '_process_start_identity',
            side_effect=lambda pid: '98765' if pid == self.sup_pid else real_start(pid)))
        self.enterContext(patch.object(supervisor, 'ROOT', self.root))
        self.enterContext(patch.object(supervisor, 'read_stop_epoch', return_value=self.parent.stop_epoch))
        active = SimpleNamespace(installation_id=self.parent.installation_id, execution=self.parent.execution)
        self.active = active
        self.enterContext(patch.object(supervisor, 'read_active_execution', return_value=active))
        self.enterContext(patch.object(supervisor, 'runtime_children', return_value=set()))
        obj = supervisor.Supervisor.__new__(supervisor.Supervisor)
        obj.resumption_binding = None
        obj.launches_open = True;obj.installation = self.parent.installation_id
        obj.invocation = self.parent.supervisor_invocation_id
        obj.control_bundle_id = self.parent.execution.control_bundle_id
        obj.control_manifest_sha256 = self.parent.execution.control_manifest_sha256
        obj.execution = self.parent.execution;obj.children = {};obj.lock = asyncio.Lock()
        obj.stop_children = AsyncMock();obj.start_maintenance = AsyncMock()
        self.sup = obj
        self.peer = Peer(self.helper, os.geteuid(), os.getegid())
        self.arguments = {'operation_id': self.operation, 'generation': 2}



class UpdateSupervisorTests(UpdateSupervisorFixture, unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_parent_and_delegate_can_stop_owned_children_without_os_restart(self):
        before = state.inspect_state(self.root)
        result = await self.sup.handle(self.peer, 'update_stop_children', self.arguments)
        self.assertEqual(result, {'stopped': True})
        self.sup.stop_children.assert_awaited_once();self.sup.start_maintenance.assert_awaited_once()
        self.assertEqual(state.inspect_state(self.root), before)
        self.assertEqual(await self.sup.handle(self.peer, 'update_assert_stopped', self.arguments), {'stopped': True})

    async def test_changed_supervisor_execution_incarnation_or_generation_never_stops_children(self):
        original = self.sup.invocation
        self.sup.invocation = 'f'*32
        with self.assertRaises(ControlError):await self.sup.handle(self.peer, 'update_stop_children', self.arguments)
        self.sup.invocation = original
        with self.assertRaises(ControlError):
            await self.sup.handle(self.peer, 'update_stop_children', {**self.arguments, 'generation': 3})
        self.active.installation_id = 'foreign'
        with self.assertRaises(ControlError):await self.sup.handle(self.peer, 'update_stop_children', self.arguments)
        self.sup.stop_children.assert_not_awaited()

    async def test_unknown_children_and_cancellation_refuse_quiescence_or_maintenance(self):
        self.sup.children = {'unexpected': object()}
        for action in ('update_assert_stopped', 'update_maintenance'):
            with self.subTest(action=action), self.assertRaises(ControlError):
                await self.sup.handle(self.peer, action, self.arguments)
        self.sup.children = {};self.sup.launches_open = False
        with self.assertRaises(ControlError):await self.sup.handle(self.peer, 'update_stop_children', self.arguments)
        self.sup.stop_children.assert_not_awaited();self.sup.start_maintenance.assert_not_awaited()

    async def test_original_owner_can_request_quiescence_but_foreign_uid_cannot(self):
        owner = Peer(self.main, os.geteuid(), os.getegid())
        self.assertEqual(await self.sup.handle(owner, 'update_assert_stopped', self.arguments), {'stopped': True})
        foreign = Peer(self.helper, os.geteuid()+1, os.getegid())
        with self.assertRaises(ControlError):await self.sup.handle(foreign, 'update_stop_children', self.arguments)
        self.sup.stop_children.assert_not_awaited()


if __name__ == '__main__':unittest.main()


class UpdateGuardedLaunchTests(UpdateSupervisorFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        # This suite qualifies custody/launch; SQLite uses its dedicated real-file suite.
        self.database_launch = self.enterContext(patch('lifecycle.update_checkpoint.require_launch_database'))
        self.sup.wait_update_guarded = AsyncMock()

    def verifying(self):
        candidate = managed_update.bound_journal(self.root, self.operation)[2].candidate
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='exclusive', next_phase='verifying')
        payload = update_controller.read_update_journal(self.path)
        payload.update(lifecycle_phase='verifying', phase='starting',
            managed_verification_execution_sha256=execution_tuple_sha256(candidate))
        self.path.write_text(json.dumps(payload))
        return candidate

    async def test_rpc_launches_only_the_selected_candidate_and_refuses_previous_before_effects(self):
        candidate = self.verifying()
        self.sup.close_maintenance = AsyncMock();self.sup.launch = AsyncMock()
        with self.assertRaises(ControlError):
            await self.sup.handle(self.peer, 'update_start_previous', self.arguments)
        self.sup.close_maintenance.assert_not_awaited();self.sup.launch.assert_not_awaited()
        result = await self.sup.handle(self.peer, 'update_start_candidate', self.arguments)
        self.assertEqual(result, {'launched': True, 'build_id': candidate.application_build_id})
        self.sup.wait_update_guarded.assert_awaited_once_with(self.peer, self.arguments)
        args, kwargs = self.sup.launch.await_args
        self.assertEqual(args, (candidate,))
        self.assertEqual(kwargs['update'], self.sup._update_controller(self.peer, self.arguments))
        self.assertEqual(kwargs['authorize'](), kwargs['update'])

    async def test_database_failure_prevents_guarded_process_and_maintenance_changes(self):
        self.verifying()
        self.sup.close_maintenance = AsyncMock();self.sup.launch = AsyncMock()
        self.database_launch.side_effect = ControlError('control_update_checkpoint_unbound')
        with self.assertRaises(ControlError):
            await self.sup.handle(self.peer, 'update_start_candidate', self.arguments)
        self.sup.close_maintenance.assert_not_awaited();self.sup.launch.assert_not_awaited()

    async def test_launch_propagates_private_guarded_token_and_rechecks_owner_before_spawning(self):
        from lifecycle.installation import InstallationStatus
        candidate = self.verifying()
        update = self.sup._update_controller(self.peer, self.arguments)
        descriptor = SimpleNamespace(installation_id=self.parent.installation_id,
            service_uid=os.geteuid(), status=InstallationStatus.READY)
        layout = SimpleNamespace(descriptor=SimpleNamespace(descriptor=descriptor))
        self.sup._deployment = Mock(return_value=object());self.sup._environment = Mock(return_value={})
        self.sup._spawn = Mock();self.sup.sequence = 0
        with patch.object(supervisor, 'layout_for_root', return_value=layout), patch.object(
                supervisor, 'verify_runtime'), patch.object(supervisor, 'verify_links'), patch.object(
                managed_update, 'read_commit_token', return_value='a'*43):
            with self.assertRaisesRegex(ControlError, 'control_controller_changed'):
                await self.sup.launch(candidate, update=update, authorize=lambda: None)
            self.sup._spawn.assert_not_called()
            await self.sup.launch(candidate, update=update, authorize=lambda: update)
        self.assertEqual(self.sup._spawn.call_count, 2)
        for call in self.sup._spawn.call_args_list:
            environment = call.args[2]
            self.assertEqual(environment['STREAMHOME_LIFECYCLE_STARTUP_MODE'], 'update_verify')
            self.assertEqual(environment['STREAMHOME_UPDATE_TRANSACTION'], self.operation)
            self.assertEqual(environment['STREAMHOME_LIFECYCLE_GENERATION'], '2')
            self.assertEqual(environment['STREAMHOME_UPDATE_COMMIT_TOKEN'], 'a'*43)


class UpdateInitialUnitTests(UpdateSupervisorFixture, unittest.IsolatedAsyncioTestCase):
    cutover = False

    def setUp(self):
        super().setUp()
        self.enterContext(patch('lifecycle.control.reader_support.require_managed_update_reader'))
        self.sup.children = {'backend': SimpleNamespace(process=SimpleNamespace(pid=self.helper), start='12346')}
        self.enterContext(patch('lifecycle.control.administration_lock.process_identity', return_value=('12346', 'S')))
        self.enterContext(patch('lifecycle.control.processes.process_cgroup', return_value='/system.slice/streamhome.service'))

    async def test_only_original_owned_backend_can_request_its_queued_fixed_unit(self):
        observed = self.sup._update_reservation(self.peer, self.arguments)
        self.assertEqual(observed[2].parent, self.parent)
        for peer in (Peer(self.main, os.geteuid(), os.getegid()), Peer(self.helper, os.geteuid()+1, os.getegid())):
            with self.assertRaises(ControlError):self.sup._update_reservation(peer, self.arguments)
        self.sup.invocation = 'f'*32
        with self.assertRaises(ControlError):self.sup._update_reservation(self.peer, self.arguments)

    async def test_already_active_operation_unit_is_never_started_or_retried(self):
        with patch.object(supervisor.subprocess, 'run') as command:
            with self.assertRaisesRegex(ControlError, 'control_controller_busy'):
                await self.sup.handle(self.peer, 'start_update_operation', self.arguments)
            command.assert_not_called()
        self.sup.stop_children.assert_not_awaited()

    async def test_start_uses_only_fixed_unit_and_returns_separately_verified_canonical_owner(self):
        authority = state.inspect_state(self.root);started = False
        expected = {'operation_id': self.operation, 'generation': 3, 'controller_pid': self.main}
        real_inspect = state.inspect_state
        def current(root):
            return state.LifecycleState(2, 3, authority.active_operation) if started else real_inspect(root)
        def start(command, **kwargs):
            nonlocal started
            self.assertEqual(command, ['/usr/bin/systemctl', '--no-pager', 'start',
                'streamhome-operation@'+self.operation+'.service'])
            self.assertEqual(kwargs['cwd'], '/')
            started = True
            return SimpleNamespace(returncode=0)
        with patch('lifecycle.control.administration.operation_unit', return_value={
                'ActiveState': 'inactive', 'MainPID': '0'}), patch.object(
                supervisor.subprocess, 'run', side_effect=start) as command, patch.object(
                state, 'inspect_state', side_effect=current), patch.object(
                self.sup, '_initial_update_owner', return_value=expected) as owner:
            result = await self.sup.handle(self.peer, 'start_update_operation', self.arguments)
        self.assertEqual(result, expected);self.assertEqual(command.call_count, 1)
        owner.assert_called_once();self.sup.stop_children.assert_not_awaited()
        self.assertEqual(real_inspect(self.root), authority)  # The OS/owner projection above was substituted.

    async def test_operator_stop_during_unit_start_never_authorizes_a_second_start_or_child_launch(self):
        def stop(*args, **kwargs):
            self.sup.launches_open = False
            return SimpleNamespace(returncode=0)
        with patch('lifecycle.control.administration.operation_unit', return_value={
                'ActiveState': 'inactive', 'MainPID': '0'}), patch.object(
                supervisor.subprocess, 'run', side_effect=stop) as command:
            with self.assertRaisesRegex(ControlError, 'control_operator_stopped'):
                await self.sup.handle(self.peer, 'start_update_operation', self.arguments)
        self.assertEqual(command.call_count, 1);self.sup.stop_children.assert_not_awaited()

    async def test_transferred_generation_waits_for_exact_owner_publication_without_acknowledging_partial_evidence(self):
        authority = state.inspect_state(self.root);started = False
        expected = {'operation_id': self.operation, 'generation': 3, 'controller_pid': self.main}
        real_inspect = state.inspect_state
        def current(root):
            return state.LifecycleState(2, 3, authority.active_operation) if started else real_inspect(root)
        def start(*args, **kwargs):
            nonlocal started
            started = True
            return SimpleNamespace(returncode=0)
        with (patch('lifecycle.control.administration.operation_unit', return_value={'ActiveState': 'inactive', 'MainPID': '0'}),
                patch.object(supervisor.subprocess, 'run', side_effect=start) as command,
                patch.object(state, 'inspect_state', side_effect=current),
                patch.object(self.sup, '_initial_update_owner', side_effect=[
                    update_controller.UpdateControllerConflict('Subordinate generation pending'),
                    ControlError('control_controller_changed'), expected]) as owner,
                patch.object(supervisor.asyncio, 'sleep', new_callable=AsyncMock) as sleep):
            result = await self.sup.handle(self.peer, 'start_update_operation', self.arguments)
        self.assertEqual(result, expected);self.assertEqual(owner.call_count, 3)
        self.assertEqual(command.call_count, 1);self.assertEqual(sleep.await_count, 2)
        self.sup.stop_children.assert_not_awaited()

    async def test_unpublished_transferred_owner_has_a_deadline_and_never_causes_another_os_start(self):
        authority = state.inspect_state(self.root);started = False
        real_inspect = state.inspect_state
        def current(root):
            return state.LifecycleState(2, 3, authority.active_operation) if started else real_inspect(root)
        def start(*args, **kwargs):
            nonlocal started
            started = True
            return SimpleNamespace(returncode=0)
        with (patch('lifecycle.control.administration.operation_unit', return_value={'ActiveState': 'inactive', 'MainPID': '0'}),
                patch.object(supervisor.subprocess, 'run', side_effect=start) as command,
                patch.object(state, 'inspect_state', side_effect=current),
                patch.object(self.sup, '_initial_update_owner', side_effect=ControlError('control_controller_changed')),
                patch.object(supervisor.time, 'monotonic', side_effect=[0, 11])):
            with self.assertRaisesRegex(ControlError, 'control_controller_start_timeout'):
                await self.sup.handle(self.peer, 'start_update_operation', self.arguments)
        self.assertEqual(command.call_count, 1);self.sup.stop_children.assert_not_awaited()


class UpdatePreflightReadinessTests(UpdateSupervisorFixture, unittest.IsolatedAsyncioTestCase):
    preflight = True

    def setUp(self):
        super().setUp()
        self.peer = Peer(self.main, os.geteuid(), os.getegid())
        self.sup.selected_readiness = Mock(return_value='general')

    async def test_only_original_owner_can_prove_existing_general_transport_before_retirement(self):
        before = state.inspect_state(self.root)
        result = await self.sup.handle(self.peer, 'update_preflight_readiness', self.arguments)
        self.assertEqual(result, {'ready': True})
        self.assertEqual(state.inspect_state(self.root), before)
        self.sup.stop_children.assert_not_awaited()
        with self.assertRaises(ControlError):
            await self.sup.handle(Peer(self.helper, os.geteuid(), os.getegid()), 'update_preflight_readiness', self.arguments)

    async def test_guarded_or_missing_readiness_never_confirms_original_service_preservation(self):
        for value in ('guarded', None):
            self.sup.selected_readiness.return_value = value
            with self.subTest(value=value):
                self.assertEqual(await self.sup.handle(self.peer, 'update_preflight_readiness', self.arguments), {'ready': False})
        def stopped():
            self.sup.launches_open = False
            return 'general'
        self.sup.selected_readiness.side_effect = stopped
        with self.assertRaises(ControlError):await self.sup.handle(self.peer, 'update_preflight_readiness', self.arguments)


class UpdateGuardedReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        self.sup.launches_open = True
        self.sup.children = {name: SimpleNamespace(process=SimpleNamespace(pid=pid))
            for name, pid in (('backend', 123), ('web', 124))}
        self.sup._update_controller = Mock()
        self.sup.selected_readiness = Mock(side_effect=[None, 'general', 'guarded'])
        self.peer = Peer(125, os.geteuid(), os.getegid())
        self.arguments = {'operation_id': 'test', 'generation': 2}
        self.waitid = self.enterContext(patch.object(supervisor.os, 'waitid', return_value=None))
        self.sleep = self.enterContext(patch.object(supervisor.asyncio, 'sleep', new_callable=AsyncMock))

    async def test_waits_for_guarded_readiness_and_rechecks_controller_after_each_probe(self):
        await self.sup.wait_update_guarded(self.peer, self.arguments)
        self.assertEqual(self.sup.selected_readiness.call_count, 3)
        self.assertEqual(self.sup._update_controller.call_count, 6)
        self.assertEqual(self.sleep.await_count, 2)

    async def test_transient_socket_unavailability_is_retried(self):
        self.sup.selected_readiness.side_effect = [ControlError('control_unavailable'), 'guarded']
        await self.sup.wait_update_guarded(self.peer, self.arguments)
        self.sleep.assert_awaited_once()

    async def test_wrong_identity_is_never_retried(self):
        self.sup.selected_readiness.side_effect = ControlError('control_runtime_identity_changed')
        with self.assertRaisesRegex(ControlError, 'control_runtime_identity_changed'):
            await self.sup.wait_update_guarded(self.peer, self.arguments)
        self.sleep.assert_not_awaited()

    async def test_dead_child_prevents_readiness_probe(self):
        self.waitid.return_value = object()
        with self.assertRaisesRegex(ControlError, 'control_runtime_exited'):
            await self.sup.wait_update_guarded(self.peer, self.arguments)
        self.sup.selected_readiness.assert_not_called()

    async def test_stop_during_probe_prevents_success_acknowledgement(self):
        def stopped(**kwargs):
            self.sup.launches_open = False
            return 'guarded'
        self.sup.selected_readiness.side_effect = stopped
        with self.assertRaisesRegex(ControlError, 'control_operator_stopped'):
            await self.sup.wait_update_guarded(self.peer, self.arguments)

    async def test_deadline_is_bounded_before_probe(self):
        with patch.object(supervisor.time, 'monotonic', side_effect=[0, 10000]):
            with self.assertRaisesRegex(ControlError, 'control_startup_recovery_timeout'):
                await self.sup.wait_update_guarded(self.peer, self.arguments)
        self.sup.selected_readiness.assert_not_called()
