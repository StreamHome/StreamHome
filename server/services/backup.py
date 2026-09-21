import os
import sqlite3
import asyncio
import time
import re
from pathlib import Path
from datetime import datetime, timedelta
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import engine
from models import PlaybackRun, PlaybackSession
from services.logger import logger
from services.queue import queue_manager
from services.rclone import rclone_service
from lifecycle.coordination import (
    AdmissionClosedError,
    EffectClass,
    run_cancellation_safe_thread,
    runtime_work,
)
from lifecycle.restore_controller import RestoreValidationError, validate_sqlite_database

BACKUP_FILENAME_RE = re.compile(r"^backup_\d{8}_\d{6}\.db$")
BACKUP_LOCK = asyncio.Lock()

def get_backup_dir() -> str:
    """Resolve absolute path to server/backup folder."""
    config_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(config_dir, "backup")


def resolve_backup_file(filename: str, *, must_exist: bool = False) -> Path | None:
    """Resolve only application-generated backup basenames inside the backup root."""
    if not isinstance(filename, str) or not BACKUP_FILENAME_RE.fullmatch(filename):
        return None
    root = Path(get_backup_dir()).resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_symlink() or (must_exist and not candidate.is_file()):
        return None
    return candidate


def validate_backup_database(path: Path) -> None:
    try:
        validate_sqlite_database(path)
    except RestoreValidationError as exc:
        raise ValueError("Backup database validation failed.") from exc

async def is_database_idle() -> bool:
    """
    Checks if the database is currently not in use.
    Returns True if:
      1. There are no active downloads or processing tasks.
      2. No playback sessions have been active/updating in the last 5 minutes.
    """
    # 1. Check queue manager tasks
    if len(queue_manager.active_tasks) > 0:
        logger.info("[Backup Service] Database is busy: Queue Manager has active tasks.")
        return False

    # 2. Check active playbacks in the last 5 minutes (UTC)
    try:
        async with AsyncSession(engine) as session:
            five_mins_ago = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
            statement = select(PlaybackSession).where(
                PlaybackSession.is_finished == False,
                PlaybackSession.updated_at >= five_mins_ago
            )
            result = await session.exec(statement)
            active_sessions = result.all()
            if len(active_sessions) > 0:
                logger.info(f"[Backup Service] Database is busy: {len(active_sessions)} active playback session(s) detected.")
                return False
            active_runs = (
                await session.exec(
                    select(PlaybackRun).where(
                        PlaybackRun.lifecycle_state == "active",
                        PlaybackRun.last_seen_at >= time.time() - 5 * 60,
                    )
                )
            ).all()
            if active_runs:
                logger.info(f"[Backup Service] Database is busy: {len(active_runs)} active playback run(s) detected.")
                return False
    except Exception as e:
        logger.error(f"[Backup Service] Error checking active playback sessions: {e}")
        # Default to False on database read error during active checks to be safe
        return False

    return True

async def create_backup() -> str:
    """
    Perform a secure online backup of database.db using SQLite Backup API.
    Creates backup in server/backup/backup_YYYYMMDD_HHMMSS.db.
    """
    async with runtime_work("backup.snapshot", [EffectClass.DATABASE_SNAPSHOT]):
        return await _create_admitted_backup()


async def _create_admitted_backup() -> str:
    async with BACKUP_LOCK:
        backup_dir = get_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        candidate_time = datetime.now()
        while True:
            backup_filename = f"backup_{candidate_time.strftime('%Y%m%d_%H%M%S')}.db"
            dest_path = os.path.join(backup_dir, backup_filename)
            if not os.path.exists(dest_path):
                break
            candidate_time += timedelta(seconds=1)

        active_db_path = str(Path(settings.db_path).resolve())
        logger.info(f"[Backup Service] Starting secure online backup: {active_db_path} -> {dest_path}")

        def run_backup():
            src_conn = sqlite3.connect(active_db_path)
            dest_conn = sqlite3.connect(dest_path)
            try:
                with dest_conn:
                    src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
                src_conn.close()

        await run_cancellation_safe_thread(run_backup)
        validate_backup_database(Path(dest_path))
        logger.info(f"[Backup Service] Backup successfully created: {backup_filename}")
        return dest_path

def prune_old_backups(keep_count: int = 7):
    """Keep only the last keep_count backup files locally."""
    try:
        backup_dir = get_backup_dir()
        files = [
            os.path.join(backup_dir, f)
            for f in os.listdir(backup_dir)
            if BACKUP_FILENAME_RE.fullmatch(f)
        ]
        # Sort files by creation time
        files.sort(key=os.path.getmtime)
        
        if len(files) > keep_count:
            files_to_delete = files[:-keep_count]
            for file_path in files_to_delete:
                os.remove(file_path)
                logger.info(f"[Backup Service] Pruned old backup file: {os.path.basename(file_path)}")
    except Exception as e:
        logger.error(f"[Backup Service] Error pruning old backups: {e}")

def get_local_backups() -> list:
    """Return a list of metadata for all local backup files."""
    backup_list = []
    try:
        backup_dir = get_backup_dir()
        if not os.path.exists(backup_dir):
            return []
        
        files = [f for f in os.listdir(backup_dir) if BACKUP_FILENAME_RE.fullmatch(f)]
        for f in files:
            file_path = os.path.join(backup_dir, f)
            mtime = os.path.getmtime(file_path)
            size = os.path.getsize(file_path)
            backup_list.append({
                "filename": f,
                "size_bytes": size,
                "formatted_size": f"{size / (1024 * 1024):.2f} MB",
                "timestamp": datetime.fromtimestamp(mtime).isoformat(),
            })
        # Sort newest first
        backup_list.sort(key=lambda x: x["timestamp"], reverse=True)
    except Exception as e:
        logger.error(f"[Backup Service] Error listing backups: {e}")
    return backup_list

async def sync_backups_to_cloud() -> bool:
    """Sync the local backup directory to the cloud backup location using Rclone."""
    if not rclone_service.cloud_write_available():
        health = rclone_service.cloud_health()
        logger.error(
            "[Backup Service] Cloud synchronization skipped because the storage circuit is open (%s).",
            health.get("errorCode") or health.get("state"),
        )
        return False
    backup_dir = get_backup_dir()
    target_remote = f"{settings.RCLONE_REMOTE_PATH}/backup"

    logger.info(f"[Backup Service] Syncing backup folder with cloud: {backup_dir} -> {target_remote}")
    try:
        result = await rclone_service.run("sync", backup_dir, target_remote, "--retries", "3", timeout=60 * 60)
    except AdmissionClosedError:
        logger.info("[Backup Service] Cloud synchronization deferred by runtime admission.")
        return False
    if result.ok:
        logger.info("[Backup Service] Cloud synchronization complete.")
        return True
    logger.error(
        "[Backup Service] Rclone sync failed (%s): %s",
        result.error_code or "rclone_failed",
        result.stderr.strip(),
    )
    if result.error_code == "rclone_unavailable":
        logger.error("[Backup Service] Rclone binary not found. Cannot sync to cloud.")
    return False
