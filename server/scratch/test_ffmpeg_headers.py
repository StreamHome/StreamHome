from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from services.ffmpeg import download_and_merge
from services.ingest_preview import ingest_preview_service
from services.media_probe import probe_media_stream


EXPECTED_HEADER = "alpha-header-regression"
EXPECTED_REFERER = "https://streamhome.invalid/"


class _HeaderProtectedMediaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, media_path: Path, *, stream_seconds: float = 0):
        self.media_path = media_path
        self.stream_seconds = max(0.0, stream_seconds)
        self.required_headers_seen = False
        super().__init__(("127.0.0.1", 0), _HeaderProtectedMediaHandler)


class _HeaderProtectedMediaHandler(BaseHTTPRequestHandler):
    @property
    def media_server(self) -> _HeaderProtectedMediaServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        self._serve(send_body=False)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._serve(send_body=True)

    def _serve(self, *, send_body: bool) -> None:
        headers_match = (
            self.headers.get("X-StreamHome-Test") == EXPECTED_HEADER
            and self.headers.get("Referer") == EXPECTED_REFERER
        )
        if not headers_match:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.media_server.required_headers_seen = True
        media_path = self.media_server.media_path
        file_size = media_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(file_size))
        self.send_header("Connection", "close")
        self.end_headers()
        if send_body:
            with media_path.open("rb") as source:
                if self.media_server.stream_seconds <= 0:
                    shutil.copyfileobj(source, self.wfile)
                    return
                bytes_per_second = max(1.0, file_size / self.media_server.stream_seconds)
                while chunk := source.read(16 * 1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(len(chunk) / bytes_per_second)


class _HeaderProtectedHlsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.requested_paths: list[str] = []
        super().__init__(("127.0.0.1", 0), _HeaderProtectedHlsHandler)


class _HeaderProtectedHlsHandler(BaseHTTPRequestHandler):
    @property
    def media_server(self) -> _HeaderProtectedHlsServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        self._serve(send_body=False)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._serve(send_body=True)

    def _serve(self, *, send_body: bool) -> None:
        if self.headers.get("Referer") != EXPECTED_REFERER:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        relative_path = unquote(urlsplit(self.path).path).lstrip("/")
        target = (self.media_server.root / relative_path).resolve()
        if not target.is_relative_to(self.media_server.root) or not target.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.media_server.requested_paths.append(relative_path)
        content_type = "text/plain" if target.suffix == ".txt" else "video/mp2t"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Connection", "close")
        self.end_headers()
        if send_body:
            with target.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)


