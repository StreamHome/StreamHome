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
from config import settings
from lifecycle.media_work import MediaRecoveryRequired
from lifecycle.coordination import EffectClass, runtime_work
from services.logger import logger


router = APIRouter(prefix="/api/media", tags=["media-sender"])


def media_mutation_effects() -> set[EffectClass]:
    effects = {EffectClass.DB_WRITE, EffectClass.DURABLE_MEDIA_WRITE}
    if settings.STORAGE_ENGINE == "CLOUD":
        effects.add(EffectClass.CLOUD_WRITE)
    return effects


def update_error(exc: MediaUpdateFailure) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


async def rollback_failed_update(db: AsyncSession) -> None:
    """Preserve the operation's API error when its failed session cannot roll back."""
    try:
        await db.rollback()
    except Exception as exc:
        # Cancellation remains authoritative. A committed/failed-close session
        # cannot make an unresolved media operation safe by raising another error.
        logger.warning("[Media Updates] Session rollback failed: %s", type(exc).__name__)


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
        async with runtime_work("media-sender.sidecar", media_mutation_effects()):
            return await update_skip_markers(media_id, markers, db)
    except MediaRecoveryRequired as exc:
        await rollback_failed_update(db)
        raise HTTPException(status_code=503, detail={"code": "media_recovery_required", "message": "This media operation needs recovery before further changes."}) from exc
    except MediaUpdateFailure as exc:
        await rollback_failed_update(db)
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
        async with runtime_work("media-sender.sidecar", media_mutation_effects()):
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
    except MediaRecoveryRequired as exc:
        await rollback_failed_update(db)
        raise HTTPException(status_code=503, detail={"code": "media_recovery_required", "message": "This media operation needs recovery before further changes."}) from exc
    except MediaUpdateFailure as exc:
        await rollback_failed_update(db)
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
        async with runtime_work("media-sender.sidecar", media_mutation_effects()):
            return await remove_subtitle(media_id, track_id, db)
    except MediaRecoveryRequired as exc:
        await rollback_failed_update(db)
        raise HTTPException(status_code=503, detail={"code": "media_recovery_required", "message": "This media operation needs recovery before further changes."}) from exc
    except MediaUpdateFailure as exc:
        await rollback_failed_update(db)
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
        async with runtime_work("media-sender.sidecar", media_mutation_effects()):
            return await upsert_audio(
                media_id,
                language,
                url=payload.url,
                headers=headers,
                client_address=client_ip(request),
                source_type=payload.source_type,
                db=db,
            )
    except MediaRecoveryRequired as exc:
        await rollback_failed_update(db)
        raise HTTPException(status_code=503, detail={"code": "media_recovery_required", "message": "This media operation needs recovery before further changes."}) from exc
    except MediaUpdateFailure as exc:
        await rollback_failed_update(db)
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
        async with runtime_work("media-sender.sidecar", media_mutation_effects()):
            return await remove_audio(media_id, language, db)
    except MediaRecoveryRequired as exc:
        await rollback_failed_update(db)
        raise HTTPException(status_code=503, detail={"code": "media_recovery_required", "message": "This media operation needs recovery before further changes."}) from exc
    except MediaUpdateFailure as exc:
        await rollback_failed_update(db)
        raise update_error(exc) from exc
