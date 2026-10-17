"""Native backup admission and offline restore evidence in disposable roots."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import config
import db as database
from config import settings
from lifecycle.restore_controller import RestoreRecoveryRequired, require_no_media_publication_evidence
from models import StorageOperation, StorageReservation
from services import backup
from services.storage_capacity import StorageCapacityError, storage_capacity
from services.rclone import rclone_service


class StorageBackupRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = self.root / 'server'
        self.server.mkdir()
        self.media = self.server / 'media'
        self.media.mkdir()
        self.path = self.server / 'database.db'
        self.engine = create_async_engine('sqlite+aiosqlite:///' + str(self.path))
        self.patches = [patch.object(database, 'engine', self.engine),
            patch.object(config, 'config_dir', str(self.server)),
            patch.object(settings, 'BASE_DIR', str(self.root)),
            patch.object(settings, 'MEDIA_DIR', str(self.media)),
            patch.object(settings, 'TEMP_DIR', str(self.server / 'temp')),
            patch.object(settings, 'db_path', str(self.path)),
            patch.object(settings, 'STORAGE_POLICY', {'revision': 1,
                'offload_enabled': False, 'local_budget_bytes': 100_000_000,
                'cloud_budget_bytes': None, 'prefetch_next_episode': False,
                'hevc_compression_mode': 'auto'}),
            patch.object(backup, 'get_backup_dir', return_value=str(self.server / 'backup'))]
        for item in self.patches:
            item.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def test_native_snapshot_is_valid_and_releases_its_peak_reservation(self):
        path = Path(await backup.create_backup())
        evidence = backup.eligible_backup(path)
        self.assertEqual(evidence.format, 'manifest_v1')
        self.assertEqual(await storage_capacity.remaining(), 0)
        async with AsyncSession(self.engine) as session:
            rows = (await session.exec(select(StorageReservation))).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].owner, 'backup.snapshot')
        self.assertEqual(rows[0].state, 'released')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(path) as snapshot:
            self.assertEqual(snapshot.execute("SELECT count(*) FROM storagereservation WHERE state='active'").fetchone()[0], 0)
        # A later restore sees the settled backup owner in the snapshot itself.
        restored_root = self.root / 'restored'
        (restored_root / 'server').mkdir(parents=True)
        import shutil
        shutil.copyfile(path, restored_root / 'server/database.db')
        require_no_media_publication_evidence(restored_root)

    async def test_budget_denial_does_not_create_a_partial_backup(self):
        settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'local_budget_bytes': 1}
        with self.assertRaises(StorageCapacityError):
            await backup.create_backup()
        self.assertEqual(list((self.server / 'backup').iterdir()), [])

    async def test_snapshot_normalizes_foreign_admission_without_clearing_live_ownership(self):
        identifier = await storage_capacity.reserve('playback-output:old:video', 10_000,
            self.media / 'old-output')
        path = Path(await backup.create_backup())
        async with AsyncSession(self.engine) as session:
            live = await session.get(StorageReservation, identifier)
            self.assertEqual(live.state, 'active')
        with sqlite3.connect(path) as snapshot:
            historic = snapshot.execute('SELECT owner,peak_bytes,state FROM storagereservation WHERE id=?', (identifier,)).fetchone()
            self.assertEqual(historic, ('playback-output:old:video', 10_000, 'snapshot'))
        await storage_capacity.release(identifier)

    async def test_large_snapshot_normalization_journal_fits_its_admitted_peak(self):
        connect = sqlite3.connect
        with connect(self.path) as connection:
            connection.executemany('INSERT INTO storagereservation '
                '(id,owner,scope,filesystem,path,peak_bytes,written_bytes,baseline_bytes,track_materialized,state,created_at,updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', [(str(index), 'historical', 'local', 'test',
                    '/tmp/' + str(index) + 'x' * 400, 0, 0, 0, False, 'active', 1, 1) for index in range(7000)])
        admitted, observed = [], []
        reserve = storage_capacity.reserve
        async def trace_reserve(owner, peak, path, **kwargs):
            if owner == 'backup.snapshot':
                admitted.append(peak)
            return await reserve(owner, peak, path, **kwargs)
        class TraceConnection(sqlite3.Connection):
            def execute(connection, statement, *args, **kwargs):
                result = super().execute(statement, *args, **kwargs)
                if statement.startswith('UPDATE storagereservation SET state='):
                    destination = Path(super().execute('PRAGMA database_list').fetchone()[2])
                    observed.append(backup._backup_owned_bytes(destination))
                return result
        def trace_connect(path, *args, **kwargs):
            if Path(str(path)).parent == self.server / 'backup':
                kwargs['factory'] = TraceConnection
            return connect(path, *args, **kwargs)
        with patch.object(storage_capacity, 'reserve', trace_reserve), \
             patch.object(backup.sqlite3, 'connect', trace_connect):
            result = Path(await backup.create_backup())
        self.assertTrue(observed)
        self.assertLessEqual(max(observed), admitted[0])
        self.assertFalse(Path(str(result) + '-journal').exists())
        self.assertEqual(await storage_capacity.remaining(), 0)

    async def test_source_growth_aborts_native_copy_and_removes_owned_partial_pair(self):
        original = storage_capacity.allocation
        @asynccontextmanager
        async def growing_source(*args, **kwargs):
            async with original(*args, **kwargs) as identifier:
                connection = sqlite3.connect(self.path)
                try:
                    connection.execute('CREATE TABLE growth (value BLOB)')
                    connection.execute('INSERT INTO growth VALUES (zeroblob(3000000))')
                    connection.commit()
                finally:
                    connection.close()
                yield identifier
        with patch.object(storage_capacity, 'allocation', growing_source):
            with self.assertRaises(StorageCapacityError):
                await backup.create_backup()
        self.assertEqual(list((self.server / 'backup').iterdir()), [])
        self.assertEqual(await storage_capacity.remaining(), 0)

    async def test_native_cloud_backup_obeys_folder_budget_and_verifies_copy(self):
        if not rclone_service.executable():
            self.skipTest('Local Rclone is unavailable')
        remote = self.root / 'remote'
        remote.mkdir()
        configuration = self.root / 'local-rclone.conf'
        configuration.write_text('[isolated]\ntype = local\n')
        path = Path(await backup.create_backup())
        with patch.object(rclone_service, 'config_path', configuration), \
             patch.object(settings, 'RCLONE_REMOTE_PATH', 'isolated:' + str(remote)):
            settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'cloud_budget_bytes': 1}
            self.assertFalse(await backup.sync_backups_to_cloud())
            self.assertEqual(list(remote.iterdir()), [])
            settings.STORAGE_POLICY = {**settings.STORAGE_POLICY, 'cloud_budget_bytes': 100_000_000}
            self.assertTrue(await backup.sync_backups_to_cloud())
        copied = remote / 'backup' / path.name
        self.assertEqual(copied.read_bytes(), path.read_bytes())
        self.assertEqual(await storage_capacity.remaining('cloud'), 0)
        async with AsyncSession(self.engine) as session:
            operations = (await session.exec(select(StorageOperation).where(StorageOperation.kind == 'backup_sync'))).all()
        self.assertEqual([row.phase for row in operations], ['verified'])

    async def test_restore_rejects_unsettled_storage_even_without_media_journals(self):
        async with AsyncSession(self.engine) as session:
            session.add(StorageOperation(id='interrupted-copy', kind='offload', status='blocked'))
            await session.commit()
        with self.assertRaises(RestoreRecoveryRequired):
            require_no_media_publication_evidence(self.root)
        async with AsyncSession(self.engine) as session:
            row = await session.get(StorageOperation, 'interrupted-copy')
            row.status, row.phase = 'completed', 'completed'
            session.add(row)
            await session.commit()
        require_no_media_publication_evidence(self.root)

    async def test_restore_validates_accepted_manifests_without_pending_operations(self):
        directory = self.media / '.storage-placements'
        directory.mkdir()
        (directory / 'invalid.json').write_text(json.dumps({'version': 1}))
        with self.assertRaises(RestoreRecoveryRequired):
            require_no_media_publication_evidence(self.root)


if __name__ == '__main__':
    unittest.main()
