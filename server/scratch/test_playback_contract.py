"""Isolated HTTP contract regression for authenticated playback runs and tickets."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jwt
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import AuthSession, DownloadTask, Movie, PlaybackRun, PlaybackSession, Profile, User
from routes.auth import get_current_user
from routes.playback import router
from services.media_source import resolve_media_source
from services.ingest_preview import ingest_preview_service
from services.playback_prep import playback_prep_service


class PlaybackContractRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.mkdtemp(prefix="streamhome-playback-contract-")
        cls.database_path = os.path.join(cls.temp_directory, "contract.db")
        cls.engine = create_async_engine(f"sqlite+aiosqlite:///{cls.database_path}")
        cls.media_directory = Path(settings.MEDIA_DIR) / "Movies" / f"PlaybackContract_{uuid.uuid4().hex}"
        cls.media_directory.mkdir(parents=True, exist_ok=True)
        cls.media_file = cls.media_directory / "contract.mp4"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise unittest.SkipTest("FFmpeg is unavailable")
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                str(cls.media_file),
            ],
            check=True,
            capture_output=True,
        )
        cls.catalog_path = f"/media/Movies/{cls.media_directory.name}/contract.mp4"
        cls.user = User(id=901, email="playback-contract@example.test", password_hash="unused")
        cls.auth_session = AuthSession(
            id="playback-contract-session",
            user_id=901,
            created_at=time.time(),
            last_seen_at=time.time(),
            expires_at=time.time() + 3600,
            ip_address="127.0.0.1",
            device_label="Contract browser",
        )

        async def seed() -> None:
            async with cls.engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            source = await resolve_media_source(cls.catalog_path, check_cloud=False)
            cls.fingerprint = source.fingerprint
            async with AsyncSession(cls.engine, expire_on_commit=False) as db:
                db.add(cls.user)
                db.add(cls.auth_session)
                db.add(Profile(id="contract-profile", name="Contract Profile", theme="ember"))
                movie = Movie(
                    id="m_playback_contract",
                    title="Playback Contract",
                    description="Secure playback contract fixture",
                    thumbnail_url="",
                    banner_url="",
                    video_url=cls.catalog_path,
                    duration="2m",
                    release_year=2026,
                    type="movie",
                    availability="available",
                    catalog_source="server",
                    probed_duration=120,
                    container="mov,mp4",
                    codec="h264",
                    width=640,
                    height=360,
                    frame_rate=24,
                    source_fingerprint=cls.fingerprint,
                    quality="1080p",
                )
                movie.languages = ["eng"]
                movie.audio_metadata = [{"index": 0, "streamIndex": 1, "language": "eng", "label": "English", "channels": 2, "default": True}]
                db.add(movie)
                await db.commit()

        asyncio.run(seed())

        async def session_override():
            async with AsyncSession(cls.engine, expire_on_commit=False) as db:
                yield db

        app = FastAPI()

        @app.middleware("http")
        async def attach_session(request: Request, call_next):
            request.state.auth_session = cls.auth_session
            return await call_next(request)

        app.include_router(router)
        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_user] = lambda: cls.user
        cls.client = TestClient(app)

        cls.cache_root = playback_prep_service.cache_path("m_playback_contract", cls.fingerprint)
        video_dir = cls.cache_root / "video_original"
        audio_dir = cls.cache_root / "audio_0_eng"
        video_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)
        (video_dir / "playlist.m3u8").write_text("#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4,\nsegment_00000.m4s\n#EXT-X-ENDLIST\n", encoding="utf-8")
        (video_dir / "init.mp4").write_bytes(b"video-init")
        (video_dir / "segment_00000.m4s").write_bytes(b"video-segment")
        (audio_dir / "playlist.m3u8").write_text("#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4,\nsegment_00000.m4s\n#EXT-X-ENDLIST\n", encoding="utf-8")
        (audio_dir / "init.mp4").write_bytes(b"audio-init")
        (audio_dir / "segment_00000.m4s").write_bytes(b"audio-segment")
        (cls.cache_root / "master.m3u8").write_text(
            "#EXTM3U\n"
            "#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",NAME=\"English\",DEFAULT=YES,URI=\"audio_0_eng/playlist.m3u8\"\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,AUDIO=\"audio\"\n"
            "video_original/playlist.m3u8\n",
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(cls.engine.dispose())
        shutil.rmtree(cls.temp_directory, ignore_errors=True)
        shutil.rmtree(cls.media_directory, ignore_errors=True)
        shutil.rmtree(cls.cache_root.parent, ignore_errors=True)

    def setUp(self) -> None:
        self.patchers = [
            patch.object(playback_prep_service, "prepare", new=AsyncMock(return_value="ready")),
            patch.object(playback_prep_service, "preparation_state", return_value="ready"),
            patch.object(playback_prep_service, "preparation_error", return_value=None),
            patch.object(playback_prep_service, "playlist_ready", return_value=True),
            patch.object(playback_prep_service, "fully_prepared", side_effect=lambda *_: self.media_file.is_file()),
            patch.object(playback_prep_service, "switching_ready", side_effect=lambda *_: self.media_file.is_file()),
            patch.object(playback_prep_service, "rendition_seekable_until", return_value=120.0),
            patch("routes.playback.record_playback_progress", new=AsyncMock(return_value="viewing-attempt-contract")),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()

    def create_run(self) -> dict:
        response = self.client.post(
            "/api/playback/runs",
            json={"movieId": "m_playback_contract", "profileId": "contract-profile"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["preparationState"], "ready")
        self.assertIn("fullyPrepared", payload)
        self.assertIn("switchingReady", payload)
        self.assertIn("resumeReady", payload)
        self.assertIn("seekableUntil", payload)
        self.assertEqual(payload["preparationProgress"]["stage"], "streamable")
        self.assertEqual(payload["nextSequenceNumber"], 1)
        self.assertIn(payload["sourceMetadata"]["sourceFormat"], {"MP4", "HLS preview"})
        if payload["sourceMetadata"]["sourceFormat"] == "MP4":
            self.assertTrue(payload["fullyPrepared"])
            self.assertTrue(payload["switchingReady"])
            self.assertTrue(payload["resumeReady"])
            self.assertEqual(payload["seekableUntil"], 120.0)
            self.assertGreaterEqual(payload["preparationProgress"]["readySegments"], 0)
            self.assertEqual(payload["renditions"][0]["label"], "1080p")
            self.assertGreater(payload["sourceMetadata"]["duration"], 0)
        self.assertIn(payload["renditions"][0]["status"], {"streamable", "ready"})
        return payload

    def test_manifest_children_and_fragments_remain_ticket_protected(self) -> None:
        run = self.create_run()
        master = self.client.get(run["manifestUrl"])
        self.assertEqual(master.status_code, 200, master.text)
        self.assertIn("/api/playback/hls/m_playback_contract/video_original/playlist.m3u8?ticket=", master.text)
        child_url = next(line for line in master.text.splitlines() if "video_original/playlist.m3u8" in line)
        child = self.client.get(child_url)
        self.assertEqual(child.status_code, 200, child.text)
        segment_url = next(line for line in child.text.splitlines() if "segment_00000.m4s" in line)
        segment = self.client.get(segment_url)
        self.assertEqual(segment.status_code, 200)
        self.assertEqual(segment.content, b"video-segment")
        denied = self.client.get("/api/playback/hls/m_playback_contract/video_original/segment_00000.m4s")
        self.assertEqual(denied.status_code, 422)
        private_metadata = self.client.get(
            f"/api/playback/hls/m_playback_contract/preparation-error.json?ticket={run['ticket']}"
        )
        self.assertEqual(private_metadata.status_code, 403)
        self.assertEqual(private_metadata.json()["detail"]["code"], "HLS_ASSET_TYPE_FORBIDDEN")

    def test_status_polling_does_not_schedule_or_write_for_unchanged_media(self) -> None:
        run = self.create_run()
        prepare = playback_prep_service.prepare
        prepare.reset_mock()

        async def timestamps() -> tuple[float, float]:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                playback_run = await db.get(PlaybackRun, run["runId"])
                return playback_run.last_seen_at, playback_run.updated_at

        before = asyncio.run(timestamps())
        refreshed = self.client.get(f"/api/playback/runs/{run['runId']}")
        after = asyncio.run(timestamps())

        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        prepare.assert_not_awaited()
        self.assertEqual(after, before)

    def test_subtitle_delivery_requires_the_exact_track_identity(self) -> None:
        subtitle_file = self.media_directory / "subtitle_eng_main.vtt"
        subtitle_file.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n", encoding="utf-8")

        async def configure_subtitle(items: list[dict]) -> None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie = await db.get(Movie, "m_playback_contract")
                movie.subtitles = items
                db.add(movie)
                await db.commit()

        asyncio.run(configure_subtitle([{"language": "eng", "fileName": subtitle_file.name, "label": "English main"}]))
        try:
            run = self.create_run()
            track_id = run["subtitles"][0]["id"]
            exact = self.client.get(
                f"/api/playback/subtitles/m_playback_contract/{track_id}?ticket={run['ticket']}"
            )
            language_only = self.client.get(
                f"/api/playback/subtitles/m_playback_contract/eng?ticket={run['ticket']}"
            )
            self.assertEqual(exact.status_code, 200, exact.text)
            self.assertEqual(language_only.status_code, 404)
            self.assertEqual(language_only.json()["detail"]["code"], "SUBTITLE_NOT_FOUND")
        finally:
            asyncio.run(configure_subtitle([]))
            subtitle_file.unlink(missing_ok=True)

    def test_pending_quality_can_be_selected_for_server_priority(self) -> None:
        run = self.create_run()
        with patch.object(playback_prep_service, "prioritize_video_rendition", new=AsyncMock(return_value="preparing")) as prioritize:
            response = self.client.post(f"/api/playback/runs/{run['runId']}/renditions/video_original/prepare")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "preparing")
        prioritize.assert_awaited_once()

    def test_ingest_preview_is_protected_and_survives_local_catalog_handoff(self) -> None:
        task_id = f"preview-{uuid.uuid4().hex}"
        preview_root = ingest_preview_service.prepare(task_id, 20)
        (preview_root / "playlist.m3u8").write_text(
            "#EXTM3U\n"
            "#EXT-X-VERSION:7\n"
            "#EXT-X-MAP:URI=\"init.mp4\"\n"
            "#EXTINF:16,\n"
            "segment_00000.m4s\n"
            "#EXT-X-ENDLIST\n",
            encoding="utf-8",
        )
        (preview_root / "init.mp4").write_bytes(b"preview-init")
        (preview_root / "segment_00000.m4s").write_bytes(b"preview-segment")
        ingest_preview_service.mark_complete(task_id)

        async def enable_preview() -> None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie = await db.get(Movie, "m_playback_contract")
                movie.tmdb_id = 918273
                movie.video_url = "https://hidden-source.invalid/master.txt"
                movie.availability = "processing"
                movie.preview_task_id = task_id
                db.add(movie)
                db.add(
                    DownloadTask(
                        id=task_id,
                        tmdb_id=918273,
                        title="Playback Contract",
                        media_type="movie",
                        video_url=movie.video_url,
                        status="DOWNLOADING",
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
                await db.commit()

        async def finish_handoff() -> None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie = await db.get(Movie, "m_playback_contract")
                movie.video_url = self.catalog_path
                movie.availability = "available"
                movie.preview_task_id = None
                movie.source_fingerprint = self.fingerprint
                db.add(movie)
                await db.commit()

        async def restore() -> None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie = await db.get(Movie, "m_playback_contract")
                movie.tmdb_id = None
                movie.video_url = self.catalog_path
                movie.availability = "available"
                movie.preview_task_id = None
                movie.source_fingerprint = self.fingerprint
                db.add(movie)
                task = await db.get(DownloadTask, task_id)
                if task:
                    await db.delete(task)
                await db.commit()

        asyncio.run(enable_preview())
        try:
            run = self.create_run()
            self.assertIn(f"/api/playback/preview/m_playback_contract/playlist.m3u8", run["manifestUrl"])
            manifest = self.client.get(run["manifestUrl"])
            self.assertEqual(manifest.status_code, 200, manifest.text)
            self.assertNotIn("hidden-source.invalid", manifest.text)
            self.assertIn("/api/playback/preview/m_playback_contract/init.mp4?ticket=", manifest.text)
            segment_url = next(line for line in manifest.text.splitlines() if "segment_00000.m4s" in line)
            segment = self.client.get(segment_url)
            self.assertEqual(segment.status_code, 200)
            self.assertEqual(segment.content, b"preview-segment")
            state_url = segment_url.replace("segment_00000.m4s", "state.json")
            state_response = self.client.get(state_url)
            self.assertEqual(state_response.status_code, 403)
            self.assertEqual(state_response.json()["detail"]["code"], "PREVIEW_ASSET_TYPE_FORBIDDEN")

            asyncio.run(finish_handoff())
            refreshed = self.client.get(f"/api/playback/runs/{run['runId']}")
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertIn("/api/playback/preview/", refreshed.json()["manifestUrl"])
            continued = self.client.get(run["manifestUrl"])
            self.assertEqual(continued.status_code, 200, continued.text)
        finally:
            asyncio.run(restore())
            ingest_preview_service.remove(task_id)

    def test_missing_media_queues_one_repair_without_discarding_catalog_identity(self) -> None:
        source_task_id = f"repair-source-{uuid.uuid4().hex}"
        missing_path = self.media_file.with_suffix(".missing")

        async def seed_repair_source() -> None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie = await db.get(Movie, "m_playback_contract")
                movie.tmdb_id = 445566
                movie.video_url = self.catalog_path
                movie.availability = "available"
                movie.preview_task_id = None
                db.add(movie)
                db.add(
                    DownloadTask(
                        id=source_task_id,
                        tmdb_id=445566,
                        title="Playback Contract",
                        media_type="movie",
                        video_url="https://media.example.test/contract.mp4",
                        status="COMPLETED",
                        language="en",
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
                await db.commit()

        async def inspect_repair() -> tuple[list[DownloadTask], Movie]:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                tasks = list(
                    (
                        await db.exec(
                            select(DownloadTask).where(
                                DownloadTask.tmdb_id == 445566,
                                DownloadTask.status == "PENDING",
                            )
                        )
                    ).all()
                )
                return tasks, await db.get(Movie, "m_playback_contract")

        async def cleanup() -> None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie = await db.get(Movie, "m_playback_contract")
                movie.tmdb_id = None
                movie.video_url = self.catalog_path
                movie.availability = "available"
                movie.preview_task_id = None
                db.add(movie)
                tasks = list((await db.exec(select(DownloadTask).where(DownloadTask.tmdb_id == 445566))).all())
                for task in tasks:
                    await db.delete(task)
                await db.commit()

        asyncio.run(seed_repair_source())
        self.media_file.replace(missing_path)
        try:
            with patch("routes.playback.validate_url", new=AsyncMock(return_value=None)):
                first = self.client.post(
                    "/api/playback/runs",
                    json={"movieId": "m_playback_contract", "profileId": "contract-profile"},
                )
                second = self.client.post(
                    "/api/playback/runs",
                    json={"movieId": "m_playback_contract", "profileId": "contract-profile"},
                )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["preparationState"], "preparing")
            self.assertEqual(second.status_code, 200, second.text)
            tasks, movie = asyncio.run(inspect_repair())
            self.assertEqual(len(tasks), 1)
            self.assertEqual(movie.video_url, self.catalog_path)
            self.assertEqual(movie.availability, "processing")
            self.assertEqual(movie.preview_task_id, tasks[0].id)
        finally:
            missing_path.replace(self.media_file)
            asyncio.run(cleanup())

    def test_progress_is_sequenced_completion_is_sticky_and_start_over_is_explicit(self) -> None:
        run = self.create_run()
        heartbeat = self.client.post(
            f"/api/playback/runs/{run['runId']}/progress",
            json={"timestamp": 40, "durationWatched": 1, "sequenceNumber": 1, "event": "heartbeat"},
        )
        self.assertEqual(heartbeat.status_code, 200, heartbeat.text)
        self.assertEqual(heartbeat.json()["nextSequenceNumber"], 2)

        ended = self.client.post(
            f"/api/playback/runs/{run['runId']}/progress",
            json={"timestamp": 120, "durationWatched": 1, "isFinished": True, "sequenceNumber": 2, "event": "ended"},
        )
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(ended.json()["status"], "finished")

        delayed = self.client.post(
            f"/api/playback/runs/{run['runId']}/progress",
            json={"timestamp": 70, "durationWatched": 0, "sequenceNumber": 3, "event": "pause"},
        )
        self.assertEqual(delayed.status_code, 200, delayed.text)
        self.assertEqual(delayed.json()["status"], "sticky_finished")

        restarted = self.client.post(f"/api/playback/runs/{run['runId']}/start-over")
        self.assertEqual(restarted.status_code, 200, restarted.text)

        async def read_session() -> PlaybackSession:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                return (await db.exec(select(PlaybackSession).where(PlaybackSession.profile_id == "contract-profile"))).one()

        session = asyncio.run(read_session())
        self.assertEqual(session.timestamp, 0)
        self.assertFalse(session.is_finished)

    def test_close_persists_confirmed_position_and_abandons_run_immediately(self) -> None:
        run = self.create_run()
        closed = self.client.post(
            f"/api/playback/runs/{run['runId']}/close",
            json={"timestamp": 37, "durationWatched": 2, "sequenceNumber": 99, "event": "exit"},
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["status"], "abandoned")

        async def read_state() -> tuple[PlaybackRun, PlaybackSession]:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                playback_run = await db.get(PlaybackRun, run["runId"])
                session = (await db.exec(select(PlaybackSession).where(
                    PlaybackSession.profile_id == "contract-profile",
                    PlaybackSession.movie_id == "m_playback_contract",
                ))).one()
                return playback_run, session

        playback_run, session = asyncio.run(read_state())
        self.assertEqual(playback_run.lifecycle_state, "abandoned")
        self.assertEqual(session.timestamp, 37)

        late_progress = self.client.post(
            f"/api/playback/runs/{run['runId']}/progress",
            json={"timestamp": 40, "durationWatched": 0, "sequenceNumber": closed.json()["nextSequenceNumber"], "event": "heartbeat"},
        )
        self.assertEqual(late_progress.status_code, 410, late_progress.text)

    def test_resume_position_survives_preparation_polling(self) -> None:
        async def seed_resume() -> None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                existing = (await db.exec(select(PlaybackSession).where(PlaybackSession.profile_id == "contract-profile"))).first()
                if existing is None:
                    existing = PlaybackSession(
                        profile_id="contract-profile",
                        movie_id="m_playback_contract",
                        timestamp=45,
                        duration_watched=45,
                        completion_rate=45 / 120,
                        updated_at=datetime.now(timezone.utc).isoformat(),
                        is_finished=False,
                    )
                else:
                    existing.timestamp = 45
                    existing.duration_watched = 45
                    existing.completion_rate = 45 / 120
                    existing.updated_at = datetime.now(timezone.utc).isoformat()
                    existing.is_finished = False
                db.add(existing)
                await db.commit()

        asyncio.run(seed_resume())
        run = self.create_run()
        self.assertEqual(run["resumePosition"], 45)
        refreshed = self.client.get(f"/api/playback/runs/{run['runId']}")
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["resumePosition"], 45)

    def test_source_replacement_invalidates_the_previous_ticket(self) -> None:
        run = self.create_run()
        self.media_file.write_bytes(self.media_file.read_bytes() + b"replacement")
        refreshed = self.client.get(f"/api/playback/runs/{run['runId']}")
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertNotEqual(refreshed.json()["ticket"], run["ticket"])
        stale = self.client.get(run["manifestUrl"])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "PLAYBACK_SOURCE_CHANGED")

    def test_expired_or_revoked_tickets_fail_closed(self) -> None:
        run = self.create_run()
        payload = jwt.decode(run["ticket"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        payload["exp"] = int(time.time()) - 1
        expired = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
        response = self.client.get(f"/api/playback/manifest/m_playback_contract?ticket={expired}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "PLAYBACK_TICKET_EXPIRED")

        async def revoke(value: float | None) -> None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                session = await db.get(AuthSession, self.auth_session.id)
                session.revoked_at = value
                db.add(session)
                await db.commit()

        asyncio.run(revoke(time.time()))
        try:
            revoked = self.client.get(run["manifestUrl"])
            self.assertEqual(revoked.status_code, 403)
            self.assertEqual(revoked.json()["detail"]["code"], "PLAYBACK_SESSION_REVOKED")
        finally:
            asyncio.run(revoke(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
