import os
import sys
import warnings
import asyncio
import json
from contextlib import asynccontextmanager
from typing import List, Optional
from datetime import datetime

import time
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db import init_db, engine
from models import (
    Movie, Episode, PlaybackSession, WatchlistItem, MovieResponse, 
    PlaybackSessionResponse, DiscoverMovieResponse, EpisodeResponse, 
    Profile, ProfileResponse, APIModel, DownloadTask, TelemetryRequest,
    RecommendationFeedResponse, MediaPreferenceRequest, RecommendationExposureBatch,
    RecommendationOnboardingRequest, User, DriveSetupJob
)
from services.recommendation import (
    process_telemetry_event,
    record_authoritative_signal,
    record_playback_progress,
    rank_movies_for_profile,
    build_recommendation_payload,
    recommendation_worker,
    set_media_preference,
    get_media_preferences,
    record_recommendation_exposures,
    set_onboarding_preferences,
    get_onboarding_preferences,
    get_recommendation_diagnostics,
    reset_media_preferences,
    persist_profile_pool,
)
from config import settings
from services.logger import logger
from services.queue import queue_manager
from services.hevc_compressor import hevc_compressor
import services.state as state
from routes.queue import router as queue_router
from routes.auth import router as auth_router, health_router, get_current_user
from routes.stream import router as stream_router
from routes.backup import router as backup_router
from routes.update import router as update_router
from routes.setup import router as setup_router

