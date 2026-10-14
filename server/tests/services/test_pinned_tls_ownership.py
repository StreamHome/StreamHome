"""Native pinned TLS ownership, using only disposable ephemeral origins.

Run through run_isolated.py. A stalled origin records the actual ClientHello and
EOF; closing a client/pool alone cannot satisfy native stream closure assertions.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpcore
import httpx

from config import settings
from lifecycle.coordination import get_runtime_coordinator
from lifecycle.source_network import PinnedAsyncTransport, PinnedNetworkBackend
from services import controlled_source, ingestion_security


class PinnedTLSOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hello, self.eof = asyncio.Event(), asyncio.Event()
        self.closing, self.release_close = asyncio.Event(), asyncio.Event()
        self.handlers: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.native_streams: list[httpcore.AsyncNetworkStream] = []
        self.handshake_tasks: list[asyncio.Task] = []
        self.clients: list[httpx.AsyncClient] = []
        self.relays: list[controlled_source.SourceRelay] = []
        self.baseline_work = len(get_runtime_coordinator().active_work())
        self.baseline_inputs = set(controlled_source._ACTIVE_INPUTS)
        self.trusted = patch.object(settings, "INGEST_TRUSTED_HOSTS", [])
        self.trusted.start()
        self.server = await asyncio.start_server(self.stall, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.release_close.set()
        # Dispose handler transports before waiting for Server.wait_closed: an
        # unclosed witness connection must fail assertions, never hang teardown.
        self.server.close()
        for writer in tuple(self.writers):
            writer.transport.abort()
        for task in tuple(self.handlers):
            task.cancel()
        await asyncio.gather(*tuple(self.handlers), return_exceptions=True)
        await self.server.wait_closed()
        self.trusted.stop()

    async def stall(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        try:
            header = await reader.readexactly(5)
            # TLS handshake record followed by a ClientHello, not an HTTP byte.
            self.assertEqual(header[0], 22)
            hello = await reader.readexactly(int.from_bytes(header[3:5], "big"))
            self.assertEqual(hello[0], 1)
            self.hello.set()
            await reader.read()
            self.eof.set()
        except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.handlers.discard(task)
            self.writers.discard(writer)

    @contextmanager
    def capture(self, *, delayed_close: bool = False):
        real_connect = httpcore.AnyIOBackend.connect_tcp
        real_client = ingestion_security.pinned_source_client
        real_start = controlled_source.SourceRelay.start

        async def connect(backend, *args, **kwargs):
            stream = await real_connect(backend, *args, **kwargs)
            self.native_streams.append(stream)
            self.handshake_tasks.append(asyncio.current_task())
            if delayed_close:
                real_close = stream.aclose

                async def close():
                    self.closing.set()
                    await self.release_close.wait()
                    await real_close()

                stream.aclose = close
            return stream

        def client(*args, **kwargs):
            owner = real_client(*args, **kwargs)
            self.clients.append(owner)
            return owner

        async def start(relay):
            await real_start(relay)
            self.relays.append(relay)

        with patch.object(httpcore.AnyIOBackend, "connect_tcp", connect), \
                patch.object(ingestion_security, "pinned_source_client", client), \
                patch.object(controlled_source.SourceRelay, "start", start):
            yield

    async def prepare(self) -> None:
        async with controlled_source.controlled_media_input(
            f"https://127.0.0.1:{self.port}/synthetic.mp4", client_address="127.0.0.1"
        ):
            self.fail("Unfinished TLS yielded a native consumer input")

    def assert_closed(self) -> None:
        self.assertEqual(len(get_runtime_coordinator().active_work()), self.baseline_work)
        self.assertEqual(controlled_source._ACTIVE_INPUTS, self.baseline_inputs)
        self.assertTrue(self.clients)
        self.assertEqual(len(self.native_streams), 1)
        self.assertEqual(self.native_streams[0].get_extra_info("socket").fileno(), -1)
        for client in self.clients:
            self.assertTrue(client.is_closed)
            self.assertEqual(client._transport.pool.connections, [])
        for relay in self.relays:
            self.assertTrue(relay.closed)
            self.assertFalse(relay.server.is_serving())
            self.assertEqual(relay.tasks, set())
            self.assertEqual(relay.resources, {})
            self.assertEqual(relay.paths, {})

    async def preparation_case(self, *, caller_cancel: bool, delayed_close: bool) -> None:
        started = asyncio.get_running_loop().time()
        with self.capture(delayed_close=delayed_close), \
                patch.object(controlled_source, "SOURCE_TIME_LIMIT_SECONDS", 5 if caller_cancel else 0.08):
            pending = asyncio.create_task(self.prepare())
            try:
                await asyncio.wait_for(self.hello.wait(), 1)
                if caller_cancel:
                    pending.cancel()
                if delayed_close:
                    await asyncio.wait_for(self.closing.wait(), 1)
                    # Cancel the handshake owner as well as (for caller-cancel
                    # cases) the relay caller while actual native close waits.
                    for _ in range(3):
                        self.handshake_tasks[0].cancel()
                        if caller_cancel:
                            pending.cancel()
                        await asyncio.sleep(0.01)
                    self.assertFalse(pending.done(), "Caller escaped unsettled native close")
                    self.assertFalse(self.eof.is_set())
                    self.assertGreaterEqual(self.native_streams[0].get_extra_info("socket").fileno(), 0)
                    self.assertEqual(len(get_runtime_coordinator().active_work()), self.baseline_work + 1)
                    self.assertFalse(self.clients[-1].is_closed)
                    self.release_close.set()
                done, _ = await asyncio.wait({pending}, timeout=1)
                self.assertTrue(done, "TLS ownership did not settle within test supervision")
                if caller_cancel:
                    self.assertTrue(pending.cancelled())
                else:
                    self.assertIsInstance(pending.exception(), TimeoutError)
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.wait_for(self.eof.wait(), 1)
                self.assert_closed()
                print("PINNED_TLS_OBSERVATION=" + json.dumps({
                    "case": "caller-cancel" if caller_cancel else "preparation-timeout",
                    "delayed_native_close": delayed_close, "client_hello": True,
                    "origin_eof": True, "native_socket_closed": True,
                    "result": "CancelledError" if caller_cancel else "TimeoutError",
                    "work_delta": 0, "clients_closed": True, "pools_empty": True,
                    "elapsed_seconds": elapsed,
                }, sort_keys=True))
            finally:
                self.release_close.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_preparation_timeout_closes_actual_stalled_tls_tcp(self) -> None:
        await self.preparation_case(caller_cancel=False, delayed_close=False)

    async def test_caller_cancellation_closes_actual_stalled_tls_tcp(self) -> None:
        await self.preparation_case(caller_cancel=True, delayed_close=False)

    async def test_timeout_retains_lease_during_repeatedly_cancelled_native_close(self) -> None:
        await self.preparation_case(caller_cancel=False, delayed_close=True)

    async def test_caller_retains_lease_during_repeatedly_cancelled_native_close(self) -> None:
        await self.preparation_case(caller_cancel=True, delayed_close=True)

    async def test_native_tls_connect_timeout_keeps_httpx_timeout_outcome(self) -> None:
        async def numeric(host, port):
            return ["127.0.0.1"]

        client = httpx.AsyncClient(transport=PinnedAsyncTransport(numeric), trust_env=False,
                                   timeout=httpx.Timeout(1, connect=0.08))
        self.clients.append(client)
        with self.capture():
            try:
                with self.assertRaises(httpx.TimeoutException):
                    await client.get(f"https://synthetic.test:{self.port}/media")
            finally:
                await client.aclose()
        self.assertTrue(self.hello.is_set())
        await asyncio.wait_for(self.eof.wait(), 1)
        self.assert_closed()

    async def test_pinned_stream_exposes_actual_connection_info(self) -> None:
        async def echo(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            self.writers.add(writer)
            try:
                writer.write(await reader.readexactly(9))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                self.handlers.discard(task)
                self.writers.discard(writer)

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def numeric(host, port):
            return ["127.0.0.1"]

        stream = await PinnedNetworkBackend(numeric).connect_tcp("synthetic.test", port, timeout=1)
        try:
            self.assertEqual(stream.get_extra_info("server_addr"), ("127.0.0.1", port))
            self.assertEqual(stream.get_extra_info("client_addr")[0], "127.0.0.1")
            self.assertIsNone(stream.get_extra_info("ssl_object"))
            self.assertIsNone(stream.get_extra_info("unavailable-info"))
            self.assertGreaterEqual(stream.get_extra_info("socket").fileno(), 0)
            self.assertFalse(stream.get_extra_info("is_readable"))
            await stream.write(b"synthetic", timeout=1)
            self.assertEqual(await stream.read(9, timeout=1), b"synthetic")
        finally:
            await stream.aclose()
            server.close()
            await server.wait_closed()
        self.assertEqual(stream.get_extra_info("socket").fileno(), -1)


if __name__ == "__main__":
    unittest.main()
