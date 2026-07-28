from fastapi import APIRouter, Depends, HTTPException, status

from models import AuthSession
from routes.auth import require_recent_reauth


router = APIRouter()


@router.get("/status")
async def get_update_status(session: AuthSession = Depends(require_recent_reauth)):
    """Reports the alpha contract: installations are upgraded explicitly by their operator."""
    del session
    return {
        "status": "unavailable",
        "update_available": False,
        "automatic_updates": False,
        "message": "In-app updates are disabled during alpha. Update from a reviewed release outside the running server.",
    }


@router.post("/trigger")
async def trigger_manual_update(session: AuthSession = Depends(require_recent_reauth)):
    """Prevents the running server from mutating or executing its own source tree."""
    del session
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "automatic_updates_unavailable",
            "message": "In-app updates are disabled during alpha. Apply a reviewed release outside the running server.",
        },
    )
