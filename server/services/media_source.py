from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlsplit

from config import settings
from services.rclone import rclone_service


CANONICAL_MEDIA_PREFIX = "/media/"
WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


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

    @property
    def available(self) -> bool:
        return self.local_exists or self.cloud_exists

    @property
    def fingerprint(self) -> str:
        if self.local_exists:
            stat = self.local_path.stat()
            value = f"local:{stat.st_size}:{stat.st_mtime_ns}"
        else:
            value = f"cloud:{self.cloud_identity or self.cloud_path or self.catalog_path}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


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


def cloud_path_for(catalog_path: str) -> str:
    canonical = canonicalize_catalog_path(catalog_path)
    relative = canonical[len(CANONICAL_MEDIA_PREFIX):]
    return f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/{relative}"


async def cloud_object_identity(remote_path: str) -> Optional[str]:
    if settings.STORAGE_ENGINE != "CLOUD" or not rclone_service.executable():
        return None
    result = await rclone_service.run("lsjson", remote_path, "--stat", timeout=30)
    if not result.ok or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        if payload.get("IsDir"):
            return None
        return json.dumps(
            {
                "path": remote_path,
                "size": payload.get("Size"),
                "modTime": payload.get("ModTime"),
                "hashes": payload.get("Hashes") or {},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    if isinstance(payload, list) and payload:
        item = payload[0]
        if item.get("IsDir"):
            return None
        return json.dumps(
            {
                "path": remote_path,
                "size": item.get("Size"),
                "modTime": item.get("ModTime"),
                "hashes": item.get("Hashes") or {},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    return None


async def resolve_media_source(catalog_path: str, *, check_cloud: bool = True) -> ResolvedMediaSource:
    canonical = canonicalize_catalog_path(catalog_path)
    relative = canonical[len(CANONICAL_MEDIA_PREFIX):]
    local_path = local_path_for(canonical)
    local_exists = local_path.is_file()
    remote = cloud_path_for(canonical) if settings.STORAGE_ENGINE == "CLOUD" else None
    cloud_identity: Optional[str] = None
    if check_cloud and not local_exists and remote:
        cloud_identity = await cloud_object_identity(remote)
    return ResolvedMediaSource(
        catalog_path=canonical,
        relative_path=relative,
        local_path=local_path,
        cloud_path=remote,
        local_exists=local_exists,
        cloud_exists=cloud_identity is not None,
        cloud_identity=cloud_identity,
    )


def is_safe_presentation_asset(catalog_path: str) -> bool:
    try:
        canonical = canonicalize_catalog_path(catalog_path)
    except MediaSourceError:
        return False
    extension = os.path.splitext(canonical.lower())[1]
    return extension in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}
