"""Actual personalized catalog ASGI/SQLite writers remain visible through cleanup.

Run through tests/run_isolated.py: application imports must resolve only disposable
configuration, data and runtime paths. No lifespan, external services or ports run.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import importlib
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import httpx
import jwt
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from lifecycle.bootstrap import StartupContext, StartupMode, StartupReadiness
from lifecycle.coordination import (
    EffectClass,
    RuntimeCoordinator,
    use_runtime_coordinator_for_testing,
)
from models import (
    AuthSession,
    Movie,
    Profile,
    ProfileMediaPreference,
    ProfileVibeVector,
    RecommendationFeedResponse,
    RecommendationRuntimeMetric,
    User,
)
from services import recommendation, state
from routes import auth


class CatalogWriterLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="streamhome-catalog-writer-")
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.directory.name) / 'catalog.db'}",
            pool_size=2,
            max_overflow=0,
            pool_timeout=1,
            connect_args={"timeout": 0.1},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def pragmas(connection, _record) -> None:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=100")

        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.auth_session = AuthSession(
            id="catalog-session", user_id=1, created_at=time.time(),
            last_seen_at=time.time(), expires_at=time.time() + 3600,
        )
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(User(id=1, email="catalog@example.test", password_hash="unused"))
            db.add(self.auth_session)
            db.add(Profile(id="profile", name="Catalog profile"))
            db.add(Profile(id="protected", name="Protected profile", pin_enabled=True,
                           pin_hash="synthetic-protected-hash", pin_version=1))
            for movie_id, title, pacing in (("m_1", "First title", 55), ("m_2", "Second title", 130)):
                db.add(Movie(
                    id=movie_id, tmdb_id=int(movie_id[2:]), title=title,
                    description="Synthetic catalog", thumbnail_url="", video_url="",
                    duration="2h", release_year=2026, catalog_source="tmdb_cache",
                    availability="cached", dialogue_wpm=pacing, dialogue_confidence=0.9,
                ))
                db.add(ProfileMediaPreference(
                    profile_id="profile", movie_id=movie_id, preference="love", updated_at=1,
                ))
            await db.commit()

        context = StartupContext(
            mode=StartupMode.NORMAL, installation_root=Path(settings.BASE_DIR),
            inspected_schema_version=1, generation=1, operation_id=None,
            operation_type=None, lifecycle_phase=None, owner_kind=None,
            guarded_identity_validated=False, allowed_effects=frozenset(),
        )
        with patch("lifecycle.bootstrap.require_startup_context", return_value=context), patch(
            "lifecycle.bootstrap.get_startup_readiness", return_value=StartupReadiness(context)
        ):
            self.application = importlib.import_module("application")
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(self.application, "engine", self.engine))
        self.patches.enter_context(patch.object(self.application, "STARTUP_CONTEXT", context))
        self.patches.enter_context(patch.object(
            self.application, "STARTUP_READINESS",
            SimpleNamespace(projection=lambda: {"generalReady": True}),
        ))
        self.patches.enter_context(patch.object(settings, "SETUP_COMPLETE", True))
        self.patches.enter_context(patch.object(settings, "RECOMMENDATION_V2_SHADOW", True))
        self.patches.enter_context(patch.object(state, "MAINTENANCE_MODE", False))
        self.coordinator = RuntimeCoordinator()
        self.patches.enter_context(use_runtime_coordinator_for_testing(self.coordinator))
        self.old_overrides = self.application.app.dependency_overrides.copy()
        self.application.app.dependency_overrides[self.application.get_current_user] = lambda: object()
        self.application.app.dependency_overrides[self.application.get_current_session] = lambda: self.auth_session
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.application.app),
            base_url="http://catalog.example.test",
        )
        self.pending: list[asyncio.Task] = []
        self.gates: list[asyncio.Event] = []
        self.writes: list[str] = []

        @event.listens_for(self.engine.sync_engine, "before_cursor_execute")
        def record_writes(_connection, _cursor, statement, _parameters, _context, _many) -> None:
            if statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")):
                self.writes.append(statement)

    async def asyncTearDown(self) -> None:
        for gate in self.gates:
            gate.set()
        if self.pending:
            await asyncio.wait_for(asyncio.gather(*self.pending, return_exceptions=True), timeout=5)
        await self.client.aclose()
        self.application.app.dependency_overrides.clear()
        self.application.app.dependency_overrides.update(self.old_overrides)
        self.patches.close()
        await self.engine.dispose()
        self.directory.cleanup()

    def gate(self) -> asyncio.Event:
        gate = asyncio.Event()
        self.gates.append(gate)
        return gate

    def request(self) -> asyncio.Task:
        task = asyncio.create_task(self.client.get("/api/movies", params={"profile_id": "profile"}))
        self.pending.append(task)
        return task

    def feed_request(self) -> asyncio.Task:
        task = asyncio.create_task(self.client.get("/api/recommendations/profile"))
        self.pending.append(task)
        return task

    async def wait(self, signal: asyncio.Event) -> None:
        await asyncio.wait_for(signal.wait(), timeout=5)

    def fence(self):
        snapshot = self.coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1,
        )
        self.assertEqual(len(snapshot.lease_ids), 1)
        self.assertEqual(
            snapshot.lease_ids,
            frozenset(item["lease_id"] for item in self.coordinator.active_work()),
        )
        return snapshot

    async def assert_pending_drain(self, snapshot) -> None:
        result = await self.coordinator.drain(snapshot, timeout=0.02)
        self.assertFalse(result.complete)
        self.assertTrue(result.timed_out)
        self.assertEqual(
            {item["lease_id"] for item in result.remaining}, snapshot.lease_ids,
        )

    async def assert_settled(self, snapshot, *, committed: bool, shadow: bool = True) -> None:
        result = await self.coordinator.drain(snapshot, timeout=1)
        self.assertTrue(result.complete)
        self.assertEqual(self.coordinator.active_work(), ())
        self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
        async with AsyncSession(self.engine) as db:
            vector = await db.get(ProfileVibeVector, "profile")
            metric = await db.get(RecommendationRuntimeMetric, "profile")
            self.assertEqual(vector is not None, committed)
            self.assertEqual(metric is not None, committed and shadow)
            if vector is not None:
                self.assertGreater(vector.dialogue_confidence, 0)
        # A fresh independent SQLite writer proves rollback/connection cleanup
        # rather than a lease disappearing while the old transaction stays open.
        async with AsyncSession(self.engine) as db:
            db.add(Profile(id="cleanup-writer", name="Cleanup writer"))
            await asyncio.wait_for(db.commit(), timeout=1)
        self.coordinator.release_fence(snapshot)

    async def assert_cancelled_request(self, pending) -> None:
        result = (await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=5))[0]
        # Starlette may surface endpoint-task cancellation as no response.
        self.assertIsInstance(result, (asyncio.CancelledError, RuntimeError))

    async def test_admitted_get_is_captured_before_rank_and_rejects_new_personalization(self) -> None:
        entered, release = asyncio.Event(), self.gate()
        rank = self.application.rank_movies_for_profile
        ranked_ids = []

        async def paused_rank(db, *args):
            entered.set()
            await release.wait()
            rows = await rank(db, *args)
            ranked_ids.extend(movie.id for _, movie, *_ in rows)
            return rows

        with patch.object(self.application, "rank_movies_for_profile", paused_rank):
            pending = self.request()
            await self.wait(entered)
            self.assertEqual(self.writes, [])
            snapshot = self.fence()
            await self.assert_pending_drain(snapshot)
            rejected = await self.client.get("/api/movies", params={"profile_id": "profile"})
            self.assertEqual(rejected.status_code, 503)
            self.assertEqual(rejected.json()["detail"]["code"], "lifecycle_work_blocked")
            self.assertEqual(rejected.headers["Retry-After"], "5")
            self.assertEqual(self.writes, [])
            self.assertFalse(pending.done())
            release.set()
            response = await asyncio.wait_for(pending, timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()], ranked_ids)
        self.assertEqual({item["title"] for item in response.json()}, {"First title", "Second title"})
        self.assertTrue(any("profilevibevector" in statement for statement in self.writes))
        self.assertTrue(any("recommendationruntimemetric" in statement for statement in self.writes))
        await self.assert_settled(snapshot, committed=True)

    async def test_pacing_only_get_retains_lease_through_commit_and_session_close(self) -> None:
        committing, closing = asyncio.Event(), asyncio.Event()
        commit_release, close_release = self.gate(), self.gate()

        class PausedSession(AsyncSession):
            async def commit(db):
                committing.set()
                await commit_release.wait()
                await super().commit()

            async def close(db):
                closing.set()
                await close_release.wait()
                await super().close()

        with patch.object(self.application, "AsyncSession", PausedSession), patch.object(
            settings, "RECOMMENDATION_V2_SHADOW", False
        ):
            pending = self.request()
            await self.wait(committing)
            self.assertTrue(any("profilevibevector" in statement for statement in self.writes))
            snapshot = self.fence()
            await self.assert_pending_drain(snapshot)
            async with AsyncSession(self.engine) as observer:
                self.assertIsNone(await observer.get(ProfileVibeVector, "profile"))
            commit_release.set()
            await self.wait(closing)
            await self.assert_pending_drain(snapshot)
            self.assertFalse(pending.done())
            async with AsyncSession(self.engine) as observer:
                self.assertIsNotNone(await observer.get(ProfileVibeVector, "profile"))
            close_release.set()
            response = await asyncio.wait_for(pending, timeout=5)
        self.assertEqual(response.status_code, 200)
        await self.assert_settled(snapshot, committed=True, shadow=False)

    async def test_cancelled_ranking_and_repeated_cleanup_cancellation_roll_back_before_release(self) -> None:
        ranked, closing, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        rank_release, close_release = self.gate(), self.gate()
        endpoint_tasks = []
        rank = self.application.rank_movies_for_profile

        async def paused_rank(db, *args):
            endpoint_tasks.append(asyncio.current_task())
            rows = await rank(db, *args)
            ranked.set()
            await rank_release.wait()
            return rows

        class PausedCloseSession(AsyncSession):
            async def close(db):
                closing.set()
                await close_release.wait()
                await super().close()
                closed.set()

        with patch.object(self.application, "AsyncSession", PausedCloseSession), patch.object(
            self.application, "rank_movies_for_profile", paused_rank
        ):
            pending = self.request()
            await self.wait(ranked)
            snapshot = self.fence()
            endpoint_tasks[0].cancel()
            await self.wait(closing)
            endpoint_tasks[0].cancel()
            await self.assert_pending_drain(snapshot)
            endpoint_tasks[0].cancel()
            await self.assert_pending_drain(snapshot)
            self.assertFalse(closed.is_set())
            close_release.set()
            await self.assert_cancelled_request(pending)
        self.assertTrue(closed.is_set())
        await self.assert_settled(snapshot, committed=False)

    async def test_cancellation_during_started_commit_waits_for_commit_and_cleanup(self) -> None:
        committing, committed, closing, closed = (asyncio.Event() for _ in range(4))
        commit_release, close_release = self.gate(), self.gate()
        endpoint_tasks = []
        rank = self.application.rank_movies_for_profile

        async def track_rank(db, *args):
            endpoint_tasks.append(asyncio.current_task())
            return await rank(db, *args)

        class PausedSession(AsyncSession):
            async def commit(db):
                committing.set()
                await commit_release.wait()
                await super().commit()
                committed.set()

            async def close(db):
                closing.set()
                await close_release.wait()
                await super().close()
                closed.set()

        with patch.object(self.application, "AsyncSession", PausedSession), patch.object(
            self.application, "rank_movies_for_profile", track_rank
        ):
            pending = self.request()
            await self.wait(committing)
            snapshot = self.fence()
            endpoint_tasks[0].cancel()
            await self.assert_pending_drain(snapshot)
            self.assertFalse(committed.is_set())
            self.assertFalse(closing.is_set())
            commit_release.set()
            await self.wait(closing)
            self.assertTrue(committed.is_set())
            endpoint_tasks[0].cancel()
            await self.assert_pending_drain(snapshot)
            self.assertFalse(closed.is_set())
            close_release.set()
            await self.assert_cancelled_request(pending)
        self.assertTrue(closed.is_set())
        await self.assert_settled(snapshot, committed=True)

    async def test_failed_commit_retains_lease_until_real_rollback_and_controlled_error(self) -> None:
        closing, closed = asyncio.Event(), asyncio.Event()
        close_release = self.gate()

        class FailedCommitSession(AsyncSession):
            async def commit(db):
                raise OperationalError("commit", {}, RuntimeError("database is locked"))

            async def close(db):
                closing.set()
                await close_release.wait()
                await super().close()
                closed.set()

        with patch.object(self.application, "AsyncSession", FailedCommitSession):
            pending = self.request()
            await self.wait(closing)
            self.assertTrue(any("recommendationruntimemetric" in statement for statement in self.writes))
            snapshot = self.fence()
            await self.assert_pending_drain(snapshot)
            self.assertFalse(closed.is_set())
            self.assertFalse(pending.done())
            close_release.set()
            response = await asyncio.wait_for(pending, timeout=5)
        self.assertTrue(closed.is_set())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "database_busy")
        await self.assert_settled(snapshot, committed=False)

    async def test_unpersonalized_read_is_unleased_and_available_under_writer_fence(self) -> None:
        snapshot = self.coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1,
        )
        with patch.object(self.application, "rank_movies_for_profile", AsyncMock()) as rank:
            response = await self.client.get("/api/movies")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()], ["m_1", "m_2"])
        rank.assert_not_awaited()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.coordinator.active_work(), ())
        self.assertEqual(snapshot.lease_ids, frozenset())
        self.assertTrue((await self.coordinator.drain(snapshot, timeout=0)).complete)
        self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
        self.coordinator.release_fence(snapshot)

    async def test_profile_authority_errors_precede_writer_admission(self) -> None:
        snapshot = self.coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1,
        )
        with patch.object(self.application, "rank_movies_for_profile", AsyncMock()) as rank:
            missing = await self.client.get("/api/movies", params={"profile_id": "missing"})
            protected = await self.client.get("/api/movies", params={"profile_id": "protected"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "profile_not_found")
        self.assertEqual(protected.status_code, 403)
        self.assertEqual(protected.json()["detail"]["code"], "profile_pin_required")
        rank.assert_not_awaited()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.coordinator.active_work(), ())
        self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
        self.coordinator.release_fence(snapshot)

    async def test_real_session_auth_preserves_reads_and_rejects_personalized_writes_under_fence(self) -> None:
        last_seen = time.time() - auth.SESSION_ACTIVITY_WRITE_INTERVAL - 1
        async with AsyncSession(self.engine) as db:
            session = await db.get(AuthSession, "catalog-session")
            session.last_seen_at = last_seen
            db.add(session)
            await db.commit()
        self.writes.clear()

        async def auth_database():
            async with AsyncSession(self.engine) as db:
                yield db

        # Keep the actual JWT/session and profile consumers. Only redirect the
        # auth database dependency to this test's disposable SQLite engine.
        self.application.app.dependency_overrides.pop(self.application.get_current_user)
        self.application.app.dependency_overrides.pop(self.application.get_current_session)
        self.application.app.dependency_overrides[auth.get_session] = auth_database
        token = jwt.encode(
            {"sub": "catalog@example.test", "jti": "catalog-session", "exp": time.time() + 3600},
            settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
        )
        snapshot = self.coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1,
        )
        with patch.object(self.application, "rank_movies_for_profile", AsyncMock()) as rank:
            unauthenticated = await self.client.get("/api/movies")
            headers = {"Authorization": f"Bearer {token}"}
            read = await self.client.get("/api/movies", headers=headers)
            personalized = await self.client.get(
                "/api/movies", params={"profile_id": "profile"}, headers=headers,
            )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(read.status_code, 200)
        self.assertEqual(personalized.status_code, 503)
        self.assertEqual(personalized.json()["detail"]["code"], "lifecycle_work_blocked")
        rank.assert_not_awaited()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.coordinator.active_work(), ())
        self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
        async with AsyncSession(self.engine) as observer:
            session = await observer.get(AuthSession, "catalog-session")
            self.assertEqual(session.last_seen_at, last_seen)
        self.coordinator.release_fence(snapshot)

    async def test_recommendation_feed_serializes_before_commit_and_rejects_closed_admission(self) -> None:
        response = await self.client.get("/api/recommendations/profile")
        self.assertEqual(response.status_code, 200)
        feed = RecommendationFeedResponse.model_validate(response.json())
        self.assertEqual(feed.profile_id, "profile")
        self.assertEqual(feed.category, "recommended")
        self.assertEqual({item.media.id for item in feed.items}, {"m_1", "m_2"})
        self.assertEqual({item.media.title for item in feed.items}, {"First title", "Second title"})
        self.assertTrue(all(item.viewer_preference == "love" for item in feed.items))
        self.assertEqual(self.coordinator.active_work(), ())
        self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
        async with AsyncSession(self.engine) as observer:
            self.assertIsNotNone(await observer.get(ProfileVibeVector, "profile"))
            metric = await observer.get(RecommendationRuntimeMetric, "profile")
            generated_at = metric.generated_at
        self.writes.clear()
        snapshot = self.coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1,
        )
        with patch.object(recommendation, "rank_movies_for_profile", AsyncMock()) as rank:
            rejected = await self.client.get("/api/recommendations/profile")
            invalid_scope = await self.client.get("/api/recommendations/profile", params={"scope": "invalid"})
            missing = await self.client.get("/api/recommendations/missing")
            protected = await self.client.get("/api/recommendations/protected")
        self.assertEqual(rejected.status_code, 503)
        self.assertEqual(rejected.json()["detail"]["code"], "lifecycle_work_blocked")
        self.assertEqual(invalid_scope.status_code, 400)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(protected.status_code, 403)
        rank.assert_not_awaited()
        self.assertEqual(self.writes, [])
        async with AsyncSession(self.engine) as observer:
            self.assertEqual((await observer.get(RecommendationRuntimeMetric, "profile")).generated_at, generated_at)
        self.coordinator.release_fence(snapshot)

    async def test_recommendation_ranker_exception_retains_lease_until_rollback_finishes(self) -> None:
        closing, closed = asyncio.Event(), asyncio.Event()
        close_release = self.gate()
        rank = recommendation.rank_movies_for_profile

        async def failing_rank(db, *args):
            await rank(db, *args)
            raise RuntimeError("Synthetic failure after actual ranking writes")

        class PausedCloseSession(AsyncSession):
            async def close(db):
                closing.set()
                await close_release.wait()
                await super().close()
                closed.set()

        with patch.object(self.application, "AsyncSession", PausedCloseSession), patch.object(
            recommendation, "rank_movies_for_profile", failing_rank
        ):
            pending = self.feed_request()
            await self.wait(closing)
            self.assertTrue(any("profilevibevector" in statement for statement in self.writes))
            self.assertTrue(any("recommendationruntimemetric" in statement for statement in self.writes))
            snapshot = self.fence()
            await self.assert_pending_drain(snapshot)
            self.assertFalse(closed.is_set())
            close_release.set()
            with self.assertRaisesRegex(RuntimeError, "Synthetic failure after actual ranking writes"):
                await asyncio.wait_for(pending, timeout=5)
        self.assertTrue(closed.is_set())
        await self.assert_settled(snapshot, committed=False)

    async def test_recommendation_repeated_cancellation_retains_writer_until_rollback(self) -> None:
        ranked, closing, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        rank_release, close_release = self.gate(), self.gate()
        endpoint_tasks = []
        rank = recommendation.rank_movies_for_profile

        async def paused_rank(db, *args):
            endpoint_tasks.append(asyncio.current_task())
            rows = await rank(db, *args)
            ranked.set()
            await rank_release.wait()
            return rows

        class PausedCloseSession(AsyncSession):
            async def close(db):
                closing.set()
                await close_release.wait()
                await super().close()
                closed.set()

        with patch.object(self.application, "AsyncSession", PausedCloseSession), patch.object(
            recommendation, "rank_movies_for_profile", paused_rank
        ):
            pending = self.feed_request()
            await self.wait(ranked)
            snapshot = self.fence()
            endpoint_tasks[0].cancel()
            await self.wait(closing)
            endpoint_tasks[0].cancel()
            await self.assert_pending_drain(snapshot)
            endpoint_tasks[0].cancel()
            await self.assert_pending_drain(snapshot)
            self.assertFalse(closed.is_set())
            close_release.set()
            await self.assert_cancelled_request(pending)
        self.assertTrue(closed.is_set())
        await self.assert_settled(snapshot, committed=False)

    async def test_recommendation_native_sqlite_commit_cancellation_remains_drain_visible(self) -> None:
        native_entered, native_finished, closed = (asyncio.Event() for _ in range(3))
        native_release = threading.Event()
        self.gates.append(native_release)
        endpoint_tasks = []
        rank = recommendation.rank_movies_for_profile
        loop = asyncio.get_running_loop()

        async def track_rank(db, *args):
            endpoint_tasks.append(asyncio.current_task())
            return await rank(db, *args)

        class PausedNativeCommitSession(AsyncSession):
            async def commit(db):
                connection = await db.connection()
                driver = connection.sync_connection.connection.driver_connection
                execute = driver._execute
                native_commit = driver._conn.commit

                async def guarded_execute(function, *args, **kwargs):
                    if function != native_commit:
                        return await execute(function, *args, **kwargs)

                    def delayed_native_commit():
                        # The production commit is already queued on the real
                        # SQLite worker. Cancelling its Python waiter cannot
                        # stop this native transaction from finishing.
                        loop.call_soon_threadsafe(native_entered.set)
                        if not native_release.wait(timeout=5):
                            raise RuntimeError("Synthetic native commit gate timed out")
                        result = function(*args, **kwargs)
                        loop.call_soon_threadsafe(native_finished.set)
                        return result

                    return await execute(delayed_native_commit)

                driver._execute = guarded_execute
                try:
                    await super().commit()
                finally:
                    driver._execute = execute

            async def close(db):
                await super().close()
                closed.set()

        with patch.object(self.application, "AsyncSession", PausedNativeCommitSession), patch.object(
            recommendation, "rank_movies_for_profile", track_rank
        ):
            pending = self.feed_request()
            await self.wait(native_entered)
            snapshot = self.fence()
            endpoint_tasks[0].cancel()
            await self.assert_pending_drain(snapshot)
            self.assertFalse(native_finished.is_set())
            self.assertFalse(closed.is_set())
            endpoint_tasks[0].cancel()
            await self.assert_pending_drain(snapshot)
            self.assertFalse(native_finished.is_set())
            self.assertFalse(closed.is_set())
            native_release.set()
            await self.assert_cancelled_request(pending)
        self.assertTrue(native_finished.is_set())
        self.assertTrue(closed.is_set())
        await self.assert_settled(snapshot, committed=True)


if __name__ == "__main__":
    unittest.main()
