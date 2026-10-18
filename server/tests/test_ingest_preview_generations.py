"""Immutable preview attempts, protected legacy runs and disposable migrations."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import db as database
from config import settings
from db import get_session
from lifecycle.ingestion_attempts import require_drained_writer
from models import AuthSession, DownloadTask, Episode, Movie, PlaybackRun, Profile, User
from routes import playback
from routes import queue as queue_routes
from routes.auth import get_current_user
from services import state
from services.ingest_preview import IngestPreviewError, IngestPreviewService, ingest_preview_service
from services.ingestion_errors import IngestionFailure
from services.queue import DownloadQueueManager


class IngestPreviewGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="preview-generations-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'test.db'}")
        self.addAsyncCleanup(self.engine.dispose)
        self.patchers = [patch.object(database, "engine", self.engine),
                         patch("services.queue.engine", self.engine),
                         patch.object(ingest_preview_service, "root", self.root / "preview"),
                         patch.object(settings, "MEDIA_DIR", str(self.root / "media"))]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.task = DownloadTask(id="preview-parent", tmdb_id=6801, media_type="movie",
            video_url="https://synthetic.invalid/video", status="DOWNLOADING", created_at="synthetic")
        self.user = User(id=6801, email="preview@example.test", password_hash="unused")
        self.auth = AuthSession(id="preview-auth", user_id=6801, created_at=time.time(),
            last_seen_at=time.time(), expires_at=time.time() + 3600)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(self.task)
            session.add(self.user)
            session.add(self.auth)
            session.add(Profile(id="preview-profile", name="Preview"))
            session.add(Movie(id="m_6801", tmdb_id=6801, title="Preview", description="Synthetic",
                thumbnail_url="", video_url="/media/missing.mp4", duration="1m", release_year=2026,
                availability="processing", preview_task_id=self.task.id))
            await session.commit()
        async def sessions():
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                yield session
        app = FastAPI()
        @app.middleware("http")
        async def attach(request: Request, call_next):
            request.state.auth_session = self.auth
            return await call_next(request)
        app.include_router(playback.router)
        app.dependency_overrides[get_session] = sessions
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://preview.test")
        self.addAsyncCleanup(self.client.aclose)

    def write_preview(self, generation, payload):
        root = ingest_preview_service.prepare(generation, 16)
        (root / "playlist.m3u8").write_text('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:16,\nsegment_00000.m4s\n#EXT-X-ENDLIST\n')
        (root / "init.mp4").write_bytes(b"init-" + payload)
        (root / "segment_00000.m4s").write_bytes(payload)
        (root / "subtitle_en.vtt").write_text("WEBVTT\nRetained caption")
        ingest_preview_service.mark_complete(generation)
        return root

    async def create_run(self):
        response = await self.client.post("/api/playback/runs", json={"movieId": "m_6801", "profileId": "preview-profile"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def segment(self, run):
        manifest = await self.client.get(run["manifestUrl"])
        self.assertEqual(manifest.status_code, 200, manifest.text)
        url = next(line for line in manifest.text.splitlines() if "segment_00000.m4s" in line)
        return await self.client.get(url)

    async def test_legacy_null_run_keeps_original_generation_after_parent_retry_and_final_handoff(self):
        old_root = self.write_preview("preview-parent", b"old-generation")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in old_root.iterdir()}
        old = await self.create_run()
        async with AsyncSession(self.engine) as session:
            run = await session.get(PlaybackRun, old["runId"])
            self.assertIsNone(run.source_preview_id)
        generation = ingest_preview_service.next_generation("preview-parent", "preview-parent")
        self.write_preview(generation, b"new-generation")
        async with AsyncSession(self.engine) as session:
            task = await session.get(DownloadTask, "preview-parent")
            task.current_preview_id = generation
            session.add(task)
            await session.commit()
        new = await self.create_run()
        self.assertEqual((await self.segment(old)).content, b"old-generation")
        self.assertEqual((await self.segment(new)).content, b"new-generation")
        async with AsyncSession(self.engine) as session:
            new_run = await session.get(PlaybackRun, new["runId"])
            self.assertEqual(new_run.source_task_id, "preview-parent")
            self.assertEqual(new_run.source_preview_id, generation)
            movie = await session.get(Movie, "m_6801")
            movie.preview_task_id = None
            movie.availability = "available"
            session.add(movie)
            await session.commit()
        refreshed = await self.client.get(f"/api/playback/runs/{old['runId']}")
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["streamBaseUrl"], old["streamBaseUrl"])
        self.assertEqual((await self.segment(old)).content, b"old-generation")
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in old_root.iterdir()})
        # A different signed-in session cannot borrow the stable run URL.
        previous = self.auth
        self.auth = AuthSession(id="other-preview-auth", user_id=6801, created_at=time.time(),
            last_seen_at=time.time(), expires_at=time.time() + 3600)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(self.auth)
            await session.commit()
        denied = await self.client.get(old["streamBaseUrl"] + "/preview/segment_00000.m4s")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.auth = previous

    async def test_restart_and_repeated_retry_never_reuse_or_remove_retained_generations(self):
        first = ingest_preview_service.next_generation("preview-parent")
        self.assertEqual(first, "preview-parent")
        root = self.write_preview(first, b"retained")
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        restarted = IngestPreviewService()
        restarted.root = ingest_preview_service.root
        generations = {first}
        for _ in range(3):
            generation = restarted.next_generation("preview-parent", first)
            self.assertNotIn(generation, generations)
            generations.add(generation)
            self.write_preview(generation, b"another-attempt")
            restarted.mark_error(generation, "SOURCE_TIMEOUT", "Synthetic retry exhausted")
        self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
        with self.assertRaises(IngestPreviewError):
            restarted.prepare(first, 16)
        self.assertNotEqual(restarted.next_generation("preview-parent"), first)

    async def test_startup_recovery_marks_only_current_generation_and_keeps_legacy_fallback(self):
        old = self.write_preview("preview-parent", b"earlier-viewer")
        before = {p.name: p.read_bytes() for p in old.iterdir()}
        generation = ingest_preview_service.next_generation("preview-parent", "preview-parent")
        current = self.write_preview(generation, b"interrupted-writer")
        async with AsyncSession(self.engine) as session:
            task = await session.get(DownloadTask, "preview-parent")
            task.current_preview_id = generation
            session.add(task)
            session.add(DownloadTask(id="legacy-recovery", tmdb_id=6802, media_type="movie",
                video_url="https://synthetic.invalid/legacy", status="MERGING", created_at="synthetic"))
            await session.commit()
        self.write_preview("legacy-recovery", b"legacy-interrupted")
        # Execute the actual helper body with disposable dependencies, without
        # importing unrelated application/bootstrap or starting its lifespan.
        source = Path(__file__).resolve().parents[1] / "application.py"
        tree = ast.parse(source.read_text())
        helper = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_recover_interrupted_ingestion")
        namespace = {"AsyncSession": AsyncSession, "engine": self.engine, "select": select,
            "DownloadTask": DownloadTask, "Movie": Movie, "Episode": Episode,
            "ingest_preview_service": ingest_preview_service}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source), "exec"), namespace)
        await namespace[helper.name]()
        self.assertEqual(before, {p.name: p.read_bytes() for p in old.iterdir()})
        self.assertEqual(ingest_preview_service.status(generation)["error_code"], "INGESTION_INTERRUPTED")
        self.assertEqual(ingest_preview_service.status("legacy-recovery")["error_code"], "INGESTION_INTERRUPTED")
        self.assertEqual((current / "segment_00000.m4s").read_bytes(), b"interrupted-writer")
        async with AsyncSession(self.engine) as session:
            task = await session.get(DownloadTask, "preview-parent")
            self.assertEqual(task.status, "FAILED")
            self.assertEqual(task.current_preview_id, generation)

    async def test_late_failure_does_not_change_new_attempt_or_accepted_task(self):
        old = self.write_preview("preview-parent", b"retained")
        generation = ingest_preview_service.next_generation("preview-parent", "preview-parent")
        new = self.write_preview(generation, b"current")
        before = {p.name: p.read_bytes() for p in new.iterdir()}
        async with AsyncSession(self.engine) as session:
            task = await session.get(DownloadTask, "preview-parent")
            task.current_preview_id = generation
            session.add(task)
            await session.commit()
        await DownloadQueueManager()._record_task_failure("preview-parent", IngestionFailure("SOURCE_TIMEOUT", "old"), "", preview_id="preview-parent")
        # Detached callbacks retain their captured generation; they cannot mark
        # the next attempt complete, failed or further buffered.
        ingest_preview_service.update_progress("preview-parent", processed_seconds=8,
            speed_multiplier=1, duration_seconds=16)
        ingest_preview_service.mark_error("preview-parent", "SOURCE_TIMEOUT", "old")
        ingest_preview_service.mark_complete("preview-parent")
        self.assertEqual(before, {p.name: p.read_bytes() for p in new.iterdir()})
        async with AsyncSession(self.engine) as session:
            task = await session.get(DownloadTask, "preview-parent")
            self.assertEqual(task.status, "DOWNLOADING")
            task.status = "COMPLETED"
            session.add(task)
            await session.commit()
        await DownloadQueueManager()._record_task_failure("preview-parent", IngestionFailure("SOURCE_TIMEOUT", "late"), "", preview_id=generation)
        self.assertEqual(before, {p.name: p.read_bytes() for p in new.iterdir()})
        self.assertEqual((old / "segment_00000.m4s").read_bytes(), b"retained")

    async def test_cancel_marks_only_drained_current_generation_before_deleting_task(self):
        old = self.write_preview("preview-parent", b"earlier-viewer")
        before = {p.name: p.read_bytes() for p in old.iterdir()}
        generation = ingest_preview_service.next_generation("preview-parent", "preview-parent")
        current = self.write_preview(generation, b"cancelled-attempt")
        async with AsyncSession(self.engine) as session:
            task = await session.get(DownloadTask, "preview-parent")
            task.current_preview_id = generation
            session.add(task)
            await session.commit()
        with patch.object(queue_routes, "engine", self.engine), patch.object(
            queue_routes.queue_manager, "cancel_task", AsyncMock(return_value=(True, True))
        ) as cancel:
            result = await queue_routes._delete_download_admitted("preview-parent")
        cancel.assert_awaited_once_with("preview-parent")
        self.assertEqual(result["status"], "success")
        self.assertEqual(before, {p.name: p.read_bytes() for p in old.iterdir()})
        self.assertEqual(ingest_preview_service.status(generation)["error_code"], "PREVIEW_CANCELLED")
        self.assertEqual((current / "segment_00000.m4s").read_bytes(), b"cancelled-attempt")
        async with AsyncSession(self.engine) as session:
            self.assertIsNone(await session.get(DownloadTask, "preview-parent"))

    async def test_unresolved_native_writer_blocks_shared_workspace_reuse(self):
        previous = SimpleNamespace(returncode=None)
        with patch.dict(state.ACTIVE_PROCESSES, {"preview-parent": previous}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "still active"):
                require_drained_writer("preview-parent")
            self.assertIs(state.ACTIVE_PROCESSES["preview-parent"], previous)
            previous.returncode = 0
            require_drained_writer("preview-parent")
            self.assertNotIn("preview-parent", state.ACTIVE_PROCESSES)

    async def test_real_native_child_must_exit_and_be_reaped_before_handoff(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with patch.dict(state.ACTIVE_PROCESSES, {"preview-parent": process}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "still active"):
                    require_drained_writer("preview-parent")
                self.assertIs(state.ACTIVE_PROCESSES["preview-parent"], process)
                process.wait(timeout=5)
                require_drained_writer("preview-parent")
                self.assertEqual(process.returncode, 0)
                self.assertNotIn("preview-parent", state.ACTIVE_PROCESSES)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()

    async def test_unadmitted_worker_preserves_prior_staging_preview_and_registry(self):
        old = self.write_preview("preview-parent", b"existing-viewer")
        preview_before = {p.name: p.read_bytes() for p in old.iterdir()}
        temporary = self.root / "temp"
        staging = temporary / "ingestion" / "preview-parent"
        staging.mkdir(parents=True)
        (staging / "writer.mp4").write_bytes(b"active-writer-output")
        (staging / "subtitle_en.vtt").write_bytes(b"WEBVTT\nRetained")
        before = {str(p.relative_to(temporary)): p.read_bytes() for p in temporary.rglob("*") if p.is_file()}
        child = SimpleNamespace(returncode=None)
        manager = DownloadQueueManager()
        manager.active_tasks.add("preview-parent")
        with patch.object(settings, "TEMP_DIR", str(temporary)), patch.dict(
            state.ACTIVE_PROCESSES, {"preview-parent": child}, clear=True
        ), patch("services.queue.write_task_diagnostics", return_value="synthetic-diagnostic"):
            await manager._process_admitted_task("preview-parent")
            self.assertIs(state.ACTIVE_PROCESSES["preview-parent"], child)
            self.assertIn("preview-parent", manager.active_tasks)
        self.assertEqual(before, {str(p.relative_to(temporary)): p.read_bytes()
            for p in temporary.rglob("*") if p.is_file()})
        self.assertEqual(preview_before, {p.name: p.read_bytes() for p in old.iterdir()})
        async with AsyncSession(self.engine) as session:
            self.assertEqual((await session.get(DownloadTask, "preview-parent")).status, "DOWNLOADING")

    async def test_additive_schema_migration_is_idempotent_and_preserves_legacy_binding(self):
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql("ALTER TABLE downloadtask DROP COLUMN current_preview_id")
            await connection.exec_driver_sql("ALTER TABLE playbackrun DROP COLUMN source_preview_id")
        with patch.object(settings, "db_path", str(self.root / "test.db")):
            await database.init_db(allow_migration_backups=False, allow_external_config_mutation=False)
            await database.init_db(allow_migration_backups=False, allow_external_config_mutation=False)
        async with AsyncSession(self.engine) as session:
            task = await session.get(DownloadTask, "preview-parent")
            self.assertIsNone(task.current_preview_id)
            self.assertEqual(task.video_url, "https://synthetic.invalid/video")
