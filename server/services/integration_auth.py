from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import IntegrationCredential


security = HTTPBearer(auto_error=False)


def integration_token_hash(token: str) -> str:
    return hmac.new(settings.JWT_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def require_integration_scope(scope: str) -> Callable:
    async def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(security),
        db: AsyncSession = Depends(get_session),
    ) -> IntegrationCredential:
        if not credentials or not credentials.credentials:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing integration credential.")
        digest = integration_token_hash(credentials.credentials)
        result = await db.execute(select(IntegrationCredential).where(IntegrationCredential.token_hash == digest))
        credential = result.scalars().first()
        now = time.time()
        if not credential or credential.revoked_at or (credential.expires_at and credential.expires_at <= now):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or inactive integration credential.")
        if scope not in credential.scopes:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "insufficient_scope", "message": "This integration credential cannot perform that operation."})
        if credential.last_used_at is None or now - credential.last_used_at >= 300:
            credential.last_used_at = now
            db.add(credential)
            await db.commit()
        request.state.integration_credential = credential
        return credential

    return dependency
