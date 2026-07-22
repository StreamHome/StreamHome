from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Request

from config import settings


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


def same_origin_request(request: Request) -> bool:
    """Protect cookie-authenticated unsafe methods against cross-site requests."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    return origin.rstrip("/") in allowed_origins()


def allowed_origins() -> set[str]:
    allowed = {settings.PUBLIC_URL.rstrip("/"), *settings.ALLOWED_ORIGINS}
    parsed = urlsplit(settings.PUBLIC_URL)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        port = f":{parsed.port}" if parsed.port else ""
        allowed.update({f"{parsed.scheme}://localhost{port}", f"{parsed.scheme}://127.0.0.1{port}"})
    return {origin for origin in allowed if origin}
