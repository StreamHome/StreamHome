import os
import asyncio
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from lifecycle.response_gate import ControllerResponse

from config import settings
from models import AuthSession
from routes.auth import require_recent_reauth
from services.logger import logger
from services.backup import (
    create_backup,
    prune_old_backups,
    get_local_backups_async,
    sync_backups_to_cloud,
    delete_backup,
)
from lifecycle import state as lifecycle_state
from lifecycle.coordination import (
    AdmissionClosedError,
    CoordinationError,
    DrainSnapshot,
    EffectClass,
    get_runtime_coordinator,
    runtime_work,
)
from lifecycle.restore_controller import (
    RestoreError,
    RestoreValidationError,
    captured_work_has_effect,
    create_restore_journal,
    prevalidate_candidate,
    public_restore_status,
    spawn_restore_controller,
    update_restore_journal,
)
import services.state as service_state

router = APIRouter()


def _retain_failed_restore_claim(operation_id: str | None) -> None:
    """Move an adopted claim to recovery_required without guessing ownership."""

    if operation_id is None:
        return
    try:
        current = lifecycle_state.inspect_state(settings.BASE_DIR)
        active = current.active_operation
        if (
            active
            and active.operation_id == operation_id
            and active.phase is not lifecycle_state.LifecyclePhase.RECOVERY_REQUIRED
        ):
            lifecycle_state.transition_operation_with_credential(
                settings.BASE_DIR,
                operation_id=operation_id,
                generation=current.generation,
                expected_phase=active.phase,
                next_phase=lifecycle_state.LifecyclePhase.RECOVERY_REQUIRED,
            )
    except lifecycle_state.LifecycleError:
        pass


def _safe_unwind_preacceptance(
    operation_id: str,
    generation: int,
    snapshot: DrainSnapshot,
) -> bool:
    """Release an exact reservation/fence before any controller handoff exists."""

    try:
        get_runtime_coordinator().release_fence(snapshot)
    except (CoordinationError, ValueError):
        return False
    try:
        lifecycle_state.complete_operation_with_credential(
            settings.BASE_DIR,
            operation_id=operation_id,
            generation=generation,
            expected_phase=lifecycle_state.LifecyclePhase.RESERVED,
        )
    except (lifecycle_state.LifecycleError, ValueError):
        _retain_failed_restore_claim(operation_id)
        return False
    if service_state.MAINTENANCE_REASON == "StreamHome is preparing an offline database restore.":
        service_state.MAINTENANCE_MODE = False
        service_state.MAINTENANCE_REASON = ""
    return True

@router.post("/run", status_code=status.HTTP_201_CREATED)
async def run_backup_endpoint(session: AuthSession = Depends(require_recent_reauth)):
    """Triggers a manual database backup and syncs it to the cloud if configured."""
    del session
    try:
        logger.info("[API] Manual backup triggered via API.")
        async with runtime_work("backup.run", [EffectClass.DATABASE_SNAPSHOT]):
            backup_path = await create_backup()
            await prune_old_backups(keep_count=7)

            cloud_synced = False
            if settings.STORAGE_ENGINE == "CLOUD":
                cloud_synced = await sync_backups_to_cloud()
            
        cloud_required = settings.STORAGE_ENGINE == "CLOUD"
        return {
            "status": "success" if not cloud_required or cloud_synced else "partial",
            "message": (
                "Backup created and synchronized successfully."
                if cloud_synced
                else "Local backup created, but cloud synchronization failed."
                if cloud_required
                else "Local backup created successfully."
            ),
            "backup_file": os.path.basename(backup_path),
            "cloud_synced": cloud_synced
        }
    except AdmissionClosedError:
        raise
    except Exception as e:
        logger.error(f"[API] Error running manual backup: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backup execution failed: {str(e)}"
        )

@router.get("/list", response_model=List[Dict[str, Any]])
async def list_backups_endpoint(session: AuthSession = Depends(require_recent_reauth)):
    """Retrieve metadata of all local backup files."""
    del session
    try:
        return await get_local_backups_async()
    except Exception as e:
        logger.error(f"[API] Error retrieving backup list: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list backups: {str(e)}"
        )

