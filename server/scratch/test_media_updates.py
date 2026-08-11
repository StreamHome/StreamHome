"""Regression coverage for independent MediaSender sidecar and marker mutations."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import IntegrationCredential, Movie
from routes.media_updates import router
from services.integration_auth import integration_token_hash


class MediaUpdateContractRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-media-update-test-")
        database_path = Path(self.temporary.name) / "media-updates.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        self.media_id = f"m_media_update_{uuid.uuid4().hex}"
        self.media_directory = Path(settings.MEDIA_DIR) / "Movies" / f"MediaUpdate_{uuid.uuid4().hex}"
        self.media_directory.mkdir(parents=True)
        self.video_path = self.media_directory / "video.mp4"
        self.video_path.write_bytes(b"immutable-main-video")
        self.catalog_path = f"/media/Movies/{self.media_directory.name}/video.mp4"
        self.token = f"shk_{uuid.uuid4().hex}"
        self.read_only_token = f"shk_{uuid.uuid4().hex}"
        asyncio.run(self._seed())

        async def session_override():
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                yield session

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = session_override
        self.client = TestClient(app)

    async def _seed(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            movie = Movie(
                id=self.media_id,
                title="TMDB-owned title",
                description="TMDB-owned description",
                thumbnail_url="",
                banner_url="",
                video_url=self.catalog_path,
                duration="1h",
                release_year=2026,
                type="movie",
                availability="available",
                catalog_source="server",
                source_fingerprint="original-fingerprint",
            )
            movie.languages = ["en"]
            movie.audio_metadata = [
                {
                    "index": 0,
                    "streamIndex": 1,
                    "codec": "aac",
                    "language": "en",
                    "label": "English",
                    "channels": 2,
                    "default": True,
                    "source": "embedded",
                }
            ]
            session.add(movie)
            ingest = IntegrationCredential(
                id="media-update-ingest",
                name="MediaSender",
                token_hash=integration_token_hash(self.token),
                scopes_str='["ingest"]',
                created_at=time.time(),
            )
            read_only = IntegrationCredential(
                id="media-update-read-only",
                name="Queue monitor",
                token_hash=integration_token_hash(self.read_only_token),
                scopes_str='["downloads:read"]',
                created_at=time.time(),
            )
            session.add(ingest)
            session.add(read_only)
            await session.commit()

    def tearDown(self) -> None:
        self.client.close()
        asyncio.run(self.engine.dispose())
        self.temporary.cleanup()
        shutil.rmtree(self.media_directory, ignore_errors=True)

    def bearer(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    async def movie(self) -> Movie:
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            return await session.get(Movie, self.media_id)

    def test_skip_markers_update_without_mutating_main_video_or_tmdb_fields(self) -> None:
        original_bytes = self.video_path.read_bytes()
        original_modified = self.video_path.stat().st_mtime_ns
        response = self.client.patch(
            f"/api/media/{self.media_id}/metadata",
            headers=self.bearer(),
            json={"skip_markers": {"intro": [{"start": 1.5, "end": 61.25}]}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["skipMarkers"]["intro"][0], {"start": 1.5, "end": 61.25})
        self.assertEqual(self.video_path.read_bytes(), original_bytes)
        self.assertEqual(self.video_path.stat().st_mtime_ns, original_modified)
        movie = asyncio.run(self.movie())
        self.assertEqual(movie.title, "TMDB-owned title")
        self.assertEqual(movie.description, "TMDB-owned description")
        self.assertEqual(movie.skip_markers["intro"][0]["end"], 61.25)
        metadata = self.media_directory / ".metadata" / "metadata.json"
        self.assertEqual(__import__("json").loads(metadata.read_text(encoding="utf-8"))["skip_markers"], movie.skip_markers)

        forbidden = self.client.patch(
            f"/api/media/{self.media_id}/metadata",
            headers=self.bearer(),
            json={"skip_markers": {}, "title": "Sender override"},
        )
        self.assertEqual(forbidden.status_code, 422, forbidden.text)
        invalid_range = self.client.patch(
            f"/api/media/{self.media_id}/metadata",
            headers=self.bearer(),
            json={"skip_markers": {"intro": [{"start": 10, "end": 5}]}},
        )
        self.assertEqual(invalid_range.status_code, 422, invalid_range.text)

    def test_subtitle_upsert_replace_and_delete_are_independent(self) -> None:
        async def prepare_subtitle(_url, destination, **_kwargs):
            destination.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n", encoding="utf-8")

        with (
            patch("routes.media_updates.validate_remote_input", new=AsyncMock(return_value={})),
            patch("services.media_updates.prepare_subtitle_asset", side_effect=prepare_subtitle),
        ):
            response = self.client.put(
                f"/api/media/{self.media_id}/subtitles/en-main",
                headers=self.bearer(),
                json={"language": "en", "label": "English CC", "url": "https://media.example/subtitle.srt"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["subtitle"]["trackId"], "en-main")
        subtitle_path = self.media_directory / "subtitle_en-main.vtt"
        self.assertTrue(subtitle_path.is_file())
        self.assertEqual(self.video_path.read_bytes(), b"immutable-main-video")

        removed = self.client.delete(
            f"/api/media/{self.media_id}/subtitles/en-main",
            headers=self.bearer(),
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertFalse(subtitle_path.exists())
        self.assertEqual(asyncio.run(self.movie()).subtitles, [])

    def test_dubbing_upsert_and_delete_refresh_only_sidecar_state(self) -> None:
        async def prepare_audio(_url, temporary_root, **_kwargs):
            result = temporary_root / "audio.m4a"
            result.write_bytes(b"validated-audio-sidecar")
            return result

        original_bytes = self.video_path.read_bytes()
        with (
            patch("routes.media_updates.validate_remote_input", new=AsyncMock(return_value={})),
            patch("services.media_updates.prepare_audio_asset", side_effect=prepare_audio),
            patch("services.media_updates.schedule_audio_preparation", new=AsyncMock()),
        ):
            response = self.client.put(
                f"/api/media/{self.media_id}/audio/tr",
                headers=self.bearer(),
                json={"url": "https://media.example/turkish.m4a"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["audio"]["language"], "tr")
        audio_path = self.media_directory / "audio" / "tr.m4a"
        self.assertEqual(audio_path.read_bytes(), b"validated-audio-sidecar")
        movie = asyncio.run(self.movie())
        self.assertIn("tr", movie.languages)
        self.assertNotEqual(movie.source_fingerprint, "original-fingerprint")
        self.assertEqual(self.video_path.read_bytes(), original_bytes)

        with patch("services.media_updates.schedule_audio_preparation", new=AsyncMock()):
            removed = self.client.delete(
                f"/api/media/{self.media_id}/audio/tr",
                headers=self.bearer(),
            )
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertFalse(audio_path.exists())
        self.assertNotIn("tr", asyncio.run(self.movie()).languages)

    def test_mutations_require_an_ingest_scoped_integration_key(self) -> None:
        missing = self.client.patch(
            f"/api/media/{self.media_id}/metadata",
            json={"skip_markers": {}},
        )
        self.assertEqual(missing.status_code, 401, missing.text)
        forbidden = self.client.patch(
            f"/api/media/{self.media_id}/metadata",
            headers=self.bearer(self.read_only_token),
            json={"skip_markers": {}},
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)


if __name__ == "__main__":
    unittest.main()
