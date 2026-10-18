"""Accepted primary audio semantics survive real measurement and HEVC refreshes."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from lifecycle.media_work import media_ownership
from models import DownloadTask, Episode, MediaOperationReceipt, Movie
from routes import playback
from services import queue
from services.audio_extractor import apply_primary_audio_language, primary_embedded_audio_index
from services.media_probe import probe_completed_media, probe_media_stream
from services.media_source import clear_cloud_object_cache, playback_source_fingerprint, resolve_media_source
from services.playback_prep import playback_prep_service
from tests.media import test_analysis_provenance as analysis_fixture
from tests.media import test_cloud_analysis_identity as cloud_fixture
from tests.media import test_hevc_mutation as hevc_fixture


async def make_media(path: Path, *, codec="libx264", frequency=440, primary_language="eng"):
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [shutil.which("ffmpeg"), "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=2",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", codec,
        "-preset", "ultrafast", "-threads", "1", "-c:a", "aac",
        "-metadata:s:a:0", "language=spa", "-metadata:s:a:1", f"language={primary_language}",
        "-disposition:a:0", "0", "-disposition:a:1", "default"]
    command += ["-crf", "0"] if codec == "libx264" else ["-x265-params", "pools=1:frame-threads=1:log-level=error"]
    result = await asyncio.to_thread(subprocess.run, [*command, str(path)], capture_output=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr.decode())
    return await probe_completed_media(str(path))


async def make_external(video: Path):
    audio = video.parent / "audio" / "tr.m4a"
    audio.parent.mkdir(exist_ok=True)
    command = [shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
        "sine=frequency=220:duration=2", "-c:a", "aac", str(audio)]
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr.decode())
    return audio


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
class PrimaryLanguageRefresh(unittest.IsolatedAsyncioTestCase):
    def assert_audio(self, item, portable, *, language, marked, physical_language="en"):
        audio = item.audio_metadata
        primary = primary_embedded_audio_index(audio)
        self.assertEqual(audio[primary]["language"], language)
        self.assertEqual(audio[primary]["label"], language.upper())
        self.assertEqual(audio[primary]["streamIndex"], 2, "default embedded stream is not first")
        self.assertEqual(audio[0]["language"], "es")
        self.assertEqual(audio[0]["streamIndex"], 1)
        self.assertNotIn("languageOverride", audio[0])
        if marked:
            self.assertEqual(audio[primary]["languageOverride"], {"source": "sender", "language": language})
        else:
            self.assertNotIn("languageOverride", audio[primary])
        external = [track for track in audio if track.get("source") == "external"]
        self.assertEqual(len(external), 1)
        self.assertEqual(external[0]["fileName"], "tr.m4a")
        self.assertEqual(external[0]["language"], "tr")
        self.assertNotIn("languageOverride", external[0])
        self.assertEqual(portable["audio_metadata"], audio)
        self.assertEqual(portable["languages"], item.languages)
        self.assertEqual(set(item.languages), {"es", language, "tr"})
        self.assertEqual(portable["language"], language)
        manifest_audio = [track for track in portable["stream_manifest"] if track["type"] == "audio"]
        self.assertEqual([track["language"] for track in manifest_audio], ["es", physical_language])
        self.assertEqual(json.loads(item.analysis_metadata_str), portable["analysis"])
        renditions = playback_prep_service.audio_renditions(item)
        primary_rendition = next(track for track in renditions if track.source == "embedded" and track.stream_index == 2)
        self.assertEqual((primary_rendition.language, primary_rendition.label), (language, language.upper()))
        self.assertEqual(len(renditions), 3)

    async def queue_fixture(self, *, submitted="tr", legacy=False):
        fixture = analysis_fixture.CatalogAnalysisProvenance()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        source = fixture.root / "input.mkv"
        await make_media(source)
        measured_source = await probe_media_stream(str(source))
        async with AsyncSession(fixture.engine) as session:
            session.add(DownloadTask(id="language-task", tmdb_id=801, media_type="movie", video_url="https://source.example/fixture",
                language=submitted, quality="720p", status="DOWNLOADING", created_at="2026-10-13T00:00:00"))
            await session.commit()
        async def download(**kwargs):
            shutil.copy2(source, kwargs["output_path"])
            return True, None
        with patch.object(queue, "validate_url", AsyncMock()), patch.object(queue, "probe_media_stream", AsyncMock(return_value=measured_source)), \
            patch.object(queue, "download_and_merge", download), patch.object(queue.vibe_analysis_manager, "enqueue", AsyncMock()), \
            patch.object(queue.tmdb_client, "fetch_movie_metadata", AsyncMock(return_value={"title": "Film", "releaseYear": 2026, "originalLanguage": "tr"})), \
            patch.object(fixture.manager, "MINIMUM_TASK_RESERVATION_BYTES", 1024**2), \
            patch.object(fixture.manager, "STORAGE_SAFETY_MARGIN_BYTES", 1024**2), \
            patch.object(fixture.manager, "ESTIMATED_MEDIA_BYTES_PER_SECOND", 1024**2):
            await fixture.manager._process_admitted_task("language-task")
        async with AsyncSession(fixture.engine) as session:
            task = await session.get(DownloadTask, "language-task")
            self.assertEqual(task.status, "COMPLETED", task.error_message)
            item = await session.get(Movie, "m_801")
            selected = await resolve_media_source(item.video_url, check_cloud=False)
            metadata = selected.local_path.parent / ".metadata" / "metadata.json"
            portable = json.loads(metadata.read_text())
            audio = item.audio_metadata
            primary = primary_embedded_audio_index(audio)
            self.assertEqual(audio[primary]["language"], submitted or "en")
            if legacy:
                # Simulate the absent field in an old accepted queue record; keep
                # the actual accepted label and source identity unchanged.
                audio[primary].pop("languageOverride")
                portable["audio_metadata"] = audio
                item.audio_metadata = audio
            analysis = json.loads(item.analysis_metadata_str)
            self.assertEqual(analysis["video_fingerprint"], selected.video_fingerprint)
            analysis["measurement_version"] = 1
            item.analysis_metadata_str = json.dumps(analysis)
            portable["analysis"] = analysis
            metadata.write_text(json.dumps(portable))
            await session.commit()
        await make_external(selected.local_path)
        return fixture, selected, metadata

    async def refresh_queue(self, *, submitted="tr", legacy=False, changed=False, unbound=False,
        portable_current=False, empty_db_audio=False):
        fixture, selected, metadata = await self.queue_fixture(submitted=submitted, legacy=legacy)
        if changed:
            await make_media(selected.local_path, frequency=1234)
            selected = await resolve_media_source(selected.catalog_path, check_cloud=False)
        if portable_current:
            portable = json.loads(metadata.read_text())
            measured = await probe_completed_media(str(selected.local_path))
            measured["analysis"]["measurement_version"] = 1
            portable.update(measured)
            metadata.write_text(json.dumps(portable))
        if unbound:
            portable = json.loads(metadata.read_text())
            portable.pop("video_fingerprint", None)
            portable.pop("source_fingerprint", None)
            metadata.write_text(json.dumps(portable))
            async with AsyncSession(fixture.engine) as session:
                item = await session.get(Movie, "m_801")
                item.source_fingerprint = None
                analysis = json.loads(item.analysis_metadata_str)
                analysis.pop("video_fingerprint", None)
                item.analysis_metadata_str = json.dumps(analysis)
                await session.commit()
        if empty_db_audio:
            portable = json.loads(metadata.read_text())
            portable.pop("video_fingerprint", None)
            portable.pop("source_fingerprint", None)
            metadata.write_text(json.dumps(portable))
            async with AsyncSession(fixture.engine) as session:
                item = await session.get(Movie, "m_801")
                item.audio_metadata = []
                await session.commit()
        async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
            item = await session.get(Movie, "m_801")
            async with media_ownership(item.id):
                await playback.ensure_source_metadata(session, item, selected)
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(Movie, "m_801")
            portable = json.loads(metadata.read_text())
            language = "en" if changed or unbound or empty_db_audio or not submitted else submitted
            self.assert_audio(item, portable, language=language, marked=bool(submitted and not legacy and not changed and not unbound and not empty_db_audio))
            self.assertEqual(item.original_language, "tr")
            self.assertEqual(json.loads(item.analysis_metadata_str).get("sender_quality"),
                None if changed or unbound else "720p")
            self.assertEqual(item.source_fingerprint, playback_source_fingerprint(selected, item.audio_metadata))
            receipts = (await session.exec(select(MediaOperationReceipt))).all()
            self.assertEqual(len(receipts), 2)
        self.assertFalse(list(Path(settings.TEMP_DIR).glob("media-operations/*/operation.json")))

    async def test_actual_queue_to_lazy_refresh_preserves_submitted_default_nonfirst_language(self):
        await self.refresh_queue()

    async def test_unmarked_genuine_queue_override_survives_same_video_without_new_provenance(self):
        await self.refresh_queue(legacy=True)

    async def test_no_submission_keeps_physical_primary_despite_turkish_catalog_language(self):
        await self.refresh_queue(submitted=None)

    async def test_changed_video_does_not_reapply_old_semantics_or_override(self):
        await self.refresh_queue(changed=True)

    async def test_unbound_legacy_video_does_not_promote_task_or_catalog_language(self):
        await self.refresh_queue(legacy=True, unbound=True)

    async def test_matching_portable_source_cannot_authorize_stale_db_audio_from_replaced_video(self):
        await self.refresh_queue(changed=True, portable_current=True)

    async def test_matching_db_source_without_tracks_cannot_authorize_unbound_portable_audio(self):
        await self.refresh_queue(empty_db_audio=True)

    async def test_lazy_acceptance_failure_restores_language_metadata_and_catalog(self):
        fixture, selected, metadata = await self.queue_fixture()
        before = metadata.read_bytes()
        async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
            item = await session.get(Movie, "m_801")
            old_audio, old_languages = copy.deepcopy(item.audio_metadata), list(item.languages)
            with patch.object(session, "commit", AsyncMock(side_effect=RuntimeError("Injected language acceptance failure"))):
                async with media_ownership(item.id):
                    with self.assertRaises(RuntimeError):
                        await playback.ensure_source_metadata(session, item, selected)
            await session.rollback()
        self.assertEqual(metadata.read_bytes(), before)
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(Movie, "m_801")
            self.assertEqual(item.audio_metadata, old_audio)
            self.assertEqual(item.languages, old_languages)
            self.assertEqual(len((await session.exec(select(MediaOperationReceipt))).all()), 1)

    async def hevc_acceptance(self, *, existing=False, legacy=False, submitted="tr", stale_db=False):
        fixture = hevc_fixture.HEVCMutationTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        measured = await make_media(fixture.video, codec="libx265" if existing else "libx264")
        await make_external(fixture.video)
        measured = await probe_completed_media(str(fixture.video))
        corrected = apply_primary_audio_language(measured["audio_metadata"], submitted)
        if legacy:
            corrected[primary_embedded_audio_index(corrected)].pop("languageOverride")
        portable = {**fixture.old_metadata, **measured, "audio_metadata": corrected,
            "language": "tr", "original_language": "tr", "languages": ["es", "tr"]}
        portable["analysis"]["sender_quality"] = "2160p"
        old_analysis = copy.deepcopy(portable["analysis"])
        if stale_db:
            portable["audio_metadata"] = measured["audio_metadata"]
            portable["language"] = "en"
            old_analysis["video_fingerprint"] = "stale-db-video"
            old_analysis["source_fingerprint"] = "stale-db-source"
        fixture.metadata.write_text(json.dumps(portable))
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(Movie, fixture.media_id)
            item.original_language = "tr"
            item.audio_metadata = corrected
            item.languages = portable["languages"]
            item.source_fingerprint = "stale-db-source" if stale_db else measured["source_fingerprint"]
            item.analysis_metadata_str = json.dumps(old_analysis)
            await session.commit()
        before_size = fixture.video.stat().st_size
        fixture.install_worker(real_encoder=True)
        await asyncio.wait_for(fixture.worker._worker_loop(), 30)
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(Movie, fixture.media_id)
            self.assertTrue(item.hevc_compressed, fixture.probes)
            self.assertEqual(item.codec, "hevc")
            final = json.loads(fixture.metadata.read_text())
            self.assert_audio(item, final, language="en" if stale_db else submitted or "en", marked=bool(submitted and not legacy and not stale_db))
            self.assertEqual(item.original_language, "tr")
            self.assertEqual(final["original_language"], "tr")
            self.assertEqual(json.loads(item.analysis_metadata_str)["sender_quality"], "2160p")
            self.assertEqual(len((await session.exec(select(MediaOperationReceipt))).all()), 1)
        if existing:
            self.assertEqual(fixture.video.stat().st_size, before_size)
            self.assertEqual([command for command in fixture.commands if Path(command[0]).name == "ffmpeg"], [])
        else:
            self.assertLess(fixture.video.stat().st_size, before_size)
            self.assertEqual(len([command for command in fixture.commands if Path(command[0]).name == "ffmpeg"]), 1)
        self.assertFalse(list(fixture.temp.glob("media-operations/*/operation.json")))

    async def test_actual_existing_hevc_refresh_preserves_submitted_primary_language(self):
        await self.hevc_acceptance(existing=True)

    async def test_actual_successful_hevc_replacement_preserves_primary_and_other_sources(self):
        await self.hevc_acceptance()

    async def test_actual_hevc_preserves_unmarked_accepted_semantics_without_claiming_sender(self):
        await self.hevc_acceptance(legacy=True)

    async def test_actual_hevc_without_submission_keeps_physical_language_and_other_sources(self):
        await self.hevc_acceptance(submitted=None)

    async def test_actual_hevc_portable_source_match_cannot_authorize_stale_db_language(self):
        await self.hevc_acceptance(existing=True, stale_db=True)

    async def test_local_reconstruction_preserves_override_and_does_not_invent_one_from_portable_fallback(self):
        for submitted in ("tr", None):
            with self.subTest(submitted=submitted):
                fixture, _, metadata = await self.queue_fixture(submitted=submitted)
                await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
                async with AsyncSession(fixture.engine) as session:
                    item = await session.get(Movie, "m_801")
                    self.assert_audio(item, json.loads(metadata.read_text()), language=submitted or "en", marked=bool(submitted))
                    self.assertTrue(item.video_url.startswith("/media/"))
                    self.assertEqual(item.availability, "available")
                    self.assertEqual(json.loads(item.analysis_metadata_str)["measurement_version"], 2)

    async def test_local_sweep_preserves_present_movie_episode_without_portable_identity_and_clears_missing(self):
        fixture = analysis_fixture.CatalogAnalysisProvenance()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        movie_url = "/media/Movies/Unannotated/video.mkv"
        episode_url = "/media/Series/Unannotated/Season_1/Episode_1/video.mkv"
        movie_path = Path(settings.MEDIA_DIR) / movie_url.removeprefix("/media/")
        episode_path = Path(settings.MEDIA_DIR) / episode_url.removeprefix("/media/")
        await make_media(movie_path)
        episode_path.parent.mkdir(parents=True)
        shutil.copy2(movie_path, episode_path)
        async with AsyncSession(fixture.engine) as session:
            session.add(Movie(id="m_present", title="Present", description="", thumbnail_url="", video_url=movie_url,
                duration="2s", release_year=2026, availability="available", catalog_source="server"))
            session.add(Movie(id="m_missing", title="Missing", description="", thumbnail_url="", video_url="/media/Movies/Missing/video.mkv",
                duration="2s", release_year=2026, availability="available", catalog_source="server"))
            for suffix in ("present", "missing"):
                session.add(Movie(id="tv_" + suffix, title="Show", description="", thumbnail_url="", video_url="", type="series",
                    duration="2s", release_year=2026, availability="available", catalog_source="server"))
                session.add(Episode(id="ep_" + suffix, movie_id="tv_" + suffix, title="Episode", description="", thumbnail_url="",
                    season_number=1, episode_number=1, duration="2s", video_url=episode_url if suffix == "present" else "/media/Series/Missing/video.mkv"))
            await session.commit()
        await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        async with AsyncSession(fixture.engine) as session:
            movie = await session.get(Movie, "m_present")
            self.assertEqual((movie.video_url, movie.availability, movie.catalog_source), (movie_url, "available", "server"))
            episode = await session.get(Episode, "ep_present")
            self.assertEqual(episode.video_url, episode_url)
            self.assertEqual((await session.get(Movie, "tv_present")).availability, "available")
            missing = await session.get(Movie, "m_missing")
            self.assertEqual((missing.video_url, missing.availability, missing.catalog_source), ("", "cached", "tmdb_cache"))
            self.assertEqual((await session.get(Episode, "ep_missing")).video_url, "")
            self.assertEqual((await session.get(Movie, "tv_missing")).availability, "cached")

    async def test_actual_reconstruction_does_not_treat_old_portable_english_fallback_as_submission(self):
        fixture = analysis_fixture.CatalogAnalysisProvenance()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        video = Path(settings.MEDIA_DIR) / "Movies" / "Physical" / "film.mkv"
        await make_media(video, primary_language="fra")
        await make_external(video)
        measured = await probe_completed_media(str(video))
        portable = {**measured, "tmdb_id": 802, "media_type": "movie", "title": "Physical",
            "video_url": "/media/Movies/Physical/film.mkv", "catalog_source": "server",
            "language": "en", "original_language": "en", "languages": ["en"]}
        portable["analysis"]["measurement_version"] = 1
        metadata = video.parent / ".metadata" / "metadata.json"
        metadata.parent.mkdir()
        metadata.write_text(json.dumps(portable))
        await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(Movie, "m_802")
            self.assertIsNotNone(item)
            self.assert_audio(item, json.loads(metadata.read_text()), language="fr", marked=False, physical_language="fr")
            self.assertEqual(item.availability, "available")

    async def test_actual_cloud_refresh_preserves_legacy_primary_and_independent_external_identity(self):
        fixture = cloud_fixture.CloudAnalysisIdentity()
        original_service = playback.playback_prep_service
        original_probe_times = playback._cloud_audio_probe_times
        # Manually reused TestCases do not get unittest's automatic cleanup pass.
        # Register restoration assertions first so they run after helper cleanup.
        self.addCleanup(lambda: self.assertIs(playback.playback_prep_service, original_service))
        self.addCleanup(lambda: self.assertIs(playback._cloud_audio_probe_times, original_probe_times))
        await fixture.asyncSetUp(cleanup_owner=self)
        self.addAsyncCleanup(fixture.asyncTearDown)
        measured = await make_media(fixture.remote)
        await make_external(fixture.remote)
        clear_cloud_object_cache()
        async with AsyncSession(fixture.engine) as session:
            item = await session.get(Movie, "m_1")
            source = await resolve_media_source(item.video_url, require_cloud=True, refresh_cloud=True)
            corrected = apply_primary_audio_language(measured["audio_metadata"], "tr")
            corrected[primary_embedded_audio_index(corrected)].pop("languageOverride")
            item.audio_metadata = corrected
            item.languages = ["es", "tr"]
            item.original_language = "tr"
            item.source_fingerprint = playback_source_fingerprint(source, corrected)
            item.analysis_metadata_str = json.dumps({"video_fingerprint": source.video_fingerprint, "measurement_version": 1})
            portable = {**json.loads(fixture.metadata.read_text()), **measured, "language": "tr", "original_language": "tr",
                "languages": item.languages, "audio_metadata": corrected, "video_fingerprint": source.video_fingerprint,
                "source_fingerprint": item.source_fingerprint, "analysis": json.loads(item.analysis_metadata_str)}
            fixture.metadata.write_text(json.dumps(portable))
            await session.commit()
        response = await fixture.create_run()
        self.assertEqual(response.status_code, 200, response.text)
        item = await fixture.catalog()
        self.assert_audio(item, json.loads(fixture.metadata.read_text()), language="tr", marked=False)
        self.assertEqual(item.original_language, "tr")
        self.assertEqual(len(response.json()["tracks"]), 3)
