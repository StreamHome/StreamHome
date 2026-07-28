from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import (
    AuthSession,
    Episode,
    Movie,
    PlaybackRun,
    PlaybackSession,
    Profile,
    ProfileMediaPreference,
    ProfileOnboardingPreference,
    ProfileRecommendation,
    ProfileTaste,
    ProfileVibeVector,
    RecommendationExposure,
    RecommendationRefreshState,
    RecommendationRuntimeMetric,
    TelemetryEvent,
    ViewingAttempt,
    WatchlistItem,
)
from routes.auth import require_recent_reauth


router = APIRouter(prefix="/api/admin/profiles", tags=["Admin profile data"])


def _movie_brief(movie: Movie | None) -> dict[str, Any] | None:
    if movie is None:
        return None
    return {
        "id": movie.id,
        "tmdbId": movie.tmdb_id,
        "title": movie.title,
        "type": movie.type or "movie",
        "releaseYear": movie.release_year,
        "thumbnailUrl": movie.local_thumbnail_url or movie.thumbnail_url,
        "catalogSource": movie.catalog_source,
        "availability": movie.availability,
        "cacheState": movie.cache_state,
    }


async def _require_profile(db: AsyncSession, profile_id: str) -> Profile:
    profile = await db.get(Profile, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "profile_not_found", "message": "The selected profile no longer exists."},
        )
    return profile


def _profile_brief(profile: Profile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "avatarColor": profile.avatar_color,
        "theme": profile.theme,
        "pinEnabled": bool(profile.pin_enabled),
        "administrator": profile.id == "1",
    }


