from lifecycle.media_work import supervised_output
import os
import json
import shutil
import asyncio
import subprocess
from typing import List, Dict, Any, Optional
from services.ffmpeg_input import local_media_input_options, native_media_environment
from services.logger import logger
from services.languages import language_label, normalize_language_tag
from services.state import register_process, unregister_process

def get_ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"

def get_ffprobe_path() -> str:
    return shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"


def normalize_language_code(value: Optional[str], fallback: str = "und") -> str:
    """Backward-compatible alias for the shared language-tag normalizer."""

    return normalize_language_tag(value, fallback)


def apply_primary_audio_language(
    audio_metadata: List[Dict[str, Any]], selected_language: Optional[str], *, record_submission: bool = True,
) -> List[Dict[str, Any]]:
    """Make the submitted language authoritative for the default embedded track."""

    primary_index = primary_embedded_audio_index(audio_metadata)
    if not selected_language or primary_index is None:
        return audio_metadata
    language = normalize_language_code(selected_language, "en")
    corrected = [dict(item) for item in audio_metadata]
    corrected[primary_index]["language"] = language
    corrected[primary_index]["label"] = language_label(language)
    if record_submission:
        corrected[primary_index]["languageOverride"] = {"source": "sender", "language": language}
    elif corrected[primary_index].get("languageOverride", {}).get("language") != language:
        corrected[primary_index].pop("languageOverride", None)
    return corrected


def primary_embedded_audio_index(audio_metadata: List[Dict[str, Any]]) -> Optional[int]:
    embedded = [idx for idx, item in enumerate(audio_metadata)
        if str(item.get("source") or "embedded").lower() == "embedded"]
    return next((idx for idx in embedded if audio_metadata[idx].get("default")),
        embedded[0] if embedded else None)


def retained_primary_audio_language(
    measured_audio: List[Dict[str, Any]],
    *retained_audio: List[Dict[str, Any]],
    same_video: bool,
) -> List[Dict[str, Any]]:
    """Keep accepted semantics for the identical primary stream of the same video.

    The source proof belongs to the accepting caller. Historical accepted labels
    remain labels; only an actual submitted override carries sender provenance.
    Physical stream manifests and every nonprimary/external track stay measured.
    """
    primary = primary_embedded_audio_index(measured_audio)
    if not same_video or primary is None:
        return measured_audio
    current = measured_audio[primary]
    stream_index = current.get("streamIndex", current.get("stream_index"))
    if stream_index is None:
        return measured_audio
    for tracks in retained_audio:
        previous_index = primary_embedded_audio_index(tracks)
        if previous_index is None:
            continue
        previous = tracks[previous_index]
        if previous.get("streamIndex", previous.get("stream_index")) != stream_index:
            continue
        language = normalize_language_code(previous.get("language"), "")
        if not language:
            continue
        corrected = [dict(item) for item in measured_audio]
        corrected[primary]["language"] = language
        corrected[primary]["label"] = previous.get("label") or language_label(language)
        override = previous.get("languageOverride")
        if isinstance(override, dict) and override.get("source") == "sender" and normalize_language_code(override.get("language"), "") == language:
            corrected[primary]["languageOverride"] = {"source": "sender", "language": language}
        return corrected
    return measured_audio


def audio_languages(audio_metadata: List[Dict[str, Any]]) -> List[str]:
    return list(dict.fromkeys(normalize_language_code(item.get("language")) for item in audio_metadata))


async def repair_completed_ingestion_languages() -> int:
    """Keep the startup hook without guessing accepted language from task history.

    DownloadTask has no immutable association with its accepted output or primary
    stream. Replacements and task deletion make the remaining task order ambiguous,
    even for unmarked legacy labels. It cannot authorize catalog or portable writes.
    Source-bound reconstruction and measurement refresh preserve accepted semantics
    independently; an unresolvable historical language correction remains deferred.
    """

    return 0


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
    Probe embedded audio and return stable language identities without creating lossy
    generated sidecars. Explicit external dubbing files remain application-owned inputs.
    """
    if not os.path.exists(video_path):
        logger.warning(f"[Audio Extractor] File not found: {video_path}")
        return []

    ffprobe = get_ffprobe_path()
    # 1. Probe for audio streams
    cmd_probe = [
        ffprobe, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index:tags=language:stream_disposition=default",
        "-of", "json", *local_media_input_options(), video_path
    ]
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_probe,
            env=native_media_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        process_key = f"audio-probe:{id(proc)}"
        register_process(process_key, proc)
        try:
            stdout, stderr = await supervised_output(proc, timeout=60.0)
        finally:
            unregister_process(process_key)
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

    languages = audio_track_labels(streams, default_lang, override_primary=override_primary)
    logger.info(
        f"[Audio Extractor] Preserved {len(streams)} embedded audio stream(s) "
        "without generating lossy sidecars."
    )
    return languages
