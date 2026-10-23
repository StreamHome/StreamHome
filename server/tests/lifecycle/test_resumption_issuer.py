from contextlib import ExitStack, contextmanager
from copy import deepcopy
import subprocess
import sys
import unittest
from unittest.mock import patch

from lifecycle.control import resumption_issuer as issuer
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_resumption_contract import plan


class ResumptionIssuerTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.owner = {'schema_version': 1, 'pid': 123, 'start_identity': '456', 'boot_id': self.plan['boot_id'],
                      'nonce': '1' * 64, 'status': 'active'}
        self.scope = {'unit': 'streamhome-install-' + self.plan['installation_id'] + '-' + '2' * 32 + '.scope',
                      'invocation': '3' * 32, 'pid': 123, 'start_identity': '456'}
        self.evidence = {'owner': self.owner, 'scope': self.scope}
        self.plan['issuer_sha256'] = issuer.digest(self.evidence)

    @contextmanager
    def fixture(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(issuer, 'require_root'))
            stack.enter_context(patch.object(issuer, 'read_protected_json', return_value=deepcopy(self.owner)))
            stack.enter_context(patch.object(issuer, 'read_stop_epoch', return_value=self.plan['stop_epoch']))
            stack.enter_context(patch.object(issuer.time, 'monotonic', return_value=100))
            stack.enter_context(patch.object(issuer.os, 'getpid', return_value=123))
            stack.enter_context(patch.object(issuer.Path, 'read_text', return_value=self.plan['boot_id']))
            stack.enter_context(patch.object(issuer, 'current_scope', return_value=self.scope))
            live = stack.enter_context(patch.object(issuer, 'require_scope'))
            yield live

    def test_both_administrator_and_scope_custody_are_required(self):
        with self.fixture() as live:
            self.assertEqual(issuer.current_issuer(self.plan['installation_id'], self.plan['stop_epoch']), self.evidence)
            issuer.require_issuer(self.evidence, self.plan)
            live.assert_called_once_with(self.scope, self.plan['installation_id'])

    def test_released_or_replaced_administrator_invalidates_a_still_live_scope(self):
        for changed in ({'status': 'released'}, {'pid': 124}, {'nonce': 'f' * 64}):
            with self.subTest(changed=changed), self.fixture() as live:
                with patch.object(issuer, 'read_protected_json', return_value={**self.owner, **changed}):
                    with self.assertRaises(ControlError):issuer.require_issuer(self.evidence, self.plan)
                live.assert_not_called()

    def test_scope_death_boot_stop_expiry_or_altered_identity_refuses_permission(self):
        with self.fixture(), patch.object(issuer, 'require_scope', side_effect=ControlError('control_stopped_repair_issuer_changed')):
            with self.assertRaises(ControlError):issuer.require_issuer(self.evidence, self.plan)
        for changed in ({'boot_id': '11111111-1111-1111-1111-111111111111'}, {'stop_epoch': 'f' * 64},
                        {'expires_at_monotonic': 99}, {'issuer_sha256': 'f' * 64}):
            with self.subTest(changed=changed), self.fixture(), self.assertRaises(ControlError):
                issuer.require_issuer(self.evidence, {**self.plan, **changed})

    def test_factory_cannot_borrow_another_live_root_issuer(self):
        with self.fixture(), patch.object(issuer.os, 'getpid', return_value=124):
            with self.assertRaises(ControlError):issuer.current_issuer(self.plan['installation_id'], self.plan['stop_epoch'])

    def test_root_issuer_module_has_no_canonical_or_app_imports(self):
        script = "import sys;import lifecycle.control.resumption_issuer;assert not {'lifecycle.state','lifecycle.management_journal','config','database','application'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':unittest.main()