def _parse_json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _safe_media_asset(url: str | None) -> dict[str, Any]:
    if not url or not url.startswith("/media/"):
        return {"url": url, "storedOnDisk": False, "sizeBytes": 0}
    root = Path(settings.MEDIA_DIR).resolve()
    candidate = (root / url.removeprefix("/media/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return {"url": url, "storedOnDisk": False, "sizeBytes": 0}
    is_file = candidate.is_file()
    return {
        "url": url,
        "storedOnDisk": is_file,
        "sizeBytes": candidate.stat().st_size if is_file else 0,
    }


def _cache_files(movie: Movie) -> dict[str, Any]:
    poster = _safe_media_asset(movie.local_thumbnail_url or movie.thumbnail_url)
    backdrop = _safe_media_asset(movie.local_banner_url or movie.banner_url)
    root = Path(settings.MEDIA_DIR).resolve()
    media_url = next(
        (
            value
            for value in (
                movie.local_thumbnail_url,
                movie.thumbnail_url,
                movie.local_banner_url,
                movie.banner_url,
            )
            if value and value.startswith("/media/")
        ),
        None,
    )
    metadata_exists = False
    metadata_size = 0
    if media_url:
        folder = (root / media_url.removeprefix("/media/")).resolve().parent
        try:
            folder.relative_to(root)
            metadata = folder / ".metadata" / "metadata.json"
            metadata_exists = metadata.is_file()
            metadata_size = metadata.stat().st_size if metadata_exists else 0
        except ValueError:
            pass
    return {
        "poster": poster,
        "backdrop": backdrop,
        "metadata": {"storedOnDisk": metadata_exists, "sizeBytes": metadata_size},
        "totalSizeBytes": poster["sizeBytes"] + backdrop["sizeBytes"] + metadata_size,
    }


async def _movies_by_id(db: AsyncSession, ids: Iterable[str]) -> dict[str, Movie]:
    unique_ids = list(dict.fromkeys(ids))
    if not unique_ids:
        return {}
    rows = (await db.exec(select(Movie).where(Movie.id.in_(unique_ids)))).all()
    return {movie.id: movie for movie in rows}


@router.get("")
async def list_profile_data_summaries(
    _: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    profiles = list((await db.exec(select(Profile).order_by(Profile.id))).all())
    attempts = list((await db.exec(select(ViewingAttempt))).all())
    watchlist = list((await db.exec(select(WatchlistItem))).all())
    recommendations = list((await db.exec(select(ProfileRecommendation))).all())
    preferences = list((await db.exec(select(ProfileMediaPreference))).all())
    sessions = list((await db.exec(select(PlaybackSession))).all())
    events = list((await db.exec(select(TelemetryEvent))).all())

    attempt_counts = Counter(row.profile_id for row in attempts)
    watchlist_counts = Counter(row.profile_id for row in watchlist)
    recommendation_counts = Counter(row.profile_id for row in recommendations)
    preference_counts = Counter(row.profile_id for row in preferences)
    session_counts = Counter(row.profile_id for row in sessions)
    watch_seconds: Counter[str] = Counter()
    last_activity: dict[str, float] = defaultdict(float)
    for row in attempts:
        watch_seconds[row.profile_id] += max(0, int(row.duration_watched or 0))
        last_activity[row.profile_id] = max(last_activity[row.profile_id], float(row.last_seen_at or 0))
    for row in events:
        last_activity[row.profile_id] = max(last_activity[row.profile_id], float(row.timestamp or 0))

    return {
        "profiles": [
            {
                **_profile_brief(profile),
                "historyCount": attempt_counts[profile.id],
                "resumeCount": session_counts[profile.id],
                "watchlistCount": watchlist_counts[profile.id],
                "recommendationCount": recommendation_counts[profile.id],
                "preferenceCount": preference_counts[profile.id],
                "watchSeconds": watch_seconds[profile.id],
                "lastActivityAt": last_activity[profile.id] or None,
            }
            for profile in profiles
        ],
        "storage": {
            "database": "server/database.db",
            "durableMedia": "server/media",
            "temporaryCaches": "server/temp",
            "browserPending": "bounded recommendation exposure delivery queue",
        },
    }


@router.get("/{profile_id}/overview")
async def get_profile_data_overview(
    profile_id: str,
    _: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    profile = await _require_profile(db, profile_id)
    attempts = list((await db.exec(select(ViewingAttempt).where(ViewingAttempt.profile_id == profile_id))).all())
    sessions = list((await db.exec(select(PlaybackSession).where(PlaybackSession.profile_id == profile_id))).all())
    watchlist = list((await db.exec(select(WatchlistItem).where(WatchlistItem.profile_id == profile_id))).all())
    recommendations = list((await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id))).all())
    preferences = list((await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id))).all())
    events = list((await db.exec(select(TelemetryEvent).where(TelemetryEvent.profile_id == profile_id))).all())
    exposures = list((await db.exec(select(RecommendationExposure).where(RecommendationExposure.profile_id == profile_id))).all())
    runs = list((await db.exec(select(PlaybackRun).where(PlaybackRun.profile_id == profile_id))).all())
    activity_values = [float(row.last_seen_at or 0) for row in attempts]
    activity_values.extend(float(row.timestamp or 0) for row in events)
    return {
        "profile": _profile_brief(profile),
        "counts": {
            "history": len(attempts),
            "resumeStates": len(sessions),
            "watchlist": len(watchlist),
            "recommendations": len(recommendations),
            "preferences": len(preferences),
            "events": len(events),
            "exposures": len(exposures),
            "playbackRuns": len(runs),
        },
        "watchSeconds": sum(max(0, int(row.duration_watched or 0)) for row in attempts),
        "completedTitles": sum(1 for row in attempts if row.completed_at is not None),
        "lastActivityAt": max(activity_values) if activity_values else None,
        "activePlaybackRuns": sum(1 for row in runs if row.lifecycle_state == "active"),
        "persistence": [
            {"label": "Profile activity and preferences", "location": "SQLite", "durable": True},
            {"label": "TMDB metadata index", "location": "SQLite / shared Movie catalog", "durable": True},
            {"label": "TMDB artwork and portable metadata", "location": "server/media", "durable": True},
            {"label": "Playback preparation and subtitle caches", "location": "server/temp", "durable": False},
            {"label": "Pending exposure delivery", "location": "browser queue until acknowledged", "durable": False},
        ],
    }


@router.get("/{profile_id}/history")
async def get_profile_history(
    profile_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    await _require_profile(db, profile_id)
    all_attempts = list((await db.exec(
        select(ViewingAttempt)
        .where(ViewingAttempt.profile_id == profile_id)
        .order_by(ViewingAttempt.last_seen_at.desc())
    )).all())
    page = all_attempts[offset:offset + limit]
    episode_ids = [row.episode_id for row in page if row.episode_id]
    episodes = {
        episode.id: episode
        for episode in (await db.exec(select(Episode).where(Episode.id.in_(episode_ids)))).all()
    } if episode_ids else {}
    resume_rows = list((await db.exec(
        select(PlaybackSession)
        .where(PlaybackSession.profile_id == profile_id)
        .order_by(PlaybackSession.updated_at.desc())
    )).all())
    movies = await _movies_by_id(
        db,
        [row.movie_id for row in page] + [row.movie_id for row in resume_rows],
    )
    return {
        "total": len(all_attempts),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": row.id,
                "movie": _movie_brief(movies.get(row.movie_id)),
                "episode": {
                    "id": episode.id,
                    "title": episode.title,
                    "seasonNumber": episode.season_number,
                    "episodeNumber": episode.episode_number,
                } if (episode := episodes.get(row.episode_id or "")) else None,
                "startedAt": row.started_at,
                "lastSeenAt": row.last_seen_at,
                "maxCompletion": row.max_completion,
                "durationWatched": row.duration_watched,
                "completedAt": row.completed_at,
                "earlyExitRecorded": row.early_exit_recorded,
                "rewatchReward": row.rewatch_reward,
            }
            for row in page
        ],
        "resumeStates": [
            {
                "movie": _movie_brief(movies.get(row.movie_id)),
                "movieId": row.movie_id,
                "episodeId": row.episode_id,
                "timestamp": row.timestamp,
                "durationWatched": row.duration_watched,
                "completionRate": row.completion_rate,
                "updatedAt": row.updated_at,
                "finished": bool(row.is_finished),
            }
            for row in resume_rows
        ],
    }


