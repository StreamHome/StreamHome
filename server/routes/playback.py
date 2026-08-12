from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Literal, Optional
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
from services.media_source import MediaSourceError, ResolvedMediaSource, playback_source_fingerprint, resolve_media_source
from services.media_probe import merge_local_external_audio, probe_cloud_external_audio, probe_completed_media
from services.playback_prep import PlaybackPreparationError, playback_prep_service
from services.playback_source import PlaybackSourceFailure, source_reader
from services.ingest_preview import IngestPreviewError, ingest_preview_service
from services.ingestion_security import UnsafeIngestionSource, validate_headers, validate_url
from services.profile_security import require_profile_access
from services.rclone import rclone_service
from services.recommendation import record_playback_progress


router = APIRouter(prefix="/api/playback", tags=["Playback"])
PLAYBACK_TICKET_MINUTES = 15
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
SAFE_SUBTITLE_LANGUAGE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
SAFE_SUBTITLE_FILE_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,180}\.vtt$", re.IGNORECASE)
ACTIVE_REPAIR_STATUSES = {"PENDING", "DOWNLOADING", "MERGING", "MOVING_CLOUD"}
CLOUD_AUDIO_PROBE_TTL_SECONDS = 300
_cloud_audio_probe_times: dict[str, float] = {}


class PlaybackRunRequest(APIModel):
    movie_id: str
    profile_id: str
    episode_id: Optional[str] = None


class PlaybackQualityRequest(APIModel):
    rendition_id: str = Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9_-]+$")


class PlaybackProgressRequest(APIModel):
    timestamp: float = Field(ge=0)
    duration_watched: float = Field(default=0, ge=0, le=120)
    is_finished: bool = False
    sequence_number: int = Field(ge=1)
    event: Literal["heartbeat", "pause", "seek", "visibility", "exit", "ended"] = "heartbeat"


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


def current_auth_session(request: Request) -> AuthSession:
    auth_session = getattr(request.state, "auth_session", None)
    if not isinstance(auth_session, AuthSession):
        raise playback_error(status.HTTP_401_UNAUTHORIZED, "AUTHENTICATION_REQUIRED", "A valid signed-in session is required.")
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
    auth_session = await db.get(AuthSession, str(payload["jti"]))
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
        source = await resolve_media_source(media_obj.video_url)
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
        normalized_headers = validate_headers(previous.headers)
        for subtitle in previous.subtitles:
            await validate_url(subtitle.get("url"), client_address=client_address)
    except UnsafeIngestionSource as exc:
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
        headers_str=json.dumps(normalized_headers),
        private_source_allowed=previous.private_source_allowed,
        status="PENDING",
        subtitles_str=previous.subtitles_str,
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
    if media_obj.source_fingerprint:
        playback_prep_service.cancel_media(media_obj.id, media_obj.source_fingerprint)
    media_obj.source_fingerprint = fingerprint
    db.add(media_obj)
    await db.commit()
    if source.local_exists:
        metadata_path = source.local_path.parent / ".metadata" / "metadata.json"
        try:
            if metadata_path.is_file():
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                payload["audio_metadata"] = list(media_obj.audio_metadata or [])
                payload["source_fingerprint"] = fingerprint
                languages = [
                    normalize_language_tag(item.get("language"))
                    for item in (media_obj.audio_metadata or [])
                ]
                payload["languages"] = list(dict.fromkeys([*languages, *(payload.get("languages") or [])]))
                temporary = metadata_path.with_suffix(f"{metadata_path.suffix}.tmp")
                temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, metadata_path)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"[Playback Metadata] Could not synchronize {metadata_path}: {exc}")


