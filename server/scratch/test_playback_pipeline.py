from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import settings
from routes.playback import parse_byte_range, rewrite_hls_playlist
from services.media_probe import probe_completed_media
from services.media_source import MediaSourceError, ResolvedMediaSource, canonicalize_catalog_path, is_safe_presentation_asset, resolve_media_source
from services.playback_prep import PlaybackPrepService, playback_prep_service
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

    def test_cloud_fingerprint_changes_when_remote_identity_changes(self) -> None:
        old_engine = settings.STORAGE_ENGINE
        settings.STORAGE_ENGINE = "CLOUD"
        catalog_path = f"/media/Movies/Cloud_{uuid.uuid4().hex}/movie.mp4"

        async def resolve_with(modified: str):
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
        self.assertEqual(legacy_probe["audio_metadata"][0]["language"], "eng")
        legacy_media = SimpleNamespace(audio_metadata=legacy_probe["audio_metadata"])
        self.assertEqual(playback_prep_service.audio_renditions(legacy_media)[0].language, "eng")

    def test_preparation_scheduler_deduplicates_and_cache_recovery_is_bounded(self) -> None:
        async def exercise_scheduler(service: PlaybackPrepService) -> None:
            release = asyncio.Event()
            calls = 0

            async def fake_remaining(*_args):
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
            self.assertTrue((cache_path / "audio_0_eng" / "segment_00000.m4s").is_file(), [str(path) for path in cache_path.rglob("*")])
            content = master.read_text(encoding="utf-8")
            self.assertIn("TYPE=AUDIO", content)
            self.assertIn("video_original/playlist.m3u8", content)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
