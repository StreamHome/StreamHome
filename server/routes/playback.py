from __future__ import annotations

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
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import APIModel, AuthSession, Episode, Movie, PlaybackRun, PlaybackSession, Profile, User
from routes.auth import get_current_user
from services.logger import logger
from services.media_source import MediaSourceError, ResolvedMediaSource, resolve_media_source
from services.media_probe import probe_completed_media
from services.playback_prep import AudioRendition, PlaybackPrepService, VideoRendition, playback_prep_service
from services.rclone import rclone_service
from services.recommendation import record_playback_progress


router = APIRouter(prefix="/api/playback", tags=["Playback"])
PLAYBACK_TICKET_MINUTES = 15
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
SAFE_SUBTITLE_LANGUAGE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


class PlaybackRunRequest(APIModel):
    movie_id: str
    profile_id: str
    episode_id: Optional[str] = None


class PlaybackProgressRequest(APIModel):
    timestamp: float = Field(ge=0)
    duration_watched: float = Field(default=0, ge=0, le=120)
    is_finished: bool = False
    sequence_number: int = Field(ge=1)
    event: Literal["heartbeat", "pause", "seek", "visibility", "exit", "ended"] = "heartbeat"


class PlaybackSourceMetadata(APIModel):
    duration: float
    container: str
    codec: str
    width: int
    height: int
    frame_rate: float


class PlaybackTrack(APIModel):
    id: str
    label: str
    language: str
    channels: int
    default: bool
    ready: bool


class PlaybackRendition(APIModel):
    id: str
    label: str
    height: int
    width: int
    original: bool
    ready: bool


class PlaybackPreparationFailure(APIModel):
    code: str
    message: str


class PlaybackRunResponse(APIModel):
    run_id: str
    media_id: str
    movie_id: str
    episode_id: Optional[str] = None
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
    if not media_obj or media_obj.source_fingerprint != payload["fingerprint"]:
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


async def synchronize_source_fingerprint(db: AsyncSession, media_obj: Any, source: ResolvedMediaSource) -> None:
    if media_obj.source_fingerprint == source.fingerprint:
        return
    if media_obj.source_fingerprint:
        playback_prep_service.cancel_media(media_obj.id, media_obj.source_fingerprint)
    media_obj.source_fingerprint = source.fingerprint
    db.add(media_obj)
    await db.commit()


async def ensure_source_metadata(db: AsyncSession, media_obj: Any, source: ResolvedMediaSource) -> None:
    if not source.local_exists:
        return
    metadata_missing = not media_obj.probed_duration or not media_obj.codec or not media_obj.width or not media_obj.height
    source_replaced = media_obj.source_fingerprint != source.fingerprint
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
        if not episode.video_url:
            continue
        try:
            if (await resolve_media_source(episode.video_url)).available:
                return episode.id
        except MediaSourceError:
            continue
    return None


def resume_position(session_rec: Optional[PlaybackSession], duration: float) -> float:
    if not session_rec or session_rec.is_finished or duration <= 0:
        return 0
    position = max(0.0, float(session_rec.timestamp))
    return position if position >= 30 and position / duration < 0.95 else 0


async def run_resume_position(db: AsyncSession, run: PlaybackRun, media_obj: Any) -> float:
    filters = [PlaybackSession.profile_id == run.profile_id, PlaybackSession.movie_id == run.movie_id]
    filters.append(PlaybackSession.episode_id == run.episode_id if run.episode_id else PlaybackSession.episode_id.is_(None))
    session_rec = (await db.exec(select(PlaybackSession).where(*filters))).first()
    return resume_position(session_rec, float(media_obj.probed_duration or 0))


def source_metadata(media_obj: Any) -> PlaybackSourceMetadata:
    return PlaybackSourceMetadata(
        duration=max(0.0, float(media_obj.probed_duration or 0)),
        container=str(media_obj.container or ""),
        codec=str(media_obj.codec or ""),
        width=max(0, int(media_obj.width or 0)),
        height=max(0, int(media_obj.height or 0)),
        frame_rate=max(0.0, float(media_obj.frame_rate or 0)),
    )


def track_contract(media_obj: Any, media_id: str, fingerprint: str) -> list[PlaybackTrack]:
    channel_by_index = {int(item.get("index", index)): int(item.get("channels", 2)) for index, item in enumerate(media_obj.audio_metadata or [])}
    return [
        PlaybackTrack(
            id=item.name,
            label=item.label,
            language=item.language,
            channels=channel_by_index.get(item.stream_index, 2),
            default=item.default,
            ready=playback_prep_service.playlist_ready(media_id, fingerprint, item.name),
        )
        for item in playback_prep_service.audio_renditions(media_obj)
    ]


