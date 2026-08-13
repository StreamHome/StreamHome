import os
import shutil
import asyncio
import json
import subprocess
import httpx
from pathlib import Path
from typing import Dict, Any, Optional
from config import settings
from services.logger import logger
from services.languages import language_label, normalize_language_tag
from services.media_source import EXTERNAL_AUDIO_EXTENSIONS, local_playback_fingerprint, local_video_fingerprint
from services.ingestion_errors import IngestionFailure, classify_failure, compact_diagnostics, sanitize_url, write_task_diagnostics
from services.ffmpeg_input import (
    ffmpeg_network_input_options,
    is_hls_media_source,
    is_http_media_source,
    normalize_source_type,
)
from services.state import register_process, unregister_process


HLS_FALLBACK_FAILURES = {"INVALID_MEDIA_SOURCE", "MEDIA_PROBE_FAILED", "FFMPEG_OPTION_UNSUPPORTED"}


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed and abs(parsed) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _probe_local_audio_timing(file_path: str) -> dict[str, Any]:
    ffprobe_path = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    try:
        result = subprocess.run(
            [
                ffprobe_path, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=start_time,duration,time_base:format=duration",
                "-of", "json", file_path,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            return {}
        payload = json.loads(result.stdout)
        stream = next(iter(payload.get("streams") or []), {})
        return {
            "startTime": _finite_float(stream.get("start_time")),
            "duration": max(0.0, _finite_float(stream.get("duration"), _finite_float((payload.get("format") or {}).get("duration")))),
            "timeBase": str(stream.get("time_base") or ""),
        }
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def merge_local_external_audio(
    file_path: str,
    retained_audio: list[dict[str, Any]],
    video_start_time: float = 0.0,
) -> list[dict[str, Any]]:
    """Refresh application-owned local dubbing sidecars without re-probing the video."""

    embedded_audio = [dict(item) for item in retained_audio if str(item.get("source") or "embedded").lower() != "external"]
    audio_dir = os.path.join(os.path.dirname(file_path), "audio")
    if not os.path.isdir(audio_dir):
        refreshed = embedded_audio
    else:
        external_files = sorted(
            path for path in (os.path.join(audio_dir, name) for name in os.listdir(audio_dir))
            if os.path.isfile(path) and os.path.splitext(path)[1].lower() in EXTERNAL_AUDIO_EXTENSIONS
        )
        embedded_by_language = {str(item.get("language") or "und"): item for item in embedded_audio}
        retained_external_by_name = {
            str(item.get("fileName") or ""): item
            for item in retained_audio
            if str(item.get("source") or "embedded").lower() == "external"
        }
        external_languages: set[str] = set()
        refreshed = list(embedded_audio)
        for external_path in external_files:
            language = normalize_language_tag(os.path.splitext(os.path.basename(external_path))[0])
            if language in external_languages:
                continue
            external_languages.add(language)
            existing = embedded_by_language.get(language)
            file_stat = os.stat(external_path)
            retained = retained_external_by_name.get(os.path.basename(external_path), {})
            timing = (
                retained
                if int(retained.get("fileSize") or -1) == file_stat.st_size
                and int(retained.get("modifiedAt") or -1) == file_stat.st_mtime_ns
                and "timelineOffset" in retained
                and "duration" in retained
                else _probe_local_audio_timing(external_path)
            )
            start_time = _finite_float(timing.get("startTime"))
            external_item = {
                "index": 0,
                "streamIndex": 0,
                "codec": os.path.splitext(external_path)[1].lstrip(".").lower(),
                "language": language,
                "label": language_label(language, existing.get("label") if existing else None),
                "channels": int(existing.get("channels") or 2) if existing else 2,
                "default": False if existing else not refreshed,
                "source": "external",
                "fileName": os.path.basename(external_path),
                "fileSize": file_stat.st_size,
                "modifiedAt": file_stat.st_mtime_ns,
                "startTime": start_time,
                "duration": max(0.0, _finite_float(timing.get("duration"))),
                "timeBase": str(timing.get("timeBase") or ""),
                "timelineOffset": start_time - video_start_time,
            }
            refreshed.append(external_item)

    if refreshed and not any(item.get("default") for item in refreshed):
        refreshed[0]["default"] = True
    for index, item in enumerate(refreshed):
        item["index"] = index
    return refreshed


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
            "stream=codec_type,height,width,start_time,duration,time_base:format=format_name,duration",
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
            process_key = f"source-probe:{id(process)}"
            register_process(process_key, process)
            try:
                stdout, stderr = await process.communicate()
            finally:
                unregister_process(process_key)
            
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
            video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
            audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
            
            return {
                "has_video": has_video,
                "has_audio": has_audio,
                "height": max_height,
                "source_type": detected_source_type,
                "diagnostics": "",
                "failure": None,
                "start_time": _finite_float((video_stream if has_video else audio_stream).get("start_time")),
                "duration": max(0.0, _finite_float((video_stream if has_video else audio_stream).get("duration"), _finite_float((data.get("format") or {}).get("duration")))),
                "time_base": str((video_stream if has_video else audio_stream).get("time_base") or ""),
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
        "video_start_time": _finite_float(video_res.get("start_time")),
        "audio_start_time": _finite_float(audio_res.get("start_time")),
        "audio_timeline_offset": _finite_float(audio_res.get("start_time")) - _finite_float(video_res.get("start_time")) if audio_url else 0.0,
        "video_duration": max(0.0, _finite_float(video_res.get("duration"))),
        "audio_duration": max(0.0, _finite_float(audio_res.get("duration"))),
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
        "-show_entries", "format=duration,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,start_time,duration,time_base:stream_tags=language,title:stream_disposition=default",
        "-of", "json", file_path
    ]
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        process_key = f"catalog-probe:{id(process)}"
        register_process(process_key, process)
        try:
            stdout, stderr = await process.communicate()
        finally:
            unregister_process(process_key)
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
        video_start_time = _finite_float(video_streams[0].get("start_time")) if video_streams else 0.0
        
        audio_meta = []
        for idx, a in enumerate(audio_streams):
            tags = a.get("tags", {})
            lang = normalize_language_tag(tags.get("language"))
            channels = int(a.get("channels") or 2)
            start_time = _finite_float(a.get("start_time"))
            audio_meta.append({
                "index": idx,
                "streamIndex": int(a.get("index", idx)),
                "codec": a.get("codec_name", ""),
                "language": lang,
                "label": language_label(lang, tags.get("title")),
                "channels": channels,
                "default": bool((a.get("disposition") or {}).get("default")),
                "startTime": start_time,
                "duration": max(0.0, _finite_float(a.get("duration"), duration)),
                "timeBase": str(a.get("time_base") or ""),
                "timelineOffset": start_time - video_start_time,
            })

        # Explicit external dubbing coexists with embedded tracks. Presentation order
        # must not erase the source stream identity used by FFmpeg mapping.
        audio_meta = merge_local_external_audio(file_path, audio_meta, video_start_time)
        stream_manifest = [
            {
                "index": int(stream.get("index", position)),
                "type": str(stream.get("codec_type") or ""),
                "codec": str(stream.get("codec_name") or ""),
                "language": normalize_language_tag((stream.get("tags") or {}).get("language")),
                "title": str((stream.get("tags") or {}).get("title") or ""),
                "default": bool((stream.get("disposition") or {}).get("default")),
                "startTime": _finite_float(stream.get("start_time")),
                "duration": max(0.0, _finite_float(stream.get("duration"), duration)),
                "timeBase": str(stream.get("time_base") or ""),
            }
            for position, stream in enumerate(streams)
        ]
            
        media_path = Path(file_path)
        source_fingerprint = local_playback_fingerprint(media_path, audio_meta)
        video_fingerprint = local_video_fingerprint(media_path)
        
        return {
            "probed_duration": duration,
            "container": container,
            "codec": codec,
            "width": width,
            "height": height,
            "frame_rate": frame_rate,
            "source_fingerprint": source_fingerprint,
            "video_fingerprint": video_fingerprint,
            "audio_metadata": audio_meta,
            "stream_manifest": stream_manifest,
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
            "startTime": 0.0,
            "duration": 0.0,
            "timeBase": "",
            "timelineOffset": 0.0,
        })
    if merged and not any(item.get("default") for item in merged):
        merged[0]["default"] = True
    for index, item in enumerate(merged):
        item["index"] = index
    return merged
