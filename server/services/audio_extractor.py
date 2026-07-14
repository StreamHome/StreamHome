import os
import json
import shutil
import asyncio
import subprocess
from typing import List, Dict, Any
from services.logger import logger

def get_ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"

def get_ffprobe_path() -> str:
    return shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"

async def extract_audio_and_strip_video(video_path: str, default_lang: str = "en") -> List[str]:
    """
    Probes the video file for audio streams. Extracts each stream to a separate MP3 file
    inside an 'audio' folder next to the video file. Removes all audio from the original
    video file to leave it silent. Returns a list of language codes extracted.
    """
    if not os.path.exists(video_path):
        logger.warning(f"[Audio Extractor] File not found: {video_path}")
        return []

    ffprobe = get_ffprobe_path()
    ffmpeg = get_ffmpeg_path()
    
    # 1. Probe for audio streams
    cmd_probe = [
        ffprobe, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index:tags=language",
        "-of", "json", video_path
    ]
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_probe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"[Audio Extractor] Probe failed: {stderr.decode('utf-8', errors='ignore')}")
            return []
            
        probe_data = json.loads(stdout.decode('utf-8', errors='ignore'))
    except Exception as e:
        logger.error(f"[Audio Extractor] Probe exception: {e}")
        return []

    streams = probe_data.get("streams", [])
    if not streams:
        logger.info(f"[Audio Extractor] No audio streams found in {video_path}. Already stripped or silent.")
        return []

    # 2. Prepare folders
    dir_name = os.path.dirname(video_path)
    audio_dir = os.path.join(dir_name, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    
    languages: List[str] = []
    logger.info(f"[Audio Extractor] Found {len(streams)} audio streams. Extracting...")
    
    # 3. Extract each audio stream
    for idx, stream in enumerate(streams):
        tags = stream.get("tags", {})
        lang = tags.get("language", "").lower()
        # Clean language name (should be short code like 'en', 'tr', etc.)
        lang = "".join(c for c in lang if c.isalnum())
        if not lang or lang == "und":
            lang = default_lang if idx == 0 else f"track_{idx}"
            
        # Avoid file conflicts (e.g. two english tracks)
        base_lang = lang
        dup_count = 1
        while lang in languages or os.path.exists(os.path.join(audio_dir, f"{lang}.mp3")):
            lang = f"{base_lang}_{dup_count}"
            dup_count += 1
            
        audio_out_path = os.path.join(audio_dir, f"{lang}.mp3")
        
        # ffmpeg extract command: -map 0:a:idx
        cmd_extract = [
            ffmpeg, "-y", "-i", video_path,
            "-map", f"0:a:{idx}",
            "-c:a", "libmp3lame", "-q:a", "2",
            audio_out_path
        ]
        
        try:
            proc_ext = await asyncio.create_subprocess_exec(
                *cmd_extract,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            _, stderr_ext = await proc_ext.communicate()
            if proc_ext.returncode == 0:
                languages.append(lang)
                logger.info(f"[Audio Extractor] Successfully extracted track {idx} as language '{lang}' to {audio_out_path}")
            else:
                logger.error(f"[Audio Extractor] Extraction failed for track {idx}: {stderr_ext.decode('utf-8', errors='ignore')}")
        except Exception as e:
            logger.error(f"[Audio Extractor] Extraction exception for track {idx}: {e}")

    if not languages:
        logger.warning("[Audio Extractor] No audio tracks could be successfully extracted. Aborting strip.")
        return []

    # 4. Strip audio from the original video file
    temp_video_path = video_path + ".silent.mp4"
    cmd_strip = [
        ffmpeg, "-y", "-i", video_path,
        "-an", "-c:v", "copy",
        "-movflags", "+faststart",
        temp_video_path
    ]
    
    try:
        proc_strip = await asyncio.create_subprocess_exec(
            *cmd_strip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        _, stderr_strip = await proc_strip.communicate()
        if proc_strip.returncode == 0:
            # Replace the original with the silent copy
            os.replace(temp_video_path, video_path)
            logger.info(f"[Audio Extractor] Successfully stripped all audio from video file: {video_path}")
        else:
            logger.error(f"[Audio Extractor] Strip failed: {stderr_strip.decode('utf-8', errors='ignore')}")
            if os.path.exists(temp_video_path):
                os.remove(temp_video_path)
    except Exception as e:
        logger.error(f"[Audio Extractor] Strip exception: {e}")
        if os.path.exists(temp_video_path):
            os.remove(temp_video_path)

    return languages
