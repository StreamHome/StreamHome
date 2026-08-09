import json
import uuid
import time
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Security, status, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import StreamingResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import engine, get_session
from models import DownloadTask, DownloadAddRequest, Movie, Episode, IntegrationCredential
from services.queue import queue_manager
from services.state import ACTIVE_DOWNLOAD_METRICS
from services.tmdb import tmdb_client
from services.vibe_analysis import compute_trope_vectors

from services.logger import logger
from routes.auth import resolve_auth
from services.ingestion_security import UnsafeIngestionSource, validate_headers, validate_url
from services.ingest_preview import ingest_preview_service
from services.integration_auth import (
    authenticate_integration_token,
    integration_token_hash,
    require_integration_scope,
)
from services.media_source import local_catalog_source_exists
from services.request_security import address_is_loopback, client_ip

router = APIRouter()
queue_security = HTTPBearer(auto_error=False)

def is_local_playable_url(url: Optional[str]) -> bool:
    return bool(url and url.startswith("/media/"))


def require_browser_or_integration_scope(scope: str, *, recent_reauthentication: bool = False):
    async def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(queue_security),
        db: AsyncSession = Depends(get_session),
    ):
        if credentials and credentials.credentials:
            digest = integration_token_hash(credentials.credentials)
            result = await db.exec(
                select(IntegrationCredential).where(IntegrationCredential.token_hash == digest)
            )
            integration = result.first()
            if integration or credentials.credentials.startswith("shk_"):
                return await authenticate_integration_token(
                    credentials.credentials,
                    scope,
                    request,
                    db,
                )

        _user, auth_session = await resolve_auth(request, credentials, db)
        if recent_reauthentication and (
            not auth_session.reauthenticated_at
            or time.time() - auth_session.reauthenticated_at > settings.REAUTHENTICATION_MINUTES * 60
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "reauthentication_required",
                    "message": "Confirm your password and authenticator code to continue.",
                },
            )
        return auth_session

    return dependency


# ----------------- Ingestion Endpoint -----------------

