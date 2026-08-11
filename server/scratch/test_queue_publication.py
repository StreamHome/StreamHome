import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import services.queue as queue_module
from config import settings
from models import DownloadTask
from services.ingestion_errors import IngestionFailure
from services.queue import DownloadQueueManager, _atomic_write_json


def test_atomic_json_replacement() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        destination = Path(temp_dir) / ".metadata" / "metadata.json"
        _atomic_write_json(str(destination), {"title": "First", "languages": ["en"]})
        _atomic_write_json(str(destination), {"title": "Second", "languages": ["tr"]})
        assert json.loads(destination.read_text(encoding="utf-8")) == {
            "title": "Second",
            "languages": ["tr"],
        }
        assert list(destination.parent.glob("*.tmp")) == []


def test_local_publication_can_roll_back_and_finalize() -> None:
    original_media_dir = settings.MEDIA_DIR
    original_temp_dir = settings.TEMP_DIR
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings.MEDIA_DIR = str(root / "media")
            settings.TEMP_DIR = str(root / "temp")
            manager = DownloadQueueManager()
            destination = root / "media" / "Movies" / "Movie_TMDB_1"
            destination.mkdir(parents=True)
            (destination / "movie.mp4").write_bytes(b"previous")

            staging = root / "temp" / "ingestion" / "task-1" / "Movies" / "Movie_TMDB_1"
            staging.mkdir(parents=True)
            (staging / "movie.mp4").write_bytes(b"replacement")
            backup = manager._publish_local_directory(str(staging), str(destination), "task-1")
            assert backup is not None
            assert (destination / "movie.mp4").read_bytes() == b"replacement"
            manager._rollback_local_publication(str(destination), backup, "task-1")
            assert (destination / "movie.mp4").read_bytes() == b"previous"

            staging = root / "temp" / "ingestion" / "task-2" / "Movies" / "Movie_TMDB_1"
            staging.mkdir(parents=True)
            (staging / "movie.mp4").write_bytes(b"committed")
            backup = manager._publish_local_directory(str(staging), str(destination), "task-2")
            manager._finalize_local_publication("task-2")
            assert (destination / "movie.mp4").read_bytes() == b"committed"
            assert backup is not None and not Path(backup).exists()
    finally:
        settings.MEDIA_DIR = original_media_dir
        settings.TEMP_DIR = original_temp_dir


def test_storage_reservations_cover_concurrent_capacity() -> None:
    original_media_dir = settings.MEDIA_DIR
    original_temp_dir = settings.TEMP_DIR
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings.MEDIA_DIR = str(root / "media")
            settings.TEMP_DIR = str(root / "temp")
            manager = DownloadQueueManager()
            manager.MINIMUM_TASK_RESERVATION_BYTES = 1024
            manager.STORAGE_SAFETY_MARGIN_BYTES = 1024
            manager._reserve_storage("task-1", 4096)
            manager._reserve_storage("task-2", 4096)
            assert manager.storage_reservations == {"task-1": 4096, "task-2": 4096}
            try:
                manager._reserve_storage("task-3", 2**63)
            except Exception as exc:
                assert getattr(exc, "failure", None) is not None
                assert exc.failure.code == "INSUFFICIENT_STORAGE"
            else:
                raise AssertionError("An impossible storage reservation was accepted")
    finally:
        settings.MEDIA_DIR = original_media_dir
        settings.TEMP_DIR = original_temp_dir


async def _completed_task_rejects_late_failure() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        database_path = Path(temp_dir) / "queue.db"
        test_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        original_engine = queue_module.engine
        queue_module.engine = test_engine
        try:
            async with test_engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with AsyncSession(test_engine, expire_on_commit=False) as session:
                task = DownloadTask(
                    id="completed-task",
                    tmdb_id=1,
                    media_type="movie",
                    video_url="https://media.example/movie.mp4",
                    status="COMPLETED",
                    created_at="2026-08-11T00:00:00",
                )
                session.add(task)
                await session.commit()

            manager = DownloadQueueManager()
            await manager._record_task_failure(
                "completed-task",
                IngestionFailure("POST_PUBLICATION_FAILED", "Optional scheduling failed."),
                "https://media.example/movie.mp4",
            )
            async with AsyncSession(test_engine, expire_on_commit=False) as session:
                persisted = await session.get(DownloadTask, "completed-task")
                assert persisted is not None
                assert persisted.status == "COMPLETED"
                assert persisted.error_message is None
        finally:
            queue_module.engine = original_engine
            await test_engine.dispose()


def test_completed_task_rejects_late_failure() -> None:
    asyncio.run(_completed_task_rejects_late_failure())


def test_queue_source_keeps_post_publication_work_best_effort() -> None:
    source = Path(queue_module.__file__).read_text(encoding="utf-8")
    final_fingerprint = source.index("            final_fingerprint = prepared_fingerprint")
    finalization_start = source.index(
        "            async with AsyncSession(engine, expire_on_commit=False) as db:",
        final_fingerprint,
    )
    finalization = source[finalization_start:source.index("            if uploaded_to_cloud", finalization_start)]
    commit = finalization.index("await db.commit()")
    completion_guard = finalization.index("task_completed = True", commit)
    playback_schedule = finalization.index("await self._schedule_playback_baseline", completion_guard)
    assert commit < completion_guard < playback_schedule
    assert "AsyncSession(engine, expire_on_commit=False)" in finalization
    assert "created_artifacts.clear()" in finalization[commit:playback_schedule]
    assert "if task.status == \"COMPLETED\"" in source
    assert "shutil.rmtree(dest_dir)" not in source
    assert '"sync",\n                local_dir,\n                staging_remote,' in source
    assert 'rclone_service.run("moveto", target_remote, backup_remote' in source
    assert "await self._rollback_cloud_publication" in source
    recovery = source[source.index("    async def sync_media_from_disk"):]
    assert '"lsf",' in recovery
    assert "cloud_inventory" in recovery
    assert "await verify_media_exists(rel_path)" not in recovery


if __name__ == "__main__":
    test_atomic_json_replacement()
    test_local_publication_can_roll_back_and_finalize()
    test_storage_reservations_cover_concurrent_capacity()
    test_completed_task_rejects_late_failure()
    test_queue_source_keeps_post_publication_work_best_effort()
    print("Queue publication regression checks passed.")
