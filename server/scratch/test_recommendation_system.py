"""Isolated regression checks for the personalized recommendation engine."""

import asyncio
import json
import os
import shutil
import tempfile
import time

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import services.recommendation as recommendation
import db as db_module
from config import settings
from models import Episode, Movie, Profile, ProfileMediaPreference, ProfileTaste, RecommendationExposure, RecommendationExposureInput, RecommendationFeedResponse, TelemetryRequest, ViewingAttempt
from services.tmdb import tmdb_client


def movie(movie_id: str, title: str, genres, *, available: bool, media_type: str = "movie") -> Movie:
    item = Movie(
        id=movie_id,
        tmdb_id=int(movie_id.split("_")[-1]),
        title=title,
        description="",
        thumbnail_url="",
        banner_url="",
        video_url="/media/video.mp4" if available and media_type == "movie" else "",
        duration="2h",
        release_year=2025,
        rating="PG-13",
        director="Director",
        type=media_type,
        vote_average=8.0,
        vote_count=1000,
        catalog_source="server" if available else "tmdb_cache",
        availability="available" if available else "cached",
    )
    item.genres = list(genres)
    item.cast = ["Lead Actor"]
    return item


async def main() -> None:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    original_engine = recommendation.engine
    original_db_engine = db_module.engine
    original_media_dir = settings.MEDIA_DIR
    cache_test_dir = os.path.join("temp", f"recommendation-regression-{os.getpid()}")
    recommendation.engine = test_engine
    db_module.engine = test_engine
    settings.MEDIA_DIR = cache_test_dir
    try:
        async with test_engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

        async with AsyncSession(test_engine) as db:
            db.add(Profile(id="profile", name="Profile"))
            db.add(movie("m_1", "Playable Action", ["Action"], available=True))
            db.add(movie("m_2", "Cached Drama", ["Drama"], available=False))
            series = movie("tv_3", "Playable Series", ["Action"], available=True, media_type="series")
            db.add(series)
            db.add(Episode(id="ep_3_s1_e1", movie_id=series.id, episode_number=1, season_number=1, title="Episode", description="", thumbnail_url="", video_url="/media/episode.mp4", duration="45m"))
            await db.commit()

        cached_id = await tmdb_client.cache_media_locally({
            "tmdb_id": 99,
            "type": "movie",
            "title": "Cached Candidate",
            "description": "Metadata only",
            "release_year": 2024,
            "genres": ["Science Fiction"],
            "cast": ["Actor"],
            "director": "Director",
            "crew": [{"name": "Director", "roles": ["Director"]}],
            "keywords": ["space"],
            "trope_vectors": [{"id": "cosmic_survival", "label": "Cosmic Survival", "railLabel": "Beyond Earth", "confidence": 0.8}],
            "duration": "2h",
            "vote_average": 7.8,
            "vote_count": 800,
            "popularity": 50.0,
        }, "", "")
        assert cached_id == "m_99"
        async with AsyncSession(test_engine) as db:
            cached = await db.get(Movie, cached_id)
            assert cached and cached.catalog_source == "tmdb_cache" and cached.availability == "cached" and not cached.video_url
        server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        metadata_files = []
        for root, _, files in os.walk(os.path.join(server_root, cache_test_dir)):
            metadata_files.extend(os.path.join(root, name) for name in files if name == "metadata.json")
        assert len(metadata_files) == 1
        with open(metadata_files[0], "r", encoding="utf-8") as cached_metadata:
            metadata = json.load(cached_metadata)
        assert metadata["catalog_source"] == "tmdb_cache" and metadata["availability"] == "cached" and metadata["video_url"] == ""

        assert await recommendation.process_telemetry_event("profile", TelemetryRequest(event_type="card_click", movie_id="m_1"))
        assert not await recommendation.process_telemetry_event("profile", TelemetryRequest(event_type="card_click", movie_id="m_1")), "hourly click dedupe failed"
        assert not await recommendation.process_telemetry_event("profile", TelemetryRequest(event_type="watchlist_add", movie_id="m_1")), "browser must not forge authoritative signals"
        assert await recommendation.record_authoritative_signal("profile", "m_1", "watchlist_add")

        assert (await recommendation.set_media_preference("profile", "m_2", "like"))["preference"] == "like"
        assert (await recommendation.set_media_preference("profile", "m_2", "love"))["preference"] == "love"
        assert (await recommendation.set_media_preference("profile", "m_2", "dislike"))["preference"] == "dislike"
        assert await recommendation.get_media_preferences("profile") == {"m_2": "dislike"}
        exposure = RecommendationExposureInput(movie_id="m_1", feed_generation="generation", surface="home-card", scope="home", category="recommended", position=0)
        assert await recommendation.record_recommendation_exposures("profile", [exposure]) == 1
        assert await recommendation.record_recommendation_exposures("profile", [exposure]) == 0, "impressions must be idempotent"

        async with AsyncSession(test_engine) as db:
            tastes = list((await db.exec(select(ProfileTaste))).all())
            assert tastes and any(taste.tag_type == "genre" and taste.tag_value == "action" for taste in tastes)
            payload = await recommendation.build_recommendation_payload(db, "profile", "home", "all", 50, 0)
            assert payload["total"] == 2, "All Releases must contain only the playable movie and series"
            assert all(item["availability"] == "available" for item in payload["items"])
            genre_payload = await recommendation.build_recommendation_payload(db, "profile", "home", "drama", 50, 0)
            assert genre_payload["total"] == 0, "disliked titles must leave personalized genre feeds"
            recommended = await recommendation.build_recommendation_payload(db, "profile", "home", "recommended", 50, 0)
            validated = RecommendationFeedResponse.model_validate(recommended)
            assert validated.algorithm_version.startswith("v2") and isinstance(validated.vibe_rails, list)
            assert all(item["media"].id != "m_2" for item in recommended["items"])
            assert len(list((await db.exec(select(ProfileMediaPreference))).all())) == 1, "preference transitions must update one row"
            assert len(list((await db.exec(select(RecommendationExposure))).all())) == 1

        first_attempt = await recommendation.record_playback_progress("profile", "m_1", None, 4000, 4000, 0.80, False)
        assert first_attempt
        async with AsyncSession(test_engine) as db:
            attempt = await db.get(ViewingAttempt, first_attempt)
            attempt.last_seen_at = time.time() - recommendation.ATTEMPT_GAP_SECONDS - 5
            db.add(attempt)
            await db.commit()
        second_attempt = await recommendation.record_playback_progress("profile", "m_1", None, 0, 0, 0.0, False)
        assert second_attempt and second_attempt != first_attempt
        await recommendation.record_playback_progress("profile", "m_1", None, 3000, 3000, 0.55, False)
        await recommendation.set_media_preference("profile", "m_1", "dislike")
        async with AsyncSession(test_engine) as db:
            attempts = list((await db.exec(select(ViewingAttempt).where(ViewingAttempt.movie_id == "m_1"))).all())
            assert any(attempt.rewatch_reward == 1.0 for attempt in attempts), "genuine rewatch reward was not recorded"
            payload = await recommendation.build_recommendation_payload(db, "profile", "home", "recommended", 50, 0)
            assert all(item["media"].id != "m_1" for item in payload["items"]), "dislike must hide personalized recommendations"
            assert [item["media"].id for item in payload["watch_again"]] == ["m_1"], "Watch Again must remain pure history even after dislike"

        half_life_taste = ProfileTaste(profile_id="profile", tag_type="genre", tag_value="action", score=10.0, last_updated=time.time() - 90 * 86400)
        assert 4.95 <= recommendation.decayed_score(half_life_taste) <= 5.05
        print("Recommendation regression checks passed.")
    finally:
        recommendation.engine = original_engine
        db_module.engine = original_db_engine
        settings.MEDIA_DIR = original_media_dir
        await test_engine.dispose()
        shutil.rmtree(os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), cache_test_dir), ignore_errors=True)
        for suffix in ("", "-shm", "-wal"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
