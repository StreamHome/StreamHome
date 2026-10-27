"""Use actual durable claims and disposable files to exercise growing ingestion."""
import asyncio
import shutil
import unittest
from unittest.mock import patch

from lifecycle.ingestion_capacity import IngestionCapacity
from services.storage_capacity import StorageCapacityError
from tests.storage import test_capacity_materialization as fixture


class IngestionCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.MaterializedCapacityTests.asyncSetUp(self)
        self.enterContext(patch('lifecycle.ingestion_capacity.storage_capacity', self.capacity))
        self.enterContext(patch('lifecycle.ingestion_capacity.MIB', 1))

    async def asyncTearDown(self):
        await fixture.MaterializedCapacityTests.asyncTearDown(self)

    async def test_underestimate_grows_one_claim_and_seals_to_actual_bytes(self):
        from config import settings
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 400}
        controller = IngestionCapacity('test-task')
        path = self.media / 'workspace'
        await controller.admit('temp', path, 100)
        path.mkdir(); (path / 'output').write_bytes(b'a' * 60)
        original = controller.workspaces['temp'].reservation
        await controller.check()
        self.assertEqual(controller.workspaces['temp'].peak, 228)
        self.assertEqual(controller.workspaces['temp'].reservation, original)
        await controller.seal('temp', headroom=1)
        self.assertEqual(await self.capacity.remaining(), 1)
        await controller.close()
        self.assertEqual(await self.capacity.remaining(), 0)

    async def test_preview_is_independent_and_denied_growth_does_not_release_it(self):
        from config import settings
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 200}
        controller = IngestionCapacity('test-task')
        await controller.admit('temp', self.media / 'workspace', 100)
        preview = self.media / 'preview'
        await controller.admit('preview-1', preview, 100)
        preview.mkdir(); (preview / 'segment').write_bytes(b'a' * 60)
        with self.assertRaises(StorageCapacityError) as error:
            await controller.check()
        self.assertEqual(error.exception.code, 'storage_local_budget_exceeded')
        self.assertEqual(await self.capacity.remaining(), 140)
        await controller.close()
        self.assertEqual((preview / 'segment').stat().st_size, 60)

    async def test_exceeded_ceiling_and_external_disk_pressure_fail_closed(self):
        controller = IngestionCapacity('test-task')
        path = self.media / 'workspace'
        await controller.admit('temp', path, 100)
        path.mkdir(); output = path / 'output'; output.write_bytes(b'a' * 101)
        with self.assertRaises(StorageCapacityError) as error:
            await controller.check()
        self.assertEqual(error.exception.code, 'storage_reservation_exceeded')
        output.write_bytes(b'a')
        with patch('lifecycle.ingestion_capacity.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 99, 1)):
            with self.assertRaises(StorageCapacityError) as error:
                await controller.check()
        self.assertEqual(error.exception.code, 'storage_disk_space_insufficient')
        await controller.close()

    async def test_cancelled_admission_keeps_owned_claim_until_cleanup(self):
        controller = IngestionCapacity('test-task')
        accepted, finish = asyncio.Event(), asyncio.Event()
        original = self.capacity.reserve
        async def delayed(*args, **kwargs):
            identifier = await original(*args, **kwargs)
            accepted.set(); await finish.wait()
            return identifier
        with patch.object(self.capacity, 'reserve', side_effect=delayed):
            task = asyncio.create_task(controller.admit('temp', self.media / 'workspace', 100))
            await accepted.wait(); task.cancel(); finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await self.capacity.remaining(), 100)
        await controller.close()
        self.assertEqual(await self.capacity.remaining(), 0)

    async def test_cancellation_after_growth_commit_keeps_controller_and_durable_peak_aligned(self):
        from config import settings
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 400}
        controller = IngestionCapacity('test-task')
        path = self.media / 'workspace'
        await controller.admit('temp', path, 100)
        path.mkdir(); (path / 'output').write_bytes(b'a' * 60)
        accepted, finish = asyncio.Event(), asyncio.Event()
        original = self.capacity.resize
        async def delayed(*args, **kwargs):
            await original(*args, **kwargs)
            accepted.set(); await finish.wait()
        with patch.object(self.capacity, 'resize', side_effect=delayed):
            task = asyncio.create_task(controller.check())
            await accepted.wait(); task.cancel(); finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(controller.workspaces['temp'].peak, 228)
        self.assertEqual(await self.capacity.remaining(), 168)
        await controller.close()
