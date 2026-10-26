from __future__ import annotations

import hmac
import ipaddress
import re
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import APIModel, AuthSession, User
from routes.auth import add_event, get_current_session, get_current_user, require_recent_reauth
from lifecycle.coordination import EffectClass, runtime_work
from lifecycle.clock import server_clock_snapshot
from lifecycle.bootstrap import (
    StartupAuthorityChangedError,
    current_readiness_projection,
    get_startup_readiness,
    require_current_guarded_authority,
    require_startup_context,
)
from services import state
from services.update import (
    ACTIVE_PHASES,
    BUSY_PHASES,
    cancel_queued_update,
    check_for_update_details,
    current_commit,
    idle_blockers,
    install_lock_active,
    launch_queued_update_if_ready,
    maintenance_window_open,
    protected_cutover_blockers,
    prepare_update_cutover,
    queue_update,
    read_update_log,
    read_update_state,
    read_update_state_strict,
    update_lock_active,
    update_lifecycle_projection,
    write_authoritative_update_state,
    write_update_state,
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
    mode: Literal["when_idle", "now"] = "when_idle"


class BrowserPresenceRequest(BaseModel):
    visible: bool


class UpdateCommitRequest(BaseModel):
    transaction_id: str
    target_commit: str


class UpdateCutoverRequest(BaseModel):
    operation_id: str
    generation: int


class UpdatePolicyResponse(APIModel):
    automatic_updates: bool
    idle_minutes: int
    check_interval_hours: int
    maintenance_start: Optional[str] = None
    maintenance_end: Optional[str] = None
    branch: str
    require_signed_commits: bool


class ServerClockResponse(APIModel):
    time_zone: str
    utc_offset: str
    local_time: str


class UpdateStatusResponse(APIModel):
    phase: str
    message: str
    current_commit: str
    target_commit: str
    update_available: bool
    automatic: bool
    install_mode: Literal["automatic", "when_idle", "now"]
    queued_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    last_checked_at: Optional[float] = None
    last_success_at: Optional[float] = None
    updated_at: Optional[float] = None
    recovery_requested_at: Optional[float] = None
    transaction_id: str
    diagnostic_id: str
    failed_target: str
    error: str
    blockers: list[str]
    maintenance_window_open: bool
    server_clock: Optional[ServerClockResponse] = None
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
    persisted = update_lifecycle_projection(read_update_state())
    installed_commit = await current_commit() or str(persisted.get("current_commit") or "")
    install_mode = str(persisted.get("install_mode") or "when_idle")
    if install_mode not in {"automatic", "when_idle", "now"}:
        install_mode = "when_idle"
    active_immediate = install_mode == "now" and (
        persisted.get("phase") == "queued" or persisted.get("phase") in BUSY_PHASES
    )
    return UpdateStatusResponse(
        phase=str(persisted.get("phase") or "idle"),
        message=str(persisted.get("message") or ""),
        current_commit=installed_commit,
        target_commit=str(persisted.get("target_commit") or ""),
        update_available=bool(persisted.get("update_available")),
        automatic=bool(persisted.get("automatic")),
        install_mode=install_mode,
        queued_at=persisted.get("queued_at"),
        started_at=persisted.get("started_at"),
        finished_at=persisted.get("finished_at"),
        last_checked_at=persisted.get("last_checked_at"),
        last_success_at=persisted.get("last_success_at"),
        updated_at=persisted.get("updated_at"),
        recovery_requested_at=persisted.get("recovery_requested_at"),
        transaction_id=str(persisted.get("transaction_id") or ""),
        diagnostic_id=str(persisted.get("diagnostic_id") or ""),
        failed_target=str(persisted.get("failed_target") or ""),
        error=str(persisted.get("error") or ""),
        blockers=await protected_cutover_blockers() if active_immediate else await idle_blockers(),
        maintenance_window_open=maintenance_window_open(),
        server_clock=ServerClockResponse(**server_clock_snapshot()),
        update_in_progress=bool(
            persisted.get("phase") in ACTIVE_PHASES
            or update_lock_active()
            or install_lock_active()
            or persisted.get("lifecycle_status") == "active"
            or persisted.get("lifecycle_status") == "unavailable"
        ),
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
        "update_not_cancellable": "The update has already entered protected cutover and cannot be cancelled safely.",
        "dirty_worktree": "Local source changes must be committed or moved before updating.",
        "untrusted_origin": "The installation does not use the official StreamHome repository.",
        "invalid_install_mode": "Choose either immediate installation or installation when idle.",
        "update_script_unavailable": "The installation-root update.sh file is unavailable or could not be made executable.",
        "lifecycle_conflict": "Another installation lifecycle operation is already active.",
        "lifecycle_unavailable": "Shared lifecycle authority is unavailable and the update was blocked.",
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


@router.post("/commit", include_in_schema=False)
async def commit_updated_runtime(
    payload: UpdateCommitRequest,
    request: Request,
    token: str = Header(default="", alias="X-StreamHome-Update-Commit"),
):
    """Release transaction-scoped maintenance after exact-build verification."""
    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "commit_forbidden", "message": "Update commit is local only."}) from exc
    if (
        not peer.is_loopback
        or not state.UPDATE_COMMIT_TOKEN
        or not hmac.compare_digest(token, state.UPDATE_COMMIT_TOKEN)
        or not state.UPDATE_TRANSACTION_ID
        or not hmac.compare_digest(payload.transaction_id, state.UPDATE_TRANSACTION_ID)
    ):
        raise HTTPException(status_code=403, detail={"code": "commit_forbidden", "message": "Update commit authorization failed."})
    startup_context = require_startup_context()
    try:
        require_current_guarded_authority(startup_context)
    except StartupAuthorityChangedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": StartupAuthorityChangedError.code,
                "message": "The guarded lifecycle authority no longer matches this runtime.",
            },
        ) from exc
    readiness = current_readiness_projection(get_startup_readiness())
    if not readiness.get("guardedReady"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "guarded_recovery_incomplete",
                "message": "The guarded runtime has not completed candidate verification.",
            },
        )
    try:
        persisted = read_update_state_strict()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "update_journal_unavailable",
                "message": "The protected update journal is unavailable.",
            },
        ) from exc
    persisted_phase = str(persisted.get("phase") or "")
    expected_build = str(persisted.get("target_commit") or "")
    if persisted_phase in {"failed", "rolled_back"}:
        expected_build = str(persisted.get("current_commit") or "")
    if (
        str(persisted.get("transaction_id") or "") != payload.transaction_id
        or persisted.get("lifecycle_generation") != startup_context.generation
        or persisted.get("lifecycle_phase") != startup_context.lifecycle_phase
        or startup_context.operation_type != "update"
        or startup_context.owner_kind != "updater"
        or (
            startup_context.generation is not None
            and startup_context.lifecycle_phase != "verifying"
        )
        or expected_build != payload.target_commit
        or settings.BUILD_ID != payload.target_commit[:12]
    ):
        raise HTTPException(status_code=403, detail={"code": "commit_forbidden", "message": "Update commit authorization failed."})
    try:
        if startup_context.generation is None:
            # Positively confirmed pre-Phase-1 first adoption has no shared
            # authority. The strict generic writer still preserves the commit
            # boundary monotonically for rollback compatibility.
            write_update_state(runtime_committed=True, runtime_committed_at=time.time())
        elif persisted.get('managed_update_version') is not None:
            from lifecycle.managed_update import accept_verification
            accept_verification(startup_context.installation_root,
                operation_id=payload.transaction_id, generation=startup_context.generation,
                expected_detail_phase=persisted_phase)
        else:
            write_authoritative_update_state(
                operation_id=payload.transaction_id,
                generation=startup_context.generation,
                expected_lifecycle_phase="verifying",
                expected_detail_phases=frozenset({persisted_phase}),
                runtime_committed=True,
                runtime_committed_at=time.time(),
            )
    except Exception as exc:
        # Atomic replacement can become indeterminate after the rename.  Keep
        # the guarded process closed and force the controller to preserve the
        # candidate rather than racing it with rollback.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "update_commit_persistence_indeterminate",
                "message": "The protected update commit could not be confirmed durably.",
            },
        ) from exc
    state.UPDATE_COMMIT_TOKEN = ""
    state.UPDATE_TRANSACTION_ID = ""
    return {
        "committed": True,
        "buildId": settings.BUILD_ID,
        "recoveryPending": True,
    }


