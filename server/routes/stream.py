import os
import shutil
import asyncio
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, status, Query
from fastapi.responses import StreamingResponse, FileResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db import engine, get_session
from models import Movie, Episode
from config import settings
from services.logger import logger
from routes.auth import get_current_user

router = APIRouter(prefix="/api/stream", tags=["Streaming"])

async def transcode_generator(input_path: str, height: int, start_sec: float, media_id: str, quality: str):
    import aiofiles
    # Resolve ffmpeg binary path dynamically
    ffmpeg_path = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    
    # Cache path files setup
    cache_dir = os.path.join(settings.TEMP_DIR, "transcode_cache")
    os.makedirs(cache_dir, exist_ok=True)
    temp_cache_file = os.path.join(cache_dir, f"{media_id}_{quality}.mp4.tmp")
    final_cache_file = os.path.join(cache_dir, f"{media_id}_{quality}.mp4")
    
    should_cache = (start_sec == 0.0)

    # Build transcoding command
    cmd = [
        ffmpeg_path,
        "-y",
        "-ss", str(start_sec),
        "-i", input_path,
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-c:a", "aac",
        "-b:a", "128k",
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "pipe:1"
    ]
    
    logger.info(f"[Streaming Router] Executing on-the-fly transcode command: {' '.join(cmd)}")
    
    f_cache = None
    if should_cache:
        # If a temp file already exists, delete it first to prevent lock/append corruption
        if os.path.exists(temp_cache_file):
            try: os.remove(temp_cache_file)
            except Exception: pass
        try:
            f_cache = await aiofiles.open(temp_cache_file, "wb")
        except Exception as e:
            logger.error(f"[Streaming Router] Failed to open cache temp file: {e}")
            should_cache = False

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        
        while True:
            chunk = await process.stdout.read(64 * 1024)  # 64 KB chunks
            if not chunk:
                break
            if should_cache and f_cache:
                await f_cache.write(chunk)
            yield chunk
            
        if f_cache:
            await f_cache.close()
            f_cache = None
            
        await process.wait()
        
        if process.returncode == 0 and should_cache:
            if os.path.exists(temp_cache_file):
                # Clean up if another thread finished first (extremely rare race condition)
                if os.path.exists(final_cache_file):
                    try: os.remove(temp_cache_file)
                    except Exception: pass
                else:
                    os.rename(temp_cache_file, final_cache_file)
                    logger.info(f"[Streaming Router] Dynamic transcode file successfully cached: {final_cache_file}")
        elif should_cache:
            if os.path.exists(temp_cache_file):
                try: os.remove(temp_cache_file)
                except Exception: pass
                
    except asyncio.CancelledError:
        logger.warning("[Streaming Router] Client disconnected from transcode stream. Killing process.")
        try:
            process.kill()
        except Exception:
            pass
        if f_cache:
            try: await f_cache.close()
            except Exception: pass
        if should_cache and os.path.exists(temp_cache_file):
            try: os.remove(temp_cache_file)
            except Exception: pass
        raise
    except Exception as e:
        logger.error(f"[Streaming Router] Exception in transcode generator: {e}")
        if f_cache:
            try: await f_cache.close()
            except Exception: pass
        if should_cache and os.path.exists(temp_cache_file):
            try: os.remove(temp_cache_file)
            except Exception: pass

@router.get("/{media_id}")
async def stream_media(
    media_id: str,
    quality: Optional[str] = Query(None), # "Source", "720p", "480p"
    start: float = Query(0.0), # Start seek point in seconds
    db: AsyncSession = Depends(get_session),
    user = Depends(get_current_user)
):
    """
    Streams media file dynamically. If quality matches Source, serves directly.
    If 720p/480p, streams an on-the-fly transcoded FFmpeg stream (or serves from cache).
    """
    video_url = None
    
    # 1. Resolve media_id to find video url
    if media_id.startswith("m_"):
        stmt = select(Movie).where(Movie.id == media_id)
        res = await db.execute(stmt)
        movie = res.scalars().first()
        if movie:
            video_url = movie.video_url
    elif media_id.startswith("ep_"):
        stmt = select(Episode).where(Episode.id == media_id)
        res = await db.execute(stmt)
        episode = res.scalars().first()
        if episode:
            video_url = episode.video_url
            
    if not video_url:
        raise HTTPException(status_code=404, detail="Media asset not found")
        
    # Remove leading slash if it exists
    rel_path = video_url.lstrip("/")
    
    # Resolve absolute path to the local video file
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    abs_path = os.path.join(base_dir, rel_path)
    
    if not os.path.exists(abs_path):
        # Fallback to check if it has a temp path or cloud directory (if mapping exists)
        raise HTTPException(status_code=404, detail=f"Media file not found on disk: {abs_path}")
        
    # 2. Check quality transcode requirements
    if not quality or quality == "Source":
        # Return static file response supporting ranges natively
        return FileResponse(abs_path, media_type="video/mp4")
        
    # 3. Transcode Cache Check
    cache_file = os.path.join(settings.TEMP_DIR, "transcode_cache", f"{media_id}_{quality}.mp4")
    if os.path.exists(cache_file):
        logger.info(f"[Streaming Router] Serving cached transcode file: {cache_file}")
        return FileResponse(cache_file, media_type="video/mp4")
        
    height = 720 if quality == "720p" else 480
    
    return StreamingResponse(
        transcode_generator(abs_path, height, start, media_id, quality),
        media_type="video/mp4",
        headers={
            "Content-Type": "video/mp4",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )
