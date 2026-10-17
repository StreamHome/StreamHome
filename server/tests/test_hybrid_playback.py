"""Hybrid placement/playback regressions; run only through run_isolated.py."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
import uvicorn

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import AuthSession, Movie, Profile, StorageKeepLocal, StorageReservation, User
from routes import playback
from routes.auth import get_current_user
from services.media_probe import probe_completed_media
from services.media_source import (clear_cloud_object_cache, cloud_object_info, local_media_identity,
                                   playback_source_fingerprint, resolve_media_source)
from services.media_updates import mixed_audio_metadata
from services.playback_prep import PlaybackPrepService
from services.playback_source import source_reader
from services.rclone import rclone_service
from services import queue as queue_service
from services import media_updates
from services import ffmpeg as ffmpeg_service
from services.tmdb import TMDBClient
from services.storage_capacity import StorageCapacityError
from lifecycle.media_publication import MediaMutation
import httpx
from services.storage_inventory import adopt_asset, persist_placement, target_generation


class HybridPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not rclone_service.executable():
            self.skipTest("local rclone unavailable")
        self.directory = tempfile.TemporaryDirectory(prefix="hybrid-playback-")
        self.root = Path(self.directory.name)
        self.media = self.root / "server/media"
        self.temp = self.root / "server/temp"
        self.remote = self.root / "remote"
        for path in (self.media, self.temp, self.remote):
            path.mkdir(parents=True)
        self.config = self.root / "rclone.conf"
        self.config.write_text("[isolated]\ntype = local\n")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'database.db'}")
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.stack = ExitStack()
        for name, value in {"BASE_DIR": str(self.root), "MEDIA_DIR": str(self.media), "TEMP_DIR": str(self.temp),
                            "STORAGE_ENGINE": "LOCAL", "RCLONE_REMOTE_PATH": f"isolated:{self.remote}",
                            "STORAGE_POLICY": {"revision": 1, "offload_enabled": False,
                                               "local_budget_bytes": 100_000_000, "cloud_budget_bytes": 100_000_000}}.items():
            self.stack.enter_context(patch.object(settings, name, value))
        self.stack.enter_context(patch("db.engine", self.engine))
        self.stack.enter_context(patch.object(queue_service, "engine", self.engine))
        self.stack.enter_context(patch.object(rclone_service, "config_path", self.config))
        self.service = PlaybackPrepService()
        self.service.initialize_storage()
        self.catalog = "/media/Movies/Hybrid/video.mkv"
        self.video = self.media / "Movies/Hybrid/video.mkv"
        self.video.parent.mkdir(parents=True)
        clear_cloud_object_cache()

    async def asyncTearDown(self):
        await self.service.shutdown(timeout=15)
        self.stack.close()
        clear_cloud_object_cache()
        await self.engine.dispose()
        self.directory.cleanup()

    async def placement(self, catalog, *, audio=None, fingerprint=None, video_fingerprint=None, media_id="m_hybrid"):
        asset = await adopt_asset(media_id, None, catalog,
                                  kind="video" if catalog == self.catalog else "audio",
                                  source_fingerprint=fingerprint, video_fingerprint=video_fingerprint)
        local = self.media / catalog.removeprefix("/media/")
        identity = local_media_identity(local)
        destination = self.remote / ".storage-assets" / asset.id / asset.generation / local.name
        destination.parent.mkdir(parents=True)
        shutil.copy2(local, destination)
        asset.remote_path = f"isolated:{destination}"
        info = await cloud_object_info(asset.remote_path, refresh=True)
        self.assertIsNotNone(info)
        asset.remote_identity = info.identity
        asset.target_generation = target_generation()
        asset.verified_at = time.time()
        persist_placement(asset, identity, audio_metadata=audio)
        return asset

    async def test_reads_work_with_local_engine_and_paused_offload(self):
        remote = self.remote / "Movies/Hybrid/video.mkv"
        remote.parent.mkdir(parents=True)
        remote.write_bytes(b"accepted-cloud-video")
        source = await resolve_media_source(self.catalog)
        self.assertFalse(source.local_exists)
        self.assertTrue(source.cloud_exists)
        reader = source_reader(source, loopback_url="http://127.0.0.1:1")
        self.assertEqual(b"".join([chunk async for chunk in reader.open_range(9, 5)]), b"cloud")

    async def test_video_and_audio_resolve_independently_and_missing_audio_is_not_invented(self):
        self.video.write_bytes(b"local-video")
        remote_audio = self.remote / "Movies/Hybrid/audio/tr.m4a"
        remote_audio.parent.mkdir(parents=True)
        remote_audio.write_bytes(b"remote-audio")
        source = await resolve_media_source(self.catalog)
        audio = SimpleNamespace(source="external", file_name="tr.m4a")
        selected = await self.service._external_audio_source(source, audio, None)
        self.assertTrue(selected.cloud_exists)
        self.assertEqual(selected.cloud_size, 12)
        missing = await self.service._external_audio_source(source, SimpleNamespace(source="external", file_name="missing.m4a"), None)
        self.assertIsNone(missing)
        self.video.unlink()
        remote_video = self.remote / "Movies/Hybrid/video.mkv"
        remote_video.write_bytes(b"cloud-video")
        local_audio = self.video.parent / "audio/tr.m4a"
        local_audio.parent.mkdir()
        local_audio.write_bytes(b"local-audio")
        source = await resolve_media_source(self.catalog)
        selected = await self.service._external_audio_source(source, audio, local_audio)
        self.assertTrue(selected.local_exists)
        self.assertFalse(selected.cloud_exists)

    async def test_accepted_video_relocation_keeps_final_playback_identity_and_replacement_invalidates(self):
        self.video.write_bytes(b"accepted-video")
        before = await resolve_media_source(self.catalog)
        fingerprint = playback_source_fingerprint(before, [])
        video_fingerprint = before.video_fingerprint
        asset = await self.placement(self.catalog, fingerprint=fingerprint, video_fingerprint=video_fingerprint)
        self.video.unlink()
        moved = await resolve_media_source(self.catalog, refresh_cloud=True)
        self.assertEqual(moved.cloud_path, asset.remote_path)
        self.assertEqual(moved.video_fingerprint, video_fingerprint)
        self.assertEqual(playback_source_fingerprint(moved, []), fingerprint)
        Path(asset.remote_path.removeprefix("isolated:")).write_bytes(b"different-video-generation")
        changed = await resolve_media_source(self.catalog, refresh_cloud=True)
        self.assertNotEqual(playback_source_fingerprint(changed, []), fingerprint)

    async def test_startup_preserves_generation_addressed_catalog_with_offload_paused_and_provider_unknown(self):
        self.video.write_bytes(b"accepted-video")
        source = await resolve_media_source(self.catalog)
        fingerprint = playback_source_fingerprint(source, [])
        await self.placement(self.catalog, fingerprint=fingerprint, video_fingerprint=source.video_fingerprint)
        self.video.unlink()
        async with AsyncSession(self.engine) as db:
            db.add(Movie(id="m_hybrid", title="Existing cloud catalog", description="", thumbnail_url="", release_year=2026,
                         video_url=self.catalog, duration="2s", availability="available", source_fingerprint=fingerprint))
            await db.commit()
        manager = queue_service.DownloadQueueManager()
        await manager.sync_media_from_disk(raise_on_critical_failure=True)
        async with AsyncSession(self.engine) as db:
            self.assertEqual((await db.get(Movie, "m_hybrid")).availability, "available")
        self.config.unlink()
        await manager.sync_media_from_disk(raise_on_critical_failure=True)
        async with AsyncSession(self.engine) as db:
            movie = await db.get(Movie, "m_hybrid")
            self.assertEqual(movie.availability, "available")
            self.assertEqual(movie.source_fingerprint, fingerprint)

    async def test_audio_only_relocation_preserves_bundle_and_real_changes_invalidate(self):
        self.video.write_bytes(b"accepted-video")
        audio = self.video.parent / "audio/tr.m4a"
        audio.parent.mkdir()
        audio.write_bytes(b"accepted-audio")
        metadata = [{"source": "external", "fileName": audio.name, "fileSize": audio.stat().st_size,
                     "modifiedAt": audio.stat().st_mtime_ns}]
        before = await resolve_media_source(self.catalog)
        fingerprint = playback_source_fingerprint(before, metadata)
        asset = await self.placement(self.catalog.rsplit("/", 1)[0] + "/audio/tr.m4a")
        audio.unlink()
        moved = await resolve_media_source(self.catalog, refresh_cloud=True)
        self.assertEqual(playback_source_fingerprint(moved, metadata), fingerprint)
        self.assertEqual(moved.video_fingerprint, before.video_fingerprint)
        Path(asset.remote_path.removeprefix("isolated:")).write_bytes(b"changed-remote-audio")
        changed = await resolve_media_source(self.catalog, refresh_cloud=True)
        self.assertNotEqual(playback_source_fingerprint(changed, metadata), fingerprint)
        audio.write_bytes(b"changed-local-audio")
        replaced = await resolve_media_source(self.catalog)
        self.assertNotEqual(playback_source_fingerprint(replaced, metadata), fingerprint)

    async def test_full_budget_denies_sidecar_download_install_and_compensation_before_copy(self):
        self.video.write_bytes(b"accepted-video")
        source = await resolve_media_source(self.catalog)
        existing = self.video.parent / "audio/tr.m4a"
        existing.parent.mkdir()
        existing.write_bytes(b"accepted-audio")
        prepared = self.temp / "new-audio.m4a"
        prepared.write_bytes(b"replacement" * 100)
        download = self.temp / "download.m4a"
        response = httpx.Response(200, content=b"new-audio", request=httpx.Request("GET", "http://fixture.invalid/audio"))
        with patch.object(settings, "STORAGE_POLICY", {"local_budget_bytes": 1, "offload_enabled": False}):
            with patch.object(media_updates, "validated_stream_request", return_value=response):
                with self.assertRaises(StorageCapacityError):
                    await media_updates.download_remote_asset("http://fixture.invalid/audio", download, headers={}, client_address="", maximum_bytes=1024)
            self.assertFalse(download.exists())
            with self.assertRaises(StorageCapacityError):
                async with MediaMutation("m_hybrid"):
                    await media_updates.install_asset(source, "audio/tr.m4a", prepared)
            with self.assertRaises(StorageCapacityError):
                async with MediaMutation("m_hybrid") as mutation:
                    await mutation.capture_local_asset(str(self.video))
        self.assertEqual(existing.read_bytes(), b"accepted-audio")
        self.assertEqual(self.video.read_bytes(), b"accepted-video")
        self.assertFalse(list(self.temp.glob("media-operations/*/operation.json")))

    async def test_new_tmdb_artwork_download_and_copy_respect_tiny_total_budget(self):
        content = b"synthetic-new-artwork" * 5
        response = httpx.Response(200, content=content, request=httpx.Request("GET", "http://fixture.invalid/poster.jpg"))
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.return_value = response
        destination = self.temp / "artwork/poster.jpg"
        prepared = self.temp / "prepared-poster.jpg"
        prepared.write_bytes(content)
        canonical = self.media / "Movies/New_TMDB_700/poster.jpg"
        with patch.object(settings, "STORAGE_POLICY", {"local_budget_bytes": 1, "offload_enabled": False}), \
             patch.object(ffmpeg_service.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(StorageCapacityError):
                await ffmpeg_service.download_and_cache_metadata_image("http://fixture.invalid/poster.jpg", str(destination))
            with self.assertRaises(StorageCapacityError):
                await media_updates.admitted_local_copy(prepared, canonical)
        self.assertFalse(destination.exists())
        self.assertFalse(canonical.exists())
        self.assertFalse(list(self.temp.rglob("*.part")))

    async def test_new_tmdb_metadata_and_generic_json_respect_tiny_total_budget(self):
        item = {"tmdb_id": 700, "type": "movie", "title": "New Metadata", "release_year": 2026,
                "description": "New cached metadata", "cast": [{"name": "Synthetic Cast"}],
                "crew": [{"name": "Synthetic Director"}], "keywords": ["synthetic"], "director": "Synthetic Director"}
        destination = self.media / "Movies/New_TMDB_700/.metadata/metadata.json"
        with patch.object(settings, "STORAGE_POLICY", {"local_budget_bytes": 1, "offload_enabled": False}):
            with self.assertRaises(StorageCapacityError):
                await media_updates.admitted_json_write(destination, {"title": "new metadata"})
            with self.assertRaises(StorageCapacityError):
                await TMDBClient().cache_media_locally(item, "", "")
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.media.glob("Movies/*/.metadata/metadata.json")))
        async with AsyncSession(self.engine) as db:
            self.assertIsNone(await db.get(Movie, "m_700"))

    async def test_cancelled_admitted_sidecar_copy_settles_then_restores_and_releases(self):
        self.video.write_bytes(b"accepted-video")
        source = await resolve_media_source(self.catalog)
        existing = self.video.parent / "audio/tr.m4a"
        existing.parent.mkdir()
        existing.write_bytes(b"accepted-audio")
        prepared = self.temp / "replacement.m4a"
        prepared.write_bytes(b"new-audio" * 100)
        started, release = threading.Event(), threading.Event()
        real_copy = media_updates.copy_bounded

        def delayed_copy(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release its owned copy")
            return real_copy(*args, **kwargs)

        async def replacing():
            async with MediaMutation("m_hybrid"):
                await media_updates.install_asset(source, "audio/tr.m4a", prepared)

        with patch.object(media_updates, "copy_bounded", delayed_copy):
            task = asyncio.create_task(replacing())
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            task.cancel()
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            async with AsyncSession(self.engine) as db:
                self.assertTrue((await db.exec(select(StorageReservation).where(StorageReservation.state == "active"))).all())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(existing.read_bytes(), b"accepted-audio")
        async with AsyncSession(self.engine) as db:
            self.assertFalse((await db.exec(select(StorageReservation).where(StorageReservation.state == "active"))).all())
        self.assertFalse(list(self.temp.glob("media-operations/*/operation.json")))

    async def test_cache_is_retained_after_open_run_and_short_lease_end(self):
        with patch.object(settings, "PLAYBACK_CACHE_GB", 1 / 1024**3):
            protected = self.service.cache_path("m_hybrid", "abcd")
            obsolete = self.service.cache_path("m_other", "beef")
            for root in (protected, obsolete):
                root.mkdir(parents=True)
                (root / "segment.m4s").write_bytes(b"segment" * 100)
            self.service.protect_run("open-paused-run", "m_hybrid", "abcd")
            self.service._active_cache_leases[str(protected)] = time.monotonic() - 600
            self.service.enforce_lru_limits({str(protected), str(obsolete)})
            self.assertTrue(protected.is_dir())
            self.assertEqual((obsolete / "segment.m4s").read_bytes(), b"segment" * 100)
            self.service.release_run("open-paused-run")
            self.service.enforce_lru_limits({str(protected), str(obsolete)})
            self.assertEqual((protected / "segment.m4s").read_bytes(), b"segment" * 100)
            self.assertEqual((obsolete / "segment.m4s").read_bytes(), b"segment" * 100)

    async def test_cold_cache_budget_failure_is_terminal_until_explicit_retry(self):
        self.video.write_bytes(b"accepted-local-video")
        source = await resolve_media_source(self.catalog)
        movie = SimpleNamespace(id="m_hybrid", source_fingerprint=playback_source_fingerprint(source, []),
                                probed_duration=2, container="matroska", codec="hevc", width=320, height=180,
                                frame_rate=10, quality="180p", audio_metadata=[], languages=[])
        with patch.object(settings, "STORAGE_POLICY", {"local_budget_bytes": 1, "offload_enabled": False}):
            self.assertFalse(await self.service.ensure_offline_baseline(movie, source, timeout=10))
            if self.service.active_jobs:
                await asyncio.gather(*list(self.service.active_jobs.values()))
            error = self.service.required_preparation_error(movie.id, movie.source_fingerprint, movie)
            self.assertEqual(error["code"], "storage_local_budget_exceeded")
            self.service.enqueue_cache_miss(movie, source)
            self.assertFalse(self.service.active_jobs)
        self.assertEqual(self.video.read_bytes(), b"accepted-local-video")

    async def test_cancelled_spool_retains_reservation_until_owned_copy_has_closed(self):
        remote = self.remote / "Movies/Hybrid/video.mkv"
        remote.parent.mkdir(parents=True)
        remote.write_bytes(b"video" * 100)
        source = await resolve_media_source(self.catalog)
        started, closing, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def copying(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await closed.wait()

        with patch.object(rclone_service, "copyto_atomic", copying):
            task = asyncio.create_task(self.service._input_process(source, "m_hybrid", source.fingerprint))
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            await asyncio.wait_for(closing.wait(), 5)
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            async with AsyncSession(self.engine) as db:
                rows = (await db.exec(select(StorageReservation).where(StorageReservation.state == "active"))).all()
            self.assertEqual(len(rows), 1)
            closed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        async with AsyncSession(self.engine) as db:
            rows = (await db.exec(select(StorageReservation).where(StorageReservation.state == "active"))).all()
        self.assertEqual(rows, [])

    async def test_native_cold_generation_deduplicates_full_spool_and_finishes_verified_baseline(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("native FFmpeg tools unavailable")
        result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                                 "color=c=blue:s=320x180:r=10:d=2", "-f", "lavfi", "-i",
                                 "sine=frequency=440:sample_rate=48000:duration=2", "-c:v", "libx264",
                                 "-preset", "ultrafast", "-threads", "1", "-c:a", "aac", str(self.video)],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        probe = await probe_completed_media(str(self.video))
        self.assertTrue(probe["valid_video"])
        source = await resolve_media_source(self.catalog)
        audio = probe["audio_metadata"]
        fingerprint = playback_source_fingerprint(source, audio)
        movie = Movie(id="m_hybrid", title="Hybrid native", description="", thumbnail_url="", release_year=2026,
                      video_url=self.catalog, duration="2s", probed_duration=probe["probed_duration"],
                      codec=probe["codec"], container=probe["container"], width=probe["width"], height=probe["height"],
                      frame_rate=probe["frame_rate"], quality="180p", source_fingerprint=fingerprint)
        movie.audio_metadata = audio
        movie.analysis_metadata_str = json.dumps(probe["analysis"])
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(movie)
            await db.commit()
        asset = await self.placement(self.catalog, audio=audio, fingerprint=fingerprint, video_fingerprint=source.video_fingerprint)
        original_bytes = self.video.read_bytes()
        self.video.unlink()
        cold = await resolve_media_source(self.catalog, refresh_cloud=True)
        self.assertFalse(await self.service.cold_recovery_ready(movie, cold))
        self.service.enqueue_cache_miss(movie, cold)
        self.service.enqueue_cache_miss(movie, cold)
        self.assertEqual(len(self.service.active_jobs), 1)
        self.assertTrue(await self.service.ensure_offline_baseline(movie, cold, timeout=60))
        if self.service.active_jobs:
            await asyncio.gather(*list(self.service.active_jobs.values()))
        self.assertTrue(self.service.rendition_complete(movie.id, fingerprint, self.service.baseline_video(movie).name))
        spools = list(self.service.cache_path(movie.id, fingerprint).glob(".sources/*/original.*"))
        self.assertEqual(len(spools), 1)
        self.assertEqual(spools[0].read_bytes(), original_bytes)
        self.assertTrue(await self.service.cold_recovery_ready(movie, cold))
        async with AsyncSession(self.engine) as db:
            db.add(StorageKeepLocal(id="native-hls-pin", media_id=movie.id, enabled=True))
            await db.commit()
        with patch.object(settings, "PLAYBACK_CACHE_GB", 1 / 1024**3):
            await self.service.enforce_lru_limits_admitted()
        self.assertTrue(self.service.rendition_complete(movie.id, fingerprint, self.service.baseline_video(movie).name))
        with patch.object(settings, "STORAGE_POLICY", {"local_budget_bytes": 1, "offload_enabled": False}):
            self.assertFalse(await self.service.cold_recovery_ready(movie, cold))
        async with AsyncSession(self.engine) as db:
            rows = (await db.exec(select(StorageReservation))).all()
        self.assertTrue(any(row.owner.startswith("playback-spool:") and row.peak_bytes == asset.size_bytes for row in rows))
        self.assertFalse([row for row in rows if row.state == "active"])

    async def test_native_browser_cold_direct_and_hls_keep_pause_transport_and_seek(self):
        node, chrome = shutil.which("node"), shutil.which("google-chrome")
        if not node or not chrome or not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("existing Node, Chrome, FFmpeg and FFprobe required")
        user = User(id=961, email="hybrid-browser@example.test", password_hash="unused")
        auth = AuthSession(id="hybrid-browser-session", user_id=user.id, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(user)
            db.add(auth)
            db.add(Profile(id="hybrid-browser-profile", name="Synthetic hybrid browser"))
            await db.commit()
        for transport, extension in (("direct", "mp4"), ("adaptive", "mkv")):
            self.catalog = f"/media/Movies/Hybrid/{transport}.{extension}"
            video = self.video.with_name(f"{transport}.{extension}")
            command = [shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=10:d=8",
                       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8", "-c:v", "libx264",
                       "-preset", "ultrafast", "-threads", "1", "-c:a", "aac"]
            if extension == "mp4":
                command += ["-movflags", "+faststart"]
            result = subprocess.run([*command, str(video)], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            probe = await probe_completed_media(str(video))
            source = await resolve_media_source(self.catalog)
            fingerprint = playback_source_fingerprint(source, probe["audio_metadata"])
            movie = Movie(id=f"m_hybrid_{transport}", title=f"Hybrid {transport}", description="", thumbnail_url="", release_year=2026,
                          video_url=self.catalog, duration="8s", probed_duration=probe["probed_duration"], codec=probe["codec"],
                          container=probe["container"], width=probe["width"], height=probe["height"], frame_rate=probe["frame_rate"],
                          quality="180p", source_fingerprint=fingerprint, analysis_metadata_str=json.dumps(probe["analysis"]))
            movie.audio_metadata = probe["audio_metadata"]
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                db.add(movie)
                await db.commit()
            await self.placement(self.catalog, audio=probe["audio_metadata"], fingerprint=fingerprint,
                                 video_fingerprint=source.video_fingerprint, media_id=movie.id)
            video.unlink()
        app = FastAPI()

        @app.middleware("http")
        async def fixture_auth(request: Request, call_next):
            if request.cookies.get("fixture-session") == auth.id:
                request.state.auth_session = auth
            return await call_next(request)

        async def sessions():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                yield db

        app.dependency_overrides[get_session] = sessions
        app.dependency_overrides[get_current_user] = lambda: user
        app.include_router(playback.router)

        @app.get("/")
        async def page():
            return HTMLResponse('<!doctype html><html lang="en"><script src="/fixture/hls.js"></script><body><video muted playsinline controls></video></body></html>')

        @app.get("/fixture/hls.js")
        async def hls_script():
            return FileResponse(Path(__file__).resolve().parents[2] / "web/node_modules/hls.js/dist/hls.min.js", media_type="text/javascript")

        @app.post("/fixture/expire-short-cache-leases")
        async def expire_short_leases():
            self.service._active_cache_leases = {key: time.monotonic() - 600 for key in self.service._active_cache_leases}
            with patch.object(settings, "PLAYBACK_CACHE_GB", 1 / 1024**3):
                await self.service.enforce_lru_limits_admitted()
            return {"status": "ok"}

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
        self.stack.enter_context(patch.object(playback, "playback_prep_service", self.service))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(server.started)
            script = Path(__file__).with_name("media") / "hybrid_playback_browser.mjs"
            output = Path(__file__).resolve().parents[2] / "hybrid-browser-observations.json"
            result = await asyncio.to_thread(subprocess.run, [node, str(script), f"http://127.0.0.1:{port}", chrome, str(output)],
                                             capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observation = json.loads(result.stdout)
            self.assertEqual(len(observation["cases"]), 2)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            listener.close()


if __name__ == "__main__":
    unittest.main()
