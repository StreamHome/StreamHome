from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlsplit

from config import settings
from services.rclone import rclone_service


CANONICAL_MEDIA_PREFIX = "/media/"
WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
EXTERNAL_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
CLOUD_OBJECT_CACHE_TTL_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class CloudObjectInfo:
    identity: str
    size: int


_cloud_object_cache: dict[str, tuple[float, CloudObjectInfo]] = {}


class MediaSourceError(ValueError):
    """Raised when a catalog media path is not safe or canonical."""


@dataclass(frozen=True, slots=True)
class ResolvedMediaSource:
    catalog_path: str
    relative_path: str
    local_path: Path
    cloud_path: Optional[str]
    local_exists: bool
    cloud_exists: bool
    cloud_identity: Optional[str] = None
    cloud_size: Optional[int] = None

    @property
    def available(self) -> bool:
        return self.local_exists or self.cloud_exists

    @property
    def fingerprint(self) -> str:
        if self.local_exists:
            value = local_media_identity(self.local_path)
        else:
            value = f"cloud:{self.cloud_identity or self.cloud_path or self.catalog_path}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    @property
    def video_fingerprint(self) -> str:
        """Identify the video bytes independently from replaceable audio sidecars."""

        if self.local_exists:
            return local_video_fingerprint(self.local_path)
        else:
            value = f"cloud:{self.cloud_identity or self.cloud_path or self.catalog_path}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def local_video_identity(media_path: Path) -> str:
    stat = media_path.stat()
    value = [{
        "name": media_path.name,
        "size": stat.st_size,
        "modified": stat.st_mtime_ns,
    }]
    return f"local:{json.dumps(value, sort_keys=True, separators=(',', ':'))}"


def local_video_fingerprint(media_path: Path) -> str:
    """Calculate the current local video identity without inspecting stream contents."""

    return hashlib.sha256(local_video_identity(media_path).encode("utf-8")).hexdigest()[:32]


def local_media_identity(media_path: Path) -> str:
    """Fingerprint the playable file and application-owned audio sidecars."""

    stat = media_path.stat()
    identity: list[dict[str, object]] = [
        {"name": media_path.name, "size": stat.st_size, "modified": stat.st_mtime_ns},
    ]
    audio_dir = media_path.parent / "audio"
    if audio_dir.is_dir():
        for audio_path in sorted(audio_dir.iterdir(), key=lambda item: item.name.lower()):
            if not audio_path.is_file() or audio_path.suffix.lower() not in EXTERNAL_AUDIO_EXTENSIONS:
                continue
            audio_stat = audio_path.stat()
            identity.append(
                {
                    "name": f"audio/{audio_path.name}",
                    "size": audio_stat.st_size,
                    "modified": audio_stat.st_mtime_ns,
                }
            )
    return f"local:{json.dumps(identity, sort_keys=True, separators=(',', ':'))}"


def playback_source_fingerprint(source: ResolvedMediaSource, audio_metadata: list[dict] | tuple[dict, ...]) -> str:
    """Bind playback caches and tickets to retained external-audio identity."""

    return bind_audio_fingerprint(source.fingerprint, audio_metadata)


