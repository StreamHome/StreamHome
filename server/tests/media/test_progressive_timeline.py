"""Real direct/adaptive source selection regressions; disposable installations only."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from db import get_session
from models import AuthSession, Movie, Profile, User
from routes import playback
from routes.auth import get_current_user
from services.media_probe import probe_completed_media
from services.media_source import playback_source_fingerprint, resolve_media_source
from services.playback_prep import MAX_HLS_START_DELTA_SECONDS, PlaybackPrepService


class ProgressiveTimelineRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg and FFprobe are required")
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-progressive-timeline-")
        self.root = Path(self.temporary.name)
        self.previous = settings.MEDIA_DIR, settings.TEMP_DIR, settings.STORAGE_ENGINE
        settings.MEDIA_DIR = str(self.root / "media")
        settings.TEMP_DIR = str(self.root / "temp")
        settings.STORAGE_ENGINE = "LOCAL"
        Path(settings.MEDIA_DIR).mkdir()
        Path(settings.TEMP_DIR).mkdir()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'catalog.db'}")
        self.service = PlaybackPrepService()
        self.patchers = [
            patch.object(db_module, "engine", self.engine),
            patch.object(playback, "playback_prep_service", self.service),
            patch("lifecycle.media_work._locks", {}),
        ]
        for patcher in self.patchers:
            patcher.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.user = User(id=601, email="progressive-timeline@example.test", password_hash="unused")
        self.auth_session = AuthSession(id="timeline-session", user_id=601, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(self.user)
            session.add(self.auth_session)
            session.add(Profile(id="timeline-profile", name="Timeline Profile"))
            await session.commit()

        async def sessions():
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                yield session

        self.app = FastAPI()

        @self.app.middleware("http")
        async def attach_session(request: Request, call_next):
            request.state.auth_session = self.auth_session
            return await call_next(request)

        self.app.include_router(playback.router)
        self.app.dependency_overrides[get_session] = sessions
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://timeline.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.quiesce_for_update(timeout=10)
        for patcher in reversed(self.patchers):
            patcher.stop()
        await self.engine.dispose()
        settings.MEDIA_DIR, settings.TEMP_DIR, settings.STORAGE_ENGINE = self.previous
        self.temporary.cleanup()

    async def fixture(self, name):
        directory = Path(settings.MEDIA_DIR) / "Movies" / name
        directory.mkdir(parents=True)
        video = directory / "film.mp4"
        base = ["-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=1"]
        encode = ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-pix_fmt", "yuv420p"]
        if name == "multiple":
            args = [*base, "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=30:d=3", "-map", "0:v:0", "-map", "1:v:0", *encode, "-disposition:v:0", "0", "-disposition:v:1", "default"]
        elif name == "audio-tail":
            args = [*base, "-f", "lavfi", "-i", "sine=frequency=600:sample_rate=48000:duration=3", "-map", "0:v:0", "-map", "1:a:0", *encode, "-c:a", "aac"]
        else:
            args = [*base, *encode]
        command = [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args, "-movflags", "+faststart", str(video)]
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        if name == "epoch":
            shifted = directory / "shifted.mp4"
            result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-copyts", "-itsoffset", "10", "-i", str(video), "-map", "0:v:0", "-c", "copy", "-avoid_negative_ts", "disabled", "-movflags", "+faststart", str(shifted)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            shifted.replace(video)
        probe = await probe_completed_media(str(video))
        self.assertTrue(probe.get("valid_video"), probe)
        self.assertAlmostEqual(probe["probed_duration"], 1.0, places=5)
        catalog_path = f"/media/Movies/{name}/film.mp4"
        source = await resolve_media_source(catalog_path, check_cloud=False)
        fingerprint = playback_source_fingerprint(source, probe["audio_metadata"])
        movie = Movie(id=f"m_{name}", title=name, description="", thumbnail_url="", banner_url="", video_url=catalog_path, duration="1s", release_year=2026, catalog_source="server", availability="available", source_fingerprint=fingerprint, probed_duration=probe["probed_duration"], codec=probe["codec"], container=probe["container"], width=probe["width"], height=probe["height"], frame_rate=probe["frame_rate"], quality=probe["quality"], analysis_metadata_str=json.dumps(probe["analysis"]))
        movie.audio_metadata = probe["audio_metadata"]
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(movie)
            await session.commit()
        return movie, source, video

    async def run_contract(self, movie):
        response = await self.client.post("/api/playback/runs", json={"movieId": movie.id, "profileId": "timeline-profile"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertAlmostEqual(payload["sourceMetadata"]["duration"], 1.0, places=5)
        self.assertEqual((payload["sourceMetadata"]["width"], payload["sourceMetadata"]["height"]), (320, 180))
        return payload

    async def rejected_direct(self, name):
        movie, _source, video = await self.fixture(name)
        self.assertIs(json.loads(movie.analysis_metadata_str)["direct_timeline_compatible"], False)
        run = await self.run_contract(movie)
        self.assertFalse(run["sourceMetadata"]["progressiveCompatible"])
        self.assertEqual(run["progressiveUrl"], "")
        endpoint = f"/api/playback/progressive/{movie.id}?ticket={run['ticket']}"
        for method in (self.client.get, self.client.head):
            response = await method(endpoint)
            self.assertEqual(response.status_code, 409, response.text)
            if method == self.client.get:
                self.assertEqual(response.json()["detail"]["code"], "PROGRESSIVE_SOURCE_INCOMPATIBLE")
        # The authenticated FFmpeg input bridge must retain original bytes; it
        # does not claim that a browser's default track is the selected video.
        bridge = await self.client.get(f"/api/playback/source/{movie.id}?ticket={run['ticket']}", headers={"Range": "bytes=0-31"})
        self.assertEqual(bridge.status_code, 206, bridge.text)
        self.assertEqual(bridge.content, video.read_bytes()[:32])
        return movie, run

    async def test_multiple_video_defaults_require_selected_adaptive_video(self):
        await self.rejected_direct("multiple")

    async def test_nonzero_epoch_requires_elapsed_adaptive_timeline(self):
        await self.rejected_direct("epoch")

    async def test_long_audio_tail_cannot_extend_selected_video_progress(self):
        await self.rejected_direct("audio-tail")

    async def test_normal_measured_mp4_keeps_direct_byte_ranges(self):
        movie, _source, video = await self.fixture("normal")
        self.assertIs(json.loads(movie.analysis_metadata_str)["direct_timeline_compatible"], True)
        run = await self.run_contract(movie)
        self.assertTrue(run["sourceMetadata"]["progressiveCompatible"])
        self.assertIn("/progressive/", run["progressiveUrl"])
        response = await self.client.get(run["progressiveUrl"], headers={"Range": "bytes=0-31"})
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(response.content, video.read_bytes()[:32])

    async def test_legacy_missing_proof_is_reprobed_before_direct_advertising(self):
        movie, _source, _video = await self.fixture("normal")
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            current = await session.get(Movie, movie.id)
            current.analysis_metadata_str = "{}"
            await session.commit()
        self.assertFalse(playback.source_metadata(current).progressive_compatible)
        run = await self.run_contract(movie)
        self.assertTrue(run["sourceMetadata"]["progressiveCompatible"])
        async with AsyncSession(self.engine) as session:
            current = await session.get(Movie, movie.id)
            self.assertIs(json.loads(current.analysis_metadata_str)["direct_timeline_compatible"], True)

    async def test_existing_ticket_cannot_bypass_missing_or_nonboolean_proof(self):
        movie, _source, _video = await self.fixture("normal")
        run = await self.run_contract(movie)
        for proof in ({}, {"direct_timeline_compatible": "true"}):
            async with AsyncSession(self.engine) as session:
                current = await session.get(Movie, movie.id)
                current.analysis_metadata_str = json.dumps(proof)
                await session.commit()
            response = await self.client.get(run["progressiveUrl"])
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], "PROGRESSIVE_SOURCE_INCOMPATIBLE")

    async def test_real_verified_hls_in_browser_matches_selected_video_elapsed_time(self):
        node = shutil.which("node")
        chrome = shutil.which("google-chrome")
        if not node or not chrome:
            self.skipTest("Node and an existing Google Chrome executable are required")
        cases = []
        for name in ("normal", "multiple", "epoch", "audio-tail"):
            movie, source, video = await self.fixture(name)
            await self.service.prepare(movie.id, movie, source, include_remaining=True)
            ready = await asyncio.wait_for(self.service.wait_until_fully_prepared(movie.id, movie.source_fingerprint, movie), timeout=30)
            self.assertTrue(ready, self.service.full_preparation_error(movie.id, movie.source_fingerprint, movie))
            cache = self.service.cache_path(movie.id, movie.source_fingerprint)
            self.assertTrue(self.service.rendition_verified(movie.id, movie.source_fingerprint, "video_original"))
            run = await self.run_contract(movie)
            self.assertTrue(run["manifestUrl"])
            manifest = await self.client.get(run["manifestUrl"])
            self.assertEqual(manifest.status_code, 200, manifest.text)
            cases.append({"name": name, "source": str(video), "cache": str(cache), "duration": 1.0, "width": 320, "height": 180, "direct": run["sourceMetadata"]["progressiveCompatible"]})
        index = self.root / "browser-cases.json"
        index.write_text(json.dumps(cases))
        script = Path(__file__).with_name("progressive_timing_browser.mjs")
        result = await asyncio.to_thread(subprocess.run, [node, str(script), str(index), chrome], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(len(summary["cases"]), 4)
        for case in summary["cases"]:
            with self.subTest(case=case["name"]):
                adaptive = case["adaptive"]
                self.assertIsNone(adaptive["error"])
                self.assertIsNone(adaptive["playError"])
                self.assertTrue(adaptive["ended"], adaptive)
                # The existing fMP4 encoder/muxer may retain a bounded preroll;
                # compare against the production timeline verification bound.
                self.assertAlmostEqual(adaptive["duration"], 1.0, delta=MAX_HLS_START_DELTA_SECONDS)
                self.assertAlmostEqual(adaptive["currentTime"], 1.0, delta=MAX_HLS_START_DELTA_SECONDS)
                self.assertAlmostEqual(adaptive["initial"]["currentTime"], 0.0, places=5)
                self.assertEqual((adaptive["width"], adaptive["height"]), (320, 180))
                self.assertGreater(adaptive["pixel"][0], 200, "adaptive output must show selected first red video")
                self.assertLess(adaptive["pixel"][2], 40)
        print("REAL_BROWSER_TIMELINE=" + json.dumps(summary), flush=True)


if __name__ == "__main__":
    unittest.main()
