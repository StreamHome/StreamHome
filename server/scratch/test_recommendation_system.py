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
from models import Episode, Movie, PlaybackMilestone, Profile, ProfileMediaPreference, ProfileTaste, RecommendationExposure, RecommendationExposureInput, RecommendationFeedResponse, RecommendationRuntimeMetric, TelemetryEvent, TelemetryRequest, ViewingAttempt
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


async def check_confirmed_rewatch_history(test_engine) -> None:
    """An authorized rewatch keeps completed history even outside restart heuristics."""
    async with AsyncSession(test_engine) as db:
        db.add(Profile(id="rewatch-history", name="Rewatch History"))
        db.add(movie("m_201", "Late First Rewatch Report", ["Action"], available=True))
        db.add(movie("m_202", "Early Credits Completion", ["Drama"], available=True))
        await db.commit()

    for movie_id, previous_position, previous_watched, previous_completion in (
        ("m_201", 4000, 4000, 0.80),
        ("m_202", 80, 80, 0.08),
    ):
        completed_id = await recommendation.record_playback_progress(
            "rewatch-history", movie_id, None, previous_position,
            previous_watched, previous_completion, True,
        )
        assert completed_id
        async with AsyncSession(test_engine) as db:
            completed = await db.get(ViewingAttempt, completed_id)
            assert completed and completed.completed_at is not None
            completed.rewatch_reward = 0.60
            db.add(completed)
            await db.commit()
            await db.refresh(completed)
            historical = completed.model_dump()
            milestones = [item.model_dump() for item in (await db.exec(
                select(PlaybackMilestone).where(PlaybackMilestone.attempt_id == completed_id).order_by(PlaybackMilestone.id)
            )).all()]
            rewards = [item.model_dump() for item in (await db.exec(
                select(TelemetryEvent).where(TelemetryEvent.profile_id == "rewatch-history", TelemetryEvent.movie_id == movie_id).order_by(TelemetryEvent.id)
            )).all()]

        new_id = await recommendation.record_playback_progress(
            "rewatch-history", movie_id, None, 75, 12, 75 / 7200, False,
            start_new_attempt=True,
        )
        assert new_id and new_id != completed_id, "confirmed rewatch must start separate history even after 60 seconds or low credits completion"
        continued_id = await recommendation.record_playback_progress(
            "rewatch-history", movie_id, None, 85, 20, 85 / 7200, False,
        )
        assert continued_id == new_id, "ordinary follow-up progress must retain the newly opened attempt"
        async with AsyncSession(test_engine) as db:
            completed = await db.get(ViewingAttempt, completed_id)
            current = await db.get(ViewingAttempt, new_id)
            assert completed and completed.model_dump() == historical, "rewatch must not rewrite completed history or its reward"
            assert current and current.completed_at is None and not current.early_exit_recorded
            assert current.max_completion == 85 / 7200 and current.duration_watched == 20
            assert current.rewatch_reward == 0.0, "opening a rewatch must not fabricate a rewatch reward"
            assert [item.model_dump() for item in (await db.exec(
                select(PlaybackMilestone).where(PlaybackMilestone.attempt_id == completed_id).order_by(PlaybackMilestone.id)
            )).all()] == milestones, "historical milestones must stay unchanged"
            assert [item.model_dump() for item in (await db.exec(
                select(TelemetryEvent).where(TelemetryEvent.profile_id == "rewatch-history", TelemetryEvent.movie_id == movie_id).order_by(TelemetryEvent.id)
            )).all()] == rewards, "a low-progress rewatch must not add completion or early-exit signals"


