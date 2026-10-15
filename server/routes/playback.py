from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import weakref
from contextvars import ContextVar
import re
import time
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Literal, Optional
from types import SimpleNamespace
from urllib.parse import quote

import aiofiles
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import APIModel, AuthSession, DownloadTask, Episode, Movie, PlaybackRun, PlaybackSession, User
from routes.auth import get_current_user
from services.logger import logger
from services.languages import language_label, normalize_language_tag
from services.audio_extractor import audio_languages, primary_embedded_audio_index, retained_primary_audio_language
from services.media_source import MediaSourceError, ResolvedMediaSource, playback_source_fingerprint, resolve_media_source
from services.media_probe import merge_local_external_audio, probe_cloud_external_audio, probe_completed_media
from services.media_analysis import current_duration_analysis, duration_label, refreshed_analysis, stored_analysis
from services.media_updates import atomic_json_write, metadata_payload, persist_portable_metadata
from services.playback_prep import playback_prep_service
from services.playback_markers import is_credits_exit_position
from services.playback_source import LocalPlaybackSource, PlaybackSourceFailure, source_reader
from services.ingest_preview import IngestPreviewError, ingest_preview_service
from services.ingestion_security import UnsafeIngestionSource, validate_headers, validate_url
from services.ingestion_headers import copy_source_headers, decode_source_headers
from services.profile_security import require_profile_access
from services.rclone import rclone_service
from services.recommendation import record_playback_progress
from lifecycle.coordination import EffectClass, WorkLease, get_runtime_coordinator, run_cancellation_safe_thread, runtime_work
from lifecycle.media_work import MediaRecoveryRequired, media_lock, media_ownership, require_resolved_media, settle
from lifecycle.media_publication import MediaMutation


router = APIRouter(prefix="/api/playback", tags=["Playback"])
PLAYBACK_TICKET_MINUTES = 15
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
SAFE_SUBTITLE_LANGUAGE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
SAFE_SUBTITLE_FILE_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,180}\.vtt$", re.IGNORECASE)
ACTIVE_REPAIR_STATUSES = {"PENDING", "DOWNLOADING", "MERGING", "MOVING_CLOUD"}
CLOUD_AUDIO_PROBE_TTL_SECONDS = 300
_cloud_audio_probe_times: dict[str, float] = {}
_run_write_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def run_write_lock(run_id: str) -> asyncio.Lock:
    return _run_write_locks.setdefault(run_id, asyncio.Lock())


_browser_stream_run: ContextVar[Optional[str]] = ContextVar("browser_stream_run", default=None)


class LeaseBoundPlaybackResponse(Response):
    """Keep an explicit playback lease through the ASGI response body lifetime."""

    def __init__(self, response: Response, lease: WorkLease):
        super().__init__(
            content=b"",
            status_code=response.status_code,
            media_type=response.media_type,
            background=response.background,
        )
        self.raw_headers = response.raw_headers
        self._response = response
        self._lease = lease

    async def __call__(self, scope, receive, send) -> None:
        coordinator = get_runtime_coordinator()
        try:
            with coordinator.bind(self._lease):
                await self._response(scope, receive, send)
        finally:
            iterator = getattr(self._response, "body_iterator", None)
            close = getattr(iterator, "aclose", None)
            try:
                if close is not None:
                    await settle(close())
            finally:
                self._lease.release()


class MediaBoundPlaybackResponse(Response):
    """Retain a pinned inode or cloud ownership until the response has ended."""

    def __init__(self, response: Response, cleanup):
        super().__init__(content=b"", status_code=response.status_code, media_type=response.media_type, background=response.background)
        self.raw_headers = response.raw_headers
        self._response = response
        self._cleanup = cleanup

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self._response(scope, receive, send)
        finally:
            await settle(self._cleanup())


def playback_response_lease(
    owner: str,
    *,
    disposable_cache: bool = False,
) -> WorkLease:
    effects = {EffectClass.PLAYBACK_STREAM}
    if disposable_cache:
        effects.add(EffectClass.DISPOSABLE_CACHE_WRITE)
    return get_runtime_coordinator().admit(owner, effects)


class PlaybackRunRequest(APIModel):
    movie_id: str
    profile_id: str
    episode_id: Optional[str] = None


class PlaybackQualityRequest(APIModel):
    rendition_id: str = Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9_-]+$")


class PlaybackProgressRequest(APIModel):
    total_seconds_played: Optional[int] = Field(default=None, ge=0, le=1_000_000_000)
    timestamp: float = Field(ge=0)
    duration_watched: float = Field(default=0, ge=0, le=120)
    is_finished: bool = False
    sequence_number: int = Field(ge=1)
    event: Literal["heartbeat", "pause", "seek", "visibility", "exit", "ended"] = "heartbeat"


class PlaybackCloseRequest(PlaybackProgressRequest):
    discard_progress: bool = False


class PlaybackStartupDiagnosticRequest(APIModel):
    transport: Literal["progressive", "hls", "native-hls"]
    stage: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_-]+$")
    error_type: Optional[str] = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_.-]*$")
    error_detail: Optional[str] = Field(default=None, max_length=120, pattern=r"^[a-zA-Z0-9_.-]*$")
    http_status: Optional[int] = Field(default=None, ge=100, le=599)
    ready_state: int = Field(ge=0, le=4)
    network_state: int = Field(ge=0, le=3)
    current_time: float = Field(ge=0)
    buffered_until: float = Field(ge=0)
    elapsed_ms: int = Field(ge=0, le=300_000)


class PlaybackSourceMetadata(APIModel):
    duration: float
    container: str
    codec: str
    width: int
    height: int
    frame_rate: float
    source_format: str
    audio_codec: str = ""
    progressive_compatible: bool = False


class PlaybackTrack(APIModel):
    id: str
    label: str
    language: str
    channels: int
    default: bool
    source: Literal["embedded", "external"]
    stream_index: int
    direct_url: Optional[str] = None
    ready: bool
    status: Literal["idle", "preparing", "streamable", "ready", "failed"]
    timeline_offset: float = 0.0
    duration: float = 0.0


class PlaybackRendition(APIModel):
    id: str
    label: str
    height: int
    width: int
    original: bool
    ready: bool
    status: Literal["idle", "preparing", "streamable", "ready", "failed"]


class PlaybackPreparationFailure(APIModel):
    code: str
    message: str


class PlaybackPreparationProgress(APIModel):
    stage: Literal["queued", "packaging", "transcoding", "audio", "streamable", "failed"]
    queue_position: int = 0
    ready_segments: int = 0
    active_workers: int = 0


class PlaybackRunResponse(APIModel):
    stream_base_url: Optional[str] = None
    run_id: str
    media_id: str
    movie_id: str
    episode_id: Optional[str] = None
    source_fingerprint: str
    resume_position: float
    source_metadata: PlaybackSourceMetadata
    tracks: list[PlaybackTrack]
    renditions: list[PlaybackRendition]
    subtitles: list[dict[str, str]]
    ticket: str
    ticket_expires_at: float
    manifest_url: Optional[str] = None
    progressive_url: str
    next_episode_id: Optional[str] = None
    preparation_state: Literal["preparing", "ready", "error"]
    preparation_error: Optional[PlaybackPreparationFailure] = None
    preparation_progress: PlaybackPreparationProgress
    seekable_until: float = 0
    resume_ready: bool = False
    switching_ready: bool = False
    fully_prepared: bool = False
    next_sequence_number: int


def playback_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def require_playable_media(*media_ids: str) -> None:
    try:
        for media_id in media_ids:
            require_resolved_media(media_id)
    except MediaRecoveryRequired as exc:
        raise playback_error(status.HTTP_503_SERVICE_UNAVAILABLE, "MEDIA_RECOVERY_REQUIRED", "This media publication requires recovery before playback.") from exc


async def run_media_ownership_ids(db: AsyncSession, run_id: str) -> tuple[str, ...]:
    # Release this lookup connection before waiting for a publisher. The actual
    # session/profile/source authorization is repeated inside the ownership scope.
    async with AsyncSession(db.bind) as lookup:
        run = await lookup.get(PlaybackRun, run_id)
        if run is None:
            raise playback_error(status.HTTP_404_NOT_FOUND, "PLAYBACK_RUN_NOT_FOUND", "The playback run does not exist.")
        return (run.movie_id, run.episode_id) if run.episode_id else (run.movie_id,)


def current_auth_session(request: Request) -> AuthSession:
    auth_session = getattr(request.state, "auth_session", None)
    if not isinstance(auth_session, AuthSession):
        raise playback_error(status.HTTP_401_UNAUTHORIZED, "AUTHENTICATION_REQUIRED", "A valid signed-in session is required.")
    return auth_session


async def active_auth_session(db: AsyncSession, request: Request) -> AuthSession:
    identity = current_auth_session(request).id
    auth_session = await db.get(AuthSession, identity, populate_existing=True)
    if not auth_session or auth_session.revoked_at or auth_session.expires_at <= time.time():
        raise playback_error(status.HTTP_401_UNAUTHORIZED, "PLAYBACK_SESSION_REVOKED", "The signed-in session has expired or was revoked.")
    request.state.auth_session = auth_session
    return auth_session


def issue_playback_ticket(
    user: User,
    auth_session: AuthSession,
    profile_id: str,
    run_id: str,
    media_id: str,
    fingerprint: str,
) -> tuple[str, float]:
    issued_at = int(time.time())
    expires_at = issued_at + PLAYBACK_TICKET_MINUTES * 60
    payload = {
        "typ": "playback",
        "sub": user.email,
        "jti": auth_session.id,
        "profile_id": profile_id,
        "run_id": run_id,
        "media_id": media_id,
        "fingerprint": fingerprint,
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM), float(expires_at)


