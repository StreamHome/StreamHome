from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from services.ffmpeg import download_and_merge


EXPECTED_HEADER = "alpha-header-regression"
EXPECTED_REFERER = "https://streamhome.invalid/"


class _HeaderProtectedMediaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, media_path: Path):
        self.media_path = media_path
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


class FFmpegHeaderRegression(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
