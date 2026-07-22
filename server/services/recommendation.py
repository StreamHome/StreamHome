"""Server-authoritative personalized ranking and recommendation cache orchestration."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db import engine
from config import settings
from models import (
    Episode,
    Movie,
    PlaybackMilestone,
    Profile,
    ProfileRecommendation,
    ProfileMediaPreference,
    ProfileOnboardingPreference,
    RecommendationExposure,
    ProfileTaste,
    ProfileVibeVector,
    RecommendationRefreshState,
    TelemetryEvent,
    TelemetryRequest,
    ViewingAttempt,
    MovieResponse,
)
from services.logger import logger
from services.vibe_analysis import VIBE_ANALYSIS_VERSION, crew_names

TASTE_HALF_LIFE_DAYS = 90.0
DECAY_RATE = math.log(2.0) / TASTE_HALF_LIFE_DAYS
TMDB_POOL_LIMIT = 80
TMDB_REFRESH_SECONDS = 6 * 60 * 60
TMDB_STALE_SECONDS = 24 * 60 * 60
ATTEMPT_GAP_SECONDS = 30 * 60
REWATCH_COOLDOWN_SECONDS = 24 * 60 * 60
ALGORITHM_VERSION = "v2.1"
SHADOW_METRICS: Dict[str, Dict[str, Any]] = {}

EVENT_WEIGHTS = {
    "card_click": 0.35,
    "search_click": 0.75,
    "search_result_select": 0.75,
    "watchlist_add": 3.0,
    "watchlist_remove": -1.5,
    "playback_10": 0.25,
    "playback_25": 0.75,
    "playback_50": 1.5,
    "playback_80": 3.0,
    "playback_95": 2.0,
    "rewatch": 2.5,
    "repeated_early_exit": -0.75,
}
PUBLIC_TELEMETRY_EVENTS = {"card_click", "search_click", "search_result_select"}
MILESTONES = ((10, 0.10), (25, 0.25), (50, 0.50), (80, 0.80), (95, 0.95))


def normalize_tag(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


def decayed_score(taste: ProfileTaste, now: Optional[float] = None) -> float:
    now = now or time.time()
    age_days = max(0.0, now - taste.last_updated) / 86400.0
    return taste.score * math.exp(-DECAY_RATE * age_days)


def infer_tmdb_id(movie: Movie) -> Optional[int]:
    if movie.tmdb_id:
        return movie.tmdb_id
    try:
        if movie.id.startswith("m_"):
            return int(movie.id[2:])
        if movie.id.startswith("tv_"):
            return int(movie.id[3:])
    except (TypeError, ValueError):
        return None
    return None


def is_available(movie: Movie, episodes: Sequence[Episode] = ()) -> bool:
    if movie.type == "series":
        return any(bool((episode.video_url or "").strip()) for episode in episodes)
    return bool((movie.video_url or "").strip())


def source_for(movie: Movie, episodes: Sequence[Episode] = ()) -> Tuple[str, str]:
    if is_available(movie, episodes):
        return "server", "available"
    if movie.availability == "processing":
        return "server", "processing"
    return "tmdb_cache", "cached"


def _movie_tags(movie: Movie) -> List[Tuple[str, str, float]]:
    tags: List[Tuple[str, str, float]] = []
    genres = [normalize_tag(value) for value in movie.genres if normalize_tag(value)]
    cast = [normalize_tag(value) for value in movie.cast[:5] if normalize_tag(value)]
    if genres:
        tags.extend(("genre", value, 0.45 / len(genres)) for value in genres)
    if cast:
        tags.extend(("actor", value, 0.15 / len(cast)) for value in cast)
    directors = crew_names(movie.crew, {"Director"}) or ([movie.director] if movie.director else [])
    clean_directors = [normalize_tag(value) for value in directors if normalize_tag(value) and not normalize_tag(value).startswith("unknown") and normalize_tag(value) not in {"various", "various directors"}]
    if clean_directors:
        tags.extend(("director", value, 0.20 / len(clean_directors)) for value in clean_directors)
    writers = [normalize_tag(value) for value in crew_names(movie.crew, {"Writer", "Screenplay", "Story", "Teleplay", "Creator"}) if normalize_tag(value)]
    if writers:
        tags.extend(("writer", value, 0.14 / len(writers)) for value in writers)
    keywords = [normalize_tag(value) for value in movie.keywords[:8] if normalize_tag(value)]
    if keywords:
        tags.extend(("keyword", value, 0.12 / len(keywords)) for value in keywords)
    tropes = [(normalize_tag(str(value.get("id") or "")), float(value.get("confidence") or 0.0)) for value in movie.trope_vectors if value.get("id")]
    if tropes:
        total_confidence = sum(max(0.05, confidence) for _, confidence in tropes)
        tags.extend(("trope", value, 0.30 * max(0.05, confidence) / total_confidence) for value, confidence in tropes)
    collection = normalize_tag(movie.collection_name or "")
    if collection:
        tags.append(("collection", collection, 0.12))
    return tags


async def _resolve_movie(db: AsyncSession, movie_id: Optional[str], tmdb_id: Optional[int]) -> Optional[Movie]:
    if movie_id:
        return await db.get(Movie, movie_id)
    if tmdb_id:
        result = await db.exec(select(Movie).where(Movie.tmdb_id == tmdb_id))
        return result.first()
    return None


async def _mark_taste_dirty(db: AsyncSession, profile_id: str, now: float) -> None:
    state = await db.get(RecommendationRefreshState, profile_id)
    if not state:
        state = RecommendationRefreshState(profile_id=profile_id)
    state.taste_version += 1
    state.refresh_requested = True
    state.last_ranked_at = now
    db.add(state)


async def _apply_signal(
    db: AsyncSession,
    profile_id: str,
    movie: Movie,
    event_type: str,
    now: float,
    multiplier: float = 1.0,
    dedupe_key: Optional[str] = None,
    episode_id: Optional[str] = None,
) -> bool:
    base_weight = EVENT_WEIGHTS.get(event_type)
    if base_weight is None:
        return False

    if dedupe_key:
        existing = await db.exec(select(TelemetryEvent).where(TelemetryEvent.dedupe_key == dedupe_key))
        if existing.first():
            return False

    event = TelemetryEvent(
        profile_id=profile_id,
        event_type=event_type,
        movie_id=movie.id,
        tmdb_id=infer_tmdb_id(movie),
        timestamp=now,
        dedupe_key=dedupe_key,
    )
    event.event_metadata = {"episodeId": episode_id} if episode_id else {}
    db.add(event)

    for tag_type, tag_value, share in _movie_tags(movie):
        result = await db.exec(
            select(ProfileTaste).where(
                ProfileTaste.profile_id == profile_id,
                ProfileTaste.tag_type == tag_type,
                ProfileTaste.tag_value == tag_value,
            )
        )
        taste = result.first()
        delta = base_weight * multiplier * share
        if taste:
            taste.score = decayed_score(taste, now) + delta
            taste.last_updated = now
        else:
            taste = ProfileTaste(
                profile_id=profile_id,
                tag_type=tag_type,
                tag_value=tag_value,
                score=delta,
                last_updated=now,
            )
        db.add(taste)

    await _mark_taste_dirty(db, profile_id, now)
    return True


async def process_telemetry_event(profile_id: str, request: TelemetryRequest) -> bool:
    """Validate and process low-trust browser interaction telemetry."""
    if request.event_type not in PUBLIC_TELEMETRY_EVENTS:
        return False
    now = time.time()
    async with AsyncSession(engine) as db:
        if not await db.get(Profile, profile_id):
            return False
        movie = await _resolve_movie(db, request.movie_id, request.tmdb_id)
        if not movie:
            return False
        bucket = int(now // 3600)
        key_source = f"{profile_id}:{request.event_type}:{movie.id}:{bucket}"
        dedupe_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
        changed = await _apply_signal(db, profile_id, movie, request.event_type, now, dedupe_key=dedupe_key)
        if changed:
            if request.event_type in {"search_click", "search_result_select"} and movie.catalog_source == "tmdb_cache":
                existing_pool = await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id, ProfileRecommendation.movie_id == movie.id))
                pool_item = existing_pool.first() or ProfileRecommendation(profile_id=profile_id, movie_id=movie.id, media_type=movie.type or "movie", generated_at=now)
                pool_item.candidate_source = "search_selection"
                pool_item.source_confidence = 0.7
                pool_item.score = max(pool_item.score, 0.7)
                pool_item.reasons = ["Selected from search"]
                db.add(pool_item)
            await db.commit()
        return changed


async def set_media_preference(profile_id: str, movie_id: str, preference: Optional[str]) -> Dict[str, Any]:
    if preference not in {None, "like", "love", "dislike"}:
        raise ValueError("Invalid media preference")
    now = time.time()
    async with AsyncSession(engine) as db:
        if not await db.get(Profile, profile_id) or not await db.get(Movie, movie_id):
            raise LookupError("Profile or media not found")
        result = await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id, ProfileMediaPreference.movie_id == movie_id))
        item = result.first()
        if preference is None:
            if item:
                await db.delete(item)
        elif item:
            item.preference = preference
            item.updated_at = now
            db.add(item)
        else:
            db.add(ProfileMediaPreference(profile_id=profile_id, movie_id=movie_id, preference=preference, updated_at=now))
        await _mark_taste_dirty(db, profile_id, now)
        await db.commit()
    return {"movie_id": movie_id, "preference": preference}


async def get_media_preferences(profile_id: str) -> Dict[str, str]:
    async with AsyncSession(engine) as db:
        result = await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id))
        return {item.movie_id: item.preference for item in result.all()}


async def record_recommendation_exposures(profile_id: str, exposures: Sequence[Any]) -> int:
    now = time.time()
    accepted = 0
    async with AsyncSession(engine) as db:
        if not await db.get(Profile, profile_id):
            return 0
        cutoff = now - 90 * 86400
        old = await db.exec(select(RecommendationExposure).where(RecommendationExposure.shown_at < cutoff))
        for item in old.all():
            await db.delete(item)
        for exposure in list(exposures)[:100]:
            if not await db.get(Movie, exposure.movie_id):
                continue
            raw = f"{profile_id}:{exposure.feed_generation}:{exposure.surface}:{exposure.movie_id}"
            dedupe = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            found = await db.exec(select(RecommendationExposure).where(RecommendationExposure.dedupe_key == dedupe))
            if found.first():
                continue
            db.add(RecommendationExposure(id=str(uuid.uuid4()), profile_id=profile_id, movie_id=exposure.movie_id, feed_generation=exposure.feed_generation[:80], surface=exposure.surface[:80], scope=exposure.scope[:20], category=exposure.category[:80], position=max(0, int(exposure.position)), shown_at=now, dedupe_key=dedupe))
            accepted += 1
        await db.commit()
    return accepted


async def set_onboarding_preferences(profile_id: str, genres: Sequence[str], title_ids: Sequence[str]) -> Dict[str, Any]:
    now = time.time()
    clean_genres = list(dict.fromkeys(normalize_tag(value) for value in genres if normalize_tag(value)))[:12]
    clean_titles = list(dict.fromkeys(value for value in title_ids if value))[:12]
    async with AsyncSession(engine) as db:
        if not await db.get(Profile, profile_id):
            raise LookupError("Profile not found")
        existing = await db.exec(select(ProfileOnboardingPreference).where(ProfileOnboardingPreference.profile_id == profile_id))
        for item in existing.all():
            await db.delete(item)
        for genre in clean_genres:
            db.add(ProfileOnboardingPreference(profile_id=profile_id, kind="genre", value=genre, updated_at=now))
        for movie_id in clean_titles:
            if await db.get(Movie, movie_id):
                db.add(ProfileOnboardingPreference(profile_id=profile_id, kind="title", value=movie_id, updated_at=now))
        await _mark_taste_dirty(db, profile_id, now)
        await db.commit()
    return {"genres": clean_genres, "title_ids": clean_titles}


async def get_onboarding_preferences(profile_id: str) -> Dict[str, Any]:
    async with AsyncSession(engine) as db:
        result = await db.exec(select(ProfileOnboardingPreference).where(ProfileOnboardingPreference.profile_id == profile_id))
        items = list(result.all())
        return {"genres": [item.value for item in items if item.kind == "genre"], "title_ids": [item.value for item in items if item.kind == "title"]}


async def get_recommendation_diagnostics(profile_id: str) -> Dict[str, Any]:
    now = time.time()
    async with AsyncSession(engine) as db:
        preferences = await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id))
        preference_rows = list(preferences.all())
        exposures = await db.exec(select(RecommendationExposure).where(RecommendationExposure.profile_id == profile_id, RecommendationExposure.shown_at >= now - 30 * 86400))
        exposure_rows = list(exposures.all())
        events = await db.exec(select(TelemetryEvent).where(TelemetryEvent.profile_id == profile_id, TelemetryEvent.timestamp >= now - 30 * 86400))
        event_rows = list(events.all())
        tastes = await _taste_map(db, profile_id, now)
        pool = await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id))
        pool_rows = list(pool.all())
        movies = await db.exec(select(Movie))
        movie_rows = list(movies.all())
        available = sum(1 for movie in movie_rows if movie.availability == "available" or bool((movie.video_url or "").strip()))
        crew_coverage = sum(1 for movie in movie_rows if movie.crew)
        trope_coverage = sum(1 for movie in movie_rows if movie.trope_vectors)
        pacing_coverage = sum(1 for movie in movie_rows if movie.dialogue_wpm and movie.dialogue_confidence >= 0.15)
        details_opens = sum(1 for event in event_rows if event.event_type in {"card_click", "search_click", "search_result_select"})
        playback_starts = sum(1 for event in event_rows if event.event_type == "playback_10")
        completions = sum(1 for event in event_rows if event.event_type in {"playback_80", "playback_95"})
        top_tastes = sorted(({"kind": kind, "value": value, "score": round(score, 3)} for (kind, value), score in tastes.items()), key=lambda item: -item["score"])[:12]
        return {
            "profile_id": profile_id,
            "period_days": 30,
            "exposures": len(exposure_rows),
            "details_opens": details_opens,
            "playback_starts": playback_starts,
            "completions": completions,
            "play_rate": round(playback_starts / len(exposure_rows), 4) if exposure_rows else 0.0,
            "completion_rate": round(completions / playback_starts, 4) if playback_starts else 0.0,
            "preferences": {name: sum(1 for item in preference_rows if item.preference == name) for name in ("like", "love", "dislike")},
            "candidate_pool": len(pool_rows),
            "candidate_sources": dict((source, sum(1 for item in pool_rows if item.candidate_source == source)) for source in sorted({item.candidate_source for item in pool_rows})),
            "catalog": {"total": len(movie_rows), "available": available, "cached": len(movie_rows) - available},
            "vibe_analysis": {
                "algorithm_version": ALGORITHM_VERSION,
                "analyzer_version": VIBE_ANALYSIS_VERSION,
                "crew_coverage": crew_coverage,
                "trope_coverage": trope_coverage,
                "pacing_coverage": pacing_coverage,
                "enabled": settings.RECOMMENDATION_V2_ENABLED,
                "shadow": settings.RECOMMENDATION_V2_SHADOW,
                "shadow_metrics": SHADOW_METRICS.get(profile_id),
            },
            "top_tastes": top_tastes,
        }


async def reset_media_preferences(profile_id: str) -> int:
    now = time.time()
    async with AsyncSession(engine) as db:
        result = await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id))
        rows = list(result.all())
        for item in rows:
            await db.delete(item)
        await _mark_taste_dirty(db, profile_id, now)
        await db.commit()
        return len(rows)


async def record_authoritative_signal(profile_id: str, movie_id: str, event_type: str) -> bool:
    """Record a signal produced by a successful server-side mutation."""
    now = time.time()
    async with AsyncSession(engine) as db:
        movie = await db.get(Movie, movie_id)
        if not movie or not await db.get(Profile, profile_id):
            return False
        changed = await _apply_signal(db, profile_id, movie, event_type, now)
        if changed:
            await db.commit()
        return changed


def _parse_iso_timestamp(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


async def record_playback_progress(
    profile_id: str,
    movie_id: str,
    episode_id: Optional[str],
    position: int,
    duration_watched: int,
    completion_rate: float,
    is_finished: bool,
) -> Optional[str]:
    """Create idempotent milestones and bounded rewatch/early-exit signals."""
    now = time.time()
    completion_rate = max(0.0, min(1.0, float(completion_rate or 0.0)))
    async with AsyncSession(engine) as db:
        movie = await db.get(Movie, movie_id)
        if not movie or not await db.get(Profile, profile_id):
            return None

        stmt = select(ViewingAttempt).where(
            ViewingAttempt.profile_id == profile_id,
            ViewingAttempt.movie_id == movie_id,
        )
        if episode_id:
            stmt = stmt.where(ViewingAttempt.episode_id == episode_id)
        else:
            stmt = stmt.where(ViewingAttempt.episode_id.is_(None))
        attempts_result = await db.exec(stmt.order_by(ViewingAttempt.last_seen_at.desc()))
        attempts = list(attempts_result.all())
        current = attempts[0] if attempts else None
        starts_new = not current or (now - current.last_seen_at > ATTEMPT_GAP_SECONDS) or (
            position < 60 and current.max_completion >= 0.25
        )

        if starts_new and current and current.max_completion < 0.10 and current.duration_watched >= 60 and not current.early_exit_recorded:
            current.early_exit_recorded = True
            db.add(current)
            earlier_exits = sum(1 for attempt in attempts[1:] if attempt.early_exit_recorded and now - attempt.last_seen_at <= 30 * 86400)
            if earlier_exits >= 1:
                await _apply_signal(
                    db, profile_id, movie, "repeated_early_exit", now,
                    dedupe_key=f"early-exit:{current.id}", episode_id=episode_id,
                )

        if starts_new:
            current = ViewingAttempt(
                id=str(uuid.uuid4()),
                profile_id=profile_id,
                movie_id=movie_id,
                episode_id=episode_id,
                started_at=now,
                last_seen_at=now,
            )
            attempts.insert(0, current)

        previous_max = current.max_completion
        current.max_completion = max(current.max_completion, completion_rate)
        current.duration_watched = max(current.duration_watched, int(duration_watched or 0))
        current.last_seen_at = now
        if is_finished or completion_rate >= 0.95:
            current.completed_at = current.completed_at or now
        db.add(current)

        for milestone, threshold in MILESTONES:
            if previous_max < threshold <= current.max_completion:
                found = await db.exec(
                    select(PlaybackMilestone).where(
                        PlaybackMilestone.attempt_id == current.id,
                        PlaybackMilestone.milestone == milestone,
                    )
                )
                if not found.first():
                    db.add(PlaybackMilestone(attempt_id=current.id, milestone=milestone, recorded_at=now))
                    await _apply_signal(
                        db, profile_id, movie, f"playback_{milestone}", now,
                        dedupe_key=f"milestone:{current.id}:{milestone}", episode_id=episode_id,
                    )

        prior_meaningful = [attempt for attempt in attempts[1:] if attempt.max_completion >= 0.50]
        if previous_max < 0.50 <= current.max_completion and prior_meaningful:
            rewarded = [attempt for attempt in prior_meaningful if attempt.rewatch_reward > 0]
            last_reward_at = max((attempt.last_seen_at for attempt in rewarded), default=0.0)
            series_daily_cap_reached = False
            if movie.type == "series":
                recent_rewatches = await db.exec(
                    select(TelemetryEvent).where(
                        TelemetryEvent.profile_id == profile_id,
                        TelemetryEvent.movie_id == movie_id,
                        TelemetryEvent.event_type == "rewatch",
                        TelemetryEvent.timestamp >= now - REWATCH_COOLDOWN_SECONDS,
                    )
                )
                series_daily_cap_reached = len(list(recent_rewatches.all())) >= 2
            if now - last_reward_at >= REWATCH_COOLDOWN_SECONDS and not series_daily_cap_reached:
                factors = (1.0, 0.60, 0.35)
                factor = factors[len(rewarded)] if len(rewarded) < len(factors) else 0.20
                current.rewatch_reward = factor
                db.add(current)
                await _apply_signal(
                    db, profile_id, movie, "rewatch", now, multiplier=factor,
                    dedupe_key=f"rewatch:{current.id}", episode_id=episode_id,
                )

        attempt_id = current.id
        await db.commit()
        return attempt_id


async def _taste_map(db: AsyncSession, profile_id: str, now: float) -> Dict[Tuple[str, str], float]:
    result = await db.exec(select(ProfileTaste).where(ProfileTaste.profile_id == profile_id))
    values = {(taste.tag_type, taste.tag_value): decayed_score(taste, now) for taste in result.all()}
    onboarding = list((await db.exec(select(ProfileOnboardingPreference).where(ProfileOnboardingPreference.profile_id == profile_id))).all())
    explicit = list((await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id))).all())
    title_ids = {item.value for item in onboarding if item.kind == "title"} | {item.movie_id for item in explicit}
    movie_by_id: Dict[str, Movie] = {}
    if title_ids:
        movie_rows = await db.exec(select(Movie).where(Movie.id.in_(title_ids)))
        movie_by_id = {movie.id: movie for movie in movie_rows.all()}
    for item in onboarding:
        if item.kind == "genre":
            values[("genre", normalize_tag(item.value))] = values.get(("genre", normalize_tag(item.value)), 0.0) + 4.0
        elif item.kind == "title":
            movie = movie_by_id.get(item.value)
            if movie:
                for kind, tag, share in _movie_tags(movie):
                    values[(kind, tag)] = values.get((kind, tag), 0.0) + 3.0 * share
    for item in explicit:
        movie = movie_by_id.get(item.movie_id)
        if not movie:
            continue
        strength = {"like": 3.0, "love": 6.0, "dislike": -1.0}.get(item.preference, 0.0)
        for kind, tag, share in _movie_tags(movie):
            values[(kind, tag)] = values.get((kind, tag), 0.0) + strength * share
    return values


async def _preference_map(db: AsyncSession, profile_id: str) -> Dict[str, str]:
    result = await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id))
    return {item.movie_id: item.preference for item in result.all()}


def _bayesian_quality(movie: Movie) -> float:
    votes = max(0, int(movie.vote_count or 0))
    rating = max(0.0, min(10.0, float(movie.vote_average or 0.0)))
    prior_votes, prior_rating = 250.0, 6.5
    adjusted = (votes / (votes + prior_votes)) * rating + (prior_votes / (votes + prior_votes)) * prior_rating
    return max(0.0, min(1.0, adjusted / 10.0))


def _recency(movie: Movie, current_year: int) -> float:
    age = max(0, current_year - int(movie.release_year or current_year))
    return math.exp(-age / 12.0)


async def _profile_pacing_vector(
    db: AsyncSession,
    profile_id: str,
    movies: Sequence[Movie],
    preferences: Dict[str, str],
    attempts: Sequence[ViewingAttempt],
) -> Optional[Dict[str, float]]:
    movie_by_id = {movie.id: movie for movie in movies}
    weights: Dict[str, float] = defaultdict(float)
    for movie_id, preference in preferences.items():
        if preference == "love":
            weights[movie_id] += 6.0
        elif preference == "like":
            weights[movie_id] += 3.0
    completed_ids: set[str] = set()
    for attempt in attempts:
        if attempt.max_completion < 0.80:
            continue
        completed_ids.add(attempt.movie_id)
        weights[attempt.movie_id] += 1.5 + min(2.0, float(attempt.rewatch_reward or 0.0) * 2.0)
    samples: List[Tuple[float, float]] = []
    for movie_id, weight in weights.items():
        movie = movie_by_id.get(movie_id)
        if not movie or not movie.dialogue_wpm or float(movie.dialogue_confidence or 0.0) < 0.15:
            continue
        samples.append((float(movie.dialogue_wpm), weight * float(movie.dialogue_confidence or 0.0)))
    distinct = len(samples)
    total_weight = sum(weight for _, weight in samples)
    if distinct < 2 or total_weight <= 0:
        vector = await db.get(ProfileVibeVector, profile_id)
        if vector:
            vector.dialogue_wpm_mean = None
            vector.dialogue_wpm_stddev = None
            vector.dialogue_confidence = 0.0
            vector.dialogue_sample_weight = total_weight
            vector.algorithm_version = ALGORITHM_VERSION
            vector.updated_at = time.time()
            db.add(vector)
        return None
    mean = sum(value * weight for value, weight in samples) / total_weight
    variance = sum(weight * (value - mean) ** 2 for value, weight in samples) / total_weight
    stddev = max(12.0, math.sqrt(max(0.0, variance)))
    confidence = min(1.0, distinct / 5.0) * min(1.0, total_weight / 12.0)
    vector = await db.get(ProfileVibeVector, profile_id) or ProfileVibeVector(profile_id=profile_id)
    vector.dialogue_wpm_mean = mean
    vector.dialogue_wpm_stddev = stddev
    vector.dialogue_confidence = confidence
    vector.dialogue_sample_weight = total_weight
    vector.algorithm_version = ALGORITHM_VERSION
    vector.updated_at = time.time()
    db.add(vector)
    return {"mean": mean, "stddev": stddev, "confidence": confidence}


def _possessive(name: str) -> str:
    return f"{name}'" if name.casefold().endswith("s") else f"{name}'s"


def _reason_detail(text: str) -> Dict[str, Any]:
    if text.startswith("Because you love ") and text.endswith(" directing style."):
        subject = text[len("Because you love "):].removesuffix(" directing style.").removesuffix("'s").removesuffix("'")
        return {"code": "auteur_director", "subject": subject, "fallbackText": text}
    if text.startswith("Because you love ") and text.endswith(" writing."):
        subject = text[len("Because you love "):].removesuffix(" writing.").removesuffix("'s").removesuffix("'")
        return {"code": "auteur_writer", "subject": subject, "fallbackText": text}
    if "dialogue" in text.casefold() or "slow-burn" in text.casefold():
        return {"code": "pacing_match", "fallbackText": text}
    if text.startswith("A ") and text.endswith(" matching your taste."):
        return {"code": "trope_match", "subject": text[2:].removesuffix(" matching your taste."), "fallbackText": text}
    if text == "You love this title":
        return {"code": "explicit_love", "fallbackText": text}
    if text == "You liked this title":
        return {"code": "explicit_like", "fallbackText": text}
    if text == "Available on your server":
        return {"code": "server_available", "fallbackText": text}
    return {"code": "taste_affinity", "fallbackText": text}


def reason_details(reasons: Sequence[str]) -> List[Dict[str, Any]]:
    return [_reason_detail(reason) for reason in reasons[:2]]


def score_movie(
    movie: Movie,
    tastes: Dict[Tuple[str, str], float],
    available: bool,
    completion: float = 0.0,
    now: Optional[float] = None,
    pacing_profile: Optional[Dict[str, float]] = None,
) -> Tuple[float, List[str]]:
    now = now or time.time()
    tag_values = [(kind, value, share, tastes.get((kind, value), 0.0)) for kind, value, share in _movie_tags(movie)]
    affinity_raw = sum(share * value for _, _, share, value in tag_values)
    affinity = (math.tanh(affinity_raw / 4.0) + 1.0) / 2.0
    quality = _bayesian_quality(movie)
    popularity = math.tanh(max(0.0, float(movie.popularity or 0.0)) / 100.0)
    quality_popularity = 0.75 * quality + 0.25 * popularity
    recency = _recency(movie, datetime.now().year)
    novelty = max(0.0, 1.0 - max(0.0, min(1.0, completion)))

    if tastes:
        total = 0.48 * affinity + 0.15 * quality_popularity + 0.10 * recency + 0.10 * novelty + 0.05 * float(available)
    else:
        total = 0.45 * quality + 0.30 * popularity + 0.15 * recency + 0.10 * float(available)

    reasons: List[str] = []
    directors = crew_names(movie.crew, {"Director"}) or ([movie.director] if movie.director else [])
    writers = crew_names(movie.crew, {"Writer", "Screenplay", "Story", "Teleplay", "Creator"})
    director_match = max(((tastes.get(("director", normalize_tag(name)), 0.0), name) for name in directors if normalize_tag(name)), default=(0.0, ""))
    writer_match = max(((tastes.get(("writer", normalize_tag(name)), 0.0), name) for name in writers if normalize_tag(name)), default=(0.0, ""))
    auteur_score, auteur_name, auteur_kind = max((director_match[0], director_match[1], "director"), (writer_match[0], writer_match[1], "writer"))
    auteur_strength = min(1.0, max(0.0, auteur_score) / 1.2)
    if auteur_strength > 0:
        total *= 1.0 + 0.55 * auteur_strength
        if auteur_strength >= 0.25 and auteur_name:
            reason = f"Because you love {_possessive(auteur_name)} directing style." if auteur_kind == "director" else f"Because you love {_possessive(auteur_name)} writing."
            reasons.append(reason)

    pacing_match = 0.0
    if pacing_profile and movie.dialogue_wpm and float(movie.dialogue_confidence or 0.0) >= 0.15:
        deviation = abs(float(movie.dialogue_wpm) - pacing_profile["mean"])
        pacing_match = math.exp(-(deviation ** 2) / (2.0 * pacing_profile["stddev"] ** 2))
        pacing_strength = pacing_match * pacing_profile["confidence"] * float(movie.dialogue_confidence or 0.0)
        total += 0.14 * pacing_strength
        if pacing_strength >= 0.25 and not reasons:
            if pacing_profile["mean"] >= 105:
                reasons.append("Based on your preference for fast-paced, witty dialogue.")
            elif pacing_profile["mean"] <= 65:
                reasons.append("A measured, atmospheric slow-burn matching your pace.")
            else:
                reasons.append("A dialogue-driven story matching your preferred pace.")

    trope_matches = []
    for trope in movie.trope_vectors:
        trope_id = normalize_tag(str(trope.get("id") or ""))
        taste = max(0.0, tastes.get(("trope", trope_id), 0.0))
        if trope_id and taste:
            trope_matches.append((taste * float(trope.get("confidence") or 0.0), trope))
    trope_overlap = sum(value for value, _ in trope_matches)
    trope_strength = 1.0 - math.exp(-0.9 * trope_overlap) if trope_overlap > 0 else 0.0
    if trope_strength:
        total += 0.20 * trope_strength
        total *= 1.0 + 0.20 * trope_strength
        if trope_strength >= 0.20 and not reasons:
            best_trope = max(trope_matches, key=lambda row: row[0])[1]
            reasons.append(f"A {str(best_trope.get('label') or 'specific vibe').casefold()} matching your taste.")

    positives = sorted((entry for entry in tag_values if entry[3] > 0.05), key=lambda entry: entry[3], reverse=True)
    if positives and not reasons:
        _, tag, _, _ = positives[0]
        label = next((value for value in movie.genres + movie.cast if normalize_tag(value) == tag), movie.director or tag)
        reasons.append(f"Because you like {label}")
    if available:
        reasons.append("Available on your server")
    elif not reasons:
        reasons.append("A highly rated discovery")
    return total, reasons[:2]


def _legacy_score_movie(movie: Movie, tastes: Dict[Tuple[str, str], float], available: bool, completion: float = 0.0) -> Tuple[float, List[str]]:
    tags: List[Tuple[str, str, float]] = []
    genres = [normalize_tag(value) for value in movie.genres if normalize_tag(value)]
    cast = [normalize_tag(value) for value in movie.cast[:5] if normalize_tag(value)]
    if genres:
        tags.extend(("genre", value, 0.60 / len(genres)) for value in genres)
    if cast:
        tags.extend(("actor", value, 0.25 / len(cast)) for value in cast)
    director = normalize_tag(movie.director or "")
    if director and not director.startswith("unknown") and director not in {"various", "various directors"}:
        tags.append(("director", director, 0.15))
    values = [(kind, tag, share, tastes.get((kind, tag), 0.0)) for kind, tag, share in tags]
    affinity_raw = sum(share * value for _, _, share, value in values)
    affinity = (math.tanh(affinity_raw / 4.0) + 1.0) / 2.0
    quality = _bayesian_quality(movie)
    popularity = math.tanh(max(0.0, float(movie.popularity or 0.0)) / 100.0)
    recency = _recency(movie, datetime.now().year)
    novelty = max(0.0, 1.0 - max(0.0, min(1.0, completion)))
    total = 0.60 * affinity + 0.15 * (0.75 * quality + 0.25 * popularity) + 0.10 * recency + 0.10 * novelty + 0.05 * float(available) if tastes else 0.45 * quality + 0.30 * popularity + 0.15 * recency + 0.10 * float(available)
    positives = sorted((entry for entry in values if entry[3] > 0.05), key=lambda entry: entry[3], reverse=True)
    reasons = [f"Because you like {positives[0][1]}"] if positives else (["Available on your server"] if available else ["A highly rated discovery"])
    return total, reasons


async def rank_movies_for_profile(
    db: AsyncSession,
    profile_id: str,
    movies: Sequence[Movie],
    episode_map: Optional[Dict[str, Sequence[Episode]]] = None,
) -> List[Tuple[float, Movie, List[str], str, str]]:
    now = time.time()
    tastes = await _taste_map(db, profile_id, now)
    preferences = await _preference_map(db, profile_id)
    playback = await db.exec(select(ViewingAttempt).where(ViewingAttempt.profile_id == profile_id))
    playback_rows = list(playback.all())
    pacing_profile = await _profile_pacing_vector(db, profile_id, movies, preferences, playback_rows)
    completion_by_movie: Dict[str, float] = defaultdict(float)
    for attempt in playback_rows:
        completion_by_movie[attempt.movie_id] = max(completion_by_movie[attempt.movie_id], attempt.max_completion)
    exposure_result = await db.exec(select(RecommendationExposure).where(RecommendationExposure.profile_id == profile_id, RecommendationExposure.shown_at >= now - 14 * 86400))
    exposure_counts: Dict[str, int] = defaultdict(int)
    for exposure in exposure_result.all():
        exposure_counts[exposure.movie_id] += 1
    signal_result = await db.exec(select(TelemetryEvent).where(TelemetryEvent.profile_id == profile_id, TelemetryEvent.timestamp >= now - 30 * 86400))
    title_signals: Dict[str, float] = defaultdict(float)
    for event in signal_result.all():
        if not event.movie_id:
            continue
        if event.event_type in {"search_click", "search_result_select"}:
            title_signals[event.movie_id] = max(title_signals[event.movie_id], 0.10)
        elif event.event_type == "card_click":
            title_signals[event.movie_id] = max(title_signals[event.movie_id], 0.025)
    ranked = []
    v2_ranked: List[Tuple[float, str]] = []
    legacy_ranked: List[Tuple[float, str]] = []
    for movie in movies:
        episodes = (episode_map or {}).get(movie.id, ())
        source, availability = source_for(movie, episodes)
        v2_score, v2_reasons = score_movie(movie, tastes, availability == "available", completion_by_movie[movie.id], now, pacing_profile)
        legacy_score, legacy_reasons = _legacy_score_movie(movie, tastes, availability == "available", completion_by_movie[movie.id])
        preference = preferences.get(movie.id)
        if preference == "love":
            v2_score += 0.45
            legacy_score += 0.45
            v2_reasons.insert(0, "You love this title")
            legacy_reasons.insert(0, "You love this title")
        elif preference == "like":
            v2_score += 0.25
            legacy_score += 0.25
            v2_reasons.insert(0, "You liked this title")
            legacy_reasons.insert(0, "You liked this title")
        elif preference == "dislike":
            v2_score -= 10.0
            legacy_score -= 10.0
        v2_score += title_signals[movie.id]
        legacy_score += title_signals[movie.id]
        v2_score -= min(0.18, max(0, exposure_counts[movie.id] - 2) * 0.025)
        legacy_score -= min(0.18, max(0, exposure_counts[movie.id] - 2) * 0.025)
        if availability == "available":
            v2_score += 0.10
            legacy_score += 0.10
        score, reasons = (v2_score, v2_reasons) if settings.RECOMMENDATION_V2_ENABLED else (legacy_score, legacy_reasons)
        ranked.append((score, movie, reasons, source, availability))
        v2_ranked.append((v2_score, movie.id))
        legacy_ranked.append((legacy_score, movie.id))
    ranked.sort(key=lambda row: (-row[0], -int(row[1].release_year or 0), row[1].title.casefold(), row[1].id))
    if settings.RECOMMENDATION_V2_SHADOW:
        v2_ranked.sort(key=lambda row: (-row[0], row[1]))
        legacy_ranked.sort(key=lambda row: (-row[0], row[1]))
        visible_ids = [row[1] for row in v2_ranked[:20]]
        legacy_ids = [row[1] for row in legacy_ranked[:20]]
        overlap = len(set(visible_ids) & set(legacy_ids))
        legacy_positions = {movie_id: index for index, movie_id in enumerate(legacy_ids)}
        displacement = [abs(index - legacy_positions[movie_id]) for index, movie_id in enumerate(visible_ids) if movie_id in legacy_positions]
        SHADOW_METRICS[profile_id] = {"top20Overlap": overlap, "meanDisplacement": round(sum(displacement) / len(displacement), 3) if displacement else 0.0, "generatedAt": now}
    return ranked


async def calculate_movie_recommendation_score(movie: Movie, profile_id: str) -> float:
    """Compatibility helper; catalog endpoints should use rank_movies_for_profile in bulk."""
    async with AsyncSession(engine) as db:
        tastes = await _taste_map(db, profile_id, time.time())
        return score_movie(movie, tastes, movie.availability == "available")[0]


async def get_profile_preferences(profile_id: str) -> Dict[str, List[str]]:
    now = time.time()
    prefs: Dict[str, List[str]] = {"genre": [], "actor": [], "director": [], "writer": [], "trope": []}
    async with AsyncSession(engine) as db:
        values = await _taste_map(db, profile_id, now)
    ordered = sorted(((kind, tag, score) for (kind, tag), score in values.items() if score > 0.05), key=lambda row: row[2], reverse=True)
    for kind, tag, _ in ordered:
        if kind in prefs and len(prefs[kind]) < 5:
            prefs[kind].append(tag)
    return prefs


def diversify_ranked(rows: Sequence[Tuple[float, Movie, List[str], str, str]]) -> List[Tuple[float, Movie, List[str], str, str]]:
    remaining = list(rows)
    output = []
    genre_counts: Dict[str, int] = defaultdict(int)
    collection_counts: Dict[str, int] = defaultdict(int)
    actor_counts: Dict[str, int] = defaultdict(int)
    auteur_counts: Dict[str, int] = defaultdict(int)
    trope_counts: Dict[str, int] = defaultdict(int)
    while remaining:
        window = remaining[:12]
        chosen = max(
            window,
            key=lambda row: row[0]
            - 0.055 * genre_counts[normalize_tag((row[1].genres or ["uncategorized"])[0])]
            - 0.07 * collection_counts[normalize_tag(row[1].collection_name or "")]
            - 0.025 * actor_counts[normalize_tag((row[1].cast or [""])[0])]
            - 0.035 * auteur_counts[normalize_tag((crew_names(row[1].crew, {"Director", "Writer", "Screenplay", "Creator"}) or [row[1].director or ""])[0])]
            - 0.04 * trope_counts[normalize_tag(str((row[1].trope_vectors or [{"id": ""}])[0].get("id") or ""))],
        )
        remaining.remove(chosen)
        output.append(chosen)
        genre_counts[normalize_tag((chosen[1].genres or ["uncategorized"])[0])] += 1
        collection_counts[normalize_tag(chosen[1].collection_name or "")] += 1
        actor_counts[normalize_tag((chosen[1].cast or [""])[0])] += 1
        auteur_counts[normalize_tag((crew_names(chosen[1].crew, {"Director", "Writer", "Screenplay", "Creator"}) or [chosen[1].director or ""])[0])] += 1
        trope_counts[normalize_tag(str((chosen[1].trope_vectors or [{"id": ""}])[0].get("id") or ""))] += 1
    return output


async def persist_profile_pool(db: AsyncSession, profile_id: str) -> None:
    movies_result = await db.exec(select(Movie).where(Movie.catalog_source == "tmdb_cache"))
    candidates = list(movies_result.all())
    ranked = await rank_movies_for_profile(db, profile_id, candidates)
    preferences = await _preference_map(db, profile_id)
    ranked = [row for row in ranked if preferences.get(row[1].id) != "dislike"]
    selected: List[Tuple[float, Movie, List[str], str, str]] = []
    for media_type in ("movie", "series"):
        typed = [row for row in ranked if row[1].type == media_type]
        exploit_count = int(TMDB_POOL_LIMIT * 0.80)
        exploit = typed[:exploit_count]
        exploit_ids = {row[1].id for row in exploit}
        exploration = sorted(
            (row for row in typed[exploit_count:] if row[1].id not in exploit_ids),
            key=lambda row: (-_bayesian_quality(row[1]), -float(row[1].popularity or 0.0), row[1].id),
        )[:TMDB_POOL_LIMIT - exploit_count]
        selected.extend(exploit + exploration)
    existing_result = await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id))
    existing = {item.movie_id: item for item in existing_result.all()}
    keep = {row[1].id for row in selected}
    for movie_id, item in existing.items():
        if movie_id not in keep:
            await db.delete(item)
    now = time.time()
    for score, movie, reasons, _, _ in selected:
        item = existing.get(movie.id) or ProfileRecommendation(profile_id=profile_id, movie_id=movie.id, media_type=movie.type or "movie", generated_at=now)
        item.score = score
        item.reasons = reasons
        item.reason_details = reason_details(reasons)
        item.generated_at = now
        preference = preferences.get(movie.id)
        item.candidate_source = "explicit_love" if preference == "love" else "explicit_like" if preference == "like" else "taste_affinity"
        item.source_confidence = 1.0 if preference == "love" else 0.9 if preference == "like" else 0.65
        db.add(item)
    state = await db.get(RecommendationRefreshState, profile_id) or RecommendationRefreshState(profile_id=profile_id)
    state.last_ranked_at = now
    state.refresh_requested = False
    db.add(state)


async def build_recommendation_payload(
    db: AsyncSession,
    profile_id: str,
    scope: str,
    category: str,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    now = time.time()
    movie_result = await db.exec(select(Movie))
    all_movies = list(movie_result.all())
    episode_result = await db.exec(select(Episode).order_by(Episode.season_number, Episode.episode_number))
    episode_map: Dict[str, List[Episode]] = defaultdict(list)
    for episode in episode_result.all():
        episode_map[episode.movie_id].append(episode)

    pool_result = await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id))
    assigned_cache_ids = {item.movie_id for item in pool_result.all()}
    if assigned_cache_ids:
        all_movies = [movie for movie in all_movies if movie.catalog_source != "tmdb_cache" or movie.id in assigned_cache_ids]
    if scope == "movies":
        all_movies = [movie for movie in all_movies if movie.type == "movie"]
    elif scope == "series":
        all_movies = [movie for movie in all_movies if movie.type == "series"]

    ranked = await rank_movies_for_profile(db, profile_id, all_movies, episode_map)
    preferences = await _preference_map(db, profile_id)
    eligible_ranked = [row for row in ranked if preferences.get(row[1].id) != "dislike"]
    tastes = await _taste_map(db, profile_id, now)
    genre_counts: Dict[str, Dict[str, Any]] = {}
    for _, movie, _, source, availability in eligible_ranked:
        for genre in movie.genres:
            key = normalize_tag(genre)
            record = genre_counts.setdefault(key, {"value": genre, "label": genre, "affinity": tastes.get(("genre", key), 0.0), "server_count": 0, "cached_count": 0})
            if availability == "available":
                record["server_count"] += 1
            else:
                record["cached_count"] += 1
    real_categories = sorted(genre_counts.values(), key=lambda item: (-item["affinity"], item["label"].casefold()))
    categories = [
        {"value": "recommended", "label": "Recommended", "affinity": 0.0, "server_count": sum(1 for row in eligible_ranked if row[4] == "available"), "cached_count": sum(1 for row in eligible_ranked if row[4] != "available")},
        {"value": "all", "label": "All Releases", "affinity": 0.0, "server_count": sum(1 for row in ranked if row[4] == "available"), "cached_count": 0},
        *real_categories,
    ]

    requested = normalize_tag(category or "recommended")
    if requested == "all":
        filtered = [row for row in ranked if row[4] == "available"]
    elif requested == "recommended":
        diversified = diversify_ranked(eligible_ranked)
        available_rows = [row for row in diversified if row[4] == "available"]
        cached_rows = [row for row in diversified if row[4] != "available"]
        if len(available_rows) >= 7:
            filtered = []
            while available_rows or cached_rows:
                filtered.extend(available_rows[:7]); del available_rows[:7]
                filtered.extend(cached_rows[:3]); del cached_rows[:3]
        else:
            filtered = diversified
    else:
        filtered = [row for row in eligible_ranked if any(normalize_tag(genre) == requested for genre in row[1].genres)]

    pool_result = await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id))
    pool_by_id = {item.movie_id: item for item in pool_result.all()}

    def serialize(row, include_reasons: bool = True):
        score, movie, reasons, source, availability = row
        pool = pool_by_id.get(movie.id)
        visible_reasons = reasons if include_reasons else []
        return {
            "media": MovieResponse.from_db(movie, episode_map.get(movie.id)),
            "source": source,
            "availability": availability,
            "score": round(score, 6),
            "reasons": visible_reasons,
            "reason_details": reason_details(visible_reasons),
            "viewer_preference": preferences.get(movie.id),
            "candidate_source": pool.candidate_source if pool else "server_catalog" if availability == "available" else "ranked_cache",
            "source_confidence": pool.source_confidence if pool else 0.7 if availability == "available" else 0.5,
        }

    vibe_rails: List[Dict[str, Any]] = []
    if requested == "recommended" and settings.RECOMMENDATION_V2_ENABLED:
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in eligible_ranked:
            movie = row[1]
            for trope in movie.trope_vectors:
                trope_id = normalize_tag(str(trope.get("id") or ""))
                affinity = max(0.0, tastes.get(("trope", trope_id), 0.0))
                if not trope_id or affinity <= 0.05 or float(trope.get("confidence") or 0.0) < 0.60:
                    continue
                group = grouped.setdefault(trope_id, {
                    "id": f"vibe-{trope_id.replace(' ', '-')}",
                    "label": trope.get("railLabel") or trope.get("label") or "Matched to Your Vibe",
                    "trope_ids": [trope_id],
                    "reason_code": "trope_match",
                    "affinity": affinity,
                    "rows": [],
                })
                group["affinity"] = max(group["affinity"], affinity)
                group["rows"].append(row)
        used_ids: set[str] = set()
        for group in sorted(grouped.values(), key=lambda item: (-item["affinity"], item["label"].casefold())):
            rows = [row for row in group["rows"] if row[1].id not in used_ids][:8]
            if len(rows) < 3:
                continue
            used_ids.update(row[1].id for row in rows)
            vibe_rails.append({
                "id": group["id"],
                "label": group["label"],
                "trope_ids": group["trope_ids"],
                "reason_code": group["reason_code"],
                "items": [serialize(row) for row in rows],
            })
            if len(vibe_rails) >= 3:
                break

    attempt_result = await db.exec(
        select(ViewingAttempt).where(
            ViewingAttempt.profile_id == profile_id,
            ViewingAttempt.max_completion >= 0.50,
        ).order_by(ViewingAttempt.last_seen_at.desc())
    )
    meaningful: Dict[str, List[ViewingAttempt]] = defaultdict(list)
    for attempt in attempt_result.all():
        meaningful[attempt.movie_id].append(attempt)
    ranked_by_id = {row[1].id: row for row in ranked}
    watch_again_rows = [ranked_by_id[movie_id] for movie_id, attempts in meaningful.items() if len(attempts) >= 2 and movie_id in ranked_by_id and ranked_by_id[movie_id][4] == "available"]
    watch_again_rows.sort(key=lambda row: max(attempt.last_seen_at for attempt in meaningful[row[1].id]), reverse=True)

    state = await db.get(RecommendationRefreshState, profile_id)
    generated_at = state.last_ranked_at if state and state.last_ranked_at else now
    stale = not state or not state.last_tmdb_refresh_at or now - state.last_tmdb_refresh_at >= TMDB_STALE_SECONDS
    payload = {
        "profile_id": profile_id,
        "scope": scope,
        "category": category or "recommended",
        "generated_at": generated_at,
        "stale": stale,
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "categories": categories,
        "items": [serialize(row) for row in filtered[offset:offset + limit]],
        "watch_again": [serialize(row, include_reasons=False) for row in watch_again_rows[:20]],
        "vibe_rails": vibe_rails,
        "algorithm_version": ALGORITHM_VERSION if settings.RECOMMENDATION_V2_ENABLED else "v1",
    }
    await db.commit()
    return payload


async def refresh_profile_cache(profile_id: str, force: bool = False) -> bool:
    """Refresh a profile's shared in-place TMDB metadata/artwork candidate pool."""
    now = time.time()
    async with AsyncSession(engine) as db:
        state = await db.get(RecommendationRefreshState, profile_id) or RecommendationRefreshState(profile_id=profile_id)
        pool_result = await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id))
        pool = list(pool_result.all())
        movie_count = sum(1 for item in pool if item.media_type == "movie")
        series_count = sum(1 for item in pool if item.media_type == "series")
        shortage = movie_count < TMDB_POOL_LIMIT or series_count < TMDB_POOL_LIMIT
        if not force and not shortage and state.next_tmdb_refresh_at and now < state.next_tmdb_refresh_at:
            if state.refresh_requested:
                await persist_profile_pool(db, profile_id)
                await db.commit()
            return False

    try:
        from services.tmdb import tmdb_client
        await tmdb_client.discover_media("trending", "movie", profile_id, cache_limit=TMDB_POOL_LIMIT)
        await tmdb_client.discover_media("trending", "tv", profile_id, cache_limit=TMDB_POOL_LIMIT)
        # A small unfiltered pool provides exploration and cold-start diversity.
        await tmdb_client.discover_media("trending", "movie", None, cache_limit=10)
        await tmdb_client.discover_media("trending", "tv", None, cache_limit=10)
        async with AsyncSession(engine) as seed_db:
            explicit_result = await seed_db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id, ProfileMediaPreference.preference.in_(["like", "love"])).order_by(ProfileMediaPreference.updated_at.desc()))
            attempt_result = await seed_db.exec(select(ViewingAttempt).where(ViewingAttempt.profile_id == profile_id, ViewingAttempt.max_completion >= 0.50).order_by(ViewingAttempt.last_seen_at.desc()))
            seed_ids = list(dict.fromkeys([item.movie_id for item in explicit_result.all()] + [item.movie_id for item in attempt_result.all()]))[:3]
            seeds = [movie for movie in [await seed_db.get(Movie, movie_id) for movie_id in seed_ids] if movie and infer_tmdb_id(movie)]
        related_ids: List[str] = []
        for seed in seeds:
            related = await tmdb_client.discover_related_media(infer_tmdb_id(seed), seed.type or "movie", limit=12)
            related_ids.extend(item["id"] for item in related)
        async with AsyncSession(engine) as db:
            await persist_profile_pool(db, profile_id)
            preferences = await _preference_map(db, profile_id)
            for movie_id in related_ids:
                result = await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id, ProfileRecommendation.movie_id == movie_id))
                item = result.first()
                movie = await db.get(Movie, movie_id)
                if not movie or preferences.get(movie_id) == "dislike":
                    continue
                if not item:
                    item = ProfileRecommendation(profile_id=profile_id, movie_id=movie.id, media_type=movie.type or "movie", score=0.7, reasons_str='["Similar to a title you enjoyed"]', generated_at=now)
                item.candidate_source = "similar_to_recent"
                item.source_confidence = max(item.source_confidence, 0.75)
                db.add(item)
            state = await db.get(RecommendationRefreshState, profile_id) or RecommendationRefreshState(profile_id=profile_id)
            state.last_tmdb_refresh_at = now
            state.next_tmdb_refresh_at = now + TMDB_REFRESH_SECONDS
            state.last_error = None
            db.add(state)
            await db.commit()
        return True
    except Exception as exc:
        logger.error(f"[Recommendation] TMDB refresh failed for profile {profile_id}: {exc}")
        async with AsyncSession(engine) as db:
            state = await db.get(RecommendationRefreshState, profile_id) or RecommendationRefreshState(profile_id=profile_id)
            state.last_error = str(exc)[:500]
            state.next_tmdb_refresh_at = now + 15 * 60
            db.add(state)
            await db.commit()
        return False


async def recommendation_worker(stop_event) -> None:
    """Coalesced hourly worker; callers own lifecycle and cancellation."""
    while not stop_event.is_set():
        try:
            from services.vibe_analysis import backfill_catalog_vibes
            await backfill_catalog_vibes()
            async with AsyncSession(engine) as db:
                profile_result = await db.exec(select(Profile))
                profile_ids = [profile.id for profile in profile_result.all()]
                state_result = await db.exec(select(RecommendationRefreshState))
                states = {state.profile_id: state for state in state_result.all()}
            now = time.time()
            for profile_id in profile_ids:
                state = states.get(profile_id)
                stale = not state or not state.last_tmdb_refresh_at or now - state.last_tmdb_refresh_at >= TMDB_STALE_SECONDS
                if stale or (state and state.refresh_requested):
                    await refresh_profile_cache(profile_id)
        except Exception as exc:
            logger.error(f"[Recommendation] Background worker error: {exc}")
        try:
            await __import__("asyncio").wait_for(stop_event.wait(), timeout=3600)
        except __import__("asyncio").TimeoutError:
            pass
