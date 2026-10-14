"""Versioned source-header storage without changing the task database schema."""
import json
from typing import Any, Callable

STORAGE_KEY = "__streamhome_source_headers__"
SOURCE_HEADERS_VERSION = 1


def encode_source_headers(video: dict[str, str], audio: dict[str, str]) -> str:
    return json.dumps({STORAGE_KEY: SOURCE_HEADERS_VERSION, "video": video, "audio": audio})


def decode_source_headers(raw: str | None, source: str = "video") -> dict[str, str]:
    try:
        value: Any = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}  # Preserve recovery of malformed legacy JSON.
    if not isinstance(value, dict):
        raise ValueError("Invalid stored source headers")
    if STORAGE_KEY in value:
        if type(value[STORAGE_KEY]) is not int or value[STORAGE_KEY] != SOURCE_HEADERS_VERSION:
            raise ValueError("Unsupported stored source header version")
        value = value.get(source)
        if not isinstance(value, dict):
            raise ValueError("Invalid stored source header set")
    return dict(value)


def copy_source_headers(raw: str | None, validate: Callable[[dict[str, str]], dict[str, str]]) -> str:
    """Copy retained intent only after validating both independent source sets."""
    return encode_source_headers(
        validate(decode_source_headers(raw, "video")),
        validate(decode_source_headers(raw, "audio")),
    )


def replace_video_source_headers(raw: str | None, video: dict[str, str]) -> str:
    """Preserve versioned audio while retaining the legacy shared setter contract."""
    try:
        stored = json.loads(raw or "{}")
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        raise ValueError("Invalid stored source headers")
    if STORAGE_KEY in stored:
        return encode_source_headers(video, decode_source_headers(raw, "audio"))
    return json.dumps(video or {})
