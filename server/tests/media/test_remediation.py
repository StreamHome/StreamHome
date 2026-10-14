"""Behavioral media regressions; run from a disposable installation only."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
import db as db_module
from models import Movie, Episode, MediaOperationReceipt, DownloadTask, MovieResponse
from lifecycle.media_publication import MediaMutation, current_mutation, recover_media_operations
from lifecycle.media_work import supervised_output
from services import media_updates, queue as queue_module
from services.media_analysis import resolution_label
from services.media_probe import probe_completed_media, probe_media_stream, probe_cloud_external_audio
from services.state import register_process, unregister_process, ACTIVE_PROCESSES
from services.ingestion_errors import IngestionTaskError


class MediaRemediation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # These tiny media fixtures exercise publication, not host free-space policy.
        for name in ("MINIMUM_TASK_RESERVATION_BYTES", "STORAGE_SAFETY_MARGIN_BYTES", "ESTIMATED_MEDIA_BYTES_PER_SECOND"):
            self.enterContext(patch.object(queue_module.DownloadQueueManager, name, 1024**2))
        self.temporary = tempfile.TemporaryDirectory(prefix='media-remediation-')
        self.root = Path(self.temporary.name)
        self.original_paths = settings.MEDIA_DIR, settings.TEMP_DIR
        settings.MEDIA_DIR = str(self.root/'media')
        settings.TEMP_DIR = str(self.root/'temp')
        Path(settings.TEMP_DIR).mkdir()
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{self.root}/test.db')
        self.patch = patch.object(db_module, 'engine', self.engine)
        self.patch.start()
        self.queue_patch = patch.object(queue_module, 'engine', self.engine)
        self.queue_patch.start()
        async with self.engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        self.directory = Path(settings.MEDIA_DIR)/'Movies'/'Film_2026_TMDB_1'
        self.directory.mkdir(parents=True)
        self.video = self.directory/'film.mp4'
        self.video.write_bytes(b'original-video')
        self.subtitle = self.directory/'subtitle_en.vtt'
        self.subtitle.write_text('WEBVTT\n\n00:00.000 --> 00:01.000\nOriginal\n')
        self.metadata = self.directory/'.metadata'/'metadata.json'
        self.metadata.parent.mkdir()
        self.metadata.write_text(json.dumps({'catalog_source':'server','skip_markers':{'intro':[]},'sentinel':'preserved'}))
        async with AsyncSession(self.engine) as session:
            movie = self.movie()
            movie.subtitles = [{'trackId':'en','fileName':'subtitle_en.vtt','language':'en','ext':'.vtt'}]
            session.add(movie)
            await session.commit()

    def movie(self):
        return Movie(id='m_1',title='Film',description='Description',thumbnail_url='',video_url='/media/Movies/Film_2026_TMDB_1/film.mp4',duration='1m',release_year=2026,quality='720p',source_fingerprint='original')

    async def asyncTearDown(self):
        self.patch.stop();self.queue_patch.stop()
        await self.engine.dispose()
        settings.MEDIA_DIR,settings.TEMP_DIR = self.original_paths
        self.temporary.cleanup()

    async def test_sidecar_db_failure_restores_file_and_portable_metadata(self):
        old_file,old_metadata = self.subtitle.read_bytes(),self.metadata.read_bytes()
        async def prepared(url,destination,**kwargs):destination.write_text('WEBVTT\nChanged')
        async with AsyncSession(self.engine,expire_on_commit=False) as session:
            with patch.object(media_updates,'prepare_subtitle_asset',prepared), patch.object(session,'commit',AsyncMock(side_effect=RuntimeError('commit failure'))):
                with self.assertRaises(RuntimeError):
                    await media_updates.upsert_subtitle('m_1','en',language='en',label=None,url='https://public.example/sub',headers={},client_address='',db=session)
            await session.rollback()
        self.assertEqual(self.subtitle.read_bytes(),old_file)
        self.assertEqual(self.metadata.read_bytes(),old_metadata)
        async with AsyncSession(self.engine) as session:
            movie=await session.get(Movie,'m_1')
            self.assertEqual(movie.source_fingerprint,'original')
            self.assertEqual(movie.subtitles[0]['fileName'],'subtitle_en.vtt')

    async def test_subtitle_remove_cancel_restores_original(self):
        old_file,old_metadata = self.subtitle.read_bytes(),self.metadata.read_bytes()
        entered=asyncio.Event()
        async def blocked(*args):entered.set();await asyncio.Event().wait()
        async with AsyncSession(self.engine,expire_on_commit=False) as session:
            with patch.object(media_updates,'persist_portable_metadata',blocked):
                task=asyncio.create_task(media_updates.remove_subtitle('m_1','en',session))
                await entered.wait();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            await session.rollback()
        self.assertEqual(self.subtitle.read_bytes(),old_file)
        self.assertEqual(self.metadata.read_bytes(),old_metadata)

    async def test_accepted_commit_cancel_preserves_new_metadata_and_catalog(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async with AsyncSession(self.engine,expire_on_commit=False) as session:
            real_commit=session.commit
            async def delayed_commit():entered.set();await release.wait();await real_commit()
            with patch.object(session,'commit',delayed_commit):
                task=asyncio.create_task(media_updates.update_skip_markers('m_1',{'intro':[{'start':1,'end':2}]},session))
                await entered.wait();task.cancel();release.set()
                with self.assertRaises(asyncio.CancelledError):await task
        async with AsyncSession(self.engine) as session:
            movie=await session.get(Movie,'m_1')
            self.assertEqual(movie.skip_markers,{'intro':[{'start':1,'end':2}]})
        self.assertEqual(json.loads(self.metadata.read_text())['skip_markers'],{'intro':[{'start':1,'end':2}]})
        self.assertEqual(list((Path(settings.TEMP_DIR)/'media-operations').glob('*/operation.json')),[])

    async def test_process_crash_recovery_restores_sidecar(self):
        mutation=MediaMutation('m_1');await mutation.__aenter__()
        mutation.capture_local(str(self.subtitle));self.subtitle.write_bytes(b'new')
        current_mutation.reset(mutation.token)
        await recover_media_operations()
        self.assertIn(b'Original',self.subtitle.read_bytes())

    async def test_crash_after_acceptance_only_cleans_evidence(self):
        mutation=MediaMutation('m_1');await mutation.__aenter__()
        mutation.capture_local(str(self.subtitle));self.subtitle.write_bytes(b'accepted')
        async with AsyncSession(self.engine) as session:await mutation.commit(session)
        current_mutation.reset(mutation.token)
        await recover_media_operations()
        self.assertEqual(self.subtitle.read_bytes(),b'accepted')

    async def test_directory_publication_recovery_retains_old_video_and_metadata(self):
        staging=Path(settings.TEMP_DIR)/'candidate';staging.mkdir()
        (staging/'film.mp4').write_bytes(b'new')
        mutation=MediaMutation('m_1');await mutation.__aenter__()
        mutation.publish_directory(str(staging),str(self.directory))
        self.assertEqual(self.video.read_bytes(),b'new')
        current_mutation.reset(mutation.token)
        await recover_media_operations()
        self.assertEqual(self.video.read_bytes(),b'original-video')
        self.assertEqual(json.loads(self.metadata.read_text())['sentinel'],'preserved')

    async def test_directory_thread_cancellation_compensates_completed_move(self):
        staging=Path(settings.TEMP_DIR)/'candidate';staging.mkdir();(staging/'film.mp4').write_bytes(b'new')
        entered=asyncio.Event()
        async def operation():
            async with MediaMutation('m_1') as mutation:
                await asyncio.to_thread(mutation.publish_directory,str(staging),str(self.directory))
                entered.set();await asyncio.Event().wait()
        task=asyncio.create_task(operation());await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.video.read_bytes(),b'original-video')

    async def test_failed_final_probe_never_changes_healthy_catalog(self):
        async with AsyncSession(self.engine) as session:
            with patch.object(queue_module,'probe_completed_media',AsyncMock(return_value={})),patch.object(queue_module,'download_and_cache_metadata_image',AsyncMock(return_value='')):
                with self.assertRaises(IngestionTaskError):
                    await queue_module.DownloadQueueManager()._catalog_media(session,1,'movie',None,None,{},str(self.video),quality='2160p',catalog_file_path=str(self.video))
            await session.rollback()
        async with AsyncSession(self.engine) as session:self.assertEqual((await session.get(Movie,'m_1')).quality,'720p')

    async def _ingest(self, *, invalid=False, cancel_before_commit=False):
        fixture=self.root/'fixture.mp4'
        subprocess.run([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i','color=size=1280x720:rate=4:duration=0.5','-c:v','libx264','-preset','ultrafast','-threads','1','-y',str(fixture)],check=True,capture_output=True,timeout=30)
        async with AsyncSession(self.engine) as session:
            session.add(DownloadTask(id='ingest-test',tmdb_id=1,media_type='movie',video_url='https://public.example/film',quality='2160p',status='DOWNLOADING',created_at='2026-10-13T00:00:00'))
            await session.commit()
        async def download(**kwargs):
            path=Path(kwargs['output_path']);path.write_bytes(b'invalid') if invalid else shutil.copy2(fixture,path)
            return True,None
        entered=asyncio.Event()
        real_commit=MediaMutation.commit
        async def blocked_commit(mutation,session):entered.set();await asyncio.Event().wait()
        with patch.object(queue_module,'validate_url',AsyncMock()), patch.object(queue_module,'probe_media_stream',AsyncMock(return_value={'has_video':True,'has_audio':False,'video_duration':.5,'scan_quality':'720p','failure':None,'video_source_type':'auto','audio_source_type':'auto'})), patch.object(queue_module,'download_and_merge',download), patch.object(queue_module.tmdb_client,'fetch_movie_metadata',AsyncMock(return_value={'title':'Film','releaseYear':2026,'duration':'2h'})), patch.object(queue_module,'download_and_cache_metadata_image',AsyncMock(return_value='')), patch.object(queue_module,'extract_audio_and_strip_video',AsyncMock(return_value=[])), patch.object(queue_module.vibe_analysis_manager,'enqueue',AsyncMock()), patch.object(queue_module.DownloadQueueManager,'_schedule_playback_baseline',AsyncMock()), patch.object(MediaMutation,'commit',blocked_commit if cancel_before_commit else real_commit):
            task=asyncio.create_task(queue_module.DownloadQueueManager()._process_admitted_task('ingest-test'))
            if cancel_before_commit:
                await asyncio.wait_for(entered.wait(),10);task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            else:await task

    async def test_real_queue_rejects_sender_label_and_publishes_measured_analysis(self):
        await self._ingest()
        async with AsyncSession(self.engine) as session:
            movie=await session.get(Movie,'m_1');task=await session.get(DownloadTask,'ingest-test')
            self.assertEqual(task.status,'COMPLETED');self.assertEqual(movie.quality,'720p')
            response=MovieResponse.from_db(movie)
            self.assertEqual(response.analysis['sender_quality'],'2160p')
            self.assertEqual(response.analysis['quality_provenance'],'final_probe')
            self.assertGreater(movie.probed_duration,0);self.assertNotEqual(movie.duration,'2h')
            published=Path(settings.MEDIA_DIR)/movie.video_url.removeprefix('/media/')
            payload=json.loads((published.parent/'.metadata'/'metadata.json').read_text())
            self.assertEqual(payload['quality'],'720p')
            self.assertEqual(payload['source_fingerprint'],movie.source_fingerprint)

    async def test_real_queue_final_probe_failure_preserves_prior_catalog(self):
        await self._ingest(invalid=True)
        async with AsyncSession(self.engine) as session:
            movie=await session.get(Movie,'m_1');task=await session.get(DownloadTask,'ingest-test')
            self.assertEqual(task.status,'FAILED');self.assertIn('MEDIA_PROBE_FAILED',task.error_message);self.assertEqual(movie.source_fingerprint,'original')
            self.assertEqual(movie.video_url,'/media/Movies/Film_2026_TMDB_1/film.mp4')
        self.assertEqual(self.video.read_bytes(),b'original-video')

    async def test_real_queue_cancel_before_acceptance_restores_catalog_and_directory(self):
        await self._ingest(cancel_before_commit=True)
        async with AsyncSession(self.engine) as session:
            movie=await session.get(Movie,'m_1');self.assertEqual(movie.source_fingerprint,'original')
        self.assertEqual(self.video.read_bytes(),b'original-video')
        self.assertEqual(json.loads(self.metadata.read_text())['sentinel'],'preserved')

    async def test_actual_process_death_before_and_after_acceptance(self):
        for accepted in (False, True):
            old=self.subtitle.read_bytes()
            code=f"""import asyncio,os
