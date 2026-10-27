"""Exercise queue publication and native cancellation with actual durable admission."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from lifecycle.media_work import supervised_output
from models import DownloadTask, StorageReservation
from services import queue
from services.ingestion_estimate import IngestionEstimate
from services.ingest_preview import ingest_preview_service
from services.state import ACTIVE_PROCESSES, register_process, unregister_process
from services.storage_capacity import storage_capacity
from services.storage_inventory import scan_managed
from tests.media import test_remediation as fixture


class QueueCapacityTests(unittest.IsolatedAsyncioTestCase):
    movie = fixture.MediaRemediation.movie

    async def asyncSetUp(self):
        await fixture.MediaRemediation.asyncSetUp(self)
        self.enterContext(patch.object(ingest_preview_service, 'root', Path(settings.TEMP_DIR) / 'ingest_preview'))

    async def asyncTearDown(self):
        await fixture.MediaRemediation.asyncTearDown(self)

    async def test_completed_queue_keeps_publication_copy_separate_and_releases_claims(self):
        accepted = []
        reserve = storage_capacity.reserve
        async def observe(owner, size, path, **kwargs):
            identifier = await reserve(owner, size, path, **kwargs)
            accepted.append((owner, size, str(path)))
            return identifier
        with patch.object(storage_capacity, 'reserve', side_effect=observe):
            await fixture.MediaRemediation._ingest(self)
        async with AsyncSession(self.engine) as db:
            task = await db.get(DownloadTask, 'ingest-test')
            self.assertEqual(task.status, 'COMPLETED', task.error_message)
            rows = (await db.exec(select(StorageReservation))).all()
            self.assertTrue(rows)
            self.assertTrue(all(row.state == 'released' for row in rows))
        self.assertEqual(len([owner for owner, _, _ in accepted if owner == 'ingestion-temp:ingest-test']), 1)
        copies = [(size, path) for owner, size, path in accepted if owner.startswith('media-mutation:')]
        self.assertTrue(any('.candidate' in path and size < 1024**2 for size, path in copies))

    async def test_denied_growth_reaps_native_writer_before_release_and_preserves_catalog(self):
        initial = scan_managed()['written_bytes']
        self.enterContext(patch.object(settings, 'STORAGE_POLICY', {'revision': 1,
            'local_budget_bytes': initial + 200, 'offload_enabled': False}))
        self.enterContext(patch('lifecycle.ingestion_capacity.MIB', 1))
        task_id = 'growth-test'
        async with AsyncSession(self.engine) as db:
            db.add(DownloadTask(id=task_id, tmdb_id=1, media_type='movie', video_url='https://public.example/media',
                                status='DOWNLOADING', created_at='2026-10-27T00:00:00'))
            await db.commit()
        children = []
        async def download(**kwargs):
            process = await asyncio.create_subprocess_exec(sys.executable, '-c',
                'import sys,time;open(sys.argv[1],"wb").write(b"x"*60);time.sleep(30)', kwargs['output_path'],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            children.append(process)
            register_process(task_id, process)
            try:
                await supervised_output(process, timeout=10)
            finally:
                unregister_process(task_id)
            return True, None
        release = storage_capacity.release
        async def checked_release(identifier):
            for process in children:
                self.assertIsNotNone(process.returncode, 'reservation released before native writer drained')
            await release(identifier)
        probe = {'has_video': True, 'has_audio': False, 'video_duration': 1, 'scan_quality': '720p',
                 'failure': None, 'video_source_type': 'auto', 'audio_source_type': 'auto'}
        with patch.object(queue, 'validate_url', AsyncMock()), \
             patch.object(queue, 'probe_media_stream', AsyncMock(return_value=probe)), \
             patch.object(queue, 'download_and_merge', download), \
             patch.object(queue.tmdb_client, 'fetch_movie_metadata', AsyncMock(return_value={'title': 'Film'})), \
             patch('services.ingestion_estimate.estimate_ingestion', return_value=IngestionEstimate(100, 100)), \
             patch.object(storage_capacity, 'release', side_effect=checked_release):
            await asyncio.wait_for(queue.DownloadQueueManager()._process_admitted_task(task_id), 15)
        self.assertTrue(children, 'native writer was never admitted')
        self.assertNotIn(task_id, ACTIVE_PROCESSES)
        self.assertEqual(self.video.read_bytes(), b'original-video')
        async with AsyncSession(self.engine) as db:
            task = await db.get(DownloadTask, task_id)
            self.assertEqual(task.status, 'FAILED')
            self.assertIn('storage_local_budget_exceeded', task.error_message)
            rows = (await db.exec(select(StorageReservation))).all()
            self.assertTrue(all(row.state == 'released' for row in rows))

    async def test_preview_admission_failure_records_failure_before_generation_changes(self):
        initial = scan_managed()['written_bytes']
        with patch.object(settings, 'STORAGE_POLICY', {'revision': 1,
                'local_budget_bytes': initial + 300 * 1024**2, 'offload_enabled': False}):
            await fixture.MediaRemediation._ingest(self)
        async with AsyncSession(self.engine) as db:
            task = await db.get(DownloadTask, 'ingest-test')
            self.assertEqual(task.status, 'FAILED')
            self.assertIn('storage_local_budget_exceeded', task.error_message)
            self.assertIsNone(task.current_preview_id)
            rows = (await db.exec(select(StorageReservation))).all()
            self.assertTrue(all(row.state == 'released' for row in rows))
        self.assertEqual(self.video.read_bytes(), b'original-video')
