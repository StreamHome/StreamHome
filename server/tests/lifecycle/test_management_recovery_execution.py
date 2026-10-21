from dataclasses import replace
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle import execution_state, management_controller as controller, management_journal as contract, management_store, management_recovery_store as recovery, runtime_links, state
from lifecycle.control.requests import parse_management_request
from tests.lifecycle import test_configuration_store as configuration_fixture
from tests.lifecycle.test_management_requests import request_payload


class ManagementRecoveryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = configuration_fixture.ConfigurationStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.state_dir = self.root / 'execution-state'
        self.state_dir.mkdir(mode=0o700)
        (self.root / 'web').mkdir()
        self.target = self.fixture._target()
        self.source = self.fixture._operation(self.target, mode='cutover')
        self.fixture._publish(self.source, self.target)
        self.layout = SimpleNamespace(state=self.state_dir, managed=True,
            descriptor=SimpleNamespace(descriptor=SimpleNamespace(installation_id=self.fixture.identity)))
        for module in (execution_state, runtime_links):
            p = patch.object(module, 'layout_for_root', return_value=self.layout)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(controller, 'ROOT', self.root)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(runtime_links, 'verify_runtime')
        p.start()
        self.addCleanup(p.stop)
        self._execution(self.source.previous)
        for name, target in (('node_modules', self.source.previous.web_dependency_target), ('dist', self.source.previous.web_dist_target)):
            (self.root / 'web' / name).symlink_to(target)
        state._atomic_write_json(self.state_dir / 'runtime-links.json', {'schema_version': 1,
            'operation_id': self.source.operation_id, 'generation': self.source.generation,
            'previous': contract.execution_tuple_sha256(self.source.previous), 'candidate': contract.execution_tuple_sha256(self.source.candidate), 'stage': 'complete'})

    def _execution(self, execution):
        record = execution_state.ExecutionRecord(self.fixture.identity, execution, self.source.operation_id, self.source.generation)
        state._atomic_write_json(self.state_dir / 'active-runtime.json', record.payload())

    def make_controller(self):
        snapshot = recovery.inspect_recovery(self.root, self.source.operation_id, state.inspect_state(self.root).generation)
        self.fixture.lease.release()
        reviewed = request_payload()
        reviewed.update(installation_id=self.source.installation_id, operation_id=self.source.operation_id,
            review_id=self.source.review_id, expected_generation=snapshot['generation'],
            previous=contract.execution_tuple_payload(self.source.previous), candidate=contract.execution_tuple_payload(self.source.candidate),
            deployment=self.target.settings.payload(), expected_desired=self.fixture.initial.reference.payload(),
            supervisor_invocation_id=self.source.supervisor_invocation_id)
        value = controller.RecoveredManagementController.__new__(controller.RecoveredManagementController)
        value.review = parse_management_request(reviewed)
        value.grant = {'source_evidence_sha256': snapshot['evidence_sha256'], 'recovery_review_id': str(uuid.uuid4()), 'action': 'continue_recovery'}
        value.invocation = '2' * 32
        value.store = self.fixture.store
        value.lease = value.claim = value.journal = None
        value.cancelled = value.preparing = False
        value.checkpoint = lambda: value.lease.require_current() if value.lease else None
        value.wait_os_receipt = Mock()
        value.wait_guarded = Mock(return_value={'exact_guarded_candidate': True})
        value.commit_and_wait_recovery = Mock()
        def rpc(service, action, **kwargs):
            if action == 'launch_recovered':
                self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.VERIFYING)
                runtime_links.verify_links(self.root, value.journal.candidate)
                return {'launched': True}
            self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
            return {'stopped': True}
        value.rpc = Mock(side_effect=rpc)
        self.addCleanup(lambda: value.lease.release() if value.lease else None)
        return value

    def accept_source(self):
        exclusive = self.fixture._phase(self.source, 'exclusive', contract.ManagementStage.EXCLUSIVE)
        verifying = self.fixture._phase(exclusive, 'verifying', contract.ManagementStage.VERIFYING)
        accepted = replace(verifying, stage=contract.ManagementStage.ACCEPTED,
            acceptance=contract.AcceptanceEvidence(contract.execution_tuple_sha256(verifying.candidate), 'a' * 64))
        management_store.advance_management_journal(self.root, verifying, accepted, lease=self.fixture.lease)
        self.fixture.store.promote_active(self.root, accepted, lease=self.fixture.lease)
        self.source = accepted
        self._execution(accepted.candidate)
        return accepted.acceptance

    def test_unaccepted_forward_continuation_reaches_new_acceptance_and_releases_authority(self):
        value = self.make_controller()
        value.execute()
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(value.journal.stage, contract.ManagementStage.SUCCEEDED)
        self.assertIsNotNone(value.journal.acceptance)
        self.assertEqual(self.fixture.store.inspect().active, self.target.reference)
        self.assertEqual(execution_state.read_active_execution(self.root).generation, 2)
        self.assertEqual(recovery.inspect_takeover(self.root, value.journal.operation_id, 2).source_journal, self.source)

    def test_accepted_forward_continuation_preserves_original_acceptance_and_candidate(self):
        acceptance = self.accept_source()
        value = self.make_controller()
        value.execute()
        self.assertEqual(value.journal.acceptance, acceptance)
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.source.candidate)
        self.assertEqual(execution_state.read_active_execution(self.root).generation, 2)
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_recovery_failure_after_acceptance_keeps_claim_and_never_restores_previous(self):
        acceptance = self.accept_source()
        value = self.make_controller()
        value.commit_and_wait_recovery.side_effect = RuntimeError('recovery fixture failed')
        with self.assertRaises(RuntimeError):
            value.execute()
        value.retain_recovery()
        self.assertEqual(value.journal.acceptance, acceptance)
        self.assertEqual(value.journal.stage, contract.ManagementStage.RECOVERY_REQUIRED)
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.source.candidate)
        self.assertEqual(self.fixture.store.inspect().active, self.target.reference)
        self.assertEqual(recovery.inspect_recovery(self.root, value.journal.operation_id, 2)['permitted_actions'], ['continue_recovery'])


if __name__ == '__main__':
    unittest.main()
