"""Job-scoped HTTP inputs whose upstream fetches never belong to FFmpeg.

Every playlist, key, init fragment, segment and direct byte range goes through the
same connection-pinned source policy. Credentials remain in this process. Relay
capabilities are random, expire when the consumer scope exits and are not durable.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

from lifecycle.coordination import EffectClass, runtime_work
from lifecycle.media_work import settle
from services.ingestion_security import (
    UnsafeIngestionSource,
    pinned_source_client,
    source_headers_for_target,
    validate_headers,
    validate_url,
    validated_stream_request,
)

MANIFEST_LIMIT_BYTES = 2 * 1024 * 1024
SOURCE_LIMIT_BYTES = 64 * 1024 * 1024 * 1024
SOURCE_TIME_LIMIT_SECONDS = 24 * 60 * 60
RESOURCE_LIMIT = 100_000
ACTIVE_CONNECTION_LIMIT = 32
_URI_ATTRIBUTE = re.compile(r'(?i)(\bURI\s*=\s*)(?:"([^"\r\n]*)"|([^,\s]+))')
_RANGE = re.compile(r"bytes=(?:\d+-\d*|-\d+)$")
_ACTIVE_INPUTS: set[str] = set()


def is_controlled_input(url: str) -> bool:
    return url in _ACTIVE_INPUTS


@dataclass(frozen=True)
class _Resource:
    url: str
    headers: dict[str, str]
    manifest: bool = False


class SourceRelay:
    def __init__(self, *, client_address: str = "") -> None:
        self.client_address = client_address
        self.capability = secrets.token_hex(32)
        self.base_url = ""
        self.server: asyncio.Server | None = None
        self.resources: dict[str, _Resource] = {}
        self.paths: dict[tuple, str] = {}
        self.tasks: set[asyncio.Task] = set()
        self.client: httpx.AsyncClient | None = None
        self.closed = False
        self.failure: Exception | None = None

    async def start(self) -> None:
        # This client is only a timeout owner. validated_stream_request always
        # creates its own pinned transport, ignoring caller proxies/cookie jars.
        self.client = pinned_source_client(client_address=self.client_address,
                                           timeout=httpx.Timeout(60.0, connect=15.0))
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=16 * 1024)
        port = self.server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}/{self.capability}"

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.closed or len(self.tasks) >= ACTIVE_CONNECTION_LIMIT:
            writer.close()
            return
        task = asyncio.create_task(self._connection(reader, writer))
        self.tasks.add(task)

    def add(self, url: str, headers: dict[str, str], *, manifest: bool = False) -> str:
        # Syntax validation precedes registration; DNS/network policy is checked
        # again when the consumer actually requests this resource.
        try:
            parsed = urlsplit(url)
            port = parsed.port
            hostname = parsed.hostname
        except ValueError as exc:
            raise UnsafeIngestionSource("Playlist resource URL is malformed.") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            raise UnsafeIngestionSource("Playlist resource protocol is unavailable.")
        if port is not None and not 1 <= port <= 65535:
            raise UnsafeIngestionSource("Playlist resource port is unavailable.")
        if parsed.username or parsed.password or "{$" in url:
            raise UnsafeIngestionSource("Playlist resource credentials or variables are unavailable.")
        if len(url) > 4096:
            raise UnsafeIngestionSource("Playlist resource URL is too long.")
        key = (url, tuple(sorted(headers.items())), manifest)
        if key in self.paths:
            return self.base_url + self.paths[key]
        if len(self.resources) >= RESOURCE_LIMIT:
            raise UnsafeIngestionSource("Playlist resource count exceeds the permitted limit.")
        suffix = PurePosixPath(parsed.path).suffix.lower()
        if manifest:
            suffix = ".m3u8"
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".bin"
        path = "/" + secrets.token_hex(16) + suffix
        self.resources[path] = _Resource(url, dict(headers), manifest)
        self.paths[key] = path
        local_url = self.base_url + path
        _ACTIVE_INPUTS.add(local_url)
        return local_url

    async def prepare_input(self, url: str, headers: dict[str, str], *, manifest: bool = False) -> str:
        # Detect disguised playlists before choosing demuxer-specific FFmpeg
        # options. Never buffer a direct video while inspecting its first chunk.
        async def inspect() -> _Resource:
            response = None
            try:
                response = await validated_stream_request(self.client, url, headers=headers,
                                                          client_address=self.client_address)
                if response.status_code not in {200, 206}:
                    raise httpx.HTTPStatusError(f"Upstream media returned HTTP {response.status_code}",
                                               request=response.request, response=response)
                first = await anext(response.aiter_bytes(64 * 1024).__aiter__(), b"")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                detected = content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"} or first.lstrip(b"\xef\xbb\xbf\r\n \t").startswith(b"#EXTM3U")
                return _Resource(response.extensions["streamhome_final_url"],
                                 response.extensions["streamhome_final_headers"], manifest or detected)
            finally:
                if response is not None:
                    _, cancelled = await settle(response.aclose())
                    if cancelled:
                        raise asyncio.CancelledError

        # One total budget includes URL/redirect validation, request headers and
        # the bounded prefix read. Cancel the fetch only once, then retain its
        # work lease while actual response/client closure settles; repeated
        # caller cancellation cannot interrupt the request owner's cleanup.
        preparation = asyncio.create_task(inspect())
        try:
            async with asyncio.timeout(SOURCE_TIME_LIMIT_SECONDS):
                resource = await asyncio.shield(preparation)
        finally:
            if not preparation.done():
                preparation.cancel()
            _, cancelled = await settle(asyncio.gather(preparation, return_exceptions=True))
            if cancelled:
                raise asyncio.CancelledError
        return self.add(resource.url, resource.headers, manifest=resource.manifest)

    def rewrite_manifest(self, data: bytes, final_url: str, headers: dict[str, str]) -> bytes:
        if len(data) > MANIFEST_LIMIT_BYTES:
            raise UnsafeIngestionSource("Playlist exceeds the permitted size.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UnsafeIngestionSource("Playlist encoding is unavailable.") from exc
        if not text.lstrip().startswith("#EXTM3U"):
            raise UnsafeIngestionSource("Source did not provide an HLS playlist.")

        def resource_url(value: str) -> str:
            if not value.strip():
                raise UnsafeIngestionSource("Playlist URI attribute is empty.")
            try:
                target = urljoin(final_url, value.strip())
            except ValueError as exc:
                raise UnsafeIngestionSource("Playlist resource URL is malformed.") from exc
            selected = source_headers_for_target(headers, final_url, target)
            manifest = urlsplit(target).path.lower().endswith((".m3u8", ".m3u"))
            return self.add(target, selected, manifest=manifest)

        output = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                output.append(resource_url(stripped))
            elif stripped.startswith("#"):
                def substitute(match):
                    return match.group(1) + '"' + resource_url(match.group(2) if match.group(2) is not None else match.group(3)) + '"'
                rewritten = _URI_ATTRIBUTE.sub(substitute, line)
                # Empty or unterminated URI attributes cannot be passed on for
                # FFmpeg to reinterpret outside this source policy.
                if re.search(r'(?i)\bURI\s*=', line) and rewritten == line:
                    raise UnsafeIngestionSource("Playlist URI attribute is malformed.")
                output.append(rewritten)
            else:
                output.append(line)
        return ("\n".join(output) + "\n").encode("utf-8")

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        response = None
        headers_sent = False
        try:
            async with asyncio.timeout(SOURCE_TIME_LIMIT_SECONDS):
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
                lines = raw.decode("iso-8859-1").split("\r\n")
                method, target, version = lines[0].split(" ", 2)
                if method not in {"GET", "HEAD"} or version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise UnsafeIngestionSource("Relay request method is unavailable.")
                prefix = "/" + self.capability
                if not target.startswith(prefix + "/"):
                    raise UnsafeIngestionSource("Relay capability is unavailable.")
                resource = self.resources.get(target[len(prefix):])
                if resource is None or self.closed:
                    raise UnsafeIngestionSource("Relay source is unavailable.")
                selected_headers = dict(resource.headers)
                for line in lines[1:]:
                    if line.lower().startswith("range:"):
                        value = line.split(":", 1)[1].strip()
                        if not _RANGE.fullmatch(value):
                            raise UnsafeIngestionSource("Relay byte range is unavailable.")
                        selected_headers = {key: value for key, value in selected_headers.items() if key.lower() != "range"}
                        selected_headers["Range"] = value
                response = await validated_stream_request(self.client, resource.url, headers=selected_headers,
                                                          client_address=self.client_address, method=method)
                if response.status_code not in {200, 206}:
                    raise httpx.HTTPStatusError(f"Upstream media returned HTTP {response.status_code}",
                                               request=response.request, response=response)
                iterator = response.aiter_bytes(64 * 1024).__aiter__()
                first = await anext(iterator, b"") if method == "GET" else b""
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                manifest = resource.manifest or content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"} or first.lstrip(b"\xef\xbb\xbf\r\n \t").startswith(b"#EXTM3U")
                if manifest and method == "GET":
                    contents = bytearray(first)
                    async for chunk in iterator:
                        contents.extend(chunk)
                        if len(contents) > MANIFEST_LIMIT_BYTES:
                            raise UnsafeIngestionSource("Playlist exceeds the permitted size.")
                    payload = self.rewrite_manifest(bytes(contents), response.extensions["streamhome_final_url"],
                                                    response.extensions["streamhome_final_headers"])
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii") + payload)
                    headers_sent = True
                    await writer.drain()
                    return
                # XML/text demuxers can contain further network references. The
                # consumer format whitelist also excludes these external loaders.
                if content_type == "application/dash+xml" or first.lstrip().startswith((b"<?xml", b"<MPD", b"<!DOCTYPE", b"<html", b"[playlist]")):
                    raise UnsafeIngestionSource("Source manifest format is unavailable.")
                length = int(response.headers.get("content-length") or 0)
                if length < 0 or length > SOURCE_LIMIT_BYTES or len(first) > SOURCE_LIMIT_BYTES:
                    raise UnsafeIngestionSource("Media response exceeds the permitted size.")
                forwarded = [f"HTTP/1.1 {response.status_code} OK", "Connection: close"]
                encoded = response.headers.get("content-encoding", "identity").lower() != "identity"
                if encoded and response.status_code == 206:
                    raise UnsafeIngestionSource("Encoded upstream byte ranges are unavailable.")
                for key in ("content-type", "content-length", "content-range", "accept-ranges"):
                    if encoded and key == "content-length":
                        continue
                    value = response.headers.get(key)
                    if value is not None and "\r" not in value and "\n" not in value:
                        forwarded.append(f"{key}: {value}")
                writer.write(("\r\n".join(forwarded) + "\r\n\r\n").encode("iso-8859-1"))
                headers_sent = True
                delivered = len(first)
                writer.write(first)
                await writer.drain()
                async for chunk in iterator:
                    delivered += len(chunk)
                    if delivered > SOURCE_LIMIT_BYTES:
                        raise UnsafeIngestionSource("Media response exceeds the permitted size.")
                    writer.write(chunk)
                    await writer.drain()
        except (Exception, asyncio.CancelledError) as exc:
            if self.failure is None and isinstance(exc, (UnsafeIngestionSource, httpx.HTTPError, TimeoutError)):
                self.failure = exc
            if not headers_sent:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                try:
                    await writer.drain()
                except (ConnectionError, OSError):
                    pass
        finally:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
                self.tasks.discard(task)

    async def close(self) -> None:
        self.closed = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.client is not None:
            await self.client.aclose()
        for path in self.resources:
            _ACTIVE_INPUTS.discard(self.base_url + path)
        self.resources.clear()
        self.paths.clear()


@asynccontextmanager
async def controlled_media_input(url: str, *, headers: dict[str, str] | None = None,
                                 client_address: str = "", source_type: str = "auto"):
    """Yield an expiring local input for an approved remote HTTP source."""
    await validate_url(url, client_address=client_address)
    selected = validate_headers(headers)
    async with runtime_work("ingestion.source-relay", [EffectClass.DISPOSABLE_CACHE_WRITE]):
        relay = SourceRelay(client_address=client_address)
        try:
            await relay.start()
            yield await relay.prepare_input(url, selected, manifest=source_type == "hls")
            if relay.failure is not None:
                raise relay.failure
        finally:
            # Keep admission until every in-flight fetch and socket has stopped,
            # including repeated consumer cancellation during relay cleanup.
            cleanup = asyncio.create_task(relay.close())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError
