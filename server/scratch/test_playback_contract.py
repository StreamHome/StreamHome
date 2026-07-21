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
from models import AuthSession, Movie, PlaybackSession, Profile, User
from routes.auth import get_current_user
from routes.playback import router
from services.media_source import resolve_media_source
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
        self.assertEqual(payload["nextSequenceNumber"], 1)
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
