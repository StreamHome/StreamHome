"""Cache retention through native preparation and interrupted lifecycle paths.

Run with server/tests/run_isolated.py; all assets are disposable synthetic bytes.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from config import settings
from lifecycle.coordination import run_cancellation_safe_thread
from services.ingest_preview import IngestPreviewError, IngestPreviewService
from services.media_source import ResolvedMediaSource
from services.playback_prep import (
    COMPLETE_MARKER, PLAYLIST_NAME, RETAINED_INVALID_MARKER,
    VIDEO_VERIFIED_MARKER, PlaybackPreparationError, PlaybackPrepService,
)
from services.rclone import rclone_service
from services.storage_capacity import storage_capacity


def contents(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class CacheRetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="retained-cache-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.service = PlaybackPrepService()
        self.service.cache_dir = self.root / "playback_cache"
        self.service.initialize_storage()

    def rendition(self, *, accepted=False, fingerprint="abcd") -> Path:
        target = self.service.cache_path("media", fingerprint) / "video_original"
        target.mkdir(parents=True, exist_ok=True)
        (target / PLAYLIST_NAME).write_text(
            '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4,\nsegment_00000.m4s\n#EXT-X-ENDLIST\n')
        (target / "init.mp4").write_bytes(b"retained init")
        (target / "segment_00000.m4s").write_bytes(b"retained segment")
        (target / "captions.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nRetained\n")
        if accepted:
            (target / COMPLETE_MARKER).write_text("1")
            (target / VIDEO_VERIFIED_MARKER).write_text("1")
        return target

    async def test_startup_lru_and_expired_lease_retain_all_bytes(self):
        accepted = self.rendition(accepted=True)
        legacy = self.rendition(fingerprint="beef")
        unknown = self.service.cache_dir / "legacy.tmp"
        unknown.write_bytes(b"ambiguous old cache")
        (legacy / ".copy.tmp").write_bytes(b"possibly accepted subtitle")
        original = contents(self.service.cache_dir)
        self.service.recover_interrupted_outputs()
        self.service.release_run("previously-paused")
        with patch.object(settings, "PLAYBACK_CACHE_GB", 1 / 1024**3):
            self.service.enforce_lru_limits({str(accepted.parent), str(legacy.parent)})
            await self.service.enforce_lru_limits_admitted()
        for name, digest in original.items():
            self.assertEqual(hashlib.sha256((self.service.cache_dir / name).read_bytes()).hexdigest(), digest)
        self.assertTrue(self.service.rendition_complete("media", "abcd", accepted.name))
        self.assertFalse(self.service.playlist_ready("media", "beef", legacy.name))
        self.assertTrue((legacy / RETAINED_INVALID_MARKER).is_file())

    async def test_retry_archives_ambiguous_generation_and_does_not_expose_it(self):
        old = self.rendition()
        before = contents(old)
        current = self.service._start_rendition_candidate("media", "abcd", old.name)
        archives = list(current.parent.glob(f".{old.name}.retained-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(contents(archives[0]), before)
        self.assertFalse(list(current.iterdir()))
        self.assertFalse(self.service.retained_asset_available("media", "abcd", f"{archives[0].name}/segment_00000.m4s"))
        self.assertFalse(self.service.retained_asset_available("media", "abcd", ".sources/private/original.mp4"))
        self.assertFalse(self.service.retained_asset_available("media", "abcd", "../original.mp4"))

    async def test_open_paused_generation_is_never_replaced_on_retry(self):
        target = self.rendition()
        original = contents(target)
        self.service.protect_rendition("paused-run", "media", "abcd", target.name)
        with self.assertRaises(PlaybackPreparationError) as failure:
            self.service._start_rendition_candidate("media", "abcd", target.name)
        self.assertEqual(failure.exception.code, "RETAINED_CACHE_IN_USE")
        self.assertEqual(contents(target), original)
        self.assertFalse(list(target.parent.glob("*.retained-*")))
        self.service.release_run("paused-run")
        self.assertNotIn("paused-run", self.service._run_cache_renditions)
        self.service._rendition_cache_leases[str(target)] = time.monotonic() + 300
        with self.assertRaises(PlaybackPreparationError):
            self.service._start_rendition_candidate("media", "abcd", target.name)
        self.assertEqual(contents(target), original)

    async def test_new_viewer_can_repair_unserved_failed_cache_while_healthy_quality_is_paused(self):
        target = self.rendition()
        original = contents(target)
        self.service._invalidate_retained_rendition(target)
        healthy = target.parent / "video_480p"
        shutil.copytree(target, healthy)
        (healthy / RETAINED_INVALID_MARKER).unlink()
        self.service._accept_rendition(healthy, healthy.name)
        healthy_bytes = contents(healthy)
        self.service.protect_rendition("healthy-paused-viewer", "media", "abcd", healthy.name)
        self.service._rendition_cache_leases[str(healthy)] = time.monotonic() - 600
        self.service.protect_run("healthy-paused-viewer", "media", "abcd")
        self.assertIn(healthy.name, self.service._run_cache_renditions["healthy-paused-viewer"])
        self.service.protect_run("new-viewer", "media", "abcd")
        self.service.touch("media", "abcd")
        # Readiness polling may protect the root, but never claims invalid bytes.
        self.service.protect_run("new-viewer", "media", "abcd")
        candidate = self.service._start_rendition_candidate("media", "abcd", target.name)
        archives = list(candidate.parent.glob(f".{target.name}.retained-*"))
        self.assertEqual(len(archives), 1)
        for name, digest in original.items():
            self.assertEqual(hashlib.sha256((archives[0] / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(contents(healthy), healthy_bytes)
        self.assertFalse(list(candidate.iterdir()))
        self.assertNotIn(target.name, self.service._run_cache_renditions["new-viewer"])
        # An exposed generation remains protected after the short lease expires.
        with self.assertRaises(PlaybackPreparationError):
            self.service._start_rendition_candidate("media", "abcd", healthy.name)

    async def test_new_viewer_repairs_unserved_corrupt_cache_with_accepted_markers(self):
        target = self.rendition(accepted=True)
        (target / "segment_00000.m4s").unlink()
        original = contents(target)
        self.assertFalse(self.service.rendition_complete("media", "abcd", target.name))
        self.service.protect_run("new-viewer", "media", "abcd")
        self.service.protect_run("new-viewer", "media", "abcd")
        self.assertFalse(self.service._run_cache_renditions["new-viewer"])
        candidate = self.service._start_rendition_candidate("media", "abcd", target.name)
        archives = list(candidate.parent.glob(f".{target.name}.retained-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(contents(archives[0]), original)
        self.assertFalse(list(candidate.iterdir()))

    async def test_restarted_active_legacy_run_stays_bound_until_explicit_close(self):
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlmodel import SQLModel
        from sqlmodel.ext.asyncio.session import AsyncSession
        from models import PlaybackRun

        target = self.rendition()
        self.service._invalidate_retained_rendition(target)
        original = contents(target)
        engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'restart.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with AsyncSession(engine) as db:
                db.add(PlaybackRun(id="pre-start-paused", profile_id="synthetic", movie_id="media", source_fingerprint="abcd"))
                await db.commit()
            with patch("db.engine", engine):
                await self.service.reconcile_run_protections()
            self.service.protect_run("new-viewer", "media", "abcd")
            with self.assertRaises(PlaybackPreparationError):
                self.service._start_rendition_candidate("media", "abcd", target.name)
            self.assertEqual(contents(target), original)
            self.service.release_run("pre-start-paused")
            self.assertNotIn("pre-start-paused", self.service._run_cache_renditions)
            self.service._start_rendition_candidate("media", "abcd", target.name)
        finally:
            await engine.dispose()

    async def test_post_acceptance_cancellation_does_not_invalidate_verified_bytes(self):
        target = self.rendition()
        self.service._accept_rendition(target, target.name)
        original = contents(target)
        # The awaiting job may be cancelled after its worker has accepted output.
        self.service._invalidate_retained_rendition(target)
        self.assertEqual(contents(target), original)
        self.assertTrue(self.service.retained_asset_available("media", "abcd", f"{target.name}/segment_00000.m4s"))

    async def test_cancellation_during_durable_acceptance_preserves_completed_generation(self):
        target = self.rendition()
        accepted = threading.Event()
        release = threading.Event()

        def accept_then_wait():
            self.service._accept_rendition(target, target.name)
            accepted.set()
            self.assertTrue(release.wait(5))

        async def job():
            try:
                await run_cancellation_safe_thread(accept_then_wait)
            except asyncio.CancelledError:
                self.service._invalidate_retained_rendition(target)
                raise

        task = asyncio.create_task(job())
        try:
            for _ in range(100):
                if accepted.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(accepted.is_set())
            task.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.service.rendition_complete("media", "abcd", target.name))
        self.assertFalse((target / RETAINED_INVALID_MARKER).exists())

    async def test_segment_eligibility_is_bounded_and_rejects_invalid_or_missing_receipts(self):
        target = self.rendition(accepted=True)
        path = f"{target.name}/segment_00000.m4s"
        with patch.object(self.service, "_read_playlist_readiness", side_effect=AssertionError("Segment delivery must not scan the playlist")):
            self.assertTrue(self.service.retained_asset_available("media", "abcd", path))
            (target / RETAINED_INVALID_MARKER).write_text("unaccepted")
            self.assertFalse(self.service.retained_asset_available("media", "abcd", path))
            (target / RETAINED_INVALID_MARKER).unlink()
            (target / VIDEO_VERIFIED_MARKER).unlink()
            self.assertFalse(self.service.retained_asset_available("media", "abcd", path))
            self.service.running_jobs.add("media:abcd:video_original")
            self.assertTrue(self.service.retained_asset_available("media", "abcd", path))
            (target / RETAINED_INVALID_MARKER).write_text("unaccepted")
            self.assertFalse(self.service.retained_asset_available("media", "abcd", path))
            self.assertFalse(self.service.retained_asset_available("media", "abcd", "/video_original/segment_00000.m4s"))
        self.service.running_jobs.clear()

    async def test_preview_reset_remove_and_ttl_never_delete_completed_or_legacy_bytes(self):
        preview = IngestPreviewService()
        preview.root = self.root / "ingest_preview"
        preview.initialize_storage()
        for task_id, complete in (("retained-complete", True), ("retained-legacy", False)):
            target = preview.prepare(task_id, 4)
            (target / "segment_00000.m4s").write_bytes(b"accepted or ambiguous preview")
            (target / "subtitle.vtt").write_text("WEBVTT\n")
            if complete:
                preview.mark_complete(task_id)
            original = contents(target)
            os.utime(target, (1, 1))
            preview.cleanup_expired()
            preview.remove(task_id)
            with self.assertRaises(IngestPreviewError):
                preview.prepare(task_id, 8)
            self.assertEqual(contents(target), original)

    async def test_wrong_size_spool_and_legacy_partials_survive_failed_copy(self):
        source = ResolvedMediaSource("/media/Movies/synthetic/video.mp4", "Movies/synthetic/video.mp4",
                                    self.root / "missing.mp4", "synthetic:source.mp4", False, True,
                                    cloud_size=100)
        source_key = hashlib.sha256(source.cloud_path.encode()).hexdigest()[:24]
        spool = self.service.cache_path("media", "abcd") / ".sources" / source_key / "original.mp4"
        spool.parent.mkdir(parents=True)
        spool.write_bytes(b"previous accepted or ambiguous source")
        partial = spool.parent / ".original.mp4.legacy.partial"
        partial.write_bytes(b"legacy subtitle or partial")
        old_bytes = spool.read_bytes()

        @asynccontextmanager
        async def allocation(*args, **kwargs):
            yield None

        async def copy(source_path, target_path, **kwargs):
            Path(target_path).write_bytes(b"failed current copy")
            return SimpleNamespace(ok=False, error_code="INJECTED_COPY_FAILURE")

        with patch.object(storage_capacity, "allocation", allocation), \
             patch.object(storage_capacity, "progress", AsyncMock()), \
             patch.object(rclone_service, "executable", return_value="rclone"), \
             patch.object(rclone_service, "cloud_write_available", return_value=True), \
             patch.object(rclone_service, "copyto_atomic", side_effect=copy):
            with self.assertRaises(PlaybackPreparationError):
                await self.service._input_process(source, "media", "abcd")
        self.assertEqual(partial.read_bytes(), b"legacy subtitle or partial")
        self.assertFalse(spool.exists())
        self.assertEqual([path.read_bytes() for path in spool.parent.glob(".spool-*/original.mp4")], [b"failed current copy"])
        self.assertEqual([path.read_bytes() for path in spool.parent.glob(".original.mp4.retained-*")], [old_bytes])

    async def test_unknown_size_legacy_spool_is_retained_but_never_served(self):
        source = ResolvedMediaSource("/media/Movies/synthetic/video.mp4", "Movies/synthetic/video.mp4",
                                    self.root / "missing.mp4", "synthetic:source.mp4", False, True,
                                    cloud_size=None)
        source_key = hashlib.sha256(source.cloud_path.encode()).hexdigest()[:24]
        spool = self.service.cache_path("media", "abcd") / ".sources" / source_key / "original.mp4"
        spool.parent.mkdir(parents=True)
        spool.write_bytes(b"ambiguous previous download")
        with patch.object(rclone_service, "executable", return_value="rclone"), \
             patch.object(rclone_service, "cloud_write_available", return_value=True), \
             patch.object(rclone_service, "copyto_atomic", AsyncMock()) as copy:
            with self.assertRaises(PlaybackPreparationError) as failure:
                await self.service._input_process(source, "media", "abcd")
        self.assertEqual(failure.exception.code, "CLOUD_SIZE_INVALID")
        self.assertEqual(spool.read_bytes(), b"ambiguous previous download")
        copy.assert_not_awaited()

    async def test_successful_new_spool_does_not_count_retained_previous_generations(self):
        source = ResolvedMediaSource("/media/Movies/synthetic/video.mp4", "Movies/synthetic/video.mp4",
                                    self.root / "missing.mp4", "synthetic:source.mp4", False, True,
                                    cloud_size=100)
        source_key = hashlib.sha256(source.cloud_path.encode()).hexdigest()[:24]
        spool = self.service.cache_path("media", "abcd") / ".sources" / source_key / "original.mp4"
        spool.parent.mkdir(parents=True)
        spool.write_bytes(b"old" * 100)
        (spool.parent / ".original.mp4.legacy.partial").write_bytes(b"legacy" * 100)

        @asynccontextmanager
        async def allocation(*args, **kwargs):
            yield None

        async def copy(source_path, target_path, **kwargs):
            Path(target_path).write_bytes(b"n" * 100)
            return SimpleNamespace(ok=True)

        with patch.object(storage_capacity, "allocation", allocation), \
             patch.object(storage_capacity, "progress", AsyncMock()), \
             patch.object(rclone_service, "executable", return_value="rclone"), \
             patch.object(rclone_service, "cloud_write_available", return_value=True), \
             patch.object(rclone_service, "copyto_atomic", side_effect=copy):
            result, process = await self.service._input_process(source, "media", "abcd")
        self.assertEqual(Path(result).read_bytes(), b"n" * 100)
        self.assertIsNone(process)
        self.assertEqual([path.read_bytes() for path in spool.parent.glob(".original.mp4.retained-*")], [b"old" * 100])
        self.assertEqual((spool.parent / ".original.mp4.legacy.partial").read_bytes(), b"legacy" * 100)

    async def native_job(self, *, post_failure=False, verification_failure=False, cancel_after_accept=False):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("native FFmpeg tools unavailable")
        video = self.root / "source.mp4"
        generated = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                                    "color=c=blue:s=160x90:r=10:d=1", "-c:v", "libx264", "-preset", "ultrafast",
                                    "-threads", "1", "-an", str(video)], capture_output=True, timeout=20)
        self.assertEqual(generated.returncode, 0, generated.stderr.decode())
        source = ResolvedMediaSource("/media/Movies/synthetic/source.mp4", "Movies/synthetic/source.mp4",
                                    video, None, True, False)
        media = SimpleNamespace(width=160, height=90, frame_rate=10, quality="90p", codec="h264",
                                audio_metadata=[], languages=[], probed_duration=1)
        target = self.service.cache_path("media", "abcd") / "video_original"
        rebuild_calls = 0

        @asynccontextmanager
        async def allocation(*args, **kwargs):
            yield None

        @asynccontextmanager
        async def owned(*args, **kwargs):
            yield str(video), None, ()

        async def rebuild(*args, **kwargs):
            nonlocal rebuild_calls
            rebuild_calls += 1
            if cancel_after_accept and rebuild_calls == 1:
                raise asyncio.CancelledError
            if post_failure and rebuild_calls == 1:
                raise RuntimeError("Injected post-acceptance master failure")

        with patch.object(storage_capacity, "allocation", allocation), \
             patch.object(self.service, "_owned_job_input", owned), \
             patch.object(self.service, "rebuild_master", side_effect=rebuild), \
             patch.object(self.service, "_publish_when_streamable", AsyncMock()):
            if verification_failure:
                with patch.object(self.service, "_verify_rendition_output", side_effect=PlaybackPreparationError("INJECTED_VERIFY", "Rejected synthetic output")):
                    await self.service._run_admitted_ffmpeg_job("media", "abcd", "video_original", source,
                                                              ["-map", "0:v:0", "-an", "-c:v", "copy"], media)
            elif cancel_after_accept:
                with self.assertRaises(asyncio.CancelledError):
                    await self.service._run_admitted_ffmpeg_job("media", "abcd", "video_original", source,
                                                              ["-map", "0:v:0", "-an", "-c:v", "copy"], media)
            else:
                await self.service._run_admitted_ffmpeg_job("media", "abcd", "video_original", source,
                                                          ["-map", "0:v:0", "-an", "-c:v", "copy"], media)
        self.assertFalse(self.service.running_jobs)
        self.assertFalse(self.service.job_started_at)
        self.assertTrue((target / "segment_00000.m4s").is_file())
        return target

    async def test_native_verified_rendition_survives_later_master_failure(self):
        target = await self.native_job(post_failure=True)
        self.assertTrue(self.service.rendition_complete("media", "abcd", target.name))
        self.assertFalse((target / RETAINED_INVALID_MARKER).exists())

    async def test_native_verified_rendition_survives_post_acceptance_cancel(self):
        target = await self.native_job(cancel_after_accept=True)
        self.assertTrue(self.service.rendition_complete("media", "abcd", target.name))
        self.assertFalse((target / RETAINED_INVALID_MARKER).exists())

    async def test_native_unverified_rendition_is_retained_and_unavailable(self):
        target = await self.native_job(verification_failure=True)
        self.assertTrue((target / RETAINED_INVALID_MARKER).is_file())
        self.assertFalse(self.service.playlist_ready("media", "abcd", target.name))
        self.assertFalse(self.service.retained_asset_available("media", "abcd", f"{target.name}/segment_00000.m4s"))
