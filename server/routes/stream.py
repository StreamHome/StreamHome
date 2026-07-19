import os
import shutil
import asyncio
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, status, Query, Request
from fastapi.responses import StreamingResponse, FileResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db import engine, get_session
from models import Movie, Episode, DownloadTask
from config import settings
from services.logger import logger
from services.rclone import rclone_service
from routes.auth import get_current_user

router = APIRouter(prefix="/api/stream", tags=["Streaming"])

import re
import json

ACTIVE_CLOUD_DOWNLOADS = set()

def get_rclone_path() -> Optional[str]:
    return rclone_service.executable()

async def download_file_from_cloud_task(target_remote: str, abs_path: str):
    try:
        if not get_rclone_path():
            logger.error(f"[Cloud Download] Rclone not found. Cannot download {target_remote} to {abs_path}")
            return
        logger.info("[Cloud Download] Starting protected background cache copy.")
        result = await rclone_service.copyto_atomic(target_remote, abs_path)
        if result.ok:
            logger.info(f"[Cloud Download] Successfully downloaded {target_remote} to local path {abs_path}!")
        else:
            logger.error(f"[Cloud Download] Rclone copy failed: {result.error_code or 'rclone_failed'}")
    except Exception as e:
        logger.error(f"[Cloud Download] Exception during download of {target_remote}: {e}")
    finally:
        ACTIVE_CLOUD_DOWNLOADS.discard(abs_path)

async def cloud_stream_generator(target_remote: str, start: int, count: Optional[int] = None):
    if not get_rclone_path():
        logger.error("[Cloud Streaming] Rclone binary not found. Cannot stream.")
        return
    arguments = ["cat", "--offset", str(start)]
    if count is not None and count > 0:
        arguments += ["--count", str(count)]
    arguments += [target_remote]
    logger.info("[Cloud Streaming] Starting protected Drive stream.")
    process = None
    try:
        process, chunks = await rclone_service.open_stream(*arguments)
        async for chunk in chunks:
            yield chunk
    except asyncio.CancelledError:
        logger.info("[Cloud Streaming] Client disconnected. Killing rclone cat subprocess.")
        if process and process.returncode is None:
            process.kill()
        raise
    except Exception as e:
        logger.error(f"[Cloud Streaming] Error in stream generator: {e}")

