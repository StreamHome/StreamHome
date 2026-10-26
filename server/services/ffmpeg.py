import os
from dataclasses import replace
import re
import time
import httpx
import shutil
import asyncio
import aiofiles
import traceback
import uuid
import subprocess
from contextlib import AsyncExitStack
from typing import Dict, Any, Optional
from services.state import update_task_metrics, remove_task_metrics, register_process, unregister_process, ACTIVE_PROCESSES
from services.logger import logger
from services.ingestion_errors import IngestionFailure, classify_failure, sanitize_url, write_task_diagnostics
from services.ffmpeg_input import ffmpeg_network_input_options, is_http_media_source, local_media_input_options, native_media_environment
from services.controlled_source import controlled_media_input
from services.ingestion_security import UnsafeIngestionSource, source_request_failure
from services.ingest_preview import ingest_preview_service
from lifecycle.ingestion_attempts import require_drained_writer
from services.media_timing import canonical_audio_filter_chain
from lifecycle.coordination import run_cancellation_safe_thread
from lifecycle.media_work import settle
from services.storage_capacity import StorageCapacityError, storage_capacity

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
    Admits the response bytes before a cancellation-safe atomic disk write.
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
                content = response.content
                try:
                    async with storage_capacity.allocation("metadata-artwork-download", len(content), temp_path) as reservation:
                        def publish_image():
                            with open(temp_path, "wb") as handle:
                                if handle.write(content) != len(content):
                                    raise OSError("Artwork write was incomplete")
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.replace(temp_path, dest_path)
                        _, cancelled = await settle(asyncio.to_thread(publish_image))
                        await storage_capacity.progress(reservation, len(content))
                        if cancelled:
                            raise asyncio.CancelledError
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
    except StorageCapacityError:
        raise
    except Exception as e:
        logger.error(f"[Metadata Cache] Exception occurred downloading metadata asset: {e}")
        
    return image_url

def _run_ffmpeg_sync(task_id: str, cmd: list, duration_secs: float, preview_enabled: bool = False, preview_id: Optional[str] = None) -> tuple[bool, str]:
    """Runs FFmpeg synchronously in a dedicated background thread, immune to asyncio loop resets."""
    stderr_lines = []
    process: subprocess.Popen[bytes] | None = None
    try:
        # Popen kullanarak süreci başlatıyoruz. Windows'ta ekstra CMD penceresi açılmasını engelliyoruz.
        process = subprocess.Popen(
            cmd,
            env=native_media_environment(),
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
                                preview_id or task_id,
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
        error_msg = "\n".join(stderr_lines)
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


def _cancel_registered_process(task_id: str) -> None:
    process = ACTIVE_PROCESSES.get(task_id)
    if process is None or process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        pass

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
    audio_timeline_offset: float = 0.0,
    client_address: str = "",
    audio_headers: Optional[Dict[str, str]] = None,
    attempt: int = 1,
    preview_id: Optional[str] = None,
) -> tuple[bool, Optional[IngestionFailure]]:
    """Give FFmpeg only scoped local inputs whose upstream fetches are pinned."""
    require_drained_writer(task_id)
    if preview_enabled and preview_id is None:
        preview_id = ingest_preview_service.next_generation(task_id)
    candidate = _ingestion_candidate_path(output_path, task_id)
    relays = []
    native_results = []
    try:
        async with AsyncExitStack() as inputs:
            selected_video = video_url
            selected_audio = audio_url
            if is_http_media_source(video_url):
                selected_video = await inputs.enter_async_context(controlled_media_input(
                    video_url, headers=headers, client_address=client_address, source_type=video_source_type,
                    task_id=task_id, input_role="video", attempt=attempt, owner_callback=relays.append,
                ))
            if audio_url and is_http_media_source(audio_url):
                selected_audio = await inputs.enter_async_context(controlled_media_input(
                    audio_url, headers=audio_headers if audio_headers is not None else headers,
                    client_address=client_address, source_type=audio_source_type,
                    task_id=task_id, input_role="audio", attempt=attempt, owner_callback=relays.append,
                ))
            success, failure = await _download_and_merge_controlled(
                task_id, selected_video, selected_audio, {}, output_path, duration_secs,
                video_source_type, audio_source_type, preview_enabled, audio_timeline_offset,
                preview_id=preview_id,
                source_failed=lambda: any(relay.failure is not None for relay in relays),
                native_result_callback=lambda diagnostics, preview: native_results.append((diagnostics, preview)),
            )
        # Every relay has settled and rejected any late source failure before the
        # candidate becomes a complete staging input for final media verification.
        if success:
            os.replace(candidate, os.path.abspath(output_path))
            if native_results and native_results[-1][1]:
                ingest_preview_service.mark_complete(preview_id)
            logger.info(f"[FFmpeg Service] Transfer validated for task {task_id}; final publication is pending.")
            update_task_metrics(task_id, 99.0, speed="Verifying media", eta="00:00:00")
        return success, failure
    except (UnsafeIngestionSource, httpx.HTTPError, TimeoutError) as error:
        if native_results and native_results[-1][0]:
            write_task_diagnostics(task_id, "native result before source rejection", native_results[-1][0])
        return False, source_request_failure(error)
    finally:
        if os.path.exists(candidate):
            os.remove(candidate)


