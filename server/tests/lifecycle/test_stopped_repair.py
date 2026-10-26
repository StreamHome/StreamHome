from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle import management_controller as controller, management_journal as contract, management_store, state
from lifecycle.control import stopped_repair as stopped, runtime_preparation as preparation, repair
from lifecycle.control import workers
from lifecycle.control import processes
from lifecycle.control.rpc import ControlError
from lifecycle.control.requests import parse_management_request
from tests.lifecycle.test_runtime_preparation import grant, replacement
from tests.lifecycle import test_configuration_store as configuration_fixture
from lifecycle import execution_state, runtime_links


def offline_grant():
    value = grant()
    value.update(schema_version=2, supervisor=None, backend=None, supervisor_invocation_id=None,
        offline_scope={'unit': 'streamhome-install-' + value['installation_id'] + '-' + '2' * 32 + '.scope',
            'invocation': '3' * 32, 'pid': 100, 'start_identity': '200'})
    return value


class StoppedPreparationTests(unittest.TestCase):
    def test_offline_preparation_has_no_backend_or_start_permission(self):
        value = offline_grant()
        self.assertEqual(preparation.parse_grant(value), value)
        for changed in ({'launch': True}, {'supervisor': {'pid': 100, 'start_identity': '200'}},
                        {'supervisor_invocation_id': '1' * 32}, {'schema_version': 1}):
            with self.subTest(changed=changed), self.assertRaises((ControlError, ValueError)):
                preparation.parse_grant({**value, **changed})

    def test_scope_changed_invocation_dead_issuer_or_foreign_cgroup_is_rejected(self):
        value = offline_grant();scope = value['offline_scope']
        expected = 'ActiveState=active\nTransient=yes\nControlGroup=/system.slice/' + scope['unit'] + '\nInvocationID=' + scope['invocation']
        with patch.object(stopped, 'process_identity', return_value=('200', 'S')), patch.object(stopped.Path, 'stat', return_value=SimpleNamespace(st_uid=0)), patch.object(stopped.Path, 'read_text', return_value='0::/system.slice/' + scope['unit'] + '\n'), patch.object(stopped, 'systemctl', return_value=expected) as ctl:
            stopped.require_scope(scope, value['installation_id'])
            ctl.return_value = expected.replace(scope['invocation'], '4' * 32)
            with self.assertRaises(ControlError):stopped.require_scope(scope, value['installation_id'])
        with patch.object(stopped, 'process_identity', return_value=('200', 'Z')):
            with self.assertRaises(ControlError):stopped.require_scope(scope, value['installation_id'])

    def test_stopped_probe_rejects_queued_start_or_live_runtime(self):
        for observed in ('ActiveState=active\nMainPID=100\nJob=', 'ActiveState=inactive\nMainPID=0\nJob=12/start'):
            with self.subTest(observed=observed), patch.object(stopped, 'systemctl', return_value=observed):
                with self.assertRaises(ControlError):stopped.require_stopped()

    def test_initial_stopped_repair_hands_off_before_grant_or_console_mutation(self):
        value = offline_grant()
        selected = SimpleNamespace(status=SimpleNamespace(value='ready'), installation_id=value['installation_id'])
        snapshot = {'lifecycle': {'active_operation': None}}
        with patch.object(repair, 'require_root'), patch.object(repair, 'descriptor', return_value=selected), patch.object(repair, 'root_stop_epoch', return_value=value['stop_epoch']), patch.object(repair, 'run_worker', return_value=snapshot), patch.object(repair, 'process_snapshot', return_value={'initial_service_state': 'stopped'}), patch.object(repair, '_prepare') as build, patch.object(repair, 'open_console') as console:
            result = repair.repair(request_path=None, control_manifest_sha256='a' * 64)
        self.assertIsInstance(result, stopped.StoppedRepairHandoff)
        self.assertEqual(result.stop_epoch, value['stop_epoch'])
        build.assert_not_called();console.assert_not_called()

    def test_scope_wrapper_uses_fixed_dispatcher_and_stop_propagation(self):
        value = offline_grant()
        handoff = stopped.StoppedRepairHandoff(value['installation_id'], value['preparation_id'], value['stop_epoch'], None)
        with patch.object(stopped, 'require_root'), patch.object(stopped.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(stopped.run_scoped(handoff), 0)
        arguments = run.call_args.args[0]
        self.assertIn('--property=StopPropagatedFrom=streamhome.service', arguments)
        self.assertIn('--repair-stopped-scoped', arguments)
        self.assertIn('/usr/local/libexec/streamhome/provision', arguments)
        self.assertNotIn('start', arguments)

    def test_control_handoff_uses_existing_cancellable_scope_and_fixed_artifact_argument(self):
        value=offline_grant()
        handoff=stopped.StoppedRepairHandoff(value['installation_id'],value['preparation_id'],value['stop_epoch'],None,
            control_artifact=Path('/explicit/local/control.deb'))
        with patch.object(stopped,'require_root'),patch.object(stopped.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(stopped.run_scoped(handoff),0)
        arguments=run.call_args.args[0]
        self.assertIn('--maintain-control-scoped',arguments)
        self.assertIn('--property=StopPropagatedFrom=streamhome.service',arguments)
        self.assertEqual(arguments[arguments.index('--artifact')+1],'/explicit/local/control.deb')
        self.assertNotIn('start',arguments)
        with patch.object(stopped,'require_root'),patch.object(stopped.subprocess,'run') as spawn:
            with self.assertRaises(ControlError):stopped.run_scoped(replace(handoff,recovery_action='continue_recovery'))
        spawn.assert_not_called()

    def test_bound_control_handoff_loads_explicit_rescue_without_global_pointer_or_start_borrowing(self):
        from lifecycle.control import privileged
        value=offline_grant()
        handoff=stopped.StoppedRepairHandoff(value['installation_id'],value['preparation_id'],value['stop_epoch'],None,
            recovery_action='repair_bound_control',control_artifact=Path('/explicit/local/control.deb'))
        with patch.object(stopped,'require_root'),patch.object(privileged,'_RESCUE_MODE',True), \
                patch.object(stopped.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(stopped.run_scoped(handoff),0)
        arguments=run.call_args.args[0]
        self.assertIn('--repair-control-scoped',arguments)
        self.assertNotIn('--rescue',arguments)
        self.assertNotIn('--maintain-control-scoped',arguments)
        self.assertNotIn('start',arguments)
        self.assertIn('--property=StopPropagatedFrom=streamhome.service',arguments)
        with patch.object(stopped,'require_root'),patch.object(stopped.subprocess,'run') as run, \
                self.assertRaises(ControlError):stopped.run_scoped(replace(handoff,control_artifact=None))
        run.assert_not_called()

    def test_old_recovery_scope_cannot_clean_up_a_newer_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for generation, name in ((2, 'old.scope'), (10, 'new.scope')):
                (root / f'recovery-{generation}.json').write_text(json.dumps({'issuer_scope': {'unit': name}}))
            with patch('lifecycle.control.tickets.read_protected_json', side_effect=lambda path: json.loads(path.read_text())):
                self.assertEqual(stopped._latest_recovery_scope(root), 'new.scope')

    def test_orphan_or_foreign_root_parent_cannot_masquerade_as_a_reader(self):
        prefix = b'/usr/bin/python3\0-I\0/usr/local/libexec/streamhome/provision\0'
        child = Mock();child.__truediv__ = Mock(return_value=SimpleNamespace(read_bytes=lambda: prefix + b'--worker\0inspect\0'))
        parent = Mock();parent.stat.return_value.st_uid = 0
        parent.__truediv__ = Mock(return_value=SimpleNamespace(read_bytes=lambda: prefix + b'--cli\0doctor\0'))
        proc = Mock();proc.__truediv__ = Mock(return_value=parent)
        fields = ['S', '100']
        with patch.object(processes, 'Path', return_value=proc):
            self.assertTrue(processes._protected_reader(child, fields))
            self.assertFalse(processes._protected_reader(child, ['S', '1']))
            parent.__truediv__.return_value = SimpleNamespace(read_bytes=lambda: b'/sbin/init\0')
            self.assertFalse(processes._protected_reader(child, fields))

    def test_root_scope_module_imports_no_authority_or_application_code(self):
        code = "import sys;import lifecycle.control.stopped_repair;assert not {'lifecycle.state','lifecycle.management_journal','config','application','models','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class StoppedControllerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = configuration_fixture.ConfigurationStoreTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.directory = self.root / 'execution-state';self.directory.mkdir(mode=0o700)
        (self.root / 'web').mkdir()
        self.value = offline_grant()
        previous = contract.parse_execution_tuple(self.value['previous'])
        previous = replace(previous, deployment_revision=self.fixture.initial.revision, policy_revisions=(), provider_revisions=())
        self.previous, self.candidate = previous, replacement(previous)
        for name, target in (('node_modules', previous.web_dependency_target), ('dist', previous.web_dist_target)):
            (self.root / 'web' / name).symlink_to(target)
        self.layout = SimpleNamespace(state=self.directory, domains=self.fixture.directory, managed=True,
            descriptor=SimpleNamespace(descriptor=SimpleNamespace(installation_id=self.fixture.identity, service_uid=os.geteuid())))
        self.stack = self.enterContext(ExitStack())
        for module in (execution_state, runtime_links):self.stack.enter_context(patch.object(module, 'layout_for_root', return_value=self.layout))
        self.stack.enter_context(patch.object(controller, 'ROOT', self.root))
        self.stack.enter_context(patch.object(runtime_links, 'verify_runtime'))
        self.verify = self.stack.enter_context(patch.object(controller, 'verify_runtime'))
        self.stopped = self.stack.enter_context(patch.object(stopped, 'require_stopped'))
        self.quiet = self.stack.enter_context(patch('lifecycle.control.processes.require_service_quiescent'))
        state._atomic_write_json(self.directory / 'active-runtime.json', execution_state.ExecutionRecord(self.fixture.identity, previous, str(uuid.uuid4()), 0).payload())

    def make_controller(self):
        from tests.lifecycle.test_management_requests import request_payload
        payload = request_payload()
        payload.update(installation_id=self.fixture.identity, operation_id=str(uuid.uuid4()), expected_generation=0,
            action='repair_environment', previous=contract.execution_tuple_payload(self.previous),
            candidate=contract.execution_tuple_payload(self.candidate), expected_desired=self.fixture.initial.reference.payload(),
            deployment=self.fixture.initial.settings.payload(), initial_service_state='stopped',
            supervisor_invocation_id=None, supervisor=None, backend=None)
        value = controller.ManagementController.__new__(controller.ManagementController)
        value.review = parse_management_request(payload)
        value.store = self.fixture.store;value.invocation = '2' * 32
        value.lease = value.claim = value.journal = None;value.preparing = False
        value.checkpoint = lambda: value.lease.require_current() if value.lease else None
        value.wait_os_receipt = Mock()
        value.rpc = Mock(side_effect=AssertionError('stopped repair cannot launch or interrupt a supervisor'))
        self.stack.enter_context(patch.object(controller, 'read_os_receipt', return_value={'stage': 'complete'}))
        self.addCleanup(lambda: value.lease.release() if value.lease else None)
        return value

    def test_real_authority_accepts_complete_inactive_runtime_without_starting_application(self):
        value = self.make_controller();value.execute()
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(value.journal.stage, contract.ManagementStage.SUCCEEDED)
        self.assertIsNotNone(value.journal.acceptance)
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.candidate)
        value.rpc.assert_not_called()
        self.assertGreaterEqual(self.stopped.call_count, 3)

    def control_controller(self, *, claim=True):
        value=self.make_controller()
        self.candidate=replace(self.previous,control_bundle_id=str(uuid.uuid4()),control_manifest_sha256='f'*64)
        value.review=replace(value.review,action=contract.ManagementAction.PROVISION_CONTROL,candidate=self.candidate,
            controller_control_manifest_sha256=self.previous.control_manifest_sha256)
        if not claim:return value
        value.lease,value.claim=state.claim_management_controller(self.root,operation_id=value.review.operation_id,expected_generation=0)
        journal=contract.ManagementJournal(value.review.installation_id,value.review.operation_id,value.claim.state.generation,
            value.review.action,value.review.activation_mode,value.review.review_id,self.previous,self.candidate,
            'stopped',value.invocation,None,contract.ManagementStage.RESERVED,None)
        value.journal=management_store.create_management_journal(self.root,journal,lease=value.lease)
        value.advance(contract.ManagementStage.PREPARED)
        return value

    def test_qualified_initial_stopped_control_runs_through_original_claim_and_acceptance(self):
        value=self.control_controller(claim=False)
        with patch('lifecycle.control.reader_support.require_control_maintenance_reader') as reader, \
                patch('lifecycle.control.control_selection.require_selected',return_value={'manifest_sha256':'f'*64}):
            value.execute()
        self.assertEqual(reader.call_count,2)
        self.assertEqual(value.journal.stage,contract.ManagementStage.SUCCEEDED)
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(execution_state.read_active_execution(self.root).execution,self.candidate)
        value.rpc.assert_not_called()

    def test_stopped_control_accepts_only_verified_root_selection_without_any_supervisor_rpc(self):
        value=self.control_controller()
        with patch('lifecycle.control.control_selection.require_selected',return_value={'manifest_sha256':'f'*64}) as selected:
            value.finish_stopped_repair(recovered=False)
        self.assertEqual(execution_state.read_active_execution(self.root).execution,self.candidate)
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(value.journal.stage,contract.ManagementStage.SUCCEEDED)
        self.assertEqual(selected.call_count,3)
        value.rpc.assert_not_called()

    def test_wrong_root_control_selection_cannot_accept_the_inactive_candidate(self):
        value=self.control_controller()
        with patch('lifecycle.control.control_selection.require_selected',side_effect=ControlError('selected reader changed')):
            with self.assertRaises(ControlError):value.finish_stopped_repair(recovered=False)
        value.retain_recovery()
        self.assertIsNone(value.journal.acceptance)
        self.assertEqual(execution_state.read_active_execution(self.root).execution,self.previous)
        self.assertEqual(value.journal.stage,contract.ManagementStage.RECOVERY_REQUIRED)
        value.rpc.assert_not_called()

    def test_stopped_control_bridge_requires_full_quiescence_before_returning_os_decision(self):
        value=self.control_controller();lease=value.lease.lease
        before=self.fixture.paths.state_path.read_bytes()
        with patch.object(workers,'ROOT',self.root),patch('lifecycle.control.requests.read_management_request',return_value=value.review), \
                patch.object(processes,'controller_peer'):
            result=workers.bridge_observe({'operation_id':value.review.operation_id,'controller_pid':lease.pid,
                'controller_invocation_id':value.invocation})
            self.assertTrue(result['os_effect_requested'])
            self.assertEqual(result['selected_control']['control_manifest_sha256'],self.candidate.control_manifest_sha256)
            self.quiet.assert_called_once_with(inspections=True,
                controller=(value.review.operation_id,lease.pid,lease.process_start_identity))
            self.quiet.side_effect=ControlError('foreign service work')
            with self.assertRaises(ControlError):
                workers.bridge_observe({'operation_id':value.review.operation_id,'controller_pid':lease.pid,
                    'controller_invocation_id':value.invocation})
        self.assertEqual(self.fixture.paths.state_path.read_bytes(),before)

    def test_live_foreign_work_refuses_activation_and_keeps_previous_execution(self):
        value = self.make_controller();self.quiet.side_effect = ControlError('control_foreign_service_process')
        with self.assertRaises(ControlError):value.execute()
        value.retain_recovery()
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.previous)
        self.assertEqual(value.journal.stage, contract.ManagementStage.RECOVERY_REQUIRED)
        self.assertIsNone(value.journal.acceptance)
        value.rpc.assert_not_called()

    def recovering(self, original):
        from lifecycle import management_recovery_store as recovery
        authority = state.inspect_state(self.root)
        plan = recovery.inspect_recovery(self.root, original.journal.operation_id, authority.generation)
        original.lease.release()
        value = controller.RecoveredManagementController.__new__(controller.RecoveredManagementController)
        value.review = replace(original.review, expected_generation=authority.generation)
        value.store = self.fixture.store;value.invocation = '3' * 32
        value.grant = {'schema_version': 1, 'source_evidence_sha256': plan['evidence_sha256'], 'recovery_review_id': str(uuid.uuid4()), 'action': 'continue_recovery'}
        value.lease = value.claim = value.journal = None
        value.checkpoint = lambda: value.lease.require_current() if value.lease else None
        value.wait_os_receipt = Mock();value.rpc = Mock(side_effect=AssertionError('stopped recovery cannot launch a supervisor'))
        self.addCleanup(lambda: value.lease.release() if value.lease else None)
        return value

    def test_interrupted_unaccepted_offline_repair_reverifies_and_completes_without_launch(self):
        first = self.make_controller();self.verify.side_effect = [None, OSError('verification interrupted')]
        with self.assertRaises(OSError):first.execute()
        first.retain_recovery()
        self.assertIsNone(first.journal.acceptance)
        second = self.recovering(first);self.verify.side_effect = None
        second.execute()
        self.assertIsNotNone(second.journal.acceptance)
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.candidate)
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        second.rpc.assert_not_called()

    def test_terminal_stopped_repair_gap_preserves_candidate_acceptance(self):
        first = self.make_controller();first.complete = Mock(side_effect=OSError('completion interrupted'))
        with self.assertRaises(OSError):first.execute()
        acceptance = first.journal.acceptance
        second = self.recovering(first);second.execute()
        self.assertEqual(second.journal.acceptance, acceptance)
        self.assertEqual(second.journal.stage, contract.ManagementStage.SUCCEEDED)
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        second.rpc.assert_not_called()

    def test_unresolvable_environment_links_are_diagnosed_without_using_that_environment(self):
        with patch.object(workers, 'ROOT', self.root), patch.object(workers, 'layout_for_root', return_value=self.layout), patch.object(workers, 'verify_runtime', side_effect=RuntimeError('symlink loop')):
            snapshot = workers.inspect_managed()
        self.assertFalse(snapshot['runtime_verified'])
        self.assertEqual(snapshot['runtime_error'], 'runtime_verification_failed')
        self.assertEqual(snapshot['execution']['execution'], contract.execution_tuple_payload(self.previous))


if __name__ == '__main__':unittest.main()
