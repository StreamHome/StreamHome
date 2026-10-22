import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from lifecycle import bootstrap, management_journal as contract, management_store, state
from tests.lifecycle import test_management_recovery_store as store_fixture


class ManagementCompletionRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        import application
        from lifecycle.storage_worker import storage_worker
        self.application = application
        self.fixture = store_fixture.ManagementRecoveryStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.journal = self.fixture.journal
        for phase, stage in (('reserved', contract.ManagementStage.PREPARED), ('exclusive', contract.ManagementStage.EXCLUSIVE),
                             ('verifying', contract.ManagementStage.VERIFYING), ('verifying', contract.ManagementStage.ACCEPTED),
                             ('verifying', contract.ManagementStage.SUCCEEDED)):
            if phase != contract.journal_phase(self.journal).value:
                state.transition_operation_with_credential(self.root, operation_id=self.journal.operation_id, generation=1,
                    expected_phase=contract.journal_phase(self.journal), next_phase=phase)
            acceptance = self.journal.acceptance
            if stage is contract.ManagementStage.ACCEPTED:
                acceptance = contract.AcceptanceEvidence(contract.execution_tuple_sha256(self.journal.candidate), 'a' * 64)
            updated = replace(self.journal, stage=stage, acceptance=acceptance)
            management_store.advance_management_journal(self.root, self.journal, updated, lease=self.fixture.lease)
            self.journal = updated
        self.context = bootstrap.StartupContext(bootstrap.StartupMode.MANAGEMENT_VERIFY, self.root, 2, 1,
            self.journal.operation_id, 'management', 'verifying', 'management_controller', True, frozenset({'db_write'}))
        self.readiness = bootstrap.StartupReadiness(self.context)
        self.enterContext(patch.object(application, 'STARTUP_CONTEXT', self.context))
        self.enterContext(patch.object(application, 'STARTUP_READINESS', self.readiness))
        self.enterContext(patch.object(application, 'revalidate_startup_context'))
        self.enterContext(patch.object(application, '_normal_recovery_steps', return_value=()))
        self.enterContext(patch('routes.lifecycle.management_commit_acknowledged', return_value=True))
        self.start = self.enterContext(patch.object(application, '_start_runtime_workers', AsyncMock()))
        self.stop = self.enterContext(patch.object(application, '_stop_runtime_workers', AsyncMock()))
        self.settle = self.enterContext(patch.object(application, '_settle_managed_runtime', AsyncMock()))
        self.enterContext(patch.object(application, '_optional_playback_baselines', AsyncMock()))
        self.enterContext(patch.object(storage_worker.measurements, 'start', AsyncMock()))
        self.coordinator = Mock()
        self.coordinator.drain = AsyncMock(return_value=SimpleNamespace(complete=True))
        self.enterContext(patch.object(application, 'get_runtime_coordinator', return_value=self.coordinator))
        self.enterContext(patch.object(application.state, 'MAINTENANCE_MODE', True))
        self.enterContext(patch.object(application.state, 'MAINTENANCE_REASON', 'guarded'))

    def complete(self):
        state.complete_operation_with_credential(self.root, operation_id=self.journal.operation_id, generation=1, expected_phase='verifying')

    async def promote(self):
        runtime = self.application._WorkerRuntime()
        await self.application._promote_committed_management(runtime, None)
        for task in runtime.tasks:
            await asyncio.gather(task, return_exceptions=True)

    async def test_completion_between_snapshot_and_next_check_settles_instead_of_stopping_workers(self):
        original = state.inspect_state
        completed = False
        def inspect(root):
            nonlocal completed
            snapshot = original(root)
            if not completed:
                completed = True
                self.complete()
            return snapshot
        with patch.object(state, 'inspect_state', side_effect=inspect):
            await self.promote()
        self.assertTrue(completed)
        self.stop.assert_not_awaited()
        self.settle.assert_awaited_once()
        self.assertTrue(self.readiness.projection()['workersStarted'])
        self.assertTrue(self.readiness.projection()['generalReady'])
        self.assertFalse(self.application.state.MAINTENANCE_MODE)

    async def test_idle_same_generation_still_requires_exact_terminal_journal(self):
        previous = replace(self.journal, stage=contract.ManagementStage.ACCEPTED)
        _, path = management_store._journal_path(self.root, previous.operation_id)
        state._atomic_write_json(path, contract.management_journal_payload(previous))
        self.complete()
        await self.promote()
        self.stop.assert_awaited_once()
        self.settle.assert_not_awaited()
        self.assertEqual(self.readiness.projection()['errorCode'], 'management_lifecycle_unresolved')

    async def test_new_generation_never_inherits_the_previous_completion(self):
        self.complete()
        self.fixture.lease.release()
        state.claim_operation_with_credential(self.root, operation_type='restore', owner_kind='restore_controller')
        await self.promote()
        self.stop.assert_awaited_once()
        self.settle.assert_not_awaited()
        self.assertFalse(self.readiness.projection()['generalReady'])


if __name__ == '__main__':
    unittest.main()
