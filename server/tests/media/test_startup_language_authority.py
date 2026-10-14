"""Task history cannot relabel a genuine accepted replacement during startup."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from db import get_session
from lifecycle.media_publication import recover_media_operations
from models import AuthSession, DownloadTask, Episode, MediaOperationReceipt, Movie, MovieResponse, Profile, User
from routes import playback, queue as queue_routes
from routes.auth import get_current_user
from services import queue
from services.audio_extractor import primary_embedded_audio_index, repair_completed_ingestion_languages
from services.media_probe import probe_media_stream
from services.media_source import playback_source_fingerprint, resolve_media_source
from services.playback_prep import PlaybackPrepService
from tests.media import test_analysis_provenance as analysis_fixture
from tests.media import test_language_refresh as language_fixture


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
class StartupLanguageAuthority(unittest.IsolatedAsyncioTestCase):
    async def accept(self, fixture, *, task_id, media_type, submitted, frequency, created_at):
        source = fixture.root / f"{task_id}.mkv"
        await language_fixture.make_media(source, frequency=frequency)
        measured = await probe_media_stream(str(source))
        async with AsyncSession(fixture.engine) as session:
            session.add(DownloadTask(
                id=task_id, tmdb_id=801, media_type=media_type,
                season=1 if media_type != "movie" else None,
                episode=1 if media_type != "movie" else None,
                video_url=f"https://source.example/{task_id}", language=submitted,
                quality="720p", status="DOWNLOADING", created_at=created_at,
            ))
            await session.commit()

        async def download(**kwargs):
            shutil.copy2(source, kwargs["output_path"])
            return True, None

        metadata = {"title": "Film", "releaseYear": 2026, "originalLanguage": "tr"}
        with patch.object(queue, "validate_url", AsyncMock()), \
            patch.object(queue, "probe_media_stream", AsyncMock(return_value=measured)), \
            patch.object(queue, "download_and_merge", download), \
            patch.object(queue.vibe_analysis_manager, "enqueue", AsyncMock()), \
            patch.object(queue.tmdb_client, "fetch_movie_metadata", AsyncMock(return_value=metadata)), \
            patch.object(queue.tmdb_client, "fetch_show_metadata", AsyncMock(return_value=metadata)), \
            patch.object(fixture.manager, "MINIMUM_TASK_RESERVATION_BYTES", 1024**2), \
            patch.object(fixture.manager, "STORAGE_SAFETY_MARGIN_BYTES", 1024**2), \
            patch.object(fixture.manager, "ESTIMATED_MEDIA_BYTES_PER_SECOND", 1024**2):
            await fixture.manager._process_admitted_task(task_id)
        async with AsyncSession(fixture.engine) as session:
            task = await session.get(DownloadTask, task_id)
            self.assertEqual(task.status, "COMPLETED", task.error_message)

    async def snapshot(self, fixture, media_type):
        model, identifier = (Movie, "m_801") if media_type == "movie" else (Episode, "ep_801_s1_e1")
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(model, identifier)
            parent = item if media_type == "movie" else await session.get(Movie, "tv_801")
            source = await resolve_media_source(item.video_url, check_cloud=False)
            metadata = source.local_path.parent / ".metadata"
            return {
                "video_url": item.video_url,
                "video_hash": hashlib.sha256(source.local_path.read_bytes()).hexdigest(),
                "video_fingerprint": source.video_fingerprint,
                "source_fingerprint": item.source_fingerprint,
                "audio": copy.deepcopy(item.audio_metadata),
                "languages": list(item.languages),
                "original_language": parent.original_language,
                "analysis": json.loads(item.analysis_metadata_str),
                "portable": {path.name: path.read_bytes() for path in metadata.glob("metadata*.json")},
                "receipt_count": len((await session.exec(select(MediaOperationReceipt))).all()),
            }

    async def assert_consumers(self, fixture, media_type, language, marked):
        model, identifier = (Movie, "m_801") if media_type == "movie" else (Episode, "ep_801_s1_e1")
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(model, identifier)
            parent = item if media_type == "movie" else await session.get(Movie, "tv_801")
            audio = item.audio_metadata
            primary = audio[primary_embedded_audio_index(audio)]
            self.assertEqual((primary["language"], primary["label"], primary["streamIndex"]), (language, language.upper(), 2))
            self.assertEqual((audio[0]["language"], audio[0]["streamIndex"]), ("es", 1))
            self.assertNotIn("languageOverride", audio[0])
            if marked:
                self.assertEqual(primary["languageOverride"], {"source": "sender", "language": language})
            else:
                self.assertNotIn("languageOverride", primary)
            external = [track for track in audio if track.get("source") == "external"]
            self.assertEqual(len(external), 1)
            self.assertEqual((external[0]["language"], external[0]["fileName"]), ("tr", "tr.m4a"))
            self.assertNotIn("languageOverride", external[0])
            source = await resolve_media_source(item.video_url, check_cloud=False)
            self.assertEqual(json.loads(item.analysis_metadata_str)["video_fingerprint"], source.video_fingerprint)
            self.assertEqual(item.source_fingerprint, playback_source_fingerprint(source, audio))
            for path in (source.local_path.parent / ".metadata").glob("metadata*.json"):
                portable = json.loads(path.read_text())
                self.assertEqual(portable["audio_metadata"], audio)
                self.assertEqual(portable["languages"], item.languages)
                self.assertEqual(portable["language"], language)
                self.assertEqual(portable["original_language"], parent.original_language)
                manifest_audio = [stream for stream in portable["stream_manifest"] if stream["type"] == "audio"]
                self.assertEqual([stream["language"] for stream in manifest_audio], ["es", "en"])
            response = MovieResponse.from_db(parent, [item] if media_type != "movie" else None)
            self.assertEqual(response.languages if media_type == "movie" else response.episodes[0].languages, item.languages)

        service = PlaybackPrepService()
        user = User(id=801, email="startup-language@example.test", password_hash="unused")
        auth = AuthSession(id="startup-language-session", user_id=user.id,
            created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600)
        async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
            session.add(user)
            session.add(auth)
            session.add(Profile(id="startup-language-profile", name="Startup language"))
            await session.commit()

        async def sessions():
            async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
                yield session

        app = FastAPI()

        @app.middleware("http")
        async def attach(request: Request, call_next):
            request.state.auth_session = auth
            return await call_next(request)

        app.include_router(playback.router)
        app.dependency_overrides[get_session] = sessions
        app.dependency_overrides[get_current_user] = lambda: user
        with patch.object(playback, "playback_prep_service", service), patch.object(service, "prepare", AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://startup-language.test") as client:
                payload = {"movieId": "m_801" if media_type == "movie" else "tv_801", "profileId": "startup-language-profile"}
                if media_type != "movie":
                    payload["episodeId"] = identifier
                response = await client.post("/api/playback/runs", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        tracks = response.json()["tracks"]
        primary = next(track for track in tracks if track["source"] == "embedded" and track["streamIndex"] == 2)
        self.assertEqual((primary["language"], primary["label"]), (language, language.upper()))
        self.assertEqual(len(tracks), 3)

    async def replacement_then_startup(self, *, media_type, submitted="tr", legacy=False, unbound=False):
        fixture = analysis_fixture.CatalogAnalysisProvenance()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        old_language = "tr" if submitted is None else "en"
        await self.accept(fixture, task_id="older-language-task", media_type=media_type,
            submitted=old_language, frequency=330, created_at="2026-10-12T00:00:00")
        old = await self.snapshot(fixture, media_type)
        await self.accept(fixture, task_id="current-language-task", media_type=media_type,
            submitted=submitted, frequency=1234, created_at="2026-10-13T00:00:00")
        accepted = await self.snapshot(fixture, media_type)
        self.assertEqual(accepted["video_url"], old["video_url"], "Replacement retains catalog identity/path")
        self.assertNotEqual(accepted["video_hash"], old["video_hash"], "Both actual queue acceptances must publish distinct originals")
        self.assertNotEqual(accepted["video_fingerprint"], old["video_fingerprint"])
        self.assertEqual(accepted["receipt_count"], 2)

        model, identifier = (Movie, "m_801") if media_type == "movie" else (Episode, "ep_801_s1_e1")
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(model, identifier)
            source = await resolve_media_source(item.video_url, check_cloud=False)
            tracks = item.audio_metadata
            if legacy:
                tracks[primary_embedded_audio_index(tracks)].pop("languageOverride")
                item.audio_metadata = tracks
            analysis = json.loads(item.analysis_metadata_str)
            analysis["measurement_version"] = 1
            if unbound:
                analysis.pop("video_fingerprint", None)
                analysis.pop("source_fingerprint", None)
                item.source_fingerprint = None
            item.analysis_metadata_str = json.dumps(analysis)
            for path in (source.local_path.parent / ".metadata").glob("metadata*.json"):
                portable = json.loads(path.read_text())
                portable["audio_metadata"] = tracks
                portable["analysis"] = analysis
                if unbound:
                    portable.pop("video_fingerprint", None)
                    portable.pop("source_fingerprint", None)
                path.write_text(json.dumps(portable))
            await session.commit()
        if not unbound:
            await language_fixture.make_external(source.local_path)

        with patch.object(queue_routes, "engine", fixture.engine), patch.object(queue_routes, "queue_manager", fixture.manager):
            deleted = await queue_routes.delete_download("current-language-task", access=None)
        self.assertEqual(deleted["status"], "success")
        async with AsyncSession(fixture.engine) as session:
            self.assertIsNone(await session.get(DownloadTask, "current-language-task"))
            self.assertEqual((await session.get(DownloadTask, "older-language-task")).status, "COMPLETED")
        deleted_snapshot = await self.snapshot(fixture, media_type)
        self.assertEqual(deleted_snapshot["video_hash"], accepted["video_hash"])
        self.assertEqual(deleted_snapshot["video_fingerprint"], accepted["video_fingerprint"])

        # Use the normal startup recovery writer sequence. Unbound legacy intent
        # is deliberately ambiguous, so its control exercises only the history
        # hook; independent measurement may legitimately recover physical tags.
        if not unbound:
            await recover_media_operations()
            await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        before = await self.snapshot(fixture, media_type)
        cache = Path(fixture.root / "temp" / "playback_cache") / identifier / "accepted-cache"
        cache.mkdir(parents=True)
        sentinel = cache / "sentinel"
        sentinel.write_bytes(b"accepted-playback-cache")
        with patch.object(playback.playback_prep_service, "cache_dir", cache.parent.parent), \
            patch.object(playback.playback_prep_service, "cancel_media") as cancel:
            self.assertEqual(await repair_completed_ingestion_languages(), 0)
            self.assertEqual(await repair_completed_ingestion_languages(), 0, "Repeated startup cannot infer a legacy correction")
        cancel.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"accepted-playback-cache")
        self.assertEqual(await self.snapshot(fixture, media_type), before,
            "History repair must preserve DB, portable aliases, original bytes and source identities")
        self.assertEqual(before["video_hash"], accepted["video_hash"])
        self.assertEqual(before["original_language"], accepted["original_language"])
        if not unbound:
            await self.assert_consumers(fixture, media_type, submitted or "en", bool(submitted and not legacy))

    async def test_movie_replacement_marker_survives_actual_new_task_deletion_and_startup(self):
        await self.replacement_then_startup(media_type="movie")

    async def test_episode_replacement_marker_and_show_language_survive_startup(self):
        await self.replacement_then_startup(media_type="series")

    async def test_movie_unmarked_accepted_label_survives_without_minting_provenance(self):
        await self.replacement_then_startup(media_type="movie", legacy=True)

    async def test_episode_unmarked_accepted_label_survives_without_minting_provenance(self):
        await self.replacement_then_startup(media_type="series", legacy=True)

    async def test_movie_without_submission_keeps_physical_primary_despite_older_task(self):
        await self.replacement_then_startup(media_type="movie", submitted=None)

    async def test_episode_without_submission_keeps_physical_primary_despite_older_task(self):
        await self.replacement_then_startup(media_type="series", submitted=None)

    async def test_unbound_legacy_movie_is_deferred_without_guessing_labels_or_provenance(self):
        await self.replacement_then_startup(media_type="movie", legacy=True, unbound=True)

    async def test_unbound_legacy_episode_is_deferred_without_changing_show_language(self):
        await self.replacement_then_startup(media_type="series", legacy=True, unbound=True)


class RetiredStartupLanguageRepair(unittest.IsolatedAsyncioTestCase):
    async def test_compatibility_hook_does_not_open_a_database_or_touch_portable_files(self):
        with patch.object(db_module, "engine", object()), \
            patch("sqlmodel.ext.asyncio.session.AsyncSession", side_effect=AssertionError("Task history has no write authority")), \
            patch.object(Path, "write_text", side_effect=AssertionError("No inferred portable correction")), \
            patch.object(shutil, "rmtree", side_effect=AssertionError("No inferred playback invalidation")):
            self.assertEqual(await repair_completed_ingestion_languages(), 0)


if __name__ == "__main__":
    unittest.main()
