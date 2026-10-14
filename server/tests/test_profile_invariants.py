"""Actual backend route protection for the permanent administrator profile."""
from __future__ import annotations

import asyncio
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from lifecycle.bootstrap import StartupMode
from models import PlaybackSession, Profile, User


class PermanentProfileRegression(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="streamhome-profile-invariants-")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.directory.name) / 'profiles.db'}")
        context = SimpleNamespace(mode=StartupMode.NORMAL, guarded=False)
        with patch("lifecycle.bootstrap.require_startup_context", return_value=context), patch("lifecycle.bootstrap.get_startup_readiness", return_value=SimpleNamespace(context=context)):
            self.application = importlib.import_module("application")
        self.engine_patch = patch.object(self.application, "engine", self.engine)
        self.engine_patch.start()

        async def seed():
            async with self.engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with AsyncSession(self.engine) as db:
                db.add(Profile(id="1", name="Administrator"))
                db.add(Profile(id="ordinary", name="Ordinary viewer"))
                db.add(PlaybackSession(profile_id="1", movie_id="retained-media", timestamp=20, updated_at="2026-10-13T00:00:00Z"))
                await db.commit()

        asyncio.run(seed())
        app = FastAPI()
        app.add_api_route("/api/profiles/{profile_id}", self.application.delete_profile, methods=["DELETE"])

        async def authenticated():
            return User(id=1, email="admin@example.test", password_hash="unused")

        app.dependency_overrides[self.application.get_current_user] = authenticated
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine_patch.stop()
        asyncio.run(self.engine.dispose())
        self.directory.cleanup()

    def test_actual_delete_route_preserves_permanent_profile_and_history(self):
        response = self.client.delete("/api/profiles/1")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "permanent_profile")

        async def check():
            async with AsyncSession(self.engine) as db:
                self.assertIsNotNone(await db.get(Profile, "1"))
                from sqlmodel import select
                history = (await db.exec(select(PlaybackSession).where(PlaybackSession.profile_id == "1"))).all()
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0].timestamp, 20)

        asyncio.run(check())

    def test_actual_delete_route_still_removes_an_ordinary_profile(self):
        response = self.client.delete("/api/profiles/ordinary")
        self.assertEqual(response.status_code, 200)

        async def check():
            async with AsyncSession(self.engine) as db:
                self.assertIsNone(await db.get(Profile, "ordinary"))
                self.assertIsNotNone(await db.get(Profile, "1"))

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
