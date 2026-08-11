import os
import re
import time
import httpx
import shutil
import asyncio
import aiofiles
import traceback
import uuid
import subprocess
from typing import Dict, Any, Optional
from services.state import update_task_metrics, remove_task_metrics, register_process, unregister_process, ACTIVE_PROCESSES
from services.logger import logger
from services.ingestion_errors import IngestionFailure, classify_failure, sanitize_url, write_task_diagnostics
from services.ffmpeg_input import ffmpeg_network_input_options, is_http_media_source
from services.ingest_preview import ingest_preview_service

# Regular expressions for parsing FFmpeg stderr progress
time_regex = re.compile(r"time=\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)")
speed_regex = re.compile(r"speed=\s*(\d+\.?\d*)x")
bitrate_regex = re.compile(r"bitrate=\s*(\d+\.?\d*)\s*kbits/s")


def _iter_ffmpeg_status_lines(stream: Any):
    """Read FFmpeg status in large chunks while honoring CR and LF delimiters."""

    buffer = bytearray()
    while chunk := stream.read(64 * 1024):
        buffer.extend(chunk)
        while True:
            delimiters = [position for position in (buffer.find(b"\r"), buffer.find(b"\n")) if position >= 0]
            if not delimiters:
                if len(buffer) > 64 * 1024:
                    del buffer[:-64 * 1024]
                break
            boundary = min(delimiters)
            line = bytes(buffer[:boundary]).decode("utf-8", errors="ignore").strip()
            del buffer[: boundary + 1]
            while buffer[:1] in (b"\r", b"\n"):
                del buffer[:1]
            if line:
                yield line
    if buffer:
        line = bytes(buffer).decode("utf-8", errors="ignore").strip()
        if line:
            yield line

async def download_and_cache_metadata_image(image_url: str, dest_path: str) -> Optional[str]:
    """
    Asynchronously downloads a remote TMDB image and caches it locally under dest_path.
    Uses aiofiles to perform non-blocking disk I/O.
    """
    if not image_url or not image_url.startswith("http"):
        return image_url
        
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    # Bypassing download if file already exists (for recovery mechanism)
    if os.path.exists(dest_path):
        server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        try:
            rel_path = os.path.relpath(dest_path, server_root)
            rel_url = "/" + rel_path.replace("\\", "/")
        except Exception:
            rel_url = "/" + dest_path.replace("\\", "/")
        return rel_url
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(image_url, timeout=15.0)
            if response.status_code == 200:
                temp_path = f"{dest_path}.{uuid.uuid4().hex}.part"
                try:
                    async with aiofiles.open(temp_path, "wb") as f:
                        await f.write(response.content)
                    os.replace(temp_path, dest_path)
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                try:
                    rel_path = os.path.relpath(dest_path, server_root)
                    rel_url = rel_path.replace("\\", "/")
                except Exception:
                    rel_url = dest_path.replace("\\", "/")
                if not rel_url.startswith("/"):
                    rel_url = "/" + rel_url
                return rel_url
            else:
                logger.error(f"[Metadata Cache] Fetch error {response.status_code} for URL: {image_url}")
    except Exception as e:
        logger.error(f"[Metadata Cache] Exception occurred downloading metadata asset: {e}")
        
    return image_url

