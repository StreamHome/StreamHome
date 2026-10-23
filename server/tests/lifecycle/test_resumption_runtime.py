from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import management_journal as journal_contract, management_store, state
from lifecycle.control import resumption_contract as contract, resumption_runtime as runtime
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_resumption_observation as observer_fixture


class ResumedRuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        fixture = observer_fixture.ResumptionObservationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture = fixture;self.root = fixture.fixture.root;self.journal = fixture.journal
        self.plan = fixture.plan;self.invocation = '5' * 32
        receipt = contract.bind_invocation(self.plan,
            contract.consume(self.plan, self.plan['stages'][0], '4' * 64, existing=None), self.invocation)
        self.enterContext(patch.object(runtime, 'layout_for_root', return_value=SimpleNamespace(managed=True,
            descriptor=SimpleNamespace(descriptor=SimpleNamespace(installation_id=self.journal.installation_id, service_uid=os.geteuid())))))
        self.enterContext(patch.object(runtime, 'effective_request', return_value=SimpleNamespace(
            controller_control_manifest_sha256=self.plan['control_manifest_sha256'], previous=self.journal.previous, candidate=self.journal.candidate)))
        self.grant = self.enterContext(patch.object(runtime, 'read_grant', return_value={'schema_version': 3,
            'resumption_plan_id': self.plan['plan_id'], 'recovery_review_id': self.plan['review_id']}))
        self.ticket = self.enterContext(patch.object(runtime, 'bound_ticket', return_value={'plan': self.plan, 'receipt': receipt}))
        self.enterContext(patch.object(runtime, 'controller_peer'))
        self.enterContext(patch.object(runtime, 'process_cgroup', return_value=runtime.RUNTIME_CGROUP))
        self.enterContext(patch.object(runtime, 'unit_snapshot', return_value={'ActiveState': 'activating',
            'MainPID': str(os.getpid()), 'InvocationID': self.invocation}))
        self.enterContext(patch.object(runtime, 'read_active_execution', return_value=SimpleNamespace(
            installation_id=self.journal.installation_id, operation_id=self.journal.operation_id,
            generation=self.journal.generation, execution=journal_contract.selected_execution(self.journal))))

    def bind(self):
        return runtime.require_binding(self.root, self.journal, self.invocation,
            control_manifest_sha256=self.plan['control_manifest_sha256'], own_supervisor=True, allow_original=False)

    def test_replacement_is_bound_without_rewriting_original_supervisor_history(self):
        original = self.journal
        binding = self.bind()
        self.assertEqual(binding.invocation, self.invocation)
        self.assertNotEqual(binding.invocation, original.supervisor_invocation_id)
        self.assertEqual(management_store.inspect_management_journal(self.root, original.operation_id), original)
        self.assertTrue(binding.require_current(self.root))

    def test_old_normal_supervisor_path_does_not_borrow_a_resumption_ticket(self):
        value = runtime.require_binding(self.root, self.journal, self.journal.supervisor_invocation_id,
            control_manifest_sha256=self.plan['control_manifest_sha256'])
        self.assertIsNone(value);self.ticket.assert_not_called()

    def test_wrong_runtime_or_changed_permission_cannot_be_enrolled(self):
        with patch.object(runtime, 'unit_snapshot', return_value={'ActiveState': 'active', 'MainPID': str(os.getpid()), 'InvocationID': 'f' * 32}):
            with self.assertRaises(ControlError):self.bind()
        self.plan['generation'] += 1
        with self.assertRaises(ControlError):self.bind()

    def test_unresolved_runtime_keeps_requiring_live_root_custody(self):
        binding = self.bind();self.grant.side_effect = ControlError('control_resumption_issuer_changed')
        with self.assertRaises(ControlError):binding.require_current(self.root)

    def test_exact_completed_ram_enrollment_survives_expiry_but_cannot_bless_another_process(self):
        binding = self.bind()
        self.complete(binding)
        self.grant.side_effect = ControlError('control_resumption_issuer_changed')
        self.assertFalse(binding.require_current(self.root))
        with patch.object(runtime.os, 'getpid', return_value=binding.pid + 1):
            with self.assertRaises(ControlError):binding.require_current(self.root)

    def complete(self, binding):
        accepted = replace(self.journal, stage=journal_contract.ManagementStage.ACCEPTED,
            acceptance=journal_contract.AcceptanceEvidence(binding.execution_sha256, 'a' * 64))
        lease = self.fixture.fixture.lease
        management_store.advance_management_journal(self.root, self.journal, accepted, lease=lease)
        terminal = replace(accepted, stage=journal_contract.ManagementStage.SUCCEEDED)
        management_store.advance_management_journal(self.root, accepted, terminal, lease=lease)
        state.complete_operation_with_credential(self.root, operation_id=terminal.operation_id,
            generation=terminal.generation, expected_phase='verifying')

    def test_completed_controller_exit_during_live_check_reconciles_exact_terminal_authority(self):
        binding = self.bind()
        def completed(*args, **kwargs):
            self.complete(binding)
            raise ControlError('control_controller_changed')
        with patch.object(runtime, 'require_binding', side_effect=completed):
            self.assertFalse(binding.require_current(self.root))

    def test_idle_without_terminal_acceptance_cannot_settle_a_resumed_process(self):
        binding = self.bind()
        with patch.object(runtime.state, 'inspect_state', return_value=state.LifecycleState(2, binding.generation, None)):
            with self.assertRaises(ControlError):binding.require_current(self.root)


if __name__ == '__main__':unittest.main()