# 💥 WINDOWS ASYNC SUBPROCESS FIX
if sys.platform == 'win32':
    # Python 3.14'ün verdiği "Deprecated" uyarılarını terminali kirletmemesi için susturuyoruz
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 🚀 MODERN FASTAPI LIFESPAN (Eski on_event yapısının yerine)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sunucu başlarken (Startup) yapılacaklar:
    try:
        await init_db()
    except Exception as db_err:
        logger.error(f"[Lifespan Startup] Database initialization failed (server continuing): {db_err}")

    # An installation flag without an administrator is never considered complete.
    try:
        async with AsyncSession(engine) as db:
            existing_user = (await db.exec(select(User))).first()
            if not existing_user:
                settings.SETUP_COMPLETE = False
    except Exception as setup_state_err:
        logger.error(f"[Lifespan Startup] Setup-state validation failed: {setup_state_err}")

    # Preserve resumable Drive setup jobs while repairing work interrupted mid-command.
    try:
        async with AsyncSession(engine) as db:
            drive_jobs = (await db.exec(select(DriveSetupJob))).all()
            changed = False
            for job in drive_jobs:
                job_changed = False
                if job.expires_at < time.time() and job.status not in {"cancelled", "failed", "expired"}:
                    job.status = "expired"
                    job.error_code = "drive_job_expired"
                    job.progress = "Google Drive setup expired. Start again."
                    rclone_service.cleanup_job(job.id)
                    job_changed = True
                elif job.status == "exchanging_code":
                    job.status = "failed"
                    job.error_code = "drive_server_restarted"
                    job.progress = "The server restarted during Google authorization. Start the Drive connection again."
                    job_changed = True
                elif job.status == "testing":
                    job.status = "selecting_folder"
                    job.error_code = "drive_test_interrupted"
                    job.progress = "The Drive test was interrupted. Run it again."
                    job_changed = True
                if job_changed:
                    changed = True
                    job.updated_at = time.time()
                    db.add(job)
            if changed:
                await db.commit()
    except Exception as drive_state_err:
        logger.error(f"[Lifespan Startup] Drive setup recovery failed: {drive_state_err}")
    
    try:
        settings.get_system_profile()
    except Exception as pf_err:
        logger.error(f"[Lifespan Startup] System profile generation failed: {pf_err}")
    
    # Check if TMDB credentials are set and show a warning if they are missing
    try:
        from services.tmdb import tmdb_client
        if not tmdb_client.api_key and not tmdb_client.read_access_token:
            logger.warning("TMDB_READ_ACCESS_TOKEN or TMDB_API_KEY is not configured in your .env file!")
            logger.warning("All media catalogs and recovery syncs will use 'Captured Movie' placeholders.")
            logger.warning("To fix: Configure the token in your .env or run: python cli.py")
    except Exception as tmdb_err:
        logger.error(f"[Lifespan Startup] TMDB client credentials verification failed: {tmdb_err}")
        
    # Clean up dangling tasks from previous run
    try:
        async with AsyncSession(engine) as db:
            stmt = select(DownloadTask).where(DownloadTask.status.in_(["DOWNLOADING", "MERGING", "MOVING_CLOUD"]))
            result = await db.exec(stmt)
            dangling_tasks = result.all()
            if dangling_tasks:
                logger.info(f"[Server Startup] Found {len(dangling_tasks)} dangling tasks from previous execution. Marking them as FAILED...")
                for task in dangling_tasks:
                    task.status = "FAILED"
                    task.error_message = "Interrupted by server shutdown/restart."
                    db.add(task)
                await db.commit()
    except Exception as dangling_err:
        logger.error(f"[Lifespan Startup] Error cleaning up dangling tasks: {dangling_err}")

    # Seed default Admin profile if none exists
    try:
        async with AsyncSession(engine) as db:
            stmt = select(Profile)
            result = await db.exec(stmt)
            if not result.first():
                logger.info("[Database] Seeding default Admin profile...")
                admin_profile = Profile(
                    id="1",
                    name="Admin",
                    avatar_color="from-blue-600 to-indigo-600",
                    theme="netflix",
                    pin_enabled=False,
                    pin=None
                )
                db.add(admin_profile)
                await db.commit()
    except Exception as seed_err:
        logger.error(f"[Lifespan Startup] Error seeding default profile: {seed_err}")
            
    try:
        # Run sync in the background so Uvicorn startup can complete instantly (prevents 504 gateway timeout)
        asyncio.create_task(queue_manager.sync_media_from_disk())
    except Exception as sync_err:
        logger.error(f"[Lifespan Startup] Error scheduling media sync from disk: {sync_err}")

    try:
        queue_manager.start()
    except Exception as q_start_err:
        logger.error(f"[Lifespan Startup] Error starting queue manager: {q_start_err}")

    try:
        from services.tmdb import tmdb_client
        await tmdb_client.start_cache_workers()
    except Exception as cache_start_err:
        logger.error(f"[Lifespan Startup] Error starting TMDB cache workers: {cache_start_err}")

    try:
        hevc_compressor.start()
    except Exception as h_start_err:
        logger.error(f"[Lifespan Startup] Error starting hevc compressor: {h_start_err}")

    # 💾 DAILY BACKUP & SYNC SCHEDULER
    async def daily_backup_worker():
        await asyncio.sleep(30)  # Wait 30s after boot before first check
        while True:
            try:
                if settings.BACKUP_ENABLED:
                    from services.backup import get_local_backups, is_database_idle, create_backup, prune_old_backups, sync_backups_to_cloud
                    backups = get_local_backups()
                    should_backup = True
                    if backups:
                        newest = backups[0]
                        newest_time = datetime.fromisoformat(newest["timestamp"])
                        elapsed = datetime.now() - newest_time
                        if elapsed.total_seconds() < 24 * 60 * 60:
                            should_backup = False
                    
                    if should_backup:
                        if await is_database_idle():
                            logger.info("[Backup Worker] Database is idle. Initiating daily database backup...")
                            backup_path = await create_backup()
                            prune_old_backups(keep_count=7)
                            if settings.STORAGE_ENGINE == "CLOUD":
                                await sync_backups_to_cloud()
                            logger.info(f"[Backup Worker] Daily database backup successfully completed: {backup_path}")
                        else:
                            logger.info("[Backup Worker] Daily backup is due, but database is currently in use. Deferring check...")
                            await asyncio.sleep(300)
                            continue
            except Exception as e:
                logger.error(f"[Backup Worker] Error in daily backup scheduler: {e}")
            await asyncio.sleep(3600)  # Check hourly

    asyncio.create_task(daily_backup_worker())

    # 🔄 AUTOMATIC UPDATE SCHEDULER
    async def auto_update_worker():
        await asyncio.sleep(45)  # Wait 45s after boot before first check
        while True:
            try:
                if settings.AUTO_UPDATE_ENABLED:
                    from services.update import check_for_github_updates, is_system_idle, pull_and_install_updates, self_restart_server
                    if await check_for_github_updates():
                        # Wait for idle state (poll every 5 minutes)
                        while not await is_system_idle():
                            logger.info("[Update Worker] Update is available, but system is currently in use. Retrying idle check in 5 minutes...")
                            await asyncio.sleep(300)
                            
                        # Idle achieved: perform upgrade
                        logger.info("[Update Worker] System is idle. Executing pull and update...")
                        success = await pull_and_install_updates()
                        if success:
                            logger.info("[Update Worker] Update successfully applied. Restarting server...")
                            self_restart_server()
                            break # Exiting current process loop
            except Exception as e:
                logger.error(f"[Update Worker] Error in automatic update scheduler: {e}")
            await asyncio.sleep(3600)  # Check every 1 hour

    asyncio.create_task(auto_update_worker())

    recommendation_stop = asyncio.Event()
    recommendation_task = asyncio.create_task(recommendation_worker(recommendation_stop))

    logger.info("[Server] Lifespan: Startup completed (with fallback checks).")
    
    yield  # Sunucu bu noktada çalışmaya devam eder
    
    # Sunucu kapanırken (Shutdown) yapılacaklar:
    try:
        await queue_manager.stop()
    except Exception as q_stop_err:
        logger.error(f"[Lifespan Shutdown] Error stopping queue manager: {q_stop_err}")
        
    try:
        hevc_compressor.stop()
    except Exception as h_stop_err:
        logger.error(f"[Lifespan Shutdown] Error stopping hevc compressor: {h_stop_err}")
    recommendation_stop.set()
    try:
        await asyncio.wait_for(recommendation_task, timeout=5)
    except asyncio.TimeoutError:
        recommendation_task.cancel()
    try:
        from services.tmdb import tmdb_client
        await tmdb_client.stop_cache_workers()
        await tmdb_client.close()
    except Exception as tmdb_close_err:
        logger.error(f"[Lifespan Shutdown] Error closing TMDB client: {tmdb_close_err}")
    logger.info("[Server] Lifespan: Queue Manager stopped securely.")

class ActivityTrackingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Exclude update endpoints from activity tracking to allow status checks
        if "/api/update" in request.url.path:
            return await call_next(request)
            
        state.ACTIVE_HTTP_REQUESTS += 1
        state.LAST_HTTP_ACTIVITY_TIMESTAMP = time.time()
        try:
            response = await call_next(request)
            return response
        finally:
            state.ACTIVE_HTTP_REQUESTS = max(0, state.ACTIVE_HTTP_REQUESTS - 1)
            state.LAST_HTTP_ACTIVITY_TIMESTAMP = time.time()

class SetupGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        allowed = request.method == "OPTIONS" or path.startswith("/api/setup") or path == "/api/health"
        if not settings.SETUP_COMPLETE and not allowed:
            return JSONResponse(
                status_code=503,
                content={"detail": {"code": "setup_required", "message": "Complete StreamHome setup before using this endpoint."}},
                headers={"Retry-After": "5"},
            )
        return await call_next(request)

# Initialize FastAPI application with modern lifespan
app = FastAPI(title="StreamHome Media Server", version=settings.APP_VERSION, lifespan=lifespan)

# Setup CORS and activity tracking middlewares
app.add_middleware(ActivityTrackingMiddleware)
app.add_middleware(SetupGateMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(queue_router)
app.include_router(auth_router)
app.include_router(health_router)
app.include_router(stream_router)
app.include_router(backup_router, prefix="/api/backup", tags=["backup"])
app.include_router(update_router, prefix="/api/update", tags=["update"])
app.include_router(setup_router)

# Ensure directories exist before mounting static files
os.makedirs(settings.MEDIA_DIR, exist_ok=True)
os.makedirs(os.path.join(settings.MEDIA_DIR, "Movies"), exist_ok=True)
os.makedirs(os.path.join(settings.MEDIA_DIR, "Series"), exist_ok=True)

# Dynamic media route replacing standard StaticFiles to support cloud fallback & background caching
from fastapi.responses import FileResponse, StreamingResponse
import re
from routes.stream import get_rclone_path, cloud_stream_generator, download_file_from_cloud_task, ACTIVE_CLOUD_DOWNLOADS
from services.rclone import rclone_service

@app.get("/media/{file_path:path}")
async def serve_media_file(file_path: str, request: Request):
    file_path = file_path.lstrip("/")
    abs_path = os.path.abspath(os.path.join(settings.MEDIA_DIR, file_path))
    if not abs_path.startswith(os.path.abspath(settings.MEDIA_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")

    # 1. If it exists on disk, serve it immediately
    if os.path.exists(abs_path):
        return FileResponse(abs_path)

    # 2. If not on disk, check if we are in CLOUD storage engine
    if settings.STORAGE_ENGINE == "CLOUD":
        target_remote = f"{settings.RCLONE_REMOTE_PATH}/{file_path.replace('\\', '/')}"
        
        # Trigger background download so it's cached locally for next time
        if abs_path not in ACTIVE_CLOUD_DOWNLOADS:
            ACTIVE_CLOUD_DOWNLOADS.add(abs_path)
            asyncio.create_task(download_file_from_cloud_task(target_remote, abs_path))
            
        # Parse range header
        range_header = request.headers.get("range")
        start_byte = 0
        end_byte = None
        file_size = 0
        
        # Determine media content-type
        content_type = "application/octet-stream"
        ext = os.path.splitext(file_path.lower())[1]
        if ext == ".mp4":
            content_type = "video/mp4"
        elif ext == ".mp3":
            content_type = "audio/mpeg"
        elif ext in (".m4a", ".aac"):
            content_type = "audio/mp4"
        elif ext == ".vtt":
            content_type = "text/vtt"
        elif ext == ".srt":
            content_type = "text/plain"
        elif ext in (".jpg", ".jpeg"):
            content_type = "image/jpeg"
        elif ext == ".png":
            content_type = "image/png"
            
        is_streamable = ext in (".mp4", ".mp3", ".m4a")
        rclone_path = get_rclone_path()
        if is_streamable and rclone_path:
            # Query size from rclone
            try:
                proc = await asyncio.create_subprocess_exec(
                    *rclone_service.command("lsjson", target_remote),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0:
                    data = json.loads(stdout)
                    if isinstance(data, list) and len(data) > 0:
                        file_size = data[0].get("Size", 0)
            except Exception as e:
                logger.error(f"[Media Router] Error fetching file size: {e}")

            # Parse byte ranges
            count = None
            if range_header:
                match = re.match(r"bytes=(\d+)-(\d*)", range_header)
                if match:
                    start_byte = int(match.group(1))
                    if match.group(2):
                        end_byte = int(match.group(2))
                    elif file_size > 0:
                        end_byte = file_size - 1
                    
                    if end_byte is not None:
                        count = end_byte - start_byte + 1
            
            if file_size > 0 and end_byte is None:
                end_byte = file_size - 1
                count = file_size - start_byte
            
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Type": content_type,
            }
            
            if range_header:
                content_range = f"bytes {start_byte}-{end_byte}/{file_size if file_size > 0 else '*'}"
                headers["Content-Range"] = content_range
                headers["Content-Length"] = str(count) if count is not None else "0"
                return StreamingResponse(
                    cloud_stream_generator(target_remote, start_byte, count),
                    status_code=206,
                    headers=headers
                )
            else:
                if file_size > 0:
                    headers["Content-Length"] = str(file_size)
                return StreamingResponse(
                    cloud_stream_generator(target_remote, 0, None),
                    status_code=200,
                    headers=headers
                )
        else:
            return StreamingResponse(
                cloud_stream_generator(target_remote, 0, None),
                status_code=200,
                headers={"Content-Type": content_type}
            )

    raise HTTPException(status_code=404, detail="File not found")


# ----------------- Movies Catalog API -----------------

@app.get("/api/movies", response_model=List[MovieResponse])
async def get_movies(profile_id: Optional[str] = Query(None), user = Depends(get_current_user)):
    """Fetches all cataloged media assets with linked episode detail mappings, optionally personalized."""
    async with AsyncSession(engine) as db:
        stmt = select(Movie)
        result = await db.exec(stmt)
        movies = result.all()
        
        ep_result = await db.exec(select(Episode).order_by(Episode.season_number, Episode.episode_number))
        episodes_by_movie = {}
        for episode in ep_result.all():
            episodes_by_movie.setdefault(episode.movie_id, []).append(episode)
        results = [(m, MovieResponse.from_db(m, episodes_by_movie.get(m.id))) for m in movies]
        
        if profile_id:
            ranked = await rank_movies_for_profile(db, profile_id, movies, episodes_by_movie)
            responses = {movie.id: response for movie, response in results}
            return [responses[movie.id] for _, movie, _, _, _ in ranked]
            
        return [res for m, res in results]

@app.get("/api/movies/featured", response_model=Optional[MovieResponse])
async def get_featured_movie(user = Depends(get_current_user)):
    """Returns the featured or most recently cataloged movie asset."""
    async with AsyncSession(engine) as db:
        stmt = select(Movie).order_by(Movie.release_year.desc())
        result = await db.exec(stmt)
        movie = result.first()
        
        if not movie:
            return None
            
        episodes = None
        if movie.type == "series":
            ep_stmt = select(Episode).where(Episode.movie_id == movie.id).order_by(Episode.season_number, Episode.episode_number)
            ep_result = await db.exec(ep_stmt)
            episodes = ep_result.all()
            
        return MovieResponse.from_db(movie, episodes)


@app.get("/api/movies/{media_id}", response_model=MovieResponse)
async def get_movie(media_id: str, user = Depends(get_current_user)):
    """Resolve one canonical playable or metadata-only catalog record."""
    async with AsyncSession(engine) as db:
        movie = await db.get(Movie, media_id)
        if not movie:
            raise HTTPException(status_code=404, detail="Media not found")
        episodes = None
        if movie.type == "series":
            result = await db.exec(select(Episode).where(Episode.movie_id == movie.id).order_by(Episode.season_number, Episode.episode_number))
            episodes = list(result.all())
        return MovieResponse.from_db(movie, episodes)


# ----------------- Playback Tracking & Pulse -----------------

@app.get("/api/track/{profile_id}", response_model=List[PlaybackSessionResponse])
async def get_playback_tracking(profile_id: str, user = Depends(get_current_user)):
    """Retrieves continue-watching tracking playback states for a profile."""
    async with AsyncSession(engine) as db:
        stmt = select(PlaybackSession).where(PlaybackSession.profile_id == profile_id)
        result = await db.exec(stmt)
        sessions = result.all()
        return [
            PlaybackSessionResponse(
                movieId=s.movie_id,
                profileId=s.profile_id,
                timestamp=s.timestamp,
                duration_watched=s.duration_watched,
                completion_rate=s.completion_rate,
                updatedAt=s.updated_at,
                episodeId=s.episode_id,
                is_finished=s.is_finished
            )
            for s in sessions
        ]

@app.post("/api/telemetry")
async def handle_telemetry(request: TelemetryRequest, profile_id: str = Query(...), user = Depends(get_current_user)):
    """Receives generic tracking telemetry from the web UI."""
    accepted = await process_telemetry_event(profile_id, request)
    return {"status": "accepted" if accepted else "ignored", "accepted": accepted}

@app.post("/api/track")
async def update_playback_tracking(request: Request, user = Depends(get_current_user)):
    """Receives 10-second playback tracker pulses from the VideoPlayer."""
    from starlette.requests import ClientDisconnect
    try:
        body = await request.json()
    except ClientDisconnect:
        print("[Tracking] Client disconnected while sending tracking update.")
        return {"status": "disconnected"}
    
    movie_id = body.get("movieId")
    profile_id = body.get("profileId")
    timestamp = body.get("timestamp")
    duration_watched = body.get("duration_watched", 0)
    completion_rate = body.get("completion_rate", 0.0)
    episode_id = body.get("episodeId")
    is_finished = body.get("is_finished", False)
    
    if not movie_id or not profile_id or timestamp is None:
        raise HTTPException(
            status_code=400,
            detail="Missing required parameters: movieId, profileId"
        )
        
    async with AsyncSession(engine) as db:
        filters = [
            PlaybackSession.movie_id == movie_id,
            PlaybackSession.profile_id == profile_id,
        ]
        if episode_id:
            filters.append(PlaybackSession.episode_id == episode_id)
        else:
            filters.append(PlaybackSession.episode_id.is_(None))
        stmt = select(PlaybackSession).where(*filters)
        result = await db.exec(stmt)
        session = result.first()
        
        now_str = datetime.utcnow().isoformat()
        if session:
            session.timestamp = int(timestamp)
            session.duration_watched = int(duration_watched)
            session.completion_rate = float(completion_rate)
            session.updated_at = now_str
            session.is_finished = is_finished
            db.add(session)
        else:
            session = PlaybackSession(
                profile_id=profile_id,
                movie_id=movie_id,
                episode_id=episode_id,
                timestamp=int(timestamp),
                duration_watched=int(duration_watched),
                completion_rate=float(completion_rate),
                updated_at=now_str,
                is_finished=is_finished
            )
            db.add(session)
            
        await db.commit()
        
    viewing_attempt_id = await record_playback_progress(
        profile_id=profile_id,
        movie_id=movie_id,
        episode_id=episode_id,
        position=int(timestamp),
        duration_watched=int(duration_watched or 0),
        completion_rate=float(completion_rate or 0.0),
        is_finished=bool(is_finished),
    )
    return {"status": "ok", "viewingSessionId": viewing_attempt_id}

# ----------------- Watchlist Management API -----------------

from pydantic import BaseModel

class WatchlistToggleRequest(BaseModel):
    profile_id: str
    movie_id: str

@app.get("/api/recommendations/{profile_id}", response_model=RecommendationFeedResponse)
async def get_recommendations(
    profile_id: str,
    scope: str = Query("home"),
    category: str = Query("recommended"),
    limit: int = Query(48, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user = Depends(get_current_user),
):
    """Return a personalized mixed cached/server catalog for the future web client."""
    if scope not in {"home", "movies", "series"}:
        raise HTTPException(status_code=400, detail="Invalid recommendation scope")
    async with AsyncSession(engine) as db:
        if not await db.get(Profile, profile_id):
            raise HTTPException(status_code=404, detail="Profile not found")
        payload = await build_recommendation_payload(db, profile_id, scope, category, limit, offset)
        return RecommendationFeedResponse(**payload)

@app.get("/api/recommendations/{profile_id}/preferences")
async def list_recommendation_preferences(profile_id: str, user = Depends(get_current_user)):
    return {"preferences": await get_media_preferences(profile_id)}

@app.put("/api/recommendations/{profile_id}/preferences/{movie_id}")
async def update_recommendation_preference(profile_id: str, movie_id: str, request: MediaPreferenceRequest, user = Depends(get_current_user)):
    try:
        return await set_media_preference(profile_id, movie_id, request.preference)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))

@app.post("/api/recommendations/{profile_id}/exposures")
async def add_recommendation_exposures(profile_id: str, request: RecommendationExposureBatch, user = Depends(get_current_user)):
    return {"accepted": await record_recommendation_exposures(profile_id, request.exposures)}

@app.get("/api/recommendations/{profile_id}/onboarding")
async def read_recommendation_onboarding(profile_id: str, user = Depends(get_current_user)):
    return await get_onboarding_preferences(profile_id)

@app.put("/api/recommendations/{profile_id}/onboarding")
async def update_recommendation_onboarding(profile_id: str, request: RecommendationOnboardingRequest, user = Depends(get_current_user)):
    try:
        return await set_onboarding_preferences(profile_id, request.genres, request.title_ids)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))

