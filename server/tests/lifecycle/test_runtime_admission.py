import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bootstrap, management_journal as contract, management_store, runtime_admission, state
from tests.lifecycle.test_management_journal import _journal_payload


class RuntimeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'server').mkdir()
        for name in ('start.sh', 'update.sh'):
            (self.root/name).write_text('#!/bin/sh\n')
        paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(paths)
        state._write_state_unlocked(paths, state.LifecycleState(2, 0, None))
        self.payload = _journal_payload()
        self.context = bootstrap.evaluate_startup(self.root, {})
        self.execution = contract.parse_execution_tuple(self.payload['previous'])
        self.admission = runtime_admission.RuntimeAdmission(self.context, self.payload['installation_id'], 'f'*32, self.execution)
        self.readiness = bootstrap.StartupReadiness(self.context)
        result = asyncio.run(bootstrap.run_mandatory_recovery(self.context, ()))
        self.readiness.record_recovery(result)
        self.readiness.mark_workers_started()
        self.readiness.mark_general_ready()
        self.lease = None
        self.addCleanup(self.release)

    def release(self):
        if self.lease:
            self.lease.release()

    def claim(self, mode='save_only'):
        payload = {**self.payload, 'operation_id': str(uuid.uuid4()), 'activation_mode': mode}
        if mode == 'os_only':
            payload.update(action='provision_control', candidate=payload['previous'])
        claim = state.claim_operation_with_credential(self.root, operation_type='management', owner_kind='management_controller', operation_id=payload['operation_id'])
        payload['generation'] = claim.state.generation
        self.journal = contract.parse_management_journal(payload)
        self.lease = state.acquire_controller_lease(self.root, operation_id=self.journal.operation_id, generation=self.journal.generation, expected_phase='reserved', owner_capability=claim.owner_capability)
        management_store.create_management_journal(self.root, self.journal, lease=self.lease)
        return self.journal

    def finish(self):
        prepared = replace(self.journal, stage=contract.ManagementStage.PREPARED)
        management_store.advance_management_journal(self.root, self.journal, prepared, lease=self.lease)
        stage = contract.ManagementStage.SAVED if prepared.activation_mode is contract.ActivationMode.SAVE_ONLY else contract.ManagementStage.OS_COMPLETED
        terminal = replace(prepared, stage=stage)
        management_store.advance_management_journal(self.root, prepared, terminal, lease=self.lease)
        state.complete_operation_with_credential(self.root, operation_id=terminal.operation_id, generation=terminal.generation, expected_phase='reserved')

    def test_normal_settlement_requires_recovery_workers_and_exact_idle_generation(self):
        with self.assertRaises(runtime_admission.RuntimeAdmissionError):
            self.admission.settle(bootstrap.StartupReadiness(self.context))
        self.admission.settle(self.readiness)
        self.assertTrue(self.admission.admitted())
        self.assertEqual(self.context.generation, 0)

    def test_save_only_then_os_only_keep_same_runtime_without_rewriting_startup_generation(self):
        self.admission.settle(self.readiness)
        for mode in ('save_only', 'os_only'):
            journal = self.claim(mode)
            self.assertFalse(self.admission.admitted())
            acknowledgement = self.admission.acknowledge_continuation(journal.operation_id, controller_pid=os.getpid())
            self.assertTrue(acknowledgement['acknowledged'])
            self.assertTrue(self.admission.admitted())
            self.finish()
            self.assertTrue(self.admission.admitted())
            self.lease.release()
        self.assertEqual(self.admission.generation, 2)
        self.assertEqual(self.context.generation, 0)

    def test_skipped_completed_claim_cannot_be_inferred_from_idle_generation(self):
        self.admission.settle(self.readiness)
        self.claim()
        self.finish()
        self.assertFalse(self.admission.admitted())

    def test_cutover_foreign_invocation_or_controller_cannot_get_continuation_ack(self):
        self.admission.settle(self.readiness)
        journal = self.claim('cutover')
        with self.assertRaises(runtime_admission.RuntimeAdmissionError):
            self.admission.acknowledge_continuation(journal.operation_id, controller_pid=os.getpid())
        self.assertFalse(self.admission.admitted())

    def test_wrong_controller_pid_is_not_accepted_even_with_valid_journal(self):
        self.admission.settle(self.readiness)
        journal = self.claim()
        with self.assertRaises(runtime_admission.RuntimeAdmissionError):
            self.admission.acknowledge_continuation(journal.operation_id, controller_pid=os.getpid()+1)

    def test_forged_disk_receipt_cannot_settle_a_new_process_local_identity(self):
        self.admission.settle(self.readiness)
        path = state.lifecycle_paths(self.root).lifecycle_dir/'settlement'/f'{os.getpid()}.{self.admission.start}.json'
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text())
        payload['settled_generation'] = 999
        path.write_text(json.dumps(payload))
        other = runtime_admission.RuntimeAdmission(self.context, self.payload['installation_id'], 'f'*32, self.execution)
        self.assertFalse(other.admitted())
        with patch.object(runtime_admission.os, 'getpid', return_value=os.getpid()+1):
            self.assertFalse(self.admission.admitted())

    def test_failed_operation_revokes_preserving_continuation(self):
        self.admission.settle(self.readiness)
        journal = self.claim()
        self.admission.acknowledge_continuation(journal.operation_id, controller_pid=os.getpid())
        state.transition_operation_with_credential(self.root, operation_id=journal.operation_id, generation=journal.generation, expected_phase='reserved', next_phase='recovery_required')
        failed = replace(journal, stage=contract.ManagementStage.RECOVERY_REQUIRED)
        management_store.advance_management_journal(self.root, journal, failed, lease=self.lease)
        self.assertFalse(self.admission.admitted())


if __name__ == '__main__':
    unittest.main()