async def ensure_source_metadata(
    db: AsyncSession,
    media_obj: Any,
    source: ResolvedMediaSource,
    *,
    refresh_sidecars: bool = False,
) -> None:
    if not source.local_exists:
        last_probe = _cloud_audio_probe_times.get(str(media_obj.id), 0)
        if source.cloud_path and (refresh_sidecars or time.monotonic() - last_probe >= CLOUD_AUDIO_PROBE_TTL_SECONDS):
            audio_metadata = await probe_cloud_external_audio(source.cloud_path, list(media_obj.audio_metadata or []))
            _cloud_audio_probe_times[str(media_obj.id)] = time.monotonic()
            if audio_metadata != list(media_obj.audio_metadata or []):
                media_obj.audio_metadata = audio_metadata
                db.add(media_obj)
                await db.commit()
        return
    previous_audio = list(media_obj.audio_metadata or [])
    refreshed_audio = merge_local_external_audio(str(source.local_path), previous_audio)
    audio_changed = refreshed_audio != previous_audio
    if audio_changed:
        media_obj.audio_metadata = refreshed_audio
        db.add(media_obj)
        await db.commit()
    metadata_missing = not media_obj.probed_duration or not media_obj.codec or not media_obj.width or not media_obj.height
    if audio_changed and not metadata_missing:
        return
    source_replaced = media_obj.source_fingerprint != playback_source_fingerprint(source, media_obj.audio_metadata or [])
    if not metadata_missing and not source_replaced:
        return
    probe = await probe_completed_media(str(source.local_path))
    if not probe:
        raise playback_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "MEDIA_PROBE_FAILED", "The media file could not be inspected for playback.")
    for field in ("probed_duration", "container", "codec", "width", "height", "frame_rate"):
        setattr(media_obj, field, probe.get(field))
    media_obj.audio_metadata = probe.get("audio_metadata", [])
    db.add(media_obj)
    await db.commit()


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


def resume_position(session_rec: Optional[PlaybackSession], duration: float) -> float:
    if not session_rec or session_rec.is_finished or duration <= 0:
        return 0
    position = max(0.0, float(session_rec.timestamp))
    return position if position >= 30 and position / duration < 0.95 else 0


async def run_resume_position(db: AsyncSession, run: PlaybackRun, media_obj: Any) -> float:
    filters = [PlaybackSession.profile_id == run.profile_id, PlaybackSession.movie_id == run.movie_id]
    filters.append(PlaybackSession.episode_id == run.episode_id if run.episode_id else PlaybackSession.episode_id.is_(None))
    session_rec = (await db.exec(select(PlaybackSession).where(*filters))).first()
    return resume_position(session_rec, media_duration_seconds(media_obj))


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
        ))
    return result