@app.get("/api/recommendations/{profile_id}/diagnostics")
async def recommendation_diagnostics(profile_id: str, user = Depends(get_current_user)):
    return await get_recommendation_diagnostics(profile_id)

@app.post("/api/recommendations/{profile_id}/rebuild")
async def rebuild_recommendations(profile_id: str, user = Depends(get_current_user)):
    async with AsyncSession(engine) as db:
        if not await db.get(Profile, profile_id):
            raise HTTPException(status_code=404, detail="Profile not found")
        await persist_profile_pool(db, profile_id)
        await db.commit()
    return {"status": "rebuilt"}

@app.delete("/api/recommendations/{profile_id}/preferences")
async def clear_recommendation_preferences(profile_id: str, user = Depends(get_current_user)):
    return {"cleared": await reset_media_preferences(profile_id)}

@app.get("/api/watchlist/{profile_id}", response_model=List[str])
async def get_watchlist(profile_id: str, user = Depends(get_current_user)):
    """Retrieves watchlist items for a profile."""
    async with AsyncSession(engine) as db:
        stmt = select(WatchlistItem).where(WatchlistItem.profile_id == profile_id)
        result = await db.exec(stmt)
        items = result.all()
        # Sort items by created_at desc so latest items are shown first
        items.sort(key=lambda x: x.created_at, reverse=True)
        return [item.movie_id for item in items]