@router.post("/restore/{filename}", status_code=status.HTTP_202_ACCEPTED)
async def restore_backup_endpoint(filename: str, session: AuthSession = Depends(require_recent_reauth)):
    """Accept an offline restore only after detached controller ownership exists."""
    del session
    coordinator = get_runtime_coordinator()
    operation_id: str | None = None
    generation: int | None = None
    snapshot: DrainSnapshot | None = None
    handoff_started = False
    try:
        candidate = await asyncio.to_thread(prevalidate_candidate, settings.BASE_DIR, filename)
        if any(
            EffectClass.DATABASE_SNAPSHOT.value in item.get("effects", ())
            for item in coordinator.active_work()
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "backup_snapshot_active",
                    "message": "Wait for the active database backup to finish before restoring.",
                },
            )
        claimed = lifecycle_state.claim_operation_with_credential(
            settings.BASE_DIR,
            operation_type=lifecycle_state.OperationType.RESTORE,
            owner_kind=lifecycle_state.OwnerKind.RESTORE_CONTROLLER,
        )
        operation = claimed.state.active_operation
        if operation is None:
            raise RuntimeError("restore claim did not create an active operation")
        operation_id = operation.operation_id
        generation = claimed.state.generation
        service_state.MAINTENANCE_MODE = True
        service_state.MAINTENANCE_REASON = "StreamHome is preparing an offline database restore."
        snapshot = coordinator.close_admission(
            list(EffectClass),
            operation_id=operation_id,
            generation=generation,
        )
        if captured_work_has_effect(
            snapshot.lease_ids,
            coordinator.active_work(),
            EffectClass.DATABASE_SNAPSHOT.value,
        ):
            if not _safe_unwind_preacceptance(operation_id, generation, snapshot):
                _retain_failed_restore_claim(operation_id)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "restore_unwind_failed", "message": "Restore remains safely blocked."},
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "backup_snapshot_active",
                    "message": "Wait for the active database backup to finish before restoring.",
                },
            )
        create_restore_journal(
            settings.BASE_DIR,
            operation_id=operation_id,
            generation=generation,
            candidate=candidate,
        )
        coordinator.request_cancellation(snapshot)
        drained = await coordinator.drain(snapshot, timeout=10.0)
        update_restore_journal(
            settings.BASE_DIR,
            operation_id,
            progress="drained",
            drain_timed_out=drained.timed_out,
        )
        handoff_started = True
        handoff = await asyncio.to_thread(
            spawn_restore_controller,
            settings.BASE_DIR,
            operation_id=operation_id,
            generation=generation,
            owner_capability=claimed.owner_capability,
        )
        return ControllerResponse(
            handoff=handoff,
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "accepted",
                "operation_id": operation_id,
                "generation": handoff.generation,
                "phase": "controller_owned",
                "status_url": "/__streamhome/restore-status",
                "restart_required": False,
            },
        )
    except HTTPException:
        raise
    except lifecycle_state.LifecycleConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "lifecycle_conflict",
                "message": "Another lifecycle operation must finish or be explicitly resolved first.",
            },
        ) from exc
    except RestoreValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": "The selected backup is not a valid restore candidate."},
        ) from exc
    except RestoreError as exc:
        if (
            not handoff_started
            and operation_id is not None
            and generation is not None
            and snapshot is not None
        ):
            if not _safe_unwind_preacceptance(operation_id, generation, snapshot):
                _retain_failed_restore_claim(operation_id)
        else:
            _retain_failed_restore_claim(operation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": exc.code,
                "message": "The restore could not establish protected controller ownership.",
            },
        ) from exc
    except Exception as exc:
        if (
            not handoff_started
            and operation_id is not None
            and generation is not None
            and snapshot is not None
        ):
            if not _safe_unwind_preacceptance(operation_id, generation, snapshot):
                _retain_failed_restore_claim(operation_id)
        else:
            _retain_failed_restore_claim(operation_id)
        logger.error("[API] Restore acceptance failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "restore_acceptance_failed", "message": "Database restore acceptance failed."},
        ) from exc


@router.get("/restore/status/{operation_id}")
async def restore_status_endpoint(
    operation_id: str,
    session: AuthSession = Depends(require_recent_reauth),
):
    del session
    try:
        return public_restore_status(settings.BASE_DIR, operation_id)
    except (RestoreError, lifecycle_state.LifecycleError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "restore_status_unavailable", "message": "Restore status is unavailable."},
        ) from exc

@router.delete("/{filename}")
async def delete_backup_endpoint(filename: str, session: AuthSession = Depends(require_recent_reauth)):
    """Delete a specific local backup file."""
    del session
    try:
        from services.backup import resolve_backup_file
        file_path = resolve_backup_file(filename, must_exist=True)
        if not file_path:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Backup file '{filename}' not found."
            )
        async with runtime_work("backup.delete", [EffectClass.DATABASE_SNAPSHOT]):
            await delete_backup(file_path)
        logger.info(f"[API] Backup file deleted: {filename}")
        return {
            "status": "success",
            "message": f"Backup file '{filename}' successfully deleted."
        }
    except HTTPException:
        raise
    except AdmissionClosedError:
        raise
    except Exception as e:
        logger.error(f"[API] Error deleting backup file '{filename}': {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete backup file: {str(e)}"
        )
