"""Read-only custody distinguishes a guarded candidate from its prior active pointer."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from lifecycle import managed_update, state, update_execution
from lifecycle.control import runtime_observation as observe
from lifecycle.execution_state import ExecutionRecord
from lifecycle.management_journal import execution_tuple_sha256
from tests.lifecycle import test_runtime_observation as runtime_fixture
from tests.lifecycle.test_update_execution import journal


class UpdateRuntimeObservationTests(unittest.TestCase):
    def setUp(self):
        fixture = runtime_fixture.ManagedRuntimeObservationTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.payload = journal()
        self.payload.update(lifecycle_generation=2, lifecycle_phase='recovery_required', phase='starting')
        self.payload['managed_execution'].update(supervisor_pid=4321, supervisor_start_identity='987',
            supervisor_invocation_id='a'*32, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        self.bound = update_execution.parse(self.payload)
        self.operation = self.bound.parent.operation_id
        self.authority = state.LifecycleState(2, 2, state.ActiveOperation(self.operation,
            state.OperationType.UPDATE, state.OwnerKind.UPDATER, state.LifecyclePhase.RECOVERY_REQUIRED, 'c'*64))
        fixture.selected.installation_id = self.bound.parent.installation_id
        fixture.snapshot.update(pid=4321, invocation='a'*32,
            execution_sha256=execution_tuple_sha256(self.bound.candidate))
        self.active = ExecutionRecord(self.bound.parent.installation_id, self.bound.parent.execution, str(uuid.uuid4()), 0)
        self.active_read = self.enterContext(patch.object(observe, 'read_active_execution', side_effect=lambda root: self.active))
        self.enterContext(patch.object(state, 'inspect_state', return_value=self.authority))
        self.enterContext(patch.object(managed_update, 'bound_journal', side_effect=lambda root, operation:
            (self.authority, self.payload, update_execution.parse(self.payload))))
        self.enterContext(patch('lifecycle.control.tickets.read_stop_epoch', return_value=self.bound.parent.stop_epoch))

    def observe(self):return observe.runtime_status(self.fixture.root)

    def accept(self, previous=False):
        selected = self.bound.parent.execution if previous else self.bound.candidate
        self.payload.update(runtime_committed=True, runtime_committed_at=1, phase='recovering',
            current_commit=selected.application_commit,
            managed_verification_execution_sha256=execution_tuple_sha256(selected),
            managed_accepted_execution_sha256=execution_tuple_sha256(selected))
        return selected

    def test_original_supervisor_with_unaccepted_candidate_is_active_despite_previous_pointer(self):
        before = json.dumps(self.payload, sort_keys=True)
        self.assertEqual(self.observe(), 'active')
        self.assertEqual(json.dumps(self.payload, sort_keys=True), before)
        self.fixture.stopped.assert_not_called();self.fixture.quiet.assert_not_called()

    def test_accepted_candidate_can_have_previous_pointer_only_as_forward_publication_gap(self):
        selected = self.accept()
        self.assertEqual(self.observe(), 'active')
        self.active = ExecutionRecord(self.bound.parent.installation_id, selected, self.operation, 2)
        self.assertEqual(self.observe(), 'active')
        self.fixture.snapshot['execution_sha256'] = execution_tuple_sha256(self.bound.parent.execution)
        self.assertEqual(self.observe(), 'unknown')

    def test_accepted_previous_runtime_cannot_be_relabelled_as_running_target(self):
        selected = self.accept(previous=True)
        self.fixture.snapshot['execution_sha256'] = execution_tuple_sha256(selected)
        self.assertEqual(self.observe(), 'active')
        self.fixture.snapshot['execution_sha256'] = execution_tuple_sha256(self.bound.candidate)
        self.assertEqual(self.observe(), 'unknown')

    def test_unaccepted_or_foreign_or_future_active_pointer_never_proves_custody(self):
        self.active = ExecutionRecord(self.bound.parent.installation_id, self.bound.candidate, self.operation, 2)
        self.assertEqual(self.observe(), 'unknown')
        selected = self.accept()
        self.active = ExecutionRecord(self.bound.parent.installation_id, selected, str(uuid.uuid4()), 2)
        self.assertEqual(self.observe(), 'unknown')
        self.active = ExecutionRecord(self.bound.parent.installation_id, selected, self.operation, 3)
        self.assertEqual(self.observe(), 'unknown')

    def test_replaced_invocation_stopped_epoch_or_malformed_journal_are_unknown(self):
        self.fixture.unit['InvocationID'] = 'b'*32
        self.assertEqual(self.observe(), 'unknown')
        self.fixture.unit['InvocationID'] = 'a'*32
        with patch('lifecycle.control.tickets.read_stop_epoch', return_value='f'*64):
            self.assertEqual(self.observe(), 'unknown')
        self.payload['managed_candidate'] = None
        self.assertEqual(self.observe(), 'unknown')
