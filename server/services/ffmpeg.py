import os
import re
import httpx
import shutil
import asyncio
import aiofiles
import traceback
import subprocess
from typing import Dict, Any, Optional
from services.state import update_task_metrics, remove_task_metrics, register_process, unregister_process, ACTIVE_PROCESSES
from services.logger import logger

# Regular expressions for parsing FFmpeg stderr progress
time_regex = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})")
speed_regex = re.compile(r"speed=\s*(\d+\.?\d*)x")
bitrate_regex = re.compile(r"bitrate=\s*(\d+\.?\d*)\s*kbits/s")

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
                async with aiofiles.open(dest_path, "wb") as f:
                    await f.write(response.content)
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

def _run_ffmpeg_sync(task_id: str, cmd: list, duration_secs: float) -> tuple[bool, str]:
    """Runs FFmpeg synchronously in a dedicated background thread, immune to asyncio loop resets."""
    logger.info(f"[FFmpeg Service] Running threaded exec command: {' '.join(cmd)}")
    stderr_lines = []
    try:
        # Popen kullanarak süreci başlatıyoruz. Windows'ta ekstra CMD penceresi açılmasını engelliyoruz.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="ignore",
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

        # Logları anlık olarak satır satır okuyup metrikleri güncelliyoruz
        for line in process.stderr:
            line = line.strip()
            if line:
                stderr_lines.append(line)
                if len(stderr_lines) > 15:
                    stderr_lines.pop(0)
            
            time_match = time_regex.search(line)
            if time_match:
                hours, minutes, seconds, centiseconds = map(float, time_match.groups())
                current_time = hours * 3600 + minutes * 60 + seconds + centiseconds / 100.0
                
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
        unregister_process(task_id)
        
        success = process.returncode == 0
        error_msg = "" if success else "\n".join(stderr_lines)
        return success, error_msg
        
    except Exception as e:
        logger.error(f"[FFmpeg Service] Thread exception for task {task_id}: {repr(e)}")
        traceback.print_exc()
        unregister_process(task_id)
        return False, f"Exception occurred during execution: {repr(e)}"

async def download_and_merge(
    task_id: str,
    video_url: str,
    audio_url: Optional[str],
    headers: Dict[str, str],
    output_path: str,
    duration_secs: float
) -> tuple[bool, str]:
    """Download video/audio streams, merge losslessly, inject headers, track progress."""
    
    abs_output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_output_path), exist_ok=True)
    
    headers_str = ""
    if headers and isinstance(headers, dict) and len(headers) > 0:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

    ffmpeg_path = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    cmd = [ffmpeg_path, "-y"]
    
    is_video_http = video_url.lower().startswith(("http://", "https://"))
    if headers_str.strip() and is_video_http:
        cmd.extend(["-headers", headers_str])
    if is_video_http:
        cmd.extend(["-protocol_whitelist", "http,https,tcp,tls,crypto,dns", "-allowed_extensions", "ALL", "-extension_picky", "0"])
    cmd.extend(["-i", video_url])
    
    if audio_url:
        is_audio_http = audio_url.lower().startswith(("http://", "https://"))
        if headers_str.strip() and is_audio_http:
            cmd.extend(["-headers", headers_str])
        if is_audio_http:
            cmd.extend(["-protocol_whitelist", "http,https,tcp,tls,crypto,dns", "-allowed_extensions", "ALL", "-extension_picky", "0"])
        # faststart eklendi
        cmd.extend(["-i", audio_url, "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart", "-map", "0:v:0", "-map", "1:a:0", "-shortest"])
    else:
        # faststart eklendi
        cmd.extend(["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-movflags", "+faststart"])
        
    cmd.append(abs_output_path)

    loop = asyncio.get_running_loop()
    
    try:
        # Senkron FFmpeg fonksiyonunu sunucuyu bloke etmemesi için ayrı bir iş parçacığına yolluyoruz
        success, error_reason = await loop.run_in_executor(None, _run_ffmpeg_sync, task_id, cmd, duration_secs)
        
        if success:
            logger.info(f"[FFmpeg Service] Task completed successfully: {task_id}")
            update_task_metrics(task_id, 100.0, speed="Finished", eta="00:00:00")
            return True, ""
        else:
            logger.error(f"[FFmpeg Service] Task failed: {task_id}. Error: {error_reason}")
            update_task_metrics(task_id, 0.0, speed="Failed", eta="00:00:00")
            return False, error_reason
            
    except asyncio.CancelledError:
        logger.warning(f"[FFmpeg Service] Task {task_id} was cancelled/terminated.")
        process = ACTIVE_PROCESSES.get(task_id)
        if process:
            try:
                process.kill()
            except Exception:
                pass
        unregister_process(task_id)
        update_task_metrics(task_id, 0.0, speed="Failed", eta="00:00:00")
        raise