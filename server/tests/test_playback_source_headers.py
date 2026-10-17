"""Retained source intent survives actual movie/episode repair and persistence.

Run through tests/run_isolated.py; all state and credentials are synthetic.
"""
from __future__ import annotations

import json
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import event
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from models import DownloadTask, Episode, Movie
from routes.playback import queue_media_repair
from services.ingestion_headers import encode_source_headers
from services.ingestion_errors import IngestionFailure
from services import queue as worker
from tests.test_source_headers import SourceServer
from scratch.test_ffmpeg_headers import _create_separate_disguised_hls_fixtures


VIDEO = {"Authorization": "Bearer synthetic-video"}
AUDIO = {"Cookie": "synthetic=audio"}
SUBTITLE = {"Authorization": "Bearer synthetic-subtitle"}


class PlaybackSourceHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Background admission monitoring can cancel a pooled connection. Match
        # production's durable SQLite file rather than losing an in-memory DB.
        self.database_directory = tempfile.TemporaryDirectory()
        self.engine = create_async_engine("sqlite+aiosqlite:///" + str(Path(self.database_directory.name) / 'headers.db'))
        from db import set_sqlite_pragma
        event.listen(self.engine.sync_engine, 'connect', set_sqlite_pragma)
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.database_directory.cleanup()

    async def seed(self, db, identifier, kind, raw_headers, subtitles):
        movie = Movie(id=f"m_{identifier}" if kind == "movie" else f"tv_{identifier}",
                      tmdb_id=identifier, title="Synthetic retained source", description="",
                      thumbnail_url="", video_url="/media/Movies/synthetic/missing.mp4",
                      duration="1m", release_year=2026, availability="available")
        db.add(movie)
        await db.flush()
        media = movie
        if kind != "movie":
            media = Episode(id=f"ep_{identifier}_s1_e1", movie_id=movie.id,
                            season_number=1, episode_number=1, title="Synthetic episode",
                            description="", thumbnail_url="", video_url=movie.video_url,
                            duration="1m")
            db.add(media)
        retained = DownloadTask(id=f"retained-{identifier}", tmdb_id=identifier,
                                title="Synthetic retained source", media_type=kind,
                                season=None if kind == "movie" else 1,
                                episode=None if kind == "movie" else 1,
                                video_url="http://127.0.0.1:9101/video",
                                audio_url="http://127.0.0.1:9102/audio",
                                headers_str=raw_headers, subtitles_str=json.dumps(subtitles),
                                status="COMPLETED", private_source_allowed=True,
                                created_at="2026-10-14T00:00:00+00:00")
        db.add(retained)
        await db.commit()
        return movie, media, retained

    async def test_movie_and_episode_repairs_preserve_distinct_empty_and_legacy_sets(self):
        cases = [(encode_source_headers(VIDEO, AUDIO), AUDIO, SUBTITLE),
                 (encode_source_headers(VIDEO, {}), {}, {}),
                 (json.dumps(VIDEO), VIDEO, None)]
        identifier = 4100
        for kind in ("movie", "tv"):
            for raw, audio, subtitle_headers in cases:
                identifier += 1
                with self.subTest(kind=kind, audio=audio):
                    subtitle = {"language": "tr", "url": "http://127.0.0.1:9103/subtitle.vtt"}
                    if subtitle_headers is not None:
                        subtitle["headers"] = subtitle_headers
                    async with AsyncSession(self.engine, expire_on_commit=False) as db:
                        movie, media, retained = await self.seed(db, identifier, kind, raw, [subtitle])
                        original_url = media.video_url
                        repaired = await queue_media_repair(db, movie, media, "MEDIA_SOURCE_MISSING")
                        self.assertIsNotNone(repaired)
                        again = await queue_media_repair(db, movie, media, "MEDIA_SOURCE_MISSING")
                        self.assertEqual(again.id, repaired.id, "Active repair must be reused")
                        repair_id = repaired.id
                        self.assertEqual(media.video_url, original_url)
                        self.assertEqual(retained.headers_str, raw, "Retained task must stay intact")
                    async with AsyncSession(self.engine) as db:
                        persisted = await db.get(DownloadTask, repair_id)
                        self.assertEqual(persisted.headers, VIDEO)
                        self.assertEqual(persisted.audio_headers, audio)
                        expected_subtitle = VIDEO if subtitle_headers is None else subtitle_headers
                        self.assertEqual(persisted.subtitles[0]["headers"], expected_subtitle)
                        self.assertEqual(persisted.status, "PENDING")

    async def test_unsafe_audio_subtitle_and_unknown_storage_create_no_repair(self):
        for identifier, raw, subtitles in (
            (4201, encode_source_headers(VIDEO, {"Cookie": "bad\r\nInjected: value"}), []),
            (4202, encode_source_headers(VIDEO, AUDIO),
             [{"language": "tr", "url": "http://127.0.0.1:9103/sub.vtt", "headers": {"X-Private": "bad"}}]),
            (4203, '{"__streamhome_source_headers__":2}', []),
        ):
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie, media, _ = await self.seed(db, identifier, "movie", raw, subtitles)
                repaired = await queue_media_repair(db, movie, media, "MEDIA_SOURCE_MISSING")
                self.assertIsNone(repaired)
                self.assertEqual(movie.availability, "available")
                self.assertIsNone(movie.preview_task_id)
                tasks = (await db.exec(select(DownloadTask).where(
                    DownloadTask.tmdb_id == identifier, DownloadTask.status == "PENDING"))).all()
                self.assertEqual(tasks, [])

    def test_video_setter_preserves_versioned_audio_and_legacy_shared_semantics(self):
        for audio in (AUDIO, {}):
            task = DownloadTask(headers_str=encode_source_headers(VIDEO, audio))
            task.headers = {"Referer": "https://player.example/"}
            self.assertEqual(task.audio_headers, audio)
            self.assertEqual(task.headers, {"Referer": "https://player.example/"})
        task = DownloadTask(headers_str=json.dumps(VIDEO))
        task.headers = {"Referer": "https://player.example/"}
        self.assertEqual(task.headers, task.audio_headers)
        task = DownloadTask(headers_str='{"__streamhome_source_headers__":2}')
        with self.assertRaises(ValueError):
            task.headers = {}

    async def test_actual_repairs_feed_worker_probe_audio_and_subtitles_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video, audio = await asyncio.to_thread(_create_separate_disguised_hls_fixtures, root)
            (root / "tr.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:00.500\nSynthetic subtitle.\n")
            identifier = 4300
            for kind in ("movie", "tv"):
                for raw, audio_headers, subtitle_headers in (
                    (encode_source_headers(VIDEO, AUDIO), AUDIO, SUBTITLE),
                    (encode_source_headers(VIDEO, {}), {}, {}),
                    (json.dumps(VIDEO), VIDEO, VIDEO),
                ):
                    identifier += 1
                    servers = [SourceServer(root, item) for item in (VIDEO, audio_headers, subtitle_headers)]
                    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
                    for thread in threads:
                        thread.start()
                    try:
                        async with AsyncSession(self.engine, expire_on_commit=False) as db:
                            subtitle = {"language": "tr", "url": servers[2].base + "/tr.vtt"}
                            if raw.startswith('{"__streamhome'):
                                subtitle["headers"] = subtitle_headers
                            movie, media, retained = await self.seed(db, identifier, kind, raw, [subtitle])
                            retained.video_url = servers[0].base + "/" + str(video.relative_to(root))
                            retained.audio_url = servers[1].base + "/" + str(audio.relative_to(root))
                            db.add(retained)
                            await db.commit()
                            repair = await queue_media_repair(db, movie, media, "MEDIA_SOURCE_MISSING")
                            repair_id = repair.id
                        manager = type(worker.queue_manager)()
                        captured = {}
                        async def stop_merge(**kwargs):
                            captured.update(kwargs)
                            return False, IngestionFailure("SOURCE_FORBIDDEN", "Synthetic stop after actual probe and subtitle fetch")
                        with ExitStack() as stack:
                            stack.enter_context(patch.object(worker, "engine", self.engine))
                            stack.enter_context(patch("db.engine", self.engine))
                            stack.enter_context(patch.object(worker, "download_and_merge", stop_merge))
                            stack.enter_context(patch.object(worker, "notify_video_sender", AsyncMock()))
                            stack.enter_context(patch.object(manager, "_reserve_storage", return_value=None))
                            for method in ("fetch_movie_metadata", "fetch_show_metadata", "fetch_episode_metadata"):
                                stack.enter_context(patch.object(worker.tmdb_client, method, AsyncMock(return_value={"title": "Synthetic repair"})))
                            await manager._process_admitted_task(repair_id)
                        self.assertEqual(captured["headers"], VIDEO)
                        self.assertEqual(captured["audio_headers"], audio_headers)
                        self.assertIn("/tr.vtt", servers[2].seen)
                        for server in servers:
                            self.assertTrue(server.seen, (kind, audio_headers))
                            self.assertEqual(server.leaks, [], (kind, audio_headers))
                        async with AsyncSession(self.engine) as db:
                            persisted = await db.get(DownloadTask, repair_id)
                            self.assertTrue(persisted.has_video)
                            self.assertTrue(persisted.has_audio)
                            self.assertEqual(persisted.status, "FAILED")
                    finally:
                        for server in servers:
                            await asyncio.to_thread(server.shutdown)
                            server.server_close()
                        for thread in threads:
                            await asyncio.to_thread(thread.join, 5)


if __name__ == "__main__":
    unittest.main()
