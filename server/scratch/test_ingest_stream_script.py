import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from models import DownloadAddRequest, DownloadTask, Movie
from routes import queue as queue_routes
from scratch.test_ingest_stream import LocalMediaBridge, build_payload, normalize_introdb_markers


class IngestionSmokeTestScriptTests(unittest.TestCase):
    def test_movie_payload_omits_tv_and_null_fields(self):
        payload = build_payload(
            tmdb_id=550,
            media_type="movie",
            video_url="https://example.test/video.mp4",
            season=None,
            episode=None,
            quality="1080p",
            language="en",
            skip_markers={"intro": []},
        )

        self.assertNotIn("season", payload)
        self.assertNotIn("episode", payload)
        self.assertNotIn("audio_url", payload)
        self.assertNotIn("subtitles", payload)

    def test_tv_payload_requires_and_includes_episode_identity(self):
        payload = build_payload(
            tmdb_id=1396,
            media_type="tv",
            video_url="https://example.test/video.mp4",
            season=1,
            episode=2,
            quality="720p",
            language="en",
            skip_markers={"intro": []},
        )

        self.assertEqual(payload["season"], 1)
        self.assertEqual(payload["episode"], 2)

    def test_introdb_milliseconds_are_converted_to_player_seconds(self):
        markers = normalize_introdb_markers(
            {
                "intro": [{"start_ms": 1_500, "end_ms": 61_250}],
                "credits": [{"start": 3_500, "end_ms": None}],
            },
            duration_ms=3_600_000,
        )

        self.assertEqual(markers["intro"], [{"start": 1.5, "end": 61.25}])
        self.assertEqual(markers["credits"], [{"start": 3500.0, "end": 3600.0}])

    def test_local_bridge_supports_head_and_byte_ranges(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            media_path = Path(temporary_directory) / "sample video.mp4"
            media_path.write_bytes(b"0123456789")

            with LocalMediaBridge(media_path) as bridge:
                head = httpx.head(bridge.url, timeout=5)
                ranged = httpx.get(bridge.url, headers={"Range": "bytes=3-6"}, timeout=5)

        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["accept-ranges"], "bytes")
        self.assertEqual(head.headers["content-length"], "10")
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.headers["content-range"], "bytes 3-6/10")
        self.assertEqual(ranged.content, b"3456")

    def test_real_ingestion_handler_accepts_bridge_url_and_catalogs_tmdb_metadata(self):
        async def scenario(database_path: Path, video_url: str):
            engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)

            metadata = {
                "title": "Catalogued Test Movie",
                "description": "TMDB description",
                "thumbnailUrl": "https://image.tmdb.org/t/p/w500/poster.jpg",
                "bannerUrl": "https://image.tmdb.org/t/p/original/backdrop.jpg",
                "duration": "2h",
                "releaseYear": 2026,
                "rating": "PG-13",
                "director": "Test Director",
                "originalLanguage": "en",
                "genres": ["Drama"],
                "cast": ["Test Actor"],
            }
            request = DownloadAddRequest(
                tmdb_id=999_001,
                media_type="movie",
                video_url=video_url,
                quality="1080p",
                language="en",
                skip_markers={"intro": [{"start": 1.0, "end": 2.0}]},
            )

            try:
                with (
                    patch.object(queue_routes, "engine", engine),
                    patch.object(queue_routes.tmdb_client, "fetch_movie_metadata", AsyncMock(return_value=metadata)),
                ):
                    response = await queue_routes.add_movie(request, token="test-token")

                async with AsyncSession(engine) as session:
                    task = await session.get(DownloadTask, response["taskId"])
                    movie = await session.get(Movie, "m_999001")
                    self.assertIsNotNone(task)
                    self.assertIsNone(task.season)
                    self.assertIsNone(task.episode)
                    self.assertEqual(task.video_url, video_url)
                    self.assertIsNotNone(movie)
                    self.assertEqual(movie.title, "Catalogued Test Movie")
                    self.assertEqual(movie.description, "TMDB description")
                    self.assertEqual(movie.skip_markers["intro"][0]["start"], 1.0)
            finally:
                await engine.dispose()

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            media_path = directory / "sample.mp4"
            media_path.write_bytes(b"streamhome-test-media")
            with LocalMediaBridge(media_path) as bridge:
                asyncio.run(scenario(directory / "ingestion.db", bridge.url))


if __name__ == "__main__":
    unittest.main()
