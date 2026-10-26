"""Root review/consent/OS dispatch with explicit substituted root and process effects."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import update_recover as control
from lifecycle.control import recover as dispatch
from lifecycle.control.rpc import ControlError
from lifecycle.control.stopped_repair import StoppedRepairHandoff


class UpdateRecoveryControlTests(unittest.TestCase):
    def setUp(self):
        self.installation = str(uuid.uuid4());self.operation = str(uuid.uuid4())
        self.snapshot = {'operation_type': 'update', 'installation_id': self.installation,
            'operation_id': self.operation, 'generation': 7, 'evidence_sha256': 'a'*64,
            'permitted_actions': ['continue_recovery'], 'runtime_status': 'active',
            'original_supervisor_invocation_id': 'b'*32, 'accepted_execution_sha256': 'e'*64}
        self.processes = {'initial_service_state': 'running', 'supervisor_invocation_id': 'b'*32,
            'supervisor': {'pid': 4321, 'start_identity': '987'}, 'backend': None}
        self.enterContext(patch.object(control, 'require_root'))
        self.epoch = self.enterContext(patch.object(control, 'root_stop_epoch', return_value='c'*64))
        self.process_reader = self.enterContext(patch.object(control, 'process_snapshot', return_value=self.processes))
        self.enterContext(patch.object(control, 'current_scope', return_value={'unit': 'synthetic-root-scope'}))
        self.worker = self.enterContext(patch.object(control, 'run_worker', return_value=self.snapshot))
        self.execute = self.enterContext(patch.object(control, '_run'))
        self.enterContext(redirect_stdout(StringIO()))

    def recover(self, **changes):
        arguments = {'request_path': None, 'control_manifest_sha256': 'd'*64, 'review_only': False,
            'action': 'continue_recovery', 'scope_epoch': None}
        return control.recover(self.snapshot, **{**arguments, **changes})

    def test_read_only_review_does_not_start_scope_or_inspect_mutable_processes(self):
        self.snapshot['permitted_actions'] = []
        self.assertEqual(self.recover(review_only=True), 0)
        self.worker.assert_not_called();self.execute.assert_not_called();self.process_reader.assert_not_called()

    def test_running_recovery_requires_existing_cancellable_root_scope_handoff(self):
        result = self.recover()
        self.assertIsInstance(result, StoppedRepairHandoff)
        self.assertEqual(result.preparation_id, self.operation)
        self.worker.assert_not_called();self.execute.assert_not_called()

    def test_decline_or_new_stop_never_prepares_or_dispatches(self):
        with patch.object(dispatch, '_consent', return_value=False):
            self.assertEqual(self.recover(scope_epoch='c'*64, request_path=Path('/synthetic-request')), 0)
        self.worker.assert_not_called();self.execute.assert_not_called()
        self.epoch.return_value = 'f'*64
        with self.assertRaises(ControlError):self.recover(scope_epoch='c'*64)

    def test_replaced_supervisor_stale_review_and_offline_request_cannot_dispatch(self):
        self.process_reader.return_value = {**self.processes, 'supervisor_invocation_id': 'f'*32}
        with self.assertRaises(ControlError):self.recover()
        self.process_reader.return_value = self.processes
        self.worker.return_value = {**self.snapshot, 'evidence_sha256': 'f'*64}
        with patch.object(dispatch, '_consent', return_value=True), self.assertRaises(ControlError):
            self.recover(scope_epoch='c'*64, request_path=Path('/synthetic-request'))
        self.snapshot['runtime_status'] = 'offline'
        with patch.object(control, 'require_stopped', side_effect=ControlError('not positively stopped')):
            with self.assertRaises(ControlError):self.recover()
        self.execute.assert_not_called()

    def test_fresh_consent_rechecks_original_review_then_prepares_and_dispatches_exact_grant(self):
        grant = {'control_manifest_sha256': 'd'*64}
        self.worker.side_effect = [self.snapshot, grant]
        with patch.object(dispatch, '_consent', return_value=True):
            self.assertEqual(self.recover(scope_epoch='c'*64, request_path=Path('/synthetic-request')), 0)
        self.assertEqual(self.worker.call_args_list[0].args[0], ['recovery-review', self.operation])
        request = self.worker.call_args_list[1].kwargs['payload']
        self.assertEqual(request['operation_id'], self.operation)
        self.assertEqual(request['expected_evidence_sha256'], self.snapshot['evidence_sha256'])
        self.assertEqual(request['action'], 'continue_recovery')
        self.execute.assert_called_once_with(grant)

    def test_existing_recover_dispatch_routes_update_review_to_its_own_adapter(self):
        with patch.object(dispatch, 'require_root'), patch.object(dispatch, 'operation_unit',
                return_value={'ActiveState': 'inactive', 'MainPID': '0'}), patch(
                'lifecycle.control.stop._require_empty'), patch.object(dispatch, 'root_stop_epoch',
                return_value='c'*64), patch.object(dispatch, 'run_worker', return_value=self.snapshot), patch.object(
                control, 'recover', return_value=8) as recover:
            self.assertEqual(dispatch.recover(self.operation, request_path=None,
                control_manifest_sha256='d'*64, review_only=True), 8)
        recover.assert_called_once_with(self.snapshot, request_path=None,
            control_manifest_sha256='d'*64, review_only=True, action='continue_recovery', scope_epoch=None)

    def test_abort_requires_positive_stopped_proof_and_only_returns_cancellable_handoff(self):
        self.snapshot.update(runtime_status='offline', permitted_actions=['safe_abort'])
        self.process_reader.return_value = {'initial_service_state': 'stopped'}
        with patch.object(control, 'require_stopped') as stopped:
            result = self.recover(action='safe_abort')
        self.assertIsInstance(result, StoppedRepairHandoff)
        stopped.assert_called_once();self.worker.assert_not_called();self.execute.assert_not_called()
        with patch.object(control, 'require_stopped', side_effect=ControlError('processes remain')):
            with self.assertRaises(ControlError):self.recover(action='safe_abort')
        self.snapshot['runtime_status'] = 'active'
        with self.assertRaises(ControlError):self.recover(action='safe_abort')

    def test_offline_continuation_requires_fresh_start_challenge_then_publishes_plan_before_dispatch(self):
        from types import SimpleNamespace
        from lifecycle.control import parent_resumption_review, resumption_review, resumption_store
        self.snapshot['runtime_status'] = 'offline'
        self.process_reader.return_value = {'initial_service_state': 'stopped'}
        review = SimpleNamespace(issuer={'scope': 'synthetic-root-scope'})
        plan = {'plan_id': str(uuid.uuid4()), 'review_id': str(uuid.uuid4())}
        grant = {'control_manifest_sha256': 'd'*64, 'schema_version': 2}
        self.worker.side_effect = [self.snapshot, grant]
        events = []
        self.execute.side_effect = lambda selected: events.append('dispatch')
        with (patch.object(control, 'require_stopped'),
                patch.object(parent_resumption_review, 'make_review', return_value=review),
                patch.object(resumption_review, 'consent', return_value=True),
                patch.object(parent_resumption_review, 'launch_plan', return_value=plan),
                patch.object(resumption_store, 'publish', side_effect=lambda *args, **kwargs: events.append('publish')),
                patch.object(dispatch, '_consent', side_effect=AssertionError('Old consent cannot approve OS start'))):
            self.assertEqual(self.recover(scope_epoch='c'*64, request_path=Path('/new-challenge')), 0)
        payload = self.worker.call_args_list[1].kwargs['payload']
        self.assertEqual(payload['resumption_plan_id'], plan['plan_id'])
        self.assertEqual(payload['review_id'], plan['review_id'])
        self.assertEqual(events, ['publish', 'dispatch'])
