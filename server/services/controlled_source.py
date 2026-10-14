"""Job-scoped HTTP inputs whose upstream fetches never belong to FFmpeg.

Every playlist, key, init fragment, segment and direct byte range goes through the
same connection-pinned source policy. Credentials remain in this process. Relay
capabilities are random, expire when the consumer scope exits and are not durable.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

from config import settings
from lifecycle.coordination import EffectClass, runtime_work
from lifecycle.media_work import settle
from services.hls_context import SourceContextPolicy
from services.ingestion_errors import write_task_diagnostics
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


class _ConsumerDisconnected(Exception):
    pass


async def _send(writer: asyncio.StreamWriter, payload: bytes) -> None:
    try:
        writer.write(payload)
        await writer.drain()
    except ConnectionError as error:
        raise _ConsumerDisconnected from error


def is_controlled_input(url: str) -> bool:
    return url in _ACTIVE_INPUTS


@dataclass(frozen=True)
class _Occurrence:
    role: str = "direct"
    playlist_id: int | None = None
    sequence: int | None = None
    start_seconds: float | None = None
    duration_seconds: float | None = None
    byte_range: str | None = None
    media_role: str = ""


@dataclass(frozen=True)
class _Resource:
    url: str
    headers: dict[str, str]
    manifest: bool = False
    resource_id: int = 0
    occurrence: _Occurrence = _Occurrence()


class SourceRelay:
    def __init__(self, *, client_address: str = "", task_id: str | None = None,
                 input_role: str = "source", attempt: int = 1) -> None:
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
        self.permissions = settings.HLS_CONTEXT_HEADER_PERMISSIONS
        self.context_policy: SourceContextPolicy | None = None
        self.task_id = task_id
        self.input_role = input_role if input_role in {"video", "audio", "source"} else "source"
        self.attempt = attempt
        self.request_count = 0
        self.failure_count = 0
        self.failures: list[dict] = []
        self.status_counts: dict[str, int] = {}

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

    def add(self, url: str, headers: dict[str, str], *, manifest: bool = False,
            occurrence: _Occurrence | None = None) -> str:
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
        occurrence = occurrence or _Occurrence(role="manifest" if manifest else "direct")
        key = (url, tuple(sorted(headers.items())), manifest, occurrence)
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
        self.resources[path] = _Resource(url, dict(headers), manifest, len(self.resources) + 1, occurrence)
        self.paths[key] = path
        local_url = self.base_url + path
        _ACTIVE_INPUTS.add(local_url)
        return local_url

    async def prepare_input(self, url: str, headers: dict[str, str], *, manifest: bool = False) -> str:
        if self.context_policy is None:
            try:
                self.context_policy = SourceContextPolicy.capture(self.permissions, url, headers)
            except (ValueError, UnicodeError):
                error = UnsafeIngestionSource("Source origin is unavailable.")
                error.streamhome_policy_hops = [{"hop": 0, "decision": "source-origin-rejected", "context_headers": []}]
                raise error from None
        # Detect disguised playlists before choosing demuxer-specific FFmpeg
        # options. Never buffer a direct video while inspecting its first chunk.
        async def inspect() -> _Resource:
            response = None
            primary_error = None
            try:
                response = await validated_stream_request(self.client, url, headers=headers,
                                                          client_address=self.client_address,
                                                          context_policy=self.context_policy)
                if response.status_code not in {200, 206}:
                    raise httpx.HTTPStatusError(f"Upstream media returned HTTP {response.status_code}",
                                               request=response.request, response=response)
                first = await anext(response.aiter_bytes(64 * 1024).__aiter__(), b"")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                detected = content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"} or first.lstrip(b"\xef\xbb\xbf\r\n \t").startswith(b"#EXTM3U")
                return _Resource(response.extensions["streamhome_final_url"],
                                 response.extensions["streamhome_final_headers"], manifest or detected)
            except BaseException as error:
                primary_error = error
                self.record_failure(error, response=response)
                if self.failure is not None and not isinstance(error, asyncio.CancelledError):
                    raise self.failure from None
                raise
            finally:
                if response is not None:
                    try:
                        _, cancelled = await settle(response.aclose())
                    except Exception as error:
                        self.record_failure(error, response=response, phase="preparation-close")
                        if primary_error is None:
                            raise self.failure from None
                    else:
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

    def rewrite_manifest(self, data: bytes, final_url: str, headers: dict[str, str],
                         *, playlist_id: int = 0, media_role: str = "") -> bytes:
        if len(data) > MANIFEST_LIMIT_BYTES:
            raise UnsafeIngestionSource("Playlist exceeds the permitted size.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UnsafeIngestionSource("Playlist encoding is unavailable.") from exc
        if not text.lstrip().startswith("#EXTM3U"):
            raise UnsafeIngestionSource("Source did not provide an HLS playlist.")

        sequence = 0
        elapsed = 0.0
        duration = None
        byte_range = None
        next_manifest = False

        def resource_url(value: str, occurrence: _Occurrence) -> str:
            if not value.strip():
                raise UnsafeIngestionSource("Playlist URI attribute is empty.")
            try:
                target = urljoin(final_url, value.strip())
            except ValueError as exc:
                raise UnsafeIngestionSource("Playlist resource URL is malformed.") from exc
            selected = source_headers_for_target(headers, final_url, target, context_policy=self.context_policy)
            manifest = occurrence.role == "manifest" or urlsplit(target).path.lower().endswith((".m3u8", ".m3u"))
            return self.add(target, selected, manifest=manifest, occurrence=occurrence)

        output = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                occurrence = _Occurrence("manifest" if next_manifest else "segment", playlist_id,
                                         None if next_manifest else sequence,
                                         None if next_manifest else elapsed,
                                         duration, byte_range, media_role)
                output.append(resource_url(stripped, occurrence))
                if not next_manifest:
                    sequence += 1
                    elapsed += duration or 0.0
                next_manifest, duration, byte_range = False, None, None
            elif stripped.startswith("#"):
                if stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    value = stripped.split(":", 1)[1]
                    sequence = int(value) if re.fullmatch(r"\d{1,19}", value) else 0
                elif stripped.startswith("#EXTINF:"):
                    try:
                        value = float(stripped.split(":", 1)[1].split(",", 1)[0])
                        duration = value if math.isfinite(value) and 0 <= value <= SOURCE_TIME_LIMIT_SECONDS else None
                    except ValueError:
                        duration = None
                elif stripped.startswith("#EXT-X-BYTERANGE:"):
                    value = stripped.split(":", 1)[1]
                    byte_range = value if re.fullmatch(r"\d{1,19}(?:@\d{1,19})?", value) else None
                elif stripped.startswith("#EXT-X-STREAM-INF:"):
                    next_manifest = True
                role = "key" if stripped.startswith("#EXT-X-KEY:") else "init" if stripped.startswith("#EXT-X-MAP:") else "manifest"
                track_role = "audio" if 'TYPE=AUDIO' in stripped else "subtitle" if 'TYPE=SUBTITLES' in stripped else media_role
                def substitute(match):
                    occurrence = _Occurrence(role, playlist_id, sequence, elapsed,
                                             byte_range=byte_range, media_role=track_role)
                    return match.group(1) + '"' + resource_url(match.group(2) if match.group(2) is not None else match.group(3), occurrence) + '"'
                rewritten = _URI_ATTRIBUTE.sub(substitute, line)
                # Empty or unterminated URI attributes cannot be passed on for
                # FFmpeg to reinterpret outside this source policy.
                if re.search(r'(?i)\bURI\s*=', line) and rewritten == line:
                    raise UnsafeIngestionSource("Playlist URI attribute is malformed.")
                output.append(rewritten)
            else:
                output.append(line)
        return ("\n".join(output) + "\n").encode("utf-8")

    def record_failure(self, error: Exception, *, resource: _Resource | None = None,
                       method: str = "GET", request_id: int = 0, phase: str | None = None,
                       response: httpx.Response | None = None) -> None:
        # Consumers may intentionally stop reading (probe prefix, native exit or
        # cancellation). Every other response/processing error rejects acceptance.
        if isinstance(error, (asyncio.CancelledError, _ConsumerDisconnected)):
            return
        error_kind = type(error).__name__
        if not isinstance(error, (UnsafeIngestionSource, httpx.HTTPError, TimeoutError)):
            error = httpx.RemoteProtocolError("Source response could not be processed.")
        if self.failure is None:
            self.failure = error
        self.failure_count += 1
        response = error.response if isinstance(error, httpx.HTTPStatusError) else response
        status = response.status_code if response is not None else 0
        self.status_counts[str(status)] = self.status_counts.get(str(status), 0) + 1
        if len(self.failures) < 8:
            snapshot = {"request_id": request_id, "method": method, "http_status": status,
                        "error_kind": error_kind, "input_role": self.input_role,
                        "phase": phase or ("resource" if resource else "preparation")}
            if resource is not None:
                snapshot.update(resource_id=resource.resource_id, **asdict(resource.occurrence))
            if response is not None:
                snapshot["policy_hops"] = response.extensions.get("streamhome_policy_hops", [])
            else:
                snapshot["policy_hops"] = getattr(error, "streamhome_policy_hops", [])
            self.failures.append(snapshot)

    def persist_failure(self) -> None:
        if self.task_id and self.failure is not None:
            snapshot = {"attempt": self.attempt, "input_role": self.input_role,
                        "request_count": self.request_count, "failure_count": self.failure_count,
                        "status_counts": self.status_counts, "failures": self.failures}
            path = write_task_diagnostics(self.task_id, "source relay", json.dumps(snapshot))
            if path:
                self.failure.streamhome_diagnostics_path = path

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        response = None
        headers_sent = False
        resource = None
        method = "GET"
        self.request_count += 1
        request_id = self.request_count
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
                                                          client_address=self.client_address, method=method,
                                                          context_policy=self.context_policy)
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
                                                    response.extensions["streamhome_final_headers"],
                                                    playlist_id=resource.resource_id,
                                                    media_role=resource.occurrence.media_role)
                    headers_sent = True
                    await _send(writer, f"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii") + payload)
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
                wire = ("\r\n".join(forwarded) + "\r\n\r\n").encode("iso-8859-1")
                headers_sent = True
                delivered = len(first)
                await _send(writer, wire + first)
                async for chunk in iterator:
                    delivered += len(chunk)
                    if delivered > SOURCE_LIMIT_BYTES:
                        raise UnsafeIngestionSource("Media response exceeds the permitted size.")
                    await _send(writer, chunk)
        except (Exception, asyncio.CancelledError) as exc:
            self.record_failure(exc, resource=resource, method=method, request_id=request_id, response=response)
            if not headers_sent:
                try:
                    await _send(writer, b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except (_ConsumerDisconnected, OSError):
                    pass
        finally:
            try:
                if response is not None:
                    try:
                        await response.aclose()
                    except Exception as error:
                        failure = error if isinstance(error, httpx.HTTPError) else httpx.TransportError("Source response could not close")
                        self.record_failure(failure, resource=resource, method=method,
                                            request_id=request_id, phase="response-close", response=response)
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
        try:
            if self.client is not None:
                await self.client.aclose()
        except Exception as error:
            failure = error if isinstance(error, httpx.HTTPError) else httpx.TransportError("Source relay could not close")
            self.record_failure(failure, phase="relay-close")
        finally:
            for path in self.resources:
                _ACTIVE_INPUTS.discard(self.base_url + path)
            self.resources.clear()
            self.paths.clear()


@asynccontextmanager
async def controlled_media_input(url: str, *, headers: dict[str, str] | None = None,
                                 client_address: str = "", source_type: str = "auto",
                                 task_id: str | None = None, input_role: str = "source", attempt: int = 1,
                                 owner_callback=None):
    """Yield an expiring local input for an approved remote HTTP source."""
    async with runtime_work("ingestion.source-relay", [EffectClass.DISPOSABLE_CACHE_WRITE]):
        relay = SourceRelay(client_address=client_address, task_id=task_id, input_role=input_role, attempt=attempt)
        if owner_callback is not None:
            owner_callback(relay)
        try:
            try:
                try:
                    selected = validate_headers(headers)
                    await validate_url(url, client_address=client_address)
                except Exception as error:
                    error.streamhome_policy_hops = [{"hop": 0, "decision": "root-validation-rejected", "context_headers": []}]
                    raise
                await relay.start()
                local_url = await relay.prepare_input(url, selected, manifest=source_type == "hls")
            except Exception as error:
                if relay.failure is None:
                    relay.record_failure(error)
                if relay.failure is not None:
                    raise relay.failure from None
                raise
            yield local_url
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
            try:
                cleanup.result()
            finally:
                relay.persist_failure()
            if cancelled:
                raise asyncio.CancelledError
        if relay.failure is not None:
            raise relay.failure