def _ingestion_candidate_path(output_path: str, task_id: str) -> str:
    root, extension = os.path.splitext(os.path.abspath(output_path))
    return f"{root}.{task_id}.part{extension or '.mp4'}"


async def _download_and_merge_controlled(
    task_id: str,
    video_url: str,
    audio_url: Optional[str],
    headers: Dict[str, str],
    output_path: str,
    duration_secs: float,
    video_source_type: str = "auto",
    audio_source_type: str = "auto",
    preview_enabled: bool = True,
    audio_timeline_offset: float = 0.0,
    source_failed=None,
    native_result_callback=None,
    preview_id: Optional[str] = None,
) -> tuple[bool, Optional[IngestionFailure]]:
    """Download video/audio streams, merge losslessly, inject headers, track progress."""
    
    require_drained_writer(task_id)
    if preview_enabled and preview_id is None:
        preview_id = ingest_preview_service.next_generation(task_id)
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
    input_cmd.extend(ffmpeg_network_input_options(video_url, video_source_type) if is_video_http else local_media_input_options())
    input_cmd.extend(["-i", video_url])
    
    if audio_url:
        is_audio_http = is_http_media_source(audio_url)
        if headers_str.strip() and is_audio_http:
            input_cmd.extend(["-headers", headers_str])
        input_cmd.extend(ffmpeg_network_input_options(audio_url, audio_source_type) if is_audio_http else local_media_input_options())
        input_cmd.extend(["-i", audio_url])
        
    temp_output_path = _ingestion_candidate_path(abs_output_path, task_id)
    native_succeeded = False
    try:
        if os.path.exists(temp_output_path):
            os.remove(temp_output_path)
    except OSError:
        pass
    preview_dir = ingest_preview_service.prepare(preview_id, duration_secs) if preview_enabled else None

    def build_command(include_preview: bool) -> list[str]:
        command = list(input_cmd)
        if audio_url:
            audio_filters = canonical_audio_filter_chain(audio_timeline_offset, duration_secs)
            split = "asplit=2[preview_external_audio][final_external_audio]" if include_preview and preview_dir is not None else "anull[final_external_audio]"
            command.extend(["-filter_complex", f"[1:a:0]{','.join(audio_filters)},{split}"])
        if include_preview and preview_dir is not None:
            command.extend(["-map", "0:v:0"])
            command.extend(["-map", "[preview_external_audio]"] if audio_url else ["-map", "0:a:0?"])
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
                "-map", "[final_external_audio]",
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

    logger.info(f"[FFmpeg Service] Starting task {task_id} from {sanitize_url(video_url)}")
    
    try:
        # Senkron FFmpeg fonksiyonunu sunucuyu bloke etmemesi için ayrı bir iş parçacığına yolluyoruz
        success, diagnostics = await run_cancellation_safe_thread(
            _run_ffmpeg_sync,
            task_id,
            build_command(preview_enabled),
            duration_secs,
            preview_enabled,
            preview_id=preview_id,
            cancellation_callback=lambda: _cancel_registered_process(task_id),
        )
        preview_succeeded = bool(preview_enabled and success)

        if not success and preview_enabled and not (source_failed and source_failed()):
            require_drained_writer(task_id)
            preview_failure = classify_failure(diagnostics, "PREVIEW_FAILED")
            ingest_preview_service.mark_error(preview_id, preview_failure.code, preview_failure.message)
            if os.path.exists(temp_output_path):
                os.remove(temp_output_path)
            logger.warning(f"[FFmpeg Service] Preview branch failed for {task_id}; retrying ingestion without play-while-downloading output.")
            success, diagnostics = await run_cancellation_safe_thread(
                _run_ffmpeg_sync,
                task_id,
                build_command(False),
                duration_secs,
                False,
                cancellation_callback=lambda: _cancel_registered_process(task_id),
            )
        
        if success:
            native_succeeded = True
            if native_result_callback is not None:
                native_result_callback(diagnostics, preview_succeeded)
            logger.info(f"[FFmpeg Service] Native process exited successfully for task {task_id}; checking sources.")
            return True, None

        diagnostics_path = write_task_diagnostics(task_id, "ffmpeg", diagnostics)
        failure = classify_failure(diagnostics)
        failure = replace(failure, diagnostics_path=diagnostics_path, stage="download")
        update_task_metrics(task_id, 0.0, speed="Failed", eta="00:00:00")
        return False, failure
            
    except asyncio.CancelledError:
        logger.warning(f"[FFmpeg Service] Task {task_id} was cancelled/terminated.")
        process = ACTIVE_PROCESSES.get(task_id)
        if process:
            try:
                process.kill()
                if isinstance(process, subprocess.Popen):
                    await run_cancellation_safe_thread(process.wait)
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
        if os.path.exists(temp_output_path) and not native_succeeded:
            try:
                os.remove(temp_output_path)
            except OSError:
                pass
