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
from models import (
    Episode,
    Movie,
    PlaybackMilestone,
    Profile,
    ProfileRecommendation,
    ProfileTaste,
    RecommendationRefreshState,
    TelemetryEvent,
    TelemetryRequest,
    ViewingAttempt,
    MovieResponse,
)
from services.logger import logger

TASTE_HALF_LIFE_DAYS = 90.0
DECAY_RATE = math.log(2.0) / TASTE_HALF_LIFE_DAYS
TMDB_POOL_LIMIT = 40
TMDB_REFRESH_SECONDS = 6 * 60 * 60
TMDB_STALE_SECONDS = 24 * 60 * 60
ATTEMPT_GAP_SECONDS = 30 * 60
REWATCH_COOLDOWN_SECONDS = 24 * 60 * 60

EVENT_WEIGHTS = {
    "card_click": 0.35,
    "search_click": 1.0,
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
PUBLIC_TELEMETRY_EVENTS = {"card_click", "search_click"}
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
        tags.extend(("genre", value, 0.60 / len(genres)) for value in genres)
    if cast:
        tags.extend(("actor", value, 0.25 / len(cast)) for value in cast)
    director = normalize_tag(movie.director or "")
    if director and not director.startswith("unknown") and director not in {"various", "various directors"}:
        tags.append(("director", director, 0.15))
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
            await db.commit()
        return changed


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
    return {(taste.tag_type, taste.tag_value): decayed_score(taste, now) for taste in result.all()}


def _bayesian_quality(movie: Movie) -> float:
    votes = max(0, int(movie.vote_count or 0))
    rating = max(0.0, min(10.0, float(movie.vote_average or 0.0)))
    prior_votes, prior_rating = 250.0, 6.5
    adjusted = (votes / (votes + prior_votes)) * rating + (prior_votes / (votes + prior_votes)) * prior_rating
    return max(0.0, min(1.0, adjusted / 10.0))


def _recency(movie: Movie, current_year: int) -> float:
    age = max(0, current_year - int(movie.release_year or current_year))
    return math.exp(-age / 12.0)


def score_movie(
    movie: Movie,
    tastes: Dict[Tuple[str, str], float],
    available: bool,
    completion: float = 0.0,
    now: Optional[float] = None,
) -> Tuple[float, List[str]]:
    now = now or time.time()
    tag_values = [(kind, value, share, tastes.get((kind, value), 0.0)) for kind, value, share in _movie_tags(movie)]
    affinity_raw = sum(share * value for kind, tag, share, value in tag_values)
    affinity = (math.tanh(affinity_raw / 4.0) + 1.0) / 2.0
    quality = _bayesian_quality(movie)
    popularity = math.tanh(max(0.0, float(movie.popularity or 0.0)) / 100.0)
    quality_popularity = 0.75 * quality + 0.25 * popularity
    recency = _recency(movie, datetime.now().year)
    novelty = max(0.0, 1.0 - max(0.0, min(1.0, completion)))

    if tastes:
        total = 0.60 * affinity + 0.15 * quality_popularity + 0.10 * recency + 0.10 * novelty + 0.05 * float(available)
    else:
        total = 0.45 * quality + 0.30 * popularity + 0.15 * recency + 0.10 * float(available)

    reasons: List[str] = []
    positives = sorted((entry for entry in tag_values if entry[3] > 0.05), key=lambda entry: entry[3], reverse=True)
    if positives:
        kind, tag, _, _ = positives[0]
        label = next((value for value in movie.genres + movie.cast if normalize_tag(value) == tag), movie.director or tag)
        reasons.append(f"Because you like {label}")
    if available:
        reasons.append("Available on your server")
    elif not reasons:
        reasons.append("A highly rated discovery")
    return total, reasons[:2]


async def rank_movies_for_profile(
    db: AsyncSession,
    profile_id: str,
    movies: Sequence[Movie],
    episode_map: Optional[Dict[str, Sequence[Episode]]] = None,
) -> List[Tuple[float, Movie, List[str], str, str]]:
    now = time.time()
    tastes = await _taste_map(db, profile_id, now)
    playback = await db.exec(select(ViewingAttempt).where(ViewingAttempt.profile_id == profile_id))
    completion_by_movie: Dict[str, float] = defaultdict(float)
    for attempt in playback.all():
        completion_by_movie[attempt.movie_id] = max(completion_by_movie[attempt.movie_id], attempt.max_completion)
    ranked = []
    for movie in movies:
        episodes = (episode_map or {}).get(movie.id, ())
        source, availability = source_for(movie, episodes)
        score, reasons = score_movie(movie, tastes, availability == "available", completion_by_movie[movie.id], now)
        ranked.append((score, movie, reasons, source, availability))
    ranked.sort(key=lambda row: (-row[0], -int(row[1].release_year or 0), row[1].title.casefold(), row[1].id))
    return ranked


async def calculate_movie_recommendation_score(movie: Movie, profile_id: str) -> float:
    """Compatibility helper; catalog endpoints should use rank_movies_for_profile in bulk."""
    async with AsyncSession(engine) as db:
        tastes = await _taste_map(db, profile_id, time.time())
        return score_movie(movie, tastes, movie.availability == "available")[0]


async def get_profile_preferences(profile_id: str) -> Dict[str, List[str]]:
    now = time.time()
    prefs: Dict[str, List[str]] = {"genre": [], "actor": [], "director": []}
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
    while remaining:
        window = remaining[:12]
        chosen = max(
            window,
            key=lambda row: row[0] - 0.045 * genre_counts[normalize_tag((row[1].genres or ["uncategorized"])[0])],
        )
        remaining.remove(chosen)
        output.append(chosen)
        genre_counts[normalize_tag((chosen[1].genres or ["uncategorized"])[0])] += 1
    return output


async def persist_profile_pool(db: AsyncSession, profile_id: str) -> None:
    movies_result = await db.exec(select(Movie).where(Movie.catalog_source == "tmdb_cache"))
    candidates = list(movies_result.all())
    ranked = await rank_movies_for_profile(db, profile_id, candidates)
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
        item.generated_at = now
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
    tastes = await _taste_map(db, profile_id, now)
    genre_counts: Dict[str, Dict[str, Any]] = {}
    for _, movie, _, source, availability in ranked:
        for genre in movie.genres:
            key = normalize_tag(genre)
            record = genre_counts.setdefault(key, {"value": genre, "label": genre, "affinity": tastes.get(("genre", key), 0.0), "server_count": 0, "cached_count": 0})
            if availability == "available":
                record["server_count"] += 1
            else:
                record["cached_count"] += 1
    real_categories = sorted(genre_counts.values(), key=lambda item: (-item["affinity"], item["label"].casefold()))
    categories = [
        {"value": "recommended", "label": "Recommended", "affinity": 0.0, "server_count": sum(1 for row in ranked if row[4] == "available"), "cached_count": sum(1 for row in ranked if row[4] != "available")},
        {"value": "all", "label": "All Releases", "affinity": 0.0, "server_count": sum(1 for row in ranked if row[4] == "available"), "cached_count": 0},
        *real_categories,
    ]

    requested = normalize_tag(category or "recommended")
    if requested == "all":
        filtered = [row for row in ranked if row[4] == "available"]
    elif requested == "recommended":
        filtered = diversify_ranked(ranked)
    else:
        filtered = [row for row in ranked if any(normalize_tag(genre) == requested for genre in row[1].genres)]

    def serialize(row):
        score, movie, reasons, source, availability = row
        return {
            "media": MovieResponse.from_db(movie, episode_map.get(movie.id)),
            "source": source,
            "availability": availability,
            "score": round(score, 6),
            "reasons": reasons,
        }

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
    return {
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
        "watch_again": [serialize(row) for row in watch_again_rows[:20]],
    }


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
        async with AsyncSession(engine) as db:
            await persist_profile_pool(db, profile_id)
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
