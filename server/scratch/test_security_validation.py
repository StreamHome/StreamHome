import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.ingestion_security import UnsafeIngestionSource, validate_headers, validate_url
from services.request_security import client_ip
from services.secret_crypto import protect_secret, reveal_secret
from starlette.requests import Request


async def expect_blocked(url: str, client_address: str = "198.51.100.10") -> None:
    try:
        await validate_url(url, client_address=client_address)
    except UnsafeIngestionSource:
        return
    raise AssertionError(f"Expected the source to be blocked: {url}")


async def run() -> None:
    assert await validate_url("https://93.184.216.34/video.mp4", client_address="198.51.100.10")
    assert await validate_url("http://127.0.0.1:8765/video.mp4", client_address="127.0.0.1")

    for unsafe in (
        "file:///etc/passwd",
        "http://127.0.0.1:8000/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/private",
        "http://[::1]/private",
    ):
        await expect_blocked(unsafe)

    assert validate_headers({"User-Agent": "StreamHome test", "Authorization": "Bearer value"})
    for headers in (
        {"X-Internal": "value"},
        {"User-Agent": "safe\r\nAuthorization: injected"},
        {"Authorization": "x" * 5000},
    ):
        try:
            validate_headers(headers)
        except UnsafeIngestionSource:
            continue
        raise AssertionError(f"Expected source headers to be blocked: {headers.keys()}")

    untrusted = Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"x-forwarded-for", b"1.2.3.4")], "client": ("198.51.100.20", 5000), "scheme": "http", "server": ("test", 80), "query_string": b""})
    trusted = Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"x-forwarded-for", b"198.51.100.30")], "client": ("127.0.0.1", 5000), "scheme": "http", "server": ("test", 80), "query_string": b""})
    assert client_ip(untrusted) == "198.51.100.20"
    assert client_ip(trusted) == "198.51.100.30"

    encrypted = protect_secret("TOTP-TEST-SECRET")
    assert encrypted != "TOTP-TEST-SECRET" and reveal_secret(encrypted) == "TOTP-TEST-SECRET"

    print("Ingestion security validation checks passed.")


if __name__ == "__main__":
    asyncio.run(run())