def rendition_contract(media_obj: Any, media_id: str, fingerprint: str) -> list[PlaybackRendition]:
    result: list[PlaybackRendition] = []
    for item in playback_prep_service.video_renditions(media_obj):
        rendition_status = playback_prep_service.rendition_status(media_id, fingerprint, item.name)
        verified = rendition_status == "ready" and playback_prep_service.rendition_verified(media_id, fingerprint, item.name)
        result.append(PlaybackRendition(
            id=item.name,
            label=item.label,
            height=item.height,
            width=item.width,
            original=item.original,
            ready=verified,
            status=rendition_status,
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
        progressive_url=f"/api/playback/progressive/{quote(media_obj.id, safe='')}?ticket={encoded_ticket}",
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


async def authorized_run(db: AsyncSession, request: Request, run_id: str) -> PlaybackRun:
    run = await db.get(PlaybackRun, run_id)
    auth_session = current_auth_session(request)
    if not run:
        raise playback_error(status.HTTP_404_NOT_FOUND, "PLAYBACK_RUN_NOT_FOUND", "The playback run does not exist.")
    if run.auth_session_id != auth_session.id:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_RUN_FORBIDDEN", "This playback run belongs to another session.")
    if run.lifecycle_state in {"expired", "abandoned"}:
        raise playback_error(status.HTTP_410_GONE, "PLAYBACK_RUN_EXPIRED", "The playback run is no longer active.")
    return run


@router.post("/runs", response_model=PlaybackRunResponse)
async def create_playback_run(
    req: PlaybackRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlaybackRunResponse:
    run_started_at = time.monotonic()
    auth_session = current_auth_session(request)
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
        if not progressive_source_compatible(media_obj) and not playback_prep_service.playback_ready(
            media_obj.id,
            source_fingerprint,
            media_obj,
        ):
            await playback_prep_service.prepare(
                media_obj.id,
                media_obj,
                source,
                include_remaining=False,
                retry_errors=False,
                foreground=True,
            )

    filters = [PlaybackSession.profile_id == req.profile_id, PlaybackSession.movie_id == req.movie_id]
    filters.append(PlaybackSession.episode_id == req.episode_id if req.episode_id else PlaybackSession.episode_id.is_(None))
    session_rec = (await db.exec(select(PlaybackSession).where(*filters))).first()
    position = resume_position(session_rec, media_duration_seconds(media_obj))
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
    run = await authorized_run(db, request, run_id)
    movie, media_obj = await resolve_run_media(db, run.movie_id, run.episode_id)
    if not retry and run.source_kind != "ingest_preview" and str(media_obj.video_url or "").startswith("/media/"):
        source = await require_available_source(media_obj)
        observed_fingerprint = playback_source_fingerprint(source, media_obj.audio_metadata or [])
        if observed_fingerprint != str(media_obj.source_fingerprint or ""):
            await ensure_source_metadata(db, media_obj, source)
            await synchronize_source_fingerprint(db, media_obj, source)
    if retry and run.source_kind != "ingest_preview":
        if run.source_kind != "ingest_preview":
            try:
                source = await require_available_source(media_obj)
                await ensure_source_metadata(db, media_obj, source)
                await synchronize_source_fingerprint(db, media_obj, source)
                await playback_prep_service.prepare(
                    media_obj.id,
                    media_obj,
                    source,
                    include_remaining=False,
                    retry_errors=True,
                    foreground=True,
                )
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
                db.add(run)
                await db.commit()
    position = 0 if run.source_kind == "ingest_preview" else await run_resume_position(db, run, media_obj)
    return await build_run_response(db, request, user, run, media_obj, initial_resume_position=position)


@router.post("/runs/{run_id}/quality")
async def prioritize_playback_quality(
    run_id: str,
    req: PlaybackQualityRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
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
    try:
        rendition_status = await playback_prep_service.prioritize_video_rendition(
            media_obj.id,
            media_obj,
            source,
            req.rendition_id,
        )
    except PlaybackPreparationError as exc:
        raise playback_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            exc.code,
            str(exc),
        ) from exc
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
    accepted_watched = min(float(req.duration_watched), elapsed_bound)
    timestamp = max(0.0, min(float(req.timestamp), duration))
    finished = bool(req.is_finished or req.event == "ended" or timestamp / duration >= 0.995)

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
    req: PlaybackProgressRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    run = await authorized_run(db, request, run_id)
    if run.lifecycle_state == "finished":
        return {"status": "sticky_finished", "nextSequenceNumber": run.sequence_number}
    close_request = req.model_copy(update={"sequence_number": run.sequence_number, "event": "exit"})
    response = await update_playback_progress(run_id, close_request, request, db, user)
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
    return f"/api/playback/hls/{quote(media_id, safe='')}/{quote(relative_path, safe='/')}?ticket={quote(ticket, safe='')}"


def protected_preview_url(media_id: str, relative_path: str, ticket: str) -> str:
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
) -> PlainTextResponse:
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
    ingest_preview_service.touch(task_id)
    content = rewrite_ingest_preview_playlist(
        playlist.read_text(encoding="utf-8"),
        media_id,
        ticket,
        PurePosixPath(),
    )
    return PlainTextResponse(content, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


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
    ingest_preview_service.touch(task_id)
    media_type = "video/iso.segment" if target.suffix.lower() == ".m4s" else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": "private, max-age=900"})


@router.get("/manifest/{media_id}")
async def serve_master_manifest(
    media_id: str,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
) -> PlainTextResponse:
    payload, _, media_obj = await validate_playback_ticket(ticket, media_id, db)
    cache_root = playback_prep_service.cache_path(media_id, str(payload["fingerprint"]))
    master_path = safe_hls_file(cache_root, "master.m3u8")
    if not master_path.is_file():
        failure = playback_prep_service.required_preparation_error(media_id, str(payload["fingerprint"]), media_obj)
        if failure:
            raise playback_error(status.HTTP_503_SERVICE_UNAVAILABLE, failure["code"], failure["message"])
        raise playback_error(status.HTTP_425_TOO_EARLY, "PLAYBACK_PREPARING", "The adaptive stream is still preparing.")
    playback_prep_service.touch(media_id, str(payload["fingerprint"]))
    content = rewrite_hls_playlist(master_path.read_text(encoding="utf-8"), media_id, ticket, PurePosixPath())
    return PlainTextResponse(content, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


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
    playback_prep_service.touch(media_id, str(payload["fingerprint"]))
    if target.suffix.lower() == ".m3u8":
        content = rewrite_hls_playlist(target.read_text(encoding="utf-8"), media_id, ticket, PurePosixPath(path).parent)
        return PlainTextResponse(content, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    media_type = "video/iso.segment" if target.suffix.lower() == ".m4s" else "video/mp4"
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": "private, max-age=900"})


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
    return StreamingResponse(
        reader.open_range(start, length),
        status_code=response_status,
        media_type=source_stat.content_type,
        headers=headers,
    )


@router.api_route("/source/{media_id}", methods=["GET", "HEAD"])
async def playback_source_bridge(
    media_id: str,
    request: Request,
    ticket: str = Query(...),
    source_id: str = Query("main"),
    db: AsyncSession = Depends(get_session),
):
    _, _, media_obj = await validate_playback_ticket(ticket, media_id, db)
    source = await require_available_source(media_obj)
    selected_source = source
    if source_id != "main":
        audio = next(
            (item for item in playback_prep_service.audio_renditions(media_obj) if item.name == source_id),
            None,
        )
        if not audio or audio.source != "external":
            raise playback_error(status.HTTP_404_NOT_FOUND, "AUDIO_SOURCE_NOT_FOUND", "The requested audio source does not exist.")
        external_path = playback_prep_service._external_audio_path(source, audio)
        external_source = playback_prep_service._external_audio_source(source, audio, external_path)
        if not external_source:
            raise playback_error(status.HTTP_404_NOT_FOUND, "AUDIO_SOURCE_MISSING", "The selected audio source is unavailable.")
        selected_source = external_source
    reader = source_reader(selected_source, loopback_url=settings.PLAYBACK_LOOPBACK_URL)
    return await seekable_source_response(reader, request)


@router.api_route("/progressive/{media_id}", methods=["GET", "HEAD"])
async def progressive_playback(
    media_id: str,
    request: Request,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
):
    _, _, media_obj = await validate_playback_ticket(ticket, media_id, db)
    source = await require_available_source(media_obj)
    reader = source_reader(source, loopback_url=settings.PLAYBACK_LOOPBACK_URL)
    return await seekable_source_response(reader, request)


@router.get("/subtitles/{media_id}/{track_id}")
async def serve_playback_subtitles(
    media_id: str,
    track_id: str,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
):
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
    subtitle_path = source.local_path.parent / file_name
    if not subtitle_path.is_file() and source.cloud_path:
        remote_subtitle = f"{source.cloud_path.rsplit('/', 1)[0]}/{file_name}"
        cache_path = Path(settings.TEMP_DIR) / "subtitle_cache" / media_id / str(media_obj.source_fingerprint) / f"subtitle_{track_id}.vtt"
        result = await rclone_service.copyto_atomic(remote_subtitle, str(cache_path), timeout=60)
        if result.ok:
            subtitle_path = cache_path
    if not subtitle_path.is_file():
        raise playback_error(status.HTTP_404_NOT_FOUND, "SUBTITLE_NOT_FOUND", "The requested subtitle track is unavailable.")
    return FileResponse(subtitle_path, media_type="text/vtt", headers={"Cache-Control": "private, max-age=900"})
