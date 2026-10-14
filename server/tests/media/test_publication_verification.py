"""Independent crash/remote rollback regressions; disposable installations only."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from models import Movie
from lifecycle.media_publication import MediaMutation, current_mutation, recover_media_operations
from lifecycle.media_work import MediaRecoveryRequired, media_lock, media_ownership, require_resolved_media
from services import media_updates
from services.media_source import playback_source_fingerprint, resolve_media_source
from services.playback_prep import PlaybackPrepService
from services.rclone import rclone_service


class PublicationVerification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-publication-verification-")
        self.root = Path(self.temporary.name)
        self.media_id = f"m_{uuid.uuid4().hex}"
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
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'verification.db'}")
        self.engine_patch = patch.object(db_module, "engine", self.engine)
        self.engine_patch.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

    async def asyncTearDown(self):
        self.config_patch.stop()
        self.engine_patch.stop()
        await self.engine.dispose()
        settings.MEDIA_DIR, settings.TEMP_DIR, settings.RCLONE_REMOTE_PATH, settings.STORAGE_ENGINE = self.previous
        self.temporary.cleanup()

    async def _cloud_file_rollback(self, *, delete=False):
        if not rclone_service.executable():
            self.skipTest("rclone is unavailable")
        target = self.root / "remote" / "Movies" / "Film" / "subtitle_en.vtt"
        target.parent.mkdir(parents=True)
        original = b"WEBVTT\n\nHealthy previous subtitle"
        target.write_bytes(original)
        remote = f"{settings.RCLONE_REMOTE_PATH}/Movies/Film/subtitle_en.vtt"
        replacement = self.root / "replacement.vtt"
        replacement.write_bytes(b"WEBVTT\n\nUnaccepted new subtitle")
        with self.assertRaisesRegex(RuntimeError, "injected acceptance failure"):
            async with MediaMutation(self.media_id) as mutation:
                await mutation.capture_cloud(remote)
                result = await (rclone_service.run("deletefile", remote) if delete else rclone_service.run("copyto", str(replacement), remote))
                self.assertTrue(result.ok)
                raise RuntimeError("injected acceptance failure")
        self.assertTrue(target.is_file(), "cloud rollback must restore a deleted original")
        self.assertEqual(target.read_bytes(), original, "file-only rollback must restore the actual remote object")
        self.assertEqual(list((Path(settings.TEMP_DIR) / "media-operations").glob("*/operation.json")), [])

    async def test_real_cloud_file_replacement_failure_restores_original(self):
        await self._cloud_file_rollback()

    async def test_real_cloud_file_deletion_failure_restores_original(self):
        await self._cloud_file_rollback(delete=True)

    async def test_completed_compensation_survives_partial_evidence_cleanup(self):
        target = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "subtitle_en.vtt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"original")
        mutation = MediaMutation(self.media_id)
        await mutation.__aenter__()
        mutation.capture_local(str(target))
        target.write_bytes(b"unaccepted")
        real_unlink = Path.unlink

        def interrupted_cleanup(path, *args, **kwargs):
            if path.parent == mutation.root and path.name.startswith("asset-"):
                real_unlink(path, *args, **kwargs)
                raise OSError("injected partial cleanup interruption")
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", interrupted_cleanup):
            with self.assertRaisesRegex(OSError, "partial cleanup"):
                await mutation.__aexit__(None, None, None)
        self.assertEqual(target.read_bytes(), b"original")
        await recover_media_operations()
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(mutation.root.exists())

    async def test_pristine_orphan_evidence_is_not_silently_skipped(self):
        mutation = MediaMutation(self.media_id)
        creating = mutation.root.with_name(f".{mutation.operation_id}.creating")
        real_replace = os.replace

        def failed_manifest_replace(source, destination):
            if Path(destination) == creating / "operation.json":
                raise OSError("injected first manifest interruption")
            return real_replace(source, destination)

        with patch("lifecycle.media_publication.os.replace", failed_manifest_replace):
            with self.assertRaisesRegex(OSError, "first manifest"):
                await mutation.__aenter__()
        self.assertFalse((mutation.root / "operation.json").exists())
        self.assertTrue((creating / "operation.tmp").is_file())
        with self.assertRaises(MediaRecoveryRequired):
            require_resolved_media("unrelated-media")
        # Startup recovery must either resolve a proven pristine operation or
        # refuse readiness; succeeding with globally unplayable media is wrong.
        try:
            await recover_media_operations()
        except RuntimeError:
            return
        require_resolved_media("unrelated-media")

    async def test_nonempty_legacy_orphan_refuses_startup_recovery(self):
        directory = Path(settings.TEMP_DIR) / "media-operations" / "unresolved-legacy"
        directory.mkdir(parents=True)
        (directory / "asset-0").write_bytes(b"potential original media evidence")
        with self.assertRaises(RuntimeError):
            await recover_media_operations()
        self.assertEqual((directory / "asset-0").read_bytes(), b"potential original media evidence")

    async def test_invalid_second_entry_prevents_all_recovery_mutations(self):
        target = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "subtitle_en.vtt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"healthy previous subtitle")
        mutation = MediaMutation(self.media_id)
        await mutation.__aenter__()
        mutation.capture_local(str(target))
        target.write_bytes(b"unaccepted subtitle")
        mutation.entries.append({"kind": "local", "path": str(self.root / "outside-media"), "existed": False, "backup": "asset-1"})
        mutation.persist()
        current_mutation.reset(mutation.token)
        with self.assertRaises((ValueError, RuntimeError)):
            await recover_media_operations()
        self.assertEqual(target.read_bytes(), b"unaccepted subtitle", "the complete recovery plan must be validated before changing any asset")
        self.assertTrue((mutation.root / "asset-0").is_file())

    async def test_background_input_does_not_open_unaccepted_replacement(self):
        target = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "film.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"healthy accepted original")
        source = await resolve_media_source("/media/Movies/Film/film.mp4")
        fingerprint = playback_source_fingerprint(source, [])
        async with AsyncSession(self.engine) as db:
            db.add(self._movie(fingerprint))
            await db.commit()
        staging = Path(settings.TEMP_DIR) / "candidate"
        staging.mkdir()
        (staging / "film.mp4").write_bytes(b"unaccepted replacement")
        service = PlaybackPrepService()
        entered = asyncio.Event()

        async def background_input():
            async with service._owned_job_input(self.media_id, fingerprint, "v_240p", source) as (input_path, process, _descriptors):
                self.assertIsNone(process)
                self.assertEqual(Path(input_path).read_bytes(), b"healthy accepted original", "background playback input must not expose a pending publication")
                entered.set()

        task = None
        async with media_ownership(self.media_id):
            async with MediaMutation(self.media_id) as mutation:
                mutation.publish_directory(str(staging), str(target.parent))
                task = asyncio.create_task(background_input())
                await asyncio.sleep(0.05)
                self.assertFalse(entered.is_set())
        await asyncio.wait_for(task, timeout=3)

    def _movie(self, fingerprint, *, probe=None):
        probe = probe or {}
        return Movie(id=self.media_id, title="Film", description="", thumbnail_url="", banner_url="", video_url="/media/Movies/Film/film.mp4", duration="2s", release_year=2026, source_fingerprint=fingerprint, catalog_source="server", availability="available", quality="240p", codec=probe.get("codec", "h264"), container="mp4", width=320, height=240, frame_rate=12, probed_duration=2)

    async def _video_fixture(self, color, destination):
        executable = shutil.which("ffmpeg")
        if not executable:
            self.skipTest("FFmpeg is unavailable")
        result = subprocess.run([executable, "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=12:duration=2", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-y", str(destination)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    async def test_actual_queued_job_rejects_old_fingerprint_after_acceptance(self):
        target = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "film.mp4"
        target.parent.mkdir(parents=True)
        await self._video_fixture("red", target)
        source = await resolve_media_source("/media/Movies/Film/film.mp4")
        old_fingerprint = playback_source_fingerprint(source, [])
        movie = self._movie(old_fingerprint)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(movie)
            await db.commit()
        staging = Path(settings.TEMP_DIR) / "candidate"
        staging.mkdir()
        await self._video_fixture("blue", staging / "film.mp4")
        service = PlaybackPrepService()
        service.semaphore = asyncio.Semaphore(0)
        rendition = service.baseline_video(movie)
        task = asyncio.create_task(service._transcode_video(self.media_id, old_fingerprint, source, rendition, movie))
        await asyncio.sleep(0)
        async with media_ownership(self.media_id):
            async with MediaMutation(self.media_id) as mutation:
                mutation.publish_directory(str(staging), str(target.parent))
                service.semaphore.release()
                await asyncio.sleep(0.05)
                self.assertFalse(task.done(), "capacity admission must still wait for the current media owner")
                self.assertFalse(service.playlist_ready(self.media_id, old_fingerprint, rendition.name))
                current_source = await resolve_media_source("/media/Movies/Film/film.mp4")
                async with AsyncSession(self.engine) as db:
                    current = await db.get(Movie, self.media_id)
                    current.source_fingerprint = playback_source_fingerprint(current_source, [])
                    await mutation.commit(db)
        await asyncio.wait_for(task, timeout=15)
        self.assertFalse(service.playlist_ready(self.media_id, old_fingerprint, rendition.name), "an obsolete job must never publish the new video under the old fingerprint")
        self.assertEqual(service.rendition_error(self.media_id, old_fingerprint, rendition.name)["code"], "PLAYBACK_SOURCE_CHANGED")

    async def test_catalog_reconciliation_cannot_accept_pending_replacement_identity(self):
        target = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "film.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"healthy accepted original")
        source = await resolve_media_source("/media/Movies/Film/film.mp4")
        fingerprint = playback_source_fingerprint(source, [])
        async with AsyncSession(self.engine) as db:
            db.add(self._movie(fingerprint))
            await db.commit()
        staging = Path(settings.TEMP_DIR) / "candidate"
        staging.mkdir()
        (staging / "film.mp4").write_bytes(b"unaccepted replacement")
        service = PlaybackPrepService()
        async with media_ownership(self.media_id):
            async with MediaMutation(self.media_id) as mutation:
                mutation.publish_directory(str(staging), str(target.parent))
                task = asyncio.create_task(service.reconcile_catalog_cache_identities())
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                async with AsyncSession(self.engine) as db:
                    self.assertEqual((await db.get(Movie, self.media_id)).source_fingerprint, fingerprint)
        await asyncio.wait_for(task, timeout=3)
        async with AsyncSession(self.engine) as db:
            self.assertEqual((await db.get(Movie, self.media_id)).source_fingerprint, fingerprint)
        self.assertEqual(target.read_bytes(), b"healthy accepted original")

    async def test_actual_ffmpeg_reads_pinned_original_after_replacement(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("Linux inherited descriptor path is unavailable")
        target = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "film.mp4"
        target.parent.mkdir(parents=True)
        await self._video_fixture("red", target)
        source = await resolve_media_source("/media/Movies/Film/film.mp4")
        fingerprint = playback_source_fingerprint(source, [])
        async with AsyncSession(self.engine) as db:
            db.add(self._movie(fingerprint))
            await db.commit()
        staging = Path(settings.TEMP_DIR) / "candidate"
        staging.mkdir()
        await self._video_fixture("blue", staging / "film.mp4")
        service = PlaybackPrepService()
        async with service._owned_job_input(self.media_id, fingerprint, "v_240p", source) as (input_path, process, descriptors):
            self.assertIsNone(process)
            async with media_ownership(self.media_id):
                async with MediaMutation(self.media_id) as mutation:
                    mutation.publish_directory(str(staging), str(target.parent))
                    # Read after pathname publication. The inherited descriptor
                    # must continue to identify the original red frame.
                    result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", input_path, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "pipe:1"], capture_output=True, pass_fds=descriptors, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    self.assertGreater(sum(result.stdout[0::3]), sum(result.stdout[2::3]) * 10)

    async def test_actual_background_job_publishes_verified_hls_from_pinned_input(self):
        target = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "film.mp4"
        target.parent.mkdir(parents=True)
        await self._video_fixture("red", target)
        source = await resolve_media_source("/media/Movies/Film/film.mp4")
        fingerprint = playback_source_fingerprint(source, [])
        movie = self._movie(fingerprint)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(movie)
            await db.commit()
        service = PlaybackPrepService()
        rendition = service.baseline_video(movie)
        await asyncio.wait_for(service._transcode_video(self.media_id, fingerprint, source, rendition, movie), timeout=20)
        self.assertIsNone(service.rendition_error(self.media_id, fingerprint, rendition.name))
        self.assertTrue(service.rendition_verified(self.media_id, fingerprint, rendition.name))
        self.assertTrue(service.playback_ready(self.media_id, fingerprint, movie))

    async def test_cancelled_owned_input_closes_descriptor_and_releases_lock(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("Linux inherited descriptor path is unavailable")
        target = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "film.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"accepted video")
        source = await resolve_media_source("/media/Movies/Film/film.mp4")
        fingerprint = playback_source_fingerprint(source, [])
        async with AsyncSession(self.engine) as db:
            db.add(self._movie(fingerprint))
            await db.commit()
        service = PlaybackPrepService()
        entered = asyncio.Event()
        descriptors = []

        async def input_job():
            async with service._owned_job_input(self.media_id, fingerprint, "video_original", source) as (_path, _process, inherited):
                descriptors.extend(inherited)
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(input_job())
        await asyncio.wait_for(entered.wait(), timeout=3)
        self.assertEqual(len(descriptors), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertFalse(media_lock(self.media_id).locked())

    async def test_cloud_spool_remains_original_after_remote_publication(self):
        if not rclone_service.executable():
            self.skipTest("rclone is unavailable")
        target = self.root / "remote" / "Movies" / "Film" / "film.mp4"
        target.parent.mkdir(parents=True)
        await self._video_fixture("red", target)
        original = target.read_bytes()
        source = await resolve_media_source("/media/Movies/Film/film.mp4")
        fingerprint = playback_source_fingerprint(source, [])
        async with AsyncSession(self.engine) as db:
            db.add(self._movie(fingerprint))
            await db.commit()
        staging = Path(settings.TEMP_DIR) / "candidate"
        staging.mkdir()
        await self._video_fixture("blue", staging / "film.mp4")
        service = PlaybackPrepService()
        async with service._owned_job_input(self.media_id, fingerprint, "video_original", source) as (input_path, process, descriptors):
            self.assertIsNone(process)
            self.assertEqual(descriptors, ())
            self.assertEqual(Path(input_path).read_bytes(), original)
            async with media_ownership(self.media_id):
                async with MediaMutation(self.media_id) as mutation:
                    await mutation.publish_cloud_directory(str(staging), "Movies/Film")
                    self.assertNotEqual(target.read_bytes(), original)
                    self.assertEqual(Path(input_path).read_bytes(), original, "cloud job must read its immutable verified spool after unlocking publication")

    async def test_first_manifest_syncs_ancestor_directories_before_publication(self):
        mutation = MediaMutation(self.media_id)
        synchronized = []
        real_fsync = os.fsync

        def fsync(fd):
            synchronized.append(Path(os.readlink(f"/proc/self/fd/{fd}")).resolve())
            return real_fsync(fd)

        with patch("lifecycle.media_publication.os.fsync", fsync):
            async with mutation:
                self.assertIn(mutation.root.parent, synchronized, "operation entry must survive power loss before any file mutation")
                self.assertIn(Path(settings.TEMP_DIR), synchronized, "new evidence-root entry must survive power loss")

    async def test_accepted_new_asset_syncs_bytes_and_parent(self):
        video = Path(settings.MEDIA_DIR) / "Movies" / "Film" / "film.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"accepted video")
        source = await resolve_media_source("/media/Movies/Film/film.mp4")
        prepared = Path(settings.TEMP_DIR) / "subtitle.vtt"
        prepared.write_bytes(b"WEBVTT\n\nNew accepted subtitle")
        synchronized = []
        real_fsync = os.fsync

        def fsync(fd):
            info = os.fstat(fd)
            synchronized.append((info.st_dev, info.st_ino))
            return real_fsync(fd)

        with patch("lifecycle.media_publication.os.fsync", fsync):
            async with MediaMutation(self.media_id) as mutation:
                destination = media_updates.install_local_asset(source, "subtitle_en.vtt", prepared)
                async with AsyncSession(self.engine) as db:
                    await mutation.commit(db)
                self.assertIn((destination.stat().st_dev, destination.stat().st_ino), synchronized, "SQLite receipt cannot precede durable media bytes")
                self.assertIn((destination.parent.stat().st_dev, destination.parent.stat().st_ino), synchronized, "SQLite receipt cannot precede durable asset rename")


if __name__ == "__main__":
    unittest.main()
