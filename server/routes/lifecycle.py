"""Narrow protected lifecycle surfaces used by offline controllers."""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from config import settings
from lifecycle import state as lifecycle_state
from lifecycle.bootstrap import (
    StartupAuthorityChangedError,
    StartupMode,
    current_readiness_projection,
    get_startup_readiness,
    require_current_guarded_authority,
    require_startup_context,
)
from lifecycle.restore_controller import RestoreConflictError, RestoreError, accept_guarded_runtime, read_restore_journal
import services.state as service_state


router = APIRouter()


class RestoreCommitRequest(BaseModel):
    operation_id: str
    generation: int
    build_id: str
    decision: str


@router.post("/restore/commit", include_in_schema=False)
async def commit_restored_runtime(
    payload: RestoreCommitRequest,
    request: Request,
    capability: str = Header(default="", alias="X-StreamHome-Restore-Commit"),
):
    """Cross the restore no-rollback boundary for one exact guarded runtime."""

    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "restore_commit_forbidden"}) from exc
    context = require_startup_context()
    if (
        not peer.is_loopback
        or context.mode is not StartupMode.RESTORE_VERIFY
        or payload.operation_id != context.operation_id
        or payload.generation != context.generation
        or payload.build_id != settings.BUILD_ID
        or payload.decision not in {"accept", "rollback"}
    ):
        raise HTTPException(status_code=403, detail={"code": "restore_commit_forbidden"})
    try:
        require_current_guarded_authority(context)
        expected_phase = lifecycle_state.LifecyclePhase.VERIFYING
        current = lifecycle_state.validate_operation_owner(
            context.installation_root,
            operation_id=payload.operation_id,
            generation=payload.generation,
            expected_phase=expected_phase,
            owner_capability=capability,
        )
        operation = current.active_operation
        if (
            operation is None
            or operation.operation_type is not lifecycle_state.OperationType.RESTORE
            or operation.owner_kind is not lifecycle_state.OwnerKind.RESTORE_CONTROLLER
        ):
            raise lifecycle_state.OwnershipError("restore authority does not match")
        startup_readiness = get_startup_readiness()
        readiness = current_readiness_projection(startup_readiness)
        if not readiness["guardedReady"]:
            # Normal recovery intentionally clears guardedReady before the
            # controller completes. Only an already accepted decision may retry
            # through that private completion window; public readiness stays shut.
            journal = read_restore_journal(context.installation_root, payload.operation_id)
            accepted_retry = (
                startup_readiness.projection()["generalReady"]
                and journal["accepted"]
                and journal["generation"] == payload.generation
            )
            if not accepted_retry:
                raise StartupAuthorityChangedError("guarded restore readiness is unavailable")
    except (StartupAuthorityChangedError, lifecycle_state.LifecycleError, RestoreError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "restore_authority_changed",
                "message": "The guarded restore authority no longer matches this runtime.",
            },
        ) from exc
    try:
        accept_guarded_runtime(
            context.installation_root,
            payload.operation_id,
            generation=payload.generation,
            decision=payload.decision,
        )
    except RestoreConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "restore_commit_conflict", "message": "The restore acceptance does not match the recorded decision or recovery phase."}) from exc
    except (RestoreError, lifecycle_state.LifecycleError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "restore_commit_persistence_failed",
                "message": "The restore acceptance boundary could not be persisted.",
            },
        ) from exc
    service_state.RESTORE_COMMIT_DECISION = payload.decision
    return {
        "committed": True,
        "buildId": settings.BUILD_ID,
        "decision": payload.decision,
        "recoveryPending": True,
    }
