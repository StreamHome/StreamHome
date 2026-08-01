from __future__ import annotations

from services.logger import logger
from services.rclone import rclone_service


ACTIVE_CLOUD_DOWNLOADS: set[str] = set()


async def download_file_from_cloud_task(target_remote: str, absolute_path: str) -> None:
    """Populate the local cache for a public presentation asset from application-owned cloud storage."""

    try:
        if not rclone_service.executable():
            logger.error("[Cloud Cache] Rclone is unavailable; the presentation asset cannot be cached.")
            return
        result = await rclone_service.copyto_atomic(target_remote, absolute_path)
        if result.ok:
            logger.info("[Cloud Cache] Presentation asset cached successfully.")
        else:
            logger.error(f"[Cloud Cache] Rclone copy failed: {result.error_code or 'rclone_failed'}")
    except Exception as exc:
        logger.error(f"[Cloud Cache] Presentation asset copy failed: {type(exc).__name__}")
    finally:
        ACTIVE_CLOUD_DOWNLOADS.discard(absolute_path)
