from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Mapping
from urllib.parse import urlsplit

from config import settings


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


class UnsafeIngestionSource(ValueError):
    pass


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
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeIngestionSource("Only absolute HTTP and HTTPS source URLs are allowed.")
    if parsed.username or parsed.password:
        raise UnsafeIngestionSource("Credentials must be supplied as approved headers, not in the URL.")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeIngestionSource("Source URL contains an invalid port.") from exc

    host = parsed.hostname.rstrip(".").lower()
    explicitly_trusted = host in settings.INGEST_TRUSTED_HOSTS
    try:
        client_ip = ipaddress.ip_address(client_address)
    except ValueError:
        client_ip = None

    loop = asyncio.get_running_loop()
    try:
        records = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    except socket.gaierror as exc:
        raise UnsafeIngestionSource("Source host could not be resolved.") from exc
    addresses = {ipaddress.ip_address(item[4][0]) for item in records}
    if not addresses:
        raise UnsafeIngestionSource("Source host did not resolve to an address.")
    for address in addresses:
        local_loopback_exception = bool(client_ip and client_ip.is_loopback and address.is_loopback)
        if _blocked(address) and not explicitly_trusted and not local_loopback_exception:
            raise UnsafeIngestionSource("Private, local, metadata, and reserved source networks are blocked.")
    return url
