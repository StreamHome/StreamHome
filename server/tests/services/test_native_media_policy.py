"""Actual native-child transport and downloaded-loader policy regressions.

Run only in a disposable source installation. All sources, proxy listeners,
media files and SQLite rows in this module are synthetic.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from lifecycle.media_work import supervised_output
from models import Movie
from services.ffmpeg import download_and_merge
from services.ffmpeg_input import native_media_environment
from services.media_probe import probe_media_stream
from services.media_source import playback_source_fingerprint, resolve_media_source
from services.media_updates import transcode_remote_audio
from services.playback_prep import PlaybackPreparationError, PlaybackPrepService
from tests.services import test_controlled_source as source_fixtures


class NativeMediaPolicyTests(unittest.IsolatedAsyncioTestCase):
    origin = source_fixtures.ControlledSourceTests.origin
    dns = source_fixtures.ControlledSourceTests.dns

    async def asyncSetUp(self) -> None:
        await source_fixtures.ControlledSourceTests.asyncSetUp(self)
        self.ffmpeg = shutil.which("ffmpeg")
        if not self.ffmpeg or not shutil.which("ffprobe"):
            self.fail("Installed FFmpeg and FFprobe are required for native policy verification")

    async def asyncTearDown(self) -> None:
        await source_fixtures.ControlledSourceTests.asyncTearDown(self)

    async def fixture(self, name: str, codec: str, *options: str, audio: bool = False) -> Path:
        destination = self.root / name
        command = [self.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
                   "-f", "lavfi", "-i", "testsrc2=size=128x96:rate=5"]
        if audio:
            command.extend(["-f", "lavfi", "-i", "sine=frequency=440", "-c:a", "aac"])
        command.extend(["-t", "1", "-c:v", codec, "-threads", "1", *options, str(destination)])
        process = await asyncio.create_subprocess_exec(
            *command, env=native_media_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await supervised_output(process, timeout=20)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        self.routes["/" + name] = (200, {}, destination.read_bytes())
        return destination

    def source_url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    async def proxy(self) -> tuple[str, list[str]]:
        requests: list[str] = []

        async def serve(reader, writer):
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                target = raw.split(b"\r\n")[0].decode().split(" ")[1]
                requests.append(target)
                # A native proxy must never replace the application's relay
                # with an unrewritten playlist. The local listener handles this
                # neutral absolute URI; it never contacts a metadata endpoint.
                body = (b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
                        b"http://169.254.169.254/neutral.ts\n#EXT-X-ENDLIST\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\n"
                             b"Content-Length: " + str(len(body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            except (OSError, ConnectionError, asyncio.IncompleteReadError, TimeoutError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, ConnectionError):
                    pass

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.servers.append(server)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests

    async def test_environment_removes_case_insensitive_proxy_settings(self) -> None:
        inherited = {"PATH": "/synthetic/bin", "HOME": "/synthetic/home", "LANG": "C",
                     "http_proxy": "synthetic", "HTTPS_PROXY": "synthetic",
                     "All_Proxy": "synthetic", "No_PrOxY": "", "FTP_PROXY": "synthetic",
                     "proxy": "synthetic"}
        self.assertEqual(native_media_environment(inherited),
                         {"PATH": "/synthetic/bin", "HOME": "/synthetic/home", "LANG": "C"})
        self.assertIn("http_proxy", inherited, "building a child environment must not mutate its parent")

    async def test_probe_ignores_ambient_proxy_and_rejects_nested_private_hls(self) -> None:
        await self.fixture("source.mp4", "libx264", "-preset", "ultrafast", "-movflags", "+faststart")
        self.routes["/blocked.m3u8"] = (200, {"Content-Type": "application/vnd.apple.mpegurl"},
                                       b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
                                       b"http://169.254.169.254/neutral.ts\n#EXT-X-ENDLIST\n")
        proxy_url, requests = await self.proxy()
        ambient = {"http_proxy": proxy_url, "HTTPS_PROXY": proxy_url,
                   "ALL_PROXY": proxy_url, "no_proxy": ""}
        with patch.dict(os.environ, ambient):
            direct = await asyncio.wait_for(probe_media_stream(self.source_url("source.mp4"),
                                                               client_address="127.0.0.1"), timeout=15)
            blocked = await asyncio.wait_for(probe_media_stream(self.source_url("blocked.m3u8"),
                                                                client_address="127.0.0.1",
                                                                video_source_type="hls"), timeout=15)
        self.assertTrue(direct["has_video"])
        self.assertFalse(blocked["has_video"])
        self.assertEqual(blocked["failure"].code, "UNSAFE_SOURCE")
        self.assertTrue(any(path == "/source.mp4" for path, _ in self.requests))
        self.assertEqual(requests, [], "native proxy must receive neither relay capability nor nested URI")

    async def test_ingestion_and_audio_conversion_ignore_ambient_proxy(self) -> None:
        await self.fixture("source.mp4", "libx264", "-preset", "ultrafast", "-movflags", "+faststart", audio=True)
        proxy_url, requests = await self.proxy()
        with patch.dict(os.environ, {"http_proxy": proxy_url, "NO_PROXY": "", "no_proxy": ""}):
            output = self.root / "accepted.mp4"
            success, failure = await asyncio.wait_for(download_and_merge(
                "native-proxy-ingestion", self.source_url("source.mp4"), None,
                {"Authorization": "Bearer synthetic", "User-Agent": "SyntheticMediaSender"},
                str(output), 1, client_address="127.0.0.1", preview_enabled=False,
            ), timeout=20)
            self.assertTrue(success, failure)
            audio = self.root / "sidecar.m4a"
            await asyncio.wait_for(transcode_remote_audio(
                self.source_url("source.mp4"), audio, headers={"Authorization": "Bearer synthetic"},
                source_type="auto", client_address="127.0.0.1",
            ), timeout=20)
        self.assertGreater(output.stat().st_size, 0)
        self.assertGreater(audio.stat().st_size, 0)
        self.assertTrue(any(headers.get("authorization") == "Bearer synthetic" for _, headers in self.requests))
        self.assertEqual(requests, [])

    async def test_safe_direct_formats_remain_probeable_through_http(self) -> None:
        fixtures = [("video.avi", "mpeg4", ()),
                    ("video.mpg", "mpeg2video", ()),
                    ("video.flv", "flv", ()),
                    ("video.h264", "libx264", ("-preset", "ultrafast")),
                    ("video.hevc", "libx265", ("-x265-params", "pools=1:frame-threads=1"))]
        for name, codec, options in fixtures:
            with self.subTest(format=name):
                path = await self.fixture(name, codec, *options)
                local = await probe_media_stream(str(path))
                remote = await asyncio.wait_for(probe_media_stream(self.source_url(name),
                                                                  client_address="127.0.0.1"), timeout=15)
                self.assertTrue(local["has_video"], local.get("failure"))
                self.assertTrue(remote["has_video"], remote.get("failure"))
                self.assertEqual(remote["width"], local["width"])
                self.assertEqual(remote["height"], local["height"])
                if name == "video.avi":
                    output = self.root / "accepted-avi.mp4"
                    success, failure = await asyncio.wait_for(download_and_merge(
                        "native-safe-avi", self.source_url(name), None, {}, str(output), 1,
                        client_address="127.0.0.1", preview_enabled=False,
                    ), timeout=20)
                    self.assertTrue(success, failure)
                    accepted = await probe_media_stream(str(output))
                    self.assertTrue(accepted["has_video"], accepted.get("failure"))

    async def test_actual_playback_preparation_rejects_downloaded_external_loaders(self) -> None:
        media_root, temp_root = self.root / "media", self.root / "temp"
        media_root.mkdir()
        temp_root.mkdir()
        engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'preparation.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        resource = self.source_url("neutral-resource.ts")
        documents = [f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{resource}\n#EXT-X-ENDLIST\n",
                     f"ffconcat version 1.0\nfile '{resource}'\n",
                     '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
                     'type="static" mediaPresentationDuration="PT1S" minBufferTime="PT1S">'
                     '<Period><AdaptationSet mimeType="video/mp4"><Representation id="1" '
                     f'bandwidth="5000" codecs="avc1.42c01e"><BaseURL>{resource}</BaseURL>'
                     '</Representation></AdaptationSet></Period></MPD>']
        proxy_url, proxy_requests = await self.proxy()
        try:
            with patch.object(settings, "MEDIA_DIR", str(media_root)), patch.object(settings, "TEMP_DIR", str(temp_root)), \
                    patch.object(settings, "STORAGE_ENGINE", "LOCAL"), patch.object(db_module, "engine", engine), \
                    patch.dict(os.environ, {"http_proxy": proxy_url, "no_proxy": ""}):
                service = PlaybackPrepService()
                for number, document in enumerate(documents):
                    with self.subTest(loader=number):
                        media_id = f"m_{4901 + number}"
                        path = media_root / "Movies" / str(number) / "movie.mp4"
                        path.parent.mkdir(parents=True)
                        path.write_text(document)
                        catalog_path = f"/media/Movies/{number}/movie.mp4"
                        source = await resolve_media_source(catalog_path)
                        fingerprint = playback_source_fingerprint(source, [])
                        movie = Movie(id=media_id, title="Synthetic", description="", thumbnail_url="", banner_url="",
                                      video_url=catalog_path, duration="1s", release_year=2026, quality="Source",
                                      source_fingerprint=fingerprint, catalog_source="server", availability="available",
                                      codec="h264", container="mp4", width=128, height=96, frame_rate=5, probed_duration=1)
                        async with AsyncSession(engine, expire_on_commit=False) as db:
                            db.add(movie)
                            await db.commit()
                        rendition = service.baseline_video(movie)
                        await asyncio.wait_for(service._transcode_video(media_id, fingerprint, source, rendition, movie), timeout=15)
                        self.assertEqual(service.rendition_error(media_id, fingerprint, rendition.name)["code"],
                                         "FFMPEG_PREPARATION_FAILED")
                        self.assertFalse(service.rendition_verified(media_id, fingerprint, rendition.name))
                self.assertEqual(self.requests, [], "downloaded loader must not issue native external requests")
                self.assertEqual(proxy_requests, [])
        finally:
            await engine.dispose()

    async def test_generated_playlist_verification_rejects_external_resources(self) -> None:
        target = self.root / "prepared"
        target.mkdir()
        playlist = target / "playlist.m3u8"
        playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
                            + self.source_url("neutral-resource.ts") + "\n#EXT-X-ENDLIST\n")
        service = PlaybackPrepService()
        with self.assertRaises(PlaybackPreparationError) as rejected:
            await asyncio.to_thread(service._verify_rendition_output, target, "v_240p")
        self.assertEqual(rejected.exception.code, "RENDITION_VERIFICATION_FAILED")
        with self.assertRaises(PlaybackPreparationError):
            await asyncio.to_thread(service._probe_playlist_timeline, playlist, "video")
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
