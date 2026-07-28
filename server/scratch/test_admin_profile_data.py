"""Regression checks for cross-profile administrative data inspection."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import main as main_module
from config import settings
from models import (
    Movie,
    PlaybackMilestone,
    PlaybackRun,
    PlaybackSession,
    Profile,
    ProfileMediaPreference,
    ProfileOnboardingPreference,
    ProfileRecommendation,
    ProfileTaste,
    RecommendationExposure,
    RecommendationRefreshState,
    RecommendationRuntimeMetric,
    TelemetryEvent,
    User,
    ViewingAttempt,
    WatchlistItem,
)
from routes import admin_profiles
from routes.auth import require_recent_reauth


async def main() -> None:
    handle, database_path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    media_temp = tempfile.TemporaryDirectory()
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    original_main_engine = main_module.engine
    original_media_dir = settings.MEDIA_DIR
    main_module.engine = test_engine
    settings.MEDIA_DIR = media_temp.name
    now = time.time()
    try:
        for route in admin_profiles.router.routes:
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            assert require_recent_reauth in dependencies, f"{route.path} is missing recent administrator reauthentication"

        async with test_engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

        folder = Path(media_temp.name) / "Movies" / "Cached_2026_TMDB_22"
        metadata_dir = folder / ".metadata"
        metadata_dir.mkdir(parents=True)
        (folder / "poster.jpg").write_bytes(b"poster")
        (folder / "backdrop.jpg").write_bytes(b"backdrop")
        (metadata_dir / "metadata.json").write_text(json.dumps({"catalog_source": "tmdb_cache"}), encoding="utf-8")

        async with AsyncSession(test_engine, expire_on_commit=False) as db:
            db.add(Profile(id="1", name="Administrator"))
            db.add(Profile(id="child", name="Child", pin_enabled=True, pin_hash="not-serialized"))
            db.add(Movie(
                id="m_22",
                tmdb_id=22,
                title="Cached",
                description="",
                thumbnail_url="/media/Movies/Cached_2026_TMDB_22/poster.jpg",
                banner_url="/media/Movies/Cached_2026_TMDB_22/backdrop.jpg",
                video_url="",
                duration="2h",
                release_year=2026,
                type="movie",
                catalog_source="tmdb_cache",
                availability="cached",
                cached_at=now,
                local_thumbnail_url="/media/Movies/Cached_2026_TMDB_22/poster.jpg",
                local_banner_url="/media/Movies/Cached_2026_TMDB_22/backdrop.jpg",
                cache_state="ready",
            ))
            db.add(PlaybackSession(profile_id="child", movie_id="m_22", timestamp=120, duration_watched=120, completion_rate=0.25, updated_at="2026-07-28T12:00:00+00:00"))
            db.add(WatchlistItem(profile_id="child", movie_id="m_22", created_at="2026-07-28T11:00:00+00:00"))
            db.add(ProfileMediaPreference(profile_id="child", movie_id="m_22", preference="love", updated_at=now))
            db.add(ProfileOnboardingPreference(profile_id="child", kind="genre", value="science fiction", updated_at=now))
            db.add(ProfileTaste(profile_id="child", tag_type="genre", tag_value="science fiction", score=3.5, last_updated=now))
            recommendation = ProfileRecommendation(profile_id="child", movie_id="m_22", media_type="movie", score=0.91, generated_at=now, candidate_source="tmdb_related", source_confidence=0.8)
            recommendation.reasons = ["Because you like science fiction"]
            db.add(recommendation)
            attempt = ViewingAttempt(id="attempt", profile_id="child", movie_id="m_22", started_at=now - 120, last_seen_at=now, max_completion=0.25, duration_watched=120)
            db.add(attempt)
            db.add(PlaybackMilestone(attempt_id="attempt", milestone=10, recorded_at=now))
            db.add(TelemetryEvent(profile_id="child", event_type="card_click", movie_id="m_22", timestamp=now, metadata_json="{}"))
            db.add(RecommendationExposure(id="exposure", profile_id="child", movie_id="m_22", feed_generation="feed", surface="home", scope="home", category="recommended", position=0, shown_at=now, dedupe_key="unique"))
            db.add(PlaybackRun(id="run", profile_id="child", movie_id="m_22", lifecycle_state="active", created_at=now, updated_at=now, last_seen_at=now, last_progress_at=now))
            db.add(RecommendationRefreshState(profile_id="child", taste_version=2, refresh_requested=False))
            db.add(RecommendationRuntimeMetric(profile_id="child", top20_overlap=17, mean_displacement=1.25, generated_at=now))
            await db.commit()

            summaries = await admin_profiles.list_profile_data_summaries(None, db)  # type: ignore[arg-type]
            child_summary = next(item for item in summaries["profiles"] if item["id"] == "child")
            assert child_summary["historyCount"] == 1 and child_summary["watchlistCount"] == 1

            overview = await admin_profiles.get_profile_data_overview("child", None, db)  # type: ignore[arg-type]
            assert overview["profile"]["pinEnabled"] is True
            assert "pin_hash" not in json.dumps(overview)
            assert overview["counts"]["recommendations"] == 1

            history = await admin_profiles.get_profile_history("child", 100, 0, None, db)  # type: ignore[arg-type]
            assert history["items"][0]["movie"]["title"] == "Cached"
            watchlist = await admin_profiles.get_profile_watchlist_data("child", 100, 0, None, db)  # type: ignore[arg-type]
            assert watchlist["items"][0]["movie"]["id"] == "m_22"
            recommendations = await admin_profiles.get_profile_recommendation_data("child", 100, 0, None, db)  # type: ignore[arg-type]
            assert recommendations["runtimeMetric"]["top20Overlap"] == 17
            assert recommendations["items"][0]["preference"] == "love"
            activity = await admin_profiles.get_profile_activity("child", 100, 0, None, db)  # type: ignore[arg-type]
            assert activity["events"][0]["type"] == "card_click"
            cache = await admin_profiles.get_profile_tmdb_cache("child", 100, 0, None, db)  # type: ignore[arg-type]
            assert cache["items"][0]["files"]["poster"]["storedOnDisk"] is True
            assert cache["items"][0]["files"]["metadata"]["storedOnDisk"] is True
            assert cache["items"][0]["associationSources"] == ["activity", "love preference", "recommendation pool", "watchlist"]

        await main_module.delete_profile("child", User(id=1, email="admin@example.com", password_hash="hash"))
        async with AsyncSession(test_engine) as db:
            assert await db.get(Profile, "child") is None
            assert await db.get(Movie, "m_22") is not None, "shared TMDB catalog must survive profile deletion"
            profile_owned_models = (
                PlaybackSession,
                WatchlistItem,
                ProfileMediaPreference,
                ProfileOnboardingPreference,
                ProfileTaste,
                ProfileRecommendation,
                ViewingAttempt,
                TelemetryEvent,
                RecommendationExposure,
                PlaybackRun,
                RecommendationRefreshState,
                RecommendationRuntimeMetric,
            )
            for model in profile_owned_models:
                remaining = list((await db.exec(select(model).where(model.profile_id == "child"))).all())
                assert remaining == [], f"{model.__name__} rows were orphaned"
            assert list((await db.exec(select(PlaybackMilestone))).all()) == []
        print("Admin profile data regression checks passed.")
    finally:
        main_module.engine = original_main_engine
        settings.MEDIA_DIR = original_media_dir
        await test_engine.dispose()
        media_temp.cleanup()
        for suffix in ("", "-shm", "-wal"):
            try:
                os.remove(database_path + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