@app.post("/api/watchlist/toggle")
async def toggle_watchlist(req: WatchlistToggleRequest, user = Depends(get_current_user)):
    """Toggles movie presence in the profile's server watchlist."""
    async with AsyncSession(engine) as db:
        stmt = select(WatchlistItem).where(
            WatchlistItem.profile_id == req.profile_id,
            WatchlistItem.movie_id == req.movie_id
        )
        result = await db.exec(stmt)
        item = result.first()
        
        if item:
            await db.delete(item)
            status = "removed"
        else:
            item = WatchlistItem(
                profile_id=req.profile_id,
                movie_id=req.movie_id,
                created_at=datetime.utcnow().isoformat()
            )
            db.add(item)
            status = "added"
            
        await db.commit()
        
        # Return updated watchlist
        stmt_all = select(WatchlistItem).where(WatchlistItem.profile_id == req.profile_id)
        result_all = await db.exec(stmt_all)
        items_all = result_all.all()
        items_all.sort(key=lambda x: x.created_at, reverse=True)
        response = {"status": status, "watchlist": [x.movie_id for x in items_all]}

    await record_authoritative_signal(
        req.profile_id,
        req.movie_id,
        "watchlist_add" if status == "added" else "watchlist_remove",
    )
    return response


@app.get("/api/discover", response_model=List[DiscoverMovieResponse])
async def get_discover_movies(category: str = "action", type: str = "movie", profile_id: Optional[str] = Query(None), user = Depends(get_current_user)):
    """Fetches trending movies or series from TMDB for the discover rows."""
    from services.tmdb import tmdb_client
    return await tmdb_client.discover_media(category, type, profile_id)

