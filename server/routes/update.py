from __future__ import annotations

import hmac
import ipaddress
import re
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import APIModel, AuthSession, User
from routes.auth import add_event, get_current_session, get_current_user, require_recent_reauth
from services import state
from services.update import (
    BUSY_PHASES,
    cancel_queued_update,
    check_for_update_details,
    current_commit,
    idle_blockers,
    launch_queued_update_if_ready,
    maintenance_window_open,
    queue_update,
    read_update_log,
    read_update_state,
    update_lock_active,
)


router = APIRouter()
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class UpdatePolicyRequest(BaseModel):
    automatic_updates: bool
    idle_minutes: int
    check_interval_hours: int
    maintenance_start: Optional[str] = None
    maintenance_end: Optional[str] = None


class UpdateInstallRequest(BaseModel):
    retry_failed_target: bool = False


class BrowserPresenceRequest(BaseModel):
    visible: bool


class UpdatePolicyResponse(APIModel):
    automatic_updates: bool
    idle_minutes: int
    check_interval_hours: int
    maintenance_start: Optional[str] = None
    maintenance_end: Optional[str] = None
    branch: str
    require_signed_commits: bool


class UpdateStatusResponse(APIModel):
    phase: str
    message: str
    current_commit: str
    target_commit: str
    update_available: bool
    automatic: bool
    queued_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    last_checked_at: Optional[float] = None
    last_success_at: Optional[float] = None
    failed_target: str
    error: str
    blockers: list[str]
    maintenance_window_open: bool
    update_in_progress: bool
    log_tail: list[str]
    policy: UpdatePolicyResponse


def _policy_response() -> UpdatePolicyResponse:
    return UpdatePolicyResponse(
        automatic_updates=settings.AUTO_UPDATE_ENABLED,
        idle_minutes=settings.UPDATE_IDLE_MINUTES,
        check_interval_hours=settings.UPDATE_CHECK_INTERVAL_HOURS,
        maintenance_start=settings.UPDATE_MAINTENANCE_START or None,
        maintenance_end=settings.UPDATE_MAINTENANCE_END or None,
        branch=settings.UPDATE_BRANCH,
        require_signed_commits=settings.UPDATE_REQUIRE_SIGNED_COMMITS,
    )


async def _status_response() -> UpdateStatusResponse:
    persisted = read_update_state()
    installed_commit = str(persisted.get("current_commit") or "") or await current_commit()
    return UpdateStatusResponse(
        phase=str(persisted.get("phase") or "idle"),
        message=str(persisted.get("message") or ""),
        current_commit=installed_commit,
        target_commit=str(persisted.get("target_commit") or ""),
        update_available=bool(persisted.get("update_available")),
        automatic=bool(persisted.get("automatic")),
        queued_at=persisted.get("queued_at"),
        started_at=persisted.get("started_at"),
        finished_at=persisted.get("finished_at"),
        last_checked_at=persisted.get("last_checked_at"),
        last_success_at=persisted.get("last_success_at"),
        failed_target=str(persisted.get("failed_target") or ""),
        error=str(persisted.get("error") or ""),
        blockers=await idle_blockers(),
        maintenance_window_open=maintenance_window_open(),
        update_in_progress=bool(persisted.get("phase") in BUSY_PHASES or update_lock_active()),
        log_tail=read_update_log(),
        policy=_policy_response(),
    )


def _runtime_error(exc: RuntimeError) -> HTTPException:
    code = str(exc)
    messages = {
        "update_in_progress": "An update is already in progress.",
        "no_update_available": "No update is currently available.",
        "failed_target_suppressed": "This target previously failed. Use the explicit retry action after reviewing its log.",
        "update_not_queued": "There is no pending update to cancel.",
        "dirty_worktree": "Local source changes must be committed or moved before updating.",
        "untrusted_origin": "The installation does not use the official StreamHome repository.",
    }
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": messages.get(code, "The update request could not be completed.")},
    )


@router.post("/presence", status_code=status.HTTP_204_NO_CONTENT)
async def update_browser_presence(
    payload: BrowserPresenceRequest,
    session: AuthSession = Depends(get_current_session),
):
    state.record_browser_presence(session.id, payload.visible)


@router.post("/handoff", include_in_schema=False)
async def approve_update_handoff(
    request: Request,
    token: str = Header(default="", alias="X-StreamHome-Update-Handoff"),
):
    """Allow only the detached localhost controller to reserve a verified-idle cutover."""
    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "handoff_forbidden", "message": "Update handoff is local only."}) from exc
    if not peer.is_loopback or not state.UPDATE_HANDOFF_TOKEN or not hmac.compare_digest(token, state.UPDATE_HANDOFF_TOKEN):
        raise HTTPException(status_code=403, detail={"code": "handoff_forbidden", "message": "Update handoff authorization failed."})
    blockers = await idle_blockers()
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={"code": "server_busy", "message": blockers[0], "blockers": blockers},
        )
    state.MAINTENANCE_MODE = True
    state.MAINTENANCE_REASON = "StreamHome is installing a validated update and will return automatically."
    state.UPDATE_HANDOFF_TOKEN = ""
    return {"approved": True}