async def validate_playback_ticket(ticket: str, media_id: str, db: AsyncSession) -> tuple[dict[str, Any], PlaybackRun, Any]:
    try:
        payload = jwt.decode(ticket, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_TICKET_EXPIRED", "The playback ticket has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_TICKET_INVALID", "The playback ticket is invalid.") from exc

    required = {"jti", "profile_id", "run_id", "media_id", "fingerprint"}
    if payload.get("typ") != "playback" or not required.issubset(payload):
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_TICKET_INVALID", "The playback ticket is incomplete.")
    if payload["media_id"] != media_id:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_TICKET_SCOPE_MISMATCH", "The playback ticket does not permit this media.")

    run = await db.get(PlaybackRun, str(payload["run_id"]))
    auth_session = await db.get(AuthSession, str(payload["jti"]), populate_existing=True)
    if not run or not auth_session:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_SESSION_INACTIVE", "The playback session is no longer active.")
    now = time.time()
    if auth_session.revoked_at or auth_session.expires_at <= now:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_SESSION_REVOKED", "The signed-in session has expired or was revoked.")
    if run.lifecycle_state not in {"active", "finished"}:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_RUN_EXPIRED", "The playback run is no longer active.")
    if (
        run.auth_session_id != auth_session.id
        or run.profile_id != payload["profile_id"]
        or (run.episode_id or run.movie_id) != media_id
    ):
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_TICKET_SCOPE_MISMATCH", "The playback ticket scope is invalid.")
    await require_profile_access(db, auth_session, run.profile_id)

    media_obj = await db.get(Episode, run.episode_id) if run.episode_id else await db.get(Movie, run.movie_id)
    if not media_obj:
        raise playback_error(status.HTTP_409_CONFLICT, "PLAYBACK_SOURCE_CHANGED", "The media source changed. Start playback again.")
    if run.source_kind == "ingest_preview":
        if not run.source_task_id or run.source_fingerprint != payload["fingerprint"]:
            raise playback_error(status.HTTP_409_CONFLICT, "PLAYBACK_SOURCE_CHANGED", "The ingestion preview changed. Start playback again.")
    elif media_obj.source_fingerprint != payload["fingerprint"]:
        raise playback_error(status.HTTP_409_CONFLICT, "PLAYBACK_SOURCE_CHANGED", "The media source changed. Start playback again.")
    return payload, run, media_obj


async def resolve_run_media(db: AsyncSession, movie_id: str, episode_id: Optional[str]) -> tuple[Movie, Any]:
    movie = await db.get(Movie, movie_id)
    if not movie:
        raise playback_error(status.HTTP_404_NOT_FOUND, "MEDIA_NOT_FOUND", "The requested title does not exist.")
    if movie.type == "series":
        if not episode_id:
            raise playback_error(status.HTTP_400_BAD_REQUEST, "EPISODE_REQUIRED", "Choose an episode before starting playback.")
        episode = await db.get(Episode, episode_id)
        if not episode or episode.movie_id != movie.id:
            raise playback_error(status.HTTP_404_NOT_FOUND, "EPISODE_NOT_FOUND", "The episode does not belong to this series.")
        return movie, episode
    if episode_id:
        raise playback_error(status.HTTP_400_BAD_REQUEST, "UNEXPECTED_EPISODE", "Movies cannot be started with an episode identifier.")
    return movie, movie


async def require_available_source(media_obj: Any) -> ResolvedMediaSource:
    try:
        source = await resolve_media_source(media_obj.video_url, refresh_cloud=True)
    except MediaSourceError as exc:
        raise playback_error(status.HTTP_409_CONFLICT, "INVALID_MEDIA_PATH", "The catalog contains an invalid playback path.") from exc
    if not source.available:
        raise playback_error(status.HTTP_409_CONFLICT, "MEDIA_SOURCE_MISSING", "The media file is not currently available on this server.")
    return source


async def clear_preview_task_reference(
    db: AsyncSession,
    media_obj: Any,
    task_id: str,
    reason: str,
) -> None:
    if str(getattr(media_obj, "preview_task_id", None) or "") != task_id:
        return
    async with runtime_work("playback.catalog-repair", [EffectClass.DB_WRITE]):
        media_obj.preview_task_id = None
        db.add(media_obj)
        await db.commit()
    logger.info(f"[Playback Run] Cleared stale preview task {task_id} for {media_obj.id}: {reason}.")


async def active_preview_task(db: AsyncSession, movie: Movie, media_obj: Any) -> Optional[DownloadTask]:
    task_id = str(getattr(media_obj, "preview_task_id", None) or "")
    if not task_id:
        return None
    task = await db.get(DownloadTask, task_id)
    if not task:
        await clear_preview_task_reference(db, media_obj, task_id, "download task no longer exists")
        return None
    task_matches_media = (
        task.tmdb_id == movie.tmdb_id
        and (
            task.media_type == "movie"
            if media_obj is movie
            else (
                task.media_type != "movie"
                and task.season == media_obj.season_number
                and task.episode == media_obj.episode_number
            )
        )
    )
    if not task_matches_media:
        await clear_preview_task_reference(db, media_obj, task_id, "download task belongs to different media")
        return None
    if task.status not in ACTIVE_REPAIR_STATUSES:
        await clear_preview_task_reference(db, media_obj, task_id, f"download task is {task.status.lower()}")
        return None
    return task


async def queue_media_repair(
    db: AsyncSession,
    movie: Movie,
    media_obj: Any,
    reason_code: str,
) -> Optional[DownloadTask]:
    """Queue one verified replacement from retained ingestion intent, without deleting the catalog row."""

    async with runtime_work("playback.catalog-repair", [EffectClass.DB_WRITE]):
        return await _queue_media_repair_admitted(db, movie, media_obj, reason_code)


async def _queue_media_repair_admitted(
    db: AsyncSession,
    movie: Movie,
    media_obj: Any,
    reason_code: str,
) -> Optional[DownloadTask]:

    filters = [DownloadTask.tmdb_id == int(movie.tmdb_id or 0)]
    if media_obj is movie:
        filters.extend([DownloadTask.media_type == "movie", DownloadTask.season.is_(None), DownloadTask.episode.is_(None)])
    else:
        filters.extend(
            [
                DownloadTask.media_type != "movie",
                DownloadTask.season == media_obj.season_number,
                DownloadTask.episode == media_obj.episode_number,
            ]
        )

    existing = (
        await db.exec(
            select(DownloadTask)
            .where(*filters, DownloadTask.status.in_(ACTIVE_REPAIR_STATUSES))
            .order_by(DownloadTask.created_at.desc())
        )
    ).first()
    if existing:
        media_obj.preview_task_id = existing.id
        if media_obj is movie:
            movie.availability = "processing"
        db.add(media_obj)
        db.add(movie)
        await db.commit()
        return existing

    previous = (
        await db.exec(
            select(DownloadTask)
            .where(*filters, DownloadTask.status == "COMPLETED")
            .order_by(DownloadTask.created_at.desc())
        )
    ).first()
    if not previous or not previous.video_url.startswith(("http://", "https://")):
        return None

    try:
        client_address = "127.0.0.1" if previous.private_source_allowed else ""
        await validate_url(previous.video_url, client_address=client_address)
        await validate_url(previous.audio_url, client_address=client_address)
        retained_headers = copy_source_headers(previous.headers_str, validate_headers)
        normalized_headers = decode_source_headers(retained_headers, "video")
        retained_subtitles = []
        for subtitle in previous.subtitles:
            await validate_url(subtitle.get("url"), client_address=client_address)
            retained_subtitles.append({
                **subtitle,
                "headers": validate_headers(subtitle.get("headers"))
                if subtitle.get("headers") is not None else dict(normalized_headers),
            })
    except (UnsafeIngestionSource, ValueError, TypeError, AttributeError) as exc:
        logger.warning(f"[Playback Repair] Retained source for {media_obj.id} is no longer safe: {type(exc).__name__}")
        return None

    repair = DownloadTask(
        id=str(uuid.uuid4()),
        tmdb_id=previous.tmdb_id,
        title=previous.title,
        media_type=previous.media_type,
        season=previous.season,
        episode=previous.episode,
        video_url=previous.video_url,
        audio_url=previous.audio_url,
        video_source_type=previous.video_source_type,
        audio_source_type=previous.audio_source_type,
        headers_str=retained_headers,
        private_source_allowed=previous.private_source_allowed,
        status="PENDING",
        subtitles_str=json.dumps(retained_subtitles),
        quality=previous.quality,
        language=previous.language,
        skip_markers_str=previous.skip_markers_str,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(repair)
    media_obj.preview_task_id = repair.id
    if media_obj is movie:
        movie.availability = "processing"
    elif movie.availability != "available":
        movie.availability = "processing"
    db.add(media_obj)
    db.add(movie)
    await db.commit()
    logger.warning(f"[Playback Repair] Queued replacement task {repair.id} for {media_obj.id} after {reason_code}.")
    return repair


async def synchronize_source_fingerprint(db: AsyncSession, media_obj: Any, source: ResolvedMediaSource) -> None:
    fingerprint = playback_source_fingerprint(source, media_obj.audio_metadata or [])
    if media_obj.source_fingerprint == fingerprint:
        return
    async with runtime_work(
        "playback.metadata-repair",
        [EffectClass.DB_WRITE, EffectClass.DURABLE_MEDIA_WRITE],
    ):
        await _synchronize_source_fingerprint_admitted(db, media_obj, source, fingerprint)


async def _synchronize_source_fingerprint_admitted(
    db: AsyncSession,
    media_obj: Any,
    source: ResolvedMediaSource,
    fingerprint: str,
) -> None:
    # Changing a ticket identity must never promote unbound historical proof.
    # The shared repair path measures changed video and republishes sidecar-only
    # identity changes while preserving proof for the same inspected video.
    old_fingerprint = media_obj.source_fingerprint
    await _ensure_source_metadata_admitted(db, media_obj, source)
    if media_obj.source_fingerprint != playback_source_fingerprint(source, media_obj.audio_metadata or []):
        raise playback_error(status.HTTP_409_CONFLICT, "MEDIA_SOURCE_CHANGED", "The inspected media source changed before acceptance.")
    if old_fingerprint and old_fingerprint != media_obj.source_fingerprint:
        playback_prep_service.cancel_media(media_obj.id, old_fingerprint)


async def ensure_source_metadata(
    db: AsyncSession,
    media_obj: Any,
    source: ResolvedMediaSource,
    *,
    refresh_sidecars: bool = False,
) -> None:
    async with runtime_work(
        "playback.metadata-repair",
        [EffectClass.DB_WRITE, EffectClass.DURABLE_MEDIA_WRITE],
    ):
        try:
            await _ensure_source_metadata_admitted(
                db,
                media_obj,
                source,
                refresh_sidecars=refresh_sidecars,
            )
        except MediaRecoveryRequired as exc:
            raise playback_error(status.HTTP_503_SERVICE_UNAVAILABLE, "MEDIA_RECOVERY_REQUIRED", "The inspected media publication needs recovery.") from exc


async def _read_cloud_portable_metadata(source: ResolvedMediaSource, temporary: Path) -> dict[str, Any]:
    remote = f"{source.cloud_path.rsplit('/', 1)[0]}/.metadata/metadata.json"
    destination = temporary / "previous-metadata.json"
    state = await rclone_service.run("lsjson", remote, "--stat", timeout=30)
    if not state.ok:
        if state.error_code == "drive_not_found":
            return {}
        raise playback_error(status.HTTP_503_SERVICE_UNAVAILABLE, "CLOUD_METADATA_UNAVAILABLE", "The cloud metadata could not be inspected safely.")
    result = await rclone_service.copyto_atomic(remote, str(destination), timeout=60)
    if not result.ok:
        raise playback_error(status.HTTP_503_SERVICE_UNAVAILABLE, "CLOUD_METADATA_UNAVAILABLE", "The cloud metadata could not be inspected safely.")
    try:
        value = json.loads(destination.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError, TypeError):
        pass
    raise playback_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "PORTABLE_METADATA_INVALID", "The portable media metadata is invalid.")


async def _ensure_source_metadata_admitted(
    db: AsyncSession,
    media_obj: Any,
    source: ResolvedMediaSource,
    *,
    refresh_sidecars: bool = False,
) -> None:
    previous_audio = list(media_obj.audio_metadata or [])
    previous_analysis = stored_analysis(media_obj.analysis_metadata_str)
    metadata_missing = not media_obj.probed_duration or not media_obj.codec or not media_obj.width or not media_obj.height
    video_identity_stale = previous_analysis.get("video_fingerprint") != source.video_fingerprint
    requires_probe = metadata_missing or video_identity_stale or not current_duration_analysis(previous_analysis)
    local_portable_metadata = None
    if source.local_exists:
        metadata_path = source.local_path.parent / ".metadata" / "metadata.json"
        if metadata_path.is_file():
            try:
                local_portable_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if not isinstance(local_portable_metadata, dict):
                    raise ValueError("Invalid portable metadata object")
            except (OSError, ValueError, TypeError) as exc:
                raise playback_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "PORTABLE_METADATA_INVALID", "The portable media metadata is invalid.") from exc
            requires_probe = requires_probe or local_portable_metadata.get("video_fingerprint") != source.video_fingerprint
        refreshed_audio = merge_local_external_audio(str(source.local_path), previous_audio)
    else:
        if not source.cloud_exists or not source.cloud_path:
            raise playback_error(status.HTTP_409_CONFLICT, "MEDIA_SOURCE_MISSING", "The media source is no longer available.")
        last_probe = _cloud_audio_probe_times.get(str(media_obj.id), 0)
        refreshed_audio = previous_audio
        if not requires_probe and (refresh_sidecars or time.monotonic() - last_probe >= CLOUD_AUDIO_PROBE_TTL_SECONDS):
            refreshed_audio = await probe_cloud_external_audio(source.cloud_path, previous_audio)
            _cloud_audio_probe_times[str(media_obj.id)] = time.monotonic()
    source_replaced = media_obj.source_fingerprint != playback_source_fingerprint(source, refreshed_audio)
    proof_unbound = previous_analysis.get("source_fingerprint") != media_obj.source_fingerprint
    if not requires_probe and refreshed_audio == previous_audio and not source_replaced and not proof_unbound:
        return

    # Read/copy/probe every remote asset before starting a SQLite write. The
    # immutable private spool is tied to this video's provider identity, rather
    # than a previous catalog fingerprint or mutable remote input URL.
    Path(settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="playback-metadata-", dir=settings.TEMP_DIR) as directory:
        temporary = Path(directory)
        metadata_path = source.local_path.parent / ".metadata" / "metadata.json"
        if source.local_exists:
            portable_metadata = local_portable_metadata or {}
            probe = await probe_completed_media(str(source.local_path)) if requires_probe else None
        else:
            portable_metadata = await _read_cloud_portable_metadata(source, temporary)
            probe = None
            if requires_probe:
                try:
                    spool, process = await playback_prep_service._input_process(source, str(media_obj.id), source.video_fingerprint)
                    if process is not None:
                        raise RuntimeError("Cloud inspection requires a seekable spool")
                    probe = await probe_completed_media(spool)
                except Exception as exc:
                    raise playback_error(status.HTTP_503_SERVICE_UNAVAILABLE, "CLOUD_PROBE_FAILED", "The cloud video could not be staged for inspection.") from exc
                observed = await resolve_media_source(source.catalog_path, require_cloud=True, refresh_cloud=True)
                if not observed.cloud_exists or observed.video_fingerprint != source.video_fingerprint:
                    raise playback_error(status.HTTP_409_CONFLICT, "MEDIA_SOURCE_CHANGED", "The cloud video changed during inspection.")
        if requires_probe and (not probe or probe.get("valid_video") is not True):
            raise playback_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "MEDIA_PROBE_FAILED", "The selected video duration could not be measured for playback.")
        if probe:
            refreshed_audio = list(probe.get("audio_metadata") or [])
            retained_audio = []
            retained_analysis = {}
            if (
                previous_analysis.get("video_fingerprint") == source.video_fingerprint
                or media_obj.source_fingerprint == playback_source_fingerprint(source, previous_audio)
            ):
                retained_audio.append(previous_audio)
                retained_analysis = refreshed_analysis(None, previous_analysis)
            portable_audio = portable_metadata.get("audio_metadata") or []
            if (
                portable_metadata.get("video_fingerprint") == source.video_fingerprint
                or portable_metadata.get("source_fingerprint") == playback_source_fingerprint(source, portable_audio)
            ):
                retained_audio.append(portable_audio)
                if "sender_quality" not in retained_analysis:
                    retained_analysis = refreshed_analysis(None, portable_metadata.get("analysis"))
            refreshed_audio = retained_primary_audio_language(
                refreshed_audio, *retained_audio, same_video=bool(retained_audio),
            )
            if not source.local_exists:
                refreshed_audio = await probe_cloud_external_audio(source.cloud_path, refreshed_audio)
                _cloud_audio_probe_times[str(media_obj.id)] = time.monotonic()
            analysis = refreshed_analysis(probe.get("analysis"), retained_analysis)
        else:
            analysis = dict(previous_analysis)
        fingerprint = playback_source_fingerprint(source, refreshed_audio)
        analysis["video_fingerprint"] = source.video_fingerprint
        analysis["source_fingerprint"] = fingerprint
        payload = dict(portable_metadata) if portable_metadata else {}
        # The current catalog owner supplies alias identity and accepted mutables;
        # a legacy portable copy cannot select a different episode's alias.
        payload.update(metadata_payload(media_obj))
        if not portable_metadata:
            series = isinstance(media_obj, Episode) or getattr(media_obj, "media_type", "movie") == "series"
            tmdb_id = getattr(media_obj, "tmdb_id", None)
            catalog_id = str(media_obj.movie_id if isinstance(media_obj, Episode) else media_obj.id)
            if not tmdb_id:
                match = re.fullmatch(r"(?:m_|tv_)([1-9][0-9]*)", catalog_id)
                tmdb_id = int(match.group(1)) if match else None
            payload.update(video_url=media_obj.video_url, catalog_source=getattr(media_obj, "catalog_source", "server"),
                media_type="series" if series else "movie", release_year=getattr(media_obj, "release_year", None), tmdb_id=tmdb_id)
            if isinstance(media_obj, Episode):
                payload.update(season=media_obj.season_number, episode=media_obj.episode_number)
        payload.update(analysis=analysis, source_fingerprint=fingerprint,
            video_fingerprint=source.video_fingerprint, audio_metadata=refreshed_audio,
            languages=audio_languages(refreshed_audio) or list(media_obj.languages))
        primary = primary_embedded_audio_index(refreshed_audio)
        sender_language = None
        if primary is not None:
            payload["language"] = refreshed_audio[primary]["language"]
            if refreshed_audio[primary].get("languageOverride", {}).get("source") == "sender":
                sender_language = refreshed_audio[primary]["language"]
                payload["original_language"] = sender_language
        if probe:
            for field in ("probed_duration", "container", "codec", "width", "height", "frame_rate", "stream_manifest"):
                payload[field] = probe.get(field)
            payload["quality"] = probe.get("quality") or "Source"
            payload["duration"] = duration_label(probe.get("probed_duration"))
        async with MediaMutation(str(media_obj.id)) as mutation:
            await persist_portable_metadata(source, payload, temporary)
            if probe:
                for field in ("probed_duration", "container", "codec", "width", "height", "frame_rate"):
                    setattr(media_obj, field, probe.get(field))
                media_obj.duration = payload["duration"]
                media_obj.quality = payload["quality"]
            media_obj.source_fingerprint = fingerprint
            media_obj.audio_metadata = refreshed_audio
            media_obj.languages = payload["languages"]
            if sender_language and isinstance(media_obj, Movie):
                media_obj.original_language = sender_language
            media_obj.analysis_metadata_str = json.dumps(analysis)
            db.add(media_obj)
            await mutation.commit(db)