@router.get("/{profile_id}/watchlist")
async def get_profile_watchlist_data(
    profile_id: str,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    await _require_profile(db, profile_id)
    rows = list((await db.exec(
        select(WatchlistItem)
        .where(WatchlistItem.profile_id == profile_id)
        .order_by(WatchlistItem.created_at.desc(), WatchlistItem.id.desc())
    )).all())
    page = rows[offset:offset + limit]
    movies = await _movies_by_id(db, (row.movie_id for row in page))
    return {
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "items": [
            {"id": row.id, "createdAt": row.created_at, "movie": _movie_brief(movies.get(row.movie_id))}
            for row in page
        ],
    }


@router.get("/{profile_id}/recommendations")
async def get_profile_recommendation_data(
    profile_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    await _require_profile(db, profile_id)
    pool = list((await db.exec(
        select(ProfileRecommendation)
        .where(ProfileRecommendation.profile_id == profile_id)
        .order_by(ProfileRecommendation.score.desc(), ProfileRecommendation.generated_at.desc())
    )).all())
    page = pool[offset:offset + limit]
    preferences = list((await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id))).all())
    movies = await _movies_by_id(
        db,
        [row.movie_id for row in page] + [row.movie_id for row in preferences],
    )
    onboarding = list((await db.exec(select(ProfileOnboardingPreference).where(ProfileOnboardingPreference.profile_id == profile_id))).all())
    tastes = list((await db.exec(
        select(ProfileTaste)
        .where(ProfileTaste.profile_id == profile_id)
        .order_by(ProfileTaste.score.desc())
        .limit(30)
    )).all())
    vibe = await db.get(ProfileVibeVector, profile_id)
    refresh = await db.get(RecommendationRefreshState, profile_id)
    runtime = await db.get(RecommendationRuntimeMetric, profile_id)
    preference_map = {row.movie_id: row.preference for row in preferences}
    return {
        "total": len(pool),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "movie": _movie_brief(movies.get(row.movie_id)),
                "score": row.score,
                "reasons": row.reasons,
                "reasonDetails": row.reason_details,
                "generatedAt": row.generated_at,
                "candidateSource": row.candidate_source,
                "sourceConfidence": row.source_confidence,
                "preference": preference_map.get(row.movie_id),
            }
            for row in page
        ],
        "preferences": [
            {
                "movieId": row.movie_id,
                "movie": _movie_brief(movies.get(row.movie_id)),
                "preference": row.preference,
                "updatedAt": row.updated_at,
            }
            for row in preferences
        ],
        "onboarding": {
            "genres": [row.value for row in onboarding if row.kind == "genre"],
            "titleIds": [row.value for row in onboarding if row.kind == "title"],
        },
        "tastes": [
            {"kind": row.tag_type, "value": row.tag_value, "score": row.score, "updatedAt": row.last_updated}
            for row in tastes
        ],
        "vibe": {
            "dialogueWpmMean": vibe.dialogue_wpm_mean,
            "dialogueConfidence": vibe.dialogue_confidence,
            "sampleWeight": vibe.dialogue_sample_weight,
            "algorithmVersion": vibe.algorithm_version,
            "updatedAt": vibe.updated_at,
        } if vibe else None,
        "refresh": {
            "tasteVersion": refresh.taste_version,
            "lastRankedAt": refresh.last_ranked_at,
            "lastTmdbRefreshAt": refresh.last_tmdb_refresh_at,
            "nextTmdbRefreshAt": refresh.next_tmdb_refresh_at,
            "refreshRequested": refresh.refresh_requested,
            "lastError": refresh.last_error,
        } if refresh else None,
        "runtimeMetric": {
            "top20Overlap": runtime.top20_overlap,
            "meanDisplacement": runtime.mean_displacement,
            "generatedAt": runtime.generated_at,
        } if runtime else None,
    }


