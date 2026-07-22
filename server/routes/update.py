import asyncio
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks

from models import AuthSession
from routes.auth import require_recent_reauth
from services.logger import logger
from services.update import (
    check_for_github_updates,
    is_git_clean,
    get_active_branch,
    pull_and_install_updates,
    self_restart_server,
    is_system_idle
)

router = APIRouter()
@router.get("/status")
async def get_update_status(session: AuthSession = Depends(require_recent_reauth)):
    """Check target GitHub repository to identify if updates are available."""
    del session
    try:
        update_available = await check_for_github_updates()
        git_clean = await is_git_clean()
        active_branch = await get_active_branch()
        system_idle = await is_system_idle()
        
        return {
            "status": "success",
            "update_available": update_available,
            "git_clean": git_clean,
            "active_branch": active_branch,
            "system_idle": system_idle
        }
    except Exception as e:
        logger.error(f"[API] Error checking update status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Update check failed: {str(e)}"
        )

@router.post("/trigger")
async def trigger_manual_update(background_tasks: BackgroundTasks, session: AuthSession = Depends(require_recent_reauth)):
    """Manually triggers git pull and dependency updates. Restarts the server asynchronously."""
    del session
    # 1. Verify working directory is clean
    if not await is_git_clean():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Local uncommitted changes detected. Aborting update to prevent loss of files."
        )
        
    try:
        # Check if updates exist
        update_available = await check_for_github_updates()
        if not update_available:
            return {
                "status": "success",
                "message": "StreamHome is already up-to-date. No pull performed."
            }
            
        success = await pull_and_install_updates()
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Git pull or dependency installation failed."
            )
            
        # Schedule self-restart with a tiny delay to allow returning HTTP response to the client
        async def delayed_restart():
            await asyncio.sleep(1.0)
            self_restart_server()
            
        background_tasks.add_task(delayed_restart)
        
        return {
            "status": "success",
            "message": "Update pulled and applied successfully. Server is restarting..."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] Error running manual update trigger: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Update failed: {str(e)}"
        )
