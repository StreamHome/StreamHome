"""Initial fixed-unit UPDATE dispatch; actual canonical state with OS/build boundaries substituted."""
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import managed_update, state, update_controller
from lifecycle.control.rpc import ControlError
from lifecycle.execution_state import ExecutionRecord
from tests.lifecycle import test_update_continuation as continuation_fixture
from tests.lifecycle import test_managed_update_preparation as preparation_fixture


class InitialUpdateDispatchTests(unittest.TestCase):
    def setUp(self):
        fixture = continuation_fixture.UpdateContinuationTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation
        self.actor = {'ActiveState': 'active', 'MainPID': str(os.getpid()), 'InvocationID': '2'*32}
        self.record = {'uid': os.geteuid(), 'status': 'S', 'executable': os.path.realpath('/usr/bin/python3'),
            'arguments': [b'/usr/bin/python3', b'-I', b'/usr/local/libexec/streamhome/provision',
                b'--operation', self.operation.encode(), b'']}
        self.enterContext(patch('lifecycle.control.processes.controller_peer'))
        self.enterContext(patch('lifecycle.control.administration.operation_unit', return_value=self.actor))
        self.enterContext(patch.object(managed_update, '_process_record', return_value=self.record))
        self.enterContext(patch.object(managed_update, 'read_active_execution', return_value=ExecutionRecord(
            fixture.installation, fixture.execution, self.operation, 0)))
        self.enterContext(patch.dict(os.environ, {'INVOCATION_ID': '2'*32}))
        self.digest = fixture.parent.execution.control_manifest_sha256

    def observe(self):
        return managed_update.require_initial_dispatch(self.root, self.operation, control_manifest_sha256=self.digest)

    def test_exact_initial_reader_owner_and_original_parent_are_read_only(self):
        before = self.fixture.path.read_bytes(), state.inspect_state(self.root)
        self.assertEqual(self.observe()[2].parent, self.fixture.parent)
        with self.assertRaises(ControlError):
            managed_update.require_initial_dispatch(self.root, self.operation, control_manifest_sha256='f'*64)
        self.actor['MainPID'] = '1'
        with self.assertRaises(ControlError):self.observe()
        self.actor['MainPID'] = str(os.getpid());self.record['arguments'] = [b'arbitrary-command']
        with self.assertRaises(ControlError):self.observe()
        self.assertEqual(before, (self.fixture.path.read_bytes(), state.inspect_state(self.root)))

    def test_original_engine_receives_exact_request_and_rechecks_before_transfer(self):
        before = state.inspect_state(self.root)
        def engine(root, **kwargs):
            self.assertEqual(root, self.root);self.assertEqual(kwargs['generation'], 1)
            self.assertEqual(kwargs['operation_id'], self.operation)
            self.assertTrue(kwargs['managed_initial']);self.assertFalse(kwargs['recovery'])
            self.assertIsNone(kwargs['acknowledgement_fd'])
            kwargs['pre_transfer_check']()
            original = self.fixture.path.read_bytes();payload = json.loads(original)
            payload['target_commit'] = 'f'*40;self.fixture.path.write_text(json.dumps(payload))
            with self.assertRaises(ControlError):kwargs['pre_transfer_check']()
            self.fixture.path.write_bytes(original)
            return 7
        with patch.object(update_controller, '_run_controller', side_effect=engine) as execute:
            self.assertEqual(managed_update.execute(self.root, self.operation, control_manifest_sha256=self.digest), 7)
        self.assertEqual(execute.call_count, 1);self.assertEqual(state.inspect_state(self.root), before)

    def test_initial_dispatch_cannot_reinterpret_a_transferred_parent_as_recovery_permission(self):
        self.fixture.transfer()
        with patch.object(update_controller, '_run_controller') as engine:
            with self.assertRaises(ControlError):
                managed_update.execute(self.root, self.operation, control_manifest_sha256=self.digest)
            engine.assert_not_called()
        self.assertEqual(state.inspect_state(self.root).generation, 2)


class InitialUpdatePreparationTests(unittest.TestCase):
    def setUp(self):
        class Fixture(preparation_fixture.ManagedUpdatePreparationFixture, unittest.TestCase):pass
        fixture = Fixture();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture = fixture;self.root = fixture.root
        payload = update_controller.read_update_journal(fixture.path);payload['phase'] = 'queued'
        fixture.path.write_text(json.dumps(payload))
        self.source = fixture.source;self.source.mkdir(parents=True, mode=0o700)
        self.script = self.source/'scripts/runtime/update.sh';self.script.parent.mkdir(parents=True)
        self.script.write_text('#!/usr/bin/env bash\nset -uo pipefail\n# StreamHome managed update engine: 1\n')
        self.enterContext(patch.object(managed_update, 'require_owner_peer', side_effect=lambda root, lease, pid:
            managed_update.bound_journal(root, lease.operation_id)))
        self.checkout = self.enterContext(patch.object(managed_update, 'prepare_checkout', return_value=self.source))
        self.prepare = self.enterContext(patch.object(managed_update, 'prepare_candidate'))

    def test_preparation_uses_existing_transfer_before_any_script_can_start(self):
        original = state.inspect_state(self.root)
        managed_update.prepare_initial(self.root, self.fixture.lease)
        self.checkout.assert_called_once_with(self.root, self.fixture.lease)
        self.prepare.assert_called_once_with(self.root, self.fixture.lease)
        self.assertEqual(update_controller.read_update_journal(self.fixture.path)['phase'], 'preflight')
        self.assertEqual(state.inspect_state(self.root), original)
        self.assertFalse(update_controller.read_update_journal(self.fixture.path)['runtime_committed'])

    def test_unsupported_exact_target_keeps_inputs_and_never_builds_or_accepts(self):
        self.script.write_text('#!/usr/bin/env bash\nexit 0\n');original = self.script.read_bytes()
        with self.assertRaisesRegex(ControlError, 'control_update_target_unsupported'):
            managed_update.prepare_initial(self.root, self.fixture.lease)
        self.prepare.assert_not_called();self.assertEqual(self.script.read_bytes(), original)
        self.assertEqual(state.inspect_state(self.root).generation, 2)
        self.assertFalse(update_controller.read_update_journal(self.fixture.path)['runtime_committed'])
