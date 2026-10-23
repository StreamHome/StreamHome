from dataclasses import replace
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch
import uuid

from lifecycle import management_journal as journal_contract, management_store, management_recovery_store as recovery, state
from lifecycle.control import resumption_observation as observer
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_management_recovery_store as recovery_fixture
from tests.lifecycle.test_resumption_contract import plan


class ResumptionObservationTests(unittest.TestCase):
    def setUp(self):
        self.prepare()

    def prepare(self, *, accepted=False):
        fixture = recovery_fixture.ManagementRecoveryStoreTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        if accepted:
            current = fixture.journal
            prepared = replace(current, stage=journal_contract.ManagementStage.PREPARED)
            management_store.advance_management_journal(fixture.root, current, prepared, lease=fixture.lease)
            state.transition_operation_with_credential(fixture.root, operation_id=current.operation_id,
                generation=current.generation, expected_phase='reserved', next_phase='exclusive')
            exclusive = replace(prepared, stage=journal_contract.ManagementStage.EXCLUSIVE)
            management_store.advance_management_journal(fixture.root, prepared, exclusive, lease=fixture.lease)
            state.transition_operation_with_credential(fixture.root, operation_id=current.operation_id,
                generation=current.generation, expected_phase='exclusive', next_phase='verifying')
            verifying = replace(exclusive, stage=journal_contract.ManagementStage.VERIFYING)
            management_store.advance_management_journal(fixture.root, exclusive, verifying, lease=fixture.lease)
            fixture.journal = replace(verifying, stage=journal_contract.ManagementStage.ACCEPTED,
                acceptance=journal_contract.AcceptanceEvidence(journal_contract.execution_tuple_sha256(verifying.candidate), 'a' * 64))
            management_store.advance_management_journal(fixture.root, verifying, fixture.journal, lease=fixture.lease)
        _, current = fixture.take_over()
        generation = current.generation
        state.transition_operation_with_credential(fixture.root, operation_id=current.operation_id,
            generation=generation, expected_phase='recovery_required', next_phase='exclusive')
        state.transition_operation_with_credential(fixture.root, operation_id=current.operation_id,
            generation=generation, expected_phase='exclusive', next_phase='verifying')
        self.journal = recovery.resume_candidate_verification(fixture.root, current, lease=fixture.lease)
        takeover = recovery.inspect_takeover(fixture.root, current.operation_id, generation)
        self.plan = plan()
        self.plan.update(installation_id=current.installation_id, operation_id=current.operation_id, generation=generation,
                         action=current.action.value, review_id=takeover.review_id)
        self.plan['stages'] = [{'kind': 'candidate_start', 'generation': generation, 'handoff_sha256': None,
            'execution_sha256': journal_contract.execution_tuple_sha256(journal_contract.selected_execution(current))}]
        self.payload = {'plan': self.plan, 'kind': 'candidate_start', 'controller_pid': os.getpid(),
                        'controller_invocation_id': current.controller_invocation_id}
        reviewed = SimpleNamespace(expected_generation=generation - 1, previous=current.previous, candidate=current.candidate,
            review_id=current.review_id, action=current.action, supervisor_invocation_id=current.supervisor_invocation_id,
            controller_control_manifest_sha256=self.plan['control_manifest_sha256'])
        self.grant = {'resumption_plan_id': self.plan['plan_id'], 'recovery_review_id': self.plan['review_id']}
        self.enterContext(patch.object(observer, 'effective_request', return_value=reviewed))
        self.enterContext(patch.object(observer, 'read_grant', side_effect=lambda *args: self.grant))
        layout = SimpleNamespace(managed=True, descriptor=SimpleNamespace(descriptor=SimpleNamespace(
            installation_id=current.installation_id, service_uid=os.geteuid())))
        self.enterContext(patch.object(observer, 'ROOT', fixture.root))
        self.enterContext(patch.object(observer, 'layout_for_root', return_value=layout))
        self.enterContext(patch.object(observer, 'controller_peer'))
        self.quiet = self.enterContext(patch.object(observer, 'require_service_quiescent'))
        self.verify = self.enterContext(patch.object(observer, 'verify_runtime'))
        self.links = self.enterContext(patch.object(observer, 'verify_links'))

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.fixture.paths.lifecycle_dir.rglob('*') if p.is_file()}

    def test_real_canonical_takeover_and_guarded_tuple_are_observed_without_mutation(self):
        before = self.snapshot()
        result = observer.observe(self.payload)
        self.assertEqual(result['execution_sha256'], self.plan['stages'][0]['execution_sha256'])
        self.assertFalse(result['accepted'])
        self.assertEqual(self.snapshot(), before)
        lease = self.fixture.lease.lease
        self.quiet.assert_called_once_with(inspections=True, controller=(self.journal.operation_id, lease.pid, lease.process_start_identity))
        self.verify.assert_called_once();self.links.assert_called_once()

    def test_stale_generation_actor_review_or_tuple_cannot_authorize_os_resumption(self):
        for changed in ({'generation': self.plan['generation'] + 1}, {'review_id': str(uuid.uuid4())}):
            with self.subTest(changed=changed), self.assertRaises(ControlError):
                observer.observe({**self.payload, 'plan': {**self.plan, **changed}})
        for changed in ({'controller_pid': os.getpid() + 1}, {'controller_invocation_id': 'f' * 32}):
            with self.subTest(changed=changed), self.assertRaises(ControlError):observer.observe({**self.payload, **changed})
        stage = {**self.plan['stages'][0], 'execution_sha256': 'f' * 64}
        with self.assertRaises(ControlError):observer.observe({**self.payload, 'plan': {**self.plan, 'stages': [stage]}})
        self.verify.assert_not_called()

    def test_foreign_work_refuses_observation_without_changing_canonical_authority(self):
        before = self.snapshot();self.quiet.side_effect = ControlError('control_foreign_service_process')
        with self.assertRaises(ControlError):observer.observe(self.payload)
        self.assertEqual(self.snapshot(), before)
        self.verify.assert_not_called()

    def test_accepted_selected_execution_is_observed_without_reversing_acceptance(self):
        self.prepare(accepted=True)
        before = self.snapshot()
        self.assertTrue(observer.observe(self.payload)['accepted'])
        self.assertEqual(self.snapshot(), before)

    def test_acceptance_appearing_after_review_cannot_replace_its_retained_source(self):
        accepted = replace(self.journal, stage=journal_contract.ManagementStage.ACCEPTED,
            acceptance=journal_contract.AcceptanceEvidence(self.plan['stages'][0]['execution_sha256'], 'a' * 64))
        management_store.advance_management_journal(self.fixture.root, self.journal, accepted, lease=self.fixture.lease)
        before = self.snapshot()
        with self.assertRaises(recovery.ManagementRecoveryError):observer.observe(self.payload)
        self.assertEqual(self.snapshot(), before)

    def test_canonical_review_cannot_borrow_another_root_permission(self):
        before = self.snapshot()
        self.grant['resumption_plan_id'] = str(uuid.uuid4())
        with self.assertRaises(ControlError):observer.observe(self.payload)
        self.verify.assert_not_called()
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':unittest.main()
