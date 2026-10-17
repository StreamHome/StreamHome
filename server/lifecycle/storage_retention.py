"""Conservative retention rules shared by automatic media lifecycle writers.

Only a known source container or server-authored portable catalog document may
be replaced automatically. Cache generations, sidecars and unclassified files
are retained; classification never grants an explicit user deletion right.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path, PurePosixPath


SOURCE_SUFFIXES = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpeg", ".mpg",
    ".flv", ".wmv", ".m2ts", ".ts", ".mp3", ".aac", ".m4a", ".flac",
    ".ogg", ".opus", ".wav", ".ac3", ".eac3", ".dts",
})
CACHE_DIRECTORIES = frozenset({
    "cache", ".cache", "playback_cache", "playback-cache", "ingest_preview",
    "ingest-preview", "previews", "preview", ".sources", "sources", "hls",
    ".streamhome-replacement-backups", ".streamhome-ingestion", ".streamhome-retained",
})


def is_protected_path(path: Path | str) -> bool:
    """Unknown paths fail closed, including files nested in retained trees."""
    value = PurePosixPath(str(path).replace("\\", "/"))
    if any(part.lower() in CACHE_DIRECTORIES or ".retained-" in part.lower()
           for part in value.parts):
        return True
    if value.parent.name == ".metadata" and re.fullmatch(
            r"metadata(?:_s[0-9]+_e[0-9]+)?\.json", value.name):
        return False
    return value.suffix.lower() not in SOURCE_SUFFIXES


def retain_existing_tree(path: Path | str) -> Path | None:
    """Atomically retire a generation without deleting or following its bytes.

    A retained sibling stays on the original filesystem and under the original
    managed root, so normal measurement still includes it. Never reuse it as a
    current playback generation without the usual source verification.
    """
    source = Path(path)
    if source.is_symlink():
        raise ValueError("Retained media must not be a symbolic link")
    if not source.exists():
        return None
    destination = source.with_name(f".{source.name}.retained-{uuid.uuid4().hex}")
    os.rename(source, destination)
    fd = os.open(source.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return destination
