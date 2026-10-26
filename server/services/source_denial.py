"""Bounded evidence extraction from an already rejected source response."""
from __future__ import annotations

import asyncio
import re

import httpx


DENIAL_LIMIT = 32 * 1024
DENIAL_TIMEOUT = 2.0


async def denial_context(response: httpx.Response) -> dict[str, str]:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if response.status_code != 403 or content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
        return {}
    try:
        async with asyncio.timeout(DENIAL_TIMEOUT):
            body = bytearray()
            # Stream raw bytes: never decompress an untrusted error response.
            async for chunk in response.aiter_raw():
                body.extend(chunk[:DENIAL_LIMIT - len(body)])
                if len(body) >= DENIAL_LIMIT:
                    break
            text = body.decode("utf-8", errors="replace").lower()
            plain = re.sub(r"<[^<>]*>", " ", text)
            cloudflare = "cloudflare" in plain
            code = re.search(r"\berror\s*(?:code\s*[:#]?\s*)?1005\b", plain)
            denial = "access denied" in plain
            if cloudflare and code and denial:
                return {"provider": "cloudflare", "provider_code": "1005"}
    except Exception:
        # Inspection is advisory. Cancellation (BaseException) still propagates.
        pass
    return {}