def _create_media_fixture(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise unittest.SkipTest("FFmpeg is not installed")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:d=1",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-an",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _create_long_media_fixture(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise unittest.SkipTest("FFmpeg is not installed")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=16",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=16",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )


def _create_disguised_hls_fixture(root: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise unittest.SkipTest("FFmpeg is not installed")
    manifest_path = root / "master.txt"
    try:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:r=24:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=44100:duration=2",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "24",
                "-c:a",
                "aac",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "0",
                "-hls_segment_filename",
                str(root / "hidden-%03d.ts"),
                str(manifest_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise unittest.SkipTest(f"FFmpeg cannot create the HLS regression fixture: {exc.stderr}") from exc
    return manifest_path


def _create_separate_disguised_hls_fixtures(root: Path) -> tuple[Path, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise unittest.SkipTest("FFmpeg is not installed")
    video_manifest = root / "video-master.txt"
    audio_manifest = root / "audio-master.txt"
    commands = [
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:r=24:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "24",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(root / "video-hidden-%03d.ts"),
            str(video_manifest),
        ],
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=44100:duration=2",
            "-vn",
            "-c:a",
            "aac",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(root / "audio-hidden-%03d.ts"),
            str(audio_manifest),
        ],
    ]
    try:
        for command in commands:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
    except subprocess.CalledProcessError as exc:
        raise unittest.SkipTest(f"FFmpeg cannot create separate HLS regression fixtures: {exc.stderr}") from exc
    return video_manifest, audio_manifest


def _probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise unittest.SkipTest("FFprobe is not installed")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return float(result.stdout.strip())


def _probe_stream_types(path: Path) -> set[str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise unittest.SkipTest("FFprobe is not installed")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


class FFmpegHeaderRegression(unittest.TestCase):
    def test_preview_becomes_ready_before_throttled_download_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "long-source.mp4"
            output_path = temporary_path / "long-output.mp4"
            task_id = f"preview-{int(time.time() * 1000)}"
            _create_long_media_fixture(source_path)

            server = _HeaderProtectedMediaServer(source_path, stream_seconds=8)
            thread = threading.Thread(
                target=server.serve_forever,
                name="ffmpeg-growing-preview-regression",
                daemon=True,
            )
            thread.start()

            async def exercise() -> tuple[bool, object, bool]:
                port = int(server.server_address[1])
                download = asyncio.create_task(
                    download_and_merge(
                        task_id=task_id,
                        video_url=f"http://127.0.0.1:{port}/long-source.mp4",
                        audio_url=None,
                        headers={
                            "X-StreamHome-Test": EXPECTED_HEADER,
                            "Referer": EXPECTED_REFERER,
                        },
                        output_path=str(output_path),
                        duration_secs=16.0,
                    )
                )
                ready_before_completion = False
                deadline = time.monotonic() + 20
                while not download.done() and time.monotonic() < deadline:
                    preview_status = ingest_preview_service.status(task_id)
                    if preview_status["phase"] == "ready":
                        ready_before_completion = True
                        break
                    await asyncio.sleep(0.1)
                success, failure = await download
                return success, failure, ready_before_completion

            try:
                success, failure, ready_before_completion = asyncio.run(exercise())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            failure_message = failure.display if failure else "unknown failure"
            self.assertTrue(success, failure_message)
            self.assertTrue(ready_before_completion, "The preview did not become playable before the final file completed.")
            self.assertTrue(ingest_preview_service.playlist_path(task_id).is_file())
            self.assertTrue(output_path.is_file())
            ingest_preview_service.remove(task_id)

    def test_missing_disguised_manifest_preserves_http_not_found_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            server = _HeaderProtectedHlsServer(Path(temporary_directory))
            thread = threading.Thread(
                target=server.serve_forever,
                name="ffmpeg-missing-hidden-hls-regression",
                daemon=True,
            )
            thread.start()
            try:
                port = int(server.server_address[1])
                probe = asyncio.run(
                    probe_media_stream(
                        f"http://127.0.0.1:{port}/missing-master.txt",
                        headers={"Referer": EXPECTED_REFERER},
                    )
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            failure = probe.get("failure")
            self.assertIsNotNone(failure)
            self.assertEqual(failure.code, "SOURCE_NOT_FOUND")

    def test_http_headers_are_sent_and_output_is_playable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "source.mp4"
            output_path = temporary_path / "output.mp4"
            _create_media_fixture(source_path)

            server = _HeaderProtectedMediaServer(source_path)
            thread = threading.Thread(
                target=server.serve_forever,
                name="ffmpeg-header-regression",
                daemon=True,
            )
            thread.start()
            try:
                port = int(server.server_address[1])
                success, failure = asyncio.run(
                    download_and_merge(
                        task_id="test-ffmpeg-headers",
                        video_url=f"http://127.0.0.1:{port}/source.mp4",
                        audio_url=None,
                        headers={
                            "X-StreamHome-Test": EXPECTED_HEADER,
                            "Referer": EXPECTED_REFERER,
                        },
                        output_path=str(output_path),
                        duration_secs=1.0,
                    )
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            failure_message = failure.display if failure else "unknown failure"
            self.assertTrue(success, failure_message)
            self.assertTrue(server.required_headers_seen)
            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)
            self.assertGreater(_probe_duration(output_path), 0.5)

    def test_disguised_hls_manifest_is_detected_with_video_audio_and_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            _create_disguised_hls_fixture(temporary_path)
            output_path = temporary_path / "disguised-output.mp4"

            server = _HeaderProtectedHlsServer(temporary_path)
            thread = threading.Thread(
                target=server.serve_forever,
                name="ffmpeg-hidden-hls-regression",
                daemon=True,
            )
            thread.start()
            try:
                port = int(server.server_address[1])
                source_url = f"http://127.0.0.1:{port}/master.txt"
                probe = asyncio.run(
                    probe_media_stream(
                        source_url,
                        headers={"Referer": EXPECTED_REFERER},
                    )
                )
                failure = probe.get("failure")
                self.assertIsNone(failure, failure.display if failure else "")
                self.assertTrue(probe["has_video"])
                self.assertTrue(probe["has_audio"])
                self.assertEqual(probe["video_source_type"], "hls")

                success, download_failure = asyncio.run(
                    download_and_merge(
                        task_id="test-hidden-hls",
                        video_url=source_url,
                        audio_url=None,
                        headers={"Referer": EXPECTED_REFERER},
                        output_path=str(output_path),
                        duration_secs=2.0,
                        video_source_type=probe["video_source_type"],
                    )
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            failure_message = download_failure.display if download_failure else "unknown failure"
            self.assertTrue(success, failure_message)
            self.assertTrue(output_path.is_file())
            self.assertGreater(_probe_duration(output_path), 1.0)
            self.assertIn("master.txt", server.requested_paths)
            self.assertTrue(any(path.endswith(".ts") for path in server.requested_paths))

    def test_explicit_disguised_video_and_audio_manifests_are_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            video_manifest, audio_manifest = _create_separate_disguised_hls_fixtures(temporary_path)
            output_path = temporary_path / "separate-output.mp4"

            server = _HeaderProtectedHlsServer(temporary_path)
            thread = threading.Thread(
                target=server.serve_forever,
                name="ffmpeg-separate-hidden-hls-regression",
                daemon=True,
            )
            thread.start()
            try:
                port = int(server.server_address[1])
                video_url = f"http://127.0.0.1:{port}/{video_manifest.name}"
                audio_url = f"http://127.0.0.1:{port}/{audio_manifest.name}"
                probe = asyncio.run(
                    probe_media_stream(
                        video_url,
                        audio_url,
                        headers={"Referer": EXPECTED_REFERER},
                        video_source_type="hls",
                        audio_source_type="hls",
                    )
                )
                failure = probe.get("failure")
                self.assertIsNone(failure, failure.display if failure else "")
                self.assertTrue(probe["has_video"])
                self.assertTrue(probe["has_audio"])
                self.assertEqual(probe["video_source_type"], "hls")
                self.assertEqual(probe["audio_source_type"], "hls")

                success, download_failure = asyncio.run(
                    download_and_merge(
                        task_id="test-separate-hidden-hls",
                        video_url=video_url,
                        audio_url=audio_url,
                        headers={"Referer": EXPECTED_REFERER},
                        output_path=str(output_path),
                        duration_secs=2.0,
                        video_source_type="hls",
                        audio_source_type="hls",
                    )
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            failure_message = download_failure.display if download_failure else "unknown failure"
            self.assertTrue(success, failure_message)
            self.assertTrue(output_path.is_file())
            self.assertEqual(_probe_stream_types(output_path), {"video", "audio"})
            self.assertIn(video_manifest.name, server.requested_paths)
            self.assertIn(audio_manifest.name, server.requested_paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
