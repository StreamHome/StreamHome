"""Human-session storage policy, capacity and placement controls.

Routes validate the browser contract; the storage services retain ownership of
durable reviews, acceptance receipts and every background transfer.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from lifecycle.coordination import AdmissionClosedError, EffectClass, runtime_work
from models import AuthSession
from routes.auth import require_recent_reauth


router = APIRouter(prefix="/api/admin/storage", tags=["Admin storage"])
RequestIdentity = Annotated[str, StringConstraints(
    min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
)]
ReviewIdentity = Annotated[str, StringConstraints(
    min_length=8, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
)]
Budget = Annotated[int, Field(strict=True, gt=0, le=9_007_199_254_740_991)]
Revision = Annotated[int, Field(strict=True, ge=0)]


class StorageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyDraft(StorageRequest):
    offload_enabled: bool = Field(strict=True)
    local_budget_bytes: Budget | None
    cloud_budget_bytes: Budget | None
    prefetch_next_episode: bool = Field(strict=True)
    hevc_compression_mode: Literal["auto", "on", "off"]


class PreviewPolicyRequest(StorageRequest):
    draft: PolicyDraft
    expected_revision: Revision


class SavePolicyRequest(PreviewPolicyRequest):
    review_id: ReviewIdentity
    request_id: RequestIdentity


class IdentifiedRequest(StorageRequest):
    request_id: RequestIdentity


class KeepLocalRequest(IdentifiedRequest):
    keep_local: bool = Field(strict=True)


class CleanupRequest(IdentifiedRequest):
    plan_id: ReviewIdentity


class EmptyRequest(StorageRequest):
    pass


def get_storage_service():
    # Laziness keeps route discovery separate from storage worker startup.
    from services.storage_policy_service import storage_policy_service

    return storage_policy_service


@asynccontextmanager
async def _mutation(*, configuration: bool = False):
    effects = {EffectClass.DB_WRITE}
    if configuration:
        effects.add(EffectClass.CONFIG_WRITE)
    try:
        async with runtime_work("storage.admin", effects):
            yield
    except AdmissionClosedError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "lifecycle_work_blocked",
            "message": "Storage changes are paused while maintenance is in progress.",
        }) from exc


async def _call(service, method: str, *args, **kwargs):
    from services.storage_policy_service import StoragePolicyError

    try:
        return await getattr(service, method)(*args, **kwargs)
    except StoragePolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={
            "code": exc.code, "message": exc.message,
        }) from exc


@router.get("/status")
async def storage_status(
    _: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    return await _call(service, "status")


@router.post("/refresh")
async def refresh_storage(
    body: IdentifiedRequest,
    session: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    async with _mutation():
        return await _call(service, "refresh", session.id, body.request_id)


@router.post("/policy/preview")
async def preview_policy(
    body: PreviewPolicyRequest,
    session: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    async with _mutation():
        return await _call(service, "preview", session.id,
                           body.draft.model_dump(), body.expected_revision)


@router.put("/policy")
async def save_policy(
    body: SavePolicyRequest,
    session: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    async with _mutation(configuration=True):
        return await _call(service, "save", session.id, body.review_id,
                           body.draft.model_dump(), body.expected_revision,
                           body.request_id)


@router.get("/operations/{operation_id}")
async def storage_operation(
    operation_id: str,
    session: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    if len(operation_id) > 160:
        raise HTTPException(status_code=404, detail={
            "code": "storage_operation_not_found", "message": "Storage operation was not found.",
        })
    return await _call(service, "operation", session.id, operation_id)


@router.get("/retention")
async def storage_retention(
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=25, ge=1, le=100),
    _: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    return await _call(service, "retention", cursor=cursor, limit=limit)


@router.put("/retention/{media_id}")
async def keep_local(
    media_id: str,
    body: KeepLocalRequest,
    session: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    if not media_id or len(media_id) > 160:
        raise HTTPException(status_code=404, detail={
            "code": "storage_media_not_found", "message": "The selected media was not found.",
        })
    async with _mutation():
        return await _call(service, "set_keep_local", session.id, media_id,
                           body.keep_local, body.request_id)


@router.get("/activity")
async def storage_activity(
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=25, ge=1, le=100),
    _: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    return await _call(service, "activity", cursor=cursor, limit=limit)


@router.post("/cleanup/preview")
async def preview_cleanup(
    body: EmptyRequest,
    session: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    del body
    async with _mutation():
        return await _call(service, "cleanup_preview", session.id)


@router.post("/cleanup")
async def execute_cleanup(
    body: CleanupRequest,
    session: AuthSession = Depends(require_recent_reauth),
    service=Depends(get_storage_service),
):
    async with _mutation():
        return await _call(service, "cleanup_execute", session.id,
                           body.plan_id, body.request_id)