async def check_caller_owned_rewatch_transaction(test_engine) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(Profile(id="rewatch-transaction", name="Rewatch Transaction"))
        db.add(movie("m_203", "Atomic Rewatch History", ["Action"], available=True))
        await db.commit()
    completed_id = await recommendation.record_playback_progress(
        "rewatch-transaction", "m_203", None, 5760, 5760, 0.80, True,
    )
    assert completed_id

    async def history_snapshot():
        async with AsyncSession(test_engine) as db:
            return {
                "attempts": [item.model_dump() for item in (await db.exec(select(ViewingAttempt).where(ViewingAttempt.profile_id == "rewatch-transaction").order_by(ViewingAttempt.id))).all()],
                "milestones": [item.model_dump() for item in (await db.exec(select(PlaybackMilestone).order_by(PlaybackMilestone.id))).all()],
                "signals": [item.model_dump() for item in (await db.exec(select(TelemetryEvent).where(TelemetryEvent.profile_id == "rewatch-transaction").order_by(TelemetryEvent.id))).all()],
                "tastes": [item.model_dump() for item in (await db.exec(select(ProfileTaste).where(ProfileTaste.profile_id == "rewatch-transaction").order_by(ProfileTaste.id))).all()],
            }

    before = await history_snapshot()
    async with AsyncSession(test_engine) as db:
        rolled_back_id = await recommendation.record_playback_progress(
            "rewatch-transaction", "m_203", None, 4320, 50, 0.60, False,
            start_new_attempt=True, session=db,
        )
        await db.flush()
        assert rolled_back_id and rolled_back_id != completed_id
        assert await db.get(ViewingAttempt, rolled_back_id)
        await db.rollback()
    assert await history_snapshot() == before, "caller rollback must discard the new attempt, milestones, signals and taste changes together"

    async with AsyncSession(test_engine) as db:
        committed_id = await recommendation.record_playback_progress(
            "rewatch-transaction", "m_203", None, 4320, 50, 0.60, False,
            start_new_attempt=True, session=db,
        )
        await db.commit()
    after = await history_snapshot()
    assert committed_id and committed_id != rolled_back_id
    assert len(after["attempts"]) == 2, "retry must commit exactly one new attempt"
    assert next(item for item in after["attempts"] if item["id"] == completed_id) == before["attempts"][0], "atomic retry must preserve completed history"
    current = next(item for item in after["attempts"] if item["id"] == committed_id)
    assert current["completed_at"] is None and current["max_completion"] == 0.60
    assert all(item["id"] != rolled_back_id for item in after["attempts"])


async def main() -> None:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    original_engine = recommendation.engine
    original_db_engine = db_module.engine
    original_media_dir = settings.MEDIA_DIR
    original_shadow_mode = settings.RECOMMENDATION_V2_SHADOW
    cache_test_dir = os.path.join("temp", f"recommendation-regression-{os.getpid()}")
    recommendation.engine = test_engine
    db_module.engine = test_engine
    settings.MEDIA_DIR = cache_test_dir
    settings.RECOMMENDATION_V2_SHADOW = True
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
            shadow_metric = await db.get(RecommendationRuntimeMetric, "profile")
            assert shadow_metric and shadow_metric.generated_at > 0, "shadow comparison metrics must survive process restart"

        first_attempt = await recommendation.record_playback_progress("profile", "m_1", None, 4000, 4000, 0.80, False)
        assert first_attempt
        completed_attempt = await recommendation.record_playback_progress("profile", "m_1", None, 4000, 0, 0.80, True)
        assert completed_attempt == first_attempt, "credits exit must complete the same viewing attempt"
        async with AsyncSession(test_engine) as db:
            attempt = await db.get(ViewingAttempt, first_attempt)
            assert attempt.completed_at is not None, "semantic credits completion must be recorded"
            assert attempt.max_completion == 0.80 and attempt.duration_watched == 4000, "completion must preserve actual progress"
            assert not (await db.exec(select(PlaybackMilestone).where(PlaybackMilestone.attempt_id == first_attempt, PlaybackMilestone.milestone == 95))).first(), "credits completion must not fabricate a 95% milestone"
            assert not (await db.exec(select(TelemetryEvent).where(TelemetryEvent.movie_id == "m_1", TelemetryEvent.event_type == "playback_95"))).first(), "credits completion must not fabricate a taste reward"
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
        await check_confirmed_rewatch_history(test_engine)
        await check_caller_owned_rewatch_transaction(test_engine)
        print("Recommendation regression checks passed.")
    finally:
        recommendation.engine = original_engine
        db_module.engine = original_db_engine
        settings.MEDIA_DIR = original_media_dir
        settings.RECOMMENDATION_V2_SHADOW = original_shadow_mode
        await test_engine.dispose()
        shutil.rmtree(os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), cache_test_dir), ignore_errors=True)
        for suffix in ("", "-shm", "-wal"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
