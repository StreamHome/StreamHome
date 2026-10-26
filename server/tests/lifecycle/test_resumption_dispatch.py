import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle.control import resumption_dispatch as dispatch, resumption_store as store
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_resumption_store as store_fixture


class ResumptionDispatchTests(unittest.TestCase):
    def setUp(self):
        self.prepare()

    def prepare(self, *, control=False):
        fixture = store_fixture.ResumptionStoreTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture = fixture;self.plan = fixture.plan;self.manifest = fixture.manifest
        if control:
            self.plan.update(schema_version=3,action='provision_control')
            self.plan['stages'][0]['control_manifest_sha256']='1'*64
            self.plan['stages'][1]['control_manifest_sha256']=self.manifest
        fixture.publish()
        self.grant = {'schema_version': 3, 'resumption_plan_id': self.plan['plan_id'],
            'recovery_review_id': self.plan['review_id'], 'issuer_scope': fixture.evidence['scope'],
            'request': {'operation_id': self.plan['operation_id'], 'installation_id': self.plan['installation_id'],
                'expected_generation': self.plan['generation'] - 1, 'action': self.plan['action'],
                'initial_service_state': 'running', 'activation_mode': 'cutover',
                'controller_control_manifest_sha256': self.manifest}}
        self.enterContext(patch.object(dispatch, 'require_root'))
        self.enterContext(patch.object(dispatch, 'descriptor', store.descriptor))
        self.enterContext(patch.object(dispatch, '_verify_owned_units'))
        self.enterContext(patch.object(dispatch, 'operation_unit', return_value={'ActiveState': 'active', 'MainPID': '123', 'InvocationID': '2' * 32}))
        observed=fixture.observation()
        if control:observed['control_manifest_sha256']='1'*64
        self.worker = self.enterContext(patch.object(dispatch, 'run_worker', return_value=observed))
        self.enterContext(patch.object(dispatch, 'unit_snapshot', return_value={'ActiveState': 'active', 'MainPID': '321', 'InvocationID': '3' * 32}))
        self.enterContext(patch.object(dispatch, 'process_cgroup', return_value=dispatch.RUNTIME_CGROUP))
        self.enterContext(patch.object(dispatch, 'supervisor_snapshot', return_value={'launches_open': True}))
        self.systemctl = self.enterContext(patch.object(dispatch, 'systemctl', side_effect=self.start_unit))
        self.enterContext(patch.object(dispatch, 'Path', side_effect=lambda value: SimpleNamespace(read_text=lambda: '0::' + dispatch.RUNTIME_CGROUP) if value == '/proc/self/cgroup' else Path(value)))
        self.enterContext(patch.object(dispatch, 'read_protected_json', side_effect=lambda path: self.grant if path.name.startswith('recovery-') else json.loads(path.read_text())))

    def start_unit(self, *arguments, **kwargs):
        self.assertEqual(arguments, ('start', 'streamhome.service'))
        store.bind_current_start(control_manifest_sha256=self.plan['stages'][0].get('control_manifest_sha256',self.manifest))
        return ''

    def start(self):return dispatch.start(self.grant, control_manifest_sha256=self.manifest)

    def test_control_prestart_revalidates_with_old_pinned_worker_and_binds_new_runtime_only(self):
        self.prepare(control=True)
        observed={**self.fixture.observation(),'control_manifest_sha256':'1'*64}
        self.fixture.consume(observation=observed)
        with self.assertRaises(ControlError):dispatch.before_start(control_manifest_sha256=self.manifest)
        self.worker.assert_not_called()
        dispatch.before_start(control_manifest_sha256='1'*64)
        self.assertEqual(self.worker.call_args.kwargs['reader_operation_id'],self.plan['operation_id'])
        self.assertEqual(json.loads(self.fixture.receipt_path().read_bytes())['status'],'bound')
        with self.assertRaises(ControlError):dispatch.before_start(control_manifest_sha256='1'*64)

    def test_control_issuer_dispatches_only_once_using_old_pinned_observer(self):
        self.prepare(control=True)
        self.assertTrue(self.start());self.assertTrue(self.start())
        self.assertEqual(self.systemctl.call_count,1)
        self.assertEqual(self.worker.call_args.kwargs['reader_operation_id'],self.plan['operation_id'])

    def test_consumed_start_observes_its_bound_invocation_without_dispatching_again(self):
        self.assertTrue(self.start())
        self.assertTrue(self.start())
        self.assertEqual(self.systemctl.call_count, 1)
        self.assertEqual(self.worker.call_count, 1)
        self.assertEqual(json.loads(self.fixture.receipt_path().read_text())['status'], 'bound')

    def test_failed_speculative_uid_read_never_consumes_permission_before_a_later_exact_observation(self):
        self.worker.side_effect=[ControlError('control_worker_failed'),ControlError('control_controller_changed'),
            self.fixture.observation()]
        self.assertFalse(self.start());self.assertFalse(self.start())
        self.systemctl.assert_not_called()
        self.assertFalse(self.fixture.receipt_path().exists())
        self.assertTrue(self.start())
        self.systemctl.assert_called_once()
        self.assertEqual(json.loads(self.fixture.receipt_path().read_text())['status'],'bound')

    def test_speculative_failure_cannot_wait_if_another_start_consumed_the_stage(self):
        def failed(*args,**kwargs):
            self.fixture.consume()
            raise ControlError('control_worker_failed')
        self.worker.side_effect=failed
        with self.assertRaisesRegex(ControlError,'control_resumption_outcome_unknown'):self.start()
        self.systemctl.assert_not_called()
        self.assertEqual(json.loads(self.fixture.receipt_path().read_text())['status'],'consumed')

    def test_speculative_failure_rechecks_issuer_and_preserves_nontransient_refusals(self):
        permission=dispatch.qualify(self.grant,control_manifest_sha256=self.manifest)
        self.worker.side_effect=ControlError('control_worker_failed')
        with patch.object(dispatch,'qualify',side_effect=[permission,ControlError('control_operator_stopped')]), \
                self.assertRaisesRegex(ControlError,'control_operator_stopped'):self.start()
        self.worker.side_effect=ControlError('control_resumption_review_changed')
        with self.assertRaisesRegex(ControlError,'control_resumption_review_changed'):self.start()
        self.systemctl.assert_not_called()
        self.assertFalse(self.fixture.receipt_path().exists())

    def test_lost_os_reply_cannot_retry_a_consumed_start(self):
        self.systemctl.side_effect = subprocess.TimeoutExpired('fixed systemctl start', 30)
        with self.assertRaisesRegex(ControlError, 'control_resumption_outcome_unknown'):self.start()
        with self.assertRaisesRegex(ControlError, 'control_resumption_outcome_unknown'):self.start()
        self.assertEqual(self.systemctl.call_count, 1)
        self.assertEqual(json.loads(self.fixture.receipt_path().read_text())['status'], 'consumed')

    def test_prestart_hook_revalidates_uid_authority_and_binds_its_invocation_once(self):
        self.fixture.consume()
        dispatch.before_start(control_manifest_sha256=self.manifest)
        arguments = self.worker.call_args
        self.assertEqual(arguments.args[0], ['resumption-observe'])
        self.assertEqual(arguments.kwargs['payload']['startup_hook']['pid'], os.getpid())
        self.assertEqual(json.loads(self.fixture.receipt_path().read_text())['invocation'], '3' * 32)
        with self.assertRaisesRegex(ControlError, 'control_resumption_already_consumed'):
            dispatch.before_start(control_manifest_sha256=self.manifest)

    def test_other_parent_or_fallback_stage_cannot_borrow_this_dispatcher(self):
        self.fixture.consume();self.fixture.consume(1)
        with self.assertRaisesRegex(ControlError, 'control_resumption_mode_unavailable'):
            dispatch.before_start(control_manifest_sha256=self.manifest)
        self.worker.assert_not_called();self.systemctl.assert_not_called()

    def test_root_dispatch_imports_no_canonical_or_application_code(self):
        script = "import sys;import lifecycle.control.resumption_dispatch;assert not {'lifecycle.state','lifecycle.management_journal','config','database','application'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':unittest.main()
