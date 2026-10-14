"""Native asyncio/uvloop consumer closure must not poison an ingestion relay.

All sockets and media are synthetic. Run from the disposable source runner.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import httpx

from lifecycle.coordination import get_runtime_coordinator
from lifecycle.media_work import supervised_output
from services import controlled_source
from services.controlled_source import _ConsumerDisconnected, _send, controlled_media_input
from services.ffmpeg_input import ffmpeg_network_input_options

try:
    import uvloop
except ImportError:
    uvloop = None


class ConsumerDisconnectCases:
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.handlers = set()
        self.release = asyncio.Event()
        self.body = b"x" * (4 * 65536)
        self.truncate = False
        self.servers = []
        self.baseline = get_runtime_coordinator().active_work()

        async def serve(reader, writer):
            self.handlers.add(asyncio.current_task())
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
                path = raw.split(b" ", 2)[1]
                if path == b"/master.m3u8":
                    body = b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n"
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                    await writer.drain()
                    return
                writer.write(f"HTTP/1.1 206 Partial Content\r\nContent-Length: {len(self.body)}\r\nContent-Range: bytes 0-{len(self.body)-1}/{len(self.body)}\r\nConnection: close\r\n\r\n".encode() + self.body[:65536])
                await writer.drain()
                await self.release.wait()
                if self.truncate:
                    return
                for offset in range(65536, len(self.body), 65536):
                    await asyncio.sleep(.02)
                    writer.write(self.body[offset:offset + 65536])
                    await writer.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                pass
            except RuntimeError:
                if not writer.is_closing():
                    raise
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
                self.handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.servers.append(server)
        self.origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.release.set()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        if self.handlers:
            await asyncio.wait_for(asyncio.gather(*self.handlers), 5)
        self.temporary.cleanup()
        self.assertEqual(get_runtime_coordinator().active_work(), self.baseline)

    async def idle(self, relay):
        async with asyncio.timeout(5):
            while relay.tasks:
                await asyncio.sleep(.01)

    async def test_hls_prefix_consumer_closure_keeps_later_requests_healthy(self):
        owners = []
        async with controlled_media_input(self.origin + "/master.m3u8", client_address="127.0.0.1", owner_callback=owners.append) as local:
            relay = owners[0]
            async with httpx.AsyncClient(trust_env=False) as consumer:
                playlist = (await consumer.get(local)).text
                segment = next(line for line in playlist.splitlines() if line.startswith("http"))
                target = urlsplit(segment)
                reader, writer = await asyncio.open_connection(target.hostname, target.port)
                try:
                    writer.write(f"GET {target.path} HTTP/1.1\r\nHost: localhost\r\nRange: bytes=0-\r\n\r\n".encode())
                    await writer.drain()
                    self.assertIn(b"206", await reader.readuntil(b"\r\n\r\n"))
                    self.assertEqual(await reader.readexactly(65536), self.body[:65536])
                    writer.transport.abort()
                    await asyncio.sleep(.1)
                    self.release.set()
                    await self.idle(relay)
                    self.assertIsNone(relay.failure)
                    self.assertEqual(relay.failure_count, 0)
                    response = await consumer.get(segment)
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.content, self.body)
                finally:
                    writer.close()
                    await writer.wait_closed()
        self.assertTrue(relay.closed)
        self.assertFalse(relay.tasks)
        self.assertFalse(controlled_source.is_controlled_input(local))

    async def test_truncated_upstream_still_rejects_acceptance(self):
        self.truncate = True
        self.release.set()
        owners = []
        with self.assertRaises(httpx.TransportError):
            async with controlled_media_input(self.origin + "/master.m3u8", client_address="127.0.0.1", owner_callback=owners.append) as local:
                async with httpx.AsyncClient(trust_env=False) as consumer:
                    playlist = (await consumer.get(local)).text
                    segment = next(line for line in playlist.splitlines() if line.startswith("http"))
                    with self.assertRaises(httpx.RemoteProtocolError):
                        await consumer.get(segment)
                    await self.idle(owners[0])
        self.assertGreater(owners[0].failure_count, 0)
        self.assertEqual(owners[0].failures[0]["http_status"], 206)

    async def test_native_ffprobe_prefix_exit_does_not_reject_valid_media(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        self.assertTrue(ffmpeg and ffprobe, "Native media tools are required")
        media = self.root / "synthetic.mp4"
        process = await asyncio.create_subprocess_exec(ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=320x180:rate=25", "-t", "16", "-c:v", "libx264", "-threads", "1", "-preset",
            "ultrafast", "-movflags", "+faststart", str(media), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await supervised_output(process, timeout=30)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.body = media.read_bytes()
        self.assertGreater(len(self.body), 65536)
        owners = []
        async with controlled_media_input(self.origin + "/synthetic.mp4", client_address="127.0.0.1", owner_callback=owners.append) as local:
            self.release.set()
            process = await asyncio.create_subprocess_exec(ffprobe, "-v", "error", "-show_entries", "stream=codec_type,width,height",
                "-of", "json", *ffmpeg_network_input_options(local), local, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await supervised_output(process, timeout=15)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout)["streams"][0]["height"], 180)
            await self.idle(owners[0])
            self.assertIsNone(owners[0].failure)
            self.assertEqual(owners[0].failure_count, 0)


class AsyncioConsumerDisconnectTests(ConsumerDisconnectCases, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipIf(uvloop is None, "uvloop is required on the supported Linux runtime")
class UvloopConsumerDisconnectTests(ConsumerDisconnectCases, unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        # IsolatedAsyncioTestCase.loop_factory is only honored from Python 3.13.
        # Runner supports explicit factories on every supported Python (3.11+).
        assert self._asyncioRunner is None, "asyncio runner is already initialized"
        self._asyncioRunner = asyncio.Runner(
            debug=True, loop_factory=uvloop.new_event_loop if uvloop is not None else None
        )

    async def asyncSetUp(self):
        self.assertIsInstance(asyncio.get_running_loop(), uvloop.Loop)
        await super().asyncSetUp()


class SendBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_error_on_an_open_writer_is_preserved(self):
        for operation in ("write", "drain"):
            with self.subTest(operation=operation):
                error = RuntimeError("synthetic writer defect")
                writer = Mock(spec=asyncio.StreamWriter)
                writer.is_closing.return_value = False
                if operation == "write":
                    writer.write.side_effect = error
                else:
                    writer.drain = AsyncMock(side_effect=error)
                with self.assertRaises(RuntimeError) as raised:
                    await _send(writer, b"synthetic")
                self.assertIs(raised.exception, error)

    async def test_closure_during_write_or_drain_is_consumer_disconnect(self):
        for operation in ("write", "drain"):
            with self.subTest(operation=operation):
                writer = Mock(spec=asyncio.StreamWriter)
                writer.is_closing.return_value = True
                if operation == "write":
                    writer.write.side_effect = RuntimeError("synthetic closed transport")
                else:
                    writer.drain = AsyncMock(side_effect=RuntimeError("synthetic closed transport"))
                with self.assertRaises(_ConsumerDisconnected):
                    await _send(writer, b"synthetic")


if __name__ == "__main__":
    unittest.main()
