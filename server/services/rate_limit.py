from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, status
from sqlalchemy import case, update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from models import RateLimitBucket


def _key(namespace: str, identity: str) -> str:
    value = f"{namespace}:{identity.strip().lower()}"
    return hmac.new(settings.JWT_SECRET.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _exception(seconds: int) -> HTTPException:
    retry = max(1, seconds)
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": str(retry)},
        detail={"code": "rate_limited", "message": "Too many requests. Try again later.", "retryAfterSeconds": retry},
    )


async def enforce(db: AsyncSession, namespace: str, identity: str) -> None:
    bucket = await db.get(RateLimitBucket, _key(namespace, identity))
    current = time.time()
    if bucket and bucket.blocked_until and bucket.blocked_until > current:
        raise _exception(int(bucket.blocked_until - current))


async def fail(db: AsyncSession, namespace: str, identity: str, *, limit: int, window_seconds: int) -> None:
    current = time.time()
    key = _key(namespace, identity)
    await db.execute(
        insert(RateLimitBucket)
        .values(
            key_hash=key,
            namespace=namespace,
            attempts=0,
            window_started_at=current,
            blocked_until=None,
            updated_at=current,
        )
        .on_conflict_do_nothing(index_elements=["key_hash"])
    )
    expired = RateLimitBucket.window_started_at <= current - window_seconds
    incremented = RateLimitBucket.attempts + 1
    await db.execute(
        update(RateLimitBucket)
        .where(RateLimitBucket.key_hash == key)
        .values(
            namespace=namespace,
            attempts=case((expired, 1), else_=incremented),
            window_started_at=case((expired, current), else_=RateLimitBucket.window_started_at),
            blocked_until=case(
                (expired, current + window_seconds if limit <= 1 else None),
                (incremented >= limit, current + window_seconds),
                else_=RateLimitBucket.blocked_until,
            ),
            updated_at=current,
        )
    )
    await db.commit()
    bucket = await db.get(RateLimitBucket, key)
    if bucket and bucket.blocked_until:
        raise _exception(window_seconds)


async def clear(db: AsyncSession, namespace: str, identity: str) -> None:
    bucket = await db.get(RateLimitBucket, _key(namespace, identity))
    if bucket:
        await db.delete(bucket)
        await db.commit()