@router.post("/cutover", include_in_schema=False)
async def prepare_managed_update_cutover(
    payload: UpdateCutoverRequest,
    request: Request,
    token: str = Header(default="", alias="X-StreamHome-Update-Cutover"),
):
    """Close process-local admission for the exact detached update controller."""

    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "cutover_forbidden", "message": "Update cutover is local only."}) from exc
    if not peer.is_loopback:
        raise HTTPException(status_code=403, detail={"code": "cutover_forbidden", "message": "Update cutover is local only."})
    try:
        await prepare_update_cutover(
            operation_id=payload.operation_id,
            generation=payload.generation,
            cutover_token=token,
        )
    except RuntimeError as exc:
        code = str(exc)
        status_code = status.HTTP_409_CONFLICT if code == "cutover_busy" else status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": code,
                "message": (
                    "Lifecycle-relevant work is still active; cutover remains deferred."
                    if code == "cutover_busy"
                    else "Update cutover authority could not be established."
                ),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "cutover_forbidden", "message": "Update cutover authority could not be established."},
        ) from exc
    return {"accepted": True, "generation": payload.generation, "phase": "exclusive"}


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
    if existing.get("phase") in ACTIVE_PHASES or update_lock_active() or install_lock_active():
        raise _runtime_error(RuntimeError("update_in_progress"))
    try:
        result = await check_for_update_details()
    except RuntimeError as exc:
        raise _runtime_error(exc) from exc
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
            install_mode=payload.mode,
        )
    except RuntimeError as exc:
        raise _runtime_error(exc) from exc
    launched = False
    async with runtime_work("update.audit", [EffectClass.DB_WRITE]):
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
                "installMode": payload.mode,
            },
        )
        await db.commit()
    try:
        launched = await launch_queued_update_if_ready()
    except RuntimeError as exc:
        raise _runtime_error(exc) from exc
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
    async with runtime_work("update.audit", [EffectClass.DB_WRITE]):
        await add_event(db, request, "update_cancelled", "success", user.id, session.id)
        await db.commit()
    return await _status_response()
