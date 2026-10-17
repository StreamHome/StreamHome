"""Fail-closed inventory and accepted reservation cancellation in disposable roots."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import threading
import uuid
import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import config
import db as database
from config import settings
from models import StorageReservation
from services.storage_capacity import StorageCapacity
from services.storage_inventory import scan_managed
from services.storage_common import StorageError
from services.storage_policy_service import StoragePolicyService


class InventoryReadFailureTests(unittest.TestCase):
    def test_unreadable_root_is_unknown_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'owned').write_bytes(b'owned bytes')
            original = os.scandir
            def scandir(path):
                if Path(path) == root:
                    raise PermissionError('synthetic root access failure')
                return original(path)
            with patch('services.storage_inventory.managed_roots', return_value=[('media', root)]), \
                 patch('services.storage_inventory.os.scandir', scandir):
                measured = scan_managed()
            self.assertFalse(measured['complete'])
            self.assertIsNone(measured['written_bytes'])
            self.assertEqual(measured['error_code'], 'storage_inventory_unreadable')

    def test_unreadable_nested_directory_keeps_partial_observation_unavailable_for_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hidden = root / 'nested'
            hidden.mkdir()
            (root / 'visible').write_bytes(b'visible')
            (hidden / 'owned').write_bytes(b'hidden bytes')
            original = os.scandir
            def scandir(path):
                if Path(path) == hidden:
                    raise PermissionError('synthetic nested access failure')
                return original(path)
            with patch('services.storage_inventory.managed_roots', return_value=[('media', root)]), \
                 patch('services.storage_inventory.os.scandir', scandir):
                measured = scan_managed()
            self.assertFalse(measured['complete'])
            self.assertIsNone(measured['written_bytes'])
            self.assertEqual(measured['observed_bytes'], len(b'visible'))
            self.assertEqual(measured['error_code'], 'storage_inventory_unreadable')

    def test_inaccessible_root_metadata_is_unknown_but_a_missing_optional_root_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Path.lstat
            def lstat(path, *args, **kwargs):
                if path == root:
                    raise PermissionError('synthetic parent access failure')
                return original(path, *args, **kwargs)
            with patch('services.storage_inventory.managed_roots', return_value=[('media', root)]), \
                 patch.object(Path, 'lstat', lstat):
                measured = scan_managed()
            self.assertIsNone(measured['written_bytes'])
            self.assertEqual(measured['error_code'], 'storage_inventory_unreadable')
            with patch('services.storage_inventory.managed_roots', return_value=[('media', root / 'absent')]):
                self.assertEqual(scan_managed()['written_bytes'], 0)


class ReservationCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + str(self.root / 'database.db'))
        self.patches = [patch.object(database, 'engine', self.engine),
            patch.object(settings, 'STORAGE_POLICY', None),
            patch('services.storage_inventory.managed_roots', return_value=[('media', self.media)])]
        for item in self.patches:
            item.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.capacity = StorageCapacity()

    async def asyncTearDown(self):
        await self.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    async def assert_settled_claim(self):
        self.assertEqual(await self.capacity.remaining(), 0)
        async with AsyncSession(self.engine) as session:
            rows = (await session.exec(select(StorageReservation))).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, 'released')

    async def test_direct_admission_cancel_after_native_commit_releases_exact_accepted_claim(self):
        accepted = asyncio.Event()
        close_allowed = asyncio.Event()
        original = AsyncSession.commit
        async def commit(session):
            await original(session)
            accepted.set()
            await close_allowed.wait()
        with patch.object(AsyncSession, 'commit', commit):
            task = asyncio.create_task(self.capacity.reserve('cancelled.writer', 4096, self.media))
            await accepted.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            close_allowed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.assert_settled_claim()

    async def test_allocation_cancel_after_accepted_admission_never_enters_physical_writer(self):
        accepted = asyncio.Event()
        return_allowed = asyncio.Event()
        entered = False
        original = self.capacity.reserve
        async def reserve(*args, **kwargs):
            identifier = await original(*args, **kwargs)
            accepted.set()
            await return_allowed.wait()
            return identifier
        async def writer():
            nonlocal entered
            async with self.capacity.allocation('cancelled.writer', 4096, self.media):
                entered = True
        with patch.object(self.capacity, 'reserve', reserve):
            task = asyncio.create_task(writer())
            await accepted.wait()
            task.cancel()
            return_allowed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(entered)
        await self.assert_settled_claim()

    async def test_policy_save_rejects_budget_below_already_admitted_unwritten_work(self):
        draft = dict(offload_enabled=False, local_budget_bytes=50, cloud_budget_bytes=None,
            prefetch_next_episode=False, hevc_compression_mode='auto')
        service = StoragePolicyService()
        review = await service.preview('human', draft, 0)
        reservation = await self.capacity.reserve('accepted.writer', 100, self.media)
        with patch('services.storage_capacity.storage_capacity', self.capacity):
            with self.assertRaises(StorageError) as error:
                await service.save('human', review['review_id'], draft, 0, str(uuid.uuid4()))
        self.assertEqual(error.exception.code, 'storage_review_protected_data_changed')
        self.assertIsNone(settings.STORAGE_POLICY)
        await self.capacity.release(reservation)

    async def test_policy_acceptance_serializes_new_admission_against_the_new_budget(self):
        draft = dict(offload_enabled=False, local_budget_bytes=64, cloud_budget_bytes=None,
            prefetch_next_episode=False, hevc_compression_mode='auto')
        service = StoragePolicyService()
        review = await service.preview('human', draft, 0)
        persisting, continue_save = threading.Event(), threading.Event()
        original = settings._save_to_json_locked
        def persist():
            persisting.set()
            if not continue_save.wait(timeout=5):
                raise RuntimeError('controlled save barrier timed out')
            return original()
        with patch('services.storage_capacity.storage_capacity', self.capacity), \
             patch.object(config, 'config_dir', str(self.root)), \
             patch.object(settings, 'BASE_DIR', str(self.root)), \
             patch.object(settings, '_save_to_json_locked', persist), \
             patch.object(StoragePolicyService, '_wake', lambda _: None):
            saving = asyncio.create_task(service.save('human', review['review_id'], draft, 0, str(uuid.uuid4())))
            try:
                started = await asyncio.to_thread(persisting.wait, 5)
                self.assertTrue(started)
                admission = asyncio.create_task(self.capacity.reserve('late.writer', 80, self.media))
                await asyncio.sleep(.02)
                self.assertFalse(admission.done(), 'new admission must wait for exact policy acceptance')
            finally:
                continue_save.set()
            await saving
            with self.assertRaises(StorageError) as error:
                await admission
            self.assertEqual(error.exception.code, 'storage_local_budget_exceeded')
        self.assertEqual(await self.capacity.remaining(), 0)


if __name__ == '__main__':
    unittest.main()