@router.get("/status", response_model=UpdateStatusResponse)
async def get_update_status(session: AuthSession = Depends(require_recent_reauth)):
    del session
    return await _status_response()


@router.post("/check", response_model=UpdateStatusResponse)
async def check_for_updates(
    request: Request,
    user: User = Depends(get_current_user),
    session: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    existing = read_update_state()
    if existing.get("phase") == "queued" or existing.get("phase") in BUSY_PHASES or update_lock_active():
        raise _runtime_error(RuntimeError("update_in_progress"))
    result = await check_for_update_details()
    await add_event(
        db,
        request,
        "update_check",
        "success" if not result.get("error") else "failure",
        user.id,
        session.id,
        {"result": result.get("phase"), "target": str(result.get("target_commit") or "")[:12]},
    )
    await db.commit()
    return await _status_response()


@router.put("/policy", response_model=UpdateStatusResponse)
async def update_policy(
    payload: UpdatePolicyRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    if not 5 <= payload.idle_minutes <= 120:
        raise HTTPException(status_code=422, detail={"code": "invalid_idle_period", "message": "Idle time must be between 5 and 120 minutes."})
    if not 1 <= payload.check_interval_hours <= 24:
        raise HTTPException(status_code=422, detail={"code": "invalid_check_interval", "message": "Check interval must be between 1 and 24 hours."})
    start = (payload.maintenance_start or "").strip()
    end = (payload.maintenance_end or "").strip()
    if bool(start) != bool(end) or (start and (not TIME_RE.fullmatch(start) or not TIME_RE.fullmatch(end))):
        raise HTTPException(status_code=422, detail={"code": "invalid_maintenance_window", "message": "Provide both maintenance times in 24-hour HH:MM format, or leave both empty."})
    previous = (
        settings.AUTO_UPDATE_ENABLED,
        settings.UPDATE_IDLE_MINUTES,
        settings.UPDATE_CHECK_INTERVAL_HOURS,
        settings.UPDATE_MAINTENANCE_START,
        settings.UPDATE_MAINTENANCE_END,
    )
    settings.AUTO_UPDATE_ENABLED = payload.automatic_updates
    settings.UPDATE_IDLE_MINUTES = payload.idle_minutes
    settings.UPDATE_CHECK_INTERVAL_HOURS = payload.check_interval_hours
    settings.UPDATE_MAINTENANCE_START = start
    settings.UPDATE_MAINTENANCE_END = end
    try:
        settings.save_to_json()
    except OSError as exc:
        (
            settings.AUTO_UPDATE_ENABLED,
            settings.UPDATE_IDLE_MINUTES,
            settings.UPDATE_CHECK_INTERVAL_HOURS,
            settings.UPDATE_MAINTENANCE_START,
            settings.UPDATE_MAINTENANCE_END,
        ) = previous
        raise HTTPException(status_code=500, detail={"code": "settings_save_failed", "message": "Update settings could not be saved."}) from exc
    await add_event(
        db,
        request,
        "update_policy_changed",
        "success",
        user.id,
        session.id,
        {
            "automatic": payload.automatic_updates,
            "idleMinutes": payload.idle_minutes,
            "checkIntervalHours": payload.check_interval_hours,
            "maintenanceWindow": f"{start}-{end}" if start else "any",
        },
    )
    await db.commit()
    return await _status_response()


@router.post("/install", response_model=UpdateStatusResponse)
async def install_update(
    payload: UpdateInstallRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    try:
        queued = await queue_update(
            automatic=False,
            allow_failed_target=payload.retry_failed_target,
        )
        launched = await launch_queued_update_if_ready()
    except RuntimeError as exc:
        raise _runtime_error(exc) from exc
    await add_event(
        db,
        request,
        "update_queued",
        "success",
        user.id,
        session.id,
        {
            "target": str(queued.get("target_commit") or "")[:12],
            "launched": launched,
            "retryFailedTarget": payload.retry_failed_target,
        },
    )
    await db.commit()
    return await _status_response()


@router.delete("/pending", response_model=UpdateStatusResponse)
async def cancel_pending_update(
    request: Request,
    user: User = Depends(get_current_user),
    session: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    try:
        await cancel_queued_update()
    except RuntimeError as exc:
        raise _runtime_error(exc) from exc
    await add_event(db, request, "update_cancelled", "success", user.id, session.id)
    await db.commit()
    return await _status_response()
