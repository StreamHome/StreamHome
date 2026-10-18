from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import os
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from lifecycle.coordination import (
    EffectClass,
    FenceIdentity,
    LeaseStateError,
    RuntimeCoordinator,
    use_runtime_coordinator_for_testing,
)


SERVER_ROOT = Path(__file__).resolve().parents[2]


class _Logger:
    def __getattr__(self, name: str):
        del name
        return lambda *args, **kwargs: None


@contextmanager
def loaded_module(relative: str, name: str, stubs: dict[str, types.ModuleType]):
    previous = {key: sys.modules.get(key) for key in [*stubs, name]}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(name, SERVER_ROOT / relative)
        if spec is None or spec.loader is None:
            raise AssertionError(f"Could not load {relative}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def module_stub(name: str, **values: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


class WriterBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def playback_stubs(self, temporary: str) -> dict[str, types.ModuleType]:
        services = module_stub("services")
        services.__path__ = []
        settings = SimpleNamespace(
            TEMP_DIR=temporary,
            PLAYBACK_TRANSCODE_CONCURRENCY=1,
        )
        return {
            "config": module_stub("config", settings=settings),
            "services": services,
            "services.logger": module_stub("services.logger", logger=_Logger()),
            "services.languages": module_stub(
                "services.languages",
                language_label=lambda value: value,
                normalize_language_tag=lambda value, fallback="und": value or fallback,
            ),
            "services.media_timing": module_stub(
                "services.media_timing",
                canonical_audio_filter_chain=lambda *args, **kwargs: [],
            ),
            "services.ffmpeg_input": module_stub(
                "services.ffmpeg_input",
                generated_hls_input_options=lambda: [],
                local_media_input_options=lambda: [],
                native_media_environment=lambda: {},
            ),
            "services.media_source": module_stub(
                "services.media_source",
                ResolvedMediaSource=object,
                clear_cloud_object_cache=lambda *_args, **_kwargs: None,
                cloud_read_enabled=lambda: False,
                playback_source_fingerprint=lambda *args, **kwargs: "a" * 32,
                resolve_media_source=lambda *args, **kwargs: None,
                resolve_related_source=mock.AsyncMock(
                    side_effect=AssertionError("Unexpected related source resolution")
                ),
            ),
            "services.rclone": module_stub(
                "services.rclone",
                rclone_service=SimpleNamespace(),
            ),
            "services.state": module_stub(
                "services.state",
                register_process=lambda *args, **kwargs: None,
                unregister_process=lambda *args, **kwargs: True,
            ),
        }

    @staticmethod
    def media() -> SimpleNamespace:
        return SimpleNamespace(
            id="m_test",
            source_fingerprint="a" * 32,
            probed_duration=120.0,
            container="mp4",
            codec="h264",
            width=1280,
            height=720,
            frame_rate=24.0,
            quality="720p",
            audio_metadata=[],
            languages=[],
        )

    async def test_playback_prepare_closed_cache_admission_mutates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with loaded_module(
                "services/playback_prep.py",
                "phase2_playback_prep_test",
                self.playback_stubs(temporary),
            ) as module:
                service = module.PlaybackPrepService()
                media = self.media()
                source = SimpleNamespace(fingerprint="a" * 32, video_fingerprint="b" * 32)
                target = service.cache_path(media.id, source.fingerprint)
                coordinator = RuntimeCoordinator()
                coordinator.close_admission(
                    [EffectClass.DISPOSABLE_CACHE_WRITE],
                    operation_id=str(uuid.uuid4()),
                    generation=1,
                )
                with use_runtime_coordinator_for_testing(coordinator):
                    state = await service.prepare(
                        media.id,
                        media,
                        source,
                        include_remaining=True,
                        retry_errors=True,
                    )
                self.assertEqual(state, "preparing")
                self.assertFalse(target.exists())
                self.assertEqual(service.active_jobs, {})

    async def test_nested_prepare_widening_preserves_existing_cache_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with loaded_module(
                "services/playback_prep.py",
                "phase2_playback_prep_nested_test",
                self.playback_stubs(temporary),
            ) as module:
                service = module.PlaybackPrepService()
                media = self.media()
                source = SimpleNamespace(fingerprint="a" * 32, video_fingerprint="b" * 32)
                target = service.cache_path(media.id, source.fingerprint)
                target.mkdir(parents=True)
                error = target / "preparation-error.json"
                master = target / module.MASTER_NAME
                error.write_text("preserve-error", encoding="utf-8")
                master.write_text("preserve-master", encoding="utf-8")
                coordinator = RuntimeCoordinator()

                with use_runtime_coordinator_for_testing(coordinator):
                    async with coordinator.work(
                        "test.media-update",
                        [EffectClass.DB_WRITE, EffectClass.DURABLE_MEDIA_WRITE],
                    ):
                        coordinator.close_admission(
                            [EffectClass.DISPOSABLE_CACHE_WRITE],
                            operation_id=str(uuid.uuid4()),
                            generation=1,
                        )
                        await service.prepare(
                            media.id,
                            media,
                            source,
                            include_remaining=True,
                            retry_errors=True,
                        )

                self.assertEqual(error.read_text(encoding="utf-8"), "preserve-error")
                self.assertEqual(master.read_text(encoding="utf-8"), "preserve-master")
                self.assertEqual(service.active_jobs, {})

    async def test_delayed_remaining_renditions_respect_closed_cache_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with loaded_module(
                "services/playback_prep.py",
                "phase2_playback_prep_delayed_test",
                self.playback_stubs(temporary),
            ) as module:
                service = module.PlaybackPrepService()
                service.playlist_ready = mock.Mock(return_value=True)
                service.preparation_error = mock.Mock(return_value=None)
                service._schedule_audio = mock.Mock()
                service._schedule_video = mock.Mock()
                coordinator = RuntimeCoordinator()
                coordinator.close_admission(
                    [EffectClass.DISPOSABLE_CACHE_WRITE],
                    operation_id=str(uuid.uuid4()),
                    generation=1,
                )
                with use_runtime_coordinator_for_testing(coordinator):
                    with mock.patch.object(module.asyncio, "sleep", mock.AsyncMock()):
                        await service._schedule_remaining_after_baseline(
                            "m_test",
                            "a" * 32,
                            SimpleNamespace(),
                            self.media(),
                            retry_errors=True,
                        )
                service._schedule_audio.assert_not_called()
                service._schedule_video.assert_not_called()

    async def test_direct_tmdb_cache_rejection_is_retained_and_deduplicated(self) -> None:
        services = module_stub("services")
        services.__path__ = []
        settings = SimpleNamespace(
            TMDB_API_KEY="",
            TMDB_READ_ACCESS_TOKEN="",
            MEDIA_DIR="unused",
        )
        stubs = {
            "httpx": module_stub(
                "httpx",
                AsyncClient=object,
                Timeout=object,
                TimeoutException=Exception,
                NetworkError=Exception,
            ),
            "config": module_stub("config", settings=settings),
            "services": services,
            "services.logger": module_stub("services.logger", logger=_Logger()),
            "services.vibe_analysis": module_stub(
                "services.vibe_analysis",
                compute_trope_vectors=lambda *args, **kwargs: [],
                relevant_crew=lambda *args, **kwargs: [],
            ),
        }
        with loaded_module("services/tmdb.py", "phase2_tmdb_test", stubs) as module:
            client = module.TMDBClient()
            coordinator = RuntimeCoordinator()
            coordinator.close_admission(
                [EffectClass.DB_WRITE, EffectClass.DURABLE_MEDIA_WRITE],
                operation_id=str(uuid.uuid4()),
                generation=1,
            )
            payload = {"tmdb_id": 42, "type": "movie", "title": "Example"}
            with use_runtime_coordinator_for_testing(coordinator):
                self.assertIsNone(await client.cache_media_locally(payload, "poster", "backdrop"))
                first_task = client._direct_cache_retry_tasks["m_42"]
                self.assertIsNone(await client.cache_media_locally(payload, "poster-2", "backdrop-2"))
                self.assertIs(client._direct_cache_retry_tasks["m_42"], first_task)
                self.assertEqual(len(client._direct_cache_deferred), 1)
                self.assertEqual(
                    client._direct_cache_deferred["m_42"][1:],
                    ("poster-2", "backdrop-2"),
                )
            await client.stop_cache_workers()
            self.assertEqual(client._direct_cache_retry_tasks, {})
            self.assertEqual(client._direct_cache_deferred, {})

    async def test_direct_tmdb_cache_resumes_once_after_exact_fence_release(self) -> None:
        services = module_stub("services")
        services.__path__ = []
        settings = SimpleNamespace(
            TMDB_API_KEY="",
            TMDB_READ_ACCESS_TOKEN="",
            MEDIA_DIR="unused",
        )
        stubs = {
            "httpx": module_stub(
                "httpx",
                AsyncClient=object,
                Timeout=object,
                TimeoutException=Exception,
                NetworkError=Exception,
            ),
            "config": module_stub("config", settings=settings),
            "services": services,
            "services.logger": module_stub("services.logger", logger=_Logger()),
            "services.vibe_analysis": module_stub(
                "services.vibe_analysis",
                compute_trope_vectors=lambda *args, **kwargs: [],
                relevant_crew=lambda *args, **kwargs: [],
            ),
        }
        with loaded_module("services/tmdb.py", "phase2_tmdb_resume_test", stubs) as module:
            client = module.TMDBClient()
            coordinator = RuntimeCoordinator()
            snapshot = coordinator.close_admission(
                [EffectClass.DB_WRITE, EffectClass.DURABLE_MEDIA_WRITE],
                operation_id=str(uuid.uuid4()),
                generation=4,
            )
            sleep_started: asyncio.Queue[float] = asyncio.Queue()
            sleep_permits: asyncio.Queue[None] = asyncio.Queue()

            async def controlled_sleep(delay: float) -> None:
                await sleep_started.put(delay)
                await sleep_permits.get()

            first_payload = {"tmdb_id": 42, "type": "movie", "title": "First"}
            latest_payload = {"tmdb_id": 42, "type": "movie", "title": "Latest"}
            cache = mock.AsyncMock(return_value="m_42")
            with use_runtime_coordinator_for_testing(coordinator):
                with mock.patch.object(module.asyncio, "sleep", controlled_sleep):
                    with mock.patch.object(client, "_cache_media_locally_admitted", cache):
                        await client.cache_media_locally(first_payload, "poster-1", "backdrop-1")
                        retry_task = client._direct_cache_retry_tasks["m_42"]
                        await client.cache_media_locally(latest_payload, "poster-2", "backdrop-2")
                        self.assertIs(client._direct_cache_retry_tasks["m_42"], retry_task)
                        self.assertEqual(await sleep_started.get(), 1.0)
                        cache.assert_not_awaited()

                        wrong_fence = replace(
                            snapshot,
                            fence=FenceIdentity(snapshot.fence.operation_id, 5),
                        )
                        with self.assertRaises(LeaseStateError):
                            coordinator.release_fence(wrong_fence)
                        sleep_permits.put_nowait(None)
                        self.assertEqual(await sleep_started.get(), 2.0)
                        cache.assert_not_awaited()

                        coordinator.release_fence(snapshot)
                        sleep_permits.put_nowait(None)
                        await retry_task

            cache.assert_awaited_once_with(latest_payload, "poster-2", "backdrop-2")
            self.assertEqual(client._direct_cache_deferred, {})
            self.assertEqual(client._direct_cache_retry_tasks, {})

    def backup_stubs(
        self, temporary: str, rclone_service: object
    ) -> dict[str, types.ModuleType]:
        services = module_stub("services")
        services.__path__ = []
        sqlmodel = module_stub("sqlmodel", select=lambda *args, **kwargs: None)
        sqlmodel.__path__ = []
        sqlmodel_ext = module_stub("sqlmodel.ext")
        sqlmodel_ext.__path__ = []
        sqlmodel_asyncio = module_stub("sqlmodel.ext.asyncio")
        sqlmodel_asyncio.__path__ = []
        return {
            "config": module_stub(
                "config",
                settings=SimpleNamespace(
                    db_path=str(Path(temporary) / "database.db"),
                    BUILD_ID="test-build",
                    RCLONE_REMOTE_PATH="remote:streamhome",
                    STORAGE_ENGINE="CLOUD",
                ),
            ),
            "db": module_stub("db", engine=object()),
            "models": module_stub(
                "models", PlaybackRun=object, PlaybackSession=object
            ),
            "services": services,
            "services.logger": module_stub("services.logger", logger=_Logger()),
            "services.queue": module_stub(
                "services.queue", queue_manager=SimpleNamespace(active_tasks=[])
            ),
            "services.rclone": module_stub(
                "services.rclone", rclone_service=rclone_service
            ),
            "services.storage_inventory": module_stub(
                "services.storage_inventory",
                hybrid_policy_active=lambda: False,
                target_generation=lambda: "unused-legacy-backup-target",
            ),
            "sqlmodel": sqlmodel,
            "sqlmodel.ext": sqlmodel_ext,
            "sqlmodel.ext.asyncio": sqlmodel_asyncio,
            "sqlmodel.ext.asyncio.session": module_stub(
                "sqlmodel.ext.asyncio.session", AsyncSession=object
            ),
        }

    @staticmethod
    def _backup_database(path: Path, marker: str) -> None:
        connection = sqlite3.connect(path)
        try:
            for table in ("user", "movie", "authsession"):
                connection.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO user VALUES ('marker', ?)", (marker,))
            connection.commit()
        finally:
            connection.close()
        path.chmod(0o600)

    async def test_cloud_snapshot_selects_only_valid_logical_backups_and_serializes_delete(self) -> None:
        class Rclone:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.sources: list[Path] = []

            def cloud_write_available(self) -> bool:
                return True

            def cloud_health(self) -> dict[str, str]:
                return {}

            async def run(self, _command: str, source: str, *_args: object, **_kwargs: object):
                self.sources.append(Path(source))
                self.started.set()
                await self.release.wait()
                return SimpleNamespace(ok=True, error_code=None, stderr="")

        with tempfile.TemporaryDirectory() as temporary:
            rclone = Rclone()
            with loaded_module(
                "services/backup.py",
                "phase6_backup_cloud_test",
                self.backup_stubs(temporary, rclone),
            ) as module:
                backup_dir = Path(temporary) / "backup"
                backup_dir.mkdir(mode=0o700)
                manifest_db = backup_dir / "backup_v1_20260921_120000.db"
                legacy_db = backup_dir / "backup_20260920_120000.db"
                self._backup_database(manifest_db, "manifest")
                self._backup_database(legacy_db, "legacy")
                module.backup_manifest.create_manifest(
                    manifest_db, application_build_id="test-build"
                )
                with mock.patch.object(module, "get_backup_dir", return_value=str(backup_dir)):
                    sync = asyncio.create_task(module.sync_backups_to_cloud())
                    await asyncio.wait_for(rclone.started.wait(), timeout=5)
                    source = rclone.sources[0]
                    self.assertEqual(
                        {path.name for path in source.iterdir()},
                        {
                            manifest_db.name,
                            f"{manifest_db.name}.manifest.json",
                            legacy_db.name,
                        },
                    )
                    delete = asyncio.create_task(module.delete_backup(manifest_db))
                    await asyncio.sleep(0.05)
                    self.assertFalse(delete.done())
                    rclone.release.set()
                    self.assertTrue(await sync)
                    await delete
                    self.assertFalse(manifest_db.exists())

    async def test_cloud_snapshot_rejects_missing_or_corrupt_manifest(self) -> None:
        rclone = SimpleNamespace(
            cloud_write_available=lambda: True,
            cloud_health=lambda: {},
            run=mock.AsyncMock(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            with loaded_module(
                "services/backup.py",
                "phase6_backup_cloud_invalid_test",
                self.backup_stubs(temporary, rclone),
            ) as module:
                backup_dir = Path(temporary) / "backup"
                backup_dir.mkdir(mode=0o700)
                database = backup_dir / "backup_v1_20260921_120000.db"
                self._backup_database(database, "candidate")
                with mock.patch.object(module, "get_backup_dir", return_value=str(backup_dir)):
                    self.assertFalse(await module.sync_backups_to_cloud())
                    rclone.run.assert_not_awaited()
                    manifest = module.backup_manifest.manifest_path_for(database)
                    manifest.write_text("{", encoding="utf-8")
                    manifest.chmod(0o600)
                    self.assertFalse(await module.sync_backups_to_cloud())
                    rclone.run.assert_not_awaited()

    async def test_backup_retention_and_schedule_ignore_invalid_databases(self) -> None:
        rclone = SimpleNamespace(cloud_write_available=lambda: True, cloud_health=lambda: {}, run=mock.AsyncMock())
        with tempfile.TemporaryDirectory() as temporary:
            with loaded_module("services/backup.py", "backup_eligibility_test", self.backup_stubs(temporary, rclone)) as module:
                backup_dir = Path(temporary) / "backup"
                backup_dir.mkdir(mode=0o700)
                old = backup_dir / "backup_20260920_120000.db"
                newer = backup_dir / "backup_20260921_120000.db"
                invalid = backup_dir / "backup_20260922_120000.db"
                invalid_manifest = backup_dir / "backup_v1_20260922_120001.db"
                self._backup_database(old, "old")
                self._backup_database(newer, "new")
                invalid.write_bytes(b"not SQLite")
                invalid.chmod(0o600)
                invalid_manifest.write_bytes(b"corrupt SQLite with matching manifest")
                invalid_manifest.chmod(0o600)
                module.backup_manifest.create_manifest(invalid_manifest, application_build_id="test")
                now = datetime.now()
                for age, path in ((72, old), (48, newer)):
                    timestamp = (now - timedelta(hours=age)).timestamp()
                    os.utime(path, (timestamp, timestamp))
                with mock.patch.object(module, "get_backup_dir", return_value=str(backup_dir)):
                    listed = module.get_local_backups()
                    self.assertEqual(sum(item["valid"] for item in listed), 2)
                    self.assertTrue(module.backup_due(listed, now=now))
                    module._prune_old_backups(keep_count=1)
                    self.assertFalse(old.exists())
                    self.assertTrue(newer.exists())
                    self.assertTrue(invalid.exists())
                    self.assertTrue(invalid_manifest.exists())
                    snapshot = module._prepare_cloud_snapshot(backup_dir)
                    try:
                        self.assertEqual({path.name for path in snapshot.iterdir()}, {newer.name})
                    finally:
                        module._remove_cloud_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
