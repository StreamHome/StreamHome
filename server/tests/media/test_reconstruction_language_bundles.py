"""Real local recovery keeps only independently bound primary audio bundles."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, Request
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db import get_session
from lifecycle.media_work import media_ownership
from models import AuthSession, DownloadTask, Episode, MediaOperationReceipt, Movie, MovieResponse, Profile, User
from routes import playback
from routes.auth import get_current_user
from services import queue
from services.audio_extractor import primary_embedded_audio_index
from services.media_probe import probe_completed_media, probe_media_stream
from services.media_source import playback_source_fingerprint, resolve_media_source
from services.playback_prep import PlaybackPrepService
from tests.media import test_analysis_provenance as analysis_fixture
from tests.media import test_language_refresh as language_fixture


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
class ReconstructionLanguageBundles(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, *, episode=False, legacy=False, submitted="tr"):
        fixture = analysis_fixture.CatalogAnalysisProvenance()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        source = fixture.root / "input.mkv"
        await language_fixture.make_media(source)
        measured = await probe_media_stream(str(source))
        media_type = "tv" if episode else "movie"
        self.model = Episode if episode else Movie
        self.identity = "ep_801_s1_e1" if episode else "m_801"
        self.movie_id = "tv_801" if episode else self.identity
        async with AsyncSession(fixture.engine) as session:
            session.add(DownloadTask(id="reconstruction-task", tmdb_id=801, media_type=media_type,
                season=1 if episode else None, episode=1 if episode else None,
                video_url="https://source.example/fixture", language=submitted, quality="720p",
                status="DOWNLOADING", created_at="2026-10-13T00:00:00"))
            await session.commit()

        async def download(**kwargs):
            shutil.copy2(source, kwargs["output_path"])
            return True, None

        catalog = {"title": "Film", "releaseYear": 2026, "originalLanguage": "tr"}
        with patch.object(queue, "validate_url", AsyncMock()), \
            patch.object(queue, "probe_media_stream", AsyncMock(return_value=measured)), \
            patch.object(queue, "download_and_merge", download), \
            patch.object(queue.vibe_analysis_manager, "enqueue", AsyncMock()), \
            patch.object(queue.tmdb_client, "fetch_movie_metadata", AsyncMock(return_value=catalog)), \
            patch.object(queue.tmdb_client, "fetch_show_metadata", AsyncMock(return_value=catalog)), \
            patch.object(fixture.manager, "MINIMUM_TASK_RESERVATION_BYTES", 1024**2), \
            patch.object(fixture.manager, "STORAGE_SAFETY_MARGIN_BYTES", 1024**2), \
            patch.object(fixture.manager, "ESTIMATED_MEDIA_BYTES_PER_SECOND", 1024**2):
            await fixture.manager._process_admitted_task("reconstruction-task")
        async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
            task = await session.get(DownloadTask, "reconstruction-task")
            self.assertEqual(task.status, "COMPLETED", task.error_message)
            item = await session.get(self.model, self.identity)
            selected = await resolve_media_source(item.video_url, check_cloud=False)
            self.assertEqual(json.loads(item.analysis_metadata_str)["video_fingerprint"], selected.video_fingerprint)
            primary = item.audio_metadata[primary_embedded_audio_index(item.audio_metadata)]
            self.assertEqual(primary["language"], submitted or "en")
            if submitted:
                self.assertEqual(primary["languageOverride"], {"source": "sender", "language": submitted})
        await language_fixture.make_external(selected.local_path)
        # Accept the independent dubbing sidecar through the actual measurement
        # writer before deliberately damaging only the portable recovery schema.
        async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
            item = await session.get(self.model, self.identity)
            async with media_ownership(item.id):
                await playback.ensure_source_metadata(session, item, selected)
        metadata = selected.local_path.parent / ".metadata" / "metadata.json"
        if legacy:
            async with AsyncSession(fixture.engine) as session:
                item = await session.get(self.model, self.identity)
                audio = item.audio_metadata
                audio[primary_embedded_audio_index(audio)].pop("languageOverride")
                item.audio_metadata = audio
                portable = json.loads(metadata.read_text())
                portable["audio_metadata"] = audio
                metadata.write_text(json.dumps(portable))
                await session.commit()
        self.expected_url = selected.catalog_path
        self.video_fingerprint = selected.video_fingerprint
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(self.model, self.identity)
            self.assertEqual(json.loads(item.analysis_metadata_str)["video_fingerprint"], self.video_fingerprint)
            self.assertEqual(item.source_fingerprint, playback_source_fingerprint(selected, item.audio_metadata))
            self.receipt_ids = {receipt.operation_id for receipt in (await session.exec(select(MediaOperationReceipt))).all()}
            self.external_before = copy.deepcopy(next(track for track in item.audio_metadata if track.get("source") == "external"))
        return fixture, selected, metadata

    async def assert_recovered(self, fixture, metadata, *, language="tr", marked=True):
        portable = json.loads(metadata.read_text())
        async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
            item = await session.get(self.model, self.identity)
            language_fixture.PrimaryLanguageRefresh().assert_audio(item, portable, language=language, marked=marked)
            self.assertEqual(item.video_url, self.expected_url)
            self.assertIsNone(item.preview_task_id)
            selected = await resolve_media_source(item.video_url, check_cloud=False)
            self.assertTrue(selected.available and selected.local_exists)
            self.assertEqual(selected.video_fingerprint, self.video_fingerprint)
            self.assertEqual(item.source_fingerprint, playback_source_fingerprint(selected, item.audio_metadata))
            self.assertEqual(portable["source_fingerprint"], item.source_fingerprint)
            self.assertEqual(portable["video_fingerprint"], selected.video_fingerprint)
            self.assertEqual(json.loads(item.analysis_metadata_str)["measurement_version"], 2)
            self.assertEqual(json.loads(item.analysis_metadata_str)["sender_quality"], "720p")
            external = next(track for track in item.audio_metadata if track.get("source") == "external")
            for field in ("source", "index", "streamIndex", "fileName", "fileSize", "modifiedAt", "language", "label",
                "default", "codec", "duration", "startTime", "timeBase", "timelineOffset", "timelineOffsetSource"):
                self.assertEqual(external[field], self.external_before[field], field)
            self.assertEqual({receipt.operation_id for receipt in (await session.exec(select(MediaOperationReceipt))).all()}, self.receipt_ids)
            catalog = await session.get(Movie, self.movie_id)
            self.assertEqual((catalog.catalog_source, catalog.availability), ("server", "available"))
            response = MovieResponse.from_db(catalog, [item] if self.model is Episode else None)
            contract = response.episodes[0] if self.model is Episode else response
            self.assertEqual(contract.video_url, self.expected_url)
            self.assertEqual(contract.languages, item.languages)
            self.assertEqual(contract.analysis, portable["analysis"])
        # Exercise the actual protected playback HTTP consumer after inspecting
        # reconstruction's own DB/portable writes so lazy repair cannot mask it.
        service = PlaybackPrepService()
        self.enterContext(patch.object(playback, "playback_prep_service", service))
        self.enterContext(patch.object(service, "prepare", AsyncMock()))
        self.addAsyncCleanup(service.quiesce_for_update, timeout=10)
        user = User(id=901, email="reconstruction@example.test", password_hash="unused")
        auth = AuthSession(id="reconstruction-session", user_id=901, created_at=time.time(),
            last_seen_at=time.time(), expires_at=time.time() + 3600)
        async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
            session.add(user)
            session.add(auth)
            session.add(Profile(id="reconstruction-profile", name="Reconstruction"))
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
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://reconstruction.test") as client:
            body = {"movieId": self.movie_id, "profileId": "reconstruction-profile"}
            if self.model is Episode:
                body["episodeId"] = self.identity
            response = await client.post("/api/playback/runs", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        tracks = response.json()["tracks"]
        self.assertEqual(len(tracks), 3)
        primary = next(track for track in tracks if track["source"] == "embedded" and track["streamIndex"] == 2)
        self.assertEqual((primary["language"], primary["label"], primary["default"]), (language, language.upper(), True))
        self.assertEqual(next(track for track in tracks if track["source"] == "embedded" and track["streamIndex"] == 1)["language"], "es")
        external = next(track for track in tracks if track["source"] == "external")
        self.assertEqual(external["language"], "tr")
        self.assertTrue(external["directUrl"].startswith("/api/playback/source/"))

    async def recover_missing_portable(self, *, episode=False, legacy=False, partial=False, source_only=False):
        fixture, selected, metadata = await self.fixture(episode=episode, legacy=legacy)
        portable = json.loads(metadata.read_text())
        if partial:
            portable["audio_metadata"] = [track for track in portable["audio_metadata"]
                if track.get("source") == "external" or track.get("streamIndex") == 1]
        else:
            portable.pop("audio_metadata")
        # Generic portable/catalog language is deliberately conflicting; it is
        # not independent accepted primary-stream authority.
        portable["language"] = "fr"
        metadata.write_text(json.dumps(portable))
        if source_only:
            async with AsyncSession(fixture.engine) as session:
                item = await session.get(self.model, self.identity)
                analysis = json.loads(item.analysis_metadata_str)
                analysis.pop("video_fingerprint")
                item.analysis_metadata_str = json.dumps(analysis)
                await session.commit()
        if source_only:
            real_probe = queue.probe_completed_media
            observed = []

            async def probe_with_independent_writer(path):
                # A real second connection must be able to commit while the
                # recovery writer is doing physical media inspection.
                async def write():
                    async with AsyncSession(fixture.engine) as session:
                        session.add(Profile(id="probe-writer", name="Probe Writer"))
                        await session.commit()
                await asyncio.wait_for(write(), timeout=5)
                observed.append(path)
                return await real_probe(path)

            with patch.object(queue, "probe_completed_media", probe_with_independent_writer):
                await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
            self.assertEqual(observed, [str(selected.local_path)])
            async with AsyncSession(fixture.engine) as session:
                self.assertIsNotNone(await session.get(Profile, "probe-writer"))
        else:
            await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        await self.assert_recovered(fixture, metadata, marked=not legacy)

    async def test_movie_bound_db_sender_survives_missing_portable_audio(self):
        await self.recover_missing_portable()

    async def test_episode_bound_db_sender_survives_missing_portable_audio(self):
        await self.recover_missing_portable(episode=True)

    async def test_movie_bound_unmarked_db_label_survives_partial_portable_audio(self):
        await self.recover_missing_portable(legacy=True, partial=True)

    async def test_episode_bound_unmarked_db_label_survives_partial_portable_audio(self):
        await self.recover_missing_portable(episode=True, legacy=True, partial=True)

    async def test_db_full_source_proof_independently_survives_absent_portable_audio(self):
        await self.recover_missing_portable(source_only=True)

    async def test_current_portable_video_cannot_authorize_stale_db_bundle(self):
        fixture, selected, metadata = await self.fixture()
        await language_fixture.make_media(selected.local_path, frequency=1234)
        selected = await resolve_media_source(selected.catalog_path, check_cloud=False)
        self.assertNotEqual(selected.video_fingerprint, self.video_fingerprint)
        self.video_fingerprint = selected.video_fingerprint
        portable = json.loads(metadata.read_text())
        portable.update(await probe_completed_media(str(selected.local_path)))
        portable["language"] = "tr"
        portable["analysis"]["measurement_version"] = 1
        portable["analysis"]["sender_quality"] = "720p"
        metadata.write_text(json.dumps(portable))
        await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        await self.assert_recovered(fixture, metadata, language="en", marked=False)

    async def test_current_portable_video_cannot_authorize_unbound_db_bundle(self):
        fixture, _, metadata = await self.fixture(episode=True)
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(self.model, self.identity)
            item.source_fingerprint = None
            item.analysis_metadata_str = "{}"
            await session.commit()
        portable = json.loads(metadata.read_text())
        measured = await probe_completed_media(str((await resolve_media_source(self.expected_url, check_cloud=False)).local_path))
        portable.update(measured)
        portable["language"] = "tr"
        portable["analysis"]["measurement_version"] = 1
        portable["analysis"]["sender_quality"] = "720p"
        metadata.write_text(json.dumps(portable))
        await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        await self.assert_recovered(fixture, metadata, language="en", marked=False)

    async def test_bound_db_wrong_primary_stream_does_not_override_measured_default(self):
        fixture, _, metadata = await self.fixture()
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(self.model, self.identity)
            audio = item.audio_metadata
            audio[primary_embedded_audio_index(audio)]["streamIndex"] = 99
            item.audio_metadata = audio
            await session.commit()
        portable = json.loads(metadata.read_text())
        portable.pop("audio_metadata")
        metadata.write_text(json.dumps(portable))
        await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        await self.assert_recovered(fixture, metadata, language="en", marked=False)

    async def test_bound_portable_sender_survives_without_db_audio(self):
        fixture, _, metadata = await self.fixture(episode=True)
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(self.model, self.identity)
            item.audio_metadata = []
            await session.commit()
        portable = json.loads(metadata.read_text())
        portable["analysis"]["measurement_version"] = 1
        metadata.write_text(json.dumps(portable))
        await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        await self.assert_recovered(fixture, metadata)

    async def test_unbound_db_and_portable_cannot_use_catalog_language_as_proof(self):
        fixture, _, metadata = await self.fixture(legacy=True)
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(self.model, self.identity)
            item.source_fingerprint = None
            item.analysis_metadata_str = "{}"
            await session.commit()
        portable = json.loads(metadata.read_text())
        portable.pop("video_fingerprint")
        portable.pop("source_fingerprint")
        portable["analysis"] = {}
        metadata.write_text(json.dumps(portable))
        await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        # Historical quality claim remains independent of audio proof.
        portable = json.loads(metadata.read_text())
        self.assertNotIn("sender_quality", portable["analysis"])
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(self.model, self.identity)
            language_fixture.PrimaryLanguageRefresh().assert_audio(item, portable, language="en", marked=False)
            self.assertEqual(item.video_url, self.expected_url)
            self.assertEqual((item.catalog_source, item.availability), ("server", "available"))
            tracks = playback.track_contract(item, item.id, item.source_fingerprint, "synthetic-ticket")
            primary = next(track for track in tracks if track.source == "embedded" and track.stream_index == 2)
            self.assertEqual((primary.language, primary.label), ("en", "EN"))

    async def test_video_changed_during_remeasurement_rejects_snapshot_db_proof(self):
        fixture, selected, metadata = await self.fixture()
        portable = json.loads(metadata.read_text())
        portable.pop("audio_metadata")
        metadata.write_text(json.dumps(portable))
        real_probe = queue.probe_completed_media

        async def replace_then_probe(path):
            await language_fixture.make_media(selected.local_path, frequency=1234)
            self.video_fingerprint = (await resolve_media_source(self.expected_url, check_cloud=False)).video_fingerprint
            return await real_probe(path)

        with patch.object(queue, "probe_completed_media", replace_then_probe):
            await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        await self.assert_recovered(fixture, metadata, language="en", marked=False)

    async def test_no_submission_missing_portable_tracks_keeps_physical_primary(self):
        fixture, _, metadata = await self.fixture(episode=True, submitted=None)
        portable = json.loads(metadata.read_text())
        portable.pop("audio_metadata")
        portable["language"] = "tr"
        metadata.write_text(json.dumps(portable))
        await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        await self.assert_recovered(fixture, metadata, language="en", marked=False)


if __name__ == "__main__":
    unittest.main()
