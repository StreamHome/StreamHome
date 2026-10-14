"""Actual MediaSender router error/cleanup boundaries in disposable fixtures."""
from __future__ import annotations

import asyncio
import json
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from lifecycle import media_publication
from lifecycle.coordination import AdmissionClosedError, EffectClass, RuntimeCoordinator, use_runtime_coordinator_for_testing
from lifecycle.media_work import MediaRecoveryRequired, require_resolved_media
from models import IntegrationCredential, MediaOperationReceipt, Movie
from routes import media_updates as routes
from services.integration_auth import integration_token_hash
from tests.media import test_acceptance_outcomes as acceptance_fixture


class MediaSenderRouterAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = acceptance_fixture.AcceptanceOutcomes()
        await self.fixture.asyncSetUp()
        self.lock_patch = patch("lifecycle.media_work._locks", {})
        self.lock_patch.start()
        self.coordinator = RuntimeCoordinator()
        self.coordinator_context = use_runtime_coordinator_for_testing(self.coordinator)
        self.coordinator_context.__enter__()
        self.token = "synthetic-route-token"
        async with AsyncSession(self.fixture.engine) as session:
            credential = IntegrationCredential(id="route-fixture", name="Route fixture", token_hash=integration_token_hash(self.token), last_used_at=time.time())
            credential.scopes = ["ingest"]
            session.add(credential)
            movie = await session.get(Movie, "m_1")
            movie.subtitles = [{"trackId": "en", "fileName": "subtitle_en.vtt", "language": "en", "ext": ".vtt"}]
            movie.audio_metadata = [{"source": "external", "language": "en", "fileName": "en.m4a"}]
            session.add(movie)
            await session.commit()
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://router.example.test", headers={"Authorization": f"Bearer {self.token}"})
        self.rollback_failures = []

    async def asyncTearDown(self):
        await self.client.aclose()
        self.coordinator_context.__exit__(None, None, None)
        self.lock_patch.stop()
        await self.fixture.asyncTearDown()

    def install_failing_rollback_session(self, *, blocked=False):
        entered = asyncio.Event()

        async def dependency():
            async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
                async def rollback():
                    self.rollback_failures.append(True)
                    if blocked:
                        entered.set()
                        await asyncio.Event().wait()
                    raise InvalidRequestError("Injected failed session rollback")

                with patch.object(session, "rollback", rollback):
                    yield session

        self.app.dependency_overrides[routes.get_session] = dependency
        return entered

    async def leave_pending_mutation(self):
        mutation = media_publication.MediaMutation("m_1")
        await mutation.__aenter__()
        mutation.capture_local(str(self.fixture.metadata))
        # Reproduce a lost request/controller without completing its operation.
        media_publication.current_mutation.reset(mutation.token)
        mutation.entered = False
        self.manifest = mutation.root / "operation.json"
        self.original_metadata = self.fixture.metadata.read_bytes()

    async def requests_for(self, media_id):
        return [
            ("PATCH", f"/api/media/{media_id}/metadata", {"skip_markers": self.fixture.markers}),
            ("PUT", f"/api/media/{media_id}/subtitles/en", {"language": "en", "url": "https://source.example.test/sidecar"}),
            ("DELETE", f"/api/media/{media_id}/subtitles/en", None),
            ("PUT", f"/api/media/{media_id}/audio/en", {"url": "https://source.example.test/audio"}),
            ("DELETE", f"/api/media/{media_id}/audio/en", None),
        ]

    async def test_actual_postcommit_close_and_rollback_failure_returns_503_with_evidence(self):
        close_calls, hook_calls = [], []

        async def dependency():
            session = AsyncSession(self.fixture.engine, expire_on_commit=False)

            @event.listens_for(session.sync_session, "after_commit")
            def acknowledgment_failure(_):
                hook_calls.append(True)
                raise RuntimeError("Injected after_commit acknowledgment failure")

            original_close = session.close
            original_rollback = session.rollback

            async def close():
                close_calls.append(True)
                if len(close_calls) == 1:
                    raise OSError("Injected first failed-session close")
                await original_close()

            async def rollback():
                try:
                    await original_rollback()
                except InvalidRequestError:
                    self.rollback_failures.append(True)
                    raise

            with patch.object(session, "close", close), patch.object(session, "rollback", rollback):
                try:
                    yield session
                finally:
                    await session.close()

        self.app.dependency_overrides[routes.get_session] = dependency
        response = await self.client.patch("/api/media/m_1/metadata", json={"skip_markers": self.fixture.markers})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "media_recovery_required")
        self.assertEqual(len(hook_calls), 1)
        self.assertEqual(len(self.rollback_failures), 1, "Actual committed ORM rollback must fail without masking 503")
        async with AsyncSession(self.fixture.engine) as session:
            self.assertEqual((await session.get(Movie, "m_1")).skip_markers, self.fixture.markers)
            receipts = (await session.exec(select(MediaOperationReceipt))).all()
            self.assertEqual(len(receipts), 1)
        manifests = list((self.fixture.root / "temp/media-operations").glob("*/operation.json"))
        self.assertEqual(len(manifests), 1)
        self.assertTrue((manifests[0].parent / "asset-0").is_file())
        self.assertEqual(json.loads(self.fixture.metadata.read_text())["skip_markers"], self.fixture.markers)
        with self.assertRaises(MediaRecoveryRequired):
            require_resolved_media("m_1")
        await media_publication.recover_media_operations()
        self.assertFalse(manifests[0].exists())
        self.assertEqual(json.loads(self.fixture.metadata.read_text())["skip_markers"], self.fixture.markers)

    async def test_all_actual_routes_keep_pending_evidence_and_stable_503_when_rollback_fails(self):
        await self.leave_pending_mutation()
        self.install_failing_rollback_session()
        with patch.object(routes, "validate_remote_input", AsyncMock(return_value={})):
            for method, path, payload in await self.requests_for("m_1"):
                with self.subTest(method=method, path=path):
                    response = await self.client.request(method, path, json=payload)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["detail"]["code"], "media_recovery_required")
                    self.assertTrue(self.manifest.is_file())
                    self.assertEqual(self.fixture.metadata.read_bytes(), self.original_metadata)
        self.assertEqual(len(self.rollback_failures), 5)

    async def test_all_actual_routes_preserve_normal_media_update_error_when_rollback_fails(self):
        self.install_failing_rollback_session()
        with patch.object(routes, "validate_remote_input", AsyncMock(return_value={})):
            for method, path, payload in await self.requests_for("m_missing"):
                with self.subTest(method=method, path=path):
                    response = await self.client.request(method, path, json=payload)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["detail"]["code"], "media_not_found")
        self.assertEqual(len(self.rollback_failures), 5)
        self.assertFalse(list((self.fixture.root / "temp/media-operations").glob("*/operation.json")))

    async def test_actual_route_rollback_does_not_swallow_caller_cancellation(self):
        await self.leave_pending_mutation()
        entered = self.install_failing_rollback_session(blocked=True)
        task = asyncio.create_task(self.client.patch("/api/media/m_1/metadata", json={"skip_markers": self.fixture.markers}))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.manifest.is_file())
        self.assertEqual(self.fixture.metadata.read_bytes(), self.original_metadata)

    async def test_actual_route_preserves_closed_runtime_admission(self):
        self.coordinator.close_admission([EffectClass.DB_WRITE, EffectClass.DURABLE_MEDIA_WRITE], operation_id=str(uuid.uuid4()), generation=1)
        async def dependency():
            async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
                yield session

        self.app.dependency_overrides[routes.get_session] = dependency
        with patch.object(routes, "update_skip_markers", AsyncMock()) as update:
            with self.assertRaises(AdmissionClosedError):
                await self.client.patch("/api/media/m_1/metadata", json={"skip_markers": self.fixture.markers})
            update.assert_not_awaited()
        self.assertEqual(json.loads(self.fixture.metadata.read_text())["skip_markers"], {"intro": []})


if __name__ == "__main__":
    unittest.main()
