import os
import sqlite3
import shutil
import asyncio
import time
import re
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import engine
from models import PlaybackSession
from services.logger import logger
from services.queue import queue_manager
from services.rclone import rclone_service

BACKUP_FILENAME_RE = re.compile(r"^backup_\d{8}_\d{6}\.db$")

def get_backup_dir() -> str:
    """Resolve absolute path to server/backup folder."""
    config_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backup_path = os.path.join(config_dir, "backup")
    os.makedirs(backup_path, exist_ok=True)
    return backup_path


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
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise ValueError("Backup does not contain a SQLite database.")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("Backup database integrity validation failed.")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"user", "movie", "authsession"}
        if not required.issubset(tables):
            raise ValueError("Backup database schema is not compatible with StreamHome.")
    finally:
        connection.close()

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
    backup_dir = get_backup_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"backup_{timestamp}.db"
    dest_path = os.path.join(backup_dir, backup_filename)

    active_db_path = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database.db"))

    logger.info(f"[Backup Service] Starting secure online backup: {active_db_path} -> {dest_path}")

    # Use run_in_executor to avoid blocking the asyncio event loop during backup I/O
    def run_backup():
        src_conn = sqlite3.connect(active_db_path)
        dest_conn = sqlite3.connect(dest_path)
        try:
            with dest_conn:
                src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            src_conn.close()

    await asyncio.get_running_loop().run_in_executor(None, run_backup)
    logger.info(f"[Backup Service] Backup successfully created: {backup_filename}")
    return dest_path

def prune_old_backups(keep_count: int = 7):
    """Keep only the last keep_count backup files locally."""
    try:
        backup_dir = get_backup_dir()
        files = [
            os.path.join(backup_dir, f)
            for f in os.listdir(backup_dir)
            if f.startswith("backup_") and f.endswith(".db")
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
        
        files = [f for f in os.listdir(backup_dir) if f.startswith("backup_") and f.endswith(".db")]
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
    backup_dir = get_backup_dir()
    target_remote = f"{settings.RCLONE_REMOTE_PATH}/backup"

    logger.info(f"[Backup Service] Syncing backup folder with cloud: {backup_dir} -> {target_remote}")
    result = await rclone_service.run("sync", backup_dir, target_remote, "--retries", "3", timeout=60 * 60)
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

async def restore_backup(filename: str) -> bool:
    """
    Safely restore a database backup file to database.db.
    Disposes active sessions/connections to prevent locking.
    """
    backup_file_path = resolve_backup_file(filename, must_exist=True)
    active_db_path = Path(settings.db_path).resolve()

    if not backup_file_path:
        logger.error(f"[Backup Service] Restore failed: Backup file '{filename}' does not exist.")
        return False

    temporary_path: Path | None = None
    try:
        validate_backup_database(backup_file_path)
        logger.warning(f"[Backup Service] Restoring database to: {filename}")

        # Build and validate the replacement before touching the live database.
        with tempfile.NamedTemporaryFile(
            dir=active_db_path.parent,
            prefix=".database-restore-",
            suffix=".db",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        shutil.copy2(backup_file_path, temporary_path)
        validate_backup_database(temporary_path)

        # Preserve a rollback point using SQLite's online backup API.
        rollback_path = Path(get_backup_dir()) / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        source = sqlite3.connect(active_db_path)
        rollback = sqlite3.connect(rollback_path)
        try:
            source.backup(rollback)
        finally:
            rollback.close()
            source.close()

        await engine.dispose()
        for suffix in ("-wal", "-shm"):
            Path(f"{active_db_path}{suffix}").unlink(missing_ok=True)
        os.replace(temporary_path, active_db_path)
        temporary_path = None
        logger.info("[Backup Service] Database successfully restored.")
        return True
    except Exception as e:
        logger.error(f"[Backup Service] Restore error: {e}")
        return False
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
