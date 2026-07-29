import os
import shutil
import asyncio
import json
import subprocess
import httpx
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional
from config import settings
from services.logger import logger
from services.languages import language_label, normalize_language_tag
from services.media_source import EXTERNAL_AUDIO_EXTENSIONS, bind_audio_fingerprint, local_media_identity
from services.ingestion_errors import IngestionFailure, classify_failure, compact_diagnostics, sanitize_url, write_task_diagnostics
from services.ffmpeg_input import (
    ffmpeg_network_input_options,
    is_hls_media_source,
    is_http_media_source,
    normalize_source_type,
)


HLS_FALLBACK_FAILURES = {"INVALID_MEDIA_SOURCE", "MEDIA_PROBE_FAILED", "FFMPEG_OPTION_UNSUPPORTED"}

async def probe_media_stream(
    video_url: str,
    audio_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    task_id: Optional[str] = None,
    video_source_type: str = "auto",
    audio_source_type: str = "auto",
) -> Dict[str, Any]:
    """
    Probes remote or local video and audio streams using ffprobe to detect
    presence of video/audio streams and determine the resolution quality.
    """
    ffprobe_path = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    
    headers_str = ""
    if headers and isinstance(headers, dict) and len(headers) > 0:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

    async def run_ffprobe(url: str, source_type: str) -> Dict[str, Any]:
        normalized_source_type = normalize_source_type(source_type)
        if not url:
            return {
                "has_video": False,
                "has_audio": False,
                "height": 0,
                "source_type": normalized_source_type,
                "diagnostics": "No media source URL was supplied.",
                "failure": IngestionFailure("MISSING_SOURCE", "No media source URL was supplied."),
            }
            
        cmd = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,height,width:format=format_name",
            "-of",
            "json",
        ]
        
        is_http = is_http_media_source(url)
        if headers_str.strip() and is_http:
            cmd.extend(["-headers", headers_str])
        cmd.extend(ffmpeg_network_input_options(url, normalized_source_type))
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
                failure = classify_failure(err_msg, "MEDIA_PROBE_FAILED")
                return {
                    "has_video": False,
                    "has_audio": False,
                    "height": 0,
                    "source_type": normalized_source_type,
                    "diagnostics": err_msg,
                    "failure": failure,
                }
                
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            streams = data.get("streams", [])
            
            has_video = any(s.get("codec_type") == "video" for s in streams)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            format_name = str((data.get("format") or {}).get("format_name") or "").lower()
            detected_source_type = (
                "hls"
                if normalized_source_type == "hls"
                or is_hls_media_source(url)
                or "hls" in {part.strip() for part in format_name.split(",")}
                else "auto"
            )
            
            # Find the max height among video streams
            heights = [int(s.get("height", 0)) for s in streams if s.get("codec_type") == "video" and s.get("height")]
            max_height = max(heights) if heights else 0
            
            return {
                "has_video": has_video,
                "has_audio": has_audio,
                "height": max_height,
                "source_type": detected_source_type,
                "diagnostics": "",
                "failure": None,
            }
            
        except Exception as e:
            failure = classify_failure(str(e), "MEDIA_PROBE_FAILED")
            return {
                "has_video": False,
                "has_audio": False,
                "height": 0,
                "source_type": normalized_source_type,
                "diagnostics": repr(e),
                "failure": failure,
            }

    async def probe_source(url: str, source_type: str, expected_stream: str) -> Dict[str, Any]:
        normalized_source_type = normalize_source_type(source_type)
        result = await run_ffprobe(url, normalized_source_type)
        expected_found = bool(result.get(f"has_{expected_stream}"))
        failure = result.get("failure")
        should_force_hls = (
            normalized_source_type == "auto"
            and is_http_media_source(url)
            and not expected_found
            and (not failure or failure.code in HLS_FALLBACK_FAILURES)
        )
        if not should_force_hls:
            return result

        logger.info(f"[Media Probe] Retrying ambiguous HTTP source as an HLS manifest: {sanitize_url(url)[:120]}")
        forced_result = await run_ffprobe(url, "hls")
        if forced_result.get(f"has_{expected_stream}") and not forced_result.get("failure"):
            return forced_result
        forced_failure = forced_result.get("failure")
        if forced_failure and forced_failure.code not in HLS_FALLBACK_FAILURES:
            return forced_result
        return result

    # Run probes
    video_res = await probe_source(video_url, video_source_type, "video")
    audio_res = (
        await probe_source(audio_url, audio_source_type, "audio")
        if audio_url
        else {"has_video": False, "has_audio": False, "height": 0, "source_type": "auto", "diagnostics": "", "failure": None}
    )
    
    has_video = video_res["has_video"]
    has_audio = video_res["has_audio"] or audio_res["has_audio"]
    height = video_res["height"]
    failure = video_res.get("failure")
    if not failure and not has_video:
        failure = IngestionFailure("INVALID_MEDIA_SOURCE", "The media sender source contains no video stream.")
    if audio_url and not audio_res["has_audio"] and not failure:
        failure = audio_res.get("failure") or IngestionFailure("INVALID_AUDIO_SOURCE", "The separate audio source contains no audio stream.")
    if failure and task_id:
        failure_result = video_res if video_res.get("failure") else audio_res
        diagnostics = str(failure_result.get("diagnostics") or "").strip()
        if diagnostics:
            diagnostics_path = write_task_diagnostics(task_id, "ffprobe", diagnostics)
            failure = IngestionFailure(failure.code, failure.message, failure.retryable, diagnostics_path)
    
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
        "video_source_type": video_res.get("source_type", normalize_source_type(video_source_type)),
        "audio_source_type": audio_res.get("source_type", normalize_source_type(audio_source_type)),
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
            lang = normalize_language_tag(tags.get("language"))
            channels = int(a.get("channels") or 2)
            audio_meta.append({
                "index": idx,
                "streamIndex": int(a.get("index", idx)),
                "codec": a.get("codec_name", ""),
                "language": lang,
                "label": language_label(lang, tags.get("title")),
                "channels": channels,
                "default": bool((a.get("disposition") or {}).get("default")),
            })

        # External application-owned dubbing is authoritative for its language even
        # when the MP4 has embedded audio. Otherwise one embedded default track hides
        # valid audio/eng.*, audio/tur.*, and other standard language-tagged files.
        audio_dir = os.path.join(os.path.dirname(file_path), "audio")
        if os.path.isdir(audio_dir):
            external_files = sorted(
                path for path in (os.path.join(audio_dir, name) for name in os.listdir(audio_dir))
                if os.path.isfile(path) and os.path.splitext(path)[1].lower() in EXTERNAL_AUDIO_EXTENSIONS
            )
            embedded_by_language = {str(item.get("language") or "und"): item for item in audio_meta}
            external_languages: set[str] = set()
            merged_audio = list(audio_meta)
            for external_path in external_files:
                language = normalize_language_tag(os.path.splitext(os.path.basename(external_path))[0])
                if language in external_languages:
                    continue
                external_languages.add(language)
                existing = embedded_by_language.get(language)
                file_stat = os.stat(external_path)
                external_item = {
                    "index": 0,
                    "streamIndex": 0,
                    "codec": os.path.splitext(external_path)[1].lstrip(".").lower(),
                    "language": language,
                    "label": language_label(language, existing.get("label") if existing else None),
                    "channels": int(existing.get("channels") or 2) if existing else 2,
                    "default": bool(existing.get("default")) if existing else False,
                    "source": "external",
                    "fileName": os.path.basename(external_path),
                    "fileSize": file_stat.st_size,
                    "modifiedAt": file_stat.st_mtime_ns,
                }
                merged_audio = [item for item in merged_audio if str(item.get("language") or "und") != language]
                merged_audio.append(external_item)
            audio_meta = merged_audio

        if audio_meta and not any(item.get("default") for item in audio_meta):
            audio_meta[0]["default"] = True
        for index, item in enumerate(audio_meta):
            item["index"] = index
            
        base_fingerprint = hashlib.sha256(local_media_identity(Path(file_path)).encode("utf-8")).hexdigest()[:32]
        source_fingerprint = bind_audio_fingerprint(base_fingerprint, audio_meta)
        
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