def rendition_contract(media_obj: Any, media_id: str, fingerprint: str) -> list[PlaybackRendition]:
    return [
        PlaybackRendition(
            id=item.name,
            label=item.label,
            height=item.height,
            width=item.width,
            original=item.original,
            ready=playback_prep_service.playlist_ready(media_id, fingerprint, item.name),
        )
        for item in playback_prep_service.video_renditions(media_obj)
    ]


def subtitle_contract(media_obj: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in media_obj.subtitles or []:
        language = str(item.get("language") or "und").lower()
        if language in seen or not SAFE_SUBTITLE_LANGUAGE_RE.fullmatch(language):
            continue
        seen.add(language)
        result.append({"id": language, "language": language, "label": str(item.get("label") or language.upper())})
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
    fingerprint = str(media_obj.source_fingerprint or "")
    state = playback_prep_service.preparation_state(media_obj.id, fingerprint, media_obj)
    failure_payload = playback_prep_service.preparation_error(media_obj.id, fingerprint)
    failure = PlaybackPreparationFailure(**failure_payload) if failure_payload else None
    ticket, expires_at = issue_playback_ticket(user, auth_session, run.profile_id, run.id, media_obj.id, fingerprint)
    encoded_ticket = quote(ticket, safe="")
    next_episode_id = await next_playable_episode(db, run.movie_id, run.episode_id) if run.episode_id else None
    return PlaybackRunResponse(
        run_id=run.id,
        media_id=media_obj.id,
        movie_id=run.movie_id,
        episode_id=run.episode_id,
        resume_position=initial_resume_position,
        source_metadata=source_metadata(media_obj),
        tracks=track_contract(media_obj, media_obj.id, fingerprint),
        renditions=rendition_contract(media_obj, media_obj.id, fingerprint),
        subtitles=subtitle_contract(media_obj),
        ticket=ticket,
        ticket_expires_at=expires_at,
        manifest_url=f"/api/playback/manifest/{quote(media_obj.id, safe='')}?ticket={encoded_ticket}" if state == "ready" else None,
        progressive_url=f"/api/playback/progressive/{quote(media_obj.id, safe='')}?ticket={encoded_ticket}",
        next_episode_id=next_episode_id,
        preparation_state=state,
        preparation_error=failure,
        next_sequence_number=run.sequence_number,
    )


async def authorized_run(db: AsyncSession, request: Request, run_id: str) -> PlaybackRun:
    run = await db.get(PlaybackRun, run_id)
    auth_session = current_auth_session(request)
    if not run:
        raise playback_error(status.HTTP_404_NOT_FOUND, "PLAYBACK_RUN_NOT_FOUND", "The playback run does not exist.")
    if run.auth_session_id != auth_session.id:
        raise playback_error(status.HTTP_403_FORBIDDEN, "PLAYBACK_RUN_FORBIDDEN", "This playback run belongs to another session.")
    if run.lifecycle_state == "expired":
        raise playback_error(status.HTTP_410_GONE, "PLAYBACK_RUN_EXPIRED", "The playback run has expired.")
    return run


@router.post("/runs", response_model=PlaybackRunResponse)
async def create_playback_run(
    req: PlaybackRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlaybackRunResponse:
    profile = await db.get(Profile, req.profile_id)
    if not profile:
        raise playback_error(status.HTTP_404_NOT_FOUND, "PROFILE_NOT_FOUND", "The selected profile does not exist.")
    movie, media_obj = await resolve_run_media(db, req.movie_id, req.episode_id)
    source = await require_available_source(media_obj)
    await ensure_source_metadata(db, media_obj, source)
    await synchronize_source_fingerprint(db, media_obj, source)

    filters = [PlaybackSession.profile_id == req.profile_id, PlaybackSession.movie_id == req.movie_id]
    filters.append(PlaybackSession.episode_id == req.episode_id if req.episode_id else PlaybackSession.episode_id.is_(None))
    session_rec = (await db.exec(select(PlaybackSession).where(*filters))).first()
    position = resume_position(session_rec, float(media_obj.probed_duration or 0))
    auth_session = current_auth_session(request)
    now = time.time()
    run = PlaybackRun(
        id=str(uuid.uuid4()),
        profile_id=req.profile_id,
        movie_id=movie.id,
        episode_id=req.episode_id,
        auth_session_id=auth_session.id,
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
    await playback_prep_service.prepare(media_obj.id, media_obj, source, include_remaining=True)
    return await build_run_response(db, request, user, run, media_obj, initial_resume_position=position)


@router.get("/runs/{run_id}", response_model=PlaybackRunResponse)
async def get_playback_run(
    run_id: str,
    request: Request,
    retry: bool = Query(False),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlaybackRunResponse:
    run = await authorized_run(db, request, run_id)
    _, media_obj = await resolve_run_media(db, run.movie_id, run.episode_id)
    source = await require_available_source(media_obj)
    await ensure_source_metadata(db, media_obj, source)
    await synchronize_source_fingerprint(db, media_obj, source)
    if retry:
        error_path = playback_prep_service.cache_path(media_obj.id, str(media_obj.source_fingerprint)) / "preparation-error.json"
        error_path.unlink(missing_ok=True)
    await playback_prep_service.prepare(media_obj.id, media_obj, source, include_remaining=True)
    run.last_seen_at = time.time()
    run.updated_at = time.time()
    db.add(run)
    await db.commit()
    position = await run_resume_position(db, run, media_obj)
    return await build_run_response(db, request, user, run, media_obj, initial_resume_position=position)


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
    duration = max(1.0, float(media_obj.probed_duration or 0) or 3600.0)
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


def rewrite_hls_playlist(content: str, media_id: str, ticket: str, base_path: PurePosixPath) -> str:
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
        return protected_hls_url(media_id, "/".join(normalized_parts), ticket)

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


def safe_hls_file(cache_root: Path, relative_path: str) -> Path:
    candidate = (cache_root / Path(*PurePosixPath(relative_path).parts)).resolve()
    try:
        candidate.relative_to(cache_root.resolve())
    except ValueError as exc:
        raise playback_error(status.HTTP_403_FORBIDDEN, "HLS_PATH_INVALID", "The HLS path is outside the playback cache.") from exc
    return candidate


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
        failure = playback_prep_service.preparation_error(media_id, str(payload["fingerprint"]))
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
        delivered += len(first)
        yield first
        async for chunk in iterator:
            delivered += len(chunk)
            yield chunk
        if delivered != length:
            logger.error(f"[Playback] Cloud stream ended early ({delivered}/{length} bytes).")
            raise RuntimeError("Google Drive ended the protected media stream before the declared content length")

    return chunks()


@router.get("/progressive/{media_id}")
async def progressive_playback(
    media_id: str,
    request: Request,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
):
    _, _, media_obj = await validate_playback_ticket(ticket, media_id, db)
    source = await require_available_source(media_obj)
    file_size = source.local_path.stat().st_size if source.local_exists else await cloud_file_size(str(source.cloud_path))
    start, end, partial = parse_byte_range(request.headers.get("range"), file_size)
    length = end - start + 1
    content_type = mimetypes.guess_type(source.catalog_path)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": content_type,
        "Cache-Control": "private, no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    chunks = local_file_chunks(source.local_path, start, length) if source.local_exists else await open_cloud_chunks(str(source.cloud_path), start, length)
    return StreamingResponse(chunks, status_code=206 if partial else 200, media_type=content_type, headers=headers)


@router.get("/subtitles/{media_id}/{language}")
async def serve_playback_subtitles(
    media_id: str,
    language: str,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_session),
):
    _, _, media_obj = await validate_playback_ticket(ticket, media_id, db)
    if not SAFE_SUBTITLE_LANGUAGE_RE.fullmatch(language):
        raise playback_error(status.HTTP_400_BAD_REQUEST, "INVALID_SUBTITLE_LANGUAGE", "The subtitle language is invalid.")
    source = await require_available_source(media_obj)
    subtitle_path = source.local_path.parent / f"subtitle_{language}.vtt"
    if not subtitle_path.is_file() and source.cloud_path:
        remote_subtitle = f"{source.cloud_path.rsplit('/', 1)[0]}/subtitle_{language}.vtt"
        cache_path = Path(settings.TEMP_DIR) / "subtitle_cache" / media_id / str(media_obj.source_fingerprint) / f"subtitle_{language}.vtt"
        result = await rclone_service.copyto_atomic(remote_subtitle, str(cache_path), timeout=60)
        if result.ok:
            subtitle_path = cache_path
    if not subtitle_path.is_file():
        raise playback_error(status.HTTP_404_NOT_FOUND, "SUBTITLE_NOT_FOUND", "The requested subtitle track is unavailable.")
    return FileResponse(subtitle_path, media_type="text/vtt", headers={"Cache-Control": "private, max-age=900"})
