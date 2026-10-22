from contextlib import ExitStack
from dataclasses import replace
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle import (bootstrap, execution_state, management_controller as controller,
    management_journal as contract, management_recovery_store as recovery, management_store,
    runtime_admission, runtime_links, state)
from lifecycle.control.requests import parse_management_request
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_configuration_store as configuration_fixture
from tests.lifecycle.test_management_journal import _journal_payload
from tests.lifecycle.test_management_requests import request_payload


class PreservingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = configuration_fixture.ConfigurationStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.context = bootstrap.evaluate_startup(self.root, {})
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(controller, 'ROOT', self.root))
        self.state_dir = self.root / 'execution-state'
        self.state_dir.mkdir(mode=0o700)
        (self.root / 'web').mkdir()
        self.layout = SimpleNamespace(state=self.state_dir, managed=True,
            descriptor=SimpleNamespace(descriptor=SimpleNamespace(installation_id=self.fixture.identity)))
        for module in (execution_state, runtime_links):
            self.stack.enter_context(patch.object(module, 'layout_for_root', return_value=self.layout))
        self.leases = []
        self.addCleanup(lambda: [lease.release() for lease in self.leases])

    def start(self, mode='save_only', running=True, publish=True, acknowledge=True):
        self.target = self.fixture._target()
        payload = _journal_payload()
        payload.update(installation_id=self.fixture.identity, activation_mode=mode,
            initial_service_state='running' if running else 'stopped',
            supervisor_invocation_id=payload['supervisor_invocation_id'] if running else None)
        payload['previous']['deployment_revision'] = self.fixture.initial.revision
        payload['candidate']['deployment_revision'] = self.target.revision
        for execution in (payload['previous'], payload['candidate']):
            execution.update(policy_revisions={}, provider_revisions={})
        if mode == 'os_only':
            payload.update(action='provision_control', candidate=payload['previous'].copy())
        self.execution = contract.parse_execution_tuple(payload['previous'])
        self.admission = runtime_admission.RuntimeAdmission(self.context, self.fixture.identity,
            payload['supervisor_invocation_id'] or 'f' * 32, self.execution)
        readiness = bootstrap.StartupReadiness(self.context)
        readiness.record_recovery(asyncio.run(bootstrap.run_mandatory_recovery(self.context, ())))
        readiness.mark_workers_started()
        readiness.mark_general_ready()
        self.admission.settle(readiness)
        lease, claim = state.claim_management_controller(self.root, operation_id=payload['operation_id'], expected_generation=0)
        self.leases.append(lease)
        payload['generation'] = claim.state.generation
        self.source = contract.parse_management_journal(payload)
        management_store.create_management_journal(self.root, self.source, lease=lease)
        if running and acknowledge:
            self.admission.acknowledge_continuation(self.source.operation_id, controller_pid=os.getpid())
        if publish:
            prepared = replace(self.source, stage=contract.ManagementStage.PREPARED)
            self.source = management_store.advance_management_journal(self.root, self.source, prepared, lease=lease)
            if mode == 'save_only':
                self.fixture.store.publish_desired(self.root, self.source, lease=lease,
                    expected=self.fixture.initial.reference, target=self.target.reference)
        self.original = request_payload()
        self.original.update(installation_id=self.source.installation_id, operation_id=self.source.operation_id,
            review_id=self.source.review_id, expected_generation=0, action=self.source.action.value,
            activation_mode=mode, previous=payload['previous'], candidate=payload['candidate'],
            deployment=(self.target.settings if mode == 'save_only' else self.fixture.initial.settings).payload(),
            expected_desired=self.fixture.initial.reference.payload(), initial_service_state=self.source.initial_service_state,
            supervisor_invocation_id=self.source.supervisor_invocation_id, interruption_approved=False,
            supervisor=self.original['supervisor'] if running else None,
            backend={'pid': os.getpid(), 'start_identity': self.admission.start} if running else None)
        self.original = parse_management_request(self.original)
        self.stack.enter_context(patch.object(controller, 'read_management_request', return_value=self.original))
        record = execution_state.ExecutionRecord(self.fixture.identity, self.execution, str(uuid.uuid4()), 0)
        state._atomic_write_json(self.state_dir / 'active-runtime.json', record.payload())
        for name, target in (('node_modules', self.execution.web_dependency_target), ('dist', self.execution.web_dist_target)):
            (self.root / 'web' / name).symlink_to(target)
        lease.release()

    def recovering(self):
        authority = state.inspect_state(self.root)
        plan = recovery.inspect_recovery(self.root, self.source.operation_id, authority.generation)
        self.assertEqual(plan['permitted_actions'], ['continue_recovery'])
        value = controller.RecoveredManagementController.__new__(controller.RecoveredManagementController)
        value.review = replace(self.original, expected_generation=authority.generation)
        value.grant = {'source_evidence_sha256': plan['evidence_sha256'], 'recovery_review_id': str(uuid.uuid4()), 'action': 'continue_recovery'}
        value.invocation = '2' * 32
        value.store = self.fixture.store
        value.lease = value.claim = value.journal = None
        value.checkpoint = lambda: value.lease.require_current() if value.lease else None
        value.wait_os_receipt = Mock()
        value.wait_guarded = Mock(side_effect=AssertionError('preserving recovery must not launch verification'))
        value.commit_and_wait_recovery = Mock(side_effect=AssertionError('preserving recovery must not commit a candidate'))
        def rpc(service, action, **kwargs):
            self.assertEqual((service, action), ('backend', 'ack_recovered_continuation'))
            return self.admission.acknowledge_recovered_continuation(value.journal.operation_id, controller_pid=os.getpid())
        value.rpc = Mock(side_effect=rpc)
        self.addCleanup(lambda: value.lease.release() if value.lease else None)
        return value

    def assert_preserved(self, value, running=True):
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.execution)
        self.assertEqual(self.fixture.store.inspect().active, self.fixture.initial.reference)
        self.assertIsNone(value.journal.acceptance)
        if running:
            self.assertTrue(self.admission.admitted())
            self.assertEqual(self.admission.generation, value.journal.generation)
            self.assertEqual(self.context.generation, 0)
        else:
            value.rpc.assert_not_called()

    def test_save_recovery_keeps_original_process_and_only_publishes_desired(self):
        self.start()
        value = self.recovering()
        value.execute()
        self.assertEqual(value.journal.stage, contract.ManagementStage.SAVED)
        self.assertEqual(self.fixture.store.inspect().desired, self.target.reference)
        self.assert_preserved(value)

    def test_failure_before_original_receipt_and_ack_can_finish_same_reviewed_save(self):
        self.start(publish=False, acknowledge=False)
        value = self.recovering()
        value.execute()
        self.assertEqual(self.fixture.store.inspect().desired, self.target.reference)
        self.assert_preserved(value)

    def test_os_only_recovery_does_not_publish_unrelated_pending_settings(self):
        self.start(mode='os_only')
        value = self.recovering()
        value.execute()
        self.assertEqual(value.journal.stage, contract.ManagementStage.OS_COMPLETED)
        self.assertEqual(self.fixture.store.inspect().desired, self.fixture.initial.reference)
        self.assert_preserved(value)

    def test_stopped_save_recovery_never_issues_backend_or_supervisor_rpc(self):
        self.start(running=False)
        value = self.recovering()
        read_text = Path.read_text
        def no_runtime(path, *args, **kwargs):
            if str(path) == '/sys/fs/cgroup' + controller.RUNTIME_CGROUP + '/cgroup.procs':
                raise FileNotFoundError
            return read_text(path, *args, **kwargs)
        with patch.object(controller.Path, 'read_text', no_runtime):
            value.execute()
        self.assert_preserved(value, running=False)

    def test_failure_after_ack_and_terminal_before_completion_rebinds_original_live_process(self):
        self.start()
        first = self.recovering()
        with patch.object(state, 'complete_operation_with_credential', side_effect=OSError('completion fixture')):
            with self.assertRaises(OSError):
                first.execute()
        self.assertEqual(first.journal.stage, contract.ManagementStage.SAVED)
        first.retain_recovery()
        self.assertFalse(self.admission.admitted())
        first.lease.release()
        second = self.recovering()
        second.execute()
        self.assertEqual(second.journal.generation, 3)
        self.assert_preserved(second)

    def test_restarted_process_cannot_readmit_from_disk_or_an_old_receipt(self):
        self.start()
        value = self.recovering()
        other = runtime_admission.RuntimeAdmission(self.context, self.fixture.identity, self.source.supervisor_invocation_id, self.execution)
        value.rpc.side_effect = lambda service, action, **kwargs: other.acknowledge_recovered_continuation(self.source.operation_id, controller_pid=os.getpid())
        with self.assertRaises(runtime_admission.RuntimeAdmissionError):
            value.execute()
        value.retain_recovery()
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)
        self.assertFalse(other.admitted())
        self.assertFalse(self.admission.admitted())

    def test_changed_active_tuple_or_unacknowledged_backend_never_releases_authority(self):
        self.start()
        value = self.recovering()
        value.rpc.side_effect = None
        value.rpc.return_value = {'acknowledged': True, 'generation': 2, 'pid': os.getpid() + 1}
        with self.assertRaisesRegex(ControlError, 'control_runtime_not_settled'):
            value.execute()
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(self.fixture.store.inspect().active, self.fixture.initial.reference)


if __name__ == '__main__':
    unittest.main()
