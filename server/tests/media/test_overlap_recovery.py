"""Crash-safe temporal ownership of directory publications and child assets."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from lifecycle.media_publication import MediaMutation, current_mutation, recover_media_operations
from lifecycle.media_work import MediaRecoveryRequired, require_resolved_media
from models import MediaOperationReceipt, Movie
from services.rclone import rclone_service


def tree_bytes(directory: Path) -> dict[str, str]:
    return {str(path.relative_to(directory)): path.read_bytes().hex()
            for path in sorted(directory.rglob("*")) if path.is_file()}


async def catalog_identity(engine) -> dict:
    async with AsyncSession(engine) as db:
        movie = await db.get(Movie, "m_1")
        return {name: getattr(movie, name) for name in
                ("video_url", "source_fingerprint", "quality", "availability", "subtitles", "audio_metadata", "skip_markers")}


async def crash_worker(mode: str, record: Path) -> None:
    """Die after real compensation side effects, before their durable acknowledgment."""
    if mode == "replay":
        payload = json.loads(record.read_text())
        settings.MEDIA_DIR, settings.TEMP_DIR = payload["media"], payload["temp"]
        settings.RCLONE_REMOTE_PATH = payload["remote"]
        settings.STORAGE_ENGINE = payload["storage"]
        engine = create_async_engine(payload["engine"])
        if payload.get("rclone_config"):
            rclone_service.config_path = Path(payload["rclone_config"])
        action = recover_media_operations
    elif mode == "cloud":
        from tests.media.test_cloud_publication import CloudPublicationRegression
        fixture = CloudPublicationRegression()
        await fixture.asyncSetUp()
        local = await fixture.prepare_local_fallback()
        engine = fixture.engine
        payload = {"directories": {str(local): tree_bytes(local), str(fixture.target): tree_bytes(fixture.target)},
                   "rclone_config": str(rclone_service.config_path)}
        action = lambda: fixture.run_queue(fail_commit=True)
    else:
        from tests.media.test_remediation import MediaRemediation
        fixture = MediaRemediation()
        await fixture.asyncSetUp()
        engine = fixture.engine
        payload = {"directories": {str(fixture.directory): tree_bytes(fixture.directory)}}
        action = lambda: fixture._ingest(cancel_before_commit=True)
    if mode != "replay":
        payload.update(root=str(fixture.root), media=settings.MEDIA_DIR, temp=settings.TEMP_DIR,
                       remote=settings.RCLONE_REMOTE_PATH, storage=settings.STORAGE_ENGINE,
                       engine=str(engine.url), catalog=await catalog_identity(engine))
        record.write_text(json.dumps(payload))
    real_persist = MediaMutation.persist

    def death(mutation):
        if mutation.compensated:
            for directory, expected in payload["directories"].items():
                assert tree_bytes(Path(directory)) == expected, "Compensation changed original tree bytes"
            os._exit(73)
        real_persist(mutation)

    with patch.object(db_module, "engine", engine), patch.object(MediaMutation, "persist", death):
        await action()
    raise AssertionError("Compensation crash point was not reached")


class OverlapRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-overlap-recovery-")
        self.root = Path(self.temporary.name)
        for name in ("MEDIA_DIR", "TEMP_DIR", "RCLONE_REMOTE_PATH", "STORAGE_ENGINE"):
            self.enterContext(patch.object(settings, name, getattr(settings, name)))
        settings.MEDIA_DIR, settings.TEMP_DIR = str(self.root / "media"), str(self.root / "temp")
        settings.STORAGE_ENGINE = "LOCAL"
        Path(settings.MEDIA_DIR).mkdir()
        Path(settings.TEMP_DIR).mkdir()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'test.db'}")
        self.enterContext(patch.object(db_module, "engine", self.engine))
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.target = Path(settings.MEDIA_DIR) / "Movies/Film"
        self.staging = Path(settings.TEMP_DIR) / "candidate"
        for directory, sentinel in ((self.target, "original"), (self.staging, "candidate")):
            (directory / ".metadata").mkdir(parents=True)
            (directory / "film.mp4").write_bytes(sentinel.encode())
            if directory == self.target:
                # Re-ingestion retains the original sidecar. A conflicting
                # candidate now correctly stops before publication; this suite
                # exercises temporal ownership after publication has begun.
                (directory / "subtitle_en.vtt").write_bytes((sentinel + " subtitle").encode())
            (directory / ".metadata/metadata.json").write_text(json.dumps({"sentinel": sentinel}))
        self.original = tree_bytes(self.target)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temporary.cleanup()

    async def interrupted_compensation(self, mutation):
        real_persist = mutation.persist

        def interrupt():
            if mutation.compensated:
                raise RuntimeError("Crash before compensated acknowledgment")
            real_persist()

        with patch.object(mutation, "persist", interrupt), self.assertRaisesRegex(RuntimeError, "acknowledgment"):
            await mutation.compensate()
        current_mutation.reset(mutation.token)

    async def test_post_publication_capture_cannot_replay_candidate_into_restored_original(self):
        mutation = await MediaMutation("m_1").__aenter__()
        mutation.publish_directory(str(self.staging), str(self.target))
        metadata = self.target / ".metadata/metadata.json"
        mutation.capture_local(str(metadata))
        self.assertEqual(mutation.entries[1]["directory_owner"], 0)
        metadata.write_text('{"sentinel":"candidate-fingerprint"}')
        await self.interrupted_compensation(mutation)
        self.assertEqual(tree_bytes(self.target), self.original)
        await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), self.original)
        self.assertFalse(mutation.root.exists())

    async def test_capture_before_directory_preserves_pre_operation_child_bytes(self):
        mutation = await MediaMutation("m_1").__aenter__()
        subtitle = self.target / "subtitle_en.vtt"
        mutation.capture_local(str(subtitle))
        subtitle.write_bytes(b"intermediate original subtitle mutation")
        mutation.publish_directory(str(self.staging), str(self.target))
        mutation.capture_local(str(self.target / ".metadata/metadata.json"))
        self.assertIsNone(mutation.entries[0]["directory_owner"])
        self.assertEqual(mutation.entries[2]["directory_owner"], 1)
        await self.interrupted_compensation(mutation)
        self.assertEqual(tree_bytes(self.target), self.original)
        await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), self.original)

    async def test_capture_during_building_is_independent_after_parent_restore(self):
        mutation = await MediaMutation("m_1").__aenter__()
        real_copytree = shutil.copytree

        def capture_then_copy(source, destination, *args, **kwargs):
            if Path(source) == self.staging:
                self.assertEqual(mutation.entries[0]["phase"], "building")
                subtitle = self.target / "subtitle_en.vtt"
                mutation.capture_local(str(subtitle))
                subtitle.write_bytes(b"changed during construction")
            return real_copytree(source, destination, *args, **kwargs)

        with patch("lifecycle.media_publication.shutil.copytree", capture_then_copy):
            mutation.publish_directory(str(self.staging), str(self.target))
        self.assertIsNone(mutation.entries[1]["directory_owner"])
        await self.interrupted_compensation(mutation)
        self.assertEqual(tree_bytes(self.target), self.original)
        await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), self.original)

    async def test_capture_during_prepared_is_independent_after_parent_restore(self):
        mutation = await MediaMutation("m_1").__aenter__()
        real_persist = mutation.persist
        captured = False

        def capture_when_prepared():
            nonlocal captured
            if mutation.entries and mutation.entries[0]["phase"] == "prepared" and not captured:
                captured = True
                subtitle = self.target / "subtitle_en.vtt"
                mutation.capture_local(str(subtitle))
                subtitle.write_bytes(b"changed before preservation")
            real_persist()

        with patch.object(mutation, "persist", capture_when_prepared):
            mutation.publish_directory(str(self.staging), str(self.target))
        self.assertIsNone(mutation.entries[1]["directory_owner"])
        await self.interrupted_compensation(mutation)
        self.assertEqual(tree_bytes(self.target), self.original)
        await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), self.original)

    async def test_original_absent_remains_absent_across_repeated_compensation(self):
        shutil.rmtree(self.target)
        mutation = await MediaMutation("m_1").__aenter__()
        mutation.publish_directory(str(self.staging), str(self.target))
        mutation.capture_local(str(self.target / ".metadata/metadata.json"))
        await self.interrupted_compensation(mutation)
        self.assertFalse(self.target.exists())
        await recover_media_operations()
        self.assertFalse(self.target.exists())
        self.assertFalse(mutation.root.exists())

    async def test_missing_child_captured_before_fresh_directory_needs_no_recreated_parent(self):
        shutil.rmtree(self.target)
        mutation = await MediaMutation("m_1").__aenter__()
        mutation.capture_local(str(self.target / ".metadata/metadata.json"))
        mutation.publish_directory(str(self.staging), str(self.target))
        self.assertFalse(mutation.entries[0]["existed"])
        self.assertIsNone(mutation.entries[0]["directory_owner"])
        await self.interrupted_compensation(mutation)
        await recover_media_operations()
        self.assertFalse(self.target.exists())
        self.assertFalse(mutation.root.exists())

    async def test_cloud_before_and_after_captures_restore_original_tree_in_temporal_order(self):
        if not rclone_service.executable():
            self.skipTest("rclone is unavailable")
        remote = self.root / "remote"
        cloud_target = remote / "Movies/Film"
        shutil.copytree(self.target, cloud_target)
        settings.RCLONE_REMOTE_PATH = f"isolated:{remote}"
        settings.STORAGE_ENGINE = "CLOUD"
        config = self.root / "rclone.conf"
        config.write_text("[isolated]\ntype = local\n")
        self.enterContext(patch.object(rclone_service, "config_path", config))
        mutation = await MediaMutation("m_1").__aenter__()
        await mutation.capture_cloud(f"{settings.RCLONE_REMOTE_PATH}/Movies/Film/subtitle_en.vtt")
        (cloud_target / "subtitle_en.vtt").write_bytes(b"intermediate cloud mutation")
        await mutation.publish_cloud_directory(str(self.staging), "Movies/Film")
        await mutation.capture_cloud(f"{settings.RCLONE_REMOTE_PATH}/Movies/Film/.metadata/metadata.json")
        self.assertIsNone(mutation.entries[0]["directory_owner"])
        self.assertEqual(mutation.entries[2]["directory_owner"], 1)
        (cloud_target / ".metadata/metadata.json").write_text('{"sentinel":"candidate-fingerprint"}')
        await self.interrupted_compensation(mutation)
        self.assertEqual(tree_bytes(cloud_target), self.original)
        await recover_media_operations()
        self.assertEqual(tree_bytes(cloud_target), self.original)

    async def test_nested_whole_directory_publication_is_rejected_before_asset_changes(self):
        mutation = await MediaMutation("m_1").__aenter__()
        mutation.publish_directory(str(self.staging), str(self.target))
        candidate = tree_bytes(self.target)
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            mutation.publish_directory(str(self.staging), str(self.target / ".metadata"))
        self.assertEqual(tree_bytes(self.target), candidate)
        self.assertEqual(len(mutation.entries), 1)
        await mutation.compensate()
        current_mutation.reset(mutation.token)
        await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), self.original)

    async def test_legacy_ambiguous_overlap_retains_all_evidence_without_mutating_original(self):
        mutation = await MediaMutation("m_1").__aenter__()
        mutation.publish_directory(str(self.staging), str(self.target))
        mutation.capture_local(str(self.target / ".metadata/metadata.json"))
        await self.interrupted_compensation(mutation)
        payload = json.loads((mutation.root / "operation.json").read_text())
        del payload["entries"][1]["directory_owner"]
        (mutation.root / "operation.json").write_text(json.dumps(payload))
        evidence = tree_bytes(mutation.root)
        with self.assertRaisesRegex(ValueError, "capture timing"):
            await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), self.original)
        self.assertEqual(tree_bytes(mutation.root), evidence)
        with self.assertRaises(MediaRecoveryRequired):
            require_resolved_media("m_1")

    async def test_legacy_child_before_directory_remains_recoverable(self):
        mutation = await MediaMutation("m_1").__aenter__()
        subtitle = self.target / "subtitle_en.vtt"
        mutation.capture_local(str(subtitle))
        del mutation.entries[0]["directory_owner"]
        subtitle.write_bytes(b"intermediate legacy mutation")
        mutation.publish_directory(str(self.staging), str(self.target))
        await self.interrupted_compensation(mutation)
        await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), self.original)

    async def test_partial_directory_compensation_failure_retains_retryable_originals(self):
        mutation = await MediaMutation("m_1").__aenter__()
        mutation.publish_directory(str(self.staging), str(self.target))
        mutation.capture_local(str(self.target / ".metadata/metadata.json"))
        real_replace = os.replace

        def fail_restore(source, destination):
            if str(source) == mutation.entries[0]["backup"]:
                raise OSError("Injected directory restore failure")
            return real_replace(source, destination)

        with patch("lifecycle.media_publication.os.replace", fail_restore), self.assertRaises(RuntimeError):
            await mutation.compensate()
        self.assertTrue(Path(mutation.entries[0]["backup"]).is_dir())
        current_mutation.reset(mutation.token)
        await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), self.original)

    async def test_invalid_owner_is_rejected_before_any_compensation(self):
        mutation = await MediaMutation("m_1").__aenter__()
        mutation.publish_directory(str(self.staging), str(self.target))
        mutation.capture_local(str(self.target / ".metadata/metadata.json"))
        mutation.entries[1]["directory_owner"] = True
        mutation.persist()
        candidate = tree_bytes(self.target)
        current_mutation.reset(mutation.token)
        with self.assertRaisesRegex(ValueError, "ownership"):
            await recover_media_operations()
        self.assertEqual(tree_bytes(self.target), candidate)
        self.assertTrue(Path(mutation.entries[0]["backup"]).is_dir())

    async def process_crash_replay(self, storage):
        if storage == "cloud" and not rclone_service.executable():
            self.skipTest("rclone is unavailable")
        record = self.root / "worker.json"
        fixture_root = None
        try:
            for mode in (storage, "replay"):
                result = await asyncio.to_thread(subprocess.run,
                    [sys.executable, "-m", "tests.media.test_overlap_recovery", "--worker", mode, str(record)],
                    capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 73, result.stderr.decode())
                payload = json.loads(record.read_text())
                fixture_root = Path(payload["root"])
                for directory, expected in payload["directories"].items():
                    self.assertEqual(tree_bytes(Path(directory)), expected)
                manifests = list(Path(payload["temp"]).glob("media-operations/*/operation.json"))
                self.assertEqual(len(manifests), 1)
                manifest = json.loads(manifests[0].read_text())
                self.assertEqual(manifest["phase"], "prepared")
            settings.MEDIA_DIR, settings.TEMP_DIR = payload["media"], payload["temp"]
            settings.STORAGE_ENGINE, settings.RCLONE_REMOTE_PATH = payload["storage"], payload["remote"]
            if payload.get("rclone_config"):
                self.enterContext(patch.object(rclone_service, "config_path", Path(payload["rclone_config"])))
            engine = create_async_engine(payload["engine"])
            try:
                self.assertEqual(await catalog_identity(engine), payload["catalog"])
                with patch.object(db_module, "engine", engine):
                    await recover_media_operations()
                for directory, expected in payload["directories"].items():
                    self.assertEqual(tree_bytes(Path(directory)), expected)
                self.assertEqual(await catalog_identity(engine), payload["catalog"])
                async with AsyncSession(engine) as db:
                    self.assertIsNone(await db.get(MediaOperationReceipt, manifest["operation_id"]))
                self.assertEqual(list(Path(payload["temp"]).glob("media-operations/*/operation.json")), [])
            finally:
                await engine.dispose()
        finally:
            if fixture_root:
                shutil.rmtree(fixture_root)

    async def test_actual_local_queue_survives_two_deaths_before_compensation_acknowledgment(self):
        await self.process_crash_replay("local")

    async def test_actual_cloud_queue_and_local_mirror_survive_two_compensation_deaths(self):
        await self.process_crash_replay("cloud")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        asyncio.run(crash_worker(sys.argv[2], Path(sys.argv[3])))
    else:
        unittest.main()