async def next_playable_episode(db: AsyncSession, movie_id: str, current_id: str) -> Optional[str]:
    episodes = (await db.exec(select(Episode).where(Episode.movie_id == movie_id))).all()
    ordered = sorted(episodes, key=lambda item: (item.season_number, item.episode_number))
    current_index = next((index for index, item in enumerate(ordered) if item.id == current_id), -1)
    if current_index < 0:
        return None
    for episode in ordered[current_index + 1:]:
        if episode.preview_task_id:
            return episode.id
        if not episode.video_url:
            continue
        try:
            if (await resolve_media_source(episode.video_url)).available:
                return episode.id
        except MediaSourceError:
            continue
    return None


def catalog_duration_seconds(value: Any) -> float:
    raw = str(value or "").strip().lower()
    if not raw:
        return 0.0
    colon_match = re.fullmatch(r"(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?", raw)
    if colon_match:
        first, second, third = (int(part) if part is not None else None for part in colon_match.groups())
        if third is not None:
            return float(first * 3600 + second * 60 + third)
        return float(first * 3600 + second * 60)
    hours = re.search(r"(\d+(?:\.\d+)?)\s*h", raw)
    minutes = re.search(r"(\d+(?:\.\d+)?)\s*m", raw)
    seconds = re.search(r"(\d+(?:\.\d+)?)\s*s", raw)
    total = (float(hours.group(1)) * 3600 if hours else 0.0)
    total += float(minutes.group(1)) * 60 if minutes else 0.0
    total += float(seconds.group(1)) if seconds else 0.0
    if total > 0:
        return total
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def media_duration_seconds(media_obj: Any) -> float:
    probed = max(0.0, float(getattr(media_obj, "probed_duration", 0) or 0))
    return probed if probed > 0 else catalog_duration_seconds(getattr(media_obj, "duration", ""))


