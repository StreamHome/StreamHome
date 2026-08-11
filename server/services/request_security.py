from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Request

from config import settings


def normalize_origin(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip()
    if not raw or any(character in raw for character in ("\r", "\n", "\t", " ", "\\", ",", "@")):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if scheme == "http" else 443
    port_suffix = f":{port}" if port and port != default_port else ""
    return f"{scheme}://{host}{port_suffix}"


def _address(value: Optional[str]) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _trusted_proxy(value: Optional[str]) -> bool:
    address = _address(value)
    if not address:
        return False
    for raw in settings.TRUSTED_PROXY_CIDRS:
        try:
            if address in ipaddress.ip_network(raw, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """Return a forwarded address only when the immediate peer is trusted."""
    peer = request.client.host if request.client else ""
    if _trusted_proxy(peer):
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if _address(forwarded):
            return forwarded[:64]
    return peer[:64] or "Unknown"


def request_is_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    return _trusted_proxy(peer) and request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def address_is_loopback(value: str) -> bool:
    address = _address(value)
    return bool(address and address.is_loopback)


def trusted_proxy_origin(request: Request) -> Optional[str]:
    """Return the browser-facing origin only when a configured proxy supplied it."""
    peer = request.client.host if request.client else ""
    if not _trusted_proxy(peer):
        return None
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if forwarded_proto not in {"http", "https"} or not forwarded_host:
        return None
    return normalize_origin(f"{forwarded_proto}://{forwarded_host}")


def same_origin_request(request: Request) -> bool:
    """Protect cookie-authenticated unsafe methods against cross-site requests."""
    raw_origin = request.headers.get("origin")
    if not raw_origin:
        return True
    origin = normalize_origin(raw_origin)
    if not origin:
        return False
    configured = {normalized for value in allowed_origins() if (normalized := normalize_origin(value))}
    if origin in configured:
        return True
    setup_origin = trusted_proxy_origin(request)
    return bool(
        not settings.SETUP_COMPLETE
        and request.url.path.startswith("/api/setup/")
        and request.cookies.get("streamhome_setup")
        and setup_origin
        and origin == setup_origin
    )


def integration_bearer_request(request: Request) -> bool:
    """Identify unsafe routes that always validate an explicit integration key."""
    authorization = request.headers.get("authorization", "").strip()
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.startswith("shk_"):
        return False
    path = request.url.path
    media_parts = path.removeprefix("/api/media/").split("/") if path.startswith("/api/media/") else []
    media_mutation = bool(
        all(media_parts)
        and (
            (request.method == "PATCH" and len(media_parts) == 2 and media_parts[1] == "metadata")
            or (
                request.method in {"PUT", "DELETE"}
                and len(media_parts) == 3
                and media_parts[1] in {"subtitles", "audio"}
            )
        )
    )
    return bool(
        (request.method == "POST" and path == "/api/add-movie")
        or media_mutation
        or (
            request.method == "DELETE"
            and path.startswith("/api/downloads/")
            and bool(path.removeprefix("/api/downloads/"))
            and "/" not in path.removeprefix("/api/downloads/")
        )
    )


def unsafe_cookie_request_requires_same_origin(request: Request) -> bool:
    cookie_authenticated = bool(
        request.cookies.get("streamhome_session")
        or request.cookies.get("streamhome_setup")
    )
    return bool(
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and cookie_authenticated
        and not integration_bearer_request(request)
    )


def allowed_origins() -> set[str]:
    allowed = {settings.PUBLIC_URL.rstrip("/"), *settings.ALLOWED_ORIGINS}
    parsed = urlsplit(settings.PUBLIC_URL)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        port = f":{parsed.port}" if parsed.port else ""
        allowed.update({f"{parsed.scheme}://localhost{port}", f"{parsed.scheme}://127.0.0.1{port}"})
    return {origin for origin in allowed if origin}
