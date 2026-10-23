from copy import deepcopy
import subprocess
import sys
import unittest
import uuid

from lifecycle.control import resumption_contract as contract
from lifecycle.control.rpc import ControlError


def plan():
    return {'schema_version': 1, 'plan_id': str(uuid.uuid4()), 'installation_id': str(uuid.uuid4()),
        'operation_id': str(uuid.uuid4()), 'review_id': str(uuid.uuid4()), 'owner_type': 'management',
        'action': 'repair_environment', 'generation': 5, 'boot_id': str(uuid.uuid4()), 'stop_epoch': 'a' * 64,
        'control_manifest_sha256': 'b' * 64, 'issuer_sha256': 'c' * 64, 'expires_at_monotonic': 1000,
        'prior_running': True, 'interruption_approved': True, 'resumption_approved': True,
        'stages': [{'kind': 'candidate_start', 'execution_sha256': 'd' * 64, 'generation': 5, 'handoff_sha256': None},
                   {'kind': 'previous_start', 'execution_sha256': 'e' * 64, 'generation': 6, 'handoff_sha256': 'f' * 64}]}


class ResumptionContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()

    def observation(self, index=0):
        stage = self.plan['stages'][index]
        return {**{name: self.plan[name] for name in ('installation_id', 'operation_id', 'owner_type', 'action')},
            **{name: stage[name] for name in ('generation', 'execution_sha256', 'handoff_sha256')},
            'accepted': False, 'candidate_failed': bool(index), 'previous_restored': bool(index)}

    def authorize(self, index=0, *, value=None, **kwargs):
        arguments = dict(boot_id=self.plan['boot_id'], stop_epoch=self.plan['stop_epoch'],
                         issuer_sha256=self.plan['issuer_sha256'], now=100, candidate_consumed=bool(index))
        arguments.update(kwargs)
        return contract.authorize_stage(self.plan, self.plan['stages'][index]['kind'],
            self.observation(index) if value is None else value, **arguments)

    def test_exact_candidate_and_separately_reviewed_previous_stage(self):
        original = deepcopy(self.plan)
        self.assertEqual(self.authorize(), self.plan['stages'][0])
        self.assertEqual(self.authorize(1), self.plan['stages'][1])
        self.assertEqual(self.plan, original)

    def test_no_implicit_fallback_retry_or_other_os_command(self):
        self.plan['stages'] = self.plan['stages'][:1]
        observed = self.observation()
        with self.assertRaises(ControlError):
            contract.authorize_stage(self.plan, 'previous_start', observed, boot_id=self.plan['boot_id'],
                stop_epoch=self.plan['stop_epoch'], issuer_sha256=self.plan['issuer_sha256'], now=100, candidate_consumed=True)
        with self.assertRaises(ControlError):self.authorize(candidate_consumed=True)
        with self.assertRaises(ControlError):contract.parse_plan({**self.plan, 'command': 'systemctl restart arbitrary.service'})
        self.plan['stages'].append(deepcopy(self.plan['stages'][0]))
        with self.assertRaises(ControlError):contract.parse_plan(self.plan)

    def test_initially_stopped_or_missing_consent_is_not_a_resume_permission(self):
        for name in ('prior_running', 'interruption_approved', 'resumption_approved'):
            for value in (False, 1, None):
                with self.subTest(name=name, value=value), self.assertRaises(ControlError):
                    contract.parse_plan({**self.plan, name: value})

    def test_boot_stop_issuer_expiry_and_nonfinite_clock_invalidate_pending_permission(self):
        for changed in ({'boot_id': str(uuid.uuid4())}, {'stop_epoch': 'b' * 64},
                        {'issuer_sha256': 'f' * 64}, {'now': 1000}, {'now': float('nan')}, {'now': True}):
            with self.subTest(changed=changed), self.assertRaises(ControlError):self.authorize(**changed)

    def test_foreign_operation_type_generation_tuple_and_unreviewed_handoff_are_rejected(self):
        for name, value in (('installation_id', str(uuid.uuid4())), ('operation_id', str(uuid.uuid4())),
                            ('owner_type', 'restore'), ('generation', 6), ('generation', True),
                            ('action', 'provision_control'), ('execution_sha256', 'e' * 64), ('handoff_sha256', 'f' * 64)):
            with self.subTest(name=name), self.assertRaises(ControlError):
                self.authorize(value={**self.observation(), name: value})

    def test_fallback_requires_failed_candidate_exact_restoration_and_no_acceptance(self):
        for changed in ({'accepted': True}, {'candidate_failed': False}, {'previous_restored': False},
                        {'candidate_failed': 1}, {'handoff_sha256': None}):
            with self.subTest(changed=changed), self.assertRaises(ControlError):
                self.authorize(1, value={**self.observation(1), **changed})
        with self.assertRaises(ControlError):self.authorize(1, candidate_consumed=False)

    def test_no_generation_jump_retargeted_fallback_or_nonfinite_plan(self):
        for changed in ({'expires_at_monotonic': float('inf')}, {'generation': True}, {'action': []}):
            with self.subTest(changed=changed), self.assertRaises(ControlError):contract.parse_plan({**self.plan, **changed})
        for changed in ({'generation': 7}, {'generation': 6, 'handoff_sha256': None},
                        {'generation': 5}, {'execution_sha256': 'd' * 64}):
            value = deepcopy(self.plan);value['stages'][1].update(changed)
            with self.subTest(changed=changed), self.assertRaises(ControlError):contract.parse_plan(value)

    def test_contract_imports_no_canonical_or_application_authority(self):
        script = "import sys;import lifecycle.control.resumption_contract;assert not {'lifecycle.state','lifecycle.management_journal','config','database','application'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_consumption_precedes_os_dispatch_and_never_rearms_after_lost_reply(self):
        stage = self.authorize()
        receipt = contract.consume(self.plan, stage, '1' * 64, existing=None)
        self.assertEqual(receipt['status'], 'consumed')
        self.assertIsNone(receipt['invocation'])
        with self.assertRaises(ControlError):contract.consume(self.plan, stage, '2' * 64, existing=receipt)
        bound = contract.bind_invocation(self.plan, receipt, '3' * 32)
        self.assertEqual(bound['invocation'], '3' * 32)
        with self.assertRaises(ControlError):contract.consume(self.plan, stage, '2' * 64, existing=bound)
        with self.assertRaises(ControlError):contract.bind_invocation(self.plan, bound, '4' * 32)
        self.assertIsNone(receipt['invocation'])

    def test_receipt_cannot_move_between_plan_stage_or_invocation(self):
        receipt = contract.consume(self.plan, self.authorize(), '1' * 64, existing=None)
        for changed in ({'kind': 'previous_start'}, {'plan_sha256': 'f' * 64}, {'stage_sha256': 'e' * 64},
                        {'nonce': '../permission'}, {'invocation': '2' * 32}, {'status': 'issued'}):
            with self.subTest(changed=changed), self.assertRaises(ControlError):
                contract.parse_receipt({**receipt, **changed}, self.plan)
        for invocation in ('0' * 32, 'x' * 32, None, 100):
            with self.subTest(invocation=invocation), self.assertRaises(ControlError):
                contract.bind_invocation(self.plan, receipt, invocation)
        other = deepcopy(self.plan);other['review_id'] = str(uuid.uuid4())
        with self.assertRaises(ControlError):contract.parse_receipt(receipt, other)


if __name__ == '__main__':unittest.main()