@app.get("/api/search", response_model=List[DiscoverMovieResponse])
async def search_tmdb_movies(query: str, user = Depends(get_current_user)):
    """Searches movies from TMDB for search suggestion results and caches posters/backdrops."""
    from services.tmdb import tmdb_client
    if not query:
        return []
    return await tmdb_client.search_media(query)

@app.get("/api/tmdb/{media_type}/{tmdb_id}")
async def get_tmdb_metadata(media_type: str, tmdb_id: int, user = Depends(get_current_user)):
    """Fetch detailed movie or TV show metadata from TMDB API."""
    from services.tmdb import tmdb_client
    try:
        if media_type.lower() in ("series", "tv"):
            data = await tmdb_client.fetch_show_metadata(tmdb_id)
        else:
            data = await tmdb_client.fetch_movie_metadata(tmdb_id)
        return data
    except Exception as e:
        logger.error(f"[API] Failed to fetch TMDB metadata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/series/{tmdb_id}/episodes", response_model=List[EpisodeResponse])
async def get_series_episodes(tmdb_id: int, user = Depends(get_current_user)):
    """Fetches real seasons and episodes for a TV series from TMDB, enriched with local catalog data if available."""
    from services.tmdb import tmdb_client
    
    # 1. Fetch local episodes cataloged in the database
    async with AsyncSession(engine) as db:
        stmt = select(Episode).where(Episode.movie_id == f"tv_{tmdb_id}").order_by(Episode.season_number, Episode.episode_number)
        result = await db.exec(stmt)
        local_episodes = result.all()
        
    # Map local episodes by (season_number, episode_number) for O(1) enrichment lookup
    local_map = {
        (e.season_number, e.episode_number): e
        for e in local_episodes
    }
        
    # 2. Fetch episodes dynamically from TMDB so we always get all seasons and episodes
    try:
        show_data = await tmdb_client._get(f"/tv/{tmdb_id}")
        if not show_data:
            raise HTTPException(status_code=404, detail="TV Series not found in TMDB")
            
        seasons = show_data.get("seasons", [])
        active_seasons = []
        today = datetime.now().date()
        for s in seasons:
            if s.get("season_number", 0) <= 0:
                continue
            air_date = s.get("air_date")
            if air_date:
                try:
                    air_dt = datetime.strptime(air_date, "%Y-%m-%d").date()
                    if air_dt > today:
                        continue
                except ValueError:
                    pass
            active_seasons.append(s)
        if not active_seasons and seasons:
            active_seasons = seasons
            
        all_episodes = []
        
        async def fetch_season_episodes(season_num: int):
            season_data = await tmdb_client._get(f"/tv/{tmdb_id}/season/{season_num}")
            if not season_data or not season_data.get("episodes"):
                return []
            
            eps = []
            for ep in season_data.get("episodes", []):
                ep_air_date = ep.get("air_date")
                if ep_air_date:
                    try:
                        ep_air_dt = datetime.strptime(ep_air_date, "%Y-%m-%d").date()
                        if ep_air_dt > today:
                            continue
                    except ValueError:
                        pass
                ep_num = ep.get("episode_number", 1)
                still_path = ep.get("still_path")
                thumbnail_url = f"https://image.tmdb.org/t/p/w300{still_path}" if still_path else ""
                
                runtime = ep.get("runtime", 0) or 45
                duration_str = f"{runtime}m"
                
                # Check if this episode is locally downloaded/cataloged in our database
                local_ep = local_map.get((season_num, ep_num))
                if local_ep:
                    eps.append(
                        EpisodeResponse(
                            id=local_ep.id,
                            episode_number=local_ep.episode_number,
                            season_number=local_ep.season_number,
                            title=local_ep.title,
                            description=local_ep.description,
                            thumbnail_url=local_ep.thumbnail_url or thumbnail_url,
                            video_url=local_ep.video_url,
                            duration=local_ep.duration
                        )
                    )
                else:
                    eps.append(
                        EpisodeResponse(
                            id=f"ep_{tmdb_id}_s{season_num}_e{ep_num}",
                            episode_number=ep_num,
                            season_number=season_num,
                            title=ep.get("name") or f"Episode {ep_num}",
                            description=ep.get("overview") or "",
                            thumbnail_url=thumbnail_url,
                            video_url="",
                            duration=duration_str
                        )
                    )
            return eps
            
        tasks = [fetch_season_episodes(s.get("season_number", 1)) for s in active_seasons]
        results = await asyncio.gather(*tasks)
        
        for r in results:
            all_episodes.extend(r)
            
        all_episodes.sort(key=lambda x: (x.season_number, x.episode_number))
        return all_episodes
        
    except Exception as e:
        print(f"[API Series] Error fetching seasons/episodes from TMDB for {tmdb_id}: {e}")
        # Fallback to local episodes if TMDB fetch fails
        return [
            EpisodeResponse(
                id=e.id,
                episode_number=e.episode_number,
                season_number=e.season_number,
                title=e.title,
                description=e.description,
                thumbnail_url=e.thumbnail_url,
                video_url=e.video_url,
                duration=e.duration
            )
            for e in local_episodes
        ]

