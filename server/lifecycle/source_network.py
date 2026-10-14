"""Connection-time address pinning for untrusted media HTTP sources.

The HTTP origin remains unchanged. Only the network backend receives a validated
numeric address, so Host, TLS SNI and certificate hostname checks still refer to
the original source host. Environment proxies and Unix sockets are unavailable.
"""

from __future__ import annotations

import ssl
import asyncio
from collections.abc import Awaitable, Callable, AsyncIterator

import httpcore
import httpx

from lifecycle.media_work import settle

AddressResolver = Callable[[str, int], Awaitable[list[str]]]


class _PinnedNetworkStream(httpcore.AsyncNetworkStream):
    """Own TCP until TLS succeeds and httpcore can own the connection."""

    def __init__(self, stream: httpcore.AsyncNetworkStream) -> None:
        self.stream = stream

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self.stream.read(max_bytes, timeout=timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self.stream.write(buffer, timeout=timeout)

    async def aclose(self) -> None:
        await self.stream.aclose()

    async def start_tls(self, ssl_context: ssl.SSLContext,
                        server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        try:
            upgraded = await self.stream.start_tls(ssl_context=ssl_context,
                                                  server_hostname=server_hostname,
                                                  timeout=timeout)
        except BaseException:
            # httpcore has not assigned its HTTP connection yet. Its AnyIO
            # stream closes on Exception, but cancellation can bypass that
            # cleanup. Keep native ownership until actual TCP closure settles,
            # even when the handshake owner receives repeated cancellation.
            await settle(self.stream.aclose())
            raise
        self.stream = upgraded
        return self

    def get_extra_info(self, info: str):
        return self.stream.get_extra_info(info)


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, resolver: AddressResolver) -> None:
        self.resolver = resolver
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Resolve and approve *all* addresses immediately before dialing. Passing
        # the hostname to the socket backend would allow a second DNS lookup.
        try:
            async with asyncio.timeout(min(timeout, 15.0) if timeout is not None else 15.0):
                addresses = await self.resolver(host, port)
                if len(addresses) > 16:
                    raise httpcore.ConnectError("Source host resolved to too many addresses")
                last_error = None
                for address in addresses:
                    try:
                        stream = await self.backend.connect_tcp(
                            address, port, timeout=timeout, local_address=local_address,
                            socket_options=socket_options,
                        )
                        return _PinnedNetworkStream(stream)
                    except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                        last_error = exc
                if last_error is not None:
                    raise last_error
                raise httpcore.ConnectError("Source host did not resolve to an approved address")
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Source connection exceeded its time limit") from exc

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("Unix source sockets are unavailable")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.stream:
                yield chunk
        except httpcore.TimeoutException as exc:
            raise httpx.TimeoutException("Source HTTP response timed out") from exc
        except (httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise httpx.TransportError("Source HTTP response failed") from exc

    async def aclose(self) -> None:
        await self.stream.aclose()


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, resolver: AddressResolver, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or ssl.create_default_context(),
            network_backend=PinnedNetworkBackend(resolver),
            max_connections=8,
            max_keepalive_connections=0,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            response = await self.pool.handle_async_request(core_request)
        except httpcore.TimeoutException as exc:
            raise httpx.TimeoutException("Source HTTP request timed out", request=request) from exc
        except (httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise httpx.TransportError("Source HTTP connection failed", request=request) from exc
        return httpx.Response(response.status, headers=response.headers,
                              stream=_ResponseStream(response.stream), extensions=response.extensions)

    async def aclose(self) -> None:
        await self.pool.aclose()
