from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from db import get_session
from models import IntegrationCredential, MediaAudioUpdate, MediaSkipMarkersUpdate, MediaSubtitleUpdate
from services.integration_auth import require_integration_scope
from services.media_updates import (
    MediaUpdateFailure,
    remove_audio,
    remove_subtitle,
    update_skip_markers,
    upsert_audio,
    upsert_subtitle,
    validate_remote_input,
)
from services.request_security import client_ip


router = APIRouter(prefix="/api/media", tags=["media-sender"])


def update_error(exc: MediaUpdateFailure) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


@router.patch("/{media_id}/metadata")
async def patch_media_metadata(
    media_id: str,
    payload: MediaSkipMarkersUpdate,
    db: AsyncSession = Depends(get_session),
    credential: IntegrationCredential = Depends(require_integration_scope("ingest")),
):
    """Replace application-owned skip markers without accepting TMDB-owned fields."""
    del credential
    markers = {
        name: [marker.model_dump() for marker in values]
        for name, values in payload.skip_markers.items()
    }
    try:
        return await update_skip_markers(media_id, markers, db)
    except MediaUpdateFailure as exc:
        await db.rollback()
        raise update_error(exc) from exc


@router.put("/{media_id}/subtitles/{track_id}")
async def put_media_subtitle(
    media_id: str,
    track_id: str,
    payload: MediaSubtitleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    credential: IntegrationCredential = Depends(require_integration_scope("ingest")),
):
    """Create or replace one application-owned subtitle track."""
    del credential
    try:
        headers = await validate_remote_input(payload.url, payload.headers, client_ip(request))
        return await upsert_subtitle(
            media_id,
            track_id,
            language=payload.language,
            label=payload.label,
            url=payload.url,
            headers=headers,
            client_address=client_ip(request),
            db=db,
        )
    except MediaUpdateFailure as exc:
        await db.rollback()
        raise update_error(exc) from exc


@router.delete("/{media_id}/subtitles/{track_id}")
async def delete_media_subtitle(
    media_id: str,
    track_id: str,
    db: AsyncSession = Depends(get_session),
    credential: IntegrationCredential = Depends(require_integration_scope("ingest")),
):
    """Remove one application-owned subtitle track and its sidecar."""
    del credential
    try:
        return await remove_subtitle(media_id, track_id, db)
    except MediaUpdateFailure as exc:
        await db.rollback()
        raise update_error(exc) from exc


@router.put("/{media_id}/audio/{language}")
async def put_media_audio(
    media_id: str,
    language: str,
    payload: MediaAudioUpdate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    credential: IntegrationCredential = Depends(require_integration_scope("ingest")),
):
    """Create or replace one application-owned dubbing sidecar."""
    del credential
    try:
        headers = await validate_remote_input(payload.url, payload.headers, client_ip(request))
        return await upsert_audio(
            media_id,
            language,
            url=payload.url,
            headers=headers,
            client_address=client_ip(request),
            source_type=payload.source_type,
            db=db,
        )
    except MediaUpdateFailure as exc:
        await db.rollback()
        raise update_error(exc) from exc


@router.delete("/{media_id}/audio/{language}")
async def delete_media_audio(
    media_id: str,
    language: str,
    db: AsyncSession = Depends(get_session),
    credential: IntegrationCredential = Depends(require_integration_scope("ingest")),
):
    """Remove one application-owned dubbing sidecar."""
    del credential
    try:
        return await remove_audio(media_id, language, db)
    except MediaUpdateFailure as exc:
        await db.rollback()
        raise update_error(exc) from exc
