from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import settings
from routes.playback import (
    catalog_duration_seconds,
    media_duration_seconds,
    parse_byte_range,
    rewrite_hls_playlist,
    subtitle_contract,
    subtitle_file_name,
)
from services.languages import normalize_language_tag
from services.media_probe import merge_local_external_audio, probe_cloud_external_audio, probe_completed_media
from services.media_source import MediaSourceError, ResolvedMediaSource, canonicalize_catalog_path, clear_cloud_object_cache, is_safe_presentation_asset, resolve_media_source
from services.playback_prep import PlaybackMediaSnapshot, PlaybackPrepService, PlaybackPreparationError, playback_prep_service
from services.playback_source import HttpPlaybackSource, LocalPlaybackSource
from services.rclone import rclone_service
from services.queue import srt_to_vtt


class PlaybackPipelineRegression(unittest.TestCase):
    media_directory: Path
    media_file: Path
    catalog_path: str

    @classmethod
    def setUpClass(cls) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise unittest.SkipTest("FFmpeg is unavailable")
        cls.media_directory = Path(settings.MEDIA_DIR) / "Movies" / f"PlaybackFixture_{uuid.uuid4().hex}"
        cls.media_directory.mkdir(parents=True, exist_ok=True)
        cls.media_file = cls.media_directory / "fixture.mp4"
        cls.catalog_path = f"/media/Movies/{cls.media_directory.name}/fixture.mp4"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "lavfi",
            "-i", "testsrc=size=640x360:rate=24",
            "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=48000",
            "-f", "lavfi",
            "-i", "sine=frequency=660:sample_rate=48000",
            "-t", "5",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-map", "2:a:0",
            "-metadata:s:a:0", "language=eng",
            "-metadata:s:a:0", "title=English",
            "-disposition:a:0", "default",
            "-metadata:s:a:1", "language=tur",
            "-metadata:s:a:1", "title=Türkçe",
            "-disposition:a:1", "0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            str(cls.media_file),
        ]
        subprocess.run(command, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.media_directory, ignore_errors=True)

    def test_canonical_source_resolution_and_path_rejection(self) -> None:
        source = asyncio.run(resolve_media_source(self.catalog_path, check_cloud=False))
        self.assertTrue(source.local_exists)
        self.assertEqual(source.local_path, self.media_file.resolve())
        self.assertEqual(canonicalize_catalog_path(self.catalog_path), self.catalog_path)
        for invalid in (
            "http://localhost:8000/media/Movies/video.mp4",
            "/media/Movies/../secret.mp4",
            "/media/Other/video.mp4",
            "C:/server/media/Movies/video.mp4",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(MediaSourceError):
                canonicalize_catalog_path(invalid)

        self.assertTrue(is_safe_presentation_asset(f"/media/Movies/{self.media_directory.name}/poster.jpg"))
        for protected in (self.catalog_path, f"/media/Movies/{self.media_directory.name}/subtitle_eng.vtt", f"/media/Movies/{self.media_directory.name}/.metadata/metadata.json"):
            with self.subTest(protected=protected):
                self.assertFalse(is_safe_presentation_asset(protected))

    def test_seekable_local_source_returns_only_the_requested_bytes(self) -> None:
        async def exercise() -> tuple[int, bytes]:
            reader = LocalPlaybackSource(self.media_file, self.catalog_path)
            source_stat = await reader.stat()
            chunks = [chunk async for chunk in reader.open_range(32, 128)]
            return source_stat.size, b"".join(chunks)

        size, payload = asyncio.run(exercise())
        self.assertEqual(size, self.media_file.stat().st_size)
        self.assertEqual(payload, self.media_file.read_bytes()[32:160])

    def test_validated_http_source_uses_origin_byte_ranges(self) -> None:
        payload = self.media_file.read_bytes()

        class RangeHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _headers(self, start: int, end: int, partial: bool) -> None:
                self.send_response(206 if partial else 200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()

            def do_HEAD(self) -> None:
                self._headers(0, len(payload) - 1, False)

            def do_GET(self) -> None:
                range_value = self.headers.get("Range", "")
                if range_value.startswith("bytes="):
                    start_text, end_text = range_value.removeprefix("bytes=").split("-", 1)
                    start = int(start_text)
                    end = min(len(payload) - 1, int(end_text))
                    self._headers(start, end, True)
                    self.wfile.write(payload[start:end + 1])
                    return
                self._headers(0, len(payload) - 1, False)
                self.wfile.write(payload)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            async def exercise() -> tuple[int, bytes]:
                reader = HttpPlaybackSource(
                    f"http://127.0.0.1:{server.server_port}/fixture.mp4",
                    client_address="127.0.0.1",
                )
                source_stat = await reader.stat()
                chunks = [chunk async for chunk in reader.open_range(64, 192)]
                return source_stat.size, b"".join(chunks)

            size, ranged = asyncio.run(exercise())
            self.assertEqual(size, len(payload))
            self.assertEqual(ranged, payload[64:256])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_background_preparation_uses_a_session_independent_snapshot(self) -> None:
        service = PlaybackPrepService()
        source = ResolvedMediaSource(
            catalog_path=self.catalog_path,
            relative_path=self.catalog_path.removeprefix("/media/"),
            local_path=self.media_file,
            cloud_path=None,
            local_exists=True,
            cloud_exists=False,
        )
        media = SimpleNamespace(
            id="m_snapshot_contract",
            source_fingerprint=source.fingerprint,
            probed_duration=5.0,
            container="mov,mp4",
            codec="h264",
            width=640,
            height=360,
            frame_rate=24.0,
            audio_metadata=[{"index": 0, "language": "eng", "label": "English", "default": True}],
            languages=["eng"],
        )
        scheduled: list[PlaybackMediaSnapshot] = []

        async def run() -> None:
            with (
                patch.object(service, "_schedule_video", side_effect=lambda _id, _fingerprint, _source, _rendition, snapshot, _priority, **_kwargs: scheduled.append(snapshot)),
                patch.object(service, "_schedule_audio", side_effect=lambda _id, _fingerprint, _source, _rendition, snapshot, _priority, **_kwargs: scheduled.append(snapshot)),
                patch.object(service, "_schedule_remaining"),
                patch.object(service, "rebuild_master", new=AsyncMock(return_value=None)),
                patch.object(service, "preparation_state", return_value="preparing"),
            ):
                await service.prepare(media.id, media, source, include_remaining=True, retry_errors=True)

        asyncio.run(run())
        self.assertTrue(scheduled)
        self.assertTrue(all(isinstance(item, PlaybackMediaSnapshot) for item in scheduled))
        media.width = 1
        self.assertTrue(all(item.width == 640 for item in scheduled))

    def test_quality_ladder_reaches_144p_without_upscaling(self) -> None:
        source_720p = SimpleNamespace(width=1280, height=720)
        renditions = playback_prep_service.video_renditions(source_720p)
        self.assertEqual([item.height for item in renditions], [720, 480, 360, 240, 144])
        self.assertTrue(all(item.height <= 720 for item in renditions))

        cinematic_source = SimpleNamespace(width=1920, height=800, quality="1080p")
        cinematic_renditions = playback_prep_service.video_renditions(cinematic_source)
        self.assertEqual(cinematic_renditions[0].height, 800)
        self.assertEqual(cinematic_renditions[0].label, "1080p")

    def test_requested_audio_is_promoted_to_foreground_work(self) -> None:
        service = PlaybackPrepService()
        source = ResolvedMediaSource(
            catalog_path=self.catalog_path,
            relative_path=self.catalog_path.removeprefix("/media/"),
            local_path=self.media_file,
            cloud_path=None,
            local_exists=True,
            cloud_exists=False,
        )
        media = SimpleNamespace(
            id="m_audio_priority",
            source_fingerprint=source.fingerprint,
            probed_duration=5.0,
            container="mov,mp4",
            codec="h264",
            width=640,
            height=360,
            frame_rate=24.0,
            quality="360p",
            audio_metadata=[
                {"index": 0, "language": "eng", "label": "English", "default": True, "source": "embedded"},
                {"index": 1, "language": "tur", "label": "Turkish", "default": False, "source": "external", "fileName": "tr.aac"},
            ],
            languages=["eng", "tur"],
        )
        scheduled = []

        async def run() -> str:
            with (
                patch.object(service, "_preempt_background_jobs", new=AsyncMock()) as preempt,
                patch.object(service, "_schedule_audio", side_effect=lambda *args, **kwargs: scheduled.append((args, kwargs))),
                patch.object(service, "_schedule_remaining"),
                patch.object(service, "playlist_ready", return_value=False),
            ):
                status = await service.prioritize_audio_rendition(media.id, media, source, "audio_1_tr")
                preempt.assert_awaited_once_with({f"{media.id}:{source.fingerprint}:audio_1_tr"})
                return status

        self.assertEqual(asyncio.run(run()), "preparing")
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0][3].name, "audio_1_tr")

    def test_rendition_status_distinguishes_streamable_complete_and_failed(self) -> None:
        media_id = f"m_status_{uuid.uuid4().hex}"
        fingerprint = "a" * 32
        rendition_name = "video_480p"
        rendition_dir = playback_prep_service.cache_path(media_id, fingerprint) / rendition_name
        self.assertEqual(playback_prep_service.rendition_status(media_id, fingerprint, rendition_name), "idle")
        rendition_dir.mkdir(parents=True)
        try:
            (rendition_dir / "playlist.m3u8").write_text(
                "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4,\nsegment_00000.m4s\n",
                encoding="utf-8",
            )
            (rendition_dir / "segment_00000.m4s").write_bytes(b"segment")
            self.assertEqual(playback_prep_service.rendition_status(media_id, fingerprint, rendition_name), "idle")
            (rendition_dir / "init.mp4").write_bytes(b"init")
            self.assertEqual(playback_prep_service.rendition_status(media_id, fingerprint, rendition_name), "streamable")
            key = f"{media_id}:{fingerprint}:{rendition_name}"
            playback_prep_service.active_jobs[key] = SimpleNamespace()  # type: ignore[assignment]
            playback_prep_service.job_priorities[key] = 100
            self.assertEqual(playback_prep_service.rendition_status(media_id, fingerprint, rendition_name), "streamable")
            playback_prep_service.job_priorities[key] = 0
            self.assertEqual(playback_prep_service.rendition_status(media_id, fingerprint, rendition_name), "streamable")
            playback_prep_service.active_jobs.pop(key, None)
            playback_prep_service.job_priorities.pop(key, None)
            (rendition_dir / ".complete").write_text("done", encoding="utf-8")
            self.assertEqual(playback_prep_service.rendition_status(media_id, fingerprint, rendition_name), "ready")
            (rendition_dir / ".complete").unlink()
            shutil.rmtree(rendition_dir)
            playback_prep_service._write_rendition_error(media_id, fingerprint, rendition_name, "TEST_FAILURE", "failed")
            self.assertEqual(playback_prep_service.rendition_status(media_id, fingerprint, rendition_name), "failed")
        finally:
            playback_prep_service.active_jobs.pop(f"{media_id}:{fingerprint}:{rendition_name}", None)
            playback_prep_service.job_priorities.pop(f"{media_id}:{fingerprint}:{rendition_name}", None)
            shutil.rmtree(playback_prep_service.cache_path(media_id, fingerprint).parent, ignore_errors=True)

    def test_complete_readiness_requires_every_rendition_and_reports_seekable_duration(self) -> None:
        media_id = f"m_readiness_{uuid.uuid4().hex}"
        fingerprint = "c" * 32
        media = SimpleNamespace(width=1280, height=720, quality="720p", codec="h264", audio_metadata=[])
        cache_path = playback_prep_service.cache_path(media_id, fingerprint)
        rendition_names = [item.name for item in playback_prep_service.video_renditions(media)]
        try:
            for index, rendition_name in enumerate(rendition_names):
                rendition_dir = cache_path / rendition_name
                rendition_dir.mkdir(parents=True)
                (rendition_dir / "playlist.m3u8").write_text(
                    "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4.0,\nsegment_00000.m4s\n#EXTINF:3.5,\nsegment_00001.m4s\n",
                    encoding="utf-8",
                )
                (rendition_dir / "init.mp4").write_bytes(b"init")
                (rendition_dir / "segment_00000.m4s").write_bytes(b"segment")
                (rendition_dir / "segment_00001.m4s").write_bytes(b"segment")
                if index == 0:
                    self.assertEqual(
                        playback_prep_service.rendition_seekable_until(media_id, fingerprint, rendition_name),
                        7.5,
                    )
                    self.assertFalse(playback_prep_service.switching_ready(media_id, fingerprint, media))
            self.assertTrue(playback_prep_service.switching_ready(media_id, fingerprint, media))
            self.assertFalse(playback_prep_service.fully_prepared(media_id, fingerprint, media))
            for rendition_name in rendition_names:
                (cache_path / rendition_name / ".complete").write_text("done", encoding="utf-8")
            self.assertTrue(playback_prep_service.fully_prepared(media_id, fingerprint, media))
        finally:
            shutil.rmtree(cache_path.parent, ignore_errors=True)

    def test_optional_rendition_failure_does_not_interrupt_ready_baseline(self) -> None:
        media_id = f"m_optional_failure_{uuid.uuid4().hex}"
        fingerprint = "b" * 32
        media = SimpleNamespace(width=1280, height=720, quality="720p", codec="h264", audio_metadata=[])
        cache_path = playback_prep_service.cache_path(media_id, fingerprint)
        baseline_dir = cache_path / "video_original"
        baseline_dir.mkdir(parents=True)
        try:
            (baseline_dir / "playlist.m3u8").write_text(
                "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4,\nsegment_00000.m4s\n#EXT-X-ENDLIST\n",
                encoding="utf-8",
            )
            (baseline_dir / "init.mp4").write_bytes(b"init")
            (baseline_dir / "segment_00000.m4s").write_bytes(b"segment")
            (cache_path / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            playback_prep_service._write_rendition_error(media_id, fingerprint, "video_480p", "TEST_FAILURE", "failed")
            playback_prep_service._write_preparation_error(media_id, fingerprint, "TEST_FAILURE", "failed")

            self.assertEqual(playback_prep_service.preparation_state(media_id, fingerprint, media), "ready")
            self.assertIsNone(playback_prep_service.required_preparation_error(media_id, fingerprint, media))
        finally:
            shutil.rmtree(cache_path.parent, ignore_errors=True)

    def test_failed_rendition_requires_an_explicit_retry_before_rescheduling(self) -> None:
        async def exercise(service: PlaybackPrepService) -> None:
            media_id = "m_sticky_failure"
            fingerprint = "b" * 32
            rendition_name = "video_480p"
            release = asyncio.Event()
            started = asyncio.Event()

            async def worker() -> None:
                started.set()
                await release.wait()

            service._write_rendition_error(
                media_id,
                fingerprint,
                rendition_name,
                "FFMPEG_PREPARATION_FAILED",
                "The rendition failed.",
            )

            service._schedule_job(media_id, fingerprint, rendition_name, worker(), 0)
            await asyncio.sleep(0)
            self.assertFalse(started.is_set())
            self.assertNotIn(f"{media_id}:{fingerprint}:{rendition_name}", service.active_jobs)
            self.assertEqual(service.rendition_status(media_id, fingerprint, rendition_name), "failed")

            service._schedule_job(
                media_id,
                fingerprint,
                rendition_name,
                worker(),
                0,
                retry_errors=True,
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertIsNone(service.rendition_error(media_id, fingerprint, rendition_name))
            self.assertIn(f"{media_id}:{fingerprint}:{rendition_name}", service.active_jobs)
            release.set()
            await asyncio.gather(*service.active_jobs.values())

        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            asyncio.run(exercise(service))

    def test_preparation_progress_reports_video_segments_and_audio_work_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            media_id = "m_progress_contract"
            fingerprint = "c" * 32
            media = SimpleNamespace(
                width=640,
                height=360,
                quality="360p",
                codec="h264",
                audio_metadata=[{"index": 0, "language": "en", "codec": "aac", "default": True}],
                languages=["en"],
            )
            baseline = service.baseline_video(media)
            audio = service.audio_renditions(media)[0]
            video_dir = service.cache_path(media_id, fingerprint) / baseline.name
            audio_dir = service.cache_path(media_id, fingerprint) / audio.name
            video_dir.mkdir(parents=True)
            audio_dir.mkdir(parents=True)
            for index in range(2):
                (video_dir / f"segment_{index:05d}.m4s").write_bytes(b"video")
            for index in range(3):
                (audio_dir / f"segment_{index:05d}.m4s").write_bytes(b"audio")
            audio_key = f"{media_id}:{fingerprint}:{audio.name}"
            service.running_jobs.add(audio_key)
            try:
                progress = service.preparation_progress(media_id, fingerprint, media)
                self.assertEqual(progress["stage"], "audio")
                self.assertEqual(progress["ready_segments"], 2)
                self.assertEqual(progress["active_workers"], 1)
            finally:
                service.running_jobs.discard(audio_key)

    def test_streamable_background_rendition_remains_advertised_during_preemption(self) -> None:
        async def exercise(service: PlaybackPrepService) -> None:
            media_id = "m_background_manifest"
            fingerprint = "c" * 32
            media = SimpleNamespace(width=1280, height=720, quality="720p", codec="h264", audio_metadata=[])
            cache_path = service.cache_path(media_id, fingerprint)
            for name in ("video_original", "video_480p"):
                rendition_dir = cache_path / name
                rendition_dir.mkdir(parents=True)
                (rendition_dir / "playlist.m3u8").write_text(
                    "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4,\nsegment_00000.m4s\n",
                    encoding="utf-8",
                )
                (rendition_dir / "init.mp4").write_bytes(b"init")
                (rendition_dir / "segment_00000.m4s").write_bytes(b"segment")
            (cache_path / "video_original" / ".complete").write_text("done", encoding="utf-8")
            key = f"{media_id}:{fingerprint}:video_480p"
            service.active_jobs[key] = SimpleNamespace()  # type: ignore[assignment]
            service.job_priorities[key] = 100
            await service.rebuild_master(media_id, fingerprint, media)
            master = (cache_path / "master.m3u8").read_text(encoding="utf-8")
            self.assertIn("video_original/playlist.m3u8", master)
            self.assertIn("video_480p/playlist.m3u8", master)
            await service._preempt_background_jobs(set())
            self.assertIn(key, service.active_jobs)
            self.assertEqual(service.rendition_status(media_id, fingerprint, "video_480p"), "streamable")
            service.active_jobs.pop(key, None)
            service.job_priorities.pop(key, None)

        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            asyncio.run(exercise(service))

    def test_external_dubbing_overrides_embedded_language_and_invalidates_identity(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg)
        sidecar_directory = self.media_directory / "sidecar-audio"
        sidecar_directory.mkdir()
        sidecar_video = sidecar_directory / "fixture.mp4"
        shutil.copy2(self.media_file, sidecar_video)
        audio_directory = sidecar_directory / "audio"
        audio_directory.mkdir()
        for language, frequency in (("eng", 330), ("tur", 550)):
            subprocess.run(
                [
                    str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000", "-t", "1",
                    "-c:a", "libmp3lame", str(audio_directory / f"{language}.mp3"),
                ],
                check=True,
                capture_output=True,
            )
        catalog_path = f"/media/Movies/{self.media_directory.name}/sidecar-audio/fixture.mp4"
        first_source = asyncio.run(resolve_media_source(catalog_path, check_cloud=False))
        first_fingerprint = first_source.fingerprint
        probe = asyncio.run(probe_completed_media(str(sidecar_video)))
        self.assertEqual([item["language"] for item in probe["audio_metadata"]], ["en", "tr"])
        self.assertTrue(all(item.get("source") == "external" for item in probe["audio_metadata"]))
        self.assertEqual([item.get("fileName") for item in probe["audio_metadata"]], ["eng.mp3", "tur.mp3"])
        refreshed = merge_local_external_audio(str(sidecar_video), [{"index": 0, "language": "en", "default": True}])
        self.assertEqual([item["language"] for item in refreshed], ["en", "tr"])
        self.assertTrue(all(item.get("source") == "external" for item in refreshed))
        with (audio_directory / "tur.mp3").open("ab") as handle:
            handle.write(b"\0")
        second_source = asyncio.run(resolve_media_source(catalog_path, check_cloud=False))
        self.assertNotEqual(first_fingerprint, second_source.fingerprint)
        self.assertEqual(first_source.video_fingerprint, second_source.video_fingerprint)

        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            media = SimpleNamespace(width=640, height=360, quality="360p")
            original = service.cache_path(
                "m_sidecar_reuse",
                first_source.video_fingerprint,
            ) / "video_original"
            original.mkdir(parents=True)
            (original / "playlist.m3u8").write_text(
                "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4,\nsegment_00000.m4s\n",
                encoding="utf-8",
            )
            (original / "init.mp4").write_bytes(b"init")
            (original / "segment_00000.m4s").write_bytes(b"unchanged-video")
            (original / ".complete").write_text("done", encoding="utf-8")

            reused = service.reuse_video_renditions(
                "m_sidecar_reuse",
                first_source.video_fingerprint,
                second_source.fingerprint,
                media,
            )
            target = service.cache_path(
                "m_sidecar_reuse",
                second_source.fingerprint,
            ) / "video_original"
            self.assertEqual(reused, ["video_original"])
            self.assertEqual((target / "segment_00000.m4s").read_bytes(), b"unchanged-video")
            self.assertTrue((target / ".complete").is_file())

    def test_verified_source_optimization_preserves_complete_video_and_audio_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            media = SimpleNamespace(
                width=640,
                height=360,
                quality="360p",
                codec="hevc",
                audio_metadata=[
                    {
                        "index": 0,
                        "language": "en",
                        "label": "English",
                        "codec": "aac",
                        "default": True,
                        "source": "embedded",
                    }
                ],
                languages=["en"],
            )
            source_fingerprint = "1" * 64
            target_fingerprint = "2" * 64
            for rendition_name in ("video_original", "audio_0_en"):
                rendition = service.cache_path(
                    "m_verified_optimization",
                    source_fingerprint,
                ) / rendition_name
                rendition.mkdir(parents=True)
                (rendition / "playlist.m3u8").write_text(
                    "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4,\nsegment_00000.m4s\n",
                    encoding="utf-8",
                )
                (rendition / "init.mp4").write_bytes(b"init")
                (rendition / "segment_00000.m4s").write_bytes(rendition_name.encode("utf-8"))
                (rendition / ".complete").write_text("done", encoding="utf-8")

            reused = service.reuse_verified_playback_cache(
                "m_verified_optimization",
                source_fingerprint,
                target_fingerprint,
                media,
            )
            self.assertEqual(set(reused), {"video_original", "audio_0_en"})

            master = asyncio.run(
                service.rebuild_master(
                    "m_verified_optimization",
                    target_fingerprint,
                    media,
                )
            )
            self.assertIsNotNone(master)
            master_text = master.read_text(encoding="utf-8")
            self.assertIn("video_original/playlist.m3u8", master_text)
            self.assertIn("audio_0_en/playlist.m3u8", master_text)
            self.assertEqual(
                (
                    service.cache_path("m_verified_optimization", target_fingerprint)
                    / "audio_0_en"
                    / "segment_00000.m4s"
                ).read_bytes(),
                b"audio_0_en",
            )

    def test_cloud_external_dubbing_uses_the_same_language_contract(self) -> None:
        response = SimpleNamespace(
            ok=True,
            stdout='[{"Name":"eng.mp3","Size":100,"ModTime":"2026-07-29T10:00:00Z"},{"Name":"tur.mp3","Size":120,"ModTime":"2026-07-29T10:01:00Z"}]',
        )
        embedded = [{"index": 0, "language": "en", "label": "English", "channels": 2, "default": True}]
        with patch.object(rclone_service, "run", new=AsyncMock(return_value=response)) as run:
            tracks = asyncio.run(probe_cloud_external_audio("streamhome:/Movies/Title/movie.mp4", embedded))
        self.assertEqual([item["language"] for item in tracks], ["en", "tr"])
        self.assertTrue(all(item["source"] == "external" for item in tracks))
        run.assert_awaited_once_with("lsjson", "streamhome:/Movies/Title/audio", "--files-only", timeout=30)

    def test_audio_and_subtitle_contracts_accept_standard_language_tags(self) -> None:
        self.assertEqual(normalize_language_tag("eng"), "en")
        self.assertEqual(normalize_language_tag("SPA"), "es")
        self.assertEqual(normalize_language_tag("fre"), "fr")
        self.assertEqual(normalize_language_tag("tur"), "tr")
        self.assertEqual(normalize_language_tag("pt_BR"), "pt-br")
        media = SimpleNamespace(
            subtitles=[
                {"language": "eng", "ext": ".vtt", "fileName": "subtitle_eng_main.vtt"},
                {"language": "eng", "ext": ".vtt", "fileName": "subtitle_eng_commentary.vtt"},
                {"language": "spa", "ext": ".vtt"},
                {"language": "fr", "ext": ".vtt"},
                {"language": "tr", "ext": ".vtt"},
                {"language": "zh-Hant-TW", "ext": ".vtt"},
            ]
        )
        tracks = subtitle_contract(media)
        self.assertTrue(all(item["id"].startswith("sub_") for item in tracks))
        self.assertEqual([item["id"] for item in tracks], [item["id"] for item in subtitle_contract(media)])
        self.assertEqual(len({item["id"] for item in tracks}), len(tracks))
        self.assertEqual([item["language"] for item in tracks], ["en", "en", "es", "fr", "tr", "zh-hant-tw"])
        self.assertEqual(subtitle_file_name(media.subtitles[0]), "subtitle_eng_main.vtt")
        self.assertEqual(subtitle_file_name(media.subtitles[1]), "subtitle_eng_commentary.vtt")
        self.assertIsNone(subtitle_file_name({"language": "eng", "fileName": "../other.vtt"}))

    def test_catalog_runtime_is_a_stable_fallback_when_probe_duration_is_missing(self) -> None:
        self.assertEqual(catalog_duration_seconds("1h 44m"), 6240)
        self.assertEqual(catalog_duration_seconds("104m"), 6240)
        self.assertEqual(catalog_duration_seconds("1:44"), 6240)
        self.assertEqual(media_duration_seconds(SimpleNamespace(probed_duration=None, duration="1h 44m")), 6240)
        self.assertEqual(media_duration_seconds(SimpleNamespace(probed_duration=6301.5, duration="1h 44m")), 6301.5)

    def test_cloud_fingerprint_changes_when_remote_identity_changes(self) -> None:
        old_engine = settings.STORAGE_ENGINE
        settings.STORAGE_ENGINE = "CLOUD"
        catalog_path = f"/media/Movies/Cloud_{uuid.uuid4().hex}/movie.mp4"

        async def resolve_with(modified: str):
            clear_cloud_object_cache()
            response = SimpleNamespace(
                ok=True,
                stdout=f'{{"Path":"movie.mp4","Size":2048,"ModTime":"{modified}","Hashes":{{"md5":"abc"}},"IsDir":false}}',
            )
            with patch.object(rclone_service, "executable", return_value="rclone"), patch.object(rclone_service, "run", new=AsyncMock(return_value=response)):
                return await resolve_media_source(catalog_path)

        try:
            first = asyncio.run(resolve_with("2026-07-21T10:00:00Z"))
            second = asyncio.run(resolve_with("2026-07-21T11:00:00Z"))
            self.assertTrue(first.cloud_exists)
            self.assertNotEqual(first.fingerprint, second.fingerprint)
        finally:
            clear_cloud_object_cache()
            settings.STORAGE_ENGINE = old_engine

    def test_cloud_source_metadata_is_reused_across_range_resolution(self) -> None:
        old_engine = settings.STORAGE_ENGINE
        settings.STORAGE_ENGINE = "CLOUD"
        catalog_path = f"/media/Movies/CloudCache_{uuid.uuid4().hex}/movie.mp4"
        response = SimpleNamespace(
            ok=True,
            stdout='{"Path":"movie.mp4","Size":4096,"ModTime":"2026-08-02T10:00:00Z","Hashes":{},"IsDir":false}',
        )
        clear_cloud_object_cache()
        try:
            with patch.object(rclone_service, "executable", return_value="rclone"), patch.object(
                rclone_service,
                "run",
                new=AsyncMock(return_value=response),
            ) as run:
                first = asyncio.run(resolve_media_source(catalog_path))
                second = asyncio.run(resolve_media_source(catalog_path))
            self.assertEqual(first.cloud_size, 4096)
            self.assertEqual(second.cloud_size, 4096)
            self.assertEqual(first.cloud_identity, second.cloud_identity)
            run.assert_awaited_once()
        finally:
            clear_cloud_object_cache()
            settings.STORAGE_ENGINE = old_engine

    def test_strict_open_suffix_and_invalid_ranges(self) -> None:
        self.assertEqual(parse_byte_range(None, 100), (0, 99, False))
        self.assertEqual(parse_byte_range("bytes=10-19", 100), (10, 19, True))
        self.assertEqual(parse_byte_range("bytes=90-", 100), (90, 99, True))
        self.assertEqual(parse_byte_range("bytes=-10", 100), (90, 99, True))
        for invalid in ("bytes=100-", "bytes=30-20", "bytes=0-1,3-4", "items=0-1"):
            with self.subTest(invalid=invalid), self.assertRaises(Exception) as context:
                parse_byte_range(invalid, 100)
            self.assertEqual(getattr(context.exception, "status_code", None), 416)

    def test_srt_conversion_is_atomic_utf8_and_preserves_recovery_source(self) -> None:
        source = self.media_directory / "subtitle_eng.srt"
        target = self.media_directory / "subtitle_eng.vtt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,500\nHello\n", encoding="utf-8")
        self.assertTrue(srt_to_vtt(str(source), str(target)))
        self.assertTrue(source.is_file())
        self.assertFalse(Path(f"{target}.tmp").exists())
        content = target.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("WEBVTT\n\n"))
        self.assertIn("00:00:00.000 --> 00:00:01.500", content)

    def test_silent_media_does_not_invent_audio_but_legacy_external_audio_is_discovered(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg)
        legacy_directory = self.media_directory / "legacy-silent"
        legacy_directory.mkdir()
        silent_video = legacy_directory / "silent.mp4"
        subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24", "-t", "1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(silent_video),
            ],
            check=True,
            capture_output=True,
        )
        silent_probe = asyncio.run(probe_completed_media(str(silent_video)))
        self.assertEqual(silent_probe["audio_metadata"], [])
        silent_media = SimpleNamespace(audio_metadata=[], languages=["eng"])
        self.assertEqual(playback_prep_service.audio_renditions(silent_media), [])

        audio_directory = legacy_directory / "audio"
        audio_directory.mkdir()
        subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
                "-c:a", "libmp3lame", str(audio_directory / "eng.mp3"),
            ],
            check=True,
            capture_output=True,
        )
        legacy_probe = asyncio.run(probe_completed_media(str(silent_video)))
        self.assertEqual(len(legacy_probe["audio_metadata"]), 1)
        self.assertEqual(legacy_probe["audio_metadata"][0]["language"], "en")
        legacy_media = SimpleNamespace(audio_metadata=legacy_probe["audio_metadata"])
        self.assertEqual(playback_prep_service.audio_renditions(legacy_media)[0].language, "en")

    def test_preparation_scheduler_deduplicates_and_cache_recovery_is_bounded(self) -> None:
        async def exercise_scheduler(service: PlaybackPrepService) -> None:
            release = asyncio.Event()
            calls = 0

            async def fake_remaining(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                await release.wait()

            service._schedule_remaining_after_baseline = fake_remaining  # type: ignore[method-assign]
            source = ResolvedMediaSource(
                catalog_path=self.catalog_path,
                relative_path=self.catalog_path.removeprefix("/media/"),
                local_path=self.media_file,
                cloud_path=None,
                local_exists=True,
                cloud_exists=False,
            )
            media = SimpleNamespace(id="m_deduplicated", source_fingerprint=source.fingerprint)
            service._schedule_remaining(media.id, source.fingerprint, source, media)
            service._schedule_remaining(media.id, source.fingerprint, source, media)
            await asyncio.sleep(0)
            self.assertEqual(calls, 1)
            self.assertEqual(sum(key.endswith(":remaining") for key in service.active_jobs), 1)
            release.set()
            await asyncio.gather(*service.active_jobs.values())

        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            asyncio.run(exercise_scheduler(service))

            interrupted_dir = service.cache_dir / "media" / "fingerprint" / ".video.tmp"
            interrupted_dir.mkdir(parents=True)
            (interrupted_dir / "partial.m4s").write_bytes(b"partial")
            interrupted_file = service.cache_dir / "orphan.tmp"
            interrupted_file.write_bytes(b"partial")
            service.recover_interrupted_outputs()
            self.assertFalse(interrupted_dir.exists())
            self.assertFalse(interrupted_file.exists())

            old_limit = settings.PLAYBACK_CACHE_GB
            try:
                settings.PLAYBACK_CACHE_GB = 0.00000003
                oldest = service.cache_dir / "old" / "fingerprint"
                newest = service.cache_dir / "new" / "fingerprint"
                oldest.mkdir(parents=True)
                newest.mkdir(parents=True)
                (oldest / "segment.m4s").write_bytes(b"o" * 24)
                (newest / "segment.m4s").write_bytes(b"n" * 24)
                os.utime(oldest, (1, 1))
                os.utime(newest, (2, 2))
                service.enforce_lru_limits()
                self.assertFalse(oldest.exists())
                self.assertTrue(newest.exists())
            finally:
                settings.PLAYBACK_CACHE_GB = old_limit

    def test_foreground_playback_preempts_background_renditions(self) -> None:
        async def exercise(service: PlaybackPrepService) -> None:
            background_started = asyncio.Event()
            foreground_started = asyncio.Event()
            hold_background = asyncio.Event()
            background_key = "m_background:abc:video_480p"
            foreground_key = "m_requested:def:video_original"

            async def background_job() -> None:
                async with service.semaphore:
                    service.running_jobs.add(background_key)
                    background_started.set()
                    try:
                        await hold_background.wait()
                    finally:
                        service.running_jobs.discard(background_key)

            async def foreground_job() -> None:
                async with service.semaphore:
                    service.running_jobs.add(foreground_key)
                    foreground_started.set()
                    service.running_jobs.discard(foreground_key)

            service._schedule_job("m_background", "abc", "video_480p", background_job(), 100)
            await asyncio.wait_for(background_started.wait(), timeout=2)
            await service._preempt_background_jobs({foreground_key})
            service._schedule_job("m_requested", "def", "video_original", foreground_job(), 0)
            await asyncio.wait_for(foreground_started.wait(), timeout=2)
            self.assertNotIn(background_key, service.active_jobs)
            self.assertNotIn(background_key, service.running_jobs)
            await asyncio.gather(*service.active_jobs.values(), return_exceptions=True)

        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            service.semaphore = asyncio.Semaphore(1)
            asyncio.run(exercise(service))

    def test_compatible_source_uses_fast_hls_packaging(self) -> None:
        async def exercise(service: PlaybackPrepService) -> None:
            source = await resolve_media_source(self.catalog_path, check_cloud=False)
            media = SimpleNamespace(codec="h264", width=1920, height=1080, quality="1080p")
            baseline = service.baseline_video(media)
            self.assertTrue(baseline.original)
            self.assertEqual(baseline.height, 1080)
            with patch.object(service, "_run_ffmpeg_job", new=AsyncMock()) as run_job:
                await service._transcode_video("m_fast", "abc", source, baseline, media)
                arguments = run_job.await_args.args[4]
                self.assertIn("copy", arguments)
                self.assertNotIn("libx264", arguments)

                audio = service.audio_renditions(SimpleNamespace(audio_metadata=[{
                    "index": 0,
                    "codec": "aac",
                    "language": "eng",
                    "default": True,
                }]))[0]
                await service._transcode_audio("m_fast", "abc", source, audio, media)
                audio_arguments = run_job.await_args.args[4]
                self.assertIn("copy", audio_arguments)
                self.assertNotIn("160k", audio_arguments)

            hevc = SimpleNamespace(codec="hevc", width=1920, height=1080, quality="1080p")
            self.assertEqual(service.baseline_video(hevc).height, 480)

        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            asyncio.run(exercise(service))

    def test_stalled_hls_job_is_terminated_with_a_specific_failure(self) -> None:
        class StalledProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.finished = asyncio.Event()
                self.killed = False

            async def wait(self) -> int:
                await self.finished.wait()
                return int(self.returncode or 0)

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9
                self.finished.set()

        async def exercise(service: PlaybackPrepService, directory: Path) -> None:
            process = StalledProcess()
            old_timeout = settings.PLAYBACK_JOB_STALL_SECONDS
            settings.PLAYBACK_JOB_STALL_SECONDS = 1
            try:
                with self.assertRaises(PlaybackPreparationError) as context:
                    await service._wait_for_ffmpeg_progress(process, directory)  # type: ignore[arg-type]
                self.assertEqual(context.exception.code, "PREPARATION_STALLED")
                self.assertTrue(process.killed)
            finally:
                settings.PLAYBACK_JOB_STALL_SECONDS = old_timeout

        with tempfile.TemporaryDirectory() as directory:
            service = PlaybackPrepService()
            service.cache_dir = Path(directory)
            asyncio.run(exercise(service, Path(directory)))

    def test_real_hls_preparation_contains_decodable_video_and_audio(self) -> None:
        async def run() -> tuple[Path, SimpleNamespace]:
            probe = await probe_completed_media(str(self.media_file))
            self.assertEqual(len(probe["audio_metadata"]), 2)
            self.assertTrue(probe["audio_metadata"][0]["default"])
            source = await resolve_media_source(self.catalog_path, check_cloud=False)
            media = SimpleNamespace(
                id="m_playback_fixture",
                video_url=self.catalog_path,
                source_fingerprint=source.fingerprint,
                probed_duration=probe["probed_duration"],
                container=probe["container"],
                codec=probe["codec"],
                width=probe["width"],
                height=probe["height"],
                frame_rate=probe["frame_rate"],
                audio_metadata=probe["audio_metadata"],
                languages=["eng", "tur"],
            )
            await playback_prep_service.prepare(media.id, media, source, include_remaining=False)
            relevant = [task for key, task in playback_prep_service.active_jobs.items() if key.startswith(f"{media.id}:{source.fingerprint}:")]
            await asyncio.gather(*relevant)
            await playback_prep_service.rebuild_master(media.id, source.fingerprint, media)
            return playback_prep_service.cache_path(media.id, source.fingerprint), media

        cache_path, media = asyncio.run(run())
        try:
            master = cache_path / "master.m3u8"
            self.assertTrue(master.is_file())
            self.assertTrue(any(cache_path.rglob("*.m4s")))
            self.assertTrue((cache_path / "audio_0_en" / "segment_00000.m4s").is_file(), [str(path) for path in cache_path.rglob("*")])
            self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", (cache_path / "video_original" / "playlist.m3u8").read_text(encoding="utf-8"))
            self.assertEqual(playback_prep_service.rendition_status(media.id, media.source_fingerprint, "video_original"), "ready")
            content = master.read_text(encoding="utf-8")
            self.assertIn("TYPE=AUDIO", content)
            self.assertIn("video_original/playlist.m3u8", content)
            self.assertNotIn("CODECS=", content)
            self.assertRegex(content, r"BANDWIDTH=\d+")
            self.assertRegex(content, r"AVERAGE-BANDWIDTH=\d+")
            rewritten = rewrite_hls_playlist(content, media.id, "ticket-value", Path("."))
            self.assertIn(f"/api/playback/hls/{media.id}/video_original/playlist.m3u8?ticket=ticket-value", rewritten)

            ffprobe = shutil.which("ffprobe")
            self.assertIsNotNone(ffprobe)
            result = subprocess.run(
                [str(ffprobe), "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(master)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stream_types = set(result.stdout.split())
            self.assertIn("video", stream_types)
            self.assertIn("audio", stream_types)
        finally:
            if os.getenv("KEEP_PLAYBACK_TEST_CACHE") != "1":
                shutil.rmtree(cache_path.parent, ignore_errors=True)

    def test_hevc_source_is_converted_to_browser_compatible_h264(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        self.assertIsNotNone(ffmpeg)
        self.assertIsNotNone(ffprobe)
        hevc_file = self.media_directory / "fixture-hevc.mp4"
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "2", "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(hevc_file),
        ]
        result = subprocess.run(command, check=False, capture_output=True)
        if result.returncode != 0:
            self.skipTest("This FFmpeg build cannot create the HEVC regression fixture")

        async def prepare() -> Path:
            catalog_path = f"/media/Movies/{self.media_directory.name}/{hevc_file.name}"
            probe = await probe_completed_media(str(hevc_file))
            self.assertEqual(probe["codec"], "hevc")
            source = await resolve_media_source(catalog_path, check_cloud=False)
            media = SimpleNamespace(
                id="m_hevc_playback_fixture",
                source_fingerprint=source.fingerprint,
                probed_duration=probe["probed_duration"],
                container=probe["container"],
                codec=probe["codec"],
                width=probe["width"],
                height=probe["height"],
                frame_rate=probe["frame_rate"],
                audio_metadata=probe["audio_metadata"],
                languages=["und"],
            )
            await playback_prep_service.prepare(media.id, media, source, include_remaining=False)
            tasks = [task for key, task in playback_prep_service.active_jobs.items() if key.startswith(f"{media.id}:{source.fingerprint}:")]
            await asyncio.gather(*tasks)
            return playback_prep_service.cache_path(media.id, source.fingerprint)

        cache_path = asyncio.run(prepare())
        try:
            playlist = cache_path / "video_original" / "playlist.m3u8"
            result = subprocess.run(
                [str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(playlist)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            codecs = {line.strip() for line in result.stdout.splitlines() if line.strip()}
            self.assertEqual(codecs, {"h264"})
        finally:
            shutil.rmtree(cache_path.parent, ignore_errors=True)

    def test_shutdown_cancels_and_boundedly_waits_for_active_preparation(self) -> None:
        service = PlaybackPrepService()

        async def run() -> None:
            blocker = asyncio.Event()
            task = asyncio.create_task(blocker.wait())
            service.active_jobs["shutdown-regression"] = task
            await asyncio.sleep(0)

            await service.shutdown(timeout=0.5)

            self.assertTrue(task.cancelled())
            service.active_jobs.clear()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
