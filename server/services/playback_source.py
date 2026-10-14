from __future__ import annotations

import mimetypes
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Mapping, Optional

import aiofiles
import aiofiles.os
import httpx

from services.ingestion_security import validate_headers, validated_stream_request
from services.media_source import ResolvedMediaSource
from services.rclone import rclone_service
from lifecycle.media_work import settle


READ_CHUNK_BYTES = 256 * 1024


class PlaybackSourceFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PlaybackSourceStat:
    size: int
    content_type: str
    supports_ranges: bool


class SeekablePlaybackSource(ABC):
    """Uniform random-access input used by direct and adaptive playback."""

    @abstractmethod
    async def stat(self) -> PlaybackSourceStat:
        raise NotImplementedError

    @abstractmethod
    async def open_range(self, start: int, length: int) -> AsyncIterator[bytes]:
        raise NotImplementedError

    @abstractmethod
    def ffmpeg_input(self, media_id: str, ticket: str, source_id: str = "main") -> str:
        raise NotImplementedError


class LocalPlaybackSource(SeekablePlaybackSource):
    def __init__(self, path: Path, catalog_path: str):
        self.path = path.resolve()
        self.catalog_path = catalog_path
        self._pinned_handle = None

    async def pin(self) -> None:
        """Open the selected inode while its media publication lock is held."""
        if self._pinned_handle is None:
            handle, cancelled = await settle(aiofiles.open(self.path, "rb"))
            self._pinned_handle = handle
            if cancelled:
                await self.close()
                import asyncio
                raise asyncio.CancelledError

    async def close(self) -> None:
        handle, self._pinned_handle = self._pinned_handle, None
        if handle is not None:
            await settle(handle.close())

    async def stat(self) -> PlaybackSourceStat:
        size = os.fstat(self._pinned_handle.fileno()).st_size if self._pinned_handle is not None else (await aiofiles.os.stat(self.path)).st_size
        return PlaybackSourceStat(
            size=size,
            content_type=mimetypes.guess_type(self.catalog_path)[0] or "application/octet-stream",
            supports_ranges=True,
        )

    async def open_range(self, start: int, length: int) -> AsyncIterator[bytes]:
        async def chunks(handle):
            await handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = await handle.read(min(READ_CHUNK_BYTES, remaining))
                if not chunk:
                    raise PlaybackSourceFailure(
                        "LOCAL_SOURCE_TRUNCATED",
                        "The local media ended before its declared size.",
                    )
                remaining -= len(chunk)
                yield chunk
        if self._pinned_handle is not None:
            async for chunk in chunks(self._pinned_handle):
                yield chunk
        else:
            async with aiofiles.open(self.path, "rb") as handle:
                async for chunk in chunks(handle):
                    yield chunk

    def ffmpeg_input(self, media_id: str, ticket: str, source_id: str = "main") -> str:
        del media_id, ticket, source_id
        return str(self.path)


class DrivePlaybackSource(SeekablePlaybackSource):
    def __init__(self, remote_path: str, size: Optional[int], catalog_path: str, loopback_url: str):
        self.remote_path = remote_path
        self.known_size = int(size or 0)
        self.catalog_path = catalog_path
        self.loopback_url = loopback_url.rstrip("/")

    async def stat(self) -> PlaybackSourceStat:
        size = self.known_size
        if size <= 0:
            result = await rclone_service.run("lsjson", self.remote_path, "--stat", timeout=30)
            if not result.ok:
                raise PlaybackSourceFailure(
                    result.error_code or "CLOUD_SOURCE_FAILED",
                    "Google Drive did not return the media file metadata.",
                )
            try:
                import json

                payload = json.loads(result.stdout)
                if isinstance(payload, list):
                    payload = payload[0]
                size = int(payload.get("Size", 0))
            except (ValueError, IndexError, KeyError, TypeError) as exc:
                raise PlaybackSourceFailure(
                    "CLOUD_SIZE_INVALID",
                    "Google Drive returned invalid media metadata.",
                ) from exc
        if size <= 0:
            raise PlaybackSourceFailure("CLOUD_SIZE_INVALID", "Google Drive returned an empty media object.")
        return PlaybackSourceStat(
            size=size,
            content_type=mimetypes.guess_type(self.catalog_path)[0] or "application/octet-stream",
            supports_ranges=True,
        )

    async def open_range(self, start: int, length: int) -> AsyncIterator[bytes]:
        try:
            process, stream = await rclone_service.open_stream(
                "cat",
                self.remote_path,
                "--offset",
                str(start),
                "--count",
                str(length),
            )
        except (FileNotFoundError, OSError) as exc:
            raise PlaybackSourceFailure(
                "RCLONE_UNAVAILABLE",
                "Google Drive range streaming is unavailable.",
            ) from exc
        delivered = 0
        try:
            async for chunk in stream:
                delivered += len(chunk)
                yield chunk
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if delivered != length:
            raise PlaybackSourceFailure(
                "CLOUD_SOURCE_TRUNCATED",
                "Google Drive ended the ranged media response early.",
            )

    def ffmpeg_input(self, media_id: str, ticket: str, source_id: str = "main") -> str:
        from urllib.parse import quote

        return (
            f"{self.loopback_url}/api/playback/source/{quote(media_id, safe='')}"
            f"?ticket={quote(ticket, safe='')}&source_id={quote(source_id, safe='')}"
        )


