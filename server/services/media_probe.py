import os
import shutil
import asyncio
import json
import subprocess
import httpx
import hashlib
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

async def probe_completed_media(file_path: str) -> Dict[str, Any]:
    """
    Probes completed local media file with FFprobe to extract detailed information:
    duration, container format, video codec, resolution width & height, frame rate,
    source fingerprint, and audio track metadata.
    """
    ffprobe_path = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    if not os.path.exists(file_path):
        logger.warning(f"[Media Probe] Completed media file not found for probing: {file_path}")
        return {}
    
    cmd = [
        ffprobe_path, "-v", "error",
        "-show_entries", "format=duration,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate,channels:stream_tags=language,title:stream_disposition=default",
        "-of", "json", file_path
    ]
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(f"[Media Probe] Failed to probe completed media {file_path}: {stderr.decode('utf-8', errors='ignore')}")
            return {}
            
        data = json.loads(stdout.decode("utf-8", errors="ignore"))
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        
        duration = float(fmt.get("duration") or 0.0)
        container = fmt.get("format_name", "")
        
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        
        codec = ""
        width = 0
        height = 0
        frame_rate = 0.0
        
        if video_streams:
            v = video_streams[0]
            codec = v.get("codec_name", "")
            width = int(v.get("width") or 0)
            height = int(v.get("height") or 0)
            
            r_fr = v.get("r_frame_rate", "")
            if "/" in r_fr:
                try:
                    num, den = map(float, r_fr.split("/"))
                    if den > 0:
                        frame_rate = round(num / den, 3)
                except Exception:
                    pass
            else:
                try:
                    frame_rate = float(r_fr)
                except ValueError:
                    pass
        
        audio_meta = []
        for idx, a in enumerate(audio_streams):
            tags = a.get("tags", {})
            lang = tags.get("language", "und").lower()
            channels = int(a.get("channels") or 2)
            audio_meta.append({
                "index": idx,
                "streamIndex": int(a.get("index", idx)),
                "codec": a.get("codec_name", ""),
                "language": lang,
                "label": tags.get("title") or lang.upper(),
                "channels": channels,
                "default": bool((a.get("disposition") or {}).get("default")),
            })

        # Older StreamHome catalogs may contain a silent video plus language-labelled
        # audio files in the sibling audio directory. Treat those as real tracks only
        # when the container itself has no audio, avoiding duplicate playlists for
        # modern files that retain their embedded default track.
        if not audio_meta:
            audio_dir = os.path.join(os.path.dirname(file_path), "audio")
            supported_audio = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
            if os.path.isdir(audio_dir):
                external_files = sorted(
                    path for path in (os.path.join(audio_dir, name) for name in os.listdir(audio_dir))
                    if os.path.isfile(path) and os.path.splitext(path)[1].lower() in supported_audio
                )
                for idx, external_path in enumerate(external_files):
                    language = os.path.splitext(os.path.basename(external_path))[0].lower() or "und"
                    audio_meta.append({
                        "index": idx,
                        "streamIndex": 0,
                        "codec": os.path.splitext(external_path)[1].lstrip(".").lower(),
                        "language": language,
                        "label": language.upper(),
                        "channels": 2,
                        "default": idx == 0,
                    })
            
        stat = os.stat(file_path)
        val = f"{stat.st_size}_{stat.st_mtime}"
        source_fingerprint = hashlib.md5(val.encode()).hexdigest()
        
        return {
            "probed_duration": duration,
            "container": container,
            "codec": codec,
            "width": width,
            "height": height,
            "frame_rate": frame_rate,
            "source_fingerprint": source_fingerprint,
            "audio_metadata": audio_meta
        }
    except Exception as e:
        logger.error(f"[Media Probe] Exception probing completed media {file_path}: {e}")
        return {}
