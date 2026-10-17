"""One-sample owned materialization prevents reusable workspace oversubscription."""
from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

import db as database
from config import settings
from models import StorageReservation
from services.storage_capacity import StorageCapacity, StorageCapacityError, _remaining_from_sample
from services.storage_inventory import scan_managed


class MaterializedCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + str(self.root / 'database.db'))
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.patches = [patch.object(database, 'engine', self.engine),
            patch.object(settings, 'STORAGE_POLICY', {'revision': 1, 'local_budget_bytes': 100,
                'offload_enabled': False, 'cloud_budget_bytes': None}),
            patch('services.storage_inventory.managed_roots', return_value=[('media', self.media)])]
        for item in self.patches:
            item.start()
        self.capacity = StorageCapacity()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_shrink_restores_full_peak_without_progress_notification(self):
        path = self.media / 'first'
        identifier = await self.capacity.reserve('first', 100, path)
        path.write_bytes(b'a' * 100)
        await self.capacity.progress(identifier, 100)
        path.unlink()
        with self.assertRaises(StorageCapacityError):
            await self.capacity.reserve('second', 100, self.media / 'second')
        self.assertEqual(await self.capacity.remaining(), 100)
        path.write_bytes(b'a' * 100)
        self.assertEqual(await self.capacity.remaining(), 0)

    async def test_partial_materialization_is_not_counted_twice_and_rename_is_stable(self):
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 150}
        workspace = self.media / 'workspace'
        first = await self.capacity.reserve('first', 100, workspace)
        workspace.mkdir()
        partial = workspace / '.partial'
        partial.write_bytes(b'a' * 30)
        await self.capacity.progress(first, 30)
        self.assertEqual(await self.capacity.remaining(), 70)
        partial.rename(workspace / 'complete')
        second = await self.capacity.reserve('second', 50, self.media / 'second')
        self.assertEqual(await self.capacity.remaining(), 120)
        with self.assertRaises(StorageCapacityError):
            await self.capacity.reserve('third', 1, self.media / 'third')
        await self.capacity.release(first)
        await self.capacity.release(second)

    async def test_existing_baseline_shrink_and_replacement_keep_future_peak(self):
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 200}
        workspace = self.media / 'workspace'
        workspace.mkdir()
        original = workspace / 'original'
        original.write_bytes(b'a' * 100)
        await self.capacity.reserve('replace', 100, workspace)
        (workspace / 'candidate').write_bytes(b'b' * 30)
        self.assertEqual(await self.capacity.remaining(), 70)
        original.unlink()
        self.assertEqual(await self.capacity.remaining(), 100)

    async def test_overlapping_roots_and_hardlink_aliases_retain_full_peaks(self):
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 400}
        parent = self.media / 'parent'
        await self.capacity.reserve('parent', 100, parent)
        await self.capacity.reserve('child', 100, parent / 'file')
        parent.mkdir()
        (parent / 'file').write_bytes(b'a' * 100)
        self.assertEqual(await self.capacity.remaining(), 200)
        alias = self.media / 'alias'
        await self.capacity.reserve('alias', 100, alias)
        os.link(parent / 'file', alias)
        self.assertEqual(scan_managed()['written_bytes'], 100)
        self.assertEqual(await self.capacity.remaining(), 300)

    async def test_shared_probe_path_keeps_full_peak(self):
        identifier = await self.capacity.reserve('proof', 100, self.media, track_materialized=False)
        (self.media / 'output').write_bytes(b'a' * 50)
        await self.capacity.progress(identifier, 50)
        self.assertEqual(await self.capacity.remaining(), 100)

    async def test_growth_after_scan_cannot_spend_the_same_peak(self):
        path = self.media / 'first'
        identifier = await self.capacity.reserve('first', 100, path)
        path.write_bytes(b'a' * 30)
        await self.capacity.progress(identifier, 30)
        original = scan_managed
        def interleaved(**kwargs):
            result = original(**kwargs)
            path.write_bytes(b'a' * 100)
            return result
        with patch('services.storage_inventory.scan_managed', side_effect=interleaved):
            with self.assertRaises(StorageCapacityError):
                await self.capacity.reserve('second', 1, self.media / 'second')

    async def test_alias_growth_uses_the_global_first_observation(self):
        path = self.media / 'first'
        await self.capacity.reserve('first', 100, path)
        path.write_bytes(b'a' * 15)
        alias = self.media / 'alias'
        os.link(path, alias)
        real_stat = Path.stat
        visited = set()
        def changing_stat(candidate, *args, **kwargs):
            result = real_stat(candidate, *args, **kwargs)
            if candidate in {path, alias} and kwargs.get('follow_symlinks', True):
                visited.add(candidate)
                if len(visited) == 1:
                    path.write_bytes(b'a' * 100)
            return result
        with patch.object(Path, 'stat', changing_stat):
            sample = scan_managed(include_entries=True)
        self.assertEqual(sample['written_bytes'], 15)
        self.assertEqual({entry[3] for entry in sample['_entries']}, {15})

    def test_device_scopes_and_cloud_progress_remain_distinct(self):
        rows = [StorageReservation(id='a', owner='a', scope='local', filesystem='a', path='/a', peak_bytes=100),
                StorageReservation(id='b', owner='b', scope='local', filesystem='b', path='/b', peak_bytes=100),
                StorageReservation(id='c', owner='c', scope='cloud', filesystem='drive', path='drive:', peak_bytes=100, written_bytes=30)]
        sample = {'_entries': [('/a/file', 1, 1, 30), ('/b/file', 2, 1, 40)]}
        self.assertEqual(_remaining_from_sample(rows, sample), 130)
        self.assertEqual(_remaining_from_sample(rows, sample, filesystem='a'), 70)
        self.assertEqual(_remaining_from_sample(rows, sample, scope='cloud'), 70)


if __name__ == '__main__':
    unittest.main()
