"""Actual rclone local-provider publication and compensation in disposable trees."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from models import AuthSession, DownloadTask, MediaOperationReceipt, Movie, PlaybackRun, Profile, User
from lifecycle.media_publication import MediaMutation, recover_media_operations
from lifecycle.media_work import MediaRecoveryRequired, require_resolved_media
from routes import playback
from services import queue
from services.media_source import local_playback_fingerprint
from fastapi import HTTPException
from starlette.requests import Request
from services.rclone import RcloneResult, rclone_service
from services.state import ACTIVE_PROCESSES


class CloudPublicationRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not rclone_service.executable():
            self.skipTest("rclone is unavailable")
        for name in ("MINIMUM_TASK_RESERVATION_BYTES", "STORAGE_SAFETY_MARGIN_BYTES", "ESTIMATED_MEDIA_BYTES_PER_SECOND"):
            self.enterContext(patch.object(queue.DownloadQueueManager, name, 1024**2))
        self.directory = tempfile.TemporaryDirectory(prefix="streamhome-cloud-publication-")
        self.root = Path(self.directory.name)
        self.previous = settings.MEDIA_DIR, settings.TEMP_DIR, settings.RCLONE_REMOTE_PATH, settings.STORAGE_ENGINE
        settings.MEDIA_DIR = str(self.root / "media")
        settings.TEMP_DIR = str(self.root / "temp")
        settings.RCLONE_REMOTE_PATH = f"isolated:{self.root / 'remote'}"
        settings.STORAGE_ENGINE = "CLOUD"
        Path(settings.MEDIA_DIR).mkdir()
        Path(settings.TEMP_DIR).mkdir()
        config = self.root / "rclone.conf"
        config.write_text("[isolated]\ntype = local\n", encoding="utf-8")
        self.config_patch = patch.object(rclone_service, "config_path", config)
        self.config_patch.start()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'cloud.db'}")
        self.engine_patch = patch.object(db_module, "engine", self.engine)
        self.engine_patch.start()
        self.queue_patch = patch.object(queue, "engine", self.engine)
        self.queue_patch.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.target = self.root / "remote" / "Movies" / "Film_2026_TMDB_1"
        self.original_filename = "film.mp4"
        (self.target / ".metadata").mkdir(parents=True)
        (self.target / "film.mp4").write_bytes(b"healthy-old-cloud-video")
        (self.target / "subtitle_en.vtt").write_bytes(b"WEBVTT\nOriginal subtitle")
        (self.target / ".metadata/metadata.json").write_text(json.dumps({"catalog_source": "server", "sentinel": "healthy-old", "skip_markers": {"intro": []}}))
        self.staging = Path(settings.TEMP_DIR) / "candidate"
        (self.staging / ".metadata").mkdir(parents=True)
        (self.staging / "film.mp4").write_bytes(b"candidate-new-cloud-video")
        (self.staging / ".metadata/metadata.json").write_text(json.dumps({"catalog_source": "server", "sentinel": "accepted-new"}))
        async with AsyncSession(self.engine) as db:
            db.add(Movie(id="m_1", title="Healthy original", description="Original metadata", thumbnail_url="", banner_url="", video_url="/media/Movies/Film_2026_TMDB_1/film.mp4", duration="1m", release_year=2026, quality="720p", source_fingerprint="old-cloud-fingerprint", availability="available", catalog_source="server"))
            await db.commit()

    async def asyncTearDown(self):
        self.config_patch.stop()
        self.engine_patch.stop()
        self.queue_patch.stop()
        await self.engine.dispose()
        settings.MEDIA_DIR, settings.TEMP_DIR, settings.RCLONE_REMOTE_PATH, settings.STORAGE_ENGINE = self.previous
        self.directory.cleanup()

    async def assert_original(self):
        self.assertEqual((self.target / self.original_filename).read_bytes(), b"healthy-old-cloud-video")
        self.assertEqual((self.target / "subtitle_en.vtt").read_bytes(), b"WEBVTT\nOriginal subtitle")
        self.assertEqual(json.loads((self.target / ".metadata/metadata.json").read_text())["sentinel"], "healthy-old")
        async with AsyncSession(self.engine) as db:
            movie = await db.get(Movie, "m_1")
            self.assertEqual(movie.title, "Healthy original")
            self.assertEqual(movie.source_fingerprint, "old-cloud-fingerprint")
            self.assertEqual(movie.video_url, f"/media/Movies/Film_2026_TMDB_1/{self.original_filename}")
            self.assertEqual(movie.availability, "available")

    async def test_real_rclone_candidate_check_publish_and_acceptance(self):
        async with MediaMutation("m_1") as mutation:
            await mutation.publish_cloud_directory(str(self.staging), "Movies/Film_2026_TMDB_1")
            async with AsyncSession(self.engine) as db:
                movie = await db.get(Movie, "m_1")
                movie.title = "Accepted replacement"
                movie.source_fingerprint = "accepted-fingerprint"
                await mutation.commit(db)
        self.assertEqual((self.target / "film.mp4").read_bytes(), b"candidate-new-cloud-video")
        self.assertFalse((self.target / "subtitle_en.vtt").exists())
        self.assertEqual(json.loads((self.target / ".metadata/metadata.json").read_text())["sentinel"], "accepted-new")
        self.assertEqual(list((Path(settings.TEMP_DIR) / "media-operations").glob("*/operation.json")), [])
        self.assertFalse(any(key.startswith("rclone:") for key in ACTIVE_PROCESSES))

    async def test_real_cloud_db_failure_restores_tree_and_keeps_healthy_catalog(self):
        with self.assertRaises(RuntimeError):
            async with MediaMutation("m_1") as mutation:
                await mutation.publish_cloud_directory(str(self.staging), "Movies/Film_2026_TMDB_1")
                async with AsyncSession(self.engine) as db:
                    movie = await db.get(Movie, "m_1")
                    movie.title = "Unaccepted replacement"
                    with patch.object(db, "commit", side_effect=RuntimeError("injected database failure")):
                        await mutation.commit(db)
        await self.assert_original()

    async def test_cancel_after_actual_target_sync_compensates_remote_tree(self):
        entered = asyncio.Event()
        real_run = rclone_service.run

        async def run(*arguments, **kwargs):
            result = await real_run(*arguments, **kwargs)
            if arguments[0] == "sync" and ".streamhome-ingestion/" in arguments[1]:
                entered.set()
                await asyncio.Event().wait()
            return result

        async def publish():
            async with MediaMutation("m_1") as mutation:
                await mutation.publish_cloud_directory(str(self.staging), "Movies/Film_2026_TMDB_1")

        with patch.object(rclone_service, "run", run):
            task = asyncio.create_task(publish())
            await asyncio.wait_for(entered.wait(), timeout=5)
            self.assertEqual((self.target / "film.mp4").read_bytes(), b"candidate-new-cloud-video")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        await self.assert_original()
        self.assertFalse(any(key.startswith("rclone:") for key in ACTIVE_PROCESSES))

    async def test_partial_publication_failure_restores_real_cloud_backup(self):
        real_run = rclone_service.run

        async def run(*arguments, **kwargs):
            result = await real_run(*arguments, **kwargs)
            if arguments[0] == "sync" and ".streamhome-ingestion/" in arguments[1]:
                (self.target / "film.mp4").write_bytes(b"partial-publication")
                return RcloneResult(1, error_code="rclone_failed")
            return result

        with patch.object(rclone_service, "run", run), self.assertRaises(RuntimeError):
            async with MediaMutation("m_1") as mutation:
                await mutation.publish_cloud_directory(str(self.staging), "Movies/Film_2026_TMDB_1")
        await self.assert_original()

    async def test_failed_compensation_retains_evidence_for_successful_restart_recovery(self):
        real_run = rclone_service.run

        async def run(*arguments, **kwargs):
            if arguments[0] == "sync" and ".streamhome-replacement-backups/" in arguments[1]:
                return RcloneResult(1, error_code="rclone_failed")
            return await real_run(*arguments, **kwargs)

        with patch.object(rclone_service, "run", run), self.assertRaises(RuntimeError):
            async with MediaMutation("m_1") as mutation:
                await mutation.publish_cloud_directory(str(self.staging), "Movies/Film_2026_TMDB_1")
                raise RuntimeError("failure before acceptance")
        evidence = list((Path(settings.TEMP_DIR) / "media-operations").glob("*/operation.json"))
        self.assertEqual(len(evidence), 1)
        backup = json.loads(evidence[0].read_text())["entries"][0]["backup"]
        backup_path = Path(backup.removeprefix("isolated:"))
        self.assertEqual((backup_path / "film.mp4").read_bytes(), b"healthy-old-cloud-video")
        # An old valid ticket must not resolve a partially replaced cloud path,
        # even if its cached cloud identity still matches the old catalog.
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            user = User(id=1, email="cloud-viewer@example.test", password_hash="unused")
            session = AuthSession(id="cloud-session", user_id=1, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600)
            db.add(user)
            db.add(session)
            db.add(Profile(id="cloud-profile", name="Cloud viewer"))
            db.add(PlaybackRun(id="cloud-run", auth_session_id=session.id, profile_id="cloud-profile", movie_id="m_1", source_fingerprint="old-cloud-fingerprint", lifecycle_state="active"))
            await db.commit()
            ticket, _ = playback.issue_playback_ticket(user, session, "cloud-profile", "cloud-run", "m_1", "old-cloud-fingerprint")
            await playback.validate_playback_ticket(ticket, "m_1", db)
            await db.rollback()
            request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("127.0.0.1", 5000), "scheme": "http", "server": ("localhost", 8000), "query_string": b""})
            with patch.object(playback, "require_available_source", AsyncMock()) as resolver:
                with self.assertRaises(HTTPException) as caught:
                    await playback.progressive_playback("m_1", request, ticket, db)
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(caught.exception.detail["code"], "MEDIA_RECOVERY_REQUIRED")
            resolver.assert_not_awaited()
        await recover_media_operations()
        await self.assert_original()
        self.assertFalse(evidence[0].exists())

    async def test_malformed_pending_evidence_fails_closed_for_all_readers(self):
        directory = Path(settings.TEMP_DIR) / "media-operations" / "invalid"
        directory.mkdir(parents=True)
        (directory / "operation.json").write_text("{broken")
        with self.assertRaises(MediaRecoveryRequired):
            require_resolved_media("unrelated-media")

    async def test_unsafe_pending_paths_fail_closed_for_unrelated_readers(self):
        directory = Path(settings.TEMP_DIR) / "media-operations" / "unsafe"
        directory.mkdir(parents=True)
        (directory / "operation.json").write_text(json.dumps({"version": 1, "operation_id": "unsafe", "media_id": "m_other", "entries": [{"kind": "local", "path": "/outside-disposable-media", "existed": False, "backup": "asset-0"}]}))
        with self.assertRaises(MediaRecoveryRequired):
            require_resolved_media("unrelated-media")

    async def run_queue(self, *, fail_commit=False, cancel_during_commit=False):
        fixture = self.root / "fixture.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=size=1280x720:rate=4:duration=0.5", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-y", str(fixture)], check=True, capture_output=True, timeout=30)
        async with AsyncSession(self.engine) as db:
            db.add(DownloadTask(id="cloud-queue", tmdb_id=1, media_type="movie", video_url="https://source.example/fixture", quality="2160p", status="DOWNLOADING", created_at="2026-10-13T00:00:00"))
            await db.commit()

        async def download(**kwargs):
            shutil.copy2(fixture, kwargs["output_path"])
            return True, None

        entered, release = asyncio.Event(), asyncio.Event()
        original_commit = AsyncSession.commit

        async def commit(db):
            if any(isinstance(item, MediaOperationReceipt) for item in db.new):
                if fail_commit:
                    raise RuntimeError("injected publication acceptance failure")
                if cancel_during_commit:
                    entered.set()
                    await release.wait()
            await original_commit(db)

        with patch.object(queue, "validate_url", AsyncMock()), patch.object(queue, "probe_media_stream", AsyncMock(return_value={"has_video": True, "has_audio": False, "video_duration": 0.5, "scan_quality": "720p", "failure": None, "video_source_type": "auto", "audio_source_type": "auto"})), patch.object(queue, "download_and_merge", download), patch.object(queue.tmdb_client, "fetch_movie_metadata", AsyncMock(return_value={"title": "Film", "releaseYear": 2026})), patch.object(queue, "download_and_cache_metadata_image", AsyncMock(return_value="")), patch.object(queue, "extract_audio_and_strip_video", AsyncMock(return_value=[])), patch.object(queue.vibe_analysis_manager, "enqueue", AsyncMock()), patch.object(queue.DownloadQueueManager, "_schedule_playback_baseline", AsyncMock()), patch.object(AsyncSession, "commit", commit):
            task = asyncio.create_task(queue.DownloadQueueManager()._process_admitted_task("cloud-queue"))
            if cancel_during_commit:
                await asyncio.wait_for(entered.wait(), timeout=10)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=10)
            else:
                await asyncio.wait_for(task, timeout=10)

    async def test_actual_queue_cloud_acceptance_failure_preserves_healthy_original(self):
        await self.run_queue(fail_commit=True)
        await self.assert_original()
        async with AsyncSession(self.engine) as db:
            task = await db.get(DownloadTask, "cloud-queue")
            self.assertEqual(task.status, "FAILED")

    async def test_actual_queue_cancel_during_successful_commit_preserves_acceptance(self):
        await self.run_queue(cancel_during_commit=True)
        async with AsyncSession(self.engine) as db:
            task = await db.get(DownloadTask, "cloud-queue")
            movie = await db.get(Movie, "m_1")
            self.assertEqual(task.status, "COMPLETED")
            self.assertEqual(movie.availability, "available")
            self.assertEqual(movie.quality, "720p")
            self.assertNotEqual(movie.source_fingerprint, "old-cloud-fingerprint")
            filename = Path(movie.video_url).name
        self.assertTrue((self.target / filename).is_file())
        self.assertEqual(json.loads((self.target / ".metadata/metadata.json").read_text())["quality"], "720p")
        self.assertEqual(list((Path(settings.TEMP_DIR) / "media-operations").glob("*/operation.json")), [])

    async def prepare_local_fallback(self):
        self.original_filename = "Film_2026.mp4"
        (self.target / "film.mp4").rename(self.target / self.original_filename)
        local = Path(settings.MEDIA_DIR) / "Movies" / self.target.name
        shutil.copytree(self.target, local)
        async with AsyncSession(self.engine) as db:
            movie = await db.get(Movie, "m_1")
            movie.video_url = f"/media/Movies/{self.target.name}/{self.original_filename}"
            await db.commit()
        return local

    async def test_actual_queue_success_updates_retained_local_fallback_and_cloud(self):
        local = await self.prepare_local_fallback()
        await self.run_queue()
        async with AsyncSession(self.engine) as db:
            movie = await db.get(Movie, "m_1")
            self.assertEqual((await db.get(DownloadTask, "cloud-queue")).status, "COMPLETED")
            self.assertEqual(movie.quality, "720p")
            filename = Path(movie.video_url).name
            self.assertEqual(movie.source_fingerprint, local_playback_fingerprint(local / filename, movie.audio_metadata))
            self.assertNotEqual(movie.source_fingerprint, "old-cloud-fingerprint")
            fingerprint = movie.source_fingerprint
        self.assertNotEqual((local / filename).read_bytes(), b"healthy-old-cloud-video")
        self.assertEqual((local / filename).read_bytes(), (self.target / filename).read_bytes())
        self.assertEqual(json.loads((local / ".metadata/metadata.json").read_text())["source_fingerprint"], fingerprint)
        self.assertEqual(json.loads((self.target / ".metadata/metadata.json").read_text())["source_fingerprint"], fingerprint)

    async def test_actual_queue_failure_restores_both_local_fallback_and_cloud(self):
        local = await self.prepare_local_fallback()
        await self.run_queue(fail_commit=True)
        await self.assert_original()
        self.assertEqual((local / self.original_filename).read_bytes(), b"healthy-old-cloud-video")
        self.assertEqual(json.loads((local / ".metadata/metadata.json").read_text())["sentinel"], "healthy-old")


if __name__ == "__main__":
    unittest.main()