def _run_ffmpeg_sync(task_id: str, cmd: list, duration_secs: float, preview_enabled: bool = False) -> tuple[bool, str]:
    """Runs FFmpeg synchronously in a dedicated background thread, immune to asyncio loop resets."""
    stderr_lines = []
    process: subprocess.Popen[bytes] | None = None
    try:
        # Popen kullanarak süreci başlatıyoruz. Windows'ta ekstra CMD penceresi açılmasını engelliyoruz.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0, # Unbuffered binary stream (Fix for Linux stdout/stderr buffering)
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        register_process(task_id, process)

        # Track download speed and size
        output_file = cmd[-1]
        last_size_check = time.time()
        last_size = 0
        if os.path.exists(output_file):
            try:
                last_size = os.path.getsize(output_file)
            except Exception:
                pass
        current_speed_str = "0 Mbps"

        assert process.stderr is not None
        for line in _iter_ffmpeg_status_lines(process.stderr):
            if line:
                stderr_lines.append(line)
                if len(stderr_lines) > 15:
                    stderr_lines.pop(0)
                
                time_match = time_regex.search(line)
                if time_match:
                    hours_str, minutes_str, seconds_str = time_match.groups()
                    current_time = float(hours_str) * 3600 + float(minutes_str) * 60 + float(seconds_str)
                    
                    base_duration = duration_secs if duration_secs > 0 else 3600.0
                    progress = min((current_time / base_duration) * 100.0, 99.9)
                    
                    speed_match = speed_regex.search(line)
                    speed_val_mult = float(speed_match.group(1)) if speed_match else 1.0
                    
                    eta = "00:00:00"
                    try:
                        if speed_val_mult > 0:
                            remaining_secs = (base_duration - current_time) / speed_val_mult
                            if remaining_secs > 0:
                                h = int(remaining_secs // 3600)
                                m = int((remaining_secs % 3600) // 60)
                                s = int(remaining_secs % 60)
                                eta = f"{h:02d}:{m:02d}:{s:02d}"
                    except Exception:
                        pass

                    if preview_enabled:
                        try:
                            ingest_preview_service.update_progress(
                                task_id,
                                processed_seconds=current_time,
                                speed_multiplier=speed_val_mult,
                                duration_seconds=duration_secs,
                            )
                        except OSError:
                            pass
                    
                    # Calculate real download speed from file size changes
                    now = time.time()
                    elapsed = now - last_size_check
                    current_size = 0
                    if os.path.exists(output_file):
                        try:
                            current_size = os.path.getsize(output_file)
                        except Exception:
                            pass
                    
                    if elapsed >= 0.5:
                        size_diff = max(0, current_size - last_size)
                        speed_bps = size_diff / elapsed if elapsed > 0 else 0
                        speed_mbps = (speed_bps * 8) / (1000**2)
                        speed_mbs = speed_bps / (1024**2)
                        
                        if speed_mbps >= 1000:
                            current_speed_str = f"{speed_mbps/1000:.1f} Gbps ({speed_mbs:.1f} MB/s)"
                        else:
                            current_speed_str = f"{speed_mbps:.1f} Mbps ({speed_mbs:.1f} MB/s)"
                            
                        last_size = current_size
                        last_size_check = now
                    
                    total_mb = current_size / (1024**2)
                    total_gb = current_size / (1024**3)
                    if total_gb >= 1.0:
                        size_str = f"{total_gb:.2f} GB"
                    else:
                        size_str = f"{total_mb:.1f} MB"
                    
                    update_task_metrics(task_id, progress, speed=current_speed_str, eta=eta, size=size_str)
                
        process.wait()
        
        success = process.returncode == 0
        error_msg = "" if success else "\n".join(stderr_lines)
        return success, error_msg
        
    except Exception as e:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        return False, f"Exception occurred during execution: {repr(e)}\n{traceback.format_exc()}"
    finally:
        if process is not None and process.stderr is not None:
            process.stderr.close()
        unregister_process(task_id)

async def download_and_merge(
    task_id: str,
    video_url: str,
    audio_url: Optional[str],
    headers: Dict[str, str],
    output_path: str,
    duration_secs: float,
    video_source_type: str = "auto",
    audio_source_type: str = "auto",
    preview_enabled: bool = True,
) -> tuple[bool, Optional[IngestionFailure]]:
    """Download video/audio streams, merge losslessly, inject headers, track progress."""
    
    abs_output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_output_path), exist_ok=True)
    
    headers_str = ""
    if headers and isinstance(headers, dict) and len(headers) > 0:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

    ffmpeg_path = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    input_cmd = [ffmpeg_path, "-y"]
    
    is_video_http = is_http_media_source(video_url)
    if headers_str.strip() and is_video_http:
        input_cmd.extend(["-headers", headers_str])
    input_cmd.extend(ffmpeg_network_input_options(video_url, video_source_type))
    input_cmd.extend(["-i", video_url])
    
    if audio_url:
        is_audio_http = is_http_media_source(audio_url)
        if headers_str.strip() and is_audio_http:
            input_cmd.extend(["-headers", headers_str])
        input_cmd.extend(ffmpeg_network_input_options(audio_url, audio_source_type))
        input_cmd.extend(["-i", audio_url])
        
    output_root, output_ext = os.path.splitext(abs_output_path)
    temp_output_path = f"{output_root}.{task_id}.part{output_ext or '.mp4'}"
    try:
        if os.path.exists(temp_output_path):
            os.remove(temp_output_path)
    except OSError:
        pass
    preview_dir = ingest_preview_service.prepare(task_id, duration_secs) if preview_enabled else None

    def build_command(include_preview: bool) -> list[str]:
        command = list(input_cmd)
        if include_preview and preview_dir is not None:
            command.extend(["-map", "0:v:0"])
            command.extend(["-map", "1:a:0"] if audio_url else ["-map", "0:a:0?"])
            command.extend([
                "-vf", "scale=-2:min(720\\,ih)",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-profile:v", "high",
                "-pix_fmt", "yuv420p",
                "-crf", "22",
                "-sc_threshold", "0",
                "-force_key_frames", "expr:gte(t,n_forced*4)",
                "-c:a", "aac",
                "-b:a", "160k",
                "-ac", "2",
                "-f", "hls",
                "-hls_time", "4",
                "-hls_playlist_type", "event",
                "-hls_flags", "independent_segments+temp_file",
                "-hls_segment_type", "fmp4",
                "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_filename", str(preview_dir / "segment_%05d.m4s"),
                str(preview_dir / "playlist.m3u8"),
            ])
        if audio_url:
            command.extend([
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map", "1:a:0",
                "-map", "0:s?",
                "-map_metadata", "0",
                "-map_chapters", "0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-c:s", "mov_text",
                "-movflags", "+faststart",
            ])
        else:
            command.extend([
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map", "0:s?",
                "-map_metadata", "0",
                "-map_chapters", "0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-c:s", "mov_text",
                "-movflags", "+faststart",
            ])
        command.append(temp_output_path)
        return command

    # Initialize metrics immediately to show 0.0% progress in CLI TUI
    update_task_metrics(task_id, 0.0, speed="Connecting...", eta="00:00:00", size="0 MB", force_write=True)

    loop = asyncio.get_running_loop()
    logger.info(f"[FFmpeg Service] Starting task {task_id} from {sanitize_url(video_url)}")
    
    try:
        # Senkron FFmpeg fonksiyonunu sunucuyu bloke etmemesi için ayrı bir iş parçacığına yolluyoruz
        success, diagnostics = await loop.run_in_executor(
            None,
            _run_ffmpeg_sync,
            task_id,
            build_command(preview_enabled),
            duration_secs,
            preview_enabled,
        )
        preview_succeeded = bool(preview_enabled and success)

        if not success and preview_enabled:
            preview_failure = classify_failure(diagnostics, "PREVIEW_FAILED")
            ingest_preview_service.mark_error(task_id, preview_failure.code, preview_failure.message)
            if os.path.exists(temp_output_path):
                os.remove(temp_output_path)
            logger.warning(f"[FFmpeg Service] Preview branch failed for {task_id}; retrying ingestion without play-while-downloading output.")
            success, diagnostics = await loop.run_in_executor(
                None,
                _run_ffmpeg_sync,
                task_id,
                build_command(False),
                duration_secs,
                False,
            )
        
        if success:
            os.replace(temp_output_path, abs_output_path)
            if preview_succeeded and ingest_preview_service.playlist_path(task_id).is_file():
                ingest_preview_service.mark_complete(task_id)
            logger.info(f"[FFmpeg Service] Task completed successfully: {task_id}")
            update_task_metrics(task_id, 100.0, speed="Finished", eta="00:00:00")
            return True, None

        diagnostics_path = write_task_diagnostics(task_id, "ffmpeg", diagnostics)
        failure = classify_failure(diagnostics)
        failure = IngestionFailure(failure.code, failure.message, failure.retryable, diagnostics_path)
        update_task_metrics(task_id, 0.0, speed="Failed", eta="00:00:00")
        return False, failure
            
    except asyncio.CancelledError:
        logger.warning(f"[FFmpeg Service] Task {task_id} was cancelled/terminated.")
        process = ACTIVE_PROCESSES.get(task_id)
        if process:
            try:
                process.kill()
                if isinstance(process, subprocess.Popen):
                    await loop.run_in_executor(None, process.wait)
                else:
                    await process.wait()
            except Exception:
                pass
        unregister_process(task_id)
        update_task_metrics(task_id, 0.0, speed="Failed", eta="00:00:00")
        raise
    except Exception as exc:
        diagnostics_path = write_task_diagnostics(task_id, "ffmpeg wrapper", traceback.format_exc())
        failure = IngestionFailure("FFMPEG_EXECUTION_FAILED", str(exc) or "FFmpeg could not process the source.", False, diagnostics_path)
        update_task_metrics(task_id, 0.0, speed="Failed", eta="00:00:00")
        return False, failure
    finally:
        if os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
            except OSError:
                pass
