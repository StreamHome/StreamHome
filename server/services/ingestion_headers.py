"""Versioned source-header storage without changing the task database schema."""
import json
from typing import Any

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