class ProfileSaveRequest(APIModel):
    id: str
    name: str
    avatar_color: Optional[str] = "from-blue-600 to-indigo-650"
    theme: Optional[str] = "netflix"
    pin_enabled: Optional[bool] = False
    pin: Optional[str] = None

@app.get("/api/profiles", response_model=List[ProfileResponse])
async def get_profiles(user = Depends(get_current_user)):
    """Retrieves all profile records from the database."""
    async with AsyncSession(engine) as db:
        stmt = select(Profile)
        result = await db.exec(stmt)
        profiles = result.all()
        return [
            ProfileResponse(
                id=p.id,
                name=p.name,
                avatar_color=p.avatar_color,
                theme=p.theme,
                pin_enabled=p.pin_enabled,
                pin=p.pin
            )
            for p in profiles
        ]

@app.post("/api/profiles", response_model=ProfileResponse)
async def save_profile(req: ProfileSaveRequest, user = Depends(get_current_user)):
    """Creates a new profile or updates an existing profile configuration in the database."""
    async with AsyncSession(engine) as db:
        stmt = select(Profile).where(Profile.id == req.id)
        result = await db.exec(stmt)
        profile = result.first()
        
        if not profile:
            profile = Profile(
                id=req.id,
                name=req.name,
                avatar_color=req.avatar_color,
                theme=req.theme,
                pin_enabled=req.pin_enabled,
                pin=req.pin
            )
            db.add(profile)
        else:
            profile.name = req.name
            profile.avatar_color = req.avatar_color
            profile.theme = req.theme
            profile.pin_enabled = req.pin_enabled
            profile.pin = req.pin
            db.add(profile)
            
        await db.commit()
        await db.refresh(profile)
        return ProfileResponse(
            id=profile.id,
            name=profile.name,
            avatar_color=profile.avatar_color,
            theme=profile.theme,
            pin_enabled=profile.pin_enabled,
            pin=profile.pin
        )

