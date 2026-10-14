"""Recommendation provider failures and SQLite transaction regressions.

Run only in a disposable installation: application imports resolve configuration
relative to the copied server directory, not to a database environment override.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from models import (
    Movie,
    Profile,
    ProfileMediaPreference,
    ProfileVibeVector,
    RecommendationRefreshState,
    RecommendationRuntimeMetric,
)
from services import recommendation, tmdb


def candidate(movie_id: str, pacing: float) -> Movie:
    return Movie(
        id=movie_id,
        title=movie_id,
        description="Synthetic candidate",
        thumbnail_url="",
        video_url="",
        duration="2h",
        release_year=2025,
        dialogue_wpm=pacing,
        dialogue_confidence=0.9,
        catalog_source="tmdb_cache",
        availability="cached",
    )


class OneIterationStop:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    async def wait(self) -> bool:
        self.stopped = True
        return True


class RecommendationReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "recommendations.db"
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.database}",
            pool_size=2,
            max_overflow=0,
            pool_timeout=1,
            connect_args={"timeout": 0.05},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_connection(connection, record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=50")
            cursor.close()

        self.engine_patch = patch.object(recommendation, "engine", self.engine)
        self.engine_patch.start()
        self.shadow_patch = patch.object(settings, "RECOMMENDATION_V2_SHADOW", True)
        self.shadow_patch.start()
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(self.engine) as db:
            db.add(Profile(id="profile", name="Synthetic profile"))
            await db.commit()

    async def asyncTearDown(self) -> None:
        self.shadow_patch.stop()
        self.engine_patch.stop()
        await self.engine.dispose()
        self.temporary.cleanup()

    async def refresh_state(self) -> RecommendationRefreshState | None:
        async with AsyncSession(self.engine) as db:
            return await db.get(RecommendationRefreshState, "profile")

    async def save_state(self, **values) -> None:
        async with AsyncSession(self.engine) as db:
            state = await db.get(RecommendationRefreshState, "profile")
            state = state or RecommendationRefreshState(profile_id="profile")
            for field, value in values.items():
                setattr(state, field, value)
            db.add(state)
            await db.commit()

    async def test_shadow_metric_uses_ranking_writer_with_two_wal_connections(self) -> None:
        # Establish two actual SQLite connections, rather than StaticPool hiding
        # the competing writer. The old engine.begin() metric write fails here.
        async with self.engine.connect() as first, self.engine.connect() as second:
            self.assertIsNot(first.sync_connection.connection.driver_connection,
                             second.sync_connection.connection.driver_connection)
            self.assertEqual((await second.execute(text("PRAGMA journal_mode"))).scalar(), "wal")

        movies = [candidate("m_1", 55), candidate("m_2", 130)]
        async with AsyncSession(self.engine) as db:
            db.add_all(movies)
            for movie in movies:
                db.add(ProfileMediaPreference(profile_id="profile", movie_id=movie.id, preference="love", updated_at=1_000_000))
            await db.commit()

        writes: list[tuple[str, int]] = []

        @event.listens_for(self.engine.sync_engine, "before_cursor_execute")
        def record_writes(connection, cursor, statement, parameters, context, executemany) -> None:
            if statement.startswith("INSERT INTO profilevibevector") or statement.startswith("INSERT INTO recommendationruntimemetric"):
                writes.append((statement.split()[2], id(connection.connection.driver_connection)))

        async with AsyncSession(self.engine) as db:
            rows = list((await db.exec(select(Movie))).all())
            await asyncio.wait_for(recommendation.rank_movies_for_profile(db, "profile", rows), timeout=2)
            async with self.engine.connect() as observer:
                self.assertEqual((await observer.execute(text("SELECT COUNT(*) FROM recommendationruntimemetric"))).scalar(), 0)
            await db.commit()
        self.assertEqual({name for name, _ in writes}, {"profilevibevector", "recommendationruntimemetric"})
        self.assertEqual(len({connection for _, connection in writes}), 1)
        async with AsyncSession(self.engine) as db:
            self.assertIsNotNone(await db.get(ProfileVibeVector, "profile"))
            self.assertIsNotNone(await db.get(RecommendationRuntimeMetric, "profile"))

    async def test_shadow_metric_rolls_back_with_ranking(self) -> None:
        async with AsyncSession(self.engine) as db:
            await recommendation.rank_movies_for_profile(db, "profile", [])
            await db.rollback()
        async with AsyncSession(self.engine) as db:
            self.assertIsNone(await db.get(RecommendationRuntimeMetric, "profile"))

    async def test_provider_failures_preserve_last_success_and_schedule_retry(self) -> None:
        now = 1_000_000.0
        for failure in (401, 429, 503, "timeout"):
            with self.subTest(failure=failure):
                await self.save_state(last_tmdb_refresh_at=now - 100, next_tmdb_refresh_at=now - 1,
                                      last_error=None, refresh_requested=False)

                def provider(request: httpx.Request) -> httpx.Response:
                    if failure == "timeout":
                        raise httpx.ReadTimeout("Synthetic timeout", request=request)
                    return httpx.Response(failure, json={"message": "Synthetic failure"}, request=request)

                client = tmdb.TMDBClient()
                client.api_key = "synthetic-key"
                client.read_access_token = ""
                client._client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
                try:
                    with patch.object(tmdb, "tmdb_client", client), patch.object(recommendation.time, "time", return_value=now), patch.object(tmdb.asyncio, "sleep", new=AsyncMock()):
                        self.assertFalse(await recommendation.refresh_profile_cache("profile"))
                    state = await self.refresh_state()
                    self.assertEqual(state.last_tmdb_refresh_at, now - 100)
                    self.assertEqual(state.next_tmdb_refresh_at, now + recommendation.TMDB_RETRY_SECONDS)
                    self.assertEqual(state.last_error, "TMDB refresh failed (TMDBDiscoveryError)")
                finally:
                    await client.close()

    async def test_complete_empty_discovery_is_success_and_not_retried_for_shortage(self) -> None:
        now = 1_000_000.0
        client = tmdb.TMDBClient()
        client._get = AsyncMock(return_value={"results": []})
        with patch.object(tmdb, "tmdb_client", client), patch.object(recommendation.time, "time", return_value=now):
            self.assertTrue(await recommendation.refresh_profile_cache("profile"))
            state = await self.refresh_state()
            self.assertEqual(state.last_tmdb_refresh_at, now)
            self.assertIsNone(state.last_error)
            self.assertEqual(state.next_tmdb_refresh_at, now + recommendation.TMDB_REFRESH_SECONDS)
            self.assertFalse(await recommendation.refresh_profile_cache("profile"))
        self.assertEqual(client._get.await_count, 12)

    async def test_partial_discovery_does_not_mark_refresh_successful(self) -> None:
        now = 1_000_000.0
        await self.save_state(last_tmdb_refresh_at=now - 100, next_tmdb_refresh_at=now - 1)
        client = tmdb.TMDBClient()
        client._get = AsyncMock(side_effect=[{"results": []}, None])
        with patch.object(tmdb, "tmdb_client", client), patch.object(recommendation.time, "time", return_value=now):
            self.assertFalse(await recommendation.refresh_profile_cache("profile"))
        state = await self.refresh_state()
        self.assertEqual(state.last_tmdb_refresh_at, now - 100)
        self.assertEqual(state.next_tmdb_refresh_at, now + recommendation.TMDB_RETRY_SECONDS)
        self.assertIsNotNone(state.last_error)

    async def test_related_discovery_rejects_failed_endpoint_before_caching_partial_data(self) -> None:
        client = tmdb.TMDBClient()
        client._get = AsyncMock(side_effect=[{"results": [{"id": 123, "title": "Synthetic"}]}, None])
        client._upsert_search_results = AsyncMock()
        with self.assertRaises(tmdb.TMDBDiscoveryError):
            await client.discover_related_media(99, "movie", limit=1, require_complete=True)
        client._upsert_search_results.assert_not_awaited()

    async def test_malformed_required_related_identity_retains_success_and_short_retry(self) -> None:
        now = 1_000_000.0
        async with AsyncSession(self.engine) as db:
            seed = candidate("m_99", 0)
            seed.tmdb_id = 99
            db.add(seed)
            db.add(ProfileMediaPreference(profile_id="profile", movie_id=seed.id,
                preference="like", updated_at=now))
            await db.commit()
        for malformed in ({"title": "No identity"}, {"id": None}, {"id": 0},
                          {"id": -1}, {"id": True}, {"id": "123"},
                          {"id": 1.5}, {"id": [123]}):
            for endpoint in ("/recommendations", "/similar"):
                with self.subTest(item=malformed, endpoint=endpoint):
                    await self.save_state(last_tmdb_refresh_at=now - 100,
                        next_tmdb_refresh_at=now - 1, last_error="Prior failure")
                    observed = []
                    def provider(request):
                        observed.append(request.url.path)
                        results = [malformed] if request.url.path.endswith(endpoint) else []
                        return httpx.Response(200, json={"results": results}, request=request)
                    client = tmdb.TMDBClient()
                    client.api_key = "synthetic-key"
                    client.read_access_token = ""
                    client._client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
                    try:
                        with patch.object(tmdb, "tmdb_client", client), patch.object(
                                recommendation.time, "time", return_value=now):
                            self.assertFalse(await recommendation.refresh_profile_cache("profile"))
                        state = await self.refresh_state()
                        self.assertEqual(state.last_tmdb_refresh_at, now - 100)
                        self.assertEqual(state.next_tmdb_refresh_at, now + recommendation.TMDB_RETRY_SECONDS)
                        self.assertEqual(state.last_error, "TMDB refresh failed (TMDBDiscoveryError)")
                        self.assertTrue(any(path.endswith(endpoint) for path in observed))
                        async with AsyncSession(self.engine) as db:
                            self.assertEqual(len((await db.exec(select(Movie))).all()), 1)
                    finally:
                        await client.close()

    async def test_related_strict_identity_validation_precedes_partial_caching(self) -> None:
        client = tmdb.TMDBClient()
        client._get = AsyncMock(side_effect=[{"results": [{"id": 123}]}, {"results": [{"title": "No identity"}]}])
        client._upsert_search_results = AsyncMock()
        with self.assertRaises(tmdb.TMDBDiscoveryError):
            await client.discover_related_media(99, "movie", require_complete=True)
        client._upsert_search_results.assert_not_awaited()

    async def test_optional_metadata_and_related_exclusions_remain_valid(self) -> None:
        client = tmdb.TMDBClient()
        client._get = AsyncMock(return_value={"results": [{"id": 99}, {"id": 123},
            {"id": 123}, {"id": 124, "adult": True}]})
        client._upsert_search_results = AsyncMock(side_effect=lambda rows: rows)
        rows = await client.discover_related_media(99, "movie", require_complete=True)
        self.assertEqual([row["id"] for row in rows], ["m_123"])
        self.assertEqual(rows[0]["title"], "Unknown Title")
        for item in ({}, {"id": False}, {"id": "invalid"}):
            self.assertEqual(tmdb._discovery_results({"results": [item]}, require_complete=False), [])

    async def test_malformed_discovery_is_failure_but_public_empty_fallback_is_preserved(self) -> None:
        for payload in (None, {}, {"results": None}, {"results": ["invalid"]}):
            with self.subTest(payload=payload):
                client = tmdb.TMDBClient()
                client._get = AsyncMock(return_value=payload)
                with self.assertRaises(tmdb.TMDBDiscoveryError):
                    await client.discover_media("trending", require_complete=True)
                self.assertEqual(await client.discover_media("trending"), [])

    async def test_worker_uses_six_hour_deadline_and_wakes_for_short_retry(self) -> None:
        now = 1_000_000.0
        await self.save_state(last_tmdb_refresh_at=now - 100, next_tmdb_refresh_at=now - 1,
                              refresh_requested=False)
        client = tmdb.TMDBClient()
        client._get = AsyncMock(return_value=None)
        delays: list[float] = []
        real_wait_for = asyncio.wait_for

        async def capture_delay(awaitable, timeout):
            delays.append(timeout)
            return await real_wait_for(awaitable, timeout)

        with patch.object(tmdb, "tmdb_client", client), patch.object(recommendation.time, "time", return_value=now), patch("services.vibe_analysis.backfill_catalog_vibes", new=AsyncMock(return_value=0)), patch.object(asyncio, "wait_for", side_effect=capture_delay):
            await recommendation.recommendation_worker(OneIterationStop())
        self.assertEqual(client._get.await_count, 1)
        self.assertEqual(delays, [recommendation.TMDB_RETRY_SECONDS])

    async def test_requested_local_ranking_does_not_bypass_retry_deadline(self) -> None:
        now = 1_000_000.0
        await self.save_state(last_tmdb_refresh_at=now - 100, next_tmdb_refresh_at=now + 900,
                              refresh_requested=True, last_error="Prior failure")
        client = tmdb.TMDBClient()
        client._get = AsyncMock()
        with patch.object(tmdb, "tmdb_client", client), patch.object(recommendation.time, "time", return_value=now):
            self.assertFalse(await recommendation.refresh_profile_cache("profile"))
        client._get.assert_not_awaited()
        state = await self.refresh_state()
        self.assertFalse(state.refresh_requested)
        self.assertEqual(state.last_error, "Prior failure")
        self.assertEqual(state.next_tmdb_refresh_at, now + 900)

    def test_legacy_refresh_state_has_six_hour_deadline(self) -> None:
        state = RecommendationRefreshState(profile_id="profile", last_tmdb_refresh_at=100)
        self.assertEqual(recommendation._tmdb_refresh_deadline(state), 100 + recommendation.TMDB_REFRESH_SECONDS)
        self.assertEqual(recommendation._tmdb_refresh_deadline(None), 0)


if __name__ == "__main__":
    unittest.main()