from pathlib import Path
from config import settings
settings.MEDIA_DIR={settings.MEDIA_DIR!r}
settings.TEMP_DIR={settings.TEMP_DIR!r}
from lifecycle.media_publication import MediaMutation
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession
async def execute():
 op=MediaMutation('m_1');await op.__aenter__()
 op.capture_local({str(self.subtitle)!r});Path({str(self.subtitle)!r}).write_bytes(b'process-published')
 if {accepted!r}:
  engine=create_async_engine({str(self.engine.url)!r})
  async with AsyncSession(engine) as db:await op.commit(db)
 os._exit(73)
asyncio.run(execute())
"""
            result=subprocess.run([sys.executable,'-c',code],capture_output=True,timeout=15)
            self.assertEqual(result.returncode,73,result.stderr.decode())
            await recover_media_operations()
            self.assertEqual(self.subtitle.read_bytes(),b'process-published' if accepted else old)

    async def test_cloud_dubbing_preserves_embedded_same_language(self):
        from services.rclone import RcloneResult, rclone_service
        embedded=[{'source':'embedded','streamIndex':1,'language':'en','default':True}]
        result=RcloneResult(0,json.dumps([{'Name':'en.m4a','Size':100,'ModTime':'now'}]))
        with patch.object(rclone_service,'run',AsyncMock(return_value=result)):
            tracks=await probe_cloud_external_audio('gdrive:media/film.mp4',embedded)
        self.assertEqual(len(tracks),2);self.assertEqual(tracks[0]['streamIndex'],1)
        self.assertEqual(tracks[1]['source'],'external')


class ProcessSupervisor(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_reaps_and_releases_registry(self):
        process=await asyncio.create_subprocess_exec(sys.executable,'-c','import time;time.sleep(60)',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        key=f'test-{process.pid}';register_process(key,process)
        task=asyncio.create_task(supervised_output(process,timeout=10))
        await asyncio.sleep(.05);task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):await task
        finally:unregister_process(key)
        self.assertIsNotNone(process.returncode);self.assertNotIn(key,ACTIVE_PROCESSES)

    async def test_timeout_kills_sigterm_ignoring_child(self):
        process=await asyncio.create_subprocess_exec(sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        with self.assertRaises(TimeoutError):await supervised_output(process,timeout=.2)
        self.assertIsNotNone(process.returncode)

    async def test_live_output_limit_is_enforced_before_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination=Path(temporary)/'growing'
            code='import time,sys;f=open(sys.argv[1],"wb",buffering=0)\nwhile True:f.write(b"x"*4096);time.sleep(.01)'
            process=await asyncio.create_subprocess_exec(sys.executable,'-c',code,str(destination),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            with self.assertRaises(ValueError):await supervised_output(process,timeout=10,output_path=destination,maximum_bytes=4096)
            self.assertIsNotNone(process.returncode)


class RealMediaAnalysis(unittest.IsolatedAsyncioTestCase):
    async def test_real_rasters_and_selected_stream_measurements(self):
        with tempfile.TemporaryDirectory() as temporary:
            for width,height,label in ((854,480,'480p'),(1280,720,'720p'),(1920,1080,'1080p'),(2560,1440,'1440p'),(3840,2160,'2160p'),(1920,800,'1080p'),(800,1920,'1080p')):
                path=Path(temporary)/f'{width}-{height}.mp4'
                result=subprocess.run([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i',f'color=size={width}x{height}:rate=4:duration=0.5','-c:v','libx264','-preset','ultrafast','-threads','1','-y',str(path)],capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                probe=await probe_completed_media(str(path))
                self.assertTrue(probe['valid_video']);self.assertEqual(probe['quality'],label)
                self.assertEqual((probe['width'],probe['height']),(width,height))
                self.assertGreater(probe['container_bitrate'],0)
                self.assertEqual(probe['analysis']['bitrate_unit'],'bits_per_second')
                remote=await probe_media_stream(str(path))
                self.assertEqual(remote['scan_quality'],label)
                self.assertGreater(remote['video_duration'],0)

    async def test_selected_first_video_is_not_replaced_by_larger_secondary_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'multiple.mkv'
            command=[shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i','color=size=854x480:rate=4:duration=0.5','-f','lavfi','-i','color=size=1280x720:rate=4:duration=0.5','-map','0:v:0','-map','1:v:0','-c:v','libx264','-preset','ultrafast','-threads','1','-y',str(path)]
            result=await asyncio.to_thread(subprocess.run,command,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            final=await probe_completed_media(str(path));source=await probe_media_stream(str(path))
            self.assertEqual(final['quality'],'480p');self.assertEqual(source['scan_quality'],'480p')
            self.assertEqual(final['video_stream_index'],0);self.assertEqual(source['video_stream_index'],0)
            self.assertEqual(final['video_bitrate'],None)
            self.assertGreater(final['container_bitrate'],0)

    async def test_invalid_and_empty_files_are_not_valid_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'invalid.mp4'
            for content in (b'',b'not-media'):
                path.write_bytes(content);self.assertFalse((await probe_completed_media(str(path))).get('valid_video'))

    def test_unknown_resolution_remains_unknown(self):
        self.assertEqual(resolution_label(None,1080),'Source')


if __name__=='__main__':unittest.main()
