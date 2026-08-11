from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable, Iterable

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import IntegrationCredential


security = HTTPBearer(auto_error=False)

INTEGRATION_SCOPE_DEFINITIONS = {
    "ingest": {
        "label": "Manage media",
        "description": "Submit media and manage application-owned markers, subtitles, and dubbing sidecars.",
    },
    "downloads:read": {
        "label": "View download queue",
        "description": "Read current and recent ingestion task status.",
    },
    "downloads:cancel": {
        "label": "Cancel downloads",
        "description": "Cancel ingestion workers and remove download tasks.",
    },
}
MAX_ACTIVE_INTEGRATION_CREDENTIALS = 50


def integration_token_hash(token: str) -> str:
    return hmac.new(settings.JWT_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_integration_scopes(scopes: Iterable[str]) -> list[str]:
    normalized = sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "missing_integration_scope", "message": "Select at least one API key permission."},
        )
    unknown = [scope for scope in normalized if scope not in INTEGRATION_SCOPE_DEFINITIONS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_integration_scope",
                "message": f"Unsupported API key permission: {unknown[0]}.",
            },
        )
    return normalized


async def authenticate_integration_token(
    token: str,
    scope: str,
    request: Request,
    db: AsyncSession,
) -> IntegrationCredential:
    digest = integration_token_hash(token)
    result = await db.exec(select(IntegrationCredential).where(IntegrationCredential.token_hash == digest))
    credential = result.first()
    current_time = time.time()
    if not credential or credential.revoked_at or (
        credential.expires_at is not None and credential.expires_at <= current_time
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_integration_credential",
                "message": "The API key is invalid, expired, or revoked.",
            },
        )
    if scope not in credential.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "insufficient_scope",
                "message": "This integration credential cannot perform that operation.",
            },
        )
    if credential.last_used_at is None or current_time - credential.last_used_at >= 300:
        credential.last_used_at = current_time
        db.add(credential)
        await db.commit()
    request.state.integration_credential = credential
    return credential


def require_integration_scope(scope: str) -> Callable:
    if scope not in INTEGRATION_SCOPE_DEFINITIONS:
        raise ValueError(f"Unknown integration scope: {scope}")

    async def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(security),
        db: AsyncSession = Depends(get_session),
    ) -> IntegrationCredential:
        if not credentials or not credentials.credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "missing_integration_credential",
                    "message": "Send the StreamHome API key as an Authorization Bearer token.",
                },
            )
        return await authenticate_integration_token(credentials.credentials, scope, request, db)

    return dependency
