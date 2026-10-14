from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from config import settings
from lifecycle.source_network import PinnedAsyncTransport


HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
ALLOWED_HEADERS = {
    "accept",
    "accept-language",
    "authorization",
    "cookie",
    "origin",
    "referer",
    "range",
    "user-agent",
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SENSITIVE_CROSS_ORIGIN_HEADERS = {"authorization", "cookie", "origin", "referer"}
MAX_VALIDATED_REDIRECTS = 5


class UnsafeIngestionSource(ValueError):
    pass


def source_request_failure(error: Exception):
    """Classify a source failure without copying its URL or headers to evidence."""
    from services.ingestion_errors import IngestionFailure, classify_failure
    if isinstance(error, UnsafeIngestionSource):
        return IngestionFailure("UNSAFE_SOURCE", "The media source was rejected by the network policy.", False)
    if isinstance(error, httpx.HTTPStatusError):
        return classify_failure(f"HTTP error {error.response.status_code}")
    if isinstance(error, (httpx.TransportError, TimeoutError)):
        return IngestionFailure("SOURCE_UNREACHABLE", "The media sender source could not be reached.", True)
    return IngestionFailure("MEDIA_SOURCE_FAILED", "The media source could not be processed.", False)


def validate_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    if len(headers) > 16:
        raise UnsafeIngestionSource("Too many source headers were supplied.")
    normalized: dict[str, str] = {}
    total = 0
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip()
        value = str(raw_value).strip()
        if not HEADER_NAME.fullmatch(name) or name.lower() not in ALLOWED_HEADERS:
            raise UnsafeIngestionSource(f"Source header '{name[:32]}' is not allowed.")
        if "\r" in value or "\n" in value or "\x00" in value:
            raise UnsafeIngestionSource("Source headers may not contain control-line characters.")
        total += len(name) + len(value)
        if len(value) > 4096 or total > 8192:
            raise UnsafeIngestionSource("Source headers exceed the permitted size.")
        normalized[name] = value
    return normalized


def _blocked(address: ipaddress._BaseAddress) -> bool:
    return not address.is_global


async def validate_url(url: str | None, *, client_address: str = "") -> str | None:
    if not url:
        return None
    if len(url) > 4096:
        raise UnsafeIngestionSource("Source URL is too long.")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise UnsafeIngestionSource("Source URL contains control characters.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise UnsafeIngestionSource("Source URL is malformed.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise UnsafeIngestionSource("Only absolute HTTP and HTTPS source URLs are allowed.")
    if parsed.username or parsed.password:
        raise UnsafeIngestionSource("Credentials must be supplied as approved headers, not in the URL.")
    try:
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeIngestionSource("Source URL contains an invalid port.") from exc
    if port < 1 or port > 65535:
        raise UnsafeIngestionSource("Source URL contains an invalid port.")

    await resolve_source_addresses(hostname, port, client_address=client_address)
    return url


async def resolve_source_addresses(host: str, port: int, *, client_address: str = "") -> list[str]:
    """Resolve once and return approved numeric socket destinations."""
    host = host.rstrip(".").lower()
    explicitly_trusted = host in settings.INGEST_TRUSTED_HOSTS
    try:
        client_ip = ipaddress.ip_address(client_address)
    except ValueError:
        client_ip = None

    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)),
            timeout=10.0,
        )
    except (socket.gaierror, UnicodeError, ValueError, asyncio.TimeoutError) as exc:
        raise UnsafeIngestionSource("Source host could not be resolved.") from exc
    addresses = {ipaddress.ip_address(item[4][0]) for item in records}
    if not addresses:
        raise UnsafeIngestionSource("Source host did not resolve to an address.")
    for address in addresses:
        local_loopback_exception = bool(client_ip and client_ip.is_loopback and address.is_loopback)
        if _blocked(address) and not explicitly_trusted and not local_loopback_exception:
            raise UnsafeIngestionSource("Private, local, metadata, and reserved source networks are blocked.")
    return sorted(str(address) for address in addresses)


def source_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme.lower(), (parsed.hostname or "").rstrip(".").lower(),
                parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80))
    except ValueError as exc:
        raise UnsafeIngestionSource("Source URL origin is malformed.") from exc


def source_headers_for_target(headers: Mapping[str, str], previous_url: str, target_url: str) -> dict[str, str]:
    if source_origin(previous_url) == source_origin(target_url):
        return dict(headers)
    return {key: value for key, value in headers.items()
            if key.lower() not in SENSITIVE_CROSS_ORIGIN_HEADERS}


def pinned_source_client(*, client_address: str = "", timeout: httpx.Timeout | float = 60.0) -> httpx.AsyncClient:
    async def resolve(host: str, port: int) -> list[str]:
        return await resolve_source_addresses(host, port, client_address=client_address)
    return httpx.AsyncClient(transport=PinnedAsyncTransport(resolve), trust_env=False,
                             follow_redirects=False, timeout=timeout,
                             headers={"Accept-Encoding": "identity"})


class _OwnedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, client: httpx.AsyncClient) -> None:
        self.stream, self.client = stream, client

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self.stream.aclose()
        finally:
            await self.client.aclose()


async def validated_stream_request(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    client_address: str = "",
    method: str = "GET",
) -> httpx.Response:
    """Open a pinned streamed request and revalidate every redirected connection.

    The caller's client supplies its timeout, never its proxy/connection transport.
    Closing the returned response also closes the isolated pinned client.
    """

    selected_url = url
    selected_headers = validate_headers(headers)
    if method not in {"GET", "HEAD"}:
        raise UnsafeIngestionSource("Source request method is unavailable.")
    pinned = pinned_source_client(client_address=client_address, timeout=client.timeout)
    try:
        for _ in range(MAX_VALIDATED_REDIRECTS + 1):
            await validate_url(selected_url, client_address=client_address)
            request = pinned.build_request(method, selected_url, headers=selected_headers)
            response = await pinned.send(request, stream=True)
            if response.status_code not in REDIRECT_STATUSES:
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    await response.aclose()
                    raise UnsafeIngestionSource("Encoded source responses are unavailable; identity encoding is required.")
                response.extensions["streamhome_final_url"] = selected_url
                response.extensions["streamhome_final_headers"] = selected_headers
                response.extensions["streamhome_client"] = pinned
                response.stream = _OwnedResponseStream(response.stream, pinned)
                return response
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise UnsafeIngestionSource("Source redirect did not include a destination.")
            try:
                target = urljoin(selected_url, location)
            except ValueError as exc:
                raise UnsafeIngestionSource("Source redirect destination is malformed.") from exc
            selected_headers = source_headers_for_target(selected_headers, selected_url, target)
            if source_origin(selected_url) != source_origin(target):
                pinned.cookies.clear()
            selected_url = target
        raise UnsafeIngestionSource("Source redirected too many times.")
    except BaseException:
        await pinned.aclose()
        raise
