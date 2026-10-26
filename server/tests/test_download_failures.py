"""Run through tests/run_isolated.py; synthetic sources, credentials and databases only."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db
from config import settings
from db import get_session
from models import DownloadTask, IntegrationCredential
from routes import queue as api
from services import queue as worker
from services.download_failures import encode_failure, public_failure
from services.ingestion_errors import IngestionFailure
from services.media_probe import probe_media_stream
from services.source_denial import denial_context, DENIAL_LIMIT
from services.integration_auth import integration_token_hash
from tests.services import test_controlled_source


class FailurePresentationTests(unittest.TestCase):
    def test_private_messages_are_never_public_and_legacy_does_not_invent_context(self):
        secret = "https://source.invalid/private-token?signature=secret Cookie: synthetic"
        failure = IngestionFailure("SOURCE_FORBIDDEN", secret, False, "/private/diagnostics",
                                   "source_preparation", "video", 403, "cloudflare", "1005")
        saved = encode_failure(failure)
        result = public_failure("FAILED", saved, failure.display)
        self.assertEqual(result["provider_code"], "1005")
        for value in (saved, json.dumps(result)):
            for forbidden in ("private-token", "signature", "Cookie", "/private", "synthetic"):
                self.assertNotIn(forbidden, value)
        old = public_failure("FAILED", None, failure.display)
        self.assertEqual(old["code"], "SOURCE_FORBIDDEN")
        self.assertNotIn("http_status", old)
        self.assertNotIn("provider_code", old)

    def test_malformed_unknown_and_inconsistent_context_is_safe(self):
        for raw in (None, "{", "[]", "x" * 4096, '{"version":true}', '{"version":2}',
                    '{"version":1,"code":[]}', '{"version":1,"code":"https://secret.invalid"}'):
            with self.subTest(raw=str(raw)[:30]):
                result = public_failure("FAILED", raw, "private arbitrary error")
                self.assertEqual(result["code"], "INGESTION_FAILED")
        raw = json.dumps({"version": 1, "code": "SOURCE_FORBIDDEN", "stage": [],
                          "input_role": "secret", "http_status": True,
                          "provider": "cloudflare", "provider_code": "1005"})
        result = public_failure("FAILED", raw, None)
        self.assertEqual(set(result), {"code", "message", "guidance"})
        raw = encode_failure(IngestionFailure("SOURCE_NOT_FOUND", "private", http_status=403,
                                              provider="cloudflare", provider_code="1005"))
        self.assertNotIn("provider", public_failure("FAILED", raw, None))
        for status in ("PENDING", "DOWNLOADING", "COMPLETED"):
            self.assertIsNone(public_failure(status, raw, "private"))

    def test_known_failures_keep_safe_specific_explanations(self):
        for code, phrase in (("SOURCE_UNAUTHORIZED", "authentication"), ("SOURCE_NOT_FOUND", "not found"),
                             ("INSUFFICIENT_STORAGE", "insufficient space"), ("SOURCE_RATE_LIMITED", "limiting"),
                             ("storage_cloud_budget_exceeded", "storage capacity")):
            result = public_failure("FAILED", encode_failure(IngestionFailure(code, "private")), None)
            self.assertEqual(result["code"], code)
            self.assertIn(phrase, result["message"])


class FailureDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "database.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.app = FastAPI()
        self.app.include_router(api.router)
        async def session():
            async with AsyncSession(self.engine) as session:
                yield session
        self.app.dependency_overrides[get_session] = session
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        self.temporary.cleanup()

    def task(self, identifier, status="FAILED", **extra):
        return DownloadTask(id=identifier, tmdb_id=1, title="Synthetic", media_type="movie",
                            video_url="https://source.invalid/private-source", status=status,
                            created_at="2026-10-26T12:00:00", **extra)

    async def test_additive_migration_preserves_legacy_rows_and_repeated_initialization(self):
        async with AsyncSession(self.engine) as session:
            session.add(self.task("old", error_message="SOURCE_FORBIDDEN — private", headers_str='{"Cookie":"synthetic"}'))
            await session.commit()
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql("ALTER TABLE downloadtask DROP COLUMN failure_details_str")
        with patch.object(db, "engine", self.engine), patch.object(settings, "db_path", str(self.path)), patch.object(settings, "API_BEARER_TOKEN", ""):
            for _ in range(2):
                await db.init_db(allow_migration_backups=False, allow_external_config_mutation=False)
        async with AsyncSession(self.engine) as session:
            task = await session.get(DownloadTask, "old")
            self.assertIsNone(task.failure_details_str)
            self.assertEqual(task.headers_str, '{"Cookie":"synthetic"}')
            self.assertEqual(api.task_failure(task)["code"], "SOURCE_FORBIDDEN")

    async def test_failure_persistence_and_existing_generation_completion_guards(self):
        async with AsyncSession(self.engine) as session:
            session.add(self.task("new", "DOWNLOADING", current_preview_id="v2"))
            session.add(self.task("done", "COMPLETED"))
            await session.commit()
        failure = IngestionFailure("SOURCE_FORBIDDEN", "private", stage="source_preparation", input_role="video", http_status=403)
        with patch.object(worker, "engine", self.engine), patch.object(worker.ingest_preview_service, "mark_error") as mark:
            await worker.queue_manager._record_task_failure("new", failure, "private", preview_id="v1")
            mark.assert_not_called()
            await worker.queue_manager._record_task_failure("done", failure, "private")
            mark.assert_not_called()
            await worker.queue_manager._record_task_failure("new", failure, "private", preview_id="v2")
            mark.assert_called_once()
        with sqlite3.connect(self.path) as connection:
            status, saved = connection.execute("SELECT status,failure_details_str FROM downloadtask WHERE id='new'").fetchone()
            self.assertEqual(status, "FAILED")
            self.assertEqual(json.loads(saved)["input_role"], "video")
            self.assertEqual(connection.execute("SELECT status,failure_details_str FROM downloadtask WHERE id='done'").fetchone(), ("COMPLETED", None))

    async def test_snapshot_sse_parity_and_existing_integration_permissions(self):
        details = encode_failure(IngestionFailure("SOURCE_FORBIDDEN", "private", stage="source_preparation", input_role="video", http_status=403))
        async with AsyncSession(self.engine) as session:
            session.add(self.task("failed", failure_details_str=details))
            session.add(self.task("active", "DOWNLOADING", failure_details_str=details))
            session.add(self.task("legacy", error_message="SOURCE_NOT_FOUND — private secret"))
            for token, scopes in (("reader", ["downloads:read"]), ("sender", ["ingest"])):
                session.add(IntegrationCredential(id=token, name=token, token_hash=integration_token_hash(token),
                            token_hint="synthetic", scopes_str=json.dumps(scopes), created_at=time.time()))
            await session.commit()
        with patch.object(api, "engine", self.engine), patch.object(api, "ACTIVE_DOWNLOAD_METRICS", {}):
            self.assertEqual((await self.client.get("/api/downloads")).status_code, 401)
            self.assertEqual((await self.client.get("/api/downloads/stream")).status_code, 401)
            self.assertEqual((await self.client.get("/api/downloads", headers={"Authorization": "Bearer sender"})).status_code, 403)
            self.assertEqual((await self.client.get("/api/downloads/stream", headers={"Authorization": "Bearer sender"})).status_code, 403)
            response = await self.client.get("/api/downloads", headers={"Authorization": "Bearer reader"})
            self.assertEqual(response.status_code, 200)
            snapshot = {item["id"]: item["failure"] for item in response.json()}
            stream = api.download_progress_generator()
            try:
                streamed = json.loads((await anext(stream)).removeprefix("data: "))
            finally:
                await stream.aclose()
            self.assertEqual(snapshot, {item["id"]: item["failure"] for item in streamed})
            self.assertIsNone(snapshot["active"])
            self.assertNotIn("provider", snapshot["failed"])
            self.assertNotIn("private", json.dumps(response.json()))


class SourceFailureEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.origin = test_controlled_source.ControlledSourceTests(methodName="test_malformed_source_urls_are_policy_errors")
        await self.origin.asyncSetUp()

    async def asyncTearDown(self):
        await self.origin.asyncTearDown()

    async def test_relay_cleanup_failure_does_not_invent_a_preparation_stage(self):
        from services.controlled_source import SourceRelay
        from services.ingestion_security import source_request_failure
        relay = SourceRelay(input_role="video")
        error = httpx.TransportError("synthetic cleanup failure")
        relay.record_failure(error, phase="relay-close")
        self.assertIsNone(source_request_failure(error).stage)

    async def test_actual_source_403_and_1005_are_distinguished_before_ffprobe(self):
        cases = [
            (b"<title>Access denied | Cloudflare</title><h1>Error <span>1005</span></h1>", True),
            (b"Access denied. Cloudflare Ray ID: 1005. Error 1020", False),
            (b"Access denied. Error 1005. Unrelated provider", False),
            (b"Forbidden", False),
        ]
        for index, (body, recognized) in enumerate(cases):
            with self.subTest(index=index):
                self.origin.routes[f"/{index}"] = (403, {"Content-Type": "text/html", "CF-Ray": "synthetic"}, body)
                with patch("services.media_probe.asyncio.create_subprocess_exec", new_callable=AsyncMock) as native:
                    result = await probe_media_stream(f"http://127.0.0.1:{self.origin.port}/{index}", client_address="127.0.0.1", task_id=f"synthetic-denial-{index}")
                native.assert_not_called()
                failure = result["failure"]
                self.assertEqual((failure.code, failure.stage, failure.input_role, failure.http_status, failure.retryable),
                                 ("SOURCE_FORBIDDEN", "source_preparation", "video", 403, False))
                self.assertEqual(failure.provider_code == "1005", recognized)
        self.assertEqual(len(self.origin.requests), len(cases))

    async def test_audio_evidence_survives_probe_diagnostic_wrapper(self):
        self.origin.routes["/audio"] = (403, {"Content-Type": "text/plain"}, b"Access denied Cloudflare Error code: 1005")
        from services.controlled_source import controlled_media_input
        from services.ingestion_security import source_request_failure
        try:
            async with controlled_media_input(f"http://127.0.0.1:{self.origin.port}/audio", client_address="127.0.0.1", input_role="audio"):
                self.fail("Rejected source yielded an input")
        except httpx.HTTPStatusError as error:
            failure = source_request_failure(error)
        copied = replace(failure, diagnostics_path="private")
        self.assertEqual((copied.input_role, copied.stage, copied.provider_code), ("audio", "source_preparation", "1005"))

    async def test_bounded_inspection_failure_and_cancellation(self):
        class Stream(httpx.AsyncByteStream):
            def __init__(self, chunks, error=None):
                self.chunks, self.error, self.reads = chunks, error, 0
            async def __aiter__(self):
                for chunk in self.chunks:
                    self.reads += 1
                    yield chunk
                if self.error:
                    raise self.error
        for error in (httpx.ReadError("synthetic"), RuntimeError("synthetic")):
            response = httpx.Response(403, headers={"Content-Type": "text/html"}, stream=Stream([], error))
            self.assertEqual(await denial_context(response), {})
            await response.aclose()
        stream = Stream([b"x" * DENIAL_LIMIT, b"Access denied Cloudflare Error 1005"])
        response = httpx.Response(403, headers={"Content-Type": "text/html"}, stream=stream)
        self.assertEqual(await denial_context(response), {})
        self.assertEqual(stream.reads, 1)
        await response.aclose()
        response = httpx.Response(403, headers={"Content-Type": "application/octet-stream"}, stream=Stream([b"Access denied Cloudflare Error 1005"]))
        self.assertEqual(await denial_context(response), {})
        await response.aclose()
        started, closed = asyncio.Event(), asyncio.Event()
        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/html\r\nContent-Length: 999\r\n\r\n")
                await writer.drain()
                started.set()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.origin.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        from services.controlled_source import controlled_media_input
        async def attempt():
            async with controlled_media_input(f"http://127.0.0.1:{port}/", client_address="127.0.0.1"):
                self.fail("Denied input yielded")
        with patch("services.source_denial.DENIAL_TIMEOUT", 0.05):
            with self.assertRaises(httpx.HTTPStatusError):
                await attempt()
        await asyncio.wait_for(closed.wait(), 2)
        closed.clear()
        task = asyncio.create_task(attempt())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 2)
