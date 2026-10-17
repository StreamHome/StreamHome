"""Retention regressions in disposable installations and native local Rclone."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from config import settings
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import db as database
from lifecycle.media_publication import MediaMutation, current_mutation, recover_media_operations
from lifecycle.media_work import MediaRecoveryRequired
from lifecycle.storage_retention import is_protected_path, retain_existing_tree
from services.queue import DownloadQueueManager, _recovery_metadata_inventory
from services.rclone import RcloneResult, rclone_service
from services.storage_capacity import storage_capacity, StorageCapacityError
from models import StorageMeasurement, StorageReservation


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StorageRetentionPublication(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-retention-publication-")
        self.root = Path(self.temporary.name)
        self.previous = settings.MEDIA_DIR, settings.TEMP_DIR, settings.RCLONE_REMOTE_PATH, settings.STORAGE_ENGINE
        settings.MEDIA_DIR = str(self.root / "media")
        settings.TEMP_DIR = str(self.root / "temp")
        settings.RCLONE_REMOTE_PATH = f"isolated:{self.root / 'remote'}"
        settings.STORAGE_ENGINE = "LOCAL"
        Path(settings.MEDIA_DIR).mkdir()
        Path(settings.TEMP_DIR).mkdir()
        config = self.root / "rclone.conf"
        config.write_text("[isolated]\ntype = local\n", encoding="utf-8")
        self.config_patch = patch.object(rclone_service, "config_path", config)
        self.config_patch.start()
        self.target = Path(settings.MEDIA_DIR) / "Movies" / "Film"
        self.staging = Path(settings.TEMP_DIR) / "staging"
        self.staging.mkdir()
        (self.staging / "film.mp4").write_bytes(b"new source bytes")

    async def asyncTearDown(self):
        self.config_patch.stop()
        settings.MEDIA_DIR, settings.TEMP_DIR, settings.RCLONE_REMOTE_PATH, settings.STORAGE_ENGINE = self.previous
        self.temporary.cleanup()

    def seed(self, root: Path):
        files = {"film.mp4": b"previous video", "subtitle_en.vtt": b"WEBVTT\nold subtitle",
                 "playback_cache/source.mp4": b"accepted cached source", "unknown.asset": b"ambiguous legacy bytes",
                 ".metadata/poster.webp": b"cached artwork"}
        for name, value in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        return {name: fingerprint(root / name) for name in files if name != "film.mp4"}

    def assert_retained(self, root: Path, expected: dict):
        for name, digest in expected.items():
            self.assertTrue((root / name).is_file(), name)
            self.assertEqual(fingerprint(root / name), digest, name)

    async def test_classification_fails_closed_for_unknowns_and_cache_containers(self):
        for name in ("subtitle.SRT", "captions.ass", "captions.ssa", "captions.sub", "captions.idx",
                     "subtitle.vtt", "poster.webp", "unknown.bin", ".sources/source.mp4",
                     "playback_cache/video.ts", "ingest_preview/preview.mp4", ".old.retained-abc/video.mp4",
                     ".streamhome-replacement-backups/operation/video.mp4"):
            self.assertTrue(is_protected_path(name), name)
        self.assertFalse(is_protected_path("film.mp4"))
        self.assertFalse(is_protected_path(".metadata/metadata_s1_e2.json"))

    async def test_retirement_preserves_hashes_and_rejects_symlinks(self):
        expected = self.seed(self.target)
        retained = retain_existing_tree(self.target)
        self.assertIsNotNone(retained)
        self.assert_retained(retained, expected)
        self.assertTrue(is_protected_path(retained / "film.mp4"))
        self.assertIsNone(retain_existing_tree(self.target))
        self.target.symlink_to(retained, target_is_directory=True)
        with self.assertRaises(ValueError):
            retain_existing_tree(self.target)

    async def test_accepted_local_replacement_preserves_original_sidecars_and_cache(self):
        expected = self.seed(self.target)
        async with MediaMutation("m_retention") as mutation:
            mutation.publish_directory(str(self.staging), str(self.target))
            mutation.accepted = True
        self.assert_retained(self.target, expected)
        self.assertEqual((self.target / "film.mp4").read_bytes(), b"new source bytes")
        archives = list(self.target.parent.glob("*.retained-*"))
        self.assertEqual(len(archives), 1)
        self.assert_retained(archives[0], expected)

    async def test_conflicting_local_subtitle_is_rejected_before_mutation(self):
        expected = self.seed(self.target)
        (self.staging / "subtitle_en.vtt").write_bytes(b"different candidate subtitle")
        with self.assertRaises(MediaRecoveryRequired):
            async with MediaMutation("m_retention") as mutation:
                mutation.publish_directory(str(self.staging), str(self.target))
        self.assert_retained(self.target, expected)
        self.assertEqual((self.target / "film.mp4").read_bytes(), b"previous video")

    async def test_local_rejection_restores_original_and_retains_new_cache(self):
        expected = self.seed(self.target)
        cache = self.staging / "playback_cache/new.m3u8"
        cache.parent.mkdir()
        cache.write_bytes(b"completed new cache")
        with self.assertRaisesRegex(RuntimeError, "reject catalog"):
            async with MediaMutation("m_retention") as mutation:
                mutation.publish_directory(str(self.staging), str(self.target))
                raise RuntimeError("reject catalog")
        self.assert_retained(self.target, expected)
        self.assertEqual((self.target / "film.mp4").read_bytes(), b"previous video")
        retained = list(self.target.parent.glob("*.retained-*"))
        self.assertEqual((retained[0] / "playback_cache/new.m3u8").read_bytes(), b"completed new cache")

    async def test_owned_queue_workspace_preserves_sidecars_and_cached_video(self):
        expected = self.seed(self.staging)
        DownloadQueueManager._retain_staging_sidecars(self.staging)
        retained = next(self.staging.parent.glob(".staging.retained-*"))
        self.assert_retained(retained, expected)
        self.assertFalse((retained / "film.mp4").exists())

    async def test_owned_queue_workspace_link_cannot_escape_retention_guard(self):
        external = self.root / "outside.vtt"
        external.write_bytes(b"must survive")
        (self.staging / "link.vtt").symlink_to(external)
        with self.assertRaises(ValueError):
            DownloadQueueManager._retain_staging_sidecars(self.staging)
        self.assertEqual(external.read_bytes(), b"must survive")
        self.assertTrue((self.staging / "film.mp4").exists())

    async def test_retained_and_unaccepted_metadata_never_enters_catalog_inventory(self):
        media = Path(settings.MEDIA_DIR)
        accepted = media / "Movies/Film/.metadata"
        accepted.mkdir(parents=True)
        (accepted / "metadata.json").write_text("{}")
        for name in (".Film.retained-abc", ".Film." + "a" * 32 + ".candidate",
                     ".Film." + "b" * 32 + ".previous", ".streamhome-ingestion",
                     ".streamhome-replacement-backups", ".streamhome-retained"):
            directory = media / name / "Film/.metadata"
            directory.mkdir(parents=True)
            (directory / "metadata.json").write_text("{}")
        inventory = _recovery_metadata_inventory(str(media))
        self.assertEqual([Path(root) for root, _ in inventory], [accepted])

    async def cloud_target(self):
        if not rclone_service.executable():
            self.skipTest("rclone is unavailable")
        return self.root / "remote/Movies/Film"

    async def test_native_cloud_acceptance_preserves_destination_only_bytes(self):
        target = await self.cloud_target()
        expected = self.seed(target)
        async with MediaMutation("m_retention") as mutation:
            await mutation.publish_cloud_directory(str(self.staging), "Movies/Film")
            mutation.accepted = True
        self.assert_retained(target, expected)
        self.assertEqual((target / "film.mp4").read_bytes(), b"new source bytes")
        archived = self.root / "remote/.streamhome-replacement-backups" / mutation.operation_id
        self.assert_retained(archived, expected)

    async def test_native_cloud_rejection_restores_sources_without_deleting_cache(self):
        target = await self.cloud_target()
        expected = self.seed(target)
        (self.staging / "subtitle_new.vtt").write_bytes(b"WEBVTT\nnew complete subtitle")
        with self.assertRaisesRegex(RuntimeError, "reject catalog"):
            async with MediaMutation("m_retention") as mutation:
                await mutation.publish_cloud_directory(str(self.staging), "Movies/Film")
                raise RuntimeError("reject catalog")
        self.assert_retained(target, expected)
        self.assertEqual((target / "film.mp4").read_bytes(), b"previous video")
        self.assertEqual((target / "subtitle_new.vtt").read_bytes(), b"WEBVTT\nnew complete subtitle")

    async def test_native_cloud_protected_collision_rejects_before_target_writes(self):
        target = await self.cloud_target()
        expected = self.seed(target)
        (self.staging / "subtitle_en.vtt").write_bytes(b"conflicting subtitle")
        with self.assertRaises(MediaRecoveryRequired):
            async with MediaMutation("m_retention") as mutation:
                await mutation.publish_cloud_directory(str(self.staging), "Movies/Film")
        self.assert_retained(target, expected)
        self.assertEqual((target / "film.mp4").read_bytes(), b"previous video")

    async def test_native_new_cloud_rejection_keeps_sidecar_without_adopting_source(self):
        target = await self.cloud_target()
        (self.staging / "subtitle_new.vtt").write_bytes(b"WEBVTT\nretained")
        with self.assertRaisesRegex(RuntimeError, "reject catalog"):
            async with MediaMutation("m_retention") as mutation:
                await mutation.publish_cloud_directory(str(self.staging), "Movies/Film")
                raise RuntimeError("reject catalog")
        self.assertFalse((target / "film.mp4").exists())
        self.assertEqual((target / "subtitle_new.vtt").read_bytes(), b"WEBVTT\nretained")

    async def test_failed_new_local_subtitle_is_retired_without_losing_payload(self):
        self.target.mkdir(parents=True)
        subtitle = self.target / "subtitle_new.vtt"
        with self.assertRaisesRegex(RuntimeError, "reject subtitle"):
            async with MediaMutation("m_retention") as mutation:
                mutation.capture_local(str(subtitle))
                subtitle.write_bytes(b"WEBVTT\ncompleted subtitle")
                raise RuntimeError("reject subtitle")
        self.assertFalse(subtitle.exists())
        retained = next(self.target.glob(".subtitle_new.vtt.retained-*"))
        self.assertEqual(retained.read_bytes(), b"WEBVTT\ncompleted subtitle")

    async def test_failed_new_cloud_subtitle_is_durably_retained(self):
        target = await self.cloud_target()
        target.mkdir(parents=True)
        subtitle = target / "subtitle_new.vtt"
        remote = f"{settings.RCLONE_REMOTE_PATH}/Movies/Film/subtitle_new.vtt"
        with self.assertRaisesRegex(RuntimeError, "reject subtitle"):
            async with MediaMutation("m_retention") as mutation:
                await mutation.capture_cloud(remote)
                subtitle.write_bytes(b"WEBVTT\ncompleted subtitle")
                raise RuntimeError("reject subtitle")
        self.assertFalse(subtitle.exists())
        archive = Path(mutation.entries[0]["retained_cloud"]["path"].removeprefix("isolated:"))
        self.assertEqual(archive.read_bytes(), b"WEBVTT\ncompleted subtitle")
        self.assertFalse(mutation.root.exists())

    async def test_cloud_archive_copy_failure_preserves_visible_payload_and_retries(self):
        target = await self.cloud_target()
        target.mkdir(parents=True)
        subtitle = target / "subtitle_new.vtt"
        remote = f"{settings.RCLONE_REMOTE_PATH}/Movies/Film/subtitle_new.vtt"
        async with MediaMutation("m_retention") as mutation:
            await mutation.capture_cloud(remote)
            subtitle.write_bytes(b"WEBVTT\ncompleted subtitle")
            real_run = rclone_service.run
            async def fail_copy(*arguments, **kwargs):
                if arguments[0] == "copyto" and ".streamhome-retained/" in arguments[2]:
                    return RcloneResult(1, error_code="rclone_failed")
                return await real_run(*arguments, **kwargs)
            with patch.object(rclone_service, "run", fail_copy), self.assertRaises(RuntimeError):
                await mutation.compensate()
            self.assertEqual(subtitle.read_bytes(), b"WEBVTT\ncompleted subtitle")
            self.assertTrue((mutation.root / "operation.json").is_file())
            await mutation.compensate()
            await mutation.compensate()
            archive = Path(mutation.entries[0]["retained_cloud"]["path"].removeprefix("isolated:"))
            self.assertEqual(archive.read_bytes(), b"WEBVTT\ncompleted subtitle")
            self.assertFalse(subtitle.exists())

    async def test_cloud_archive_delete_ack_loss_is_retryable(self):
        target = await self.cloud_target()
        target.mkdir(parents=True)
        subtitle = target / "subtitle_new.vtt"
        remote = f"{settings.RCLONE_REMOTE_PATH}/Movies/Film/subtitle_new.vtt"
        async with MediaMutation("m_retention") as mutation:
            await mutation.capture_cloud(remote)
            subtitle.write_bytes(b"WEBVTT\ncompleted subtitle")
            real_run = rclone_service.run
            async def lose_ack(*arguments, **kwargs):
                result = await real_run(*arguments, **kwargs)
                if arguments[:2] == ("deletefile", remote):
                    return RcloneResult(1, error_code="rclone_failed")
                return result
            with patch.object(rclone_service, "run", lose_ack), self.assertRaises(RuntimeError):
                await mutation.compensate()
            self.assertFalse(subtitle.exists())
            await mutation.compensate()
            archive = Path(mutation.entries[0]["retained_cloud"]["path"].removeprefix("isolated:"))
            self.assertEqual(archive.read_bytes(), b"WEBVTT\ncompleted subtitle")

    async def test_cloud_archive_never_removes_a_changed_visible_generation(self):
        target = await self.cloud_target()
        target.mkdir(parents=True)
        subtitle = target / "subtitle_new.vtt"
        remote = f"{settings.RCLONE_REMOTE_PATH}/Movies/Film/subtitle_new.vtt"
        mutation = await MediaMutation("m_retention").__aenter__()
        try:
            await mutation.capture_cloud(remote)
            subtitle.write_bytes(b"WEBVTT\ncompleted subtitle")
            real_run = rclone_service.run
            async def change_generation(*arguments, **kwargs):
                result = await real_run(*arguments, **kwargs)
                if arguments[0] == "copyto" and ".streamhome-retained/" in arguments[2]:
                    subtitle.write_bytes(b"WEBVTT\nexternal replacement must survive")
                return result
            with patch.object(rclone_service, "run", change_generation), self.assertRaises(RuntimeError):
                await mutation.compensate()
            self.assertEqual(subtitle.read_bytes(), b"WEBVTT\nexternal replacement must survive")
            archive = Path(mutation.entries[0]["retained_cloud"]["path"].removeprefix("isolated:"))
            self.assertEqual(archive.read_bytes(), b"WEBVTT\ncompleted subtitle")
        finally:
            current_mutation.reset(mutation.token)
            mutation.entered = False

    async def hybrid_archive_capacity_scenario(self, failure: str | None):
        target = await self.cloud_target()
        target.mkdir(parents=True)
        subtitle = target / "subtitle_new.vtt"
        remote = f"{settings.RCLONE_REMOTE_PATH}/Movies/Film/subtitle_new.vtt"
        engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'capacity.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        try:
            with patch.object(database, "engine", engine), patch.object(settings, "STORAGE_POLICY",
                    {"revision": 1, "offload_enabled": True, "local_budget_bytes": None, "cloud_budget_bytes": 100}), \
                    patch("services.storage_inventory.target_generation", return_value="test-target"):
                async with AsyncSession(engine) as db:
                    db.add(StorageMeasurement(id="cloud", measured_at=time.time(), payload=json.dumps({
                        "target_generation": "test-target", "used_bytes": 90, "account_free_bytes": 1000,
                        "capacity_valid": True})))
                    await db.commit()
                mutation = await MediaMutation("m_retention").__aenter__()
                try:
                    await mutation.capture_cloud(remote)
                    subtitle.write_bytes(b"0123456789")
                    if failure:
                        real_run, real_progress = rclone_service.run, storage_capacity.progress
                        async def copy_ack_lost(*arguments, **kwargs):
                            result = await real_run(*arguments, **kwargs)
                            if arguments[0] == "copyto" and ".streamhome-retained/" in arguments[2]:
                                return RcloneResult(1, error_code="rclone_failed")
                            return result
                        async def progress_ack_lost(*arguments, **kwargs):
                            await real_progress(*arguments, **kwargs)
                            raise RuntimeError("lost progress acknowledgment")
                        if failure == "cancel":
                            entered, release = asyncio.Event(), asyncio.Event()
                            async def delayed_progress(*arguments, **kwargs):
                                await real_progress(*arguments, **kwargs)
                                entered.set()
                                await release.wait()
                            with patch.object(storage_capacity, "progress", delayed_progress):
                                copying = asyncio.create_task(mutation.compensate())
                                await asyncio.wait_for(entered.wait(), timeout=10)
                                copying.cancel()
                                release.set()
                                with self.assertRaises(asyncio.CancelledError):
                                    await copying
                            self.assertEqual(subtitle.read_bytes(), b"0123456789")
                        else:
                            fault = (patch.object(rclone_service, "run", copy_ack_lost) if failure == "copy"
                                     else patch.object(storage_capacity, "progress", progress_ack_lost))
                            with fault, self.assertRaises(RuntimeError):
                                await mutation.compensate()
                        async with AsyncSession(engine) as db:
                            claims = (await db.exec(select(StorageReservation).where(StorageReservation.state == "active"))).all()
                            self.assertEqual(len(claims), 1)
                            self.assertEqual(claims[0].peak_bytes, 10)
                        # Simulate restart: reconstruct exact claims from the
                        # persisted operation journal, not process-local maps.
                        current_mutation.reset(mutation.token)
                        mutation.entered = False
                        await recover_media_operations()
                    else:
                        await mutation.compensate()
                        await mutation.compensate()
                        await mutation.__aexit__(None, None, None)
                    await recover_media_operations()
                    async with AsyncSession(engine) as db:
                        value = json.loads((await db.get(StorageMeasurement, "cloud")).payload)
                        self.assertEqual(value["used_bytes"], 100)
                        self.assertEqual(value["account_free_bytes"], 990)
                        claims = (await db.exec(select(StorageReservation))).all()
                        self.assertEqual(len(claims), 1)
                        self.assertEqual(claims[0].written_bytes, 10)
                        self.assertEqual(claims[0].state, "released")
                    with self.assertRaises(StorageCapacityError) as blocked:
                        await storage_capacity.reserve("second writer", 1, f"{settings.RCLONE_REMOTE_PATH}/next", scope="cloud")
                    self.assertEqual(blocked.exception.code, "storage_cloud_budget_exceeded")
                    self.assertFalse(subtitle.exists())
                finally:
                    if mutation.entered:
                        current_mutation.reset(mutation.token)
                        mutation.entered = False
        finally:
            await engine.dispose()

    async def test_hybrid_archive_progress_survives_cleanup_and_blocks_second_writer(self):
        await self.hybrid_archive_capacity_scenario(None)

    async def test_hybrid_archive_copy_ack_loss_recovers_owned_claim_and_progress(self):
        await self.hybrid_archive_capacity_scenario("copy")

    async def test_hybrid_archive_progress_ack_loss_does_not_charge_twice_on_restart(self):
        await self.hybrid_archive_capacity_scenario("progress")

    async def test_hybrid_archive_progress_cancellation_keeps_claim_until_restart(self):
        await self.hybrid_archive_capacity_scenario("cancel")
