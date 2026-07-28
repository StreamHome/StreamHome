import os
import json
import shutil
import asyncio
import subprocess
import re
from typing import List, Dict, Any, Optional
from services.logger import logger
from services.languages import language_label, normalize_language_tag

def get_ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"

def get_ffprobe_path() -> str:
    return shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"


def normalize_language_code(value: Optional[str], fallback: str = "und") -> str:
    """Backward-compatible alias for the shared language-tag normalizer."""

    return normalize_language_tag(value, fallback)


def apply_primary_audio_language(audio_metadata: List[Dict[str, Any]], selected_language: Optional[str]) -> List[Dict[str, Any]]:
    """Make the submitted language authoritative for the default embedded track."""

    if not selected_language or not audio_metadata:
        return audio_metadata
    language = normalize_language_code(selected_language, "en")
    corrected = [dict(item) for item in audio_metadata]
    primary_index = next((idx for idx, item in enumerate(corrected) if item.get("default")), 0)
    corrected[primary_index]["language"] = language
    corrected[primary_index]["label"] = language_label(language)
    return corrected


def _repair_portable_metadata(video_url: str, selected_language: str, languages: List[str], audio_metadata: List[Dict[str, Any]]) -> None:
    if not video_url.startswith("/media/"):
        return
    from services.media_source import local_path_for

    media_path = local_path_for(video_url)
    metadata_dir = media_path.parent / ".metadata"
    if not metadata_dir.is_dir():
        return
    for metadata_path in metadata_dir.glob("metadata*.json"):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload["language"] = selected_language
            payload["original_language"] = selected_language
            payload["languages"] = languages
            payload["audio_metadata"] = audio_metadata
            temporary = metadata_path.with_suffix(f"{metadata_path.suffix}.tmp")
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, metadata_path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[Audio Language Repair] Could not update {metadata_path}: {exc}")


async def repair_completed_ingestion_languages() -> int:
    """Repair catalog rows created before submitted languages overrode bad source tags."""

    from db import engine
    from models import DownloadTask, Episode, Movie
    from services.playback_prep import playback_prep_service
    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession

    async with AsyncSession(engine, expire_on_commit=False) as db:
        result = await db.exec(
            select(DownloadTask).where(
                DownloadTask.status == "COMPLETED",
                DownloadTask.language.is_not(None),
            )
        )
        tasks = sorted(result.all(), key=lambda task: str(task.created_at or ""), reverse=True)
        seen: set[str] = set()
        repaired: list[tuple[str, str, List[str], List[Dict[str, Any]]]] = []
        for task in tasks:
            identity = (
                f"m_{task.tmdb_id}"
                if task.media_type == "movie"
                else f"ep_{task.tmdb_id}_s{task.season or 1}_e{task.episode or 1}"
            )
            if identity in seen:
                continue
            seen.add(identity)
            selected_language = normalize_language_code(task.language, "en")
            entity = await db.get(Movie if task.media_type == "movie" else Episode, identity)
            if not entity or not entity.video_url.startswith("/media/"):
                continue
            current_languages = [normalize_language_tag(value) for value in entity.languages]
            corrected_languages = [selected_language] + [
                value for value in current_languages if value not in {selected_language, "und"}
            ]
            corrected_audio = apply_primary_audio_language(entity.audio_metadata, selected_language)
            changed = entity.languages != corrected_languages or entity.audio_metadata != corrected_audio
            if isinstance(entity, Movie) and entity.original_language != selected_language:
                entity.original_language = selected_language
                changed = True
            if not isinstance(entity, Movie):
                show = await db.get(Movie, entity.movie_id)
                if show and show.original_language != selected_language:
                    show.original_language = selected_language
                    db.add(show)
                    changed = True
            if not changed:
                continue
            entity.languages = corrected_languages
            entity.audio_metadata = corrected_audio
            db.add(entity)
            repaired.append((entity.id, entity.video_url, corrected_languages, corrected_audio))
        if repaired:
            await db.commit()

    for media_id, video_url, languages, audio_metadata in repaired:
        playback_prep_service.cancel_media(media_id)
        shutil.rmtree(playback_prep_service.cache_dir / re.sub(r"[^a-zA-Z0-9_.-]", "_", media_id), ignore_errors=True)
        await asyncio.to_thread(_repair_portable_metadata, video_url, languages[0], languages, audio_metadata)
    if repaired:
        logger.info(f"[Audio Language Repair] Corrected {len(repaired)} completed catalog item(s).")
    return len(repaired)


def audio_track_labels(
    streams: List[Dict[str, Any]],
    default_lang: str = "en",
    *,
    override_primary: bool = False,
) -> List[str]:
    """Return stable, unique filenames for the source's current audio layout."""

    labels: List[str] = []
    primary_index = next(
        (idx for idx, stream in enumerate(streams) if (stream.get("disposition") or {}).get("default")),
        0,
    )
    selected_language = normalize_language_code(default_lang, "en")
    for idx, stream in enumerate(streams):
        tags = stream.get("tags", {})
        tagged_language = normalize_language_tag(tags.get("language"), "")
        if tagged_language == "und":
            tagged_language = ""
        if override_primary and idx == primary_index:
            lang = selected_language
        else:
            lang = tagged_language or (selected_language if idx == primary_index else f"track_{idx}")

        base_lang = lang
        duplicate = 1
        while lang in labels:
            lang = f"{base_lang}_{duplicate}"
            duplicate += 1
        labels.append(lang)
    return labels

async def extract_audio_and_strip_video(
    video_path: str,
    default_lang: str = "en",
    *,
    override_primary: bool = False,
) -> List[str]:
    """
    Probes the video file for audio streams. Extracts each stream to a separate MP3 file
    inside an 'audio' folder next to the video file. Does NOT strip audio from the original
    video file (keeps the default audio intact). Returns a list of language codes extracted.
    """
    if not os.path.exists(video_path):
        logger.warning(f"[Audio Extractor] File not found: {video_path}")
        return []

    ffprobe = get_ffprobe_path()
    ffmpeg = get_ffmpeg_path()
    
    # 1. Probe for audio streams
    cmd_probe = [
        ffprobe, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index:tags=language:stream_disposition=default",
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
        logger.info(f"[Audio Extractor] No audio streams found in {video_path}.")
        return []

    # 2. Prepare folders
    dir_name = os.path.dirname(video_path)
    audio_dir = os.path.join(dir_name, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    
    languages: List[str] = []
    logger.info(f"[Audio Extractor] Found {len(streams)} audio streams. Extracting...")
    
    # 3. Extract each audio stream using deterministic names. FFmpeg's -y
    # refreshes the current track instead of inventing en_1, en_2,
    # and so on each time the same title is re-ingested.
    expected_languages = audio_track_labels(streams, default_lang, override_primary=override_primary)
    for idx, lang in enumerate(expected_languages):
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

    if len(languages) == len(expected_languages):
        expected_files = {f"{lang}.mp3" for lang in expected_languages}
        for existing_name in os.listdir(audio_dir):
            if existing_name.lower().endswith(".mp3") and existing_name not in expected_files:
                try:
                    os.remove(os.path.join(audio_dir, existing_name))
                    logger.info(f"[Audio Extractor] Removed stale generated track: {existing_name}")
                except OSError as error:
                    logger.warning(f"[Audio Extractor] Could not remove stale track {existing_name}: {error}")

    return languages
