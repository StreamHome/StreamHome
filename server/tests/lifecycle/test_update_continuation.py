"""Real settled admission/canonical transfers; OS parent/unit projections substituted."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bootstrap, managed_update, runtime_admission, state, update_controller, update_execution, update_history
from lifecycle.execution_state import ExecutionRecord
from lifecycle.management_journal import parse_execution_tuple
from tests.lifecycle.test_update_execution import journal


class UpdateContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)/'application';self.root.mkdir(mode=0o700)
        (self.root/'server').mkdir(mode=0o700)
        for name in ('start.sh', 'update.sh'):(self.root/name).write_text('#!/bin/sh\n')
        paths = state.lifecycle_paths(self.root);state._ensure_lifecycle_directories(paths)
        state._write_state_unlocked(paths, state.LifecycleState(2, 0, None))
        payload = journal();self.parent_payload = payload['managed_execution']
        self.execution = parse_execution_tuple(self.parent_payload['execution'])
        self.installation = self.parent_payload['installation_id']
        self.context = bootstrap.evaluate_startup(self.root, {})
        self.admission = runtime_admission.RuntimeAdmission(self.context, self.installation, '1'*32, self.execution)
        ready = bootstrap.StartupReadiness(self.context)
        ready.record_recovery(asyncio.run(bootstrap.run_mandatory_recovery(self.context, ())))
        ready.mark_workers_started();ready.mark_general_ready();self.admission.settle(ready)
        self.assertTrue(self.admission.admitted())
        claim = state.claim_operation_with_credential(self.root, operation_type='update', owner_kind='updater')
        self.operation = claim.state.active_operation.operation_id
        payload.update(transaction_id=self.operation, phase='queued', managed_candidate=None)
        payload['managed_execution'].update(operation_id=self.operation, supervisor_pid=123456,
            supervisor_start_identity='999', boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        self.parent_payload = payload['managed_execution']
        self.path, _, _ = update_controller._update_paths(self.root, self.operation, 1)
        self.path.write_text(json.dumps(payload));self.path.chmod(0o600)
        self.parent = update_execution.parse(payload).parent
        from lifecycle.layout import layout_for_root
        from types import SimpleNamespace
        layout = layout_for_root(self.root)
        self.layout = replace(layout, descriptor=SimpleNamespace(descriptor=SimpleNamespace(
            service_uid=os.geteuid(), installation_id=self.installation)))
        self.enterContext(patch.object(managed_update, 'layout_for_root', return_value=self.layout))
        active = ExecutionRecord(self.installation, self.execution, str(uuid.uuid4()), 0)
        self.enterContext(patch('lifecycle.update_continuation.read_active_execution', return_value=active))
        self.enterContext(patch('lifecycle.control.privileged.unit_snapshot', return_value={
            'ActiveState': 'active', 'MainPID': '123456', 'InvocationID': '1'*32}))
        self.enterContext(patch('lifecycle.control.tickets.read_stop_epoch', return_value=self.parent.stop_epoch))
        self.enterContext(patch('lifecycle.control.processes.process_cgroup', return_value='/system.slice/streamhome.service'))
        self.enterContext(patch.object(os, 'getppid', return_value=123456))
        start = state._process_start_identity
        self.enterContext(patch.object(state, '_process_start_identity', side_effect=lambda pid: '999' if pid == 123456 else start(pid)))
        # The actual owner's unit argv/cgroup contract has separate strict tests.
        self.owner = self.enterContext(patch.object(managed_update, 'require_owner_peer',
            side_effect=lambda root, lease, pid: managed_update.bound_journal(root, lease.operation_id)))
        self.enterContext(patch('lifecycle.control.reader_support.require_managed_update_reader'))
        self.lease = None
        self.addCleanup(lambda: self.lease.release() if self.lease is not None and not self.lease._released else None)

    def transfer(self):
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=1)
        self.lease, _ = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation,
            generation=1, expected_phase='reserved', owner_capability=capability, next_owner_kind='updater')
        update_controller._record_transferred_generation(self.root, operation_id=self.operation,
            previous_generation=1, generation=2, lifecycle_phase=state.LifecyclePhase.RESERVED,
            target_commit='e'*40, previous_commit='a'*40)
        update_controller.mutate_update_journal(self.root, operation_id=self.operation, generation=2,
            expected_lifecycle_phase='reserved', changes={'phase': 'preflight'})

    def test_only_original_settled_memory_acknowledges_and_stays_live_during_reserved_preflight(self):
        self.assertFalse(self.admission.admitted())
        acknowledgement = self.admission.acknowledge_update_reservation(self.operation)
        self.assertTrue(acknowledgement['acknowledged']);self.assertTrue(self.admission.admitted())
        self.transfer();self.assertTrue(self.admission.admitted())
        self.assertEqual(self.context.generation, 0);self.assertEqual(self.admission.generation, 0)
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='reserved', next_phase='exclusive')
        self.assertFalse(self.admission.admitted())
        fresh = runtime_admission.RuntimeAdmission(self.context, self.installation, '1'*32, self.execution)
        self.assertFalse(fresh.admitted())
        with self.assertRaises(runtime_admission.RuntimeAdmissionError):fresh.acknowledge_update_reservation(self.operation)

    def test_changed_request_stop_epoch_or_original_parent_closes_continuation(self):
        self.admission.acknowledge_update_reservation(self.operation)
        original = self.path.read_bytes()
        payload = json.loads(original);payload['target_commit'] = 'f'*40;self.path.write_text(json.dumps(payload))
        self.assertFalse(self.admission.admitted())
        self.path.write_bytes(original);self.assertTrue(self.admission.admitted())
        with patch('lifecycle.control.tickets.read_stop_epoch', return_value='f'*64):
            self.assertFalse(self.admission.admitted())
        with patch.object(os, 'getppid', return_value=123457):self.assertFalse(self.admission.admitted())
        self.assertTrue(self.admission.admitted())

    def test_dead_or_unproven_transferred_controller_cannot_keep_work_admitted(self):
        self.admission.acknowledge_update_reservation(self.operation);self.transfer()
        from lifecycle.control.rpc import ControlError
        self.owner.side_effect = ControlError('control_controller_changed')
        self.assertFalse(self.admission.admitted())
        self.assertEqual(self.admission.generation, 0)

    def test_cancelled_unprepared_reservation_needs_retained_history_before_memory_settles(self):
        self.admission.acknowledge_update_reservation(self.operation)
        payload = update_controller.read_update_journal(self.path)
        payload.update(phase='update_available')
        self.path.write_text(json.dumps(payload))
        state.complete_operation_with_credential(self.root, operation_id=self.operation, generation=1,
            expected_phase='reserved', expected_operation_type='update')
        self.assertFalse(self.admission.admitted())
        path = update_history._path(self.root, self.operation, 1, create=True)
        state._atomic_write_json(path, update_history._record(payload))
        # Discovery may later replace the global status; retained evidence is exact.
        self.path.unlink()
        self.assertTrue(self.admission.admitted())
        self.assertEqual(self.admission.generation, 1);self.assertEqual(self.context.generation, 0)
        self.assertIsNone(self.admission._pending_update)

    def test_retained_receipt_alone_cannot_admit_a_replacement_process_or_another_request(self):
        self.admission.acknowledge_update_reservation(self.operation)
        fresh = runtime_admission.RuntimeAdmission(self.context, self.installation, '1'*32, self.execution)
        self.assertFalse(fresh.admitted())
        with self.assertRaises(runtime_admission.RuntimeAdmissionError):self.admission.acknowledge_update_reservation(self.operation)
        with self.assertRaises(runtime_admission.RuntimeAdmissionError):self.admission.acknowledge_continuation(str(uuid.uuid4()), controller_pid=os.getpid())


    def start(self):
        return managed_update.spawn_initial(self.root, operation_id=self.operation, generation=1,
            target_commit='e'*40, previous_commit='a'*40, automatic=False,
            install_mode='when_idle', update_branch='main')

    def test_fixed_unit_handoff_observes_original_transfer_without_spawning_detached_process(self):
        self.admission.acknowledge_update_reservation(self.operation)
        def request(*args, **kwargs):
            self.assertEqual(kwargs['action'], 'start_update_operation')
            self.assertEqual(kwargs['arguments'], {'operation_id': self.operation, 'generation': 1})
            self.assertEqual(kwargs['expected_pid'], self.parent.supervisor_pid)
            self.transfer()
            return {'operation_id': self.operation, 'generation': 2, 'controller_pid': self.lease.lease.pid}
        with patch.object(runtime_admission, 'require_runtime_admission', return_value=self.admission), patch(
                'lifecycle.control.rpc.request', side_effect=request):
            result = self.start()
        self.assertEqual(result.generation, 2);self.assertEqual(result.controller_pid, self.lease.lease.pid)
        self.assertFalse(hasattr(result, 'process'))
        self.assertTrue(self.admission.admitted())

    def test_missing_memory_acknowledgement_never_requests_an_os_unit(self):
        from lifecycle.control.rpc import ControlError
        with patch.object(runtime_admission, 'require_runtime_admission', return_value=self.admission), patch(
                'lifecycle.control.rpc.request') as request:
            with self.assertRaises(ControlError):self.start()
            request.assert_not_called()

    def test_ambiguous_handoff_keeps_actual_transferred_authority_without_stopping_or_retrying(self):
        from lifecycle.control.rpc import ControlError
        self.admission.acknowledge_update_reservation(self.operation)
        def request(*args, **kwargs):
            self.transfer()
            return {'operation_id': self.operation, 'generation': True, 'controller_pid': self.lease.lease.pid}
        with patch.object(runtime_admission, 'require_runtime_admission', return_value=self.admission), patch(
                'lifecycle.control.rpc.request', side_effect=request) as invoke:
            with self.assertRaises(ControlError):self.start()
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(state.inspect_state(self.root).generation, 2)
        self.assertEqual(update_controller.read_update_journal(self.path)['phase'], 'preflight')
        self.lease.require_current()


    def test_known_unadopted_claim_can_abort_without_losing_original_runtime_generation(self):
        claimed = state.inspect_state(self.root)
        self.admission.note_update_claim(self.operation)
        self.assertFalse(self.admission.admitted())
        self.path.unlink()  # The producer did not publish its original journal.
        result = self.admission.abort_unjournaled_update_claim(claimed)
        self.assertIsNone(result.state.active_operation)
        self.assertEqual(self.admission.generation, 1);self.assertEqual(self.context.generation, 0)
        self.assertTrue(self.admission.admitted())
        self.assertFalse((state.lifecycle_paths(self.root).lifecycle_dir/'update-history').exists())

    def test_unwitnessed_or_adopted_claim_never_uses_abort_as_admission_shortcut(self):
        claimed = state.inspect_state(self.root)
        with self.assertRaises(runtime_admission.RuntimeAdmissionError):
            self.admission.abort_unjournaled_update_claim(claimed)
        self.admission.note_update_claim(self.operation)
        from lifecycle.control.rpc import ControlError
        original = self.path.read_bytes()
        for generation in (1, 99):
            payload = json.loads(original);payload['lifecycle_generation'] = generation
            self.path.write_text(json.dumps(payload))
            with self.subTest(generation=generation), self.assertRaises(ControlError):
                self.admission.abort_unjournaled_update_claim(claimed)
            self.assertEqual(state.inspect_state(self.root), claimed)
            self.assertEqual(self.admission.generation, 0)

    def test_failed_receipt_retries_only_known_in_memory_completion_without_repeating_canonical_abort(self):
        self.admission.note_update_claim(self.operation);claimed = state.inspect_state(self.root)
        self.path.unlink()
        complete = state.complete_operation_with_credential
        with patch.object(state, 'complete_operation_with_credential', wraps=complete) as calls:
            with patch.object(self.admission, '_receipt', side_effect=OSError('Injected receipt failure')):
                with self.assertRaises(OSError):self.admission.abort_unjournaled_update_claim(claimed)
            self.assertIsNone(state.inspect_state(self.root).active_operation)
            fresh = runtime_admission.RuntimeAdmission(self.context, self.installation, '1'*32, self.execution)
            self.assertFalse(fresh.admitted())
            self.assertTrue(self.admission.admitted())
            self.assertEqual(calls.call_count, 1)
        self.assertEqual(self.admission.generation, 1)
        self.assertIsNone(self.admission._completed_update_claim)

    def test_known_abort_cannot_skip_a_subsequent_unrelated_canonical_generation(self):
        self.admission.note_update_claim(self.operation);claimed = state.inspect_state(self.root)
        self.path.unlink()
        with patch.object(self.admission, '_receipt', side_effect=OSError('Injected receipt failure')):
            with self.assertRaises(OSError):self.admission.abort_unjournaled_update_claim(claimed)
        next_claim = state.claim_operation_with_credential(self.root,
            operation_type='management', owner_kind='management_controller')
        self.assertFalse(self.admission.admitted())
        self.assertEqual(self.admission.generation, 1)
        self.assertEqual(state.inspect_state(self.root), next_claim.state)

    def test_successfully_published_journal_consumes_only_its_matching_original_claim_witness(self):
        self.admission.note_update_claim(self.operation)
        self.assertEqual(self.admission._pending_update_claim.parent, self.parent)
        self.admission.acknowledge_update_reservation(self.operation)
        self.assertIsNone(self.admission._pending_update_claim)
        self.assertTrue(self.admission.admitted())
