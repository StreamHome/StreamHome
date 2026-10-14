"""Behavioral regressions for pacing and enrichment metadata ownership."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from lifecycle import media_work
from lifecycle.coordination import RuntimeCoordinator, use_runtime_coordinator_for_testing
from lifecycle.media_publication import MediaMutation
from models import Episode, Movie, User
from services import vibe_analysis
from services.tmdb import tmdb_client


class VibeOwnershipRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="streamhome-vibe-ownership-")
        self.root = Path(self.directory.name)
        self.previous = settings.MEDIA_DIR, settings.TEMP_DIR
        settings.MEDIA_DIR = str(self.root / "media")
        settings.TEMP_DIR = str(self.root / "temp")
        Path(settings.MEDIA_DIR).mkdir()
        Path(settings.TEMP_DIR).mkdir()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.root / 'catalog.db'}",
            connect_args={"timeout": 0.2},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def pragmas(connection, _record):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=200")

        self.patches = [patch.object(vibe_analysis, "engine", self.engine),
                        patch.object(media_work, "_locks", {})]
        for item in self.patches:
            item.start()
        self.coordinator = use_runtime_coordinator_for_testing(RuntimeCoordinator())
        self.coordinator.__enter__()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(self.engine) as db:
            db.add(User(id=1, email="writer@example.test", password_hash="unused"))
            await db.commit()

    async def asyncTearDown(self):
        self.coordinator.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        settings.MEDIA_DIR, settings.TEMP_DIR = self.previous
        self.directory.cleanup()

    def portable(self, folder: Path, *, accepted="original", quality="720p"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".metadata").mkdir(exist_ok=True)
        payload = {
            "catalog_source": "server", "quality": quality,
            "source_fingerprint": accepted, "probed_duration": 120,
            "skip_markers": {"intro": [{"start": 1, "end": 3}]},
            "subtitles": [{"language": "en", "fileName": "subtitle_en.vtt"}],
            "audio_metadata": [{"source": "embedded", "streamIndex": 2,
                                "language": "en", "label": accepted}],
            "analysis": {"quality_provenance": "final_probe"},
            "sentinel": accepted,
        }
        (folder / ".metadata/metadata.json").write_text(json.dumps(payload))
        (folder / "film.mp4").write_bytes(accepted.encode())
        (folder / "subtitle_en.vtt").write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:30.000\n"
            + " ".join(["dialogue"] * 40)
            + "\n\n00:00:31.000 --> 00:01:00.000\n"
            + " ".join(["conversation"] * 40)
            + "\n\n00:01:01.000 --> 00:01:40.000\n"
            + " ".join(["narration"] * 40) + "\n",
            encoding="utf-8",
        )
        return payload

    def movie(self, identifier, *, video_url="", media_type="movie"):
        return Movie(id=identifier, title="Fixture", description="", thumbnail_url="",
                     banner_url="", video_url=video_url, duration="2m", release_year=2026,
                     tmdb_id=int(identifier.split("_")[1]), type=media_type,
                     quality="720p", probed_duration=120)

    async def seed_movie(self):
        folder = Path(settings.MEDIA_DIR) / "Movies" / "Fixture_TMDB_1"
        payload = self.portable(folder)
        async with AsyncSession(self.engine) as db:
            movie = self.movie("m_1", video_url="/media/Movies/Fixture_TMDB_1/film.mp4")
            movie.subtitles = payload["subtitles"]
            movie.audio_metadata = payload["audio_metadata"]
            movie.skip_markers = payload["skip_markers"]
            movie.source_fingerprint = payload["source_fingerprint"]
            db.add(movie)
            await db.commit()
        return folder, payload

    def paused_metadata_writer(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = vibe_analysis._update_metadata_file

        def write(folder, entity):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("metadata test barrier was not released")
            original(folder, entity)

        return entered, release, patch.object(vibe_analysis, "_update_metadata_file", write)

    async def publish_replacement(self, folder: Path):
        staging = Path(settings.TEMP_DIR) / "replacement"
        payload = self.portable(staging, accepted="new-accepted", quality="1080p")
        payload["skip_markers"] = {"credits": [{"start": 110, "end": 119}]}
        (staging / ".metadata/metadata.json").write_text(json.dumps(payload))
        async with MediaMutation("m_1") as mutation:
            mutation.publish_directory(str(staging), str(folder))
            async with AsyncSession(self.engine) as db:
                movie = await db.get(Movie, "m_1")
                movie.quality = payload["quality"]
                movie.audio_metadata = payload["audio_metadata"]
                movie.skip_markers = payload["skip_markers"]
                movie.source_fingerprint = payload["source_fingerprint"]
                movie.vibe_analysis_status = None
                movie.vibe_analysis_version = 0
                movie.dialogue_wpm = None
                await mutation.commit(db)
        return payload

    async def analysis_thread_race(self, *, cancel=False):
        folder, original_payload = await self.seed_movie()
        entered, release, paused = self.paused_metadata_writer()
        acquired = asyncio.Event()

        async def publisher():
            async with media_work.media_ownership("m_1"):
                acquired.set()
                return await self.publish_replacement(folder)

        with paused:
            analysis = asyncio.create_task(vibe_analysis.VibeAnalysisManager().analyze_entity("movie", "m_1"))
            publication = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(media_work.media_lock("m_1").locked())
                publication = asyncio.create_task(publisher())
                await asyncio.sleep(0)
                self.assertFalse(acquired.is_set())
                if cancel:
                    analysis.cancel()
                    await asyncio.sleep(0.03)
                    self.assertFalse(analysis.done(), "cancellation must wait for the metadata thread")
                    self.assertFalse(acquired.is_set())
                self.assertEqual(json.loads((folder / ".metadata/metadata.json").read_text()), original_payload)
            finally:
                release.set()
            if cancel:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(analysis, 2)
            else:
                metrics = await asyncio.wait_for(analysis, 2)
                self.assertEqual(metrics.word_count, 120)
            accepted_payload = await asyncio.wait_for(publication, 2)
        self.assertEqual(json.loads((folder / ".metadata/metadata.json").read_text()), accepted_payload)
        async with AsyncSession(self.engine) as db:
            movie = await db.get(Movie, "m_1")
            self.assertEqual(movie.quality, "1080p")
            self.assertEqual(movie.source_fingerprint, "new-accepted")
            self.assertEqual(movie.audio_metadata, accepted_payload["audio_metadata"])
            self.assertEqual(movie.skip_markers, accepted_payload["skip_markers"])
            self.assertIsNone(movie.dialogue_wpm)

    async def test_analysis_keeps_ownership_until_metadata_thread_finishes(self):
        await self.analysis_thread_race()

    async def test_cancelled_analysis_keeps_ownership_until_metadata_thread_finishes(self):
        await self.analysis_thread_race(cancel=True)

    async def test_episode_analysis_also_holds_parent_aggregate_ownership(self):
        folder = Path(settings.MEDIA_DIR) / "Series" / "Show_TMDB_2" / "Season_1" / "Episode_1"
        payload = self.portable(folder)
        async with AsyncSession(self.engine) as db:
            db.add(self.movie("tv_2", media_type="series"))
            episode = Episode(id="ep_2_s1_e1", movie_id="tv_2", season_number=1,
                              episode_number=1, title="First", description="", thumbnail_url="",
                              video_url="/media/Series/Show_TMDB_2/Season_1/Episode_1/film.mp4",
                              duration="2m", probed_duration=120, quality="720p")
            episode.subtitles = payload["subtitles"]
            episode.audio_metadata = payload["audio_metadata"]
            db.add(episode)
            await db.commit()
        entered, release, paused = self.paused_metadata_writer()
        parent_acquired = asyncio.Event()

        async def parent_writer():
            async with media_work.media_ownership("tv_2"):
                parent_acquired.set()

        with paused:
            analysis = asyncio.create_task(vibe_analysis.VibeAnalysisManager().analyze_entity("episode", "ep_2_s1_e1"))
            writer = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                writer = asyncio.create_task(parent_writer())
                await asyncio.sleep(0)
                self.assertFalse(parent_acquired.is_set())
                self.assertTrue(media_work.media_lock("tv_2").locked())
            finally:
                release.set()
            metrics = await asyncio.wait_for(analysis, 2)
            await asyncio.wait_for(writer, 2)
        current = json.loads((folder / ".metadata/metadata.json").read_text())
        for key, value in payload.items():
            self.assertEqual(current[key], value)
        self.assertEqual(current["dialogue_word_count"], 120)
        async with AsyncSession(self.engine) as db:
            show = await db.get(Movie, "tv_2")
            self.assertEqual(show.dialogue_wpm, metrics.wpm)
            self.assertEqual(show.dialogue_word_count, 120)
            self.assertEqual((await db.get(Episode, "ep_2_s1_e1")).audio_metadata, payload["audio_metadata"])

    async def test_backfill_provider_wait_reloads_accepted_catalog_and_portable_metadata(self):
        folder, _payload = await self.seed_movie()
        entered, release = asyncio.Event(), asyncio.Event()
        details = {"crew": [{"name": "Fixture Director", "roles": ["Director"]}],
                   "keywords": ["fixture keyword"], "director": "Fixture Director",
                   "tropeVectors": [{"id": "fixture-trope", "confidence": 0.9}]}

        async def provider(_tmdb_id):
            entered.set()
            await release.wait()
            return details

        with patch.object(tmdb_client, "api_key", "synthetic-test-key"), \
             patch.object(tmdb_client, "fetch_movie_metadata", provider):
            backfill = asyncio.create_task(vibe_analysis.backfill_catalog_vibes())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                async with AsyncSession(self.engine) as writer:
                    user = await writer.get(User, 1)
                    user.email = "concurrent-writer@example.test"
                    await asyncio.wait_for(writer.commit(), 1)
                async with media_work.media_ownership("m_1"):
                    accepted = await self.publish_replacement(folder)
                self.assertFalse(backfill.done())
            finally:
                release.set()
            self.assertEqual(await asyncio.wait_for(backfill, 2), 1)
        current = json.loads((folder / ".metadata/metadata.json").read_text())
        for key, value in accepted.items():
            self.assertEqual(current[key], value)
        self.assertEqual(current["crew"], details["crew"])
        self.assertEqual(current["keywords"], details["keywords"])
        async with AsyncSession(self.engine) as db:
            movie = await db.get(Movie, "m_1")
            self.assertEqual(movie.quality, "1080p")
            self.assertEqual(movie.source_fingerprint, "new-accepted")
            self.assertEqual(movie.skip_markers, accepted["skip_markers"])
            self.assertEqual(movie.audio_metadata, accepted["audio_metadata"])
            self.assertEqual(movie.crew, details["crew"])
            self.assertEqual((await db.get(User, 1)).email, "concurrent-writer@example.test")

    async def test_series_artwork_pointer_does_not_grant_child_portable_ownership(self):
        folder = Path(settings.MEDIA_DIR) / "Series" / "Show_TMDB_2" / "Season_1" / "Episode_1"
        self.portable(folder, accepted="child-only")
        path = folder / ".metadata/metadata.json"
        original = path.read_bytes()
        async with AsyncSession(self.engine) as db:
            show = self.movie("tv_2", media_type="series")
            show.local_thumbnail_url = "/media/Series/Show_TMDB_2/Season_1/Episode_1/poster.jpg"
            db.add(show)
            await db.commit()

        async def provider(_tmdb_id):
            return {"crew": [{"name": "Show Creator", "roles": ["Creator"]}],
                    "keywords": ["series-only"], "director": "Show Creator"}

        with patch.object(tmdb_client, "api_key", "synthetic-test-key"), \
             patch.object(tmdb_client, "fetch_show_metadata", provider):
            self.assertEqual(await vibe_analysis.backfill_catalog_vibes(), 1)
        self.assertEqual(path.read_bytes(), original)
        async with AsyncSession(self.engine) as db:
            self.assertEqual((await db.get(Movie, "tv_2")).director, "Show Creator")


if __name__ == "__main__":
    unittest.main()