@router.post("/api/add-movie", status_code=status.HTTP_201_CREATED)
async def add_movie(
    payload: DownloadAddRequest,
    request: Request,
    credential: IntegrationCredential = Depends(require_integration_scope("ingest")),
):
    """Ingests media stream payload from browser extension and registers it in SQLite."""
    del credential
    try:
        source_client = client_ip(request)
        await validate_url(payload.video_url, client_address=source_client)
        await validate_url(payload.audio_url, client_address=source_client)
        for subtitle in payload.subtitles or []:
            await validate_url(subtitle.url, client_address=source_client)
        headers_dict = validate_headers(payload.headers)
    except UnsafeIngestionSource as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "unsafe_ingestion_source", "message": str(exc)},
        ) from exc

    logger.info(f"[API] Received media ingestion payload for TMDB ID: {payload.tmdb_id}")
    
    # Query TMDB asynchronously to resolve the title
    media_title = f"TMDB {payload.tmdb_id}"
    meta: Dict[str, Any] = {}
    try:
        if payload.media_type == "movie":
            meta = await tmdb_client.fetch_movie_metadata(payload.tmdb_id)
            media_title = meta.get("title", media_title)
        else:
            meta = await tmdb_client.fetch_show_metadata(payload.tmdb_id)
            show_title = meta.get("title", media_title)
            if payload.season is not None and payload.episode is not None:
                media_title = f"{show_title} S{payload.season:02d}E{payload.episode:02d}"
            else:
                media_title = show_title
    except Exception as e:
        logger.error(f"[API] Error fetching initial TMDB metadata: {e}")

    task_id = str(uuid.uuid4())
    
    # Save the task to SQLite database using AsyncSession
    async with AsyncSession(engine) as db:
        subtitles_list = [{"language": s.language, "url": s.url} for s in payload.subtitles] if payload.subtitles else []
        new_task = DownloadTask(
            id=task_id,
            tmdb_id=payload.tmdb_id,
            title=media_title,
            media_type=payload.media_type,
            season=payload.season,
            episode=payload.episode,
            video_url=payload.video_url,
            audio_url=payload.audio_url,
            video_source_type=payload.video_source_type,
            audio_source_type=payload.audio_source_type,
            headers_str=json.dumps(headers_dict),
            private_source_allowed=address_is_loopback(source_client),
            status="PENDING",
            subtitles_str=json.dumps(subtitles_list),
            quality=payload.quality,
            language=payload.language,
            skip_markers_str=json.dumps(payload.skip_markers or {}),
            created_at=datetime.now(timezone.utc).isoformat()
        )
        db.add(new_task)
        
        # Instant Cataloging: Add to Movie/Episode immediately with external metadata/video
        if payload.media_type == "movie":
            movie_id = f"m_{payload.tmdb_id}"
            movie = await db.get(Movie, movie_id)
            if not movie:
                movie = Movie(
                    id=movie_id,
                    tmdb_id=payload.tmdb_id,
                    title=meta.get("title", media_title),
                    description=meta.get("description", ""),
                    thumbnail_url=meta.get("thumbnailUrl", ""),
                    banner_url=meta.get("bannerUrl", ""),
                    video_url=payload.video_url, # External proxy
                    duration=meta.get("duration", "2h"),
                    release_year=meta.get("releaseYear", 2026),
                    rating=meta.get("rating", "PG-13"),
                    director=meta.get("director", "Unknown"),
                    original_language=payload.language or meta.get("originalLanguage", "en"),
                    type="movie",
                    quality=payload.quality or "Source",
                    vote_average=meta.get("vote_average", 7.5),
                    vote_count=meta.get("vote_count", 100),
                    catalog_source="server",
                    availability="processing",
                    preview_task_id=task_id,
                )
                movie.genres = meta.get("genres", [])
                movie.cast = meta.get("cast", [])
                movie.crew = meta.get("crew", [])
                movie.keywords = meta.get("keywords", [])
                movie.collection_name = meta.get("collectionName") or meta.get("collection_name")
                movie.trope_vectors = meta.get("tropeVectors") or compute_trope_vectors(movie.genres, movie.keywords, movie.description)
                movie.skip_markers = payload.skip_markers or {}
                db.add(movie)
            else:
                physical_local_media = local_catalog_source_exists(movie.video_url)
                preserve_local_media = is_local_playable_url(movie.video_url) and (
                    movie.availability == "available" or physical_local_media
                )
                if not preserve_local_media:
                    movie.video_url = payload.video_url
                    movie.availability = "processing"
                    movie.preview_task_id = task_id
                else:
                    movie.preview_task_id = None
                    if physical_local_media:
                        movie.availability = "available"
                movie.tmdb_id = payload.tmdb_id
                movie.catalog_source = "server"
                if payload.skip_markers:
                    movie.skip_markers = payload.skip_markers
                db.add(movie)
        else:
            show_id = f"tv_{payload.tmdb_id}"
            show = await db.get(Movie, show_id)
            if not show:
                show = Movie(
                    id=show_id,
                    tmdb_id=payload.tmdb_id,
                    title=meta.get("title", media_title),
                    description=meta.get("description", ""),
                    thumbnail_url=meta.get("thumbnailUrl", ""),
                    banner_url=meta.get("bannerUrl", ""),
                    video_url="",
                    duration=meta.get("duration", "45m"),
                    release_year=meta.get("releaseYear", 2026),
                    rating=meta.get("rating", "TV-14"),
                    director=meta.get("director", "Various"),
                    original_language=payload.language or meta.get("originalLanguage", "en"),
                    type="series",
                    vote_average=meta.get("vote_average", 7.5),
                    vote_count=meta.get("vote_count", 100),
                    catalog_source="server",
                    availability="processing"
                )
                show.genres = meta.get("genres", [])
                show.cast = meta.get("cast", [])
                show.crew = meta.get("crew", [])
                show.keywords = meta.get("keywords", [])
                show.collection_name = meta.get("collectionName") or meta.get("collection_name")
                show.trope_vectors = meta.get("tropeVectors") or compute_trope_vectors(show.genres, show.keywords, show.description)
                db.add(show)
                
            if payload.season is not None and payload.episode is not None:
                ep_id = f"ep_{payload.tmdb_id}_s{payload.season}_e{payload.episode}"
                ep_entry = await db.get(Episode, ep_id)
                ep_meta = meta.get("episode_detail", {})
                ep_title = ep_meta.get("title", f"Episode {payload.episode}")
                ep_desc = ep_meta.get("description", f"Season {payload.season}, Episode {payload.episode}")
                preserve_local_episode = bool(ep_entry and is_local_playable_url(ep_entry.video_url))
                if not ep_entry:
                    ep_entry = Episode(
                        id=ep_id,
                        movie_id=show_id,
                        episode_number=payload.episode,
                        season_number=payload.season,
                        title=ep_title,
                        description=ep_desc,
                        thumbnail_url=ep_meta.get("thumbnailUrl", ""),
                        video_url=payload.video_url, # External proxy
                        duration=ep_meta.get("duration", "45m"),
                        quality=payload.quality or "Source",
                        preview_task_id=task_id,
                    )
                    ep_entry.skip_markers = payload.skip_markers or {}
                    db.add(ep_entry)
                else:
                    if not preserve_local_episode:
                        ep_entry.video_url = payload.video_url
                        ep_entry.preview_task_id = task_id
                    if payload.skip_markers:
                        ep_entry.skip_markers = payload.skip_markers
                    db.add(ep_entry)
                show.tmdb_id = payload.tmdb_id
                show.catalog_source = "server"
                if show.availability != "available":
                    show.availability = "processing"
                db.add(show)

        await db.commit()
        await db.refresh(new_task)

    return {
        "status": "success",
        "taskId": task_id,
        "title": media_title,
        "message": "Media download task queued successfully."
    }