def resume_position(session_rec: Optional[PlaybackSession], duration: float, *, is_movie: bool = False) -> float:
    if not session_rec or session_rec.is_finished or duration <= 0:
        return 0
    position = max(0.0, float(session_rec.timestamp))
    return position if 30 <= position < duration and (is_movie or position / duration < 0.95) else 0


async def run_resume_position(db: AsyncSession, run: PlaybackRun, media_obj: Any) -> float:
    filters = [PlaybackSession.profile_id == run.profile_id, PlaybackSession.movie_id == run.movie_id]
    filters.append(PlaybackSession.episode_id == run.episode_id if run.episode_id else PlaybackSession.episode_id.is_(None))
    session_rec = (await db.exec(select(PlaybackSession).where(*filters))).first()
    return resume_position(session_rec, media_duration_seconds(media_obj), is_movie=not run.episode_id and media_obj.type == "movie")


def source_metadata(media_obj: Any) -> PlaybackSourceMetadata:
    container = str(media_obj.container or "")
    suffix = Path(str(getattr(media_obj, "video_url", ""))).suffix.lower()
    if suffix == ".mp4" or "mp4" in container.lower():
        source_format = "MP4"
    elif suffix:
        source_format = suffix.lstrip(".").upper()
    else:
        source_format = container.split(",", 1)[0].upper() if container else "Server media"
    embedded_audio = [
        item
        for item in (media_obj.audio_metadata or [])
        if str(item.get("source") or "embedded").lower() == "embedded"
    ]
    default_audio = next((item for item in embedded_audio if item.get("default")), embedded_audio[0] if embedded_audio else None)
    audio_codec = str((default_audio or {}).get("codec") or "").lower()
    progressive_compatible = (
        (suffix == ".mp4" or "mp4" in container.lower())
        and str(media_obj.codec or "").lower() in {"h264", "avc", "avc1"}
        and audio_codec in {"", "aac", "mp4a", "mp3"}
        and stored_analysis(getattr(media_obj, "analysis_metadata_str", None)).get("direct_timeline_compatible") is True
        and current_duration_analysis(getattr(media_obj, "analysis_metadata_str", None))
        and stored_analysis(getattr(media_obj, "analysis_metadata_str", None)).get("source_fingerprint") == media_obj.source_fingerprint
    )
    return PlaybackSourceMetadata(
        duration=media_duration_seconds(media_obj),
        container=container,
        codec=str(media_obj.codec or ""),
        width=max(0, int(media_obj.width or 0)),
        height=max(0, int(media_obj.height or 0)),
        frame_rate=max(0.0, float(media_obj.frame_rate or 0)),
        source_format=source_format,
        audio_codec=audio_codec,
        progressive_compatible=progressive_compatible,
    )


def progressive_source_compatible(media_obj: Any) -> bool:
    return source_metadata(media_obj).progressive_compatible


def track_contract(media_obj: Any, media_id: str, fingerprint: str, encoded_ticket: str) -> list[PlaybackTrack]:
    metadata = list(media_obj.audio_metadata or [])
    direct_embedded_ready = progressive_source_compatible(media_obj)
    result: list[PlaybackTrack] = []
    for item in playback_prep_service.audio_renditions(media_obj):
        rendition_status = playback_prep_service.rendition_status(media_id, fingerprint, item.name)
        direct_url = (
            f"/api/playback/source/{quote(media_id, safe='')}?ticket={encoded_ticket}&source_id={quote(item.name, safe='')}"
            if item.source == "external"
            else None
        )
        ready = bool(direct_url) or (item.default and direct_embedded_ready) or rendition_status in {"streamable", "ready"}
        effective_status = "ready" if bool(direct_url) or (item.default and direct_embedded_ready) else rendition_status
        source_item = next(
            (
                candidate
                for position, candidate in enumerate(metadata)
                if str(candidate.get("source") or "embedded").lower() == item.source
                and (
                    int(candidate.get("streamIndex", candidate.get("index", position))) == item.stream_index
                    if item.source == "embedded"
                    else int(candidate.get("index", position)) == item.stream_index
                )
            ),
            {},
        )
        result.append(PlaybackTrack(
            id=item.name,
            label=item.label,
            language=item.language,
            channels=int(source_item.get("channels", 2)),
            default=item.default,
            source="external" if item.source == "external" else "embedded",
            stream_index=item.stream_index,
            direct_url=direct_url,
            ready=ready,
            status=effective_status,
            timeline_offset=float(source_item.get("timelineOffset") or 0.0),
            duration=max(0.0, float(source_item.get("duration") or 0.0)),
        ))
    return result


def rendition_contract(media_obj: Any, media_id: str, fingerprint: str) -> list[PlaybackRendition]:
    result: list[PlaybackRendition] = []
    for item in playback_prep_service.video_renditions(media_obj):
        rendition_status = playback_prep_service.rendition_status(media_id, fingerprint, item.name)
        verified = rendition_status == "ready" and playback_prep_service.rendition_verified(media_id, fingerprint, item.name)
        if not verified:
            continue
        result.append(PlaybackRendition(
            id=item.name,
            label=item.label,
            height=item.height,
            width=item.width,
            original=item.original,
            ready=True,
            status="ready",
        ))
    return result


def ready_hls_manifest_url(media_obj: Any, fingerprint: str, encoded_ticket: str, renditions: list[PlaybackRendition]) -> Optional[str]:
    if not renditions or not playback_prep_service.playback_ready(media_obj.id, fingerprint, media_obj):
        return None
    return f"/api/playback/manifest/{quote(media_obj.id, safe='')}?ticket={encoded_ticket}"


