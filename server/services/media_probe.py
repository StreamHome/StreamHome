import os
import shutil
import asyncio
import json
import subprocess
import httpx
from typing import Dict, Any, Optional
from config import settings
from services.logger import logger
from services.ingestion_errors import IngestionFailure, classify_failure, compact_diagnostics, sanitize_url, write_task_diagnostics

async def probe_media_stream(
    video_url: str,
    audio_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Probes remote or local video and audio streams using ffprobe to detect
    presence of video/audio streams and determine the resolution quality.
    """
    ffprobe_path = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    
    headers_str = ""
    if headers and isinstance(headers, dict) and len(headers) > 0:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

    async def run_ffprobe(url: str) -> Dict[str, Any]:
        if not url:
            return {"has_video": False, "has_audio": False, "height": 0, "failure": IngestionFailure("MISSING_SOURCE", "No media source URL was supplied.")}
            
        cmd = [ffprobe_path, "-v", "error", "-show_entries", "stream=codec_type,height,width", "-of", "json"]
        
        is_http = url.lower().startswith(("http://", "https://"))
        if headers_str.strip() and is_http:
            cmd.extend(["-headers", headers_str])
        if is_http:
            cmd.extend(["-protocol_whitelist", "http,https,tcp,tls,crypto,dns", "-allowed_extensions", "ALL", "-extension_picky", "0"])
        cmd.append(url)
        
        logger.info(f"[Media Probe] Probing source: {sanitize_url(url)[:120]}")
        
        try:
            # Run ffprobe process without opening a window on Windows
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="ignore").strip()
                diagnostics_path = write_task_diagnostics(task_id, "ffprobe", err_msg) if task_id else None
                failure = classify_failure(err_msg, "MEDIA_PROBE_FAILED")
                failure = IngestionFailure(failure.code, failure.message, failure.retryable, diagnostics_path)
                return {"has_video": False, "has_audio": False, "height": 0, "failure": failure}
                
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            streams = data.get("streams", [])
            
            has_video = any(s.get("codec_type") == "video" for s in streams)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            
            # Find the max height among video streams
            heights = [int(s.get("height", 0)) for s in streams if s.get("codec_type") == "video" and s.get("height")]
            max_height = max(heights) if heights else 0
            
            return {"has_video": has_video, "has_audio": has_audio, "height": max_height, "failure": None}
            
        except Exception as e:
            diagnostics_path = write_task_diagnostics(task_id, "ffprobe exception", repr(e)) if task_id else None
            failure = classify_failure(str(e), "MEDIA_PROBE_FAILED")
            failure = IngestionFailure(failure.code, failure.message, failure.retryable, diagnostics_path)
            return {"has_video": False, "has_audio": False, "height": 0, "failure": failure}

    # Run probes
    video_res = await run_ffprobe(video_url)
    audio_res = await run_ffprobe(audio_url) if audio_url else {"has_video": False, "has_audio": False, "height": 0, "failure": None}
    
    has_video = video_res["has_video"]
    has_audio = video_res["has_audio"] or audio_res["has_audio"]
    height = video_res["height"]
    failure = video_res.get("failure")
    if not failure and not has_video:
        failure = IngestionFailure("INVALID_MEDIA_SOURCE", "The media sender source contains no video stream.")
    if audio_url and not audio_res["has_audio"] and not failure:
        failure = audio_res.get("failure") or IngestionFailure("INVALID_AUDIO_SOURCE", "The separate audio source contains no audio stream.")
    
    # Map height to quality string
    quality = "Source"
    if has_video:
        if height >= 1080:
            quality = "1080p"
        elif height >= 720:
            quality = "720p"
        elif height >= 480:
            quality = "480p"
        elif height > 0:
            quality = f"{height}p"
    else:
        if has_audio:
            quality = "Audio Only"
            
    if not failure:
        logger.info(f"[Media Probe] Scan complete. Video: {has_video}, Audio: {has_audio}, Quality: {quality} (Height: {height})")
    
    return {
        "has_video": has_video,
        "has_audio": has_audio,
        "scan_quality": quality,
        "failure": failure,
    }

async def notify_video_sender(
    task_id: str,
    tmdb_id: int,
    has_video: bool,
    has_audio: bool,
    quality: str
) -> bool:
    """
    Sends a POST request to settings.VIDEO_SENDER_API_URL with details of
    the media scan. Returns True if successful, False otherwise.
    """
    url = settings.VIDEO_SENDER_API_URL
    if not url:
        return False
        
    payload = {
        "taskId": task_id,
        "tmdbId": tmdb_id,
        "hasVideo": has_video,
        "hasAudio": has_audio,
        "quality": quality
    }
    
    logger.info(f"[Media Probe] Dispatching media scan notification to sender: {sanitize_url(url)}")
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code in (200, 201, 204):
                logger.info(f"[Media Probe] Notification sent successfully. Status: {response.status_code}")
                return True
            else:
                logger.warning(f"[Media Probe] Sender API returned HTTP {response.status_code}: {compact_diagnostics(response.text, 160)}")
                return False
    except Exception as e:
        logger.warning(f"[Media Probe] Sender notification failed: {type(e).__name__}")
        return False