# ----------------- Real-time SSE progress Stream -----------------

async def download_progress_generator():
    """
    Generates Server-Sent Events combining DB task states and transient active metrics.
    Throttles database queries to prevent database flooding (Fix 2).
    """
    last_db_query_time = 0.0
    cached_completed_tasks = []
    cached_active_tasks = []
    
    while True:
        try:
            now = time.time()
            active_task_ids = list(ACTIVE_DOWNLOAD_METRICS.keys())
            
            # Fetch completed/failed tasks from DB every 10 seconds
            # This drastically reduces SQLite read pressure
            if now - last_db_query_time > 10.0:
                async with AsyncSession(engine) as db:
                    if active_task_ids:
                        # Query active tasks + recently completed tasks (e.g. last 10 minutes)
                        stmt = select(DownloadTask).order_by(DownloadTask.created_at.desc())
                        result = await db.exec(stmt)
                        tasks = result.all()
                        
                        # Cache the tasks
                        cached_completed_tasks = [t for t in tasks if t.status in ("COMPLETED", "FAILED")]
                        cached_active_tasks = [t for t in tasks if t.status not in ("COMPLETED", "FAILED")]
                    else:
                        # If idle, just pull all tasks once
                        stmt = select(DownloadTask).order_by(DownloadTask.created_at.desc())
                        result = await db.exec(stmt)
                        tasks = result.all()
                        cached_completed_tasks = [t for t in tasks if t.status in ("COMPLETED", "FAILED")]
                        cached_active_tasks = [t for t in tasks if t.status not in ("COMPLETED", "FAILED")]
                        
                    last_db_query_time = now
            else:
                # Reuse the most recent database snapshot between refreshes.
                pass
            
            download_list = []
            
            # Process active tasks (which we just fetched, or empty if idle)
            for t in cached_active_tasks:
                metrics = ACTIVE_DOWNLOAD_METRICS.get(t.id, {"progress": 0.0, "speed": "0 KB/s", "eta": "00:00:00"})
                status_text = t.status
                progress = metrics["progress"]
                
                if status_text == "DOWNLOADING":
                    status_text = "Downloading"
                elif status_text == "MERGING":
                    status_text = "Compressing with FFmpeg (H.265)"
                    
                download_list.append({
                    "id": t.id,
                    "title": t.title or f"TMDB {t.tmdb_id}",
                    "status": status_text,
                    "progress": progress,
                    "speed": metrics["speed"],
                    "eta": metrics["eta"]
                })
            
            # Process cached completed/failed tasks (limit to most recent ones to keep payload small)
            for t in cached_completed_tasks[:15]:
                status_text = "Completed" if t.status == "COMPLETED" else "Failed"
                progress = 100.0 if t.status == "COMPLETED" else 0.0
                download_list.append({
                    "id": t.id,
                    "title": t.title or f"TMDB {t.tmdb_id}",
                    "status": status_text,
                    "progress": progress,
                    "speed": "Finished" if t.status == "COMPLETED" else "Failed",
                    "eta": "00:00:00"
                })
            
            yield f"data: {json.dumps(download_list)}\n\n"
            await asyncio.sleep(1.0)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[SSE generator] Error assembling stream data: {e}")
            await asyncio.sleep(2.0)