@router.get("/{profile_id}/activity")
async def get_profile_activity(
    profile_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    await _require_profile(db, profile_id)
    events = list((await db.exec(
        select(TelemetryEvent)
        .where(TelemetryEvent.profile_id == profile_id)
        .order_by(TelemetryEvent.timestamp.desc())
    )).all())
    page = events[offset:offset + limit]
    exposures = list((await db.exec(
        select(RecommendationExposure)
        .where(RecommendationExposure.profile_id == profile_id)
        .order_by(RecommendationExposure.shown_at.desc())
        .limit(100)
    )).all())
    runs = list((await db.exec(
        select(PlaybackRun)
        .where(PlaybackRun.profile_id == profile_id)
        .order_by(PlaybackRun.updated_at.desc())
        .limit(100)
    )).all())
    movie_ids = [row.movie_id for row in page if row.movie_id]
    movie_ids.extend(row.movie_id for row in exposures)
    movie_ids.extend(row.movie_id for row in runs)
    movies = await _movies_by_id(db, movie_ids)
    return {
        "total": len(events),
        "limit": limit,
        "offset": offset,
        "events": [
            {
                "id": row.id,
                "type": row.event_type,
                "movie": _movie_brief(movies.get(row.movie_id or "")),
                "tmdbId": row.tmdb_id,
                "timestamp": row.timestamp,
                "metadata": _parse_json_object(row.metadata_json),
            }
            for row in page
        ],
        "exposures": [
            {
                "id": row.id,
                "movie": _movie_brief(movies.get(row.movie_id)),
                "feedGeneration": row.feed_generation,
                "surface": row.surface,
                "scope": row.scope,
                "category": row.category,
                "position": row.position,
                "shownAt": row.shown_at,
            }
            for row in exposures
        ],
        "playbackRuns": [
            {
                "id": row.id,
                "movie": _movie_brief(movies.get(row.movie_id)),
                "episodeId": row.episode_id,
                "state": row.lifecycle_state,
                "createdAt": row.created_at,
                "updatedAt": row.updated_at,
                "lastSeenAt": row.last_seen_at,
                "secondsPlayed": row.total_seconds_played,
            }
            for row in runs
        ],
    }


@router.get("/{profile_id}/cache")
async def get_profile_tmdb_cache(
    profile_id: str,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: AuthSession = Depends(require_recent_reauth),
    db: AsyncSession = Depends(get_session),
):
    await _require_profile(db, profile_id)
    associations: dict[str, set[str]] = defaultdict(set)
    recommendation_rows = (await db.exec(select(ProfileRecommendation).where(ProfileRecommendation.profile_id == profile_id))).all()
    watchlist_rows = (await db.exec(select(WatchlistItem).where(WatchlistItem.profile_id == profile_id))).all()
    preference_rows = (await db.exec(select(ProfileMediaPreference).where(ProfileMediaPreference.profile_id == profile_id))).all()
    event_rows = (await db.exec(select(TelemetryEvent).where(TelemetryEvent.profile_id == profile_id))).all()
    for row in recommendation_rows:
        associations[row.movie_id].add("recommendation pool")
    for row in watchlist_rows:
        associations[row.movie_id].add("watchlist")
    for row in preference_rows:
        associations[row.movie_id].add(f"{row.preference} preference")
    for row in event_rows:
        if row.movie_id:
            associations[row.movie_id].add("activity")
    cached = list((await db.exec(
        select(Movie)
        .where(Movie.catalog_source == "tmdb_cache")
        .order_by(Movie.cached_at.desc(), Movie.title)
    )).all())
    linked = [movie for movie in cached if movie.id in associations]
    page = linked[offset:offset + limit]
    return {
        "total": len(linked),
        "sharedCacheTotal": len(cached),
        "unreferencedSharedTotal": sum(1 for movie in cached if movie.id not in associations),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "movie": _movie_brief(movie),
                "associationSources": sorted(associations[movie.id]),
                "cachedAt": movie.cached_at,
                "metadataRefreshedAt": movie.metadata_refreshed_at,
                "cacheState": movie.cache_state,
                "retryCount": movie.cache_retry_count,
                "nextRetryAt": movie.cache_next_retry_at,
                "lastError": movie.cache_last_error,
                "files": _cache_files(movie),
                "shared": True,
            }
            for movie in page
        ],
    }
