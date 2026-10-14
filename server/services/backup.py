import os
import sqlite3
import asyncio
import shutil
import stat
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
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
from lifecycle import backup_manifest
from lifecycle import state as lifecycle_state
from lifecycle.restore_controller import RestoreValidationError, validate_sqlite_database

BACKUP_FILENAME_RE = backup_manifest.LEGACY_BACKUP_RE
BACKUP_LOCK = asyncio.Lock()

def get_backup_dir() -> str:
    """Resolve absolute path to server/backup folder."""
    config_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(config_dir, "backup")


def resolve_backup_file(filename: str, *, must_exist: bool = False) -> Path | None:
    """Resolve only application-generated backup basenames inside the backup root."""
    if not isinstance(filename, str) or not backup_manifest.is_supported_backup_name(filename):
        return None
    root = Path(get_backup_dir()).resolve()
    candidate = root / filename
    try:
        file_state = candidate.lstat()
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return None
    if candidate.is_symlink() or not stat.S_ISREG(file_state.st_mode):
        return None
    if must_exist and not candidate.is_file():
        return None
    return candidate


def validate_backup_database(path: Path) -> None:
    try:
        validate_sqlite_database(path, standalone=True)
    except RestoreValidationError as exc:
        raise ValueError("Backup database validation failed.") from exc


def eligible_backup(path: Path) -> backup_manifest.BackupEvidence:
    """Require both exact pair identity and a usable standalone SQLite database."""
    evidence = backup_manifest.validate_backup_pair(path)
    validate_backup_database(path)
    if backup_manifest.validate_backup_pair(path) != evidence:
        raise ValueError("Backup identity changed during validation.")
    return evidence


