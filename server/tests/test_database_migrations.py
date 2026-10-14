"""Imported database migration checks; run only in a disposable installation."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import db
from config import settings
from models import Movie


class DatabaseMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmdb_backfill_handles_both_prefixes_and_preserves_valid_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "database.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            fixtures = {"m_123": (None, 123), "tv_456": (None, 456),
                "tv_789": (0, 789), "tv_222": (202, 202), "tv_4x": (0, 0),
                "m_no": (None, None), "tv_": (None, None)}
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(SQLModel.metadata.create_all)
                async with AsyncSession(engine) as session:
                    for identifier, (existing, _) in fixtures.items():
                        session.add(Movie(id=identifier, title="Isolated legacy record", tmdb_id=existing,
                            description="", thumbnail_url="", video_url="", duration="0m", release_year=2020))
                    await session.commit()
                with (mock.patch.object(db, "engine", engine),
                      mock.patch.object(settings, "db_path", str(path)),
                      mock.patch.object(settings, "API_BEARER_TOKEN", "")):
                    for _ in range(2):
                        await db.init_db(allow_migration_backups=False, allow_external_config_mutation=False)
                        async with AsyncSession(engine) as session:
                            records = (await session.exec(select(Movie))).all()
                            self.assertEqual({item.id: item.tmdb_id for item in records},
                                {identifier: expected for identifier, (_, expected) in fixtures.items()})
            finally:
                await engine.dispose()