def bind_audio_fingerprint(source_fingerprint: str, audio_metadata: list[dict] | tuple[dict, ...]) -> str:
    """Combine a base media identity with portable external-audio metadata."""

    external_identity = [
        {
            "file": str(item.get("fileName") or item.get("file_name") or ""),
            "size": int(item.get("fileSize") or item.get("file_size") or 0),
            "modified": str(item.get("modifiedAt") or item.get("modified_at") or ""),
        }
        for item in audio_metadata
        if str(item.get("source") or "").lower() == "external"
    ]
    if not external_identity:
        return source_fingerprint
    value = json.dumps(
        {"source": source_fingerprint, "audio": external_identity},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def local_playback_fingerprint(
    media_path: Path,
    audio_metadata: list[dict] | tuple[dict, ...],
) -> str:
    """Calculate the current local playback identity without opening media streams."""

    base_fingerprint = hashlib.sha256(local_media_identity(media_path).encode("utf-8")).hexdigest()[:32]
    return bind_audio_fingerprint(base_fingerprint, audio_metadata)


def canonicalize_catalog_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaSourceError("Media path is empty")

    raw = value.strip().replace("\\", "/")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise MediaSourceError("Media path must be a normalized /media/... catalog path")
    if WINDOWS_DRIVE_RE.match(raw):
        raise MediaSourceError("Absolute filesystem paths are not valid catalog paths")
    if not raw.startswith(CANONICAL_MEDIA_PREFIX):
        raise MediaSourceError("Media path must begin with /media/")

    relative = raw[len(CANONICAL_MEDIA_PREFIX):]
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise MediaSourceError("Media path contains unsafe components")
    if parts[0] not in {"Movies", "Series"}:
        raise MediaSourceError("Media path must be inside Movies or Series")

    canonical = f"{CANONICAL_MEDIA_PREFIX}{'/'.join(parts)}"
    media_root = Path(settings.MEDIA_DIR).resolve()
    candidate = (media_root / Path(*parts)).resolve()
    try:
        candidate.relative_to(media_root)
    except ValueError as exc:
        raise MediaSourceError("Media path escapes the media directory") from exc
    return canonical


def local_path_for(catalog_path: str) -> Path:
    canonical = canonicalize_catalog_path(catalog_path)
    parts = PurePosixPath(canonical[len(CANONICAL_MEDIA_PREFIX):]).parts
    return (Path(settings.MEDIA_DIR).resolve() / Path(*parts)).resolve()


def catalog_path_from_storage(file_path: str) -> str:
    """Convert an absolute media/temp file into its canonical ``/media`` URL."""

    candidate = Path(file_path).resolve()
    for storage_root in (Path(settings.MEDIA_DIR).resolve(), Path(settings.TEMP_DIR).resolve()):
        try:
            relative = candidate.relative_to(storage_root)
        except ValueError:
            continue
        return canonicalize_catalog_path(f"{CANONICAL_MEDIA_PREFIX}{relative.as_posix()}")
    raise MediaSourceError("Completed media file is outside the configured media and temp directories")


def cloud_path_for(catalog_path: str) -> str:
    canonical = canonicalize_catalog_path(catalog_path)
    relative = canonical[len(CANONICAL_MEDIA_PREFIX):]
    return f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/{relative}"


def clear_cloud_object_cache() -> None:
    _cloud_object_cache.clear()


async def cloud_object_info(remote_path: str) -> Optional[CloudObjectInfo]:
    if settings.STORAGE_ENGINE != "CLOUD" or not rclone_service.executable():
        return None
    now = time.monotonic()
    if len(_cloud_object_cache) > 1024:
        expired_paths = [path for path, (expires_at, _) in _cloud_object_cache.items() if expires_at <= now]
        for expired_path in expired_paths:
            _cloud_object_cache.pop(expired_path, None)
    cached = _cloud_object_cache.get(remote_path)
    if cached and cached[0] > now:
        return cached[1]
    result = await rclone_service.run("lsjson", remote_path, "--stat", timeout=30)
    if not result.ok or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    item: Optional[dict] = None
    if isinstance(payload, dict):
        item = payload
    if isinstance(payload, list) and payload:
        item = payload[0]
    if not item or item.get("IsDir"):
        return None
    try:
        size = int(item.get("Size", 0))
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    identity = json.dumps(
        {
            "path": remote_path,
            "size": size,
            "modTime": item.get("ModTime"),
            "hashes": item.get("Hashes") or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    info = CloudObjectInfo(identity=identity, size=size)
    _cloud_object_cache[remote_path] = (time.monotonic() + CLOUD_OBJECT_CACHE_TTL_SECONDS, info)
    return info


async def cloud_object_identity(remote_path: str) -> Optional[str]:
    info = await cloud_object_info(remote_path)
    return info.identity if info else None


async def resolve_media_source(catalog_path: str, *, check_cloud: bool = True) -> ResolvedMediaSource:
    canonical = canonicalize_catalog_path(catalog_path)
    relative = canonical[len(CANONICAL_MEDIA_PREFIX):]
    local_path = local_path_for(canonical)
    local_exists = local_path.is_file()
    remote = cloud_path_for(canonical) if settings.STORAGE_ENGINE == "CLOUD" else None
    cloud_identity: Optional[str] = None
    cloud_size: Optional[int] = None
    if check_cloud and not local_exists and remote:
        cloud_info = await cloud_object_info(remote)
        cloud_identity = cloud_info.identity if cloud_info else None
        cloud_size = cloud_info.size if cloud_info else None
    return ResolvedMediaSource(
        catalog_path=canonical,
        relative_path=relative,
        local_path=local_path,
        cloud_path=remote,
        local_exists=local_exists,
        cloud_exists=cloud_identity is not None,
        cloud_identity=cloud_identity,
        cloud_size=cloud_size,
    )


def is_safe_presentation_asset(catalog_path: str) -> bool:
    try:
        canonical = canonicalize_catalog_path(catalog_path)
    except MediaSourceError:
        return False
    extension = os.path.splitext(canonical.lower())[1]
    return extension in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}