def backup_due(backups: list[dict], *, now: datetime | None = None) -> bool:
    valid = [item for item in backups if item.get("valid") is True]
    if not valid:
        return True
    newest = max(datetime.fromisoformat(item["timestamp"]) for item in valid)
    return ((now or datetime.now()) - newest).total_seconds() >= 24 * 60 * 60

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
    backup_dir = Path(get_backup_dir())

    def prepare_backup_directory() -> None:
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        lifecycle_state.qualify_local_filesystem(
            backup_dir,
            require_private=True,
            description="backup directory",
        )

    await run_cancellation_safe_thread(prepare_backup_directory)
    async with BACKUP_LOCK:
        candidate_time = datetime.now()
        while True:
            backup_filename = f"backup_v1_{candidate_time.strftime('%Y%m%d_%H%M%S')}.db"
            dest_path = backup_dir / backup_filename
            if not dest_path.exists() and not dest_path.is_symlink():
                break
            candidate_time += timedelta(seconds=1)

        active_db_path = str(Path(settings.db_path).resolve())
        logger.info(f"[Backup Service] Starting secure online backup: {active_db_path} -> {dest_path}")
        def run_backup() -> None:
            database_created = False
            manifest_created = False
            try:
                descriptor = os.open(
                    dest_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                database_created = True
                os.close(descriptor)
                src_conn = sqlite3.connect(active_db_path)
                dest_conn = sqlite3.connect(str(dest_path))
                try:
                    with dest_conn:
                        src_conn.backup(dest_conn)
                finally:
                    dest_conn.close()
                    src_conn.close()
                os.chmod(dest_path, 0o600)
                descriptor = os.open(
                    dest_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                validate_backup_database(dest_path)
                backup_manifest.create_manifest(
                    dest_path,
                    application_build_id=settings.BUILD_ID,
                    created_at=datetime.now(timezone.utc),
                )
                manifest_created = True
            except BaseException:
                manifest_path = backup_manifest.manifest_path_for(dest_path)
                created_paths = []
                if database_created:
                    created_paths.append(dest_path)
                if manifest_created or manifest_path.exists() or manifest_path.is_symlink():
                    created_paths.append(manifest_path)
                for path in created_paths:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                directory_descriptor = os.open(
                    backup_dir,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
                raise

        await run_cancellation_safe_thread(run_backup)
        logger.info(f"[Backup Service] Backup successfully created: {backup_filename}")
        return str(dest_path)


def delete_backup_pair(path: Path) -> None:
    """Delete a logical backup without ever leaving a new DB downgraded to legacy."""
    try:
        lifecycle_state.qualify_local_filesystem(
            path.parent,
            require_private=False,
            description="backup directory",
        )
        backup_manifest.delete_backup_pair(path)
    except backup_manifest.BackupManifestError as exc:
        raise ValueError("Backup pair deletion failed.") from exc


async def delete_backup(path: Path) -> None:
    async with BACKUP_LOCK:
        await run_cancellation_safe_thread(delete_backup_pair, path)


def _prune_old_backups(keep_count: int = 7) -> None:
    """Retain invalid evidence and apply the quota only to usable backup pairs."""
    if type(keep_count) is not int or keep_count < 1:
        raise ValueError("Backup retention count must be positive.")
    try:
        valid: list[tuple[Path, backup_manifest.BackupEvidence]] = []
        for path in Path(get_backup_dir()).iterdir():
            if not backup_manifest.is_supported_backup_name(path.name):
                continue
            try:
                valid.append((path, eligible_backup(path)))
            except (backup_manifest.BackupManifestError, ValueError, OSError):
                continue
        valid.sort(key=lambda item: item[0].stat().st_mtime_ns)
        targets = valid[:-keep_count]
        # Validate the entire selection before the first deletion, then check each
        # exact pair again immediately before deleting it.
        if any(eligible_backup(path) != evidence for path, evidence in targets):
            raise ValueError("Backup retention evidence changed.")
        for path, evidence in targets:
            if eligible_backup(path) != evidence:
                raise ValueError("Backup retention evidence changed.")
            delete_backup_pair(path)
            logger.info("[Backup Service] Pruned old backup file: %s", path.name)
    except Exception as e:
        logger.error(f"[Backup Service] Error pruning old backups: {e}")


async def prune_old_backups(keep_count: int = 7) -> None:
    async with BACKUP_LOCK:
        await run_cancellation_safe_thread(_prune_old_backups, keep_count)

def get_local_backups() -> list:
    """Return a list of metadata for all local backup files."""
    backup_list = []
    try:
        backup_dir = get_backup_dir()
        if not os.path.exists(backup_dir):
            return []
        
        files = [f for f in os.listdir(backup_dir) if backup_manifest.is_supported_backup_name(f)]
        for f in files:
            file_path = Path(backup_dir) / f
            try:
                file_state = file_path.lstat()
            except OSError:
                continue
            mtime = file_state.st_mtime
            size = file_state.st_size if stat.S_ISREG(file_state.st_mode) else 0
            try:
                evidence = eligible_backup(file_path)
                backup_format = evidence.format
                manifest_status = "valid" if evidence.manifest else "legacy"
                valid = True
            except (backup_manifest.BackupManifestError, ValueError, OSError):
                backup_format = "manifest_v1" if backup_manifest.is_manifest_backup_name(f) else "legacy"
                manifest_status = "invalid"
                valid = False
            backup_list.append({
                "filename": f,
                "size_bytes": size,
                "formatted_size": f"{size / (1024 * 1024):.2f} MB",
                "timestamp": datetime.fromtimestamp(mtime).isoformat(),
                "backup_format": backup_format,
                "manifest_status": manifest_status,
                "valid": valid,
            })
        # Sort newest first
        backup_list.sort(key=lambda x: x["timestamp"], reverse=True)
    except Exception as e:
        logger.error(f"[Backup Service] Error listing backups: {e}")
    return backup_list


async def get_local_backups_async() -> list:
    async with BACKUP_LOCK:
        return await run_cancellation_safe_thread(get_local_backups)


def _prepare_cloud_snapshot(backup_dir: Path) -> Path:
    lifecycle_state.qualify_local_filesystem(
        backup_dir,
        require_private=False,
        description="backup directory",
    )
    entries = tuple(backup_dir.iterdir())
    database_paths = {
        entry.name: entry
        for entry in entries
        if backup_manifest.is_supported_backup_name(entry.name)
    }
    evidence: list[tuple[Path, Path | None, backup_manifest.BackupEvidence]] = []
    for name, database_path in sorted(database_paths.items()):
        try:
            pair = eligible_backup(database_path)
        except (backup_manifest.BackupManifestError, ValueError, OSError):
            logger.warning("[Backup Service] Invalid local backup retained and excluded from cloud selection: %s", database_path.name)
            continue
        manifest_path = backup_manifest.manifest_path_for(database_path)
        if pair.manifest is not None:
            evidence.append((database_path, manifest_path, pair))
        else:
            evidence.append((database_path, None, pair))
    # Orphaned/invalid manifests remain visible locally, outside the cloud snapshot.
    if not evidence:
        raise ValueError("Cloud backup selection has no usable backup pairs.")

    snapshot = Path(
        tempfile.mkdtemp(
            prefix=".streamhome-cloud-backups.",
            dir=backup_dir.parent,
        )
    )
    os.chmod(snapshot, 0o700)
    try:
        for database_path, manifest_path, pair in evidence:
            os.link(database_path, snapshot / database_path.name, follow_symlinks=False)
            if manifest_path is not None:
                os.link(manifest_path, snapshot / manifest_path.name, follow_symlinks=False)
        if any(eligible_backup(snapshot / database_path.name) != pair for database_path, _, pair in evidence):
            raise ValueError("Cloud backup snapshot identity changed.")
        return snapshot
    except BaseException:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise


def _remove_cloud_snapshot(snapshot: Path) -> None:
    shutil.rmtree(snapshot)

async def sync_backups_to_cloud() -> bool:
    """Sync the local backup directory to the cloud backup location using Rclone."""
    if not rclone_service.cloud_write_available():
        health = rclone_service.cloud_health()
        logger.error(
            "[Backup Service] Cloud synchronization skipped because the storage circuit is open (%s).",
            health.get("errorCode") or health.get("state"),
        )
        return False
    backup_dir = Path(get_backup_dir())
    target_remote = f"{settings.RCLONE_REMOTE_PATH}/backup"

    logger.info(f"[Backup Service] Syncing backup folder with cloud: {backup_dir} -> {target_remote}")
    async with BACKUP_LOCK:
        snapshot: Path | None = None
        try:
            snapshot = await run_cancellation_safe_thread(
                _prepare_cloud_snapshot, backup_dir
            )
            result = await rclone_service.run(
                "sync", str(snapshot), target_remote, "--retries", "3", timeout=60 * 60
            )
        except (AdmissionClosedError, ValueError):
            logger.info("[Backup Service] Cloud synchronization deferred by runtime admission or invalid local evidence.")
            return False
        finally:
            if snapshot is not None:
                await run_cancellation_safe_thread(_remove_cloud_snapshot, snapshot)
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