async def current_download_snapshot() -> list[dict[str, Any]]:
    async with AsyncSession(engine) as db:
        result = await db.exec(select(DownloadTask).order_by(DownloadTask.created_at.desc()))
        tasks = result.all()

    snapshot: list[dict[str, Any]] = []
    for task in tasks:
        metrics = ACTIVE_DOWNLOAD_METRICS.get(
            task.id,
            {"progress": 0.0, "speed": "0 KB/s", "eta": "00:00:00"},
        )
        status_text = task.status
        if status_text == "DOWNLOADING":
            status_text = "Downloading"
        elif status_text == "MERGING":
            status_text = "Compressing with FFmpeg (H.265)"
        elif status_text == "COMPLETED":
            status_text = "Completed"
        elif status_text == "FAILED":
            status_text = "Failed"

        snapshot.append({
            "id": task.id,
            "title": task.title or f"TMDB {task.tmdb_id}",
            "status": status_text,
            "progress": 100.0 if task.status == "COMPLETED" else (0.0 if task.status == "FAILED" else metrics["progress"]),
            "speed": "Finished" if task.status == "COMPLETED" else ("Failed" if task.status == "FAILED" else metrics["speed"]),
            "eta": "00:00:00" if task.status in ("COMPLETED", "FAILED") else metrics["eta"],
        })
    return snapshot


@router.get("/api/downloads")
async def get_downloads(
    access=Depends(require_browser_or_integration_scope("downloads:read")),
):
    del access
    return await current_download_snapshot()


@router.get("/api/downloads/stream")
async def get_downloads_stream(
    access=Depends(require_browser_or_integration_scope("downloads:read")),
):
    """SSE streaming channel tracking download state queues in real time."""
    del access
    return StreamingResponse(
        download_progress_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

# ----------------- Task Deletion and Process Termination -----------------

@router.delete("/api/downloads/{task_id}")
async def delete_download(
    task_id: str,
    access=Depends(
        require_browser_or_integration_scope(
            "downloads:cancel",
            recent_reauthentication=True,
        )
    ),
):
    """Cancels the worker and child process before deleting its database record."""
    del access
    async with AsyncSession(engine) as db:
        task = await db.get(DownloadTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

    worker_cancelled, process_killed = await queue_manager.cancel_task(task_id)
    if worker_cancelled or process_killed:
        logger.info(f"[API] Active ingestion for task {task_id} was cancelled.")

    async with AsyncSession(engine) as db:
        task = await db.get(DownloadTask, task_id)
        if not task:
            return {"status": "success", "message": f"Task {task_id} was already removed.", "processKilled": process_killed}
        ingest_preview_service.mark_error(task_id, "PREVIEW_CANCELLED", "The download and its play-while-downloading preview were cancelled.")
        if task.media_type == "movie":
            movie = await db.get(Movie, f"m_{task.tmdb_id}")
            if movie and movie.preview_task_id == task_id:
                movie.preview_task_id = None
                if not is_local_playable_url(movie.video_url):
                    movie.video_url = ""
                    movie.availability = "cached"
                db.add(movie)
        elif task.season is not None and task.episode is not None:
            episode = await db.get(Episode, f"ep_{task.tmdb_id}_s{task.season}_e{task.episode}")
            if episode and episode.preview_task_id == task_id:
                episode.preview_task_id = None
                if not is_local_playable_url(episode.video_url):
                    episode.video_url = ""
                db.add(episode)
            show = await db.get(Movie, f"tv_{task.tmdb_id}")
            if show:
                episode_result = await db.exec(select(Episode).where(Episode.movie_id == show.id))
                has_local_episode = any(is_local_playable_url(item.video_url) for item in episode_result.all())
                show.availability = "available" if has_local_episode else "cached"
                db.add(show)
        await db.delete(task)
        await db.commit()
        
    return {
        "status": "success",
        "message": f"Task {task_id} deleted successfully.",
        "processKilled": process_killed,
        "workerCancelled": worker_cancelled,
    }
