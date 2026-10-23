from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle.control import recover, privileged, resumption_review, resumption_store, stopped_repair
from lifecycle.control.rpc import ControlError


class ScopedResumptionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.operation = str(uuid.uuid4())
        self.snapshot = {'operation_id': self.operation, 'installation_id': str(uuid.uuid4()),
            'generation': 4, 'initial_service_state': 'running', 'activation_mode': 'cutover',
            'supervisor_invocation_id': '1' * 32, 'permitted_actions': ['continue_recovery'],
            'evidence_sha256': 'a' * 64}
        self.stopped = {'initial_service_state': 'stopped', 'supervisor_invocation_id': None,
                        'supervisor': None, 'backend': None}
        self.enterContext(patch.object(recover, 'require_root'))
        self.enterContext(patch.object(recover, 'operation_unit', return_value={'ActiveState': 'inactive', 'MainPID': '0'}))
        self.enterContext(patch.object(privileged, 'unit_snapshot', return_value={'ActiveState': 'inactive', 'MainPID': '0'}))
        self.enterContext(patch('lifecycle.control.stop._require_empty'))
        self.enterContext(patch.object(stopped_repair, 'require_stopped'))
        self.enterContext(patch.object(recover, 'root_stop_epoch', return_value='b' * 64))
        self.enterContext(patch.object(recover, 'process_snapshot', return_value=self.stopped))
        self.worker = self.enterContext(patch.object(recover, 'run_worker', return_value=self.snapshot))
        self.make = self.enterContext(patch.object(resumption_review, 'make_review'))
        self.consent = self.enterContext(patch.object(resumption_review, 'consent', return_value=True))
        self.plan = {'plan_id': str(uuid.uuid4()), 'review_id': str(uuid.uuid4())}
        self.launch_plan = self.enterContext(patch.object(resumption_review, 'launch_plan', return_value=self.plan))
        self.publish = self.enterContext(patch.object(resumption_store, 'publish'))
        self.run = self.enterContext(patch.object(recover, 'run_operation'))
        self.enterContext(patch.object(recover, '_boot_enabled', return_value=True))
        self.enterContext(patch.object(stopped_repair, 'current_scope', return_value={'unit': 'owned-test-scope'}))
        self.enterContext(redirect_stdout(io.StringIO()))

    def invoke(self, **kwargs):
        return recover.recover(self.operation, request_path=Path('/test-request'),
            control_manifest_sha256='c' * 64, **kwargs)

    def test_outer_dispatch_hands_off_before_asking_for_start_consent(self):
        handoff = self.invoke()
        self.assertIsInstance(handoff, stopped_repair.StoppedRepairHandoff)
        self.assertEqual(handoff.preparation_id, self.operation)
        self.make.assert_not_called();self.publish.assert_not_called();self.run.assert_not_called()

    def test_read_only_review_cannot_mint_a_start_permission(self):
        self.assertEqual(self.invoke(review_only=True), 0)
        self.make.assert_not_called();self.publish.assert_not_called();self.run.assert_not_called()

    def test_declined_fresh_scope_cannot_publish_or_claim(self):
        self.consent.return_value = False
        self.assertEqual(self.invoke(scope_epoch='b' * 64), 0)
        self.publish.assert_not_called();self.run.assert_not_called()
        self.assertEqual(self.worker.call_count, 1)

    def test_matching_fresh_consent_publishes_plan_before_controller_dispatch(self):
        grant = {'schema_version': 3, 'request': {'controller_control_manifest_sha256': 'c' * 64}}
        def worker(arguments, **kwargs):
            if arguments == ['prepare-recovery']:
                payload = kwargs['payload']
                self.assertEqual(payload['processes'], self.stopped)
                self.assertEqual(payload['resumption_plan_id'], self.plan['plan_id'])
                self.assertEqual(payload['recovery_review_id'], self.plan['review_id'])
                return grant
            return self.snapshot
        self.worker.side_effect = worker
        def dispatch(payload, **kwargs):
            self.publish.assert_called_once_with(self.plan, self.make.return_value.issuer,
                                                control_manifest_sha256='c' * 64)
            self.assertIs(kwargs['recovery_grant'], grant)
        self.run.side_effect = dispatch
        with patch.object(recover, '_consent', side_effect=AssertionError('Legacy consent cannot authorize a start')):
            self.assertEqual(self.invoke(scope_epoch='b' * 64), 0)
        self.launch_plan.assert_called_once_with(self.make.return_value, approved=True)
        self.run.assert_called_once()

    def test_canonical_change_after_consent_prevents_permission_publication(self):
        self.worker.side_effect = [self.snapshot, {**self.snapshot, 'generation': 5}]
        with self.assertRaisesRegex(ControlError, 'control_recovery_review_stale'):
            self.invoke(scope_epoch='b' * 64)
        self.publish.assert_not_called();self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
