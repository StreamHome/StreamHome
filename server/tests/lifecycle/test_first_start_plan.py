from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import first_start_plan as plan, provisioning
from lifecycle.control import provisioning_contract as contract
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_provisioning import prepared_intent


class FirstStartIssuerPlanTests(unittest.TestCase):
    def setUp(self):
        self.intent = contract.advance_intent(prepared_intent(), stage='first_start_requested', start_permit='issued',
            start_boot_id=str(uuid.uuid4()), start_stop_epoch='a' * 64)
        self.scope = {'unit': 'streamhome-install-' + self.intent.installation_id + '-' + '2' * 32 + '.scope', 'invocation': '3' * 32}
        self.owner = {'schema_version': 1, 'pid': 321, 'start_identity': '123', 'boot_id': self.intent.start_boot_id,
            'nonce': 'e' * 64, 'status': 'active'}
        self.value = {'schema_version': 1, 'action': 'first_start', 'installation_id': self.intent.installation_id,
            'operation_id': self.intent.operation_id, 'review_id': self.intent.operation_id,
            'issued_intent_sha256': contract.intent_sha256(self.intent), 'execution_sha256': self.intent.execution_sha256,
            'boot_id': self.intent.start_boot_id, 'stop_epoch': self.intent.start_stop_epoch,
            'scope_unit': self.scope['unit'], 'scope_invocation': self.scope['invocation'], 'owner_sha256': plan._hash(self.owner),
            'expires_at_monotonic': 1000, 'nonce': 'f' * 64}

    @contextmanager
    def fixtures(self):
        def path(name):
            if name == '/proc/sys/kernel/random/boot_id':
                return SimpleNamespace(read_text=lambda: self.intent.start_boot_id)
            if name == '/proc/321':
                return SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=0))
            if name == '/proc/321/cgroup':
                return SimpleNamespace(read_text=lambda: '0::/system.slice/' + self.scope['unit'] + '\n')
            return Path(name)
        with ExitStack() as stack:
            stack.enter_context(patch.object(plan, 'require_root'))
            stack.enter_context(patch.object(plan, 'Path', side_effect=path))
            stack.enter_context(patch.object(plan, 'read_protected_json', side_effect=lambda name, **kwargs: self.owner if name == plan.OWNER else self.value))
            stack.enter_context(patch.object(plan, 'process_identity', return_value=('123', 'S')))
            stack.enter_context(patch.object(plan, 'read_stop_epoch', return_value=self.intent.start_stop_epoch))
            stack.enter_context(patch.object(plan.os, 'getpid', return_value=321))
            stack.enter_context(patch.object(plan.time, 'monotonic', return_value=100))
            scope = stack.enter_context(patch.object(plan, '_require_install_scope', return_value=self.scope))
            metadata = 'ActiveState=active\nTransient=yes\nControlGroup=/system.slice/' + self.scope['unit'] + '\nInvocationID=' + self.scope['invocation']
            ctl = stack.enter_context(patch.object(plan, 'systemctl', return_value=metadata))
            yield scope, ctl

    def test_live_exact_issuer_can_use_only_its_immutable_plan(self):
        with self.fixtures():
            self.assertEqual(plan.require_issued_plan(self.intent, issuer=True), self.value)

    def test_expiry_stop_owner_change_and_different_scope_block_permission(self):
        for scenario in ('expired', 'stop', 'owner', 'scope', 'retarget'):
            with self.subTest(scenario=scenario), self.fixtures() as (scope, ctl):
                original = dict(self.value)
                try:
                    if scenario == 'expired':self.value['expires_at_monotonic'] = 99
                    elif scenario == 'stop':self.value['stop_epoch'] = 'b' * 64
                    elif scenario == 'owner':self.value['owner_sha256'] = 'b' * 64
                    elif scenario == 'scope':ctl.return_value = 'ActiveState=inactive'
                    else:scope.return_value = {**self.scope, 'invocation': '4' * 32}
                    with self.assertRaises(ControlError):
                        plan.require_issued_plan(self.intent, issuer=True)
                finally:self.value = original

    def test_publication_does_not_overwrite_an_existing_review(self):
        with tempfile.TemporaryDirectory() as directory, self.fixtures(), patch.object(plan, 'PLANS', Path(directory)), patch.object(plan, 'root_directory', side_effect=lambda *args, **kwargs: os.open(directory, os.O_RDONLY | os.O_DIRECTORY)), patch.object(plan.secrets, 'token_hex', return_value='f' * 64), patch.object(plan, 'atomic_root_json') as write:
            plan.publish_start_plan(self.intent)
            emitted = write.call_args.args[1]
            self.assertEqual(emitted['scope_unit'], self.scope['unit'])
            self.assertEqual(emitted['owner_sha256'], plan._hash(self.owner))
            self.assertEqual(emitted['issued_intent_sha256'], contract.intent_sha256(self.intent))
            (Path(directory) / (self.intent.operation_id + '.json')).write_text(json.dumps(emitted))
            with self.assertRaisesRegex(ControlError, 'control_start_plan_already_issued'):
                plan.publish_start_plan(self.intent)
            self.assertEqual(write.call_count, 1)

    def test_old_cleanup_cannot_stop_a_later_administrators_builder(self):
        with patch.object(provisioning, 'systemctl', side_effect=['loaded', '']) as ctl, patch('lifecycle.control.stop._require_empty'), patch.object(provisioning, 'administrative_lock', side_effect=ControlError('control_administrator_busy')), patch.object(provisioning, 'read_intent') as read:
            provisioning._stop_install_scope_work(self.intent.installation_id, scope_unit=self.scope['unit'])
        self.assertEqual(ctl.call_args.args, ('stop', self.scope['unit']))
        self.assertEqual(ctl.call_count, 2)
        read.assert_not_called()

    def test_new_issuer_cannot_reuse_a_cancelled_issued_permission(self):
        with patch('lifecycle.control.administration.root_stop_epoch', return_value=self.intent.start_stop_epoch), patch.object(provisioning, 'unit_snapshot', return_value={'ActiveState': 'inactive', 'MainPID': '0'}), patch.object(plan, 'require_issued_plan', side_effect=ControlError('control_start_plan_issuer_changed')) as authorize, patch.object(provisioning, '_advance') as persist, patch.object(provisioning, 'systemctl') as ctl:
            with self.assertRaisesRegex(ControlError, 'control_start_plan_issuer_changed'):
                provisioning._first_start(self.intent, expected_stop_epoch=self.intent.start_stop_epoch)
        authorize.assert_called_once_with(self.intent, issuer=True)
        persist.assert_not_called()
        ctl.assert_not_called()


if __name__ == '__main__':
    unittest.main()
