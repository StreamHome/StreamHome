from contextlib import ExitStack
from copy import deepcopy
import subprocess
import sys
import unittest
from unittest.mock import patch

from lifecycle.control import resumption_ticket as ticket, resumption_contract as contract
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_resumption_contract import plan


class ResumptionTicketTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.evidence = {'owner': {'schema_version': 1, 'pid': 123, 'start_identity': '456',
            'boot_id': self.plan['boot_id'], 'nonce': '1' * 64, 'status': 'active'},
            'scope': {'unit': 'streamhome-install-' + self.plan['installation_id'] + '-' + '2' * 32 + '.scope',
                      'invocation': '3' * 32, 'pid': 123, 'start_identity': '456'}}
        self.plan['issuer_sha256'] = contract.digest(self.evidence)
        self.receipt = contract.bind_invocation(self.plan,
            contract.consume(self.plan, self.plan['stages'][0], '4' * 64, existing=None), '5' * 32)
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(ticket, 'read_protected_json', side_effect=lambda path: {'plan': self.plan, 'issuer': self.evidence} if path.name == 'plan.json' else self.receipt))
        stack.enter_context(patch.object(ticket, 'read_stop_epoch', return_value=self.plan['stop_epoch']))
        stack.enter_context(patch.object(ticket.Path, 'read_text', return_value=self.plan['boot_id']))
        stack.enter_context(patch.object(ticket.time, 'monotonic', return_value=100))
        self.live = stack.enter_context(patch.object(ticket, 'require_scope'))

    def read(self, **kwargs):
        arguments = dict(installation_id=self.plan['installation_id'], control_manifest_sha256=self.plan['control_manifest_sha256'])
        arguments.update(kwargs)
        return ticket.bound_ticket(self.plan['plan_id'], 'candidate_start', '5' * 32, **arguments)

    def test_only_one_bound_invocation_is_eligible_for_separate_startup_review(self):
        self.assertEqual(self.read(), {'plan': self.plan, 'receipt': self.receipt})
        self.live.assert_called_with(self.evidence['scope'], self.plan['installation_id'])
        with self.assertRaises(ControlError):ticket.bound_ticket(self.plan['plan_id'], 'candidate_start', '6' * 32,
            installation_id=self.plan['installation_id'], control_manifest_sha256=self.plan['control_manifest_sha256'])

    def test_control_runtime_ticket_requires_the_candidate_reader_not_issuing_reader(self):
        self.plan.update(schema_version=3,action='provision_control')
        self.plan['stages'][0]['control_manifest_sha256']='1'*64
        self.plan['stages'][1]['control_manifest_sha256']=self.plan['control_manifest_sha256']
        self.receipt=contract.bind_invocation(self.plan,
            contract.consume(self.plan,self.plan['stages'][0],'4'*64,existing=None),'5'*32)
        with self.assertRaises(ControlError):self.read()
        self.assertEqual(self.read(control_manifest_sha256='1'*64)['receipt'],self.receipt)
        self.assertEqual(ticket.reviewed_plan(self.plan['plan_id'],installation_id=self.plan['installation_id'],
            control_manifest_sha256=self.plan['control_manifest_sha256'])['plan'],self.plan)

    def test_consumed_without_binding_is_not_a_runtime_ticket(self):
        self.receipt = {**self.receipt, 'status': 'consumed', 'invocation': None}
        with self.assertRaises(ControlError):self.read()

    def test_stop_expiry_control_or_dead_issuer_blocks_pending_ticket(self):
        with patch.object(ticket, 'read_stop_epoch', return_value='f' * 64):
            with self.assertRaises(ControlError):self.read()
        with patch.object(ticket.time, 'monotonic', return_value=1000):
            with self.assertRaises(ControlError):self.read()
        with self.assertRaises(ControlError):self.read(control_manifest_sha256='f' * 64)
        self.live.side_effect = ControlError('control_stopped_repair_issuer_changed')
        with self.assertRaises(ControlError):self.read()

    def test_changed_owner_identity_cannot_borrow_valid_root_scope(self):
        self.evidence = deepcopy(self.evidence);self.evidence['owner']['pid'] = 124
        self.plan['issuer_sha256'] = contract.digest(self.evidence)
        self.receipt['plan_sha256'] = contract.digest(self.plan)
        with self.assertRaises(ControlError):self.read()
        self.live.assert_not_called()

    def test_current_ticket_requires_the_exact_dispatch_nonce(self):
        intent = contract.start_intent(self.plan, self.receipt)
        def read(path):
            if path.name == 'resumption-start.json':return intent
            return {'plan': self.plan, 'issuer': self.evidence} if path.name == 'plan.json' else self.receipt
        with patch.object(ticket, 'read_protected_json', side_effect=read):
            self.assertEqual(ticket.current_ticket('5' * 32, installation_id=self.plan['installation_id'],
                control_manifest_sha256=self.plan['control_manifest_sha256'])['receipt'], self.receipt)
            intent['nonce'] = '0' * 64
            with self.assertRaises(ControlError):ticket.current_ticket('5' * 32, installation_id=self.plan['installation_id'],
                control_manifest_sha256=self.plan['control_manifest_sha256'])

    def test_runtime_ticket_has_no_application_or_canonical_imports(self):
        script = "import sys;import lifecycle.control.resumption_ticket;assert not {'lifecycle.state','lifecycle.management_journal','config','database','application'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':unittest.main()