class HttpPlaybackSource(SeekablePlaybackSource):
    """Validated source-URL reader with explicit redirect revalidation."""

    def __init__(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        client_address: str = "",
    ):
        self.url = url
        self.headers = validate_headers(headers)
        self.client_address = client_address
        self._resolved_url = url
        self._stat: PlaybackSourceStat | None = None

    async def _request(
        self,
        method: str,
        *,
        range_header: str | None = None,
    ) -> httpx.Response:
        selected_url = self._resolved_url
        headers = dict(self.headers)
        if range_header:
            headers = {key: value for key, value in headers.items() if key.lower() != "range"}
            headers["Range"] = range_header
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(20, read=60)) as client:
            response = await validated_stream_request(client, selected_url, headers=headers,
                                                      client_address=self.client_address, method=method)
        self._resolved_url = response.extensions["streamhome_final_url"]
        # The owned response stream closes its actual pinned client. Keeping this
        # source's headers after a cross-origin redirect would reintroduce them on
        # the next range request, so persist only the final approved header set.
        self.headers = {key: value for key, value in response.extensions["streamhome_final_headers"].items()
                        if key.lower() != "range"}
        return response

    @staticmethod
    async def _close_response(response: httpx.Response) -> None:
        client = response.extensions.get("streamhome_client")
        await response.aclose()
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()

    async def stat(self) -> PlaybackSourceStat:
        if self._stat:
            return self._stat
        response = await self._request("HEAD")
        try:
            size = int(response.headers.get("content-length") or 0)
            supports_ranges = "bytes" in response.headers.get("accept-ranges", "").lower()
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
        finally:
            await self._close_response(response)
        if size <= 0 or not supports_ranges:
            response = await self._request("GET", range_header="bytes=0-0")
            try:
                content_range = response.headers.get("content-range", "")
                if response.status_code != 206 or "/" not in content_range:
                    raise PlaybackSourceFailure(
                        "HTTP_RANGE_UNSUPPORTED",
                        "The media source does not support random-access byte ranges.",
                    )
                size = int(content_range.rsplit("/", 1)[1])
                supports_ranges = True
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
            finally:
                await self._close_response(response)
        self._stat = PlaybackSourceStat(
            size=size,
            content_type=content_type or mimetypes.guess_type(self._resolved_url)[0] or "application/octet-stream",
            supports_ranges=supports_ranges,
        )
        return self._stat

    async def open_range(self, start: int, length: int) -> AsyncIterator[bytes]:
        end = start + length - 1
        response = await self._request("GET", range_header=f"bytes={start}-{end}")
        if response.status_code != 206:
            await self._close_response(response)
            raise PlaybackSourceFailure(
                "HTTP_RANGE_UNSUPPORTED",
                "The media source refused the requested byte range.",
            )
        delivered = 0
        try:
            async for chunk in response.aiter_bytes(READ_CHUNK_BYTES):
                delivered += len(chunk)
                if delivered > length:
                    chunk = chunk[: length - (delivered - len(chunk))]
                if chunk:
                    yield chunk
                if delivered >= length:
                    break
        finally:
            await self._close_response(response)
        if delivered < length:
            raise PlaybackSourceFailure(
                "HTTP_SOURCE_TRUNCATED",
                "The media source ended the ranged response early.",
            )

    def ffmpeg_input(self, media_id: str, ticket: str, source_id: str = "main") -> str:
        del media_id, ticket, source_id
        raise PlaybackSourceFailure(
            "HTTP_INPUT_REQUIRES_RELAY",
            "Remote FFmpeg input requires a controlled media input scope.",
        )


def source_reader(source: ResolvedMediaSource, *, loopback_url: str) -> SeekablePlaybackSource:
    if source.local_exists:
        return LocalPlaybackSource(source.local_path, source.catalog_path)
    if source.cloud_exists and source.cloud_path:
        return DrivePlaybackSource(
            source.cloud_path,
            source.cloud_size,
            source.catalog_path,
            loopback_url,
        )
    raise PlaybackSourceFailure("MEDIA_SOURCE_MISSING", "The media source is unavailable.")
