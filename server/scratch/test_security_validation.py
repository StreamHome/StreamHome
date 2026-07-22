import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.ingestion_security import UnsafeIngestionSource, validate_headers, validate_url
from services.request_security import client_ip
from services.rate_limit import _key as rate_limit_key
from services.rate_limit import fail as record_rate_limit_failure
from services.secret_crypto import protect_secret, reveal_secret
from models import RateLimitBucket
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
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

    temp_root = Path(__file__).resolve().parents[2] / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_root) as directory:
        database_path = Path(directory) / "rate-limit.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}?timeout=30")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)

            identity = "concurrent-security-test"
            key = rate_limit_key("test", identity)
            async with AsyncSession(engine) as session:
                session.add(RateLimitBucket(
                    key_hash=key,
                    namespace="test",
                    attempts=4,
                    window_started_at=time.time() - 120,
                    updated_at=time.time() - 120,
                ))
                await session.commit()
                await record_rate_limit_failure(session, "test", identity, limit=5, window_seconds=60)
                reset_bucket = await session.get(RateLimitBucket, key)
                assert reset_bucket and reset_bucket.attempts == 1 and reset_bucket.blocked_until is None

            async def record_concurrent_failure() -> None:
                async with AsyncSession(engine) as session:
                    await record_rate_limit_failure(session, "test", "parallel", limit=100, window_seconds=60)

            await asyncio.gather(*(record_concurrent_failure() for _ in range(8)))
            async with AsyncSession(engine) as session:
                parallel = await session.get(RateLimitBucket, rate_limit_key("test", "parallel"))
                assert parallel and parallel.attempts == 8
        finally:
            await engine.dispose()

    print("Ingestion security validation checks passed.")


if __name__ == "__main__":
    asyncio.run(run())