async def probe_cloud_external_audio(cloud_video_path: str, embedded_audio: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge application-owned Drive audio sidecars into retained track metadata."""

    from services.rclone import rclone_service

    remote_parent = cloud_video_path.rsplit("/", 1)[0]
    result = await rclone_service.run("lsjson", f"{remote_parent}/audio", "--files-only", timeout=30)
    if not result.ok or not result.stdout.strip():
        return embedded_audio
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return embedded_audio
    if not isinstance(payload, list):
        return embedded_audio

    merged = [dict(item) for item in embedded_audio if str(item.get("source") or "embedded") != "external"]
    embedded_by_language = {str(item.get("language") or "und"): item for item in merged}
    external_languages: set[str] = set()
    for item in sorted(payload, key=lambda value: str(value.get("Name") or value.get("Path") or "").lower()):
        file_name = str(item.get("Name") or item.get("Path") or "").rsplit("/", 1)[-1]
        extension = os.path.splitext(file_name)[1].lower()
        if not file_name or file_name in {".", ".."} or "/" in file_name or "\\" in file_name or extension not in EXTERNAL_AUDIO_EXTENSIONS:
            continue
        language = normalize_language_tag(os.path.splitext(file_name)[0])
        if language in external_languages:
            continue
        external_languages.add(language)
        existing = embedded_by_language.get(language)
        merged = [entry for entry in merged if str(entry.get("language") or "und") != language]
        merged.append({
            "index": 0,
            "streamIndex": 0,
            "codec": extension.lstrip("."),
            "language": language,
            "label": language_label(language, existing.get("label") if existing else None),
            "channels": int(existing.get("channels") or 2) if existing else 2,
            "default": bool(existing.get("default")) if existing else False,
            "source": "external",
            "fileName": file_name,
            "fileSize": int(item.get("Size") or 0),
            "modifiedAt": str(item.get("ModTime") or ""),
        })
    if merged and not any(item.get("default") for item in merged):
        merged[0]["default"] = True
    for index, item in enumerate(merged):
        item["index"] = index
    return merged