@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str, user = Depends(get_current_user)):
    """Deletes a profile from the database."""
    async with AsyncSession(engine) as db:
        stmt = select(Profile).where(Profile.id == profile_id)
        result = await db.exec(stmt)
        profile = result.first()
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        await db.delete(profile)
        await db.commit()
        return {"status": "deleted"}


# ----------------- System Settings API -----------------

class SystemSettingsRequest(APIModel):
    storage_engine: str
    rclone_remote_path: str
    hevc_compression_mode: str = "auto"

class SystemSettingsResponse(APIModel):
    storage_engine: str
    rclone_remote_path: str
    hevc_compression_mode: str
    drive_configured: bool = False
    drive_reachable: Optional[bool] = None
    drive_error_code: Optional[str] = None
    google_drive_audience: str = "external"
    google_drive_publishing_status: str = "production"

async def _drive_settings_status() -> tuple[bool, Optional[bool], Optional[str]]:
    configured = rclone_service.config_path.exists() and ":" in settings.RCLONE_REMOTE_PATH
    if not configured:
        return False, None, None
    result = await rclone_service.run("about", settings.RCLONE_REMOTE_PATH.split(":", 1)[0] + ":", "--json", timeout=12)
    return True, result.ok, result.error_code

@app.get("/api/system/settings", response_model=SystemSettingsResponse)
async def get_system_settings(user = Depends(get_current_user)):
    """Retrieves current server storage engine and Rclone settings."""
    drive_configured, drive_reachable, drive_error_code = await _drive_settings_status()
    return SystemSettingsResponse(
        storage_engine=settings.STORAGE_ENGINE,
        rclone_remote_path=settings.RCLONE_REMOTE_PATH,
        hevc_compression_mode=settings.HEVC_COMPRESSION_MODE,
        drive_configured=drive_configured,
        drive_reachable=drive_reachable,
        drive_error_code=drive_error_code,
        google_drive_audience=settings.GOOGLE_DRIVE_AUDIENCE,
        google_drive_publishing_status=settings.GOOGLE_DRIVE_PUBLISHING_STATUS,
    )

@app.post("/api/system/settings", response_model=SystemSettingsResponse)
async def save_system_settings(req: SystemSettingsRequest, user = Depends(get_current_user)):
    """Updates server storage engine settings and persists them to settings.json."""
    if req.storage_engine not in ["LOCAL", "CLOUD"]:
        raise HTTPException(status_code=400, detail="Invalid storage engine value. Must be LOCAL or CLOUD.")
    
    if req.hevc_compression_mode not in ["auto", "on", "off"]:
        raise HTTPException(status_code=400, detail="Invalid hevc compression mode. Must be auto, on, or off.")
    if req.storage_engine == "CLOUD":
        if req.rclone_remote_path != settings.RCLONE_REMOTE_PATH:
            raise HTTPException(status_code=400, detail="Google Drive targets must be changed through the guided Drive connection flow.")
        configured, reachable, error_code = await _drive_settings_status()
        if not configured or not reachable:
            raise HTTPException(status_code=422, detail={"code": error_code or "drive_not_configured", "message": "Connect and test Google Drive before enabling cloud storage."})
    
    settings.STORAGE_ENGINE = req.storage_engine
    settings.RCLONE_REMOTE_PATH = req.rclone_remote_path
    settings.HEVC_COMPRESSION_MODE = req.hevc_compression_mode
    settings.save_to_json()
    drive_configured, drive_reachable, drive_error_code = await _drive_settings_status()
    return SystemSettingsResponse(
        storage_engine=settings.STORAGE_ENGINE,
        rclone_remote_path=settings.RCLONE_REMOTE_PATH,
        hevc_compression_mode=settings.HEVC_COMPRESSION_MODE,
        drive_configured=drive_configured,
        drive_reachable=drive_reachable,
        drive_error_code=drive_error_code,
        google_drive_audience=settings.GOOGLE_DRIVE_AUDIENCE,
        google_drive_publishing_status=settings.GOOGLE_DRIVE_PUBLISHING_STATUS,
    )

@app.post("/api/system/drive/test")
async def test_system_drive(user = Depends(get_current_user)):
    configured, reachable, error_code = await _drive_settings_status()
    if not configured:
        raise HTTPException(status_code=409, detail={"code": "drive_not_configured", "message": "Google Drive has not been configured."})
    if not reachable:
        raise HTTPException(status_code=422, detail={"code": error_code or "drive_unreachable", "message": "Google Drive could not be reached."})
    return {"configured": True, "reachable": True, "remotePath": settings.RCLONE_REMOTE_PATH}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
