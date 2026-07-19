import json
import uuid
import time
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Security, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db import engine
from models import DownloadTask, DownloadAddRequest, Movie, Episode
from config import settings
from services.queue import queue_manager
from services.state import ACTIVE_DOWNLOAD_METRICS, cancel_and_kill_process
from services.tmdb import tmdb_client

from services.logger import logger
from routes.auth import get_current_user

router = APIRouter()

# Authentication dependency
security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != settings.API_BEARER_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Bearer token."
        )
    return credentials.credentials

def is_safe_url(url: Optional[str]) -> bool:
    if not url:
        return True
    url_lower = url.lower()
    return url_lower.startswith("http://") or url_lower.startswith("https://")


def is_local_playable_url(url: Optional[str]) -> bool:
    return bool(url and url.startswith("/media/"))

# ----------------- Ingestion Endpoint -----------------

@router.post("/api/add-movie", status_code=status.HTTP_201_CREATED)
async def add_movie(payload: DownloadAddRequest, token: str = Depends(verify_token)):
    """Ingests media stream payload from browser extension and registers it in SQLite."""
    if not is_safe_url(payload.video_url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Forbidden URL protocol. Only HTTP and HTTPS are allowed."
        )
    if payload.audio_url and not is_safe_url(payload.audio_url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Forbidden URL protocol. Only HTTP and HTTPS are allowed."
        )
    if payload.subtitles:
        for sub in payload.subtitles:
            if not is_safe_url(sub.url):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Forbidden URL protocol. Only HTTP and HTTPS are allowed."
                )

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
        headers_dict = payload.headers or {}
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
            headers_str=json.dumps(headers_dict),
            status="PENDING",
            subtitles_str=json.dumps(subtitles_list),
            quality=payload.quality,
            language=payload.language,
            skip_markers_str=json.dumps(payload.skip_markers or {}),
            created_at=datetime.utcnow().isoformat()
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
                    availability="processing"
                )
                movie.genres = meta.get("genres", [])
                movie.cast = meta.get("cast", [])
                movie.skip_markers = payload.skip_markers or {}
                db.add(movie)
            else:
                preserve_local_media = movie.availability == "available" and is_local_playable_url(movie.video_url)
                if not preserve_local_media:
                    movie.video_url = payload.video_url
                    movie.availability = "processing"
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
                        quality=payload.quality or "Source"
                    )
                    ep_entry.skip_markers = payload.skip_markers or {}
                    db.add(ep_entry)
                else:
                    if not preserve_local_episode:
                        ep_entry.video_url = payload.video_url
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
                        active_tasks = [t for t in tasks if t.status not in ("COMPLETED", "FAILED")]
                    else:
                        # If idle, just pull all tasks once
                        stmt = select(DownloadTask).order_by(DownloadTask.created_at.desc())
                        result = await db.exec(stmt)
                        tasks = result.all()
                        cached_completed_tasks = [t for t in tasks if t.status in ("COMPLETED", "FAILED")]
                        active_tasks = []
                        
                    last_db_query_time = now
            else:
                # Use cached completed tasks, and active tasks list is empty
                active_tasks = []
            
            download_list = []
            
            # Process active tasks (which we just fetched, or empty if idle)
            for t in active_tasks:
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
                    "sourceUrl": t.video_url,
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
                    "sourceUrl": t.video_url,
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

@router.get("/api/downloads/stream")
async def get_downloads_stream(user = Depends(get_current_user)):
    """SSE streaming channel tracking download state queues in real time."""
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
async def delete_download(task_id: str, token: str = Depends(verify_token)):
    """Deletes download task from DB and terminates active FFmpeg OS process (Fix 2)."""
    # 1. Kill the active OS process registered in state.py if running
    killed = await cancel_and_kill_process(task_id)
    if killed:
        logger.info(f"[API] Running download process for task {task_id} was cancelled and terminated.")
        
    # 2. Asynchronously delete from DB
    async with AsyncSession(engine) as db:
        task = await db.get(DownloadTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        await db.delete(task)
        await db.commit()
        
    return {
        "status": "success",
        "message": f"Task {task_id} deleted successfully.",
        "processKilled": killed
    }
