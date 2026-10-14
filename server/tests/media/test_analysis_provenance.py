"""Selected-video duration and historical sender claims across real media flows."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from models import DownloadTask, Episode, Movie, MovieResponse
from services import queue
from services.media_analysis import measured_analysis, refreshed_analysis
from services.media_probe import probe_completed_media, probe_media_stream
from services.media_source import resolve_media_source
from lifecycle.media_work import media_ownership
from routes import playback
from tests.media import test_hevc_mutation as hevc_fixture


async def make_video(path: Path, *, secondary="video", epoch=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=size=854x480:rate=4:duration=1", "-f", "lavfi", "-i",
        "color=size=1280x720:rate=4:duration=3" if secondary == "video" else "sine=frequency=440:duration=3",
        "-map", "0:v:0", "-map", "1:v:0" if secondary == "video" else "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac"]
    if epoch:
        command += ["-vf", f"setpts=PTS+{epoch}/TB", "-copyts", "-avoid_negative_ts", "disabled"]
    result = await asyncio.to_thread(subprocess.run, [*command, "-y", str(path)], capture_output=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr.decode())


class SelectedVideoMeasurements(unittest.IsolatedAsyncioTestCase):
    async def test_real_unequal_video_streams_and_epochs_in_mp4_and_matroska(self):
        with tempfile.TemporaryDirectory(prefix="selected-duration-") as temporary:
            for extension in ("mp4", "mkv"):
                for epoch in (0.0, 10.0):
                    with self.subTest(extension=extension, epoch=epoch):
                        path = Path(temporary) / f"video-{epoch}.{extension}"
                        await make_video(path, epoch=epoch)
                        final = await probe_completed_media(str(path))
                        source = await probe_media_stream(str(path))
                        self.assertTrue(final["valid_video"])
                        self.assertFalse(final["analysis"]["direct_timeline_compatible"])
                        self.assertEqual(final["video_stream_index"], 0)
                        self.assertEqual(final["quality"], "480p")
                        self.assertAlmostEqual(final["probed_duration"], 1.0, places=5)
                        self.assertFalse(final["analysis"]["direct_timeline_compatible"])
                        self.assertAlmostEqual(final["analysis"]["probed_duration"], 1.0, places=5)
                        self.assertAlmostEqual(final["stream_manifest"][0]["duration"], 1.0, places=5)
                        self.assertAlmostEqual(final["stream_manifest"][1]["duration"], 3.0, places=5)
                        self.assertAlmostEqual(source["video_duration"], 1.0, places=5)
                        self.assertAlmostEqual(source["video_start_time"], epoch, places=5)
                        self.assertEqual(final["duration_provenance"], "selected_stream_packets")

    async def test_real_longer_audio_does_not_determine_selected_video_duration(self):
        with tempfile.TemporaryDirectory(prefix="selected-audio-tail-") as temporary:
            for extension in ("mp4", "mkv"):
                for epoch in (0.0, 10.0):
                    with self.subTest(extension=extension, epoch=epoch):
                        path = Path(temporary) / f"tail-{epoch}.{extension}"
                        await make_video(path, secondary="audio", epoch=epoch)
                        final = await probe_completed_media(str(path))
                        source = await probe_media_stream(str(path))
                        self.assertAlmostEqual(final["probed_duration"], 1.0, places=5)
                        self.assertAlmostEqual(final["stream_manifest"][0]["startTime"], epoch, places=5)
                        self.assertAlmostEqual(source["video_start_time"], epoch, places=5)
                        self.assertGreater(final["audio_metadata"][0]["duration"], 2.9)

    async def test_unavailable_packet_endpoint_withholds_selected_duration(self):
        from services import media_probe
        with tempfile.TemporaryDirectory(prefix="unknown-packet-epoch-") as temporary:
            path = Path(temporary) / "video.mkv"
            await make_video(path)
            real_spawn = asyncio.create_subprocess_exec
            async def spawn(*command, **kwargs):
                if "packet=stream_index,pts_time,duration_time" in command:
                    raise OSError("Injected packet inspection failure")
                return await real_spawn(*command, **kwargs)
            with patch.object(media_probe.asyncio, "create_subprocess_exec", spawn):
                measured = await probe_completed_media(str(path))
            self.assertEqual(measured["analysis"]["duration_provenance"], "unknown")
            self.assertFalse(measured["valid_video"])
            self.assertEqual(measured["analysis"]["estimated_duration_provenance"], "container_fallback")
            self.assertFalse(measured["analysis"]["direct_timeline_compatible"])
            self.assertIsNone(measured["analysis"]["probed_duration"])
            self.assertEqual(measured["analysis"]["estimated_duration"], 3.0)

    async def test_direct_proof_requires_real_single_zero_epoch_video(self):
        with tempfile.TemporaryDirectory(prefix="direct-timeline-") as temporary:
            path = Path(temporary) / "single.mp4"
            command = [shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=size=854x480:rate=4:duration=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-y", str(path)]
            result = await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            measured = await probe_completed_media(str(path))
            self.assertTrue(measured["analysis"]["direct_timeline_compatible"])

    def test_missing_stream_duration_exposes_estimate_or_unknown(self):
        video = {"width": 1280, "height": 720}
        estimated = measured_analysis(video, {"duration": "3", "format_name": "avi"})
        self.assertIsNone(estimated["probed_duration"])
        self.assertEqual(estimated["estimated_duration"], 3)
        self.assertEqual(estimated["duration_provenance"], "unknown")
        self.assertEqual(estimated["estimated_duration_provenance"], "container_fallback")
        unknown = measured_analysis(video, {"duration": "nan"})
        self.assertIsNone(unknown["probed_duration"])
        self.assertEqual(unknown["duration_provenance"], "unknown")
        ticks = measured_analysis({**video, "duration_ts": 1000, "time_base": "1/1000"}, {"duration": 9})
        self.assertIsNone(ticks["probed_duration"])
        self.assertEqual(ticks["estimated_duration"], 1)
        self.assertEqual(ticks["estimated_duration_provenance"], "selected_stream_ticks")

    def test_refresh_does_not_promote_measured_quality_to_sender_claim(self):
        current = {"quality": "480p", "width": 854, "video_bitrate": None}
        self.assertEqual(refreshed_analysis(current, {"quality": "2160p", "width": 3840}), current)
        self.assertEqual(refreshed_analysis(current, {"sender_quality": "2160p"}), {**current, "sender_quality": "2160p"})
        self.assertEqual(refreshed_analysis(current, {"sender_quality": None}), current)


class CatalogAnalysisProvenance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="analysis-catalog-")
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(settings, "MEDIA_DIR", str(self.root / "media")))
        self.stack.enter_context(patch.object(settings, "TEMP_DIR", str(self.root / "temp")))
        self.stack.enter_context(patch.object(settings, "STORAGE_ENGINE", "LOCAL"))
        Path(settings.TEMP_DIR).mkdir()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'catalog.db'}")
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.stack.enter_context(patch.object(db_module, "engine", self.engine))
        self.stack.enter_context(patch.object(queue, "engine", self.engine))
        self.stack.enter_context(patch.object(queue, "download_and_cache_metadata_image", AsyncMock(return_value="")))
        metadata = {"title": "Film", "releaseYear": 2026, "duration": "2h"}
        self.stack.enter_context(patch.object(queue.tmdb_client, "fetch_movie_metadata", AsyncMock(return_value=metadata)))
        self.stack.enter_context(patch.object(queue.tmdb_client, "fetch_show_metadata", AsyncMock(return_value=metadata)))
        self.stack.enter_context(patch.object(queue.tmdb_client, "fetch_episode_metadata", AsyncMock(return_value={"title": "Verified episode", "duration": "45m"})))
        self.manager = queue.DownloadQueueManager()
        self.stack.enter_context(patch.object(self.manager, "_schedule_playback_baseline", AsyncMock()))

    async def asyncTearDown(self):
        self.stack.close()
        await self.engine.dispose()
        self.temporary.cleanup()

    async def portable_fixture(self, identifier, media_type, sender_quality, *, reprobe=False):
        settings.MEDIA_DIR = str(self.root / f"media-{identifier}")
        relative = "Movies/Film" if media_type == "movie" else "Series/Show/Season_1/Episode_1"
        video = Path(settings.MEDIA_DIR) / relative / "film.mkv"
        await make_video(video)
        measured = await probe_completed_media(str(video))
        analysis = dict(measured["analysis"])
        if sender_quality is not None:
            analysis["sender_quality"] = sender_quality
        payload = {**measured, "tmdb_id": identifier, "media_type": media_type, "title": "Film",
            "video_url": f"/media/{relative}/film.mkv", "catalog_source": "server", "quality": "480p",
            "language": "en", "analysis": analysis, "release_year": 2026}
        if media_type != "movie":
            payload.update(season=1, episode=1)
        if reprobe:
            payload["source_fingerprint"] = "previous-fingerprint"
            payload["analysis"] = {**analysis, "width": 1, "video_bitrate": 999999}
        metadata = video.parent / ".metadata" / "metadata.json"
        metadata.parent.mkdir()
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        return video, metadata, payload

    async def assert_catalog(self, identifier, media_type, expected_claim, metadata=None):
        async with AsyncSession(self.engine) as session:
            if media_type == "movie":
                item = await session.get(Movie, f"m_{identifier}")
                response = MovieResponse.from_db(item)
            else:
                item = await session.get(Episode, f"ep_{identifier}_s1_e1")
                show = await session.get(Movie, f"tv_{identifier}")
                response = MovieResponse.from_db(show, [item]).episodes[0]
            self.assertIsNotNone(item)
            analysis = json.loads(item.analysis_metadata_str)
            self.assertEqual(analysis.get("sender_quality"), expected_claim)
            if expected_claim is None:
                self.assertNotIn("sender_quality", analysis)
            self.assertEqual(analysis["width"], 854)
            self.assertAlmostEqual(item.probed_duration, 1.0, places=5)
            self.assertEqual(item.duration, "1s")
            self.assertEqual(item.quality, "480p")
            self.assertEqual(response.analysis, analysis)
        if metadata:
            portable = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(portable["analysis"], analysis)
            self.assertAlmostEqual(portable["probed_duration"], 1.0, places=5)

    async def test_local_rebuild_retains_real_claim_and_preserves_absence(self):
        for number, (media_type, claim) in enumerate((("movie", "2160p"), ("movie", None), ("series", "2160p"), ("series", None)), 101):
            with self.subTest(media_type=media_type, claim=claim):
                _, metadata, _ = await self.portable_fixture(number, media_type, claim)
                await self.manager.sync_media_from_disk(raise_on_critical_failure=True)
                await self.assert_catalog(number, media_type, claim, metadata)

    async def test_local_reprobe_existing_movie_and_episode_refreshes_measurements(self):
        for number, media_type in enumerate(("movie", "series"), 201):
            with self.subTest(media_type=media_type):
                _, metadata, payload = await self.portable_fixture(number, media_type, "2160p", reprobe=True)
                async with AsyncSession(self.engine) as session:
                    movie = Movie(id=f"m_{number}" if media_type == "movie" else f"tv_{number}",
                        title="Verified fixture", description="Description", thumbnail_url="", video_url=payload["video_url"] if media_type == "movie" else "",
                        duration="3s", release_year=2026, type="movie" if media_type == "movie" else "series", catalog_source="server", availability="available")
                    if media_type == "movie":
                        movie.analysis_metadata_str = json.dumps(payload["analysis"])
                    session.add(movie)
                    if media_type != "movie":
                        session.add(Episode(id=f"ep_{number}_s1_e1", movie_id=movie.id, episode_number=1, season_number=1,
                            title="Verified episode", description="Description", thumbnail_url="", video_url=payload["video_url"],
                            duration="3s", analysis_metadata_str=json.dumps(payload["analysis"])))
                    await session.commit()
                await self.manager.sync_media_from_disk(raise_on_critical_failure=True)
                await self.assert_catalog(number, media_type, "2160p", metadata)

    async def test_legacy_matching_fingerprint_reprobes_container_duration(self):
        _, metadata, payload = await self.portable_fixture(251, "movie", "2160p")
        payload["probed_duration"] = 3.0
        payload["analysis"]["probed_duration"] = 3.0
        payload["analysis"]["duration_provenance"] = "final_probe"
        metadata.write_text(json.dumps(payload))
        await self.manager.sync_media_from_disk(raise_on_critical_failure=True)
        await self.assert_catalog(251, "movie", "2160p", metadata)

    async def lazy_route_fixture(self):
        _, metadata, payload = await self.portable_fixture(501, "movie", "2160p")
        payload["probed_duration"] = 3.0
        payload["analysis"] = {"quality": "720p", "sender_quality": "2160p", "width": 1280, "duration_provenance": "final_probe"}
        metadata.write_text(json.dumps(payload))
        async with AsyncSession(self.engine) as session:
            session.add(Movie(id="m_501", title="Film", description="Description", thumbnail_url="", video_url=payload["video_url"],
                duration="3s", probed_duration=3.0, release_year=2026, quality="720p", width=1280, height=720, codec="h264",
                source_fingerprint=payload["source_fingerprint"], analysis_metadata_str=json.dumps(payload["analysis"])))
            await session.commit()
        source = await resolve_media_source(payload["video_url"], check_cloud=False)
        return metadata, source

    async def test_lazy_route_refresh_preserves_claim_and_accepts_all_analysis_surfaces(self):
        metadata, source = await self.lazy_route_fixture()
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            movie = await session.get(Movie, "m_501")
            async with media_ownership(movie.id):
                await playback.ensure_source_metadata(session, movie, source)
        await self.assert_catalog(501, "movie", "2160p", metadata)

    async def test_lazy_route_commit_failure_restores_portable_measurements(self):
        metadata, source = await self.lazy_route_fixture()
        before = metadata.read_bytes()
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            movie = await session.get(Movie, "m_501")
            with patch.object(session, "commit", AsyncMock(side_effect=RuntimeError("Injected lazy catalog failure"))):
                async with media_ownership(movie.id):
                    with self.assertRaises(RuntimeError):
                        await playback.ensure_source_metadata(session, movie, source)
            await session.rollback()
        self.assertEqual(metadata.read_bytes(), before)
        async with AsyncSession(self.engine) as session:
            movie = await session.get(Movie, "m_501")
            self.assertEqual(movie.probed_duration, 3.0)
            self.assertEqual(movie.quality, "720p")
            self.assertEqual(json.loads(movie.analysis_metadata_str)["sender_quality"], "2160p")

    async def test_cloud_reconstruction_preserves_portable_provenance(self):
        for number, (media_type, claim) in enumerate((("movie", "2160p"), ("movie", None), ("series", "2160p"), ("series", None)), 301):
            with self.subTest(media_type=media_type, claim=claim):
                _, _, payload = await self.portable_fixture(number, media_type, claim)
                async with AsyncSession(self.engine) as session:
                    await self.manager._restore_cloud_catalog_entry(session, payload, {"title": "Film", "releaseYear": 2026}, 1 if media_type != "movie" else None, 1 if media_type != "movie" else None)
                    await session.commit()
                await self.assert_catalog(number, media_type, claim)

    async def test_actual_queue_publication_uses_selected_duration_in_all_surfaces(self):
        for number, extension in enumerate(("mp4", "mkv"), 401):
            with self.subTest(extension=extension):
                fixture = self.root / f"input.{extension}"
                await make_video(fixture)
                source_probe = await probe_media_stream(str(fixture))
                async with AsyncSession(self.engine) as session:
                    session.add(DownloadTask(id=f"analysis-{number}", tmdb_id=number, media_type="movie", video_url="https://public.example/film",
                        quality="2160p", status="DOWNLOADING", created_at="2026-10-13T00:00:00"))
                    await session.commit()
                async def download(**kwargs):
                    shutil.copy2(fixture, kwargs["output_path"])
                    return True, None
                with patch.object(queue, "validate_url", AsyncMock()), patch.object(queue, "probe_media_stream", AsyncMock(return_value=source_probe)), \
                     patch.object(queue, "download_and_merge", download), patch.object(queue, "extract_audio_and_strip_video", AsyncMock(return_value=[])), \
                     patch.object(queue.vibe_analysis_manager, "enqueue", AsyncMock()), \
                     patch.object(self.manager, "MINIMUM_TASK_RESERVATION_BYTES", 1024**2), \
                     patch.object(self.manager, "STORAGE_SAFETY_MARGIN_BYTES", 1024**2), \
                     patch.object(self.manager, "ESTIMATED_MEDIA_BYTES_PER_SECOND", 1024**2):
                    await self.manager._process_admitted_task(f"analysis-{number}")
                async with AsyncSession(self.engine) as session:
                    task = await session.get(DownloadTask, f"analysis-{number}")
                    self.assertEqual(task.status, "COMPLETED", task.error_message)
                    movie = await session.get(Movie, f"m_{number}")
                    metadata = Path(settings.MEDIA_DIR) / movie.video_url.removeprefix("/media/")
                await self.assert_catalog(number, "movie", "2160p", metadata.parent / ".metadata" / "metadata.json")


class HEVCAnalysisProvenance(unittest.IsolatedAsyncioTestCase):
    async def test_already_hevc_refreshes_portable_and_catalog_measurements_atomically(self):
        fixture = hevc_fixture.HEVCMutationTests()
        await fixture.asyncSetUp()
        try:
            async with AsyncSession(fixture.engine) as session:
                movie = await session.get(Movie, fixture.media_id)
                movie.source_fingerprint = "existing-hevc"
                movie.analysis_metadata_str = json.dumps({"sender_quality": "2160p", "width": 1920,
                    "video_fingerprint": "existing-hevc", "source_fingerprint": "existing-hevc"})
                await session.commit()
            async def inspect(_path):
                result = hevc_fixture.probe("hevc", "existing-hevc")
                result["analysis"] = {"width": 1280, "quality": "720p", "probed_duration": 20.0}
                return result
            fixture.install_worker(probe_override=inspect)
            await fixture.worker._worker_loop()
            self.assertEqual(fixture.video.read_bytes(), hevc_fixture.ORIGINAL)
            async with AsyncSession(fixture.engine) as session:
                movie = await session.get(Movie, fixture.media_id)
                analysis = json.loads(movie.analysis_metadata_str)
                self.assertTrue(movie.hevc_compressed)
                self.assertEqual(analysis["sender_quality"], "2160p")
                self.assertEqual(json.loads(fixture.metadata.read_text())["analysis"], analysis)
        finally:
            await fixture.asyncTearDown()

    async def test_worker_refresh_preserves_sender_claim_in_db_portable_and_api(self):
        for claim in ("2160p", None):
            with self.subTest(claim=claim):
                fixture = hevc_fixture.HEVCMutationTests()
                await fixture.asyncSetUp()
                try:
                    prior = {"quality": "1080p", "width": 1920, "video_bitrate": 99999}
                    if claim is not None:
                        prior["sender_quality"] = claim
                    async with AsyncSession(fixture.engine) as session:
                        movie = await session.get(Movie, fixture.media_id)
                        movie.analysis_metadata_str = json.dumps(prior)
                        await session.commit()
                    portable = json.loads(fixture.metadata.read_text())
                    portable["analysis"] = prior
                    fixture.metadata.write_text(json.dumps(portable))
                    async def inspect(path):
                        original = Path(path).read_bytes() == hevc_fixture.ORIGINAL
                        result = hevc_fixture.probe("h264" if original else "hevc", "original" if original else "optimized")
                        result["analysis"] = {"quality": "720p", "width": 1280, "video_bitrate": 1234, "quality_provenance": "final_probe"}
                        return result
                    fixture.install_worker(probe_override=inspect)
                    await fixture.worker._worker_loop()
                    async with AsyncSession(fixture.engine) as session:
                        movie = await session.get(Movie, fixture.media_id)
                        self.assertTrue(movie.hevc_compressed)
                        analysis = json.loads(movie.analysis_metadata_str)
                        self.assertEqual(analysis.get("sender_quality"), claim)
                        if claim is None:
                            self.assertNotIn("sender_quality", analysis)
                        self.assertEqual(analysis["width"], 1280)
                        self.assertEqual(analysis["video_bitrate"], 1234)
                        self.assertEqual(MovieResponse.from_db(movie).analysis, analysis)
                        self.assertEqual(json.loads(fixture.metadata.read_text())["analysis"], analysis)
                finally:
                    await fixture.asyncTearDown()


if __name__ == "__main__":
    unittest.main()
