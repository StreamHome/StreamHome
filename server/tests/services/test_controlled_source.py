"""Actual connection, redirect, HLS and FFmpeg source-boundary regressions.

All origins, TLS certificates, media and databases are synthetic and disposable.
Run application imports only from a disposable source installation.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import re
import shutil
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from config import settings
from lifecycle.coordination import get_runtime_coordinator
from lifecycle.media_work import supervised_output
from lifecycle.source_network import PinnedAsyncTransport
from services.controlled_source import SourceRelay, controlled_media_input, is_controlled_input
from services.ffmpeg import download_and_merge
from services.ffmpeg_input import ffmpeg_network_input_options
from services.ingestion_security import UnsafeIngestionSource, validated_stream_request, validate_url
from services.playback_source import HttpPlaybackSource
from services.media_probe import probe_completed_media, probe_media_stream, _probe_local_audio_timing
from services.audio_extractor import extract_audio_and_strip_video


class ControlledSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.servers: list[asyncio.Server] = []
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.original_getaddrinfo = socket.getaddrinfo
        self.trusted = patch.object(settings, "INGEST_TRUSTED_HOSTS", [])
        self.trusted.start()
        self.server = await self.origin()
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.trusted.stop()
        self.temporary.cleanup()

    async def origin(self, ssl_context=None) -> asyncio.Server:
        async def serve(reader, writer):
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                lines = raw.decode("iso-8859-1").split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                headers = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                           for line in lines[1:] if ":" in line}
                self.requests.append((path, headers))
                status, response_headers, body = self.routes.get(path, (404, {}, b""))
                response_headers = dict(response_headers)
                if status == 200 and headers.get("range"):
                    start, end = headers["range"].removeprefix("bytes=").split("-", 1)
                    start = int(start or "0")
                    end = min(int(end) if end else len(body) - 1, len(body) - 1)
                    response_headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                    status, body = 206, body[start:end + 1]
                response_headers.setdefault("Content-Length", str(len(body)))
                response_headers.setdefault("Accept-Ranges", "bytes")
                response_headers["Connection"] = "close"
                wire = [f"HTTP/1.1 {status} Synthetic"] + [f"{key}: {value}" for key, value in response_headers.items()]
                writer.write(("\r\n".join(wire) + "\r\n\r\n").encode("iso-8859-1"))
                if method != "HEAD":
                    writer.write(body)
                await writer.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
        server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=ssl_context)
        self.servers.append(server)
        return server

    def dns(self, destinations: dict[str, str]):
        def getaddrinfo(host, port, *args, **kwargs):
            if host in destinations:
                address = destinations[host]
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
            return self.original_getaddrinfo(host, port, *args, **kwargs)
        return patch.object(socket, "getaddrinfo", side_effect=getaddrinfo)

    async def test_public_validation_cannot_rebind_the_connection_to_loopback(self) -> None:
        calls = 0
        def rebinding(host, port, *args, **kwargs):
            nonlocal calls
            if host == "rebind.test":
                calls += 1
                address = "93.184.216.34" if calls == 1 else "127.0.0.1"
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
            return self.original_getaddrinfo(host, port, *args, **kwargs)
        async with httpx.AsyncClient(trust_env=False) as client:
            with patch.object(socket, "getaddrinfo", side_effect=rebinding):
                with self.assertRaises(UnsafeIngestionSource):
                    await validated_stream_request(client, f"http://rebind.test:{self.port}/media",
                                                   client_address="8.8.8.8")
        self.assertEqual(calls, 2)
        self.assertEqual(self.requests, [])

    async def test_numeric_dial_preserves_http_origin_host(self) -> None:
        settings.INGEST_TRUSTED_HOSTS = ["pinned.test"]
        self.routes["/media"] = (200, {}, b"synthetic media")
        with self.dns({"pinned.test": "127.0.0.1"}) as resolver:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await validated_stream_request(client, f"http://pinned.test:{self.port}/media",
                                                          client_address="8.8.8.8")
                try:
                    self.assertEqual(await response.aread(), b"synthetic media")
                finally:
                    await response.aclose()
            hostname_lookups = [call for call in resolver.call_args_list if call.args[0] == "pinned.test"]
        self.assertEqual(len(hostname_lookups), 2)
        self.assertEqual(self.requests[-1][1]["host"], f"pinned.test:{self.port}")

    async def test_mixed_public_private_dns_answer_is_rejected(self) -> None:
        records = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, self.port))
                   for ip in ("93.184.216.34", "169.254.169.254")]
        with patch.object(socket, "getaddrinfo", return_value=records):
            async with httpx.AsyncClient(trust_env=False) as client:
                with self.assertRaises(UnsafeIngestionSource):
                    await validated_stream_request(client, "http://mixed.test/media", client_address="8.8.8.8")
        self.assertEqual(self.requests, [])

    async def test_redirect_to_metadata_network_is_rejected_before_dial(self) -> None:
        settings.INGEST_TRUSTED_HOSTS = ["origin.test"]
        self.routes["/redirect"] = (302, {"Location": "http://metadata.test/latest"}, b"")
        with self.dns({"origin.test": "127.0.0.1", "metadata.test": "169.254.169.254"}):
            async with httpx.AsyncClient(trust_env=False) as client:
                with self.assertRaises(UnsafeIngestionSource):
                    await validated_stream_request(client, f"http://origin.test:{self.port}/redirect",
                                                   client_address="8.8.8.8")
        self.assertEqual([path for path, _ in self.requests], ["/redirect"])

    async def test_malformed_redirect_destination_is_rejected(self) -> None:
        for target in ("http://[", "http://foreign.test:invalid/media"):
            with self.subTest(destination=target):
                self.routes["/redirect"] = (302, {"Location": target}, b"")
                async with httpx.AsyncClient(trust_env=False) as client:
                    # HTTPX may reject an invalid Location while constructing
                    # next_request, even with automatic following disabled.
                    with self.assertRaises((UnsafeIngestionSource, httpx.RemoteProtocolError)):
                        await validated_stream_request(client, f"http://127.0.0.1:{self.port}/redirect",
                                                       client_address="127.0.0.1")

    async def test_http_playback_redirect_and_later_ranges_cannot_reintroduce_credentials(self) -> None:
        settings.INGEST_TRUSTED_HOSTS = ["a.unit.test", "b.unit.test"]
        self.routes["/redirect"] = (302, {"Location": f"http://b.unit.test:{self.port}/media"}, b"")
        self.routes["/media"] = (200, {"Content-Type": "video/mp4"}, b"0123456789")
        reader = HttpPlaybackSource(f"http://a.unit.test:{self.port}/redirect",
                                    headers={"Authorization": "Bearer synthetic", "Cookie": "synthetic=cookie"},
                                    client_address="8.8.8.8")
        with self.dns({"a.unit.test": "127.0.0.1", "b.unit.test": "127.0.0.1"}):
            self.assertEqual((await reader.stat()).size, 10)
            self.assertEqual(b"".join([chunk async for chunk in reader.open_range(3, 4)]), b"3456")
            self.assertEqual(b"".join([chunk async for chunk in reader.open_range(0, 2)]), b"01")
        requests = [headers for path, headers in self.requests if path == "/media"]
        self.assertGreaterEqual(len(requests), 3)
        for headers in requests:
            self.assertNotIn("authorization", headers)
            self.assertNotIn("cookie", headers)
        self.assertNotIn("Range", reader.headers)

    async def test_redirect_strips_credentials_and_cross_domain_cookie_jar(self) -> None:
        settings.INGEST_TRUSTED_HOSTS = ["a.unit.test", "b.unit.test"]
        self.routes["/redirect"] = (302, {"Location": f"http://b.unit.test:{self.port}/media",
                                                   "Set-Cookie": "session=synthetic; Domain=unit.test; Path=/"}, b"")
        self.routes["/media"] = (200, {}, b"media")
        selected = {"Authorization": "Bearer synthetic", "Cookie": "synthetic=cookie",
                    "Origin": "https://synthetic.test", "Referer": "https://synthetic.test/page",
                    "User-Agent": "SyntheticMediaSender"}
        with self.dns({"a.unit.test": "127.0.0.1", "b.unit.test": "127.0.0.1"}):
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await validated_stream_request(client, f"http://a.unit.test:{self.port}/redirect",
                                                          headers=selected, client_address="8.8.8.8")
                await response.aread()
                await response.aclose()
        redirected = self.requests[-1][1]
        for name in ("authorization", "cookie", "origin", "referer"):
            self.assertNotIn(name, redirected)
        self.assertEqual(redirected["user-agent"], "SyntheticMediaSender")

    async def test_hls_rewrites_nested_playlist_key_init_and_segments(self) -> None:
        settings.INGEST_TRUSTED_HOSTS = ["origin.test", "keys.test"]
        self.routes["/master"] = (200, {"Content-Type": "application/vnd.apple.mpegurl"}, b"#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI=\"child.m3u8\"\n#EXT-X-STREAM-INF:BANDWIDTH=5000\nchild.m3u8\n")
        child = f"#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"http://keys.test:{self.port}/key\"\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n".encode()
        self.routes["/child.m3u8"] = (200, {}, child)
        self.routes["/key"] = (200, {}, b"0123456789abcdef")
        self.routes["/init.mp4"] = (200, {}, b"synthetic init")
        self.routes["/segment.ts"] = (200, {}, b"synthetic segment")
        with self.dns({"origin.test": "127.0.0.1", "keys.test": "127.0.0.1"}):
            async with controlled_media_input(f"http://origin.test:{self.port}/master",
                                               headers={"Authorization": "Bearer synthetic"},
                                               client_address="8.8.8.8") as local:
                self.assertTrue(local.endswith(".m3u8"))
                async with httpx.AsyncClient(trust_env=False) as consumer:
                    master = (await consumer.get(local)).text
                    self.assertNotIn("origin.test", master)
                    nested = next(line for line in master.splitlines() if line.startswith("http"))
                    playlist = (await consumer.get(nested)).text
                    self.assertNotIn("keys.test", playlist)
                    targets = re.findall(r'URI="([^"]+)"', playlist)
                    targets.append(next(line for line in playlist.splitlines() if line.startswith("http")))
                    for target in targets:
                        self.assertEqual((await consumer.get(target)).status_code, 200)
        key_request = next(headers for path, headers in self.requests if path == "/key")
        self.assertNotIn("authorization", key_request)
        segment_request = next(headers for path, headers in self.requests if path == "/segment.ts")
        self.assertEqual(segment_request["authorization"], "Bearer synthetic")

    async def test_nested_hls_private_destination_does_not_inherit_relay_loopback_trust(self) -> None:
        settings.INGEST_TRUSTED_HOSTS = ["origin.test"]
        self.routes["/master.m3u8"] = (200, {}, f"#EXTM3U\n#EXTINF:1,\nhttp://blocked.test:{self.port}/private\n#EXT-X-ENDLIST\n".encode())
        self.routes["/private"] = (200, {}, b"private")
        with self.dns({"origin.test": "127.0.0.1", "blocked.test": "127.0.0.1"}):
            with self.assertRaises(UnsafeIngestionSource):
                async with controlled_media_input(f"http://origin.test:{self.port}/master.m3u8", client_address="8.8.8.8") as local:
                    async with httpx.AsyncClient(trust_env=False) as consumer:
                        playlist = (await consumer.get(local)).text
                        segment = next(line for line in playlist.splitlines() if line.startswith("http"))
                        self.assertEqual((await consumer.get(segment)).status_code, 502)
        self.assertFalse(any(path == "/private" for path, _ in self.requests))

    async def test_non_http_key_and_segment_uri_fail_closed(self) -> None:
        for uri in ("file:///etc/passwd", "ftp://foreign.test/file", "crypto:http://foreign.test/file"):
            with self.subTest(uri=uri):
                self.routes["/playlist"] = (200, {}, f"#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"{uri}\"\n#EXTINF:1,\nsegment.ts\n".encode())
                with self.assertRaises(UnsafeIngestionSource):
                    async with controlled_media_input(f"http://127.0.0.1:{self.port}/playlist", client_address="127.0.0.1") as local:
                        async with httpx.AsyncClient(trust_env=False) as consumer:
                            self.assertEqual((await consumer.get(local)).status_code, 502)

    async def test_disguised_hls_and_direct_ranges_keep_separate_input_options(self) -> None:
        self.routes["/disguised.mp4"] = (200, {}, b"#EXTM3U\n#EXT-X-ENDLIST\n")
        self.routes["/video.mp4"] = (200, {"Content-Type": "video/mp4"}, b"0123456789")
        async with controlled_media_input(f"http://127.0.0.1:{self.port}/disguised.mp4", client_address="127.0.0.1") as local:
            self.assertIn("-allowed_extensions", ffmpeg_network_input_options(local))
        async with controlled_media_input(f"http://127.0.0.1:{self.port}/video.mp4", client_address="127.0.0.1") as local:
            self.assertNotIn("-allowed_extensions", ffmpeg_network_input_options(local))
            async with httpx.AsyncClient(trust_env=False) as consumer:
                response = await consumer.get(local, headers={"Range": "bytes=3-6"})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, b"3456")
                self.assertEqual(response.headers["content-range"], "bytes 3-6/10")
            self.assertTrue(is_controlled_input(local))
        self.assertFalse(is_controlled_input(local))
        with self.assertRaises(UnsafeIngestionSource):
            ffmpeg_network_input_options(local)
        with self.assertRaises(UnsafeIngestionSource):
            ffmpeg_network_input_options("https://foreign.test/video.mp4")

    async def test_manifest_limits_and_unsupported_external_manifest_fail_closed(self) -> None:
        self.routes["/large"] = (200, {}, b"#EXTM3U\n" + b"# padded\n" * 20)
        with patch("services.controlled_source.MANIFEST_LIMIT_BYTES", 64):
            with self.assertRaises(UnsafeIngestionSource):
                async with controlled_media_input(f"http://127.0.0.1:{self.port}/large", client_address="127.0.0.1") as local:
                    async with httpx.AsyncClient(trust_env=False) as consumer:
                        self.assertEqual((await consumer.get(local)).status_code, 502)
        self.routes["/dash"] = (200, {"Content-Type": "application/dash+xml"}, b'<MPD><BaseURL>http://foreign.test/</BaseURL></MPD>')
        with self.assertRaises(UnsafeIngestionSource):
            async with controlled_media_input(f"http://127.0.0.1:{self.port}/dash", client_address="127.0.0.1") as local:
                async with httpx.AsyncClient(trust_env=False) as consumer:
                    self.assertEqual((await consumer.get(local)).status_code, 502)

    async def test_declared_byte_limit_and_compression_bomb_are_rejected(self) -> None:
        self.routes["/large.bin"] = (200, {}, b"x" * 128)
        with patch("services.controlled_source.SOURCE_LIMIT_BYTES", 64):
            with self.assertRaises(UnsafeIngestionSource):
                async with controlled_media_input(f"http://127.0.0.1:{self.port}/large.bin", client_address="127.0.0.1") as local:
                    async with httpx.AsyncClient(trust_env=False) as consumer:
                        self.assertEqual((await consumer.get(local)).status_code, 502)
        self.routes["/encoded"] = (200, {"Content-Encoding": "gzip"}, b"synthetic encoding bomb")
        with self.assertRaises(UnsafeIngestionSource):
            async with controlled_media_input(f"http://127.0.0.1:{self.port}/encoded", client_address="127.0.0.1"):
                self.fail("Encoded source reached decoder")

    async def test_http_failures_keep_permanent_and_retryable_classifications(self) -> None:
        for status, code, retryable in ((401, "SOURCE_UNAUTHORIZED", False), (403, "SOURCE_FORBIDDEN", False),
                                       (404, "SOURCE_NOT_FOUND", False), (429, "SOURCE_RATE_LIMITED", True),
                                       (503, "SOURCE_UNAVAILABLE", True)):
            with self.subTest(status=status):
                self.routes["/error"] = (status, {}, b"")
                success, failure = await download_and_merge(
                    "synthetic-error", f"http://127.0.0.1:{self.port}/error", None, {},
                    str(self.root / "unused.mp4"), 2, client_address="127.0.0.1", preview_enabled=False,
                )
                self.assertFalse(success)
                self.assertEqual(failure.code, code)
                self.assertEqual(failure.retryable, retryable)

    async def test_original_client_policy_is_required_for_loopback_source(self) -> None:
        with self.assertRaises(UnsafeIngestionSource):
            async with controlled_media_input(f"http://127.0.0.1:{self.port}/video.mp4", client_address="8.8.8.8"):
                self.fail("Private source was admitted")

    async def test_malformed_source_urls_are_policy_errors(self) -> None:
        for url in ("http://[", "http://127.0.0.1:0/media", "http://host\x00.test/media", "http://host\n.test/media", "file:///etc/passwd"):
            with self.subTest(url=repr(url)):
                with self.assertRaises(UnsafeIngestionSource):
                    await validate_url(url, client_address="127.0.0.1")

    async def test_disguised_local_audio_and_video_manifests_cannot_fetch_remote_resources(self) -> None:
        self.routes["/secret-segment.ts"] = (200, {}, b"synthetic secret segment")
        manifest = self.root / "audio.m4a"
        manifest.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:{self.port}/secret-segment.ts\n#EXT-X-ENDLIST\n")
        self.assertEqual(await probe_completed_media(str(manifest)), {})
        self.assertEqual(await asyncio.to_thread(_probe_local_audio_timing, str(manifest)), {})
        self.assertEqual(await extract_audio_and_strip_video(str(manifest)), [])
        result = await probe_media_stream(str(manifest))
        self.assertFalse(result["has_audio"])
        self.assertIsNotNone(result.get("failure"))
        # A conventional extension forces HLS recognition, so this also verifies
        # the format/protocol boundary independently of extension-based refusal.
        conventional = manifest.with_suffix(".m3u8")
        conventional.write_bytes(manifest.read_bytes())
        self.assertEqual(await probe_completed_media(str(conventional)), {})
        self.assertEqual(await extract_audio_and_strip_video(str(conventional)), [])
        self.assertEqual(self.requests, [])

    async def test_relay_scope_releases_work_and_capability_on_cancellation(self) -> None:
        self.routes["/media"] = (200, {}, b"media")
        entered = asyncio.Event()
        local_urls = []
        async def consume():
            async with controlled_media_input(f"http://127.0.0.1:{self.port}/media", client_address="127.0.0.1") as local:
                local_urls.append(local)
                entered.set()
                await asyncio.Event().wait()
        baseline = len(get_runtime_coordinator().active_work())
        closing, release = asyncio.Event(), asyncio.Event()
        real_close = SourceRelay.close
        async def delayed_close(relay):
            closing.set()
            await release.wait()
            await real_close(relay)
        with patch.object(SourceRelay, "close", delayed_close):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), timeout=5)
            self.assertEqual(len(get_runtime_coordinator().active_work()), baseline + 1)
            task.cancel()
            await asyncio.wait_for(closing.wait(), timeout=5)
            task.cancel()
            self.assertEqual(len(get_runtime_coordinator().active_work()), baseline + 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(get_runtime_coordinator().active_work()), baseline)
        self.assertFalse(is_controlled_input(local_urls[0]))

    async def test_https_numeric_dial_preserves_sni_and_certificate_hostname(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tls.test")])
        now = datetime.datetime.now(datetime.UTC)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - datetime.timedelta(minutes=1))
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName("tls.test")]), critical=False)
                       .sign(key, hashes.SHA256()))
        certificate_path, key_path = self.root / "certificate.pem", self.root / "key.pem"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        observed_sni = []
        server_context.set_servername_callback(lambda sock, server_name, context: observed_sni.append(server_name))
        server = await self.origin(server_context)
        port = server.sockets[0].getsockname()[1]
        self.routes["/tls"] = (200, {}, b"TLS media")
        client_context = ssl.create_default_context(cafile=certificate_path)
        async def numeric(host, port):
            return ["127.0.0.1"]
        async with httpx.AsyncClient(transport=PinnedAsyncTransport(numeric, ssl_context=client_context), trust_env=False) as client:
            self.assertEqual((await client.get(f"https://tls.test:{port}/tls")).content, b"TLS media")
            with self.assertRaises(httpx.TransportError):
                await client.get(f"https://wrong.test:{port}/tls")
        self.assertEqual(observed_sni, ["tls.test", "wrong.test"])
        self.assertEqual(next(headers for path, headers in self.requests if path == "/tls")["host"], f"tls.test:{port}")

    async def test_real_ffmpeg_consumes_direct_video_and_encrypted_hls_through_relay(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.fail("FFmpeg is required for the approved source-boundary verification")
        source = self.root / "source.mp4"
        fixture = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=5", "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-g", "5", "-c:a", "aac", "-movflags", "+faststart", str(source),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await supervised_output(fixture, timeout=15)
        self.assertEqual(fixture.returncode, 0, stderr.decode(errors="replace"))
        encryption_key = self.root / "key"
        encryption_key.write_bytes(b"0123456789abcdef")
        key_info = self.root / "key-info"
        key_info.write_text(f"key\n{encryption_key}\n")
        fixture = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-c", "copy",
            "-hls_time", "1", "-hls_key_info_file", str(key_info), str(self.root / "playlist.m3u8"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await supervised_output(fixture, timeout=15)
        self.assertEqual(fixture.returncode, 0, stderr.decode(errors="replace"))
        for path in self.root.iterdir():
            if path.suffix in {".mp4", ".m3u8", ".ts"} or path.name == "key":
                self.routes["/" + path.name] = (200, {}, path.read_bytes())
        for name in ("source.mp4", "playlist.m3u8"):
            with self.subTest(source=name):
                output = self.root / (name + ".result.mp4")
                success, failure = await asyncio.wait_for(download_and_merge(
                    "controlled-" + name, f"http://127.0.0.1:{self.port}/{name}", None,
                    {"User-Agent": "SyntheticMediaSender"}, str(output), 2,
                    client_address="127.0.0.1", preview_enabled=False,
                ), timeout=20)
                self.assertTrue(success, failure)
                self.assertGreater(output.stat().st_size, 0)
        self.assertTrue(any(path == "/key" for path, _ in self.requests))
        self.assertTrue(all(headers.get("user-agent") == "SyntheticMediaSender" for _, headers in self.requests))


if __name__ == "__main__":
    unittest.main()
