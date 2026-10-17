"""Storage HTTP authority/contract regressions; run through run_isolated.py."""
from __future__ import annotations

import time
import unittest
import uuid
import tempfile
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from lifecycle.coordination import (
    EffectClass, RuntimeCoordinator, use_runtime_coordinator_for_testing,
)
from models import AuthSession
from routes import admin_storage as storage, auth


DRAFT = {
    "offload_enabled": False,
    "local_budget_bytes": 50_000_000_000,
    "cloud_budget_bytes": 1_000_000_000_000,
    "prefetch_next_episode": False,
    "hevc_compression_mode": "auto",
}


class AdminStorageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = AuthSession(id="storage-human-session", user_id=1,
            created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600,
            reauthenticated_at=time.time())
        methods = ("status", "refresh", "preview", "save", "operation", "retention",
                   "set_keep_local", "activity", "cleanup_preview", "cleanup_execute")
        self.service = SimpleNamespace(**{
            method: AsyncMock(return_value={"method": method}) for method in methods
        })
        self.app = FastAPI()
        self.app.include_router(storage.router)
        self.app.dependency_overrides[storage.get_storage_service] = lambda: self.service
        async def human_session():
            return self.session
        self.app.dependency_overrides[auth.get_current_session] = human_session
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="https://watch.example.test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_recent_human_authority_is_required_for_every_surface(self):
        self.session.reauthenticated_at = None
        cases = [
            ("GET", "/status", None), ("GET", "/retention", None),
            ("GET", "/activity", None), ("GET", "/operations/unknown", None),
            ("POST", "/refresh", {"request_id": "storage-request-1"}),
            ("POST", "/policy/preview", {"draft": DRAFT, "expected_revision": 0}),
            ("PUT", "/policy", {"draft": DRAFT, "expected_revision": 0,
                "review_id": "review-123456", "request_id": "storage-request-2"}),
            ("PUT", "/retention/movie-1", {"keep_local": True, "request_id": "storage-request-3"}),
            ("POST", "/cleanup/preview", {}),
            ("POST", "/cleanup", {"plan_id": "plan-123456", "request_id": "storage-request-4"}),
        ]
        for method, path, payload in cases:
            with self.subTest(method=method, path=path):
                response = await self.client.request(method, "/api/admin/storage" + path, json=payload)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["detail"]["code"], "reauthentication_required")
        for value in vars(self.service).values():
            value.assert_not_awaited()

    async def test_anonymous_setup_and_machine_credentials_do_not_grant_access(self):
        self.app.dependency_overrides.pop(auth.get_current_session)
        for headers in ({}, {"Authorization": "Bearer integration-token"},
                        {"Cookie": "streamhome_setup=synthetic-bootstrap-session"}):
            response = await self.client.get("/api/admin/storage/status", headers=headers)
            self.assertEqual(response.status_code, 401, response.text)
        self.service.status.assert_not_awaited()

    async def test_exact_review_and_save_payload_reach_the_same_session(self):
        response = await self.client.post("/api/admin/storage/policy/preview",
            json={"draft": DRAFT, "expected_revision": 3})
        self.assertEqual(response.status_code, 200, response.text)
        self.service.preview.assert_awaited_once_with(self.session.id, DRAFT, 3)
        response = await self.client.put("/api/admin/storage/policy", json={
            "draft": DRAFT, "expected_revision": 3, "review_id": "review-123456",
            "request_id": "save-12345678",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.service.save.assert_awaited_once_with(self.session.id, "review-123456",
                                                 DRAFT, 3, "save-12345678")

    async def test_strict_budget_and_boolean_fields_reject_ambiguous_input(self):
        bad_drafts = [
            {**DRAFT, "local_budget_bytes": True},
            {**DRAFT, "cloud_budget_bytes": "1000"},
            {**DRAFT, "local_budget_bytes": 0},
            {**DRAFT, "local_budget_bytes": 1.5},
            {**DRAFT, "cloud_budget_bytes": 2 ** 53},
            {**DRAFT, "offload_enabled": "false"},
            {**DRAFT, "prefetch_next_episode": 1},
            {**DRAFT, "hevc_compression_mode": "invented"},
            {**DRAFT, "storage_engine": "CLOUD"},
        ]
        for draft in bad_drafts:
            response = await self.client.post("/api/admin/storage/policy/preview",
                json={"draft": draft, "expected_revision": 0})
            self.assertEqual(response.status_code, 422, response.text)
        self.service.preview.assert_not_awaited()

    async def test_browser_cannot_add_its_own_authority_or_target(self):
        response = await self.client.put("/api/admin/storage/policy", json={
            "draft": DRAFT, "expected_revision": 0, "review_id": "review-123456",
            "request_id": "save-12345678", "auth_session_id": "other-human",
            "target_generation": "invented", "reauthenticated": True,
        })
        self.assertEqual(response.status_code, 422)
        self.service.save.assert_not_awaited()

    async def test_operation_lookup_is_scoped_to_the_current_human(self):
        response = await self.client.get("/api/admin/storage/operations/lost-ack-123")
        self.assertEqual(response.status_code, 200)
        self.service.operation.assert_awaited_once_with(self.session.id, "lost-ack-123")

    async def test_real_service_http_review_save_receipt_and_legacy_writer_boundary(self):
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlmodel import SQLModel
        import config
        import db as database
        from config import settings
        from services.storage_policy_service import StoragePolicyService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / 'server'
            (server / 'media').mkdir(parents=True)
            engine = create_async_engine('sqlite+aiosqlite:///' + str(server / 'database.db'))
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(SQLModel.metadata.create_all)
                with patch.object(database, 'engine', engine), \
                     patch.object(config, 'config_dir', str(server)), \
                     patch.object(settings, 'BASE_DIR', str(root)), \
                     patch.object(settings, 'MEDIA_DIR', str(server / 'media')), \
                     patch.object(settings, 'STORAGE_POLICY', None), \
                     patch.object(StoragePolicyService, '_wake', lambda _: None):
                    real_service = StoragePolicyService()
                    self.app.dependency_overrides[storage.get_storage_service] = lambda: real_service
                    status = await self.client.get('/api/admin/storage/status')
                    self.assertEqual(status.status_code, 200, status.text)
                    self.assertIsNone(status.json()['local']['written_bytes'])
                    review = await self.client.post('/api/admin/storage/policy/preview',
                        json={'draft': DRAFT, 'expected_revision': 0})
                    self.assertEqual(review.status_code, 200, review.text)
                    saved = await self.client.put('/api/admin/storage/policy', json={
                        'review_id': review.json()['review_id'], 'draft': DRAFT,
                        'expected_revision': 0, 'request_id': 'real-http-save-1234'})
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertEqual(saved.json()['policy']['revision'], 1)
                    receipt = await self.client.get('/api/admin/storage/operations/real-http-save-1234')
                    self.assertEqual(receipt.json()['operation'], saved.json()['operation'])
                    status = await self.client.get('/api/admin/storage/status')
                    self.assertEqual(status.json()['policy']['local_budget_bytes'], DRAFT['local_budget_bytes'])
                    self.assertEqual(settings.STORAGE_POLICY['revision'], 1)
            finally:
                await engine.dispose()

    async def test_actual_legacy_writer_rejects_hybrid_policy_in_guarded_import(self):
        # A fresh child follows the real lifecycle import gate, without starting
        # the application or retaining a startup context in this test process.
        script = '''
import asyncio
from pathlib import Path
from lifecycle.bootstrap import evaluate_startup
evaluate_startup(Path.cwd().parent, install=True)
from config import settings
settings.STORAGE_POLICY = {'revision': 1}
from application import _save_system_settings_locked, SystemSettingsRequest
from fastapi import HTTPException
async def check():
    try:
        await _save_system_settings_locked(SystemSettingsRequest(storage_engine='CLOUD',
            rclone_remote_path='unreviewed:target', hevc_compression_mode='off'))
    except HTTPException as error:
        assert error.status_code == 409
        assert error.detail['code'] == 'storage_policy_managed'
    else:
        raise AssertionError('Legacy writer bypassed reviewed hybrid policy')
    assert settings.STORAGE_POLICY == {'revision': 1}
asyncio.run(check())
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_pagination_is_bounded_and_keep_does_not_accept_paths(self):
        for endpoint in ("retention", "activity"):
            for suffix in ("?limit=0", "?limit=101", "?cursor=" + "a" * 2049):
                self.assertEqual((await self.client.get(
                    "/api/admin/storage/" + endpoint + suffix)).status_code, 422)
        response = await self.client.get("/api/admin/storage/retention?cursor=next-page&limit=10")
        self.assertEqual(response.status_code, 200)
        self.service.retention.assert_awaited_once_with(cursor="next-page", limit=10)
        response = await self.client.put("/api/admin/storage/retention/episode:123", json={
            "keep_local": False, "request_id": "keep-request-123",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.service.set_keep_local.assert_awaited_once_with(
            self.session.id, "episode:123", False, "keep-request-123")
        self.assertEqual((await self.client.put("/api/admin/storage/retention/movie-1",
            json={"keep_local": True, "request_id": "keep-request-456",
                  "path": "/arbitrary/local/path"})).status_code, 422)

    async def test_configuration_fence_blocks_save_without_blocking_status(self):
        coordinator = RuntimeCoordinator()
        with use_runtime_coordinator_for_testing(coordinator):
            coordinator.close_admission([EffectClass.CONFIG_WRITE],
                operation_id=str(uuid.uuid4()), generation=1)
            response = await self.client.put("/api/admin/storage/policy", json={
                "draft": DRAFT, "expected_revision": 0, "review_id": "review-123456",
                "request_id": "save-12345678",
            })
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["detail"]["code"], "lifecycle_work_blocked")
            self.service.save.assert_not_awaited()
            self.assertEqual((await self.client.get("/api/admin/storage/status")).status_code, 200)

    async def test_domain_conflicts_are_structured_without_private_exceptions(self):
        from services.storage_policy_service import StoragePolicyError
        self.service.preview.side_effect = StoragePolicyError(
            "storage_review_changed", "Storage settings changed. Review them again.", 409)
        response = await self.client.post("/api/admin/storage/policy/preview",
            json={"draft": DRAFT, "expected_revision": 0})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": {
            "code": "storage_review_changed", "message": "Storage settings changed. Review them again.",
        }})


if __name__ == "__main__":
    unittest.main()
