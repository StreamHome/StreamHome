"""Actual cloud-only playback refresh binds measured proof to inspected originals."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import unittest
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, Request
from sqlalchemy import event, text
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from lifecycle import media_publication
from lifecycle.media_publication import recover_media_operations
from models import AuthSession, MediaOperationReceipt, Movie, Profile, User
from routes import playback
from routes.auth import get_current_user
from services.media_probe import probe_completed_media
from services.media_source import clear_cloud_object_cache, resolve_media_source
from services.playback_prep import PlaybackPrepService
from services.rclone import RcloneResult, rclone_service
from tests.media import test_cloud_publication as cloud_fixture


class CloudAnalysisIdentity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self, *, cleanup_owner=None):
        cleanup_owner = cleanup_owner if cleanup_owner is not None else self
        self.resources = AsyncExitStack()
        cleanup_owner.addAsyncCleanup(self.resources.aclose)
        self.fixture = cloud_fixture.CloudPublicationRegression(methodName="test_real_rclone_candidate_check_publish_and_acceptance")
        await self.fixture.asyncSetUp(cleanup_owner=SimpleNamespace(addAsyncCleanup=self.resources.push_async_callback))
        self.engine = self.fixture.engine
        self.service = PlaybackPrepService()
        self.resources.push_async_callback(self.service.quiesce_for_update, timeout=10)
        self.resources.enter_context(patch.object(playback, "playback_prep_service", self.service))
        self.resources.enter_context(patch.object(self.service, "prepare", AsyncMock()))
        self.resources.enter_context(patch.object(playback, "_cloud_audio_probe_times", {}))
        self.resources.callback(clear_cloud_object_cache)
        self.remote = self.fixture.target / "film.mp4"
        self.metadata = self.fixture.target / ".metadata" / "metadata.json"
        self.make_video()
        self.user = User(id=801, email="cloud-analysis@example.test", password_hash="unused")
        self.auth = AuthSession(id="cloud-analysis-session", user_id=801, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            movie = await session.get(Movie, "m_1")
            movie.duration = "2h"
            movie.probed_duration = None
            movie.analysis_metadata_str = "{}"
            # Analysis refresh must preserve accepted DB mutables. Seed the
            # same accepted markers in both halves of this legacy fixture.
            movie.skip_markers_str = json.dumps({"intro": []})
            session.add(self.user)
            session.add(self.auth)
            session.add(Profile(id="cloud-analysis-profile", name="Cloud Analysis"))
            await session.commit()
        self.session_hook = None

        async def sessions():
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                if self.session_hook:
                    self.session_hook(session)
                yield session

        app = FastAPI()
        @app.middleware("http")
        async def attach(request: Request, call_next):
            request.state.auth_session = self.auth
            return await call_next(request)
        app.include_router(playback.router)
        app.dependency_overrides[get_session] = sessions
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://cloud-analysis.test")
        self.resources.push_async_callback(self.client.aclose)
        clear_cloud_object_cache()

    async def asyncTearDown(self):
        await self.resources.aclose()

    def make_video(self, *, duration=1, multiple=False):
        command = [shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s=320x180:r=4:d={duration}"]
        if multiple:
            command += ["-f", "lavfi", "-i", "color=c=blue:s=640x360:r=4:d=3", "-map", "0:v:0", "-map", "1:v:0", "-disposition:v:0", "0", "-disposition:v:1", "default"]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", str(self.remote)]
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    async def create_run(self):
        return await self.client.post("/api/playback/runs", json={"movieId": "m_1", "profileId": "cloud-analysis-profile"})

    async def catalog(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            return await session.get(Movie, "m_1")

    async def assert_bound_metadata(self, duration):
        movie = await self.catalog()
        analysis = json.loads(movie.analysis_metadata_str)
        portable = json.loads(self.metadata.read_text())
        source = await resolve_media_source(movie.video_url)
        self.assertAlmostEqual(movie.probed_duration, duration, places=5)
        self.assertAlmostEqual(portable["probed_duration"], duration, places=5)
        self.assertEqual(portable["analysis"], analysis)
        self.assertEqual(analysis["video_fingerprint"], source.video_fingerprint)
        self.assertEqual(analysis["source_fingerprint"], movie.source_fingerprint)
        self.assertEqual(portable["source_fingerprint"], movie.source_fingerprint)
        self.assertEqual(portable["sentinel"], "healthy-old")
        self.assertEqual(portable["skip_markers"], {"intro": []})
        self.assertEqual(list((self.fixture.root / "temp" / "media-operations").glob("*/operation.json")), [])
        return movie, analysis

    async def test_available_cloud_legacy_refresh_measures_original_instead_of_tmdb_runtime(self):
        response = await self.create_run()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sourceMetadata"]["duration"], 1)
        self.assertEqual(response.json()["sourceMetadata"]["width"], 320)
        movie, analysis = await self.assert_bound_metadata(1)
        self.assertEqual(movie.duration, "1s")
        self.assertEqual(movie.quality, "180p")
        self.assertTrue(analysis["direct_timeline_compatible"])
        self.assertTrue(response.json()["sourceMetadata"]["progressiveCompatible"])

    async def test_existing_cloud_run_refreshes_legacy_measurements_with_unchanged_fingerprint(self):
        first = await self.create_run()
        self.assertEqual(first.status_code, 200, first.text)
        async with AsyncSession(self.engine) as session:
            movie = await session.get(Movie, "m_1")
            movie.analysis_metadata_str = "{}"
            movie.probed_duration = None
            movie.duration = "2h"
            movie.width = 0
            movie.height = 0
            await session.commit()
        response = await self.client.get(f"/api/playback/runs/{first.json()['runId']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sourceMetadata"]["duration"], 1)
        self.assertEqual(response.json()["sourceMetadata"]["width"], 320)
        await self.assert_bound_metadata(1)

    async def test_missing_cloud_portable_metadata_is_created_with_catalog_identity_and_proof(self):
        self.metadata.unlink()
        response = await self.create_run()
        self.assertEqual(response.status_code, 200, response.text)
        portable = json.loads(self.metadata.read_text())
        current = await self.catalog()
        self.assertEqual(portable["tmdb_id"], 1)
        self.assertEqual(portable["media_type"], "movie")
        self.assertEqual(portable["video_url"], current.video_url)
        self.assertEqual(portable["probed_duration"], 1)
        self.assertEqual(portable["analysis"], json.loads(current.analysis_metadata_str))

    async def test_remote_inspection_and_publication_do_not_hold_sqlite_writer(self):
        async with AsyncSession(self.engine) as session:
            session.add(Movie(id="m_writer", title="Writer", description="", thumbnail_url="", video_url="", duration="", release_year=2026))
            await session.commit()
        real_run = rclone_service.run
        checks = []
        async def run(*arguments, **kwargs):
            async with self.engine.connect() as connection:
                await connection.execute(text("PRAGMA busy_timeout=250"))
                await connection.execute(text("UPDATE movie SET title='Independent writer' WHERE id='m_writer'"))
                await connection.commit()
            checks.append(arguments[0])
            return await real_run(*arguments, **kwargs)
        with patch.object(rclone_service, "run", run):
            response = await self.create_run()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("copyto", checks)
        self.assertIn("moveto", checks)
        await self.assert_bound_metadata(1)

    async def test_changed_cloud_original_replaces_duration_and_revokes_old_direct_proof(self):
        first = await self.create_run()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["sourceMetadata"]["progressiveCompatible"])
        old_fingerprint = first.json()["sourceFingerprint"]
        self.make_video(duration=2, multiple=True)
        # Old tickets cannot exploit a cached stat while the physical object differs.
        old_ticket = await self.client.get(first.json()["progressiveUrl"], headers={"Range": "bytes=0-31"})
        self.assertEqual(old_ticket.status_code, 409, old_ticket.text)
        self.assertEqual(old_ticket.json()["detail"]["code"], "PLAYBACK_SOURCE_CHANGED")
        # Source admission must refresh this object even while its old stat is cached.
        response = await self.create_run()
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()
        self.assertNotEqual(run["sourceFingerprint"], old_fingerprint)
        self.assertEqual(run["sourceMetadata"]["duration"], 2)
        self.assertFalse(run["sourceMetadata"]["progressiveCompatible"])
        self.assertEqual(run["progressiveUrl"], "")
        _, analysis = await self.assert_bound_metadata(2)
        self.assertFalse(analysis["direct_timeline_compatible"])
        progressive = await self.client.get(f"/api/playback/progressive/m_1?ticket={run['ticket']}")
        self.assertEqual(progressive.status_code, 409, progressive.text)

    async def test_sidecar_change_preserves_proof_only_for_same_bound_video(self):
        first = await self.create_run()
        self.assertEqual(first.status_code, 200, first.text)
        old = await self.catalog()
        audio = self.fixture.target / "audio"
        audio.mkdir()
        result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", str(audio / "en.m4a")], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        playback._cloud_audio_probe_times["m_1"] = 0
        with patch.object(playback, "probe_completed_media", AsyncMock(side_effect=AssertionError("Unchanged inspected video should not be reprobed"))):
            response = await self.create_run()
        self.assertEqual(response.status_code, 200, response.text)
        current, analysis = await self.assert_bound_metadata(1)
        self.assertNotEqual(current.source_fingerprint, old.source_fingerprint)
        self.assertEqual(json.loads(old.analysis_metadata_str)["video_fingerprint"], analysis["video_fingerprint"])
        self.assertTrue(response.json()["sourceMetadata"]["progressiveCompatible"])
        self.assertTrue(any(item["source"] == "external" for item in current.audio_metadata))

    async def test_failed_cloud_spool_does_not_adopt_new_identity_or_old_proof(self):
        first = await self.create_run()
        self.assertEqual(first.status_code, 200, first.text)
        old = await self.catalog()
        portable = self.metadata.read_bytes()
        self.make_video(duration=2, multiple=True)
        clear_cloud_object_cache()
        real_copy = rclone_service.copyto_atomic
        async def copy(source, destination, **kwargs):
            if source.endswith("film.mp4"):
                return RcloneResult(1, error_code="rclone_failed")
            return await real_copy(source, destination, **kwargs)
        with patch.object(rclone_service, "copyto_atomic", copy):
            response = await self.create_run()
        self.assertEqual(response.status_code, 503, response.text)
        current = await self.catalog()
        self.assertEqual(current.source_fingerprint, old.source_fingerprint)
        self.assertEqual(current.analysis_metadata_str, old.analysis_metadata_str)
        self.assertEqual(self.metadata.read_bytes(), portable)

    async def test_unprobeable_replacement_cannot_adopt_stale_proof(self):
        first = await self.create_run()
        self.assertEqual(first.status_code, 200, first.text)
        old = await self.catalog()
        portable = self.metadata.read_bytes()
        self.remote.write_bytes(b"invalid completed media")
        clear_cloud_object_cache()
        response = await self.create_run()
        self.assertEqual(response.status_code, 422, response.text)
        current = await self.catalog()
        self.assertEqual(current.source_fingerprint, old.source_fingerprint)
        self.assertEqual(current.analysis_metadata_str, old.analysis_metadata_str)
        self.assertEqual(self.metadata.read_bytes(), portable)

    def after_commit_failure(self, session):
        def fail(_):
            raise RuntimeError("Injected cloud analysis acknowledgment failure")
        event.listen(session.sync_session, "after_commit", fail)

    async def test_actual_after_commit_failure_keeps_accepted_cloud_metadata_and_current_proof(self):
        self.session_hook = self.after_commit_failure
        response = await self.create_run()
        self.assertEqual(response.status_code, 500)
        self.session_hook = None
        movie, analysis = await self.assert_bound_metadata(1)
        self.assertTrue(analysis["direct_timeline_compatible"])
        response = await self.create_run()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sourceFingerprint"], movie.source_fingerprint)

    async def test_unknown_receipt_retains_cloud_metadata_recovery_evidence_and_blocks_readers(self):
        self.session_hook = self.after_commit_failure
        with patch.object(media_publication, "_read_acceptance_receipt", AsyncMock(side_effect=RuntimeError("Injected receipt verification outage"))):
            response = await self.create_run()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "MEDIA_RECOVERY_REQUIRED")
        self.session_hook = None
        pending = list((self.fixture.root / "temp" / "media-operations").glob("*/operation.json"))
        self.assertEqual(len(pending), 1)
        response = await self.create_run()
        self.assertEqual(response.status_code, 503, response.text)
        await recover_media_operations()
        await self.assert_bound_metadata(1)
        response = await self.create_run()
        self.assertEqual(response.status_code, 200, response.text)

    async def test_precommit_failure_restores_remote_metadata_and_old_catalog(self):
        first = await self.create_run()
        self.assertEqual(first.status_code, 200, first.text)
        old = await self.catalog()
        portable = self.metadata.read_bytes()
        self.make_video(duration=2, multiple=True)
        clear_cloud_object_cache()
        def before_commit(session):
            def fail(_):
                raise RuntimeError("Injected precommit failure")
            event.listen(session.sync_session, "before_commit", fail)
        self.session_hook = before_commit
        response = await self.create_run()
        self.assertEqual(response.status_code, 500)
        self.session_hook = None
        current = await self.catalog()
        self.assertEqual(current.source_fingerprint, old.source_fingerprint)
        self.assertEqual(current.analysis_metadata_str, old.analysis_metadata_str)
        self.assertEqual(self.metadata.read_bytes(), portable)
        response = await self.create_run()
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_bound_metadata(2)

    async def test_remote_video_change_during_spool_is_not_accepted(self):
        old = await self.catalog()
        portable = self.metadata.read_bytes()
        real_copy = rclone_service.copyto_atomic
        async def copy(source, destination, **kwargs):
            result = await real_copy(source, destination, **kwargs)
            if source.endswith("film.mp4"):
                self.make_video(duration=2)
            return result
        with patch.object(rclone_service, "copyto_atomic", copy):
            response = await self.create_run()
        self.assertEqual(response.status_code, 409, response.text)
        current = await self.catalog()
        self.assertEqual(current.source_fingerprint, old.source_fingerprint)
        self.assertEqual(current.analysis_metadata_str, old.analysis_metadata_str)
        self.assertEqual(self.metadata.read_bytes(), portable)


class ReusedCloudFixtureCleanup(unittest.IsolatedAsyncioTestCase):
    async def assert_failed_setup_restores_resources(self, *, fail_analysis):
        original_settings = {name: getattr(settings, name) for name in
            ("MEDIA_DIR", "TEMP_DIR", "RCLONE_REMOTE_PATH", "STORAGE_ENGINE")}
        original_engine = cloud_fixture.db_module.engine
        original_queue_engine = cloud_fixture.queue.engine
        original_config = rclone_service.config_path
        original_service = playback.playback_prep_service
        original_probe_times = playback._cloud_audio_probe_times
        original_reservations = {name: getattr(cloud_fixture.queue.DownloadQueueManager, name) for name in
            ("MINIMUM_TASK_RESERVATION_BYTES", "STORAGE_SAFETY_MARGIN_BYTES", "ESTIMATED_MEDIA_BYTES_PER_SECOND")}
        fixture = CloudAnalysisIdentity() if fail_analysis else cloud_fixture.CloudPublicationRegression()
        target = patch.object(CloudAnalysisIdentity, "make_video", side_effect=RuntimeError("injected setup failure")) if fail_analysis else \
            patch.object(cloud_fixture.SQLModel.metadata, "create_all", side_effect=RuntimeError("injected setup failure"))
        async with AsyncExitStack() as resources:
            owner = SimpleNamespace(addAsyncCleanup=resources.push_async_callback)
            with target, self.assertRaisesRegex(RuntimeError, "injected setup failure"):
                await fixture.asyncSetUp(cleanup_owner=owner)
        nested = fixture.fixture if fail_analysis else fixture
        self.assertFalse(nested.root.exists())
        self.assertEqual({name: getattr(settings, name) for name in original_settings}, original_settings)
        self.assertIs(cloud_fixture.db_module.engine, original_engine)
        self.assertIs(cloud_fixture.queue.engine, original_queue_engine)
        self.assertIs(rclone_service.config_path, original_config)
        self.assertIs(playback.playback_prep_service, original_service)
        self.assertIs(playback._cloud_audio_probe_times, original_probe_times)
        self.assertEqual({name: getattr(cloud_fixture.queue.DownloadQueueManager, name) for name in original_reservations}, original_reservations)
        if fail_analysis:
            self.assertTrue(fixture.service.update_quiesced)
            self.assertFalse(fixture.service.active_jobs)
        # Cleanup remains safe when the normal teardown also requests it.
        await fixture.asyncTearDown()

    async def test_nested_publication_setup_failure_restores_resources(self):
        await self.assert_failed_setup_restores_resources(fail_analysis=False)

    async def test_analysis_setup_failure_restores_resources_and_service_aliases(self):
        await self.assert_failed_setup_restores_resources(fail_analysis=True)
