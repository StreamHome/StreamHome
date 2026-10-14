"""Behavioral ownership, SQLite, cloud-track and artwork regressions."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import event
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from models import Episode, Movie, User
from lifecycle.media_publication import MediaMutation
from lifecycle.media_work import media_ownership
from services import ffmpeg, hevc_compressor, media_probe, media_source, queue
from services.rclone import RcloneResult, rclone_service
from services.tmdb import tmdb_client


class CatalogRaceRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="streamhome-catalog-races-")
        self.root = Path(self.directory.name)
        self.previous = settings.MEDIA_DIR, settings.TEMP_DIR
        settings.MEDIA_DIR = str(self.root / "media")
        settings.TEMP_DIR = str(self.root / "temp")
        Path(settings.MEDIA_DIR).mkdir()
        Path(settings.TEMP_DIR).mkdir()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'catalog.db'}", connect_args={"timeout": 0.2})

        @event.listens_for(self.engine.sync_engine, "connect")
        def pragmas(connection, _record):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=200")

        self.patches = [patch.object(db_module, "engine", self.engine), patch.object(queue, "engine", self.engine), patch.object(hevc_compressor, "engine", self.engine)]
        for item in self.patches:
            item.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(self.engine) as db:
            db.add(User(id=1, email="writer@example.test", password_hash="unused"))
            await db.commit()

    async def asyncTearDown(self):
        for item in self.patches:
            item.stop()
        await self.engine.dispose()
        settings.MEDIA_DIR, settings.TEMP_DIR = self.previous
        self.directory.cleanup()

    def movie(self, identifier, *, title="Film", video_url="", source="tmdb_cache"):
        return Movie(id=identifier, title=title, description="", thumbnail_url="", banner_url="", video_url=video_url, duration="1m", release_year=2026, catalog_source=source, availability="available" if video_url else "cached")

    async def test_tmdb_artwork_wait_cannot_overwrite_new_server_metadata(self):
        folder = Path(settings.MEDIA_DIR) / "Movies" / "Film_2026_TMDB_1"
        (folder / ".metadata").mkdir(parents=True)
        (folder / ".metadata/metadata.json").write_text(json.dumps({"catalog_source": "tmdb_cache", "video_url": ""}))
        async with AsyncSession(self.engine) as db:
            db.add(self.movie("m_1"))
            await db.commit()
        entered, release = asyncio.Event(), asyncio.Event()

        async def artwork(_url, destination):
            entered.set()
            await release.wait()
            Path(destination).write_bytes(b"cached-artwork")
            return "cached-artwork"

        item = {"tmdb_id": 1, "type": "movie", "title": "Film", "release_year": 2026, "cast": [{"name": "Fixture"}], "crew": [{"name": "Fixture"}], "keywords": ["fixture"], "director": "Fixture"}
        accepted_payload = {"catalog_source": "server", "video_url": "/media/Movies/Film_2026_TMDB_1/film.mp4", "skip_markers": {"intro": [{"start": 1, "end": 2}]}, "subtitles": [{"fileName": "subtitle_en.vtt"}], "audio_metadata": [{"source": "embedded", "streamIndex": 1}], "sentinel": "accepted-ingestion"}
        with patch.object(ffmpeg, "download_and_cache_metadata_image", artwork):
            task = asyncio.create_task(tmdb_client._cache_media_locally_admitted(item, "https://image.example/poster", "https://image.example/backdrop"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            staging = Path(settings.TEMP_DIR) / "accepted-candidate"
            (staging / ".metadata").mkdir(parents=True)
            (staging / "film.mp4").write_bytes(b"accepted-video")
            (staging / "poster.jpg").write_bytes(b"accepted-poster")
            (staging / ".metadata/metadata.json").write_text(json.dumps(accepted_payload))
            async with media_ownership("m_1"):
                async with MediaMutation("m_1") as mutation:
                    mutation.publish_directory(str(staging), str(folder))
                    async with AsyncSession(self.engine) as db:
                        movie = await db.get(Movie, "m_1")
                        movie.catalog_source = "server"
                        movie.availability = "available"
                        movie.video_url = accepted_payload["video_url"]
                        await mutation.commit(db)
            accepted_metadata = (folder / ".metadata/metadata.json").read_bytes()
            release.set()
            self.assertEqual(await asyncio.wait_for(task, timeout=2), "m_1")
        self.assertEqual((folder / ".metadata/metadata.json").read_bytes(), accepted_metadata)
        self.assertEqual((folder / "poster.jpg").read_bytes(), b"accepted-poster")

    async def test_tmdb_artwork_and_new_parent_entries_sync_before_acceptance(self):
        folder = Path(settings.MEDIA_DIR) / "Movies" / "Durable_2026_TMDB_91"
        synchronized = set()
        real_fsync, real_commit = os.fsync, MediaMutation.commit

        def fsync(descriptor):
            synchronized.add(os.readlink(f"/proc/self/fd/{descriptor}"))
            return real_fsync(descriptor)

        async def download(_url, destination):
            Path(destination).write_bytes(b"durable-artwork")
            return destination

        async def commit(mutation, session):
            self.assertTrue({str(folder / "poster.jpg"), str(folder / "backdrop.jpg"),
                             str(folder), str(folder.parent), str(Path(settings.MEDIA_DIR)),
                             str(Path(settings.MEDIA_DIR).parent)}.issubset(synchronized))
            await real_commit(mutation, session)

        item = {"tmdb_id": 91, "type": "movie", "title": "Durable", "release_year": 2026,
                "cast": [{"name": "Fixture"}], "crew": [{"name": "Fixture"}],
                "keywords": ["fixture"], "director": "Fixture"}
        with patch.object(ffmpeg, "download_and_cache_metadata_image", download), \
                patch.object(os, "fsync", fsync), patch.object(MediaMutation, "commit", commit):
            self.assertEqual(await tmdb_client._cache_media_locally_admitted(
                item, "https://image.example/poster", "https://image.example/backdrop"), "m_91")
        async with AsyncSession(self.engine) as session:
            self.assertIsNotNone(await session.get(Movie, "m_91"))
        self.assertEqual((folder / "poster.jpg").read_bytes(), b"durable-artwork")

    async def test_tmdb_artwork_sync_failure_never_accepts_catalog(self):
        folder = Path(settings.MEDIA_DIR) / "Movies" / "Unsynced_2026_TMDB_92"
        real_fsync = os.fsync

        def fsync(descriptor):
            if os.readlink(f"/proc/self/fd/{descriptor}") == str(folder / "poster.jpg"):
                raise OSError("injected canonical artwork sync failure")
            return real_fsync(descriptor)

        async def download(_url, destination):
            Path(destination).write_bytes(b"unsynced-artwork")
            return destination

        item = {"tmdb_id": 92, "type": "movie", "title": "Unsynced", "release_year": 2026,
                "cast": [{"name": "Fixture"}], "crew": [{"name": "Fixture"}],
                "keywords": ["fixture"], "director": "Fixture"}
        with patch.object(ffmpeg, "download_and_cache_metadata_image", download), patch.object(os, "fsync", fsync):
            with self.assertRaisesRegex(OSError, "artwork sync failure"):
                await tmdb_client._cache_media_locally_admitted(
                    item, "https://image.example/poster", "https://image.example/backdrop")
        async with AsyncSession(self.engine) as session:
            self.assertIsNone(await session.get(Movie, "m_92"))
        self.assertFalse((folder / "poster.jpg").exists())

    async def episode_catalog(self, *, blocked_artwork=False):
        staging = Path(settings.TEMP_DIR) / "ingestion" / "task" / "Series" / "Show_TMDB_2" / "Season_1" / "Episode_1"
        staging.mkdir(parents=True)
        video = staging / "episode.mp4"
        video.write_bytes(b"candidate-video")
        target = Path(settings.MEDIA_DIR) / "Series" / "Show_TMDB_2" / "Season_1" / "Episode_1"
        entered, release = asyncio.Event(), asyncio.Event()

        async def artwork(url, destination):
            if blocked_artwork and Path(destination).name == "thumbnail.jpg":
                entered.set()
                await release.wait()
            if url:
                Path(destination).write_bytes(Path(destination).name.encode())
            return str(destination)

        probe = {"valid_video": True, "quality": "720p", "width": 1280, "height": 720, "codec": "h264", "container": "mp4", "probed_duration": 3, "source_fingerprint": "candidate", "video_fingerprint": "candidate-video", "audio_metadata": [], "analysis": {"quality_provenance": "final_probe"}}
        metadata = {"title": "Show", "releaseYear": 2026, "thumbnailUrl": "https://image.example/poster", "bannerUrl": "https://image.example/backdrop", "episode_detail": {"thumbnailUrl": "https://image.example/still", "title": "First"}}

        async def catalog():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                with patch.object(queue, "download_and_cache_metadata_image", artwork), patch.object(queue, "probe_completed_media", AsyncMock(return_value=probe)):
                    await queue.DownloadQueueManager()._catalog_media(db, 2, "series", 1, 1, metadata, str(video), quality="2160p", catalog_file_path=str(target / video.name))
                    result = await db.get(Episode, "ep_2_s1_e1")
                async with MediaMutation(result.id) as mutation:
                    mutation.publish_directory(str(staging), str(target))
                    await mutation.commit(db)
                return result.id

        task = asyncio.create_task(catalog())
        if blocked_artwork:
            await asyncio.wait_for(entered.wait(), timeout=2)
            async with AsyncSession(self.engine) as writer:
                user = await writer.get(User, 1)
                user.email = "concurrent-writer@example.test"
                await asyncio.wait_for(writer.commit(), timeout=1)
            self.assertFalse(task.done())
            release.set()
        identifier = await asyncio.wait_for(task, timeout=3)
        return identifier, target

    async def test_episode_artwork_wait_does_not_hold_sqlite_writer_lock(self):
        async with AsyncSession(self.engine) as db:
            db.add(self.movie("tv_2", title="Old show", source="server"))
            await db.commit()
        await self.episode_catalog(blocked_artwork=True)
        async with AsyncSession(self.engine) as db:
            self.assertEqual((await db.get(User, 1)).email, "concurrent-writer@example.test")

    async def test_episode_artwork_urls_and_files_survive_publication(self):
        identifier, target = await self.episode_catalog()
        async with AsyncSession(self.engine) as db:
            episode = await db.get(Episode, identifier)
            show = await db.get(Movie, "tv_2")
            self.assertTrue(episode.thumbnail_url.startswith("/media/Series/Show_TMDB_2/Season_1/Episode_1/"))
            for value in (episode.thumbnail_url, show.thumbnail_url, show.banner_url):
                self.assertNotIn("/temp/", value)
                self.assertTrue((Path(settings.MEDIA_DIR) / value.removeprefix("/media/")).is_file(), value)
            self.assertEqual(episode.quality, "720p")
        self.assertEqual((target / "poster.jpg").read_bytes(), b"poster.jpg")
        self.assertEqual((target / "backdrop.jpg").read_bytes(), b"backdrop.jpg")
        self.assertEqual((target / "thumbnail.jpg").read_bytes(), b"thumbnail.jpg")

    async def test_cloud_catalog_rebuild_keeps_episode_artwork_and_measured_duration(self):
        identifier, target = await self.episode_catalog()
        payload = json.loads((target / ".metadata/metadata.json").read_text())
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await db.execute(delete(Episode).where(Episode.id == identifier))
            await db.execute(delete(Movie).where(Movie.id == "tv_2"))
            await db.commit()
            await queue.DownloadQueueManager()._restore_cloud_catalog_entry(db, payload, {"title": "Show", "releaseYear": 2026, "episode_detail": {"duration": "45m"}}, 1, 1)
            await db.commit()
            restored = await db.get(Episode, identifier)
            show = await db.get(Movie, "tv_2")
            for value in (restored.thumbnail_url, show.thumbnail_url, show.banner_url):
                self.assertTrue((Path(settings.MEDIA_DIR) / value.removeprefix("/media/")).is_file(), value)
            self.assertEqual(restored.duration, "3s")
            self.assertEqual(restored.quality, "720p")
            self.assertEqual(restored.probed_duration, 3)

    async def test_cloud_track_removal_preserves_embedded_language_and_stream_identity(self):
        embedded = [{"source": "embedded", "streamIndex": 2, "language": "en", "default": True, "label": "Original English"}]
        files = RcloneResult(0, json.dumps([{"Name": "en.m4a", "Size": 100, "ModTime": "2026-10-13T00:00:00Z"}]))
        with patch.object(rclone_service, "run", AsyncMock(return_value=files)):
            dubbed = await media_probe.probe_cloud_external_audio("isolated:media/film.mp4", embedded)
        with patch.object(rclone_service, "run", AsyncMock(return_value=RcloneResult(0, "[]"))):
            removed = await media_probe.probe_cloud_external_audio("isolated:media/film.mp4", dubbed)
        self.assertEqual(len(dubbed), 2)
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["source"], "embedded")
        self.assertEqual(removed[0]["streamIndex"], 2)

    async def test_cloud_verification_is_not_skipped_when_local_fallback_exists(self):
        previous_storage = settings.STORAGE_ENGINE
        settings.STORAGE_ENGINE = "CLOUD"
        try:
            path = Path(settings.MEDIA_DIR) / "Movies" / "Fallback" / "film.mp4"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"fallback")
            info = media_source.CloudObjectInfo(identity="uploaded-cloud-identity", size=100)
            with patch.object(media_source, "cloud_object_info", AsyncMock(return_value=info)) as cloud_stat:
                source = await media_source.resolve_media_source("/media/Movies/Fallback/film.mp4", require_cloud=True)
            cloud_stat.assert_awaited_once()
            self.assertTrue(source.local_exists)
            self.assertTrue(source.cloud_exists)
        finally:
            settings.STORAGE_ENGINE = previous_storage

    async def test_hevc_skips_cloud_only_and_recently_deferred_candidates(self):
        local = Path(settings.MEDIA_DIR) / "Movies" / "Local" / "film.mp4"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local-video")
        async with AsyncSession(self.engine) as db:
            db.add(self.movie("m_1", video_url="/media/Movies/Cloud/film.mp4", source="server"))
            db.add(self.movie("m_2", video_url="/media/Movies/Local/film.mp4", source="server"))
            db.add(self.movie("m_3", video_url="/media/Movies/Local/film.mp4", source="server"))
            await db.commit()
        compressor = hevc_compressor.HEVCCompressorWorker()
        model, identifier, _path = await compressor._next_candidate()
        self.assertIs(model, Movie)
        self.assertEqual(identifier, "m_2")
        _model, later_identifier, _path = await compressor._next_candidate()
        self.assertEqual(later_identifier, "m_3")


if __name__ == "__main__":
    unittest.main()