async def transcode_generator(input_path: str, height: int, start_sec: float, media_id: str, quality: str, audio_track_idx: int = 0, should_cache: bool = True):
    import aiofiles
    # Only cache if we're generating from the beginning
    if start_sec > 0:
        should_cache = False
        
    # Resolve ffmpeg binary path dynamically
    ffmpeg_path = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    
    # Cache path files setup
    cache_dir = os.path.join(settings.TEMP_DIR, "transcode_cache")
    os.makedirs(cache_dir, exist_ok=True)
    temp_cache_file = os.path.join(cache_dir, f"{media_id}_{quality}.mp4.tmp")
    final_cache_file = os.path.join(cache_dir, f"{media_id}_{quality}.mp4")
    
    original_input_path = input_path
    
    # If input path is not on disk, check if it's on cloud
    stdin_arg = None
    rclone_input_proc = None
    
    if not os.path.exists(input_path):
        media_idx = input_path.replace("\\", "/").find("/media/")
        if media_idx != -1:
            sub_path = input_path[media_idx + 7:].replace("\\", "/")
            target_remote = f"{settings.RCLONE_REMOTE_PATH}/{sub_path}"
            
            rclone_path = get_rclone_path()
            if rclone_path:
                rclone_cmd = rclone_service.command("cat", target_remote)
                logger.info("[Streaming Router] Opening protected cloud transcoding source.")
                rclone_input_proc = await asyncio.create_subprocess_exec(
                    *rclone_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdin_arg = rclone_input_proc.stdout
                input_path = "pipe:0"

    # Find the audio directory and locate the requested track
    audio_file_path = None
    parent_dir = os.path.dirname(original_input_path)
    audio_dir = os.path.join(parent_dir, "audio")
    
    # For cloud paths, the local audio dir might not exist yet. We can try to download the audio file synchronously.
    if input_path == "pipe:0":
        os.makedirs(audio_dir, exist_ok=True)
        # Try to resolve remote audio files
        # We can list the files in the remote 'audio' directory using rclone lsjson
        rclone_path = get_rclone_path()
        if rclone_path:
            media_idx = original_input_path.replace("\\", "/").find("/media/")
            if media_idx != -1:
                sub_path_dir = os.path.dirname(original_input_path[media_idx + 7:]).replace("\\", "/")
                remote_audio_dir = f"{settings.RCLONE_REMOTE_PATH}/{sub_path_dir}/audio"
                try:
                    proc_ls = await asyncio.create_subprocess_exec(
                        *rclone_service.command("lsjson", remote_audio_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout_ls, _ = await proc_ls.communicate()
                    if proc_ls.returncode == 0:
                        audio_data = json.loads(stdout_ls)
                        audio_files = sorted([item["Name"] for item in audio_data if item["Name"].endswith(".mp3")])
                        if audio_files:
                            idx = min(max(0, audio_track_idx), len(audio_files) - 1)
                            audio_filename = audio_files[idx]
                            audio_file_path = os.path.join(audio_dir, audio_filename)
                            
                            # Download the audio file synchronously if not on disk
                            if not os.path.exists(audio_file_path):
                                remote_audio_file = f"{remote_audio_dir}/{audio_filename}"
                                await rclone_service.copyto_atomic(remote_audio_file, audio_file_path)
                except Exception as e:
                    logger.error(f"[Streaming Router] Error checking/downloading cloud audio tracks: {e}")
    else:
        if os.path.exists(audio_dir):
            try:
                audio_files = sorted([f for f in os.listdir(audio_dir) if f.endswith(".mp3")])
                if audio_files:
                    idx = min(max(0, audio_track_idx), len(audio_files) - 1)
                    audio_file_path = os.path.join(audio_dir, audio_files[idx])
            except Exception:
                pass

    # Set dynamic bitrate ceilings based on requested quality
    maxrate = "1500k"
    bufsize = "3000k"
    crf = "26"
    audio_bitrate = "128k"
    
    if quality == "1080p":
        maxrate = "2500k"
        bufsize = "5000k"
        audio_bitrate = "96k"
    elif quality == "720p":
        maxrate = "1500k"
        bufsize = "3000k"
        audio_bitrate = "96k"
    elif quality == "480p":
        maxrate = "800k"
        bufsize = "1600k"
        audio_bitrate = "96k"
    elif quality == "360p":
        maxrate = "400k"
        bufsize = "800k"
        audio_bitrate = "64k"
    elif quality == "240p":
        maxrate = "250k"
        bufsize = "500k"
        audio_bitrate = "64k"

    # Build transcoding command
    if audio_file_path and os.path.exists(audio_file_path):
        if input_path == "pipe:0":
            cmd = [
                ffmpeg_path,
                "-y",
                "-i", "pipe:0",
                "-ss", str(start_sec),
                "-i", audio_file_path,
                "-vf", f"scale=-2:{height}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-crf", crf,
                "-maxrate", maxrate,
                "-bufsize", bufsize,
                "-c:a", "aac",
                "-b:a", audio_bitrate,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+faststart",
                "pipe:1"
            ]
        else:
            cmd = [
                ffmpeg_path,
                "-y",
                "-ss", str(start_sec),
                "-i", input_path,
                "-ss", str(start_sec),
                "-i", audio_file_path,
                "-vf", f"scale=-2:{height}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-crf", crf,
                "-maxrate", maxrate,
                "-bufsize", bufsize,
                "-c:a", "aac",
                "-b:a", audio_bitrate,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+faststart",
                "pipe:1"
            ]
    else:
        if input_path == "pipe:0":
            cmd = [
                ffmpeg_path,
                "-y",
                "-i", "pipe:0",
                "-ss", str(start_sec),
                "-vf", f"scale=-2:{height}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-crf", crf,
                "-maxrate", maxrate,
                "-bufsize", bufsize,
                "-c:a", "aac",
                "-b:a", audio_bitrate,
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+faststart",
                "pipe:1"
            ]
        else:
            cmd = [
                ffmpeg_path,
                "-y",
                "-ss", str(start_sec),
                "-i", input_path,
                "-vf", f"scale=-2:{height}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-crf", crf,
                "-maxrate", maxrate,
                "-bufsize", bufsize,
                "-c:a", "aac",
                "-b:a", audio_bitrate,
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
            stdin=stdin_arg,
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
        if rclone_input_proc:
            try: rclone_input_proc.kill()
            except Exception: pass
        if f_cache:
            try: await f_cache.close()
            except Exception: pass
        if should_cache and os.path.exists(temp_cache_file):
            try: os.remove(temp_cache_file)
            except Exception: pass
        raise
    except Exception as e:
        logger.error(f"[Streaming Router] Exception in transcode generator: {e}")
        if rclone_input_proc:
            try: rclone_input_proc.kill()
            except Exception: pass
        if f_cache:
            try: await f_cache.close()
            except Exception: pass
        if should_cache and os.path.exists(temp_cache_file):
            try: os.remove(temp_cache_file)
            except Exception: pass

@router.get("/{media_id}")
async def stream_media(
    media_id: str,
    request: Request,
    quality: Optional[str] = Query(None), # "Source", "720p", "480p"
    start: float = Query(0.0), # Start seek point in seconds
    audio_track: Optional[int] = Query(0), # Requested audio track index
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
        # Fallback: check if the file exists on the cloud
        if settings.STORAGE_ENGINE == "CLOUD" and rel_path.startswith("media/"):
            sub_path = rel_path[6:].replace('\\', '/')
            target_remote = f"{settings.RCLONE_REMOTE_PATH}/{sub_path}"
            
            # Start background copy if not already in progress
            if abs_path not in ACTIVE_CLOUD_DOWNLOADS:
                ACTIVE_CLOUD_DOWNLOADS.add(abs_path)
                asyncio.create_task(download_file_from_cloud_task(target_remote, abs_path))
            
            # Serve the cloud stream directly (using byte ranges if "Source" is requested)
            if not quality or quality == "Source":
                # Parse Range header
                range_header = request.headers.get("range")
                start_byte = 0
                end_byte = None
                
                # Fetch size from rclone
                file_size = 0
                rclone_path = get_rclone_path()
                if rclone_path:
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
                        logger.error(f"[Streaming Router] Error fetching file size from cloud: {e}")
                
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
                    "Content-Type": "video/mp4",
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

        # Fallback 2: Check if it's currently downloading and we can proxy the external URL
        tmdb_id_str = None
        if media_id.startswith("m_"):
            tmdb_id_str = media_id[2:]
        elif media_id.startswith("ep_"):
            parts = media_id.split("_")
            if len(parts) >= 2:
                tmdb_id_str = parts[1]
                
        if tmdb_id_str and tmdb_id_str.isdigit():
            tmdb_id = int(tmdb_id_str)
            stmt = select(DownloadTask).where(DownloadTask.tmdb_id == tmdb_id).order_by(DownloadTask.created_at.desc())
            res = await db.execute(stmt)
            task = res.scalars().first()
            
            if task and task.status in ["PENDING", "DOWNLOADING", "MERGING"] and task.video_url.startswith("http"):
                # Seamless Proxy to external URL
                if not quality or quality == "Source":
                    import httpx
                    range_header = request.headers.get("range")
                    proxy_headers = {}
                    if range_header:
                        proxy_headers["Range"] = range_header
                    
                    client = httpx.AsyncClient(follow_redirects=True)
                    req = client.build_request("GET", task.video_url, headers=proxy_headers)
                    try:
                        resp = await client.send(req, stream=True)
                    except Exception as e:
                        await client.aclose()
                        raise HTTPException(status_code=502, detail=f"Proxy error: {e}")
                        
                    resp_headers = {
                        "Accept-Ranges": "bytes",
                        "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
                    }
                    for h in ["Content-Length", "Content-Range"]:
                        if h in resp.headers:
                            resp_headers[h] = resp.headers[h]
                            
                    async def proxy_streamer():
                        try:
                            async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                                yield chunk
                        finally:
                            await resp.aclose()
                            await client.aclose()
                            
                    return StreamingResponse(
                        proxy_streamer(),
                        status_code=resp.status_code,
                        headers=resp_headers
                    )
                else:
                    # For transcoded streams, feed the external URL to FFmpeg
                    abs_path = task.video_url
            else:
                raise HTTPException(status_code=404, detail=f"Media file not found on disk: {abs_path}")
        else:
            raise HTTPException(status_code=404, detail=f"Media file not found on disk: {abs_path}")
        
    # 2. Check quality transcode requirements
    if not quality or quality == "Source":
        # Return static file response supporting ranges natively
        return FileResponse(abs_path, media_type="video/mp4")
        
    # 3. Transcode Cache Check
    cache_file = os.path.join(settings.TEMP_DIR, "transcode_cache", f"{media_id}_{quality}.mp4")
    if os.path.exists(cache_file):
        if start > 0:
            logger.info(f"[Streaming Router] Seeking into cached transcode file: {cache_file} at {start}s")
            # We must output an MP4 stream starting at `start` to satisfy the frontend's expectation
            ffmpeg_path = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
            cmd = [
                ffmpeg_path,
                "-y",
                "-ss", str(start),
                "-i", cache_file,
                "-c", "copy",
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+faststart",
                "pipe:1"
            ]
            async def cache_seek_generator():
                process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                )
                try:
                    while True:
                        chunk = await process.stdout.read(64 * 1024)
                        if not chunk: break
                        yield chunk
                finally:
                    try: process.kill()
                    except: pass
            
            return StreamingResponse(
                cache_seek_generator(),
                media_type="video/mp4",
                headers={"Content-Type": "video/mp4", "Cache-Control": "no-cache", "Connection": "keep-alive"}
            )
        else:
            logger.info(f"[Streaming Router] Serving cached transcode file: {cache_file}")
            return FileResponse(cache_file, media_type="video/mp4")
        
    height = 720
    if quality == "1080p": height = 1080
    elif quality == "720p": height = 720
    elif quality == "480p": height = 480
    elif quality == "360p": height = 360
    elif quality == "240p": height = 240
    
    return StreamingResponse(
        transcode_generator(abs_path, height, start, media_id, quality, audio_track),
        media_type="video/mp4",
        headers={
            "Content-Type": "video/mp4",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )
