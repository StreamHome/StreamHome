import os
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status

from config import settings
from models import AuthSession
from routes.auth import require_recent_reauth
from services.logger import logger
from services.backup import (
    create_backup,
    prune_old_backups,
    get_local_backups,
    sync_backups_to_cloud,
    restore_backup,
    get_backup_dir
)

router = APIRouter()

@router.post("/run", status_code=status.HTTP_201_CREATED)
async def run_backup_endpoint(session: AuthSession = Depends(require_recent_reauth)):
    """Triggers a manual database backup and syncs it to the cloud if configured."""
    del session
    try:
        logger.info("[API] Manual backup triggered via API.")
        backup_path = await create_backup()
        prune_old_backups(keep_count=7)
        
        cloud_synced = False
        if settings.STORAGE_ENGINE == "CLOUD":
            cloud_synced = await sync_backups_to_cloud()
            
        return {
            "status": "success",
            "message": "Backup created successfully.",
            "backup_file": os.path.basename(backup_path),
            "cloud_synced": cloud_synced
        }
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
        return get_local_backups()
    except Exception as e:
        logger.error(f"[API] Error retrieving backup list: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list backups: {str(e)}"
        )

@router.post("/restore/{filename}")
async def restore_backup_endpoint(filename: str, session: AuthSession = Depends(require_recent_reauth)):
    """Restore the database from the specified backup file."""
    del session
    try:
        success = await restore_backup(filename)
        if success:
            return {
                "status": "success",
                "message": f"Database successfully restored from: {filename}"
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to restore database from backup: {filename}"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] Error restoring backup '{filename}': {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database restore failed: {str(e)}"
        )

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
        file_path.unlink()
        logger.info(f"[API] Backup file deleted: {filename}")
        return {
            "status": "success",
            "message": f"Backup file '{filename}' successfully deleted."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] Error deleting backup file '{filename}': {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete backup file: {str(e)}"
        )
