"""Native local-origin preparation deadlines and settled source ownership.

Run only through run_isolated.py. Every socket, header and source is synthetic;
the origins bind ephemeral loopback ports and never contact external targets.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from config import settings
from lifecycle.coordination import get_runtime_coordinator
from services import controlled_source, ingestion_security
from services.controlled_source import SourceRelay, controlled_media_input, is_controlled_input
from services.ffmpeg_input import ffmpeg_network_input_options


class SourcePreparationDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.trusted = patch.object(settings, "INGEST_TRUSTED_HOSTS", [])
        self.trusted.start()
        self.handlers: set[asyncio.Task] = set()
        self.requests: list[str] = []
        self.disconnected: dict[str, asyncio.Event] = {}
        self.relays: list[SourceRelay] = []
        self.responses: list[httpx.Response] = []
        self.clients: list[httpx.AsyncClient] = []
        self.baseline_work = len(get_runtime_coordinator().active_work())
        self.baseline_inputs = set(controlled_source._ACTIVE_INPUTS)
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.handlers):
            task.cancel()
        await asyncio.gather(*tuple(self.handlers), return_exceptions=True)
        self.trusted.stop()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.handlers.add(task)
        sender = None
        path = ""
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            path = raw.split(b"\r\n", 1)[0].decode().split(" ")[1]
            self.requests.append(path)
            disconnected = self.disconnected.setdefault(path, asyncio.Event())
            if path == "/no-headers":
                await reader.read()
            elif path == "/partial-headers":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: ")
                await writer.drain()
                sender = asyncio.create_task(self.drip(writer, chunked=False))
                await reader.read()
            elif path in {"/slow-prefix", "/full-prefix"}:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                             b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
                if path == "/full-prefix":
                    writer.write(b"10000\r\n" + b"x" * (64 * 1024) + b"\r\n")
                await writer.drain()
                sender = asyncio.create_task(self.drip(writer, chunked=True))
                await reader.read()
            else:
                routes = {
                    "/direct.mp4": (200, "video/mp4", b"short direct source", ""),
                    "/empty.mp4": (200, "video/mp4", b"", ""),
                    "/disguised.mp4": (200, "application/octet-stream", b"#EXTM3U\n#EXT-X-ENDLIST\n", ""),
                    "/not-found": (404, "application/octet-stream", b"", ""),
                    "/redirect": (302, "application/octet-stream", b"", "/disguised.mp4"),
                    "/slow-redirect": (302, "application/octet-stream", b"", "/slow-prefix"),
                }
                status, content_type, body, location = routes[path]
                fields = [f"HTTP/1.1 {status} Synthetic", f"Content-Type: {content_type}",
                          f"Content-Length: {len(body)}", "Connection: close"]
                if location:
                    fields.append("Location: " + location)
                writer.write(("\r\n".join(fields) + "\r\n\r\n").encode() + body)
                await writer.drain()
            disconnected.set()
        except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.handlers.discard(task)

    @staticmethod
    async def drip(writer: asyncio.StreamWriter, *, chunked: bool) -> None:
        # Each read arrives inside HTTPX's idle timeout; only the total policy
        # can stop the 64 KiB prefix accumulator or partial response headers.
        while True:
            writer.write(b"1\r\nx\r\n" if chunked else b"x")
            await writer.drain()
            await asyncio.sleep(0.01)

    @contextmanager
    def capture(self, *, close_response=None, close_client=None):
        real_start = SourceRelay.start
        real_request = controlled_source.validated_stream_request
        real_client = ingestion_security.pinned_source_client

        async def start(relay):
            await real_start(relay)
            self.relays.append(relay)

        async def request(*args, **kwargs):
            response = await real_request(*args, **kwargs)
            self.responses.append(response)
            if close_response is not None:
                original = response.aclose

                async def close():
                    await close_response()
                    await original()

                response.aclose = close
            return response

        def client(*args, **kwargs):
            owner = real_client(*args, **kwargs)
            self.clients.append(owner)
            if close_client is not None:
                original = owner.aclose

                async def close():
                    await close_client()
                    await original()

                owner.aclose = close
            return owner

        with patch.object(SourceRelay, "start", start), \
                patch.object(controlled_source, "validated_stream_request", request), \
                patch.object(ingestion_security, "pinned_source_client", client):
            yield

    async def prepare(self, path: str) -> None:
        async with controlled_media_input(f"http://127.0.0.1:{self.port}{path}",
                                          client_address="127.0.0.1"):
            self.fail("Unfinished preparatory response yielded a consumer input")

    def assert_closed(self) -> None:
        self.assertEqual(len(get_runtime_coordinator().active_work()), self.baseline_work)
        self.assertEqual(controlled_source._ACTIVE_INPUTS, self.baseline_inputs)
        self.assertTrue(self.relays)
        self.assertTrue(self.clients)
        for relay in self.relays:
            self.assertTrue(relay.closed)
            self.assertFalse(relay.server.is_serving())
            self.assertTrue(relay.client.is_closed)
            self.assertEqual(relay.tasks, set())
            self.assertEqual(relay.resources, {})
            self.assertEqual(relay.paths, {})
        for response in self.responses:
            self.assertTrue(response.is_closed)
        for client in self.clients:
            self.assertTrue(client.is_closed)
            self.assertEqual(client._transport.pool.connections, [])

    async def timeout_case(self, path: str, *, response_expected: bool) -> None:
        with self.capture(), patch.object(controlled_source, "SOURCE_TIME_LIMIT_SECONDS", 0.05):
            started = asyncio.get_running_loop().time()
            task = asyncio.create_task(self.prepare(path))
            # The observer's deadline is supervision for a broken test, not the
            # source policy. Its expiry cannot satisfy the following assertions.
            done, _ = await asyncio.wait({task}, timeout=2)
            elapsed = asyncio.get_running_loop().time() - started
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.fail("Preparation exceeded both the source policy and test supervision")
            self.assertIsInstance(task.exception(), TimeoutError)
            self.assertLess(elapsed, 0.22, "The original 0.22-second active-prefix witness must end")
        self.assertEqual(bool(self.responses), response_expected)
        self.assert_closed()
        final_path = "/slow-prefix" if path == "/slow-redirect" else path
        await asyncio.wait_for(self.disconnected[final_path].wait(), timeout=1)
        print("SOURCE_PREPARATION_OBSERVATION=" + json.dumps({
            "case": path, "configured_seconds": 0.05, "completion_seconds": elapsed,
            "result": "TimeoutError", "yielded_native_input": False,
            "requests": self.requests, "upstream_eof": True,
            "responses_closed": all(item.is_closed for item in self.responses),
            "pinned_clients_closed": all(item.is_closed for item in self.clients),
            "work_delta_after_cleanup": len(get_runtime_coordinator().active_work()) - self.baseline_work,
        }, sort_keys=True))

    async def test_initial_chunked_prefix_obeys_total_deadline(self) -> None:
        await self.timeout_case("/slow-prefix", response_expected=True)

    async def test_initial_request_opening_obeys_total_deadline(self) -> None:
        await self.timeout_case("/no-headers", response_expected=False)

    async def test_partial_response_headers_obey_total_deadline(self) -> None:
        await self.timeout_case("/partial-headers", response_expected=False)

    async def test_redirect_and_prefix_share_preparation_deadline(self) -> None:
        await self.timeout_case("/slow-redirect", response_expected=True)
        self.assertEqual(self.requests, ["/slow-redirect", "/slow-prefix"])

    async def cancelled_cleanup_case(self, *, initial_cancel: bool, opening: bool = False) -> None:
        closing, release = asyncio.Event(), asyncio.Event()

        async def gated_close():
            closing.set()
            await release.wait()

        path = "/no-headers" if opening else "/slow-prefix"
        capture = {"close_client": gated_close} if opening else {"close_response": gated_close}
        with self.capture(**capture), patch.object(controlled_source, "SOURCE_TIME_LIMIT_SECONDS",
                                                   5 if initial_cancel else 0.05):
            task = asyncio.create_task(self.prepare(path))
            try:
                if initial_cancel:
                    for _ in range(200):
                        if self.responses:
                            break
                        await asyncio.sleep(0.005)
                    self.assertTrue(self.responses, "Actual streamed response must open before cancellation")
                    task.cancel()
                await asyncio.wait_for(closing.wait(), timeout=2)
                task.cancel()
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done(), "Caller returned before owned HTTP closure settled")
                self.assertEqual(len(get_runtime_coordinator().active_work()), self.baseline_work + 1)
                self.assertEqual(controlled_source._ACTIVE_INPUTS, self.baseline_inputs)
                self.assertTrue(self.clients)
                self.assertFalse(self.clients[0].is_closed)
                # HTTPcore can close the socket immediately when a read is
                # cancelled, before the remaining response/client cleanup.
                # Admission must still cover that actual pending cleanup.
                if not opening:
                    self.assertFalse(self.responses[0].is_closed)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assert_closed()
        await asyncio.wait_for(self.disconnected[path].wait(), timeout=1)
        print("SOURCE_PREPARATION_OBSERVATION=" + json.dumps({
            "case": "repeated-cancel-" + ("request-opening" if opening else
                                           "caller" if initial_cancel else "timeout"),
            "lease_delta_during_pending_close": 1, "caller_completed_during_pending_close": False,
            "result": "CancelledError", "upstream_eof_after_cleanup": True,
            "responses_closed": all(item.is_closed for item in self.responses),
            "pinned_clients_closed": all(item.is_closed for item in self.clients),
            "work_delta_after_cleanup": len(get_runtime_coordinator().active_work()) - self.baseline_work,
        }, sort_keys=True))

    async def test_timeout_cleanup_retains_lease_through_repeated_cancel(self) -> None:
        await self.cancelled_cleanup_case(initial_cancel=False)

    async def test_caller_cancel_cleanup_retains_lease_through_repeated_cancel(self) -> None:
        await self.cancelled_cleanup_case(initial_cancel=True)

    async def test_request_owner_cleanup_retains_lease_through_repeated_cancel(self) -> None:
        await self.cancelled_cleanup_case(initial_cancel=False, opening=True)

    async def test_disguised_hls_direct_early_eof_and_empty_sources_remain_supported(self) -> None:
        with self.capture(), patch.object(controlled_source, "SOURCE_TIME_LIMIT_SECONDS", 1):
            for path, expected, hls in (
                ("/direct.mp4", b"short direct source", False),
                ("/empty.mp4", b"", False),
                ("/disguised.mp4", b"#EXTM3U\n#EXT-X-ENDLIST\n", True),
                ("/redirect", b"#EXTM3U\n#EXT-X-ENDLIST\n", True),
            ):
                with self.subTest(path=path):
                    async with controlled_media_input(f"http://127.0.0.1:{self.port}{path}",
                                                      client_address="127.0.0.1") as local:
                        self.assertTrue(is_controlled_input(local))
                        self.assertEqual("-allowed_extensions" in ffmpeg_network_input_options(local), hls)
                        async with httpx.AsyncClient(trust_env=False) as consumer:
                            response = await consumer.get(local)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.content, expected)
                    self.assertFalse(is_controlled_input(local))
        self.assert_closed()

    async def test_full_bounded_prefix_does_not_wait_for_direct_source_eof(self) -> None:
        with self.capture(), patch.object(controlled_source, "SOURCE_TIME_LIMIT_SECONDS", 1):
            async with controlled_media_input(f"http://127.0.0.1:{self.port}/full-prefix",
                                              client_address="127.0.0.1") as local:
                self.assertTrue(is_controlled_input(local))
                self.assertNotIn("-allowed_extensions", ffmpeg_network_input_options(local))
            self.assertFalse(is_controlled_input(local))
        self.assert_closed()
        await asyncio.wait_for(self.disconnected["/full-prefix"].wait(), timeout=1)

    async def test_http_status_failure_closes_actual_response_without_capability(self) -> None:
        with self.capture(), patch.object(controlled_source, "SOURCE_TIME_LIMIT_SECONDS", 1):
            with self.assertRaises(httpx.HTTPStatusError) as caught:
                await self.prepare("/not-found")
            self.assertEqual(caught.exception.response.status_code, 404)
        self.assertEqual(self.requests, ["/not-found"])
        self.assert_closed()


if __name__ == "__main__":
    unittest.main()
