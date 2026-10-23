from dataclasses import replace
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle import execution_state, management_controller as controller, management_journal as contract, management_store, management_recovery_store as recovery, runtime_links, state
from lifecycle.control.requests import parse_management_request
from lifecycle.control import workers
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

    def make_controller(self, action='continue_recovery', expected_desired=None):
        snapshot = recovery.inspect_recovery(self.root, self.source.operation_id, state.inspect_state(self.root).generation)
        self.fixture.lease.release()
        reviewed = request_payload()
        reviewed.update(installation_id=self.source.installation_id, operation_id=self.source.operation_id,
            review_id=self.source.review_id, expected_generation=snapshot['generation'],
            previous=contract.execution_tuple_payload(self.source.previous), candidate=contract.execution_tuple_payload(self.source.candidate),
            deployment=self.target.settings.payload(), expected_desired=(expected_desired or self.fixture.initial.reference).payload(),
            supervisor_invocation_id=self.source.supervisor_invocation_id)
        value = controller.RecoveredManagementController.__new__(controller.RecoveredManagementController)
        value.review = parse_management_request(reviewed)
        original_patch = patch.object(controller, 'read_management_request', return_value=replace(value.review, expected_generation=self.source.generation - 1))
        original_patch.start()
        self.addCleanup(original_patch.stop)
        value.grant = {'schema_version': 1, 'source_evidence_sha256': snapshot['evidence_sha256'], 'recovery_review_id': str(uuid.uuid4()), 'action': action}
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
                runtime_links.verify_links(self.root, contract.selected_execution(value.journal))
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

    def test_stopped_originally_running_recovery_pairs_links_before_guarded_os_start(self):
        value = self.make_controller()
        value.grant['schema_version'] = 3
        def resumed():
            self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.VERIFYING)
            runtime_links.verify_links(self.root, value.journal.candidate)
            self.assertEqual(value.journal.supervisor_invocation_id, self.source.supervisor_invocation_id)
            return object()
        value.resumed_binding = Mock(side_effect=resumed)
        with patch('lifecycle.control.stopped_repair.require_stopped') as stopped, patch('lifecycle.control.processes.require_service_quiescent') as quiescent:
            value.execute()
        self.assertGreater(stopped.call_count, 0)
        self.assertGreater(quiescent.call_count, 0)
        value.rpc.assert_not_called()
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(value.journal.stage, contract.ManagementStage.SUCCEEDED)

    def test_failed_resumption_never_accepts_or_silently_calls_the_historical_supervisor(self):
        value = self.make_controller()
        value.grant['schema_version'] = 3
        value.resumed_binding = Mock(side_effect=RuntimeError('root permission canceled'))
        with patch('lifecycle.control.stopped_repair.require_stopped'), patch('lifecycle.control.processes.require_service_quiescent'):
            with self.assertRaisesRegex(RuntimeError, 'root permission canceled'):
                value.execute()
        value.rpc.assert_not_called()
        value.wait_guarded.assert_not_called()
        self.assertIsNone(value.journal.acceptance)
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.source.previous)

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

    def test_succeeded_journal_before_authority_release_can_be_reviewed_and_completed(self):
        first = self.make_controller()
        first.complete = Mock(side_effect=OSError('completion reply gap'))
        with self.assertRaises(OSError):
            first.execute()
        self.assertEqual(first.journal.stage, contract.ManagementStage.SUCCEEDED)
        acceptance = first.journal.acceptance
        first.lease.release()
        second = self.make_controller()
        second.execute()
        self.assertEqual(second.journal.acceptance, acceptance)
        self.assertEqual(execution_state.read_active_execution(self.root).generation, 3)
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def original_previous_record(self):
        # The original accepted runtime belongs to a prior, completed operation.
        record = execution_state.ExecutionRecord(self.fixture.identity, self.source.previous, str(uuid.uuid4()), 0)
        state._atomic_write_json(self.state_dir / 'active-runtime.json', record.payload())

    def test_guarded_rollback_accepts_previous_without_relabeling_the_candidate(self):
        self.original_previous_record()
        value = self.make_controller(action='rollback')
        value.execute()
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(value.journal.stage, contract.ManagementStage.ROLLED_BACK)
        self.assertEqual(value.journal.recovery_target, 'previous')
        self.assertEqual(value.journal.previous, self.source.previous)
        self.assertEqual(value.journal.candidate, self.source.candidate)
        self.assertEqual(value.journal.acceptance.execution_tuple_sha256, contract.execution_tuple_sha256(self.source.previous))
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.source.previous)
        manifest = self.fixture.store.inspect()
        self.assertEqual(manifest.active, self.fixture.initial.reference)
        self.assertEqual(manifest.desired, self.target.reference)
        self.assertIn(self.target.revision, manifest.quarantined)

    def test_accepted_previous_failure_can_only_continue_previous_with_original_acceptance(self):
        self.original_previous_record()
        first = self.make_controller(action='rollback')
        first.commit_and_wait_recovery.side_effect = OSError('previous recovery failed')
        with self.assertRaises(OSError):
            first.execute()
        first.retain_recovery()
        acceptance = first.journal.acceptance
        self.assertIsNotNone(acceptance)
        self.assertEqual(recovery.inspect_recovery(self.root, first.journal.operation_id, 2)['permitted_actions'], ['continue_recovery'])
        first.lease.release()
        second = self.make_controller()
        second.execute()
        self.assertEqual(second.journal.acceptance, acceptance)
        self.assertEqual(second.journal.recovery_target, 'previous')
        self.assertEqual(second.journal.stage, contract.ManagementStage.ROLLED_BACK)
        self.assertEqual(execution_state.read_active_execution(self.root).execution, self.source.previous)
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_rolled_back_terminal_before_completion_can_be_reviewed_again(self):
        self.original_previous_record()
        first = self.make_controller(action='rollback')
        first.complete = Mock(side_effect=OSError('completion fixture'))
        with self.assertRaises(OSError):
            first.execute()
        self.assertEqual(first.journal.stage, contract.ManagementStage.ROLLED_BACK)
        acceptance = first.journal.acceptance
        first.lease.release()
        second = self.make_controller()
        second.execute()
        self.assertEqual(second.journal.acceptance, acceptance)
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_safe_abort_uses_guarded_previous_verification_and_no_candidate_acceptance(self):
        self.original_previous_record()
        value = self.make_controller(action='safe_abort')
        value.execute()
        self.assertEqual(value.journal.stage, contract.ManagementStage.ROLLED_BACK)
        self.assertEqual(contract.selected_execution(value.journal), self.source.previous)
        value.wait_guarded.assert_called_once()
        value.commit_and_wait_recovery.assert_called_once()

    def test_early_abort_does_not_publish_an_originally_unpublished_desired_revision(self):
        self.original_previous_record()
        manifest = self.fixture.store.inspect()
        self.fixture._fixture_manifest(replace(manifest, desired=self.fixture.initial.reference).payload())
        (self.fixture.directory / f'apply-{self.source.operation_id}-{self.source.generation}.json').unlink()
        value = self.make_controller(action='safe_abort')
        value.execute()
        manifest = self.fixture.store.inspect()
        self.assertEqual(manifest.active, self.fixture.initial.reference)
        self.assertEqual(manifest.desired, self.fixture.initial.reference)
        self.assertIn(self.target.revision, manifest.quarantined)
        self.assertEqual(value.journal.stage, contract.ManagementStage.ROLLED_BACK)

    def test_lost_accepted_previous_quarantine_is_not_recreated_from_acceptance(self):
        self.original_previous_record()
        first = self.make_controller(action='rollback')
        first.commit_and_wait_recovery.side_effect = OSError('previous recovery failure')
        with self.assertRaises(OSError):
            first.execute()
        first.retain_recovery()
        first.lease.release()
        manifest = self.fixture.store.inspect()
        self.fixture._fixture_manifest(replace(manifest, quarantined=()).payload())
        second = self.make_controller()
        with self.assertRaises(ValueError):
            second.execute()
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(self.fixture.store.inspect().quarantined, ())

    def test_explicit_configure_retry_uses_a_new_revision_instead_of_reusing_quarantine(self):
        self.original_previous_record()
        value = self.make_controller(action='rollback')
        value.execute()
        reviewed = value.review.payload()
        names = ('operation_id', 'review_id', 'boot_enabled', 'previous_boot_enabled', 'initial_service_state',
            'supervisor_invocation_id', 'supervisor', 'backend', 'boot_id', 'stop_epoch')
        payload = {key: reviewed[key] for key in names}
        payload.update(operation_id=str(uuid.uuid4()), review_id=str(uuid.uuid4()), purpose='configure',
            expected_generation=2, expected_desired_revision=self.target.revision,
            deployment=self.target.settings.payload(), activate_deployment=True, interruption_approved=True)
        with patch.object(workers, 'ROOT', self.root), patch.object(workers, 'layout_for_root',
                return_value=SimpleNamespace(domains=self.fixture.directory)):
            prepared = workers.prepare_management_request(payload, control_manifest_sha256=reviewed['controller_control_manifest_sha256'])
        self.assertFalse(prepared['no_changes'])
        retry = parse_management_request(prepared['request'])
        self.assertNotEqual(retry.candidate.deployment_revision, self.target.revision)
        self.assertEqual(retry.previous, self.source.previous)
        self.assertEqual(retry.activation_mode, contract.ActivationMode.CUTOVER)

    def test_next_early_abort_recognizes_the_completed_previous_rollback_binding(self):
        self.original_previous_record()
        first = self.make_controller(action='rollback')
        first.execute()
        previous = first.journal.previous
        first.lease.release()
        for index in range(3):
            with self.subTest(index=index):
                expected = self.fixture.store.inspect().desired
                self.target = self.fixture._target(5000 + index)
                operation = str(uuid.uuid4())
                baseline = state.inspect_state(self.root).generation
                lease, claim = state.claim_management_controller(self.root, operation_id=operation, expected_generation=baseline)
                self.fixture.lease = lease
                journal = replace(self.source, operation_id=operation, generation=claim.state.generation,
                    review_id=str(uuid.uuid4()), previous=previous,
                    candidate=replace(previous, deployment_revision=self.target.revision), stage=contract.ManagementStage.RESERVED)
                management_store.create_management_journal(self.root, journal, lease=lease)
                self.source = management_store.advance_management_journal(self.root, journal,
                    replace(journal, stage=contract.ManagementStage.PREPARED), lease=lease)
                second = self.make_controller(action='safe_abort', expected_desired=expected)
                second.execute()
                self.assertEqual(second.journal.stage, contract.ManagementStage.ROLLED_BACK)
                self.assertEqual(execution_state.read_active_execution(self.root).execution, previous)
                self.assertIsNone(state.inspect_state(self.root).active_operation)
                second.lease.release()

    def test_completed_rollback_binding_requires_its_exact_complete_recovery_receipt(self):
        self.original_previous_record()
        first = self.make_controller(action='rollback')
        first.execute()
        following = replace(self.source, operation_id=str(uuid.uuid4()), generation=3)
        original = recovery._read(self.state_dir / 'runtime-links.json')
        self.assertTrue(runtime_links._completed_previous_binding(self.root, following, original))
        path = self.state_dir / f'runtime-link-recovery-{first.journal.operation_id}-{first.journal.generation}.json'
        receipt = recovery._read(path)
        for changed in ({'stage': 'applying'}, {'source_receipt_sha256': 'f' * 64}, {'target': 'candidate'}):
            state._atomic_write_json(path, {**receipt, **changed})
            with self.subTest(changed=changed), self.assertRaises(runtime_links.RuntimeBuildError):
                runtime_links._completed_previous_binding(self.root, following, original)


if __name__ == '__main__':
    unittest.main()
