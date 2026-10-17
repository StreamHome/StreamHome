"""Automatic measurement contracts in disposable installations only."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import config
from config import settings
import db as database
from lifecycle.coordination import EffectClass, RuntimeCoordinator, use_runtime_coordinator_for_testing
from lifecycle.storage_measurements import StorageMeasurements
from lifecycle.storage_worker import StorageWorker
from models import StorageMeasurement, StorageOperation, StorageReservation
from services.rclone import RcloneResult, rclone_service
from services.storage_capacity import StorageCapacity
from services.storage_common import StorageError
from services.storage_inventory import target_generation
from services.storage_policy_service import StoragePolicyService


class AutomaticMeasurementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = self.root / 'server'
        self.media = self.server / 'media'
        self.media.mkdir(parents=True)
        (self.server / 'temp').mkdir()
        self.configuration = self.server / 'rclone' / 'rclone.conf'
        self.configuration.parent.mkdir()
        self.configuration.write_text('[unit]\ntype = local\n')
        self.engine = create_async_engine('sqlite+aiosqlite:///' + str(self.server / 'database.db'))
        self.capacity = StorageCapacity()
        self.patches = [patch.object(database, 'engine', self.engine),
            patch.object(config, 'config_dir', str(self.server)),
            patch.object(settings, 'BASE_DIR', str(self.root)),
            patch.object(settings, 'MEDIA_DIR', str(self.media)),
            patch.object(settings, 'TEMP_DIR', str(self.server / 'temp')),
            patch.object(settings, 'db_path', str(self.server / 'database.db')),
            patch.object(settings, 'STORAGE_POLICY', None),
            patch.object(settings, 'STORAGE_ENGINE', 'LOCAL'),
            patch.object(settings, 'RCLONE_REMOTE_PATH', 'unit:media'),
            patch.object(rclone_service, 'config_path', self.configuration),
            patch('lifecycle.storage_worker.storage_capacity', self.capacity),
            patch('services.storage_capacity.storage_capacity', self.capacity)]
        for item in self.patches:
            item.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.worker = StorageWorker()
        self.service = StoragePolicyService()
        self.patches.append(patch('lifecycle.storage_worker.storage_worker', self.worker))
        self.patches[-1].start()

    async def asyncTearDown(self):
        await self.worker.stop()
        await self.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def sample(self, name):
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            row = await session.get(StorageMeasurement, name)
            return (json.loads(row.payload), row.measured_at) if row else ({}, None)

    async def wait_until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.005)

    async def provider(self, command, *args, **kwargs):
        return RcloneResult(0, stdout=json.dumps({'bytes': 100} if command == 'size' else {'total': 10000, 'free': 9900}))

    async def test_startup_measures_both_with_offload_disabled_and_no_activity(self):
        with patch.object(self.worker, 'tick', AsyncMock()), patch.object(rclone_service, 'run', side_effect=self.provider):
            await self.worker.start()
            await self.wait_until(lambda: all(state.last_success_at for state in self.worker.measurements.states.values()))
        status = await self.service.status()
        self.assertIsInstance(status['local']['written_bytes'], int)
        self.assertEqual(status['cloud']['used_bytes'], 100)
        self.assertEqual(status['offload']['state'], 'paused')
        self.assertEqual(status['local']['measured_at'], status['local']['refresh']['last_success_at'])
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            self.assertEqual((await session.exec(select(StorageOperation))).all(), [])

    async def test_unconfigured_cloud_does_no_provider_work_and_local_continues(self):
        self.configuration.unlink()
        with patch.object(self.worker, 'tick', AsyncMock()), patch.object(rclone_service, 'run', AsyncMock()) as provider:
            await self.worker.start()
            await self.wait_until(lambda: self.worker.measurements.states['local'].last_success_at is not None)
            self.assertEqual((await self.service.status())['cloud']['refresh']['state'], 'unconfigured')
            provider.assert_not_awaited()
        with self.assertRaises(StorageError):
            await self.worker.measure_cloud()

    async def test_size_scan_never_adopts_or_hashes_assets(self):
        (self.media / 'sample.mp4').write_bytes(b'counted-without-reading')
        with patch('services.storage_inventory._hash_file', side_effect=AssertionError('must not hash')), \
             patch('services.storage_inventory.storage_inventory.reconcile_catalog', AsyncMock(side_effect=AssertionError('must not adopt'))):
            await self.worker.measure_local()
        self.assertGreaterEqual((await self.sample('local'))[0]['written_bytes'], 23)

    async def test_single_flight_coalesces_concurrent_background_requests(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def scan():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
        with patch.object(self.worker, '_measure_local', scan):
            first = asyncio.create_task(self.worker.measurements.measure('local'))
            await entered.wait()
            second = asyncio.create_task(self.worker.measurements.measure('local'))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(calls, 1)

    async def test_fresh_authority_request_cannot_join_pre_write_observation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        with patch.object(self.worker, '_measure_cloud', AsyncMock(side_effect=lambda: None)) as measure:
            async def delayed():
                entered.set()
                await release.wait()
            measure.side_effect = delayed
            first = asyncio.create_task(self.worker.measurements.measure('cloud'))
            await entered.wait()
            second = asyncio.create_task(self.worker.measure_cloud())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(measure.await_count, 2)

    async def test_cloud_failure_retains_last_good_values_and_timestamp(self):
        with patch.object(rclone_service, 'run', side_effect=self.provider):
            await self.worker.measure_cloud()
        old, stamp = await self.sample('cloud')
        with patch.object(rclone_service, 'run', AsyncMock(return_value=RcloneResult(124, error_code='rclone_timeout'))):
            with self.assertRaises(StorageError):
                await self.worker.measure_cloud()
        current, current_stamp = await self.sample('cloud')
        self.assertEqual(current['used_bytes'], old['used_bytes'])
        self.assertEqual(current_stamp, stamp)
        self.assertFalse(current['capacity_valid'])
        self.assertEqual((await self.service.status())['cloud']['refresh']['error_code'], 'rclone_timeout')
        with self.assertRaises(StorageError) as error:
            await self.capacity.reserve('cloud.writer', 1, 'unit:media', scope='cloud')
        self.assertEqual(error.exception.code, 'storage_cloud_measurement_unknown')

    async def test_local_failure_retains_last_good_and_cloud_succeeds(self):
        await self.worker.measure_local()
        old = await self.sample('local')
        with patch.object(self.worker, '_measure_local', AsyncMock(side_effect=OSError('private path'))), \
             patch.object(rclone_service, 'run', side_effect=self.provider):
            result = await asyncio.gather(self.worker.measure_local(), self.worker.measure_cloud(), return_exceptions=True)
        self.assertIsInstance(result[0], OSError)
        self.assertEqual(await self.sample('local'), old)
        status = await self.service.status()
        self.assertEqual(status['local']['refresh']['state'], 'error')
        self.assertEqual(status['cloud']['refresh']['state'], 'idle')
        self.assertNotIn('private path', json.dumps(status))

    async def test_target_change_rejects_old_in_flight_result(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(command, *args, **kwargs):
            if command == 'size':
                entered.set()
                await release.wait()
            return await self.provider(command)
        with patch.object(rclone_service, 'run', side_effect=delayed):
            task = asyncio.create_task(self.worker.measure_cloud())
            await entered.wait()
            settings.RCLONE_REMOTE_PATH = 'unit:new-target'
            release.set()
            with self.assertRaises(StorageError) as error:
                await task
        self.assertEqual(error.exception.code, 'storage_target_changed')
        self.assertEqual(await self.sample('cloud'), ({}, None))
        self.assertIsNone((await self.service.status())['cloud']['measured_at'])

    async def test_failure_backoff_is_bounded_and_completion_based(self):
        coordinator = self.worker.measurements
        with patch.object(self.worker, '_measure_cloud', AsyncMock(side_effect=StorageError('drive_rate_limited'))), \
             patch('lifecycle.storage_measurements.random.uniform', return_value=0):
            for expected in [300, 600, 900, 900]:
                with self.assertRaises(StorageError):
                    await coordinator.measure('cloud')
                state = coordinator.states['cloud']
                self.assertAlmostEqual(state.due - state.completed, expected, places=3)
                self.assertGreaterEqual(state.next_attempt_at, state.last_attempt_at + expected)

    async def test_cached_status_does_not_scan_or_start_provider_work(self):
        with patch('services.storage_inventory.storage_inventory.local_measurement', AsyncMock(side_effect=AssertionError('scan'))), \
             patch.object(rclone_service, 'run', AsyncMock(side_effect=AssertionError('provider'))):
            status = await self.service.status()
        self.assertEqual(status['local']['refresh']['state'], 'idle')

    async def test_provider_wait_does_not_block_local_admission_or_cached_status(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(command, *args, **kwargs):
            if command == 'size':
                entered.set()
                await release.wait()
            return await self.provider(command)
        with patch.object(rclone_service, 'run', side_effect=delayed):
            task = asyncio.create_task(self.worker.measure_cloud())
            await entered.wait()
            try:
                reservation = await asyncio.wait_for(self.capacity.reserve('local.writer', 1, self.media), 2)
                self.assertIsNotNone(await asyncio.wait_for(self.service.status(), 2))
                await self.capacity.release(reservation)
            finally:
                release.set()
                await task

    async def test_delayed_cloud_snapshot_cannot_erase_progress_and_release(self):
        await self.worker._save_measurement('cloud', {'target_generation': target_generation(), 'used_bytes': 100, 'account_free_bytes': 9900})
        reservation = await self.capacity.reserve('cloud.writer', 50, 'unit:media', scope='cloud')
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(command, *args, **kwargs):
            if command == 'size':
                entered.set()
                await release.wait()
            return await self.provider(command)
        with patch.object(rclone_service, 'run', side_effect=delayed):
            measurement = asyncio.create_task(self.worker.measure_cloud())
            await entered.wait()
            async def finish_writer():
                await self.capacity.progress(reservation, 50)
                await self.capacity.release(reservation)
            writer = asyncio.create_task(finish_writer())
            await asyncio.sleep(.03)
            self.assertFalse(writer.done())
            release.set()
            await asyncio.gather(measurement, writer)
        measured, _ = await self.sample('cloud')
        self.assertEqual(measured['used_bytes'], 150)
        self.assertEqual(measured['account_free_bytes'], 9850)
        self.assertEqual(await self.capacity.remaining('cloud'), 0)
        settings.STORAGE_POLICY = {'cloud_budget_bytes': 150}
        with self.assertRaises(StorageError) as error:
            await self.capacity.reserve('cloud.next', 1, 'unit:media', scope='cloud')
        self.assertEqual(error.exception.code, 'storage_cloud_budget_exceeded')

    async def test_dashboard_sample_older_than_60s_cannot_admit_cloud_write(self):
        with patch.object(rclone_service, 'run', side_effect=self.provider):
            await self.worker.measure_cloud()
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            row = await session.get(StorageMeasurement, 'cloud')
            row.measured_at = time.time() - 61
            session.add(row)
            await session.commit()
        self.assertEqual((await self.service.status())['cloud']['used_bytes'], 100)
        with self.assertRaises(StorageError) as error:
            await self.capacity.reserve('cloud.writer', 1, 'unit:media', scope='cloud')
        self.assertEqual(error.exception.code, 'storage_cloud_measurement_unknown')

    async def test_stop_cancels_owned_tasks_and_clears_running_progress(self):
        entered = asyncio.Event()
        async def scan():
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.worker, '_measure_local', scan), patch.object(self.worker, 'tick', AsyncMock()):
            self.configuration.unlink()
            await self.worker.start()
            await entered.wait()
            tasks = list(self.worker.measurements.tasks)
            await self.worker.stop()
        self.assertTrue(all(task.done() for task in tasks))
        self.assertEqual(self.worker.measurements.tasks, [])
        self.assertNotEqual(self.worker.measurements.states['local'].state, 'running')

    async def test_owned_local_scan_remains_registered_until_thread_settles(self):
        entered, release = threading.Event(), threading.Event()
        from lifecycle.coordination import run_cancellation_safe_thread
        def scan():
            entered.set()
            release.wait(timeout=5)
        async def scan_async():
            await run_cancellation_safe_thread(scan)
        coordinator = RuntimeCoordinator()
        with use_runtime_coordinator_for_testing(coordinator), patch.object(self.worker, '_measure_local', scan_async):
            task = asyncio.create_task(self.worker.measure_local())
            await self.wait_until(entered.is_set)
            snapshot = coordinator.close_admission([EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1)
            task.cancel()
            try:
                self.assertFalse((await coordinator.drain(snapshot, timeout=.02)).complete)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue((await coordinator.drain(snapshot, timeout=1)).complete)

    async def test_burst_invalidations_debounce_without_overlapping_or_resetting_backoff(self):
        coordinator = self.worker.measurements
        coordinator.loop = asyncio.get_running_loop()
        state = coordinator.states['local']
        state.completed = time.monotonic()
        coordinator._schedule('local', failed=False)
        original = state.due
        for _ in range(50):
            coordinator.invalidate()
        await asyncio.sleep(0)
        self.assertTrue(state.dirty)
        self.assertEqual(len(coordinator.tasks), 0)
        with patch.object(self.worker, '_measure_local', AsyncMock()) as measure:
            loop = asyncio.create_task(coordinator._run('local'))
            await asyncio.sleep(.02)
            self.assertLess(state.due, original)
            self.assertAlmostEqual(state.due - state.completed, 10, places=2)
            measure.assert_not_awaited()
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
        state.failures = 2
        coordinator._schedule('local', failed=True)
        failed_due = state.due
        loop = asyncio.create_task(coordinator._run('local'))
        await asyncio.sleep(.02)
        self.assertEqual(state.due, failed_due)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    async def test_policy_acceptance_fences_cloud_admission_against_new_budget(self):
        await self.worker._save_measurement('cloud', {'target_generation': target_generation(), 'used_bytes': 100, 'account_free_bytes': 9900})
        draft = dict(offload_enabled=False, local_budget_bytes=None, cloud_budget_bytes=100,
            prefetch_next_episode=False, hevc_compression_mode='auto')
        review = await self.service.preview('human', draft, 0)
        entered, release = threading.Event(), threading.Event()
        original = settings._save_to_json_locked
        def persist():
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('synthetic save barrier')
            return original()
        with patch.object(settings, '_save_to_json_locked', persist):
            save = asyncio.create_task(self.service.save('human', review['review_id'], draft, 0, str(uuid.uuid4())))
            await self.wait_until(entered.is_set)
            reserve = asyncio.create_task(self.capacity.reserve('cloud.new', 1, 'unit:media', scope='cloud'))
            await asyncio.sleep(.03)
            self.assertFalse(reserve.done())
            release.set()
            await save
            with self.assertRaises(StorageError) as error:
                await reserve
        self.assertEqual(error.exception.code, 'storage_cloud_budget_exceeded')


if __name__ == '__main__':
    unittest.main()