def subtitle_track_id(item: dict[str, Any]) -> Optional[str]:
    file_tag = str(item.get("language") or "und").lower()
    if not SAFE_SUBTITLE_LANGUAGE_RE.fullmatch(file_tag):
        return None
    file_name = subtitle_file_name(item)
    if not file_name:
        return None
    identity = json.dumps(
        {
            "fileTag": file_tag,
            "fileName": file_name.lower(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sub_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def subtitle_file_name(item: dict[str, Any]) -> Optional[str]:
    file_tag = str(item.get("language") or "und").lower()
    if not SAFE_SUBTITLE_LANGUAGE_RE.fullmatch(file_tag):
        return None
    explicit = str(item.get("fileName") or item.get("file_name") or "").strip()
    candidate = explicit or f"subtitle_{file_tag}.vtt"
    if Path(candidate).name != candidate or not SAFE_SUBTITLE_FILE_RE.fullmatch(candidate):
        return None
    return candidate


def subtitle_contract(media_obj: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in media_obj.subtitles or []:
        file_tag = str(item.get("language") or "und").lower()
        language = normalize_language_tag(file_tag)
        track_id = subtitle_track_id(item)
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        result.append({"id": track_id, "language": language, "label": language_label(language, item.get("label"))})
    return result


async def build_run_response(
    db: AsyncSession,
    request: Request,
    user: User,
    run: PlaybackRun,
    media_obj: Any,
    *,
    initial_resume_position: float,
) -> PlaybackRunResponse:
    auth_session = current_auth_session(request)
    if run.source_kind == "ingest_preview":
        if not run.source_task_id or not run.source_fingerprint:
            raise playback_error(status.HTTP_409_CONFLICT, "PREVIEW_SOURCE_MISSING", "The play-while-downloading source is unavailable.")
        preview_status = ingest_preview_service.status(run.source_task_id)
        fingerprint = run.source_fingerprint
        failure = (
            PlaybackPreparationFailure(
                code=preview_status["error_code"] or "PREVIEW_FAILED",
                message=preview_status["error_message"] or "The play-while-downloading stream could not be prepared.",
            )
            if preview_status["phase"] == "error"
            else None
        )
        ticket, expires_at = issue_playback_ticket(user, auth_session, run.profile_id, run.id, media_obj.id, fingerprint)
        encoded_ticket = quote(ticket, safe="")
        duration = max(float(getattr(media_obj, "probed_duration", 0) or 0), float(preview_status["duration_seconds"] or 0))
        return PlaybackRunResponse(
        stream_base_url=f"/api/playback/streams/{quote(run.id, safe='')}",
            run_id=run.id,
            media_id=media_obj.id,
            movie_id=run.movie_id,
            episode_id=run.episode_id,
            source_fingerprint=fingerprint,
            resume_position=0,
            source_metadata=PlaybackSourceMetadata(
                duration=duration,
                container="hls",
                codec="h264",
                width=max(0, int(getattr(media_obj, "width", 0) or 0)),
                height=min(720, max(0, int(getattr(media_obj, "height", 0) or 720))),
                frame_rate=max(0.0, float(getattr(media_obj, "frame_rate", 0) or 0)),
                source_format="HLS preview",
                audio_codec="aac",
                progressive_compatible=False,
            ),
            tracks=[],
            renditions=[
                PlaybackRendition(
                    id="ingest_preview",
                    label="Downloading preview",
                    height=min(720, max(1, int(getattr(media_obj, "height", 0) or 720))),
                    width=max(0, int(getattr(media_obj, "width", 0) or 0)),
                    original=False,
                    ready=preview_status["phase"] == "ready",
                    status=(
                        "ready"
                        if preview_status["phase"] == "ready"
                        else "failed" if preview_status["phase"] == "error" else "preparing"
                    ),
                )
            ],
            subtitles=[],
            ticket=ticket,
            ticket_expires_at=expires_at,
            manifest_url=(
                f"/api/playback/preview/{quote(media_obj.id, safe='')}/playlist.m3u8?ticket={encoded_ticket}"
                if preview_status["phase"] == "ready"
                else None
            ),
            progressive_url="",
            next_episode_id=None,
            preparation_state=preview_status["phase"],
            preparation_error=failure,
            preparation_progress=PlaybackPreparationProgress(
                stage=(
                    "streamable"
                    if preview_status["phase"] == "ready"
                    else "failed" if preview_status["phase"] == "error" else "transcoding"
                ),
                ready_segments=max(0, int(preview_status.get("segment_count") or 0)),
                active_workers=1 if preview_status["phase"] == "preparing" else 0,
            ),
            seekable_until=max(0, int(preview_status.get("segment_count") or 0)) * 4,
            resume_ready=True,
            switching_ready=preview_status["phase"] == "ready",
            fully_prepared=preview_status["phase"] == "ready",
            next_sequence_number=run.sequence_number,
        )

    fingerprint = str(media_obj.source_fingerprint or "")
    ticket, expires_at = issue_playback_ticket(user, auth_session, run.profile_id, run.id, media_obj.id, fingerprint)
    encoded_ticket = quote(ticket, safe="")
    next_episode_id = await next_playable_episode(db, run.movie_id, run.episode_id) if run.episode_id else None
    # Copy loaded scalar/JSON fields before moving filesystem work off the loop.
    media_obj = SimpleNamespace(**{name: getattr(media_obj, name, None) for name in (
        "id", "source_fingerprint", "probed_duration", "duration", "video_url", "container",
        "codec", "width", "height", "frame_rate", "quality", "analysis_metadata_str",
        "audio_metadata", "languages", "subtitles",
    )})
    run = SimpleNamespace(id=run.id, movie_id=run.movie_id, episode_id=run.episode_id, sequence_number=run.sequence_number)

    def catalog_response() -> PlaybackRunResponse:
        with playback_prep_service.readiness_snapshot():
            tracks = track_contract(media_obj, media_obj.id, fingerprint, encoded_ticket)
            renditions = rendition_contract(media_obj, media_obj.id, fingerprint)
            manifest_url = ready_hls_manifest_url(media_obj, fingerprint, encoded_ticket, renditions)
            duration = media_duration_seconds(media_obj)
            direct_ready = progressive_source_compatible(media_obj)
            adaptive_ready = bool(manifest_url)
            required_failure = playback_prep_service.required_preparation_error(media_obj.id, fingerprint, media_obj)
            preparation_error = (
                PlaybackPreparationFailure(code=required_failure["code"], message=required_failure["message"])
                if required_failure and not direct_ready
                else None
            )
            if direct_ready or adaptive_ready:
                preparation_state = "ready"
            elif preparation_error:
                preparation_state = "error"
            else:
                preparation_state = "preparing"
            progress_payload = playback_prep_service.preparation_progress(media_obj.id, fingerprint, media_obj)
            progress = PlaybackPreparationProgress(**progress_payload)
            baseline = playback_prep_service.baseline_video(media_obj)
            adaptive_seekable = playback_prep_service.rendition_seekable_until(
                media_obj.id,
                fingerprint,
                baseline.name,
            )
            return PlaybackRunResponse(
                stream_base_url=f"/api/playback/streams/{quote(run.id, safe='')}",
                run_id=run.id,
                media_id=media_obj.id,
                movie_id=run.movie_id,
                episode_id=run.episode_id,
                source_fingerprint=fingerprint,
                resume_position=initial_resume_position,
                source_metadata=source_metadata(media_obj),
                tracks=tracks,
                renditions=renditions,
                subtitles=subtitle_contract(media_obj),
                ticket=ticket,
                ticket_expires_at=expires_at,
                manifest_url=manifest_url,
                progressive_url=(
                    f"/api/playback/progressive/{quote(media_obj.id, safe='')}?ticket={encoded_ticket}"
                    if direct_ready else ""
                ),
                next_episode_id=next_episode_id,
                preparation_state=preparation_state,
                preparation_error=preparation_error,
                preparation_progress=(
                    PlaybackPreparationProgress(stage="streamable", ready_segments=0, active_workers=0)
                    if direct_ready
                    else progress
                ),
                seekable_until=duration if direct_ready else adaptive_seekable,
                resume_ready=direct_ready or adaptive_seekable >= initial_resume_position,
                switching_ready=playback_prep_service.switching_ready(media_obj.id, fingerprint, media_obj),
                fully_prepared=playback_prep_service.fully_prepared(media_obj.id, fingerprint, media_obj),
                next_sequence_number=run.sequence_number,
            )

    return await run_cancellation_safe_thread(catalog_response)


async def authorized_run(db: AsyncSession, request: Request, run_id: str, *, allow_abandoned: bool = False) -> PlaybackRun:
    run = await db.get(PlaybackRun, run_id)
    auth_session = await active_auth_session(db, request)
    if not run:
        raise playback_error(status.HTTP_404_NOT_FOUND, "PLAYBACK_RUN_NOT_FOUND", "The playback run does not exist.")
    if run.auth_session_id != auth_session.id:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_RUN_FORBIDDEN", "This playback run belongs to another session.")
    await require_profile_access(db, auth_session, run.profile_id)
    if run.lifecycle_state == "expired" or (run.lifecycle_state == "abandoned" and not allow_abandoned):
        raise playback_error(status.HTTP_410_GONE, "PLAYBACK_RUN_EXPIRED", "The playback run is no longer active.")
    return run


@router.post("/runs", response_model=PlaybackRunResponse)
async def create_playback_run(
    req: PlaybackRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlaybackRunResponse:
    media_ids = (req.movie_id, req.episode_id) if req.episode_id else (req.movie_id,)
    async with media_ownership(*media_ids):
        require_playable_media(*media_ids)
        return await _create_playback_run_locked(req, request, db, user)


async def _create_playback_run_locked(req: PlaybackRunRequest, request: Request, db: AsyncSession, user: User) -> PlaybackRunResponse:
    run_started_at = time.monotonic()
    auth_session = await active_auth_session(db, request)
    await require_profile_access(db, auth_session, req.profile_id)
    movie, media_obj = await resolve_run_media(db, req.movie_id, req.episode_id)
    preview_task: Optional[DownloadTask] = None
    source: Optional[ResolvedMediaSource] = None
    try:
        source = await require_available_source(media_obj)
        await ensure_source_metadata(db, media_obj, source)
        await synchronize_source_fingerprint(db, media_obj, source)
        stale_preview_task_id = str(getattr(media_obj, "preview_task_id", None) or "")
        if stale_preview_task_id:
            await clear_preview_task_reference(
                db,
                media_obj,
                stale_preview_task_id,
                "completed catalog source is available",
            )
    except HTTPException as source_error:
        detail = source_error.detail if isinstance(source_error.detail, dict) else {}
        error_code = str(detail.get("code") or "")
        if error_code not in {"MEDIA_SOURCE_MISSING", "MEDIA_PROBE_FAILED", "INVALID_MEDIA_PATH"}:
            raise
        preview_task = await active_preview_task(db, movie, media_obj)
        if not preview_task:
            preview_task = await queue_media_repair(db, movie, media_obj, error_code)
            if not preview_task:
                raise
        source = None
    if preview_task:
        source_kind = "ingest_preview"
        source_fingerprint = ingest_preview_service.fingerprint(preview_task.id)
    else:
        source_kind = "catalog"
        source_fingerprint = str(media_obj.source_fingerprint or source.fingerprint)

    filters = [PlaybackSession.profile_id == req.profile_id, PlaybackSession.movie_id == req.movie_id]
    filters.append(PlaybackSession.episode_id == req.episode_id if req.episode_id else PlaybackSession.episode_id.is_(None))
    session_rec = (await db.exec(select(PlaybackSession).where(*filters))).first()
    position = resume_position(session_rec, media_duration_seconds(media_obj), is_movie=not req.episode_id and movie.type == "movie")
    now = time.time()
    run = PlaybackRun(
        id=str(uuid.uuid4()),
        profile_id=req.profile_id,
        movie_id=movie.id,
        episode_id=req.episode_id,
        auth_session_id=auth_session.id,
        source_kind=source_kind,
        source_fingerprint=source_fingerprint,
        source_task_id=preview_task.id if preview_task else None,
        sequence_number=1,
        lifecycle_state="active",
        created_at=now,
        updated_at=now,
        last_seen_at=now,
        last_progress_at=now,
        total_seconds_played=0,
    )
    db.add(run)
    await db.commit()
    response = await build_run_response(db, request, user, run, media_obj, initial_resume_position=position)
    logger.info(
        f"[Playback Run] Created {run.id} for {media_obj.id} in {time.monotonic() - run_started_at:.3f}s; "
        f"delivery=direct+ready-hls resume={position:.1f}s."
    )
    return response


@router.get("/runs/{run_id}", response_model=PlaybackRunResponse)
async def get_playback_run(
    run_id: str,
    request: Request,
    retry: bool = Query(False),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlaybackRunResponse:
    media_ids = await run_media_ownership_ids(db, run_id)
    async with media_ownership(*media_ids):
        require_playable_media(*media_ids)
        return await _get_playback_run_locked(run_id, request, retry, db, user)


async def _get_playback_run_locked(run_id: str, request: Request, retry: bool, db: AsyncSession, user: User) -> PlaybackRunResponse:
    run = await authorized_run(db, request, run_id)
    movie, media_obj = await resolve_run_media(db, run.movie_id, run.episode_id)
    if not retry and run.source_kind != "ingest_preview" and str(media_obj.video_url or "").startswith("/media/"):
        source = await require_available_source(media_obj)
        observed_fingerprint = playback_source_fingerprint(source, media_obj.audio_metadata or [])
        analysis = stored_analysis(media_obj.analysis_metadata_str)
        if (observed_fingerprint != str(media_obj.source_fingerprint or "")
                or not current_duration_analysis(analysis)
                or analysis.get("video_fingerprint") != source.video_fingerprint
                or analysis.get("source_fingerprint") != media_obj.source_fingerprint):
            await ensure_source_metadata(db, media_obj, source)
            await synchronize_source_fingerprint(db, media_obj, source)
    if retry and run.source_kind != "ingest_preview":
        if run.source_kind != "ingest_preview":
            try:
                source = await require_available_source(media_obj)
                await ensure_source_metadata(db, media_obj, source)
                await synchronize_source_fingerprint(db, media_obj, source)
            except HTTPException as source_error:
                detail = source_error.detail if isinstance(source_error.detail, dict) else {}
                error_code = str(detail.get("code") or "")
                if error_code not in {"MEDIA_SOURCE_MISSING", "MEDIA_PROBE_FAILED", "INVALID_MEDIA_PATH"}:
                    raise
                repair_task = await queue_media_repair(db, movie, media_obj, error_code)
                if not repair_task:
                    raise
                run.source_kind = "ingest_preview"
                run.source_task_id = repair_task.id
                run.source_fingerprint = ingest_preview_service.fingerprint(repair_task.id)
                run.updated_at = time.time()
                async with runtime_work("playback.catalog-repair", [EffectClass.DB_WRITE]):
                    db.add(run)
                    await db.commit()
    position = 0 if run.source_kind == "ingest_preview" else await run_resume_position(db, run, media_obj)
    return await build_run_response(db, request, user, run, media_obj, initial_resume_position=position)


@router.post("/runs/{run_id}/quality")
async def select_playback_quality(
    run_id: str,
    req: PlaybackQualityRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    media_ids = await run_media_ownership_ids(db, run_id)
    async with media_ownership(*media_ids):
        require_playable_media(*media_ids)
        return await _select_playback_quality_locked(run_id, req, request, db, user)


async def _select_playback_quality_locked(run_id: str, req: PlaybackQualityRequest, request: Request, db: AsyncSession, user: User) -> dict[str, str]:
    del user
    run = await authorized_run(db, request, run_id)
    if run.source_kind == "ingest_preview":
        raise playback_error(
            status.HTTP_409_CONFLICT,
            "QUALITY_SELECTION_UNAVAILABLE",
            "Quality selection is unavailable while the media source is still downloading.",
        )
    _, media_obj = await resolve_run_media(db, run.movie_id, run.episode_id)
    source = await require_available_source(media_obj)
    await ensure_source_metadata(db, media_obj, source)
    await synchronize_source_fingerprint(db, media_obj, source)
    if req.rendition_id not in {item.name for item in playback_prep_service.video_renditions(media_obj)}:
        raise playback_error(
            status.HTTP_404_NOT_FOUND,
            "RENDITION_NOT_FOUND",
            "The requested playback quality does not exist for this media source.",
        )
    fingerprint = str(media_obj.source_fingerprint or source.fingerprint)
    rendition_status = playback_prep_service.rendition_status(
        media_obj.id,
        fingerprint,
        req.rendition_id,
    )
    if rendition_status not in {"streamable", "ready"}:
        raise playback_error(
            status.HTTP_409_CONFLICT,
            "RENDITION_NOT_READY",
            "The requested quality is not present in the prepared adaptive stream.",
        )
    return {"status": rendition_status, "renditionId": req.rendition_id}


@router.post("/runs/{run_id}/diagnostics", status_code=status.HTTP_204_NO_CONTENT)
async def record_playback_startup_diagnostic(
    run_id: str,
    req: PlaybackStartupDiagnosticRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Response:
    del user
    run = await authorized_run(db, request, run_id)
    logger.warning(
        "[Playback Startup] "
        f"run={run.id} media={run.episode_id or run.movie_id} transport={req.transport} stage={req.stage} "
        f"error_type={req.error_type or 'none'} error_detail={req.error_detail or 'none'} "
        f"http_status={req.http_status or 0} ready_state={req.ready_state} network_state={req.network_state} "
        f"current_time={req.current_time:.3f} buffered_until={req.buffered_until:.3f} elapsed_ms={req.elapsed_ms}"
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/runs/{run_id}/progress")
async def update_playback_progress(
    run_id: str,
    req: PlaybackProgressRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    identity = current_auth_session(request).id
    await db.rollback()
    # Retain only the lookup anchor; authorization is read again under the writer.
    request.state.auth_session = AuthSession(id=identity, user_id=0)
    async with run_write_lock(run_id):
        return await _update_playback_progress_locked(run_id, req, request, db, user)


async def _update_playback_progress_locked(
    run_id: str,
    req: PlaybackProgressRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    del user
    run = await authorized_run(db, request, run_id)
    if run.lifecycle_state == "finished":
        return {"status": "sticky_finished", "nextSequenceNumber": run.sequence_number}
    if req.sequence_number != run.sequence_number:
        raise playback_error(
            status.HTTP_409_CONFLICT,
            "PLAYBACK_SEQUENCE_MISMATCH",
            f"Expected progress sequence {run.sequence_number}.",
        )

    _, media_obj = await resolve_run_media(db, run.movie_id, run.episode_id)
    duration = max(1.0, media_duration_seconds(media_obj) or 3600.0)
    now = time.time()
    elapsed_bound = max(0.0, min(120.0, now - run.last_progress_at + 2.0))
    requested_watched = (
        max(0, req.total_seconds_played - run.total_seconds_played)
        if req.total_seconds_played is not None else float(req.duration_watched)
    )
    if req.total_seconds_played is not None:
        # Delayed snapshots may arrive together. Bound the cumulative lifetime,
        # rather than dropping the older snapshot's remaining watch time.
        elapsed_bound = max(0.0, now - run.created_at + 2.0 - run.total_seconds_played)
    accepted_watched = min(float(requested_watched), 120.0, elapsed_bound)
    timestamp = max(0.0, min(float(req.timestamp), duration))
    analysis = stored_analysis(media_obj.analysis_metadata_str)
    credits_exit = (
        req.event == "exit"
        and run.source_kind == "catalog"
        and not run.episode_id
        and media_obj.type == "movie"
        and bool(run.source_fingerprint)
        and run.source_fingerprint == media_obj.source_fingerprint
        and current_duration_analysis(analysis)
        and analysis.get("source_fingerprint") == run.source_fingerprint
        and is_credits_exit_position(media_obj.skip_markers, timestamp, media_obj.probed_duration)
    )
    finished = bool(req.is_finished or req.event == "ended" or timestamp / duration >= 0.995 or credits_exit)

    run.sequence_number += 1
    run.total_seconds_played += int(accepted_watched)
    run.last_seen_at = now
    run.last_progress_at = now
    run.updated_at = now
    if finished:
        run.lifecycle_state = "finished"
    db.add(run)

    filters = [PlaybackSession.profile_id == run.profile_id, PlaybackSession.movie_id == run.movie_id]
    filters.append(PlaybackSession.episode_id == run.episode_id if run.episode_id else PlaybackSession.episode_id.is_(None))
    session_rec = (await db.exec(select(PlaybackSession).where(*filters))).first()
    updated_at = datetime.now(timezone.utc).isoformat()
    completion_rate = min(timestamp / duration, 1.0)
    if session_rec is None:
        session_rec = PlaybackSession(
            profile_id=run.profile_id,
            movie_id=run.movie_id,
            episode_id=run.episode_id,
            timestamp=int(timestamp),
            duration_watched=int(accepted_watched),
            completion_rate=completion_rate,
            updated_at=updated_at,
            is_finished=finished,
        )
        db.add(session_rec)
    elif not session_rec.is_finished:
        session_rec.timestamp = int(timestamp)
        session_rec.duration_watched = int(session_rec.duration_watched or 0) + int(accepted_watched)
        session_rec.completion_rate = completion_rate
        session_rec.updated_at = updated_at
        session_rec.is_finished = finished
        db.add(session_rec)
    await db.commit()

    viewing_attempt_id = await record_playback_progress(
        profile_id=run.profile_id,
        movie_id=run.movie_id,
        episode_id=run.episode_id,
        position=int(timestamp),
        duration_watched=int(accepted_watched),
        completion_rate=completion_rate,
        is_finished=finished,
    )
    return {
        "status": "finished" if finished else "ok",
        "viewingSessionId": viewing_attempt_id,
        "acceptedSeconds": accepted_watched,
        "nextSequenceNumber": run.sequence_number,
    }


@router.post("/runs/{run_id}/close")
async def close_playback_run(
    run_id: str,
    req: PlaybackCloseRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    identity = current_auth_session(request).id
    await db.rollback()
    # Retain only the lookup anchor; authorization is read again under the writer.
    request.state.auth_session = AuthSession(id=identity, user_id=0)
    async with run_write_lock(run_id):
        return await _close_playback_run_locked(run_id, req, request, db, user)


async def _close_playback_run_locked(
    run_id: str,
    req: PlaybackCloseRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    run = await authorized_run(db, request, run_id, allow_abandoned=True)
    if run.lifecycle_state == "abandoned":
        return {"status": "abandoned", "nextSequenceNumber": run.sequence_number}
    if run.lifecycle_state == "finished":
        return {"status": "sticky_finished", "nextSequenceNumber": run.sequence_number}
    if req.discard_progress:
        run.lifecycle_state = "abandoned"
        run.last_seen_at = time.time()
        run.updated_at = run.last_seen_at
        db.add(run)
        await db.commit()
        return {"status": "abandoned", "nextSequenceNumber": run.sequence_number}
    close_request = req.model_copy(update={"sequence_number": run.sequence_number, "event": "exit"})
    response = await _update_playback_progress_locked(run_id, close_request, request, db, user)
    refreshed_run = await db.get(PlaybackRun, run_id)
    if refreshed_run and refreshed_run.lifecycle_state != "finished":
        refreshed_run.lifecycle_state = "abandoned"
        refreshed_run.last_seen_at = time.time()
        refreshed_run.updated_at = refreshed_run.last_seen_at
        db.add(refreshed_run)
        await db.commit()
        response["status"] = "abandoned"
    return response


@router.post("/runs/{run_id}/start-over")
async def start_over_playback_run(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    identity = current_auth_session(request).id
    await db.rollback()
    # Retain only the lookup anchor; authorization is read again under the writer.
    request.state.auth_session = AuthSession(id=identity, user_id=0)
    async with run_write_lock(run_id):
        return await _start_over_playback_run_locked(run_id, request, db, user)


async def _start_over_playback_run_locked(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    del user
    run = await authorized_run(db, request, run_id)
    filters = [PlaybackSession.profile_id == run.profile_id, PlaybackSession.movie_id == run.movie_id]
    filters.append(PlaybackSession.episode_id == run.episode_id if run.episode_id else PlaybackSession.episode_id.is_(None))
    session_rec = (await db.exec(select(PlaybackSession).where(*filters))).first()
    if session_rec:
        session_rec.timestamp = 0
        session_rec.duration_watched = 0
        session_rec.is_finished = False
        session_rec.completion_rate = 0.0
        session_rec.updated_at = datetime.now(timezone.utc).isoformat()
        db.add(session_rec)
    run.lifecycle_state = "active"
    run.total_seconds_played = 0
    run.updated_at = time.time()
    run.last_seen_at = time.time()
    db.add(run)
    await db.commit()
    return {"status": "ok", "nextSequenceNumber": run.sequence_number}


def protected_hls_url(media_id: str, relative_path: str, ticket: str) -> str:
    run_id = _browser_stream_run.get()
    if run_id is not None:
        return f"/api/playback/streams/{quote(run_id, safe='')}/hls/{quote(relative_path, safe='/')}"
    return f"/api/playback/hls/{quote(media_id, safe='')}/{quote(relative_path, safe='/')}?ticket={quote(ticket, safe='')}"


def protected_preview_url(media_id: str, relative_path: str, ticket: str) -> str:
    run_id = _browser_stream_run.get()
    if run_id is not None:
        return f"/api/playback/streams/{quote(run_id, safe='')}/preview/{quote(relative_path, safe='/')}"
    return f"/api/playback/preview/{quote(media_id, safe='')}/{quote(relative_path, safe='/')}?ticket={quote(ticket, safe='')}"


def rewrite_playlist(
    content: str,
    media_id: str,
    ticket: str,
    base_path: PurePosixPath,
    url_builder,
) -> str:
    def resolve_reference(reference: str) -> str:
        reference_path = PurePosixPath(reference)
        joined = base_path / reference_path
        normalized_parts: list[str] = []
        for part in joined.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not normalized_parts:
                    raise playback_error(status.HTTP_403_FORBIDDEN, "HLS_PATH_INVALID", "The HLS playlist contains an unsafe path.")
                normalized_parts.pop()
            else:
                normalized_parts.append(part)
        return url_builder(media_id, "/".join(normalized_parts), ticket)

    rewritten: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            rewritten.append(resolve_reference(stripped))
            continue
        if 'URI="' in line:
            line = re.sub(r'URI="([^"]+)"', lambda match: f'URI="{resolve_reference(match.group(1))}"', line)
        rewritten.append(line)
    return "\n".join(rewritten) + "\n"


def rewrite_hls_playlist(content: str, media_id: str, ticket: str, base_path: PurePosixPath) -> str:
    return rewrite_playlist(content, media_id, ticket, base_path, protected_hls_url)


def rewrite_ingest_preview_playlist(content: str, media_id: str, ticket: str, base_path: PurePosixPath) -> str:
    return rewrite_playlist(content, media_id, ticket, base_path, protected_preview_url)


def safe_hls_file(cache_root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.suffix.lower() not in {".m3u8", ".m4s", ".mp4"} or any(part in {"", ".", ".."} for part in relative.parts):
        raise playback_error(status.HTTP_403_FORBIDDEN, "HLS_ASSET_TYPE_FORBIDDEN", "Only HLS playlists and media fragments may be requested.")
    candidate = (cache_root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(cache_root.resolve())
    except ValueError as exc:
        raise playback_error(status.HTTP_403_FORBIDDEN, "HLS_PATH_INVALID", "The HLS path is outside the playback cache.") from exc
    return candidate


async def preview_run_from_ticket(
    media_id: str,
    ticket: str,
    db: AsyncSession,
) -> tuple[PlaybackRun, str]:
    _, run, _ = await validate_playback_ticket(ticket, media_id, db)
    if run.source_kind != "ingest_preview" or not run.source_task_id:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PREVIEW_TICKET_REQUIRED", "This playback ticket does not permit an ingestion preview.")
    try:
        ingest_preview_service.task_path(run.source_task_id)
    except IngestPreviewError as exc:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PREVIEW_SOURCE_INVALID", "The ingestion preview source is invalid.") from exc
    return run, run.source_task_id


@router.get("/preview/{media_id}/playlist.m3u8")
async def serve_ingest_preview_manifest(
    media_id: str,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    _, task_id = await preview_run_from_ticket(media_id, ticket, db)
    preview_status = ingest_preview_service.status(task_id)
    if preview_status["phase"] == "error":
        raise playback_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            preview_status["error_code"] or "PREVIEW_FAILED",
            preview_status["error_message"] or "The play-while-downloading stream failed.",
        )
    playlist = ingest_preview_service.playlist_path(task_id)
    if preview_status["phase"] != "ready" or not playlist.is_file():
        raise playback_error(status.HTTP_425_TOO_EARLY, "PREVIEW_PREPARING", "The play-while-downloading buffer is still preparing.")
    lease = playback_response_lease("playback.preview-manifest", disposable_cache=True)
    try:
        ingest_preview_service.touch(task_id)
        content = rewrite_ingest_preview_playlist(
            playlist.read_text(encoding="utf-8"),
            media_id,
            ticket,
            PurePosixPath(),
        )
        return LeaseBoundPlaybackResponse(
            PlainTextResponse(content, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"}),
            lease,
        )
    except Exception:
        lease.release()
        raise


@router.get("/preview/{media_id}/{path:path}")
async def serve_ingest_preview_asset(
    media_id: str,
    path: str,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
):
    _, task_id = await preview_run_from_ticket(media_id, ticket, db)
    if PurePosixPath(path).suffix.lower() not in {".m4s", ".mp4"}:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PREVIEW_ASSET_TYPE_FORBIDDEN", "Only ingestion preview media fragments may be requested.")
    try:
        target = ingest_preview_service.safe_asset(task_id, path)
    except IngestPreviewError as exc:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PREVIEW_PATH_INVALID", "The ingestion preview path is invalid.") from exc
    if not target.is_file():
        preview_status = ingest_preview_service.status(task_id)
        if preview_status["phase"] == "error":
            raise playback_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                preview_status["error_code"] or "PREVIEW_FAILED",
                preview_status["error_message"] or "The play-while-downloading stream failed.",
            )
        raise playback_error(status.HTTP_404_NOT_FOUND, "PREVIEW_ASSET_NOT_READY", "The requested ingestion preview segment is not ready.")
    lease = playback_response_lease("playback.preview-segment", disposable_cache=True)
    try:
        ingest_preview_service.touch(task_id)
        media_type = "video/iso.segment" if target.suffix.lower() == ".m4s" else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return LeaseBoundPlaybackResponse(
            FileResponse(target, media_type=media_type, headers={"Cache-Control": "private, max-age=900"}),
            lease,
        )
    except Exception:
        lease.release()
        raise


@router.get("/manifest/{media_id}")
async def serve_master_manifest(
    media_id: str,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
    rendition_id: Optional[str] = Query(None, max_length=96, pattern=r"^[a-zA-Z0-9_-]+$"),
) -> Response:
    payload, _, media_obj = await validate_playback_ticket(ticket, media_id, db)
    cache_root = playback_prep_service.cache_path(media_id, str(payload["fingerprint"]))
    master_path = safe_hls_file(cache_root, "master.m3u8")
    if not master_path.is_file():
        failure = playback_prep_service.required_preparation_error(media_id, str(payload["fingerprint"]), media_obj)
        if failure:
            raise playback_error(status.HTTP_503_SERVICE_UNAVAILABLE, failure["code"], failure["message"])
        raise playback_error(status.HTTP_425_TOO_EARLY, "PLAYBACK_PREPARING", "The adaptive stream is still preparing.")
    lease = playback_response_lease("playback.hls-manifest", disposable_cache=True)
    try:
        playback_prep_service.touch(media_id, str(payload["fingerprint"]))
        raw_content = master_path.read_text(encoding="utf-8")
        if isinstance(rendition_id, str):
            allowed = {item.id for item in rendition_contract(media_obj, media_id, str(payload["fingerprint"]))}
            if rendition_id not in allowed:
                raise playback_error(404, "RENDITION_NOT_READY", "The selected quality is not available.")
            lines = raw_content.splitlines()
            selected_lines = []
            index = 0
            while index < len(lines):
                line = lines[index]
                if line.startswith("#EXT-X-STREAM-INF:"):
                    uri = lines[index + 1] if index + 1 < len(lines) else ""
                    if uri == f"{rendition_id}/playlist.m3u8":
                        selected_lines.extend([line, uri])
                    index += 2
                else:
                    selected_lines.append(line)
                    index += 1
            raw_content = "\n".join(selected_lines) + "\n"
        content = rewrite_hls_playlist(raw_content, media_id, ticket, PurePosixPath())
        return LeaseBoundPlaybackResponse(
            PlainTextResponse(content, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"}),
            lease,
        )
    except Exception:
        lease.release()
        raise


@router.get("/hls/{media_id}/{path:path}")
async def serve_hls_asset(
    media_id: str,
    path: str,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
):
    payload, _, _ = await validate_playback_ticket(ticket, media_id, db)
    cache_root = playback_prep_service.cache_path(media_id, str(payload["fingerprint"]))
    target = safe_hls_file(cache_root, path)
    if not target.is_file():
        raise playback_error(status.HTTP_404_NOT_FOUND, "HLS_ASSET_NOT_READY", "The requested playback rendition is not ready.")
    lease = playback_response_lease("playback.hls-asset", disposable_cache=True)
    try:
        playback_prep_service.touch(media_id, str(payload["fingerprint"]))
        if target.suffix.lower() == ".m3u8":
            content = rewrite_hls_playlist(target.read_text(encoding="utf-8"), media_id, ticket, PurePosixPath(path).parent)
            response = PlainTextResponse(content, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
        else:
            media_type = "video/iso.segment" if target.suffix.lower() == ".m4s" else "video/mp4"
            response = FileResponse(target, media_type=media_type, headers={"Cache-Control": "private, max-age=900"})
        return LeaseBoundPlaybackResponse(response, lease)
    except Exception:
        lease.release()
        raise


def parse_byte_range(range_header: Optional[str], file_size: int) -> tuple[int, int, bool]:
    if file_size <= 0:
        raise playback_error(status.HTTP_502_BAD_GATEWAY, "MEDIA_SIZE_UNKNOWN", "The media source reported an invalid size.")
    if not range_header:
        return 0, file_size - 1, False
    if "," in range_header:
        raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "Multiple byte ranges are not supported."}, headers={"Content-Range": f"bytes */{file_size}"})
    match = RANGE_RE.fullmatch(range_header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "The byte range is invalid."}, headers={"Content-Range": f"bytes */{file_size}"})
    if not match.group(1):
        suffix = int(match.group(2))
        if suffix <= 0:
            raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "The suffix range is invalid."}, headers={"Content-Range": f"bytes */{file_size}"})
        start = max(0, file_size - suffix)
        return start, file_size - 1, True
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1
    if start >= file_size or end < start:
        raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "The byte range is outside the media file."}, headers={"Content-Range": f"bytes */{file_size}"})
    return start, min(end, file_size - 1), True


async def local_file_chunks(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
    async with aiofiles.open(path, "rb") as handle:
        await handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = await handle.read(min(256 * 1024, remaining))
            if not chunk:
                raise RuntimeError("Local media ended before the declared content length")
            remaining -= len(chunk)
            yield chunk


async def cloud_file_size(remote_path: str) -> int:
    result = await rclone_service.run("lsjson", remote_path, "--stat", timeout=30)
    if not result.ok:
        raise playback_error(status.HTTP_502_BAD_GATEWAY, result.error_code or "CLOUD_SOURCE_FAILED", "Google Drive did not return the media file metadata.")
    try:
        payload = json.loads(result.stdout)
        if isinstance(payload, list):
            payload = payload[0]
        return int(payload.get("Size", 0))
    except (ValueError, IndexError, KeyError, json.JSONDecodeError, AttributeError) as exc:
        raise playback_error(status.HTTP_502_BAD_GATEWAY, "CLOUD_SIZE_INVALID", "Google Drive returned invalid media metadata.") from exc


async def open_cloud_chunks(remote_path: str, start: int, length: int) -> AsyncIterator[bytes]:
    try:
        process, stream = await rclone_service.open_stream("cat", remote_path, "--offset", str(start), "--count", str(length))
    except (FileNotFoundError, OSError) as exc:
        raise playback_error(status.HTTP_503_SERVICE_UNAVAILABLE, "RCLONE_UNAVAILABLE", "Google Drive streaming is unavailable.") from exc
    iterator = stream.__aiter__()
    try:
        first = await iterator.__anext__()
    except StopAsyncIteration as exc:
        await process.wait()
        raise playback_error(status.HTTP_502_BAD_GATEWAY, "EMPTY_CLOUD_STREAM", "Google Drive returned no media bytes.") from exc

    async def chunks() -> AsyncIterator[bytes]:
        delivered = 0
        try:
            delivered += len(first)
            yield first
            async for chunk in iterator:
                delivered += len(chunk)
                yield chunk
            if delivered != length:
                logger.error(f"[Playback] Cloud stream ended early ({delivered}/{length} bytes).")
                raise RuntimeError("Google Drive ended the protected media stream before the declared content length")
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    return chunks()


async def seekable_source_response(reader: Any, request: Request):
    try:
        source_stat = await reader.stat()
    except PlaybackSourceFailure as exc:
        raise playback_error(status.HTTP_502_BAD_GATEWAY, exc.code, str(exc)) from exc
    start, end, partial = parse_byte_range(request.headers.get("range"), source_stat.size)
    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": source_stat.content_type,
        "Cache-Control": "private, no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{source_stat.size}"
    response_status = 206 if partial else 200
    if request.method == "HEAD":
        return Response(status_code=response_status, headers=headers, media_type=source_stat.content_type)
    coordinator = get_runtime_coordinator()
    lease = coordinator.admit("playback.progressive-stream", [EffectClass.PLAYBACK_STREAM])

    async def coordinated_chunks() -> AsyncIterator[bytes]:
        iterator = reader.open_range(start, length).__aiter__()
        try:
            async for chunk in iterator:
                yield chunk
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await settle(close())

    try:
        return LeaseBoundPlaybackResponse(
            StreamingResponse(
                coordinated_chunks(),
                status_code=response_status,
                media_type=source_stat.content_type,
                headers=headers,
            ),
            lease,
        )
    except BaseException:
        lease.release()
        raise


async def protected_source_response(media_id: str, request: Request, ticket: str, db: AsyncSession, *, source_id: str = "main", require_progressive: bool = False):
    # Ticket routes arrive without DB work. Acquire ownership before taking a
    # SQLite snapshot, so waiting for a publisher never retains a DB transaction.
    lock = media_lock(media_id)
    await lock.acquire()
    reader = None
    handed_off = False
    try:
        require_playable_media(media_id)
        _, _, media_obj = await validate_playback_ticket(ticket, media_id, db)
        source = await require_available_source(media_obj)
        observed = playback_source_fingerprint(source, media_obj.audio_metadata or [])
        if observed != str(media_obj.source_fingerprint or ""):
            raise playback_error(status.HTTP_409_CONFLICT, "PLAYBACK_SOURCE_CHANGED", "The media source changed. Start playback again.")
        if require_progressive and not progressive_source_compatible(media_obj):
            raise playback_error(
                status.HTTP_409_CONFLICT,
                "PROGRESSIVE_SOURCE_INCOMPATIBLE",
                "This source requires a verified adaptive rendition for the selected video timeline.",
            )
        selected_source = source
        if source_id != "main":
            audio = next((item for item in playback_prep_service.audio_renditions(media_obj) if item.name == source_id), None)
            if not audio or audio.source != "external":
                raise playback_error(status.HTTP_404_NOT_FOUND, "AUDIO_SOURCE_NOT_FOUND", "The requested audio source does not exist.")
            external_path = playback_prep_service._external_audio_path(source, audio)
            selected_source = playback_prep_service._external_audio_source(source, audio, external_path)
            if not selected_source:
                raise playback_error(status.HTTP_404_NOT_FOUND, "AUDIO_SOURCE_MISSING", "The selected audio source is unavailable.")
        reader = source_reader(selected_source, loopback_url=settings.PLAYBACK_LOOPBACK_URL)
        if isinstance(reader, LocalPlaybackSource):
            await reader.pin()
            response = await seekable_source_response(reader, request)
            lock.release()
            cleanup = reader.close
        else:
            response = await seekable_source_response(reader, request)

            async def cleanup():
                lock.release()

        handed_off = True
        return MediaBoundPlaybackResponse(response, cleanup)
    finally:
        if not handed_off:
            if isinstance(reader, LocalPlaybackSource):
                await reader.close()
            if lock.locked():
                lock.release()


@router.api_route("/source/{media_id}", methods=["GET", "HEAD"])
async def playback_source_bridge(
    media_id: str,
    request: Request,
    ticket: str = Query(...),
    source_id: str = Query("main"),
    db: AsyncSession = Depends(get_session),
):
    return await protected_source_response(media_id, request, ticket, db, source_id=source_id)


@router.api_route("/progressive/{media_id}", methods=["GET", "HEAD"])
async def progressive_playback(
    media_id: str,
    request: Request,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
):
    return await protected_source_response(media_id, request, ticket, db, require_progressive=True)


@router.get("/subtitles/{media_id}/{track_id}")
async def serve_playback_subtitles(
    media_id: str,
    track_id: str,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
):
    lock = media_lock(media_id)
    await lock.acquire()
    try:
        require_playable_media(media_id)
        return await _serve_playback_subtitles_locked(media_id, track_id, ticket, db)
    finally:
        lock.release()


async def _serve_playback_subtitles_locked(media_id: str, track_id: str, ticket: str, db: AsyncSession):
    _, _, media_obj = await validate_playback_ticket(ticket, media_id, db)
    if not SAFE_SUBTITLE_LANGUAGE_RE.fullmatch(track_id):
        raise playback_error(status.HTTP_400_BAD_REQUEST, "INVALID_SUBTITLE_LANGUAGE", "The subtitle language is invalid.")
    matched_track = next((item for item in media_obj.subtitles or [] if subtitle_track_id(item) == track_id), None)
    if not matched_track:
        raise playback_error(status.HTTP_404_NOT_FOUND, "SUBTITLE_NOT_FOUND", "The requested subtitle track is unavailable.")
    file_name = subtitle_file_name(matched_track)
    if not file_name:
        raise playback_error(status.HTTP_404_NOT_FOUND, "SUBTITLE_NOT_FOUND", "The requested subtitle track is unavailable.")
    source = await require_available_source(media_obj)
    if playback_source_fingerprint(source, media_obj.audio_metadata or []) != str(media_obj.source_fingerprint or ""):
        raise playback_error(status.HTTP_409_CONFLICT, "PLAYBACK_SOURCE_CHANGED", "The media source changed. Start playback again.")
    subtitle_path = source.local_path.parent / file_name
    lease = playback_response_lease("playback.subtitle", disposable_cache=True)
    reader = None
    try:
        if not subtitle_path.is_file() and source.cloud_path:
            remote_subtitle = f"{source.cloud_path.rsplit('/', 1)[0]}/{file_name}"
            cache_path = Path(settings.TEMP_DIR) / "subtitle_cache" / media_id / str(media_obj.source_fingerprint) / f"subtitle_{track_id}.vtt"
            result = await rclone_service.copyto_atomic(remote_subtitle, str(cache_path), timeout=60)
            if result.ok:
                subtitle_path = cache_path
        if not subtitle_path.is_file():
            raise playback_error(status.HTTP_404_NOT_FOUND, "SUBTITLE_NOT_FOUND", "The requested subtitle track is unavailable.")
        reader = LocalPlaybackSource(subtitle_path, file_name)
        await reader.pin()
        file_stat = await reader.stat()
        return MediaBoundPlaybackResponse(
            LeaseBoundPlaybackResponse(
                StreamingResponse(reader.open_range(0, file_stat.size), media_type="text/vtt", headers={"Cache-Control": "private, max-age=900", "Content-Length": str(file_stat.size)}),
                lease,
            ),
            reader.close,
        )
    except BaseException:
        if reader is not None:
            await reader.close()
        lease.release()
        raise


async def browser_stream_access(run_id: str, request: Request, db: AsyncSession, user: User) -> tuple[str, str, str]:
    """Resolve a cookie/session-owned run without retaining a snapshot across source admission.

    URLs contain an opaque run identity, never a reusable authentication credential.
    The existing protected delivery validates a short-lived server-internal ticket
    again after source admission. No ticket expiry or revocation rule is relaxed.
    """
    async with AsyncSession(db.bind, expire_on_commit=False) as lookup:
        run = await authorized_run(lookup, request, run_id)
        session = await active_auth_session(lookup, request)
        fingerprint = str(run.source_fingerprint or "")
        if not fingerprint:
            raise playback_error(409, "PLAYBACK_SOURCE_CHANGED", "Start playback again to authorize the current source.")
        media_id = run.episode_id or run.movie_id
        ticket, _ = issue_playback_ticket(user, session, run.profile_id, run.id, media_id, fingerprint)
        kind = run.source_kind
    # Authentication dependencies may have read using the route's session. Do not
    # carry that read transaction into a bounded or blocked media ownership wait.
    await db.rollback()
    return media_id, ticket, kind


@router.api_route("/streams/{run_id}/{asset:path}", methods=["GET", "HEAD"])
async def serve_browser_stream(
    run_id: str,
    asset: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    media_id, ticket, kind = await browser_stream_access(run_id, request, db, user)
    token = _browser_stream_run.set(run_id)
    try:
        if asset == "progressive" and kind == "catalog":
            return await protected_source_response(media_id, request, ticket, db, require_progressive=True)
        if asset.startswith("source/") and kind == "catalog":
            return await protected_source_response(media_id, request, ticket, db, source_id=asset.removeprefix("source/"))
        if asset == "manifest":
            if kind == "ingest_preview":
                return await serve_ingest_preview_manifest(media_id, ticket, db)
            return await serve_master_manifest(media_id, ticket, db, request.query_params.get("rendition_id"))
        if asset.startswith("hls/") and kind == "catalog":
            return await serve_hls_asset(media_id, asset.removeprefix("hls/"), ticket, db)
        if asset.startswith("preview/") and kind == "ingest_preview":
            path = asset.removeprefix("preview/")
            if path == "playlist.m3u8":
                return await serve_ingest_preview_manifest(media_id, ticket, db)
            return await serve_ingest_preview_asset(media_id, path, ticket, db)
        if asset.startswith("subtitles/") and kind == "catalog":
            return await serve_playback_subtitles(media_id, asset.removeprefix("subtitles/"), ticket, db)
        raise playback_error(404, "PLAYBACK_ASSET_NOT_FOUND", "The requested playback asset does not exist.")
    finally:
        _browser_stream_run.reset(token)
