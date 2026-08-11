from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiofiles
import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from models import Episode, Movie
from services.ffmpeg_input import ffmpeg_network_input_options
from services.ingestion_security import UnsafeIngestionSource, validate_headers, validate_url, validated_stream_request
from services.languages import normalize_language_tag
from services.logger import logger
from services.media_probe import merge_local_external_audio, probe_cloud_external_audio, probe_completed_media
from services.media_source import (
    EXTERNAL_AUDIO_EXTENSIONS,
    ResolvedMediaSource,
    local_playback_fingerprint,
    playback_source_fingerprint,
    resolve_media_source,
)
from services.playback_prep import playback_prep_service
from services.rclone import rclone_service
from services.state import register_process, unregister_process


TRACK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SUBTITLE_LIMIT_BYTES = 10 * 1024 * 1024
AUDIO_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 256 * 1024
_media_locks: dict[str, asyncio.Lock] = {}
_media_locks_guard = asyncio.Lock()


class MediaUpdateFailure(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(slots=True)
class MutableMedia:
    item: Movie | Episode
    media_type: str
    source: ResolvedMediaSource


async def media_lock(media_id: str) -> asyncio.Lock:
    async with _media_locks_guard:
        return _media_locks.setdefault(media_id, asyncio.Lock())


def temporary_update_directory() -> tempfile.TemporaryDirectory[str]:
    root = Path(settings.TEMP_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="streamhome-media-update-", dir=root)


async def require_mutable_media(media_id: str, db: AsyncSession) -> MutableMedia:
    item = await db.get(Movie, media_id)
    media_type = "movie"
    if item is None:
        item = await db.get(Episode, media_id)
        media_type = "episode"
    if item is None:
        raise MediaUpdateFailure(404, "media_not_found", "The requested media item does not exist.")
    if not item.video_url.startswith("/media/"):
        raise MediaUpdateFailure(409, "media_not_ready", "Media mutations require a completed catalog source.")
    try:
        source = await resolve_media_source(item.video_url)
    except ValueError as exc:
        raise MediaUpdateFailure(409, "media_source_invalid", "The catalog source is not mutable.") from exc
    if not source.available:
        raise MediaUpdateFailure(409, "media_source_missing", "The completed media source is unavailable.")
    return MutableMedia(item=item, media_type=media_type, source=source)


def metadata_payload(item: Movie | Episode) -> dict:
    return {
        "title": item.title,
        "description": item.description,
        "quality": item.quality,
        "languages": item.languages,
        "subtitles": item.subtitles,
        "skip_markers": item.skip_markers,
        "audio_metadata": item.audio_metadata,
        "source_fingerprint": item.source_fingerprint,
    }


async def load_portable_metadata(source: ResolvedMediaSource, temporary_root: Path) -> dict:
    local_metadata = source.local_path.parent / ".metadata" / "metadata.json"
    if local_metadata.is_file():
        try:
            value = json.loads(local_metadata.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    if source.cloud_path:
        remote_metadata = f"{source.cloud_path.rsplit('/', 1)[0]}/.metadata/metadata.json"
        downloaded = temporary_root / "metadata.json"
        result = await rclone_service.copyto_atomic(remote_metadata, str(downloaded), timeout=60)
        if result.ok and downloaded.is_file():
            try:
                value = json.loads(downloaded.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


async def upload_cloud_file(local_path: Path, remote_path: str) -> None:
    if not rclone_service.cloud_write_available():
        raise MediaUpdateFailure(503, "cloud_write_unavailable", "Cloud storage is not accepting writes.")
    staging = f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/.streamhome-media-updates/{uuid.uuid4().hex}/{local_path.name}"
    copied = await rclone_service.run("copyto", str(local_path), staging, timeout=900)
    if not copied.ok:
        raise MediaUpdateFailure(502, copied.error_code or "cloud_upload_failed", "The sidecar could not be uploaded to cloud storage.")
    published = await rclone_service.run("moveto", staging, remote_path, timeout=900)
    if not published.ok:
        await rclone_service.run("deletefile", staging, timeout=60)
        raise MediaUpdateFailure(502, published.error_code or "cloud_publish_failed", "The sidecar could not be published to cloud storage.")


async def persist_portable_metadata(source: ResolvedMediaSource, metadata: dict, temporary_root: Path) -> None:
    serialized = temporary_root / "portable-metadata.json"
    atomic_json_write(serialized, metadata)
    if source.local_exists:
        atomic_json_write(source.local_path.parent / ".metadata" / "metadata.json", metadata)
    if settings.STORAGE_ENGINE == "CLOUD" and source.cloud_path:
        remote = f"{source.cloud_path.rsplit('/', 1)[0]}/.metadata/metadata.json"
        await upload_cloud_file(serialized, remote)


async def download_remote_asset(
    url: str,
    destination: Path,
    *,
    headers: dict[str, str],
    client_address: str,
    maximum_bytes: int,
) -> None:
    timeout = httpx.Timeout(connect=20.0, read=60.0, write=20.0, pool=20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await validated_stream_request(
            client,
            url,
            headers=headers,
            client_address=client_address,
        )
        try:
            response.raise_for_status()
            declared_length = int(response.headers.get("content-length") or 0)
            if declared_length > maximum_bytes:
                raise MediaUpdateFailure(413, "asset_too_large", "The remote asset exceeds the permitted size.")
            received = 0
            async with aiofiles.open(destination, "wb") as handle:
                async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                    received += len(chunk)
                    if received > maximum_bytes:
                        raise MediaUpdateFailure(413, "asset_too_large", "The remote asset exceeds the permitted size.")
                    await handle.write(chunk)
            if received == 0:
                raise MediaUpdateFailure(422, "asset_empty", "The remote asset is empty.")
        except httpx.HTTPStatusError as exc:
            raise MediaUpdateFailure(502, "asset_download_failed", f"The remote asset returned HTTP {exc.response.status_code}.") from exc
        finally:
            await response.aclose()


def subtitle_as_webvtt(raw: bytes) -> bytes:
    if len(raw) > SUBTITLE_LIMIT_BYTES:
        raise MediaUpdateFailure(413, "subtitle_too_large", "The subtitle exceeds the permitted size.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MediaUpdateFailure(422, "subtitle_encoding_invalid", "Subtitles must use UTF-8 encoding.") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if normalized.startswith("WEBVTT"):
        return f"{normalized}\n".encode("utf-8")
    lines = normalized.split("\n")
    converted = [line.replace(",", ".") if " --> " in line else line for line in lines]
    if not any(" --> " in line for line in converted):
        raise MediaUpdateFailure(422, "subtitle_format_invalid", "The subtitle must be WebVTT or SRT.")
    return ("WEBVTT\n\n" + "\n".join(converted) + "\n").encode("utf-8")


async def prepare_subtitle_asset(
    url: str,
    destination: Path,
    *,
    headers: dict[str, str],
    client_address: str,
) -> None:
    downloaded = destination.with_suffix(".download")
    await download_remote_asset(
        url,
        downloaded,
        headers=headers,
        client_address=client_address,
        maximum_bytes=SUBTITLE_LIMIT_BYTES,
    )
    destination.write_bytes(subtitle_as_webvtt(downloaded.read_bytes()))


def audio_url_extension(url: str) -> str:
    return Path(urlsplit(url).path).suffix.lower()


async def transcode_remote_audio(
    url: str,
    destination: Path,
    *,
    headers: dict[str, str],
    source_type: str,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaUpdateFailure(503, "ffmpeg_unavailable", "FFmpeg is required for this audio source.")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if headers:
        command.extend(["-headers", "".join(f"{name}: {value}\r\n" for name, value in headers.items())])
    command.extend(ffmpeg_network_input_options(url, source_type))
    command.extend(["-i", url, "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "192k", str(destination)])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    process_key = f"media-update-audio:{id(process)}"
    register_process(process_key, process)
    try:
        _, stderr = await process.communicate()
    finally:
        unregister_process(process_key)
    if process.returncode != 0:
        logger.warning("[Media Update] FFmpeg rejected a remote dubbing source: %s", stderr.decode("utf-8", errors="ignore")[-1000:])
        raise MediaUpdateFailure(422, "audio_conversion_failed", "The remote source could not be converted to an audio sidecar.")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise MediaUpdateFailure(422, "audio_empty", "The converted audio sidecar is empty.")
    if destination.stat().st_size > AUDIO_LIMIT_BYTES:
        raise MediaUpdateFailure(413, "audio_too_large", "The converted audio sidecar exceeds the permitted size.")


async def prepare_audio_asset(
    url: str,
    temporary_root: Path,
    *,
    headers: dict[str, str],
    client_address: str,
    source_type: str,
) -> Path:
    extension = audio_url_extension(url)
    if source_type == "auto" and extension in EXTERNAL_AUDIO_EXTENSIONS:
        destination = temporary_root / f"audio{extension}"
        await download_remote_asset(
            url,
            destination,
            headers=headers,
            client_address=client_address,
            maximum_bytes=AUDIO_LIMIT_BYTES,
        )
    else:
        destination = temporary_root / "audio.m4a"
        await transcode_remote_audio(url, destination, headers=headers, source_type=source_type)
    probe = await probe_completed_media(str(destination))
    if not probe.get("audio_metadata"):
        raise MediaUpdateFailure(422, "audio_stream_missing", "The supplied dubbing asset contains no audio stream.")
    return destination


def install_local_asset(source: ResolvedMediaSource, relative_path: str, prepared: Path) -> Path:
    destination = source.local_path.parent / Path(relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(prepared, temporary)
    os.replace(temporary, destination)
    return destination


async def install_asset(source: ResolvedMediaSource, relative_path: str, prepared: Path) -> None:
    if source.local_exists:
        install_local_asset(source, relative_path, prepared)
    if settings.STORAGE_ENGINE == "CLOUD" and source.cloud_path:
        remote_parent = source.cloud_path.rsplit("/", 1)[0]
        await upload_cloud_file(prepared, f"{remote_parent}/{relative_path.replace(os.sep, '/')}")


async def delete_asset(source: ResolvedMediaSource, relative_path: str) -> None:
    if source.local_exists:
        path = source.local_path.parent / Path(relative_path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if settings.STORAGE_ENGINE == "CLOUD" and source.cloud_path:
        remote_parent = source.cloud_path.rsplit("/", 1)[0]
        result = await rclone_service.run("deletefile", f"{remote_parent}/{relative_path.replace(os.sep, '/')}", timeout=60)
        if not result.ok and result.error_code != "drive_not_found":
            raise MediaUpdateFailure(502, result.error_code or "cloud_delete_failed", "The cloud sidecar could not be deleted.")


def portable_subtitle_match(item: dict, track_id: str) -> bool:
    if str(item.get("trackId") or item.get("track_id") or "") == track_id:
        return True
    file_name = str(item.get("fileName") or item.get("file_name") or "")
    return Path(file_name).stem == f"subtitle_{track_id}"


async def update_skip_markers(media_id: str, markers: dict, db: AsyncSession) -> dict:
    lock = await media_lock(media_id)
    async with lock:
        mutable = await require_mutable_media(media_id, db)
        with temporary_update_directory() as temporary:
            temporary_root = Path(temporary)
            metadata = await load_portable_metadata(mutable.source, temporary_root)
            mutable.item.skip_markers = markers
            metadata.update(metadata_payload(mutable.item))
            await persist_portable_metadata(mutable.source, metadata, temporary_root)
            db.add(mutable.item)
            await db.commit()
        return {"mediaId": media_id, "skipMarkers": mutable.item.skip_markers}


async def upsert_subtitle(
    media_id: str,
    track_id: str,
    *,
    language: str,
    label: str | None,
    url: str,
    headers: dict[str, str],
    client_address: str,
    db: AsyncSession,
) -> dict:
    if not TRACK_ID_RE.fullmatch(track_id):
        raise MediaUpdateFailure(400, "subtitle_track_id_invalid", "The subtitle track ID is invalid.")
    lock = await media_lock(media_id)
    async with lock:
        mutable = await require_mutable_media(media_id, db)
        with temporary_update_directory() as temporary:
            temporary_root = Path(temporary)
            prepared = temporary_root / f"subtitle_{track_id}.vtt"
            await prepare_subtitle_asset(url, prepared, headers=headers, client_address=client_address)
            file_name = prepared.name
            await install_asset(mutable.source, file_name, prepared)
            subtitles = [entry for entry in mutable.item.subtitles if not portable_subtitle_match(entry, track_id)]
            entry = {
                "trackId": track_id,
                "language": normalize_language_tag(language),
                "ext": ".vtt",
                "fileName": file_name,
            }
            if label:
                entry["label"] = label.strip()
            subtitles.append(entry)
            mutable.item.subtitles = subtitles
            metadata = await load_portable_metadata(mutable.source, temporary_root)
            metadata.update(metadata_payload(mutable.item))
            await persist_portable_metadata(mutable.source, metadata, temporary_root)
            mutable.item.vibe_analysis_status = "queued"
            mutable.item.vibe_analysis_version = 0
            db.add(mutable.item)
            await db.commit()
        return {"mediaId": media_id, "subtitle": entry, "subtitles": mutable.item.subtitles}


async def remove_subtitle(media_id: str, track_id: str, db: AsyncSession) -> dict:
    if not TRACK_ID_RE.fullmatch(track_id):
        raise MediaUpdateFailure(400, "subtitle_track_id_invalid", "The subtitle track ID is invalid.")
    lock = await media_lock(media_id)
    async with lock:
        mutable = await require_mutable_media(media_id, db)
        matched = next((entry for entry in mutable.item.subtitles if portable_subtitle_match(entry, track_id)), None)
        if not matched:
            raise MediaUpdateFailure(404, "subtitle_not_found", "The subtitle track does not exist.")
        file_name = str(matched.get("fileName") or matched.get("file_name") or f"subtitle_{track_id}.vtt")
        if Path(file_name).name != file_name:
            raise MediaUpdateFailure(409, "subtitle_path_invalid", "The stored subtitle path is invalid.")
        with temporary_update_directory() as temporary:
            temporary_root = Path(temporary)
            await delete_asset(mutable.source, file_name)
            mutable.item.subtitles = [entry for entry in mutable.item.subtitles if not portable_subtitle_match(entry, track_id)]
            metadata = await load_portable_metadata(mutable.source, temporary_root)
            metadata.update(metadata_payload(mutable.item))
            await persist_portable_metadata(mutable.source, metadata, temporary_root)
            mutable.item.vibe_analysis_status = "queued" if mutable.item.subtitles else "unavailable"
            mutable.item.vibe_analysis_version = 0
            db.add(mutable.item)
            await db.commit()
        return {"mediaId": media_id, "removedTrackId": track_id, "subtitles": mutable.item.subtitles}


async def refresh_audio_state(mutable: MutableMedia) -> None:
    embedded = [entry for entry in mutable.item.audio_metadata if str(entry.get("source") or "embedded").lower() != "external"]
    if mutable.source.local_exists:
        audio_metadata = merge_local_external_audio(str(mutable.source.local_path), embedded)
        fingerprint = local_playback_fingerprint(mutable.source.local_path, audio_metadata)
    elif mutable.source.cloud_path:
        audio_metadata = await probe_cloud_external_audio(mutable.source.cloud_path, embedded)
        fingerprint = playback_source_fingerprint(mutable.source, audio_metadata)
    else:
        audio_metadata = embedded
        fingerprint = mutable.source.fingerprint
    mutable.item.audio_metadata = audio_metadata
    mutable.item.languages = list(dict.fromkeys(str(entry.get("language") or "und") for entry in audio_metadata))
    mutable.item.source_fingerprint = fingerprint


async def remove_other_audio_extensions(source: ResolvedMediaSource, language: str, retained_extension: str | None) -> None:
    for extension in EXTERNAL_AUDIO_EXTENSIONS:
        if extension == retained_extension:
            continue
        await delete_asset(source, f"audio/{language}{extension}")


async def schedule_audio_preparation(media_id: str, mutable: MutableMedia, previous_fingerprint: str | None) -> None:
    playback_prep_service.cancel_media(media_id, previous_fingerprint)
    try:
        refreshed_source = await resolve_media_source(mutable.item.video_url)
        await playback_prep_service.prepare(
            media_id,
            mutable.item,
            refreshed_source,
            include_remaining=True,
            foreground=False,
        )
    except Exception as exc:
        logger.warning("[Media Update] Adaptive audio preparation scheduling failed for %s: %s", media_id, type(exc).__name__)


async def upsert_audio(
    media_id: str,
    language: str,
    *,
    url: str,
    headers: dict[str, str],
    client_address: str,
    source_type: str,
    db: AsyncSession,
) -> dict:
    normalized_language = normalize_language_tag(language)
    if normalized_language == "und" and language.strip().lower() not in {"und", "unknown"}:
        raise MediaUpdateFailure(400, "audio_language_invalid", "The dubbing language is invalid.")
    lock = await media_lock(media_id)
    async with lock:
        mutable = await require_mutable_media(media_id, db)
        previous_fingerprint = mutable.item.source_fingerprint
        with temporary_update_directory() as temporary:
            temporary_root = Path(temporary)
            prepared = await prepare_audio_asset(
                url,
                temporary_root,
                headers=headers,
                client_address=client_address,
                source_type=source_type,
            )
            relative_path = f"audio/{normalized_language}{prepared.suffix.lower()}"
            await install_asset(mutable.source, relative_path, prepared)
            await remove_other_audio_extensions(mutable.source, normalized_language, prepared.suffix.lower())
            await refresh_audio_state(mutable)
            metadata = await load_portable_metadata(mutable.source, temporary_root)
            metadata.update(metadata_payload(mutable.item))
            await persist_portable_metadata(mutable.source, metadata, temporary_root)
            db.add(mutable.item)
            await db.commit()
        await schedule_audio_preparation(media_id, mutable, previous_fingerprint)
        track = next(
            (entry for entry in mutable.item.audio_metadata if entry.get("source") == "external" and entry.get("language") == normalized_language),
            None,
        )
        return {"mediaId": media_id, "audio": track, "languages": mutable.item.languages}


async def remove_audio(media_id: str, language: str, db: AsyncSession) -> dict:
    normalized_language = normalize_language_tag(language)
    lock = await media_lock(media_id)
    async with lock:
        mutable = await require_mutable_media(media_id, db)
        existing = next(
            (entry for entry in mutable.item.audio_metadata if entry.get("source") == "external" and entry.get("language") == normalized_language),
            None,
        )
        if not existing:
            raise MediaUpdateFailure(404, "audio_not_found", "The dubbing track does not exist.")
        previous_fingerprint = mutable.item.source_fingerprint
        file_name = str(existing.get("fileName") or "")
        if Path(file_name).name != file_name or Path(file_name).suffix.lower() not in EXTERNAL_AUDIO_EXTENSIONS:
            raise MediaUpdateFailure(409, "audio_path_invalid", "The stored dubbing path is invalid.")
        with temporary_update_directory() as temporary:
            temporary_root = Path(temporary)
            await delete_asset(mutable.source, f"audio/{file_name}")
            await refresh_audio_state(mutable)
            metadata = await load_portable_metadata(mutable.source, temporary_root)
            metadata.update(metadata_payload(mutable.item))
            await persist_portable_metadata(mutable.source, metadata, temporary_root)
            db.add(mutable.item)
            await db.commit()
        await schedule_audio_preparation(media_id, mutable, previous_fingerprint)
        return {"mediaId": media_id, "removedLanguage": normalized_language, "languages": mutable.item.languages}


async def validate_remote_input(url: str, headers: dict[str, str] | None, client_address: str) -> dict[str, str]:
    try:
        await validate_url(url, client_address=client_address)
        return validate_headers(headers)
    except UnsafeIngestionSource as exc:
        raise MediaUpdateFailure(400, "unsafe_ingestion_source", str(exc)) from exc
