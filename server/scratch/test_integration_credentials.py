"""Regression checks for administrator-managed, scoped integration credentials."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from db import get_session
from models import AuthSession, User
from routes.auth import get_current_user, require_recent_reauth, router
from routes.queue import require_browser_or_integration_scope
from services.integration_auth import require_integration_scope


class IntegrationCredentialRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "integration-credentials.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        asyncio.run(self._create_database())

        self.user = User(
            id=1,
            email="admin@example.test",
            password_hash="unused-in-overridden-dependency",
            two_factor_enabled=True,
        )
        current_time = time.time()
        self.auth_session = AuthSession(
            id="admin-session",
            user_id=1,
            created_at=current_time,
            last_seen_at=current_time,
            expires_at=current_time + 3600,
            reauthenticated_at=current_time,
        )

        async def session_override():
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                yield session

        async def user_override():
            return self.user

        async def reauthentication_override():
            return self.auth_session

        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_session] = session_override
        self.app.dependency_overrides[get_current_user] = user_override
        self.app.dependency_overrides[require_recent_reauth] = reauthentication_override

        @self.app.get("/integration-test/ingest")
        async def integration_ingest(credential=Depends(require_integration_scope("ingest"))):
            return {"credentialId": credential.id}

        @self.app.get("/integration-test/downloads")
        async def integration_downloads(credential=Depends(require_integration_scope("downloads:read"))):
            return {"credentialId": credential.id}

        @self.app.delete("/integration-test/downloads")
        async def integration_cancel(credential=Depends(require_integration_scope("downloads:cancel"))):
            return {"credentialId": credential.id}

        @self.app.get("/integration-test/shared-downloads")
        async def shared_downloads(access=Depends(require_browser_or_integration_scope("downloads:read"))):
            return {"credentialId": access.id}

        @self.app.delete("/integration-test/shared-downloads")
        async def shared_cancel(
            access=Depends(
                require_browser_or_integration_scope(
                    "downloads:cancel",
                    recent_reauthentication=True,
                )
            ),
        ):
            return {"credentialId": access.id}

        self.client = TestClient(self.app)

    async def _create_database(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

    def tearDown(self) -> None:
        self.client.close()
        asyncio.run(self.engine.dispose())
        self.temporary.cleanup()

    @staticmethod
    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_multiple_named_keys_keep_independent_permissions_and_revocation(self) -> None:
        available = self.client.get("/api/auth/integrations/scopes")
        self.assertEqual(available.status_code, 200)
        self.assertEqual(
            {item["id"] for item in available.json()},
            {"ingest", "downloads:read", "downloads:cancel"},
        )

        first = self.client.post(
            "/api/auth/integrations",
            json={"name": "MediaSender bedroom", "scopes": ["ingest"]},
        )
        self.assertEqual(first.status_code, 201, first.text)
        first_body = first.json()
        first_token = first_body["token"]
        self.assertTrue(first_token.startswith("shk_"))
        self.assertNotIn(first_token, str(first_body["credential"]))

        second = self.client.post(
            "/api/auth/integrations",
            json={
                "name": "Queue monitor",
                "scopes": ["downloads:read", "downloads:cancel"],
                "expires_in_days": 90,
            },
        )
        self.assertEqual(second.status_code, 201, second.text)
        second_body = second.json()
        second_token = second_body["token"]

        credentials = self.client.get("/api/auth/integrations")
        self.assertEqual(credentials.status_code, 200)
        listed = credentials.json()
        self.assertEqual(len(listed), 2)
        self.assertEqual({item["name"] for item in listed}, {"MediaSender bedroom", "Queue monitor"})
        self.assertNotIn(first_token, credentials.text)
        self.assertNotIn(second_token, credentials.text)

        self.assertEqual(
            self.client.get("/integration-test/ingest", headers=self.bearer(first_token)).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/integration-test/downloads", headers=self.bearer(first_token)).status_code,
            403,
        )
        insufficient = self.client.get("/integration-test/downloads", headers=self.bearer(first_token))
        self.assertEqual(insufficient.json()["detail"]["code"], "insufficient_scope")
        self.assertEqual(
            self.client.get("/integration-test/downloads", headers=self.bearer(second_token)).status_code,
            200,
        )
        self.assertEqual(
            self.client.delete("/integration-test/downloads", headers=self.bearer(second_token)).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/integration-test/shared-downloads", headers=self.bearer(second_token)).status_code,
            200,
        )
        self.assertEqual(
            self.client.delete("/integration-test/shared-downloads", headers=self.bearer(second_token)).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/integration-test/ingest", headers=self.bearer(second_token)).status_code,
            403,
        )

        first_id = first_body["credential"]["id"]
        updated = self.client.put(
            f"/api/auth/integrations/{first_id}",
            json={"name": "Bedroom automation", "scopes": ["ingest", "downloads:read"]},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "Bedroom automation")
        self.assertEqual(updated.json()["scopes"], ["downloads:read", "ingest"])
        self.assertEqual(
            self.client.get("/integration-test/downloads", headers=self.bearer(first_token)).status_code,
            200,
        )

        revoked = self.client.delete(f"/api/auth/integrations/{first_id}")
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(
            self.client.get("/integration-test/ingest", headers=self.bearer(first_token)).status_code,
            401,
        )
        inactive = self.client.get("/integration-test/ingest", headers=self.bearer(first_token))
        self.assertEqual(inactive.json()["detail"]["code"], "invalid_integration_credential")
        self.assertEqual(
            self.client.get("/integration-test/downloads", headers=self.bearer(second_token)).status_code,
            200,
        )

    def test_invalid_or_empty_permissions_fail_closed(self) -> None:
        unsupported = self.client.post(
            "/api/auth/integrations",
            json={"name": "Unsafe key", "scopes": ["backups:restore"]},
        )
        self.assertEqual(unsupported.status_code, 422)
        self.assertEqual(unsupported.json()["detail"]["code"], "invalid_integration_scope")

        empty = self.client.post(
            "/api/auth/integrations",
            json={"name": "Empty key", "scopes": []},
        )
        self.assertEqual(empty.status_code, 422)

    def test_management_endpoints_require_an_authenticated_reauthenticated_admin(self) -> None:
        user_override = self.app.dependency_overrides.pop(get_current_user)
        reauthentication_override = self.app.dependency_overrides.pop(require_recent_reauth)
        try:
            response = self.client.post(
                "/api/auth/integrations",
                json={"name": "Unauthorized key", "scopes": ["ingest"]},
            )
            self.assertEqual(response.status_code, 401)
        finally:
            self.app.dependency_overrides[get_current_user] = user_override
            self.app.dependency_overrides[require_recent_reauth] = reauthentication_override


if __name__ == "__main__":
    unittest.main()
