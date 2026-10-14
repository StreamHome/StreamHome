"""Real commit-outcome and caller-finally regressions; disposable sources only."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from lifecycle import media_publication
from lifecycle.media_work import MediaRecoveryRequired, require_resolved_media
from models import DownloadTask, MediaOperationReceipt, Movie
from services import media_updates
from services.ingest_preview import IngestPreviewService
from services.media_source import clear_cloud_object_cache, playback_source_fingerprint, resolve_media_source
from tests.media import test_cloud_publication as cloud_fixture
from tests.media import test_hevc_mutation as hevc_fixture


class AcceptanceOutcomes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-acceptance-outcomes-")
        self.root = Path(self.temporary.name)
        self.previous_paths = settings.MEDIA_DIR, settings.TEMP_DIR, settings.STORAGE_ENGINE
        settings.MEDIA_DIR = str(self.root / "media")
        settings.TEMP_DIR = str(self.root / "temp")
        settings.STORAGE_ENGINE = "LOCAL"
        Path(settings.TEMP_DIR).mkdir()
        directory = Path(settings.MEDIA_DIR) / "Movies" / "Film"
        directory.mkdir(parents=True)
        (directory / "film.mp4").write_bytes(b"original-video")
        self.metadata = directory / ".metadata" / "metadata.json"
        self.metadata.parent.mkdir()
        self.metadata.write_text(json.dumps({"catalog_source": "server", "skip_markers": {"intro": []}}))
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'acceptance.db'}")

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()

        self.engine_patch = patch.object(db_module, "engine", self.engine)
        self.engine_patch.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(self.engine) as session:
            session.add(Movie(id="m_1", title="Film", description="", thumbnail_url="",
                video_url="/media/Movies/Film/film.mp4", duration="1s", release_year=2026,
                skip_markers_str='{"intro":[]}'))
            await session.commit()
        self.markers = {"intro": [{"start": 1, "end": 2}]}

    async def asyncTearDown(self):
        self.engine_patch.stop()
        await self.engine.dispose()
        settings.MEDIA_DIR, settings.TEMP_DIR, settings.STORAGE_ENGINE = self.previous_paths
        self.temporary.cleanup()

    async def exercise(self, *, before=False, after=False, after_cancel=False,
                       unknown=False, cancel=False, cancel_reader=False,
                       mismatch=False, close_failure=False):
        entered, release = asyncio.Event(), asyncio.Event()
        operations = []
        original_enter = media_publication.MediaMutation.__aenter__
        original_reader = media_publication._read_acceptance_receipt

        async def observe_enter(mutation):
            result = await original_enter(mutation)
            operations.append(mutation.operation_id)
            return result

        async def reader(engine, operation_id):
            self.assertIs(engine, self.engine, "Outcome must use the exact committing engine")
            if cancel_reader:
                entered.set()
                await release.wait()
            if unknown:
                raise OSError("Injected acceptance reader outage")
            return await original_reader(engine, operation_id)

        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            if before:
                @event.listens_for(session.sync_session, "before_commit")
                def reject(_):
                    raise RuntimeError("Injected failure before SQLite commit")
            if after:
                @event.listens_for(session.sync_session, "after_commit")
                def reject_ack(_):
                    if after_cancel:
                        raise asyncio.CancelledError
                    raise RuntimeError("Injected failure after SQLite commit")
            original_commit = session.commit

            async def commit():
                if cancel:
                    entered.set()
                    await release.wait()
                if mismatch:
                    async with AsyncSession(self.engine) as conflicting:
                        conflicting.add(MediaOperationReceipt(operation_id=operations[0], media_id="m_other"))
                        await conflicting.commit()
                await original_commit()

            async def fail_close():
                raise OSError("Injected acceptance transaction release outage")

            with patch.object(media_publication.MediaMutation, "__aenter__", observe_enter), patch.object(media_publication, "_read_acceptance_receipt", reader), patch.object(session, "commit", commit):
                if close_failure:
                    close_patch = patch.object(session, "close", fail_close)
                    close_patch.start()
                task = asyncio.create_task(media_updates.update_skip_markers("m_1", self.markers, session))
                if cancel or cancel_reader:
                    await asyncio.wait_for(entered.wait(), 5)
                    task.cancel()
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done(), "Cancellation must wait for the actual acceptance decision")
                    release.set()
                error = None
                try:
                    result = await asyncio.wait_for(task, 5)
                    self.assertEqual(result["skipMarkers"], self.markers)
                except BaseException as caught:
                    error = caught
                finally:
                    if close_failure:
                        close_patch.stop()

        async with AsyncSession(self.engine) as session:
            movie = await session.get(Movie, "m_1")
            catalog_markers = movie.skip_markers
            receipt = await session.get(MediaOperationReceipt, operations[0])
            accepted = receipt is not None and receipt.media_id == "m_1"
        self.assertEqual(accepted, after or (not before and not mismatch))
        portable_markers = json.loads(self.metadata.read_text())["skip_markers"]
        manifests = list((Path(settings.TEMP_DIR) / "media-operations").glob("*/operation.json"))
        pending = unknown or mismatch or close_failure
        self.assertEqual(bool(manifests), pending)
        if pending:
            self.assertIsInstance(error, MediaRecoveryRequired)
            self.assertEqual(portable_markers, self.markers, "Unknown outcomes must never compensate potentially accepted files")
            self.assertTrue((manifests[0].parent / "asset-0").is_file(), "Original evidence must remain intact")
            with self.assertRaises(MediaRecoveryRequired):
                require_resolved_media("m_1")
            if mismatch:
                with self.assertRaisesRegex(RuntimeError, "receipt mismatch"):
                    await media_publication.recover_media_operations()
                self.assertTrue(manifests[0].is_file())
                self.assertEqual(json.loads(self.metadata.read_text())["skip_markers"], self.markers)
            else:
                await media_publication.recover_media_operations()
                self.assertFalse(manifests[0].exists())
                self.assertEqual(json.loads(self.metadata.read_text())["skip_markers"], catalog_markers)
                require_resolved_media("m_1")
        else:
            self.assertEqual(portable_markers, catalog_markers)
            self.assertEqual(portable_markers, self.markers if accepted else {"intro": []})
            if after_cancel or ((cancel or cancel_reader) and not (before or after)):
                self.assertIsInstance(error, asyncio.CancelledError)
            elif before or after:
                self.assertIsInstance(error, RuntimeError)
            else:
                self.assertIsNone(error)

    async def test_normal_acceptance_remains_coherent(self):
        await self.exercise()

    async def test_before_commit_hook_failure_restores_original(self):
        await self.exercise(before=True)

    async def test_after_commit_hook_failure_preserves_accepted_media(self):
        await self.exercise(after=True)

    async def test_after_commit_hook_cancellation_preserves_accepted_media(self):
        await self.exercise(after=True, after_cancel=True)

    async def test_repeated_cancel_during_commit_acknowledgment_fault_preserves_acceptance(self):
        await self.exercise(after=True, cancel=True)

    async def test_repeated_cancel_during_receipt_resolution_waits_for_acceptance(self):
        await self.exercise(after=True, cancel_reader=True)

    async def test_unknown_precommit_outcome_retained_until_restart_compensation(self):
        await self.exercise(before=True, unknown=True)

    async def test_unknown_accepted_outcome_retained_until_restart_cleanup(self):
        await self.exercise(after=True, unknown=True)

    async def test_receipt_lookup_uses_committing_engine_when_global_engine_differs(self):
        unrelated = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'unrelated.db'}")
        try:
            async with unrelated.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            with patch.object(db_module, "engine", unrelated):
                await self.exercise(after=True)
        finally:
            await unrelated.dispose()

    async def test_mismatched_receipt_keeps_evidence_and_rejects_startup(self):
        await self.exercise(mismatch=True)

    async def test_transaction_release_failure_keeps_accepted_evidence_for_restart(self):
        await self.exercise(after=True, close_failure=True)

    async def test_external_transaction_bind_rejected_before_acceptance(self):
        async with self.engine.connect() as connection:
            async with AsyncSession(connection) as session:
                with self.assertRaisesRegex(ValueError, "engine-bound"):
                    await media_updates.update_skip_markers("m_1", self.markers, session)
        self.assertEqual(json.loads(self.metadata.read_text())["skip_markers"], {"intro": []})
        self.assertFalse(list((Path(settings.TEMP_DIR) / "media-operations").glob("*/operation.json")))


class ActualQueueAcceptanceOutcomes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = cloud_fixture.CloudPublicationRegression()
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def completed_preview(self):
        preview = IngestPreviewService()
        preview.root = self.fixture.root / "preview"
        preview.prepare("cloud-queue", 0.5)
        preview.playlist_path("cloud-queue").write_text("#EXTM3U\n#EXTINF:0.5,\nsegment_0.m4s\n#EXT-X-ENDLIST\n")
        preview.mark_complete("cloud-queue")
        self.assertEqual(preview.status("cloud-queue")["phase"], "ready")
        return preview

    async def exercise_queue(self, *, local_fallback=False, unknown=False):
        fixture = self.fixture
        local = await fixture.prepare_local_fallback() if local_fallback else None
        hook_calls = []
        preview = self.completed_preview()

        # after_commit runs before pending objects leave their retained strong
        # transaction snapshot; session.new is empty by this point.
        def before_commit(session):
            session.info["media_acceptance"] = any(isinstance(item, MediaOperationReceipt) for item in session.new)

        def after_commit(session):
            if session.info.pop("media_acceptance", False):
                hook_calls.append(True)
                raise RuntimeError("Injected actual queue after_commit failure")

        event.listen(AsyncSession.sync_session_class, "before_commit", before_commit)
        event.listen(AsyncSession.sync_session_class, "after_commit", after_commit)
        try:
            with patch.object(cloud_fixture.queue, "ingest_preview_service", preview):
                if unknown:
                    with patch.object(media_publication, "_read_acceptance_receipt", side_effect=OSError("Injected queue receipt reader outage")):
                        await fixture.run_queue()
                else:
                    await fixture.run_queue()
        finally:
            event.remove(AsyncSession.sync_session_class, "before_commit", before_commit)
            event.remove(AsyncSession.sync_session_class, "after_commit", after_commit)
        self.assertEqual(len(hook_calls), 1, "Actual acceptance event must trigger the fault")
        self.assertEqual(preview.status("cloud-queue")["phase"], "ready", "Late accepted failure must preserve the completed preview")
        self.assertEqual(preview.status("cloud-queue")["error_code"], "")
        async with AsyncSession(fixture.engine) as session:
            task = await session.get(DownloadTask, "cloud-queue")
            movie = await session.get(Movie, "m_1")
            receipts = (await session.exec(select(MediaOperationReceipt))).all()
            self.assertEqual(task.status, "COMPLETED", "Late acknowledgment failure must not overwrite durable completion")
            self.assertEqual(movie.quality, "720p")
            self.assertEqual(len(receipts), 1)
            filename = Path(movie.video_url).name
            fingerprint = movie.source_fingerprint
        self.assertNotEqual(fingerprint, "old-cloud-fingerprint")
        self.assertTrue((fixture.target / filename).is_file())
        self.assertNotEqual((fixture.target / filename).read_bytes(), b"healthy-old-cloud-video")
        self.assertEqual(json.loads((fixture.target / ".metadata/metadata.json").read_text())["quality"], "720p")
        if local is not None:
            self.assertEqual((local / filename).read_bytes(), (fixture.target / filename).read_bytes())
            self.assertEqual(json.loads((local / ".metadata/metadata.json").read_text())["source_fingerprint"], fingerprint)
        manifests = list((Path(settings.TEMP_DIR) / "media-operations").glob("*/operation.json"))
        self.assertEqual(bool(manifests), unknown)
        if unknown:
            with self.assertRaises(MediaRecoveryRequired):
                require_resolved_media("m_1")
            await media_publication.recover_media_operations()
            self.assertFalse(manifests[0].exists())
            self.assertTrue((fixture.target / filename).is_file())

    async def test_actual_cloud_queue_after_commit_error_preserves_acceptance(self):
        await self.exercise_queue()

    async def test_actual_queue_after_commit_error_preserves_local_and_cloud_mirror(self):
        await self.exercise_queue(local_fallback=True)

    async def test_actual_queue_unknown_accepted_outcome_retains_restart_evidence(self):
        await self.exercise_queue(unknown=True)

    async def test_actual_queue_cancel_during_successful_commit_preserves_completed_preview(self):
        preview = self.completed_preview()
        with patch.object(cloud_fixture.queue, "ingest_preview_service", preview):
            await self.fixture.run_queue(cancel_during_commit=True)
        async with AsyncSession(self.fixture.engine) as session:
            self.assertEqual((await session.get(DownloadTask, "cloud-queue")).status, "COMPLETED")
        self.assertEqual(preview.status("cloud-queue")["phase"], "ready")
        self.assertEqual(preview.status("cloud-queue")["error_code"], "")

    async def test_actual_queue_rejected_acceptance_marks_preview_and_task_failed(self):
        preview = self.completed_preview()
        with patch.object(cloud_fixture.queue, "ingest_preview_service", preview):
            await self.fixture.run_queue(fail_commit=True)
        async with AsyncSession(self.fixture.engine) as session:
            task = await session.get(DownloadTask, "cloud-queue")
            self.assertEqual(task.status, "FAILED")
        self.assertEqual(preview.status("cloud-queue")["phase"], "error")
        self.assertEqual(preview.status("cloud-queue")["error_code"], "QUEUE_INTERNAL_ERROR")
        await self.fixture.assert_original()

    async def test_actual_same_path_cloud_rejection_immediately_resolves_restored_identity(self):
        fixture = self.fixture
        original_file = fixture.target / "film.mp4"
        restored_file = fixture.target / "Film_2026.mp4"
        original_file.rename(restored_file)
        catalog_url = f"/media/Movies/{fixture.target.name}/{restored_file.name}"
        clear_cloud_object_cache()
        original = await resolve_media_source(catalog_url, require_cloud=True)
        original_fingerprint = playback_source_fingerprint(original, [])
        self.assertFalse(original.local_exists, "Regression must exercise a cloud-only source")
        async with AsyncSession(fixture.engine) as session:
            movie = await session.get(Movie, "m_1")
            movie.video_url = catalog_url
            movie.source_fingerprint = original_fingerprint
            session.add(movie)
            await session.commit()
        await fixture.run_queue(fail_commit=True)
        self.assertEqual(restored_file.read_bytes(), b"healthy-old-cloud-video")
        async with AsyncSession(fixture.engine) as session:
            movie = await session.get(Movie, "m_1")
            self.assertEqual(movie.video_url, catalog_url)
            self.assertEqual(movie.source_fingerprint, original_fingerprint)
        # Do not clear the cache or wait for its TTL here: normal readers must
        # immediately observe the restored bytes rather than the rejected stat.
        immediate = await resolve_media_source(catalog_url, require_cloud=True)
        self.assertEqual(immediate.cloud_size, restored_file.stat().st_size)
        self.assertEqual(immediate.cloud_identity, original.cloud_identity)
        self.assertEqual(playback_source_fingerprint(immediate, []), original_fingerprint)


class ActualHEVCAcceptanceOutcomes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = hevc_fixture.HEVCMutationTests()
        await self.fixture.asyncSetUp()
        self.engine_patch = patch.object(db_module, "engine", self.fixture.engine)
        self.engine_patch.start()

    async def asyncTearDown(self):
        self.engine_patch.stop()
        await self.fixture.asyncTearDown()

    async def exercise_hevc(self, *, unknown=False):
        fixture = self.fixture
        fixture.install_worker()
        hook_calls = []

        def before_commit(session):
            session.info["media_acceptance"] = any(isinstance(item, MediaOperationReceipt) for item in session.new)

        def after_commit(session):
            if session.info.pop("media_acceptance", False):
                hook_calls.append(True)
                raise RuntimeError("Injected actual HEVC after_commit failure")

        event.listen(AsyncSession.sync_session_class, "before_commit", before_commit)
        event.listen(AsyncSession.sync_session_class, "after_commit", after_commit)
        try:
            if unknown:
                with patch.object(media_publication, "_read_acceptance_receipt", side_effect=OSError("Injected HEVC receipt reader outage")):
                    await asyncio.wait_for(fixture.worker._worker_loop(), 5)
            else:
                await asyncio.wait_for(fixture.worker._worker_loop(), 5)
        finally:
            event.remove(AsyncSession.sync_session_class, "before_commit", before_commit)
            event.remove(AsyncSession.sync_session_class, "after_commit", after_commit)
        self.assertEqual(len(hook_calls), 1)
        self.assertEqual(fixture.video.read_bytes(), hevc_fixture.OPTIMIZED)
        if unknown:
            manifests = list(fixture.temp.glob("media-operations/*/operation.json"))
            self.assertEqual(len(manifests), 1)
            with self.assertRaises(MediaRecoveryRequired):
                require_resolved_media(fixture.media_id)
            await media_publication.recover_media_operations()
        await fixture.assert_state(accepted=True)

    async def test_actual_hevc_after_commit_error_preserves_accepted_video_and_catalog(self):
        await self.exercise_hevc()

    async def test_actual_hevc_unknown_accepted_outcome_retains_restart_evidence(self):
        await self.exercise_hevc(unknown=True)


if __name__ == "__main__":
    unittest.main()
