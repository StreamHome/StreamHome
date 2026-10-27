"""One-sample owned materialization prevents reusable workspace oversubscription."""
from __future__ import annotations

from pathlib import Path
import os
import asyncio
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as database
from config import settings
from models import StorageReservation
from services.storage_capacity import StorageCapacity, StorageCapacityError, _owned_entries, _remaining_from_sample
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

    async def test_resize_preserves_baseline_and_does_not_double_count_written_bytes(self):
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 200}
        path = self.media / 'workspace'
        path.mkdir()
        (path / 'existing').write_bytes(b'a' * 20)
        identifier = await self.capacity.reserve('writer', 100, path)
        (path / 'output').write_bytes(b'b' * 40)
        await self.capacity.progress(identifier, 40)
        await self.capacity.resize(identifier, 150)
        self.assertEqual(await self.capacity.remaining(), 110)
        async with AsyncSession(self.engine) as db:
            row = await db.get(StorageReservation, identifier)
            self.assertEqual((row.baseline_bytes, row.peak_bytes), (20, 150))
        await self.capacity.resize(identifier, 40)
        self.assertEqual(await self.capacity.remaining(), 0)
        with self.assertRaises(StorageCapacityError):
            await self.capacity.resize(identifier, 39)

    async def test_rejected_growth_retains_original_claim_against_concurrent_admission(self):
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 150}
        identifier = await self.capacity.reserve('writer', 100, self.media / 'first')
        await self.capacity.reserve('other', 50, self.media / 'second')
        with self.assertRaises(StorageCapacityError) as error:
            await self.capacity.resize(identifier, 101)
        self.assertEqual(error.exception.code, 'storage_local_budget_exceeded')
        self.assertEqual(await self.capacity.remaining(), 150)

    async def test_resize_keeps_physical_floor_and_missing_claim_fails_closed(self):
        identifier = await self.capacity.reserve('writer', 50, self.media / 'first')
        with patch('services.storage_capacity.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 20, 80)):
            with self.assertRaises(StorageCapacityError) as error:
                await self.capacity.resize(identifier, 60)
        self.assertEqual(error.exception.code, 'storage_disk_space_insufficient')
        self.assertEqual(await self.capacity.remaining(), 50)
        await self.capacity.release(identifier)
        with self.assertRaises(StorageCapacityError):
            await self.capacity.resize(identifier, 60)

    async def test_pressure_commit_cannot_accept_rejected_growth(self):
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'offload_enabled': True}
        identifier = await self.capacity.reserve('writer', 100, self.media / 'first')
        with patch('lifecycle.storage_worker.storage_worker.wake'):
            with self.assertRaises(StorageCapacityError):
                await self.capacity.resize(identifier, 101)
        self.assertEqual(await self.capacity.remaining(), 100)

    async def test_independent_authorities_cannot_admit_growth_and_a_new_writer_over_budget(self):
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 180}
        identifier = await self.capacity.reserve('first', 100, self.media / 'first')
        other = StorageCapacity()
        outcomes = await asyncio.gather(self.capacity.resize(identifier, 150),
            other.reserve('second', 50, self.media / 'second'), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, StorageCapacityError) for result in outcomes), 1)
        self.assertLessEqual(await self.capacity.remaining(), 180)

    def test_device_scopes_and_cloud_progress_remain_distinct(self):
        rows = [StorageReservation(id='a', owner='a', scope='local', filesystem='a', path='/a', peak_bytes=100),
                StorageReservation(id='b', owner='b', scope='local', filesystem='b', path='/b', peak_bytes=100),
                StorageReservation(id='c', owner='c', scope='cloud', filesystem='drive', path='drive:', peak_bytes=100, written_bytes=30)]
        sample = {'_entries': [('/a/file', 1, 1, 30), ('/b/file', 2, 1, 40)]}
        self.assertEqual(_remaining_from_sample(rows, sample), 130)
        self.assertEqual(_remaining_from_sample(rows, sample, filesystem='a'), 70)
        self.assertEqual(_remaining_from_sample(rows, sample, scope='cloud'), 70)


class InventoryAccountingTests(unittest.TestCase):
    def test_owned_paths_keep_component_boundaries_and_deduplicate_aliases(self):
        root = Path.cwd() / 'inventory'
        sample = {'_entries': [
            (str(root / 'job' / 'output.mp4'), 1, 1, 30),
            (str(root / 'job' / 'alias.mp4'), 1, 1, 30),
            (str(root / 'job-old' / 'output.mp4'), 1, 2, 40),
            (str(root / 'jobs' / 'output.mp4'), 1, 3, 50),
        ]}
        original = list(sample['_entries'])
        with patch.object(Path, 'stat', side_effect=AssertionError('Accounting must use the sample')):
            self.assertEqual(_owned_entries(str(root / 'job'), sample), {(1, 1): 30})
            self.assertEqual(_owned_entries(str(root / 'job' / 'output.mp4'), sample), {(1, 1): 30})
        self.assertEqual(sample, {'_entries': original})

    def test_backup_companions_are_exact_paths_and_require_backup_ownership(self):
        path = str(Path.cwd() / 'inventory' / 'database.db')
        names = [path, *(path + suffix for suffix in ('-journal', '-wal', '-shm', '.manifest.json'))]
        sample = {'_entries': [(name, 1, index, 10) for index, name in enumerate(names)]}
        sample['_entries'].extend([
            (path + '-wal.old', 1, 5, 100),
            (str(Path(path + '-wal') / 'child'), 1, 6, 100),
        ])
        self.assertEqual(_owned_entries(path, sample), {(1, 0): 10})
        self.assertEqual(_owned_entries(path, sample, sqlite_backup=True),
                         {(1, index): 10 for index in range(5)})

    def test_inventory_path_parsing_does_not_multiply_by_reservation_count(self):
        root = Path.cwd() / 'inventory'
        claims, files_per_claim = 16, 32
        rows = [StorageReservation(id=str(index), owner='writer', scope='local', filesystem='local',
                    path=str(root / f'job-{index}'), peak_bytes=100) for index in range(claims)]
        sample = {'_entries': [(str(root / f'job-{index}' / f'{file}.bin'), 1,
                    index * files_per_claim + file, 1)
                    for index in range(claims) for file in range(files_per_claim)]}
        with patch('services.storage_capacity.Path', wraps=Path) as paths:
            self.assertEqual(_remaining_from_sample(rows, sample), claims * (100 - files_per_claim))
        # Permit linear inventory preparation and pairwise claim-conflict checks,
        # but reject reparsing the entire inventory for every reservation.
        self.assertLessEqual(paths.call_count, 2 * len(sample['_entries']) + 2 * claims ** 2)
        self.assertEqual(set(sample), {'_entries'})


if __name__ == '__main__':
    unittest.main()
