"""Imported database migration checks; run only in a disposable installation."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import db
from config import settings
from models import Movie, PlaybackSession, PlaybackSessionResponse


class DatabaseMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_progress_owner_upgrade_backs_up_and_preserves_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "database.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            try:
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(
                        "CREATE TABLE playbacksession (id INTEGER PRIMARY KEY, profile_id TEXT NOT NULL, "
                        "movie_id TEXT NOT NULL, episode_id TEXT, timestamp INTEGER NOT NULL, "
                        "duration_watched INTEGER, completion_rate FLOAT, updated_at TEXT NOT NULL, is_finished BOOLEAN)"
                    )
                    await connection.exec_driver_sql(
                        "INSERT INTO playbacksession VALUES (1, 'profile', 'm_1', NULL, 105, 83, 0.875, '2026-10-15T12:00:00+00:00', 1)"
                    )
                    # Every other table is current: only the private owner column
                    # should trigger the pre-playback migration backup.
                    await connection.run_sync(SQLModel.metadata.create_all)
                with (mock.patch.object(db, "engine", engine),
                      mock.patch.object(settings, "db_path", str(path)),
                      mock.patch.object(settings, "API_BEARER_TOKEN", "")):
                    await db.init_db(allow_migration_backups=True, allow_external_config_mutation=False)
                    backups = list((path.parent / "backup").glob("pre-playback-*.db"))
                    self.assertEqual(len(backups), 1)
                    with sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True) as snapshot:
                        columns = {row[1] for row in snapshot.execute("PRAGMA table_info(playbacksession)")}
                        self.assertNotIn("progress_run_id", columns)
                        self.assertEqual(snapshot.execute("SELECT timestamp, is_finished FROM playbacksession").fetchone(), (105, 1))
                    async with AsyncSession(engine) as session:
                        saved = await session.get(PlaybackSession, 1)
                        self.assertEqual((saved.timestamp, saved.duration_watched, saved.completion_rate, saved.is_finished), (105, 83, 0.875, True))
                        self.assertIsNone(saved.progress_run_id)
                        saved.progress_run_id = "retained-private-owner"
                        session.add(saved)
                        await session.commit()
                    await db.init_db(allow_migration_backups=True, allow_external_config_mutation=False)
                    self.assertEqual(len(list((path.parent / "backup").glob("pre-playback-*.db"))), 1)
                    async with AsyncSession(engine) as session:
                        saved = await session.get(PlaybackSession, 1)
                        self.assertEqual(saved.progress_run_id, "retained-private-owner")
                        response = PlaybackSessionResponse(movieId=saved.movie_id, profileId=saved.profile_id,
                            timestamp=saved.timestamp, duration_watched=saved.duration_watched,
                            completion_rate=saved.completion_rate, updatedAt=saved.updated_at,
                            episodeId=saved.episode_id, is_finished=saved.is_finished)
                        wire = response.model_dump(by_alias=True)
                        self.assertNotIn("progress_run_id", wire)
                        self.assertNotIn("progressRunId", wire)
            finally:
                await engine.dispose()

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
