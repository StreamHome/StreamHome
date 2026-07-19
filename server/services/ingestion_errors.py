import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

@dataclass(frozen=True)
class IngestionFailure:
    code: str
    message: str
    retryable: bool = False
    diagnostics_path: Optional[str] = None

    @property
    def display(self) -> str:
        return f"{self.code} — {self.message}"


class IngestionTaskError(Exception):
    def __init__(self, failure: IngestionFailure):
        super().__init__(failure.display)
        self.failure = failure


def sanitize_url(value: str) -> str:
    """Remove credentials, query parameters, and fragments from a URL before logging."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            return value
        hostname = parsed.hostname or ""
        netloc = hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except Exception:
        return "<redacted-url>"


_URL_PATTERN = re.compile(r"https?://[^\s'\"]+", re.IGNORECASE)


def redact_diagnostics(value: str) -> str:
    return _URL_PATTERN.sub(lambda match: sanitize_url(match.group(0)), value or "")


def compact_diagnostics(value: str, limit: int = 280) -> str:
    lines = [line.strip() for line in redact_diagnostics(value).splitlines() if line.strip()]
    preferred = [
        line for line in lines
        if any(marker in line.lower() for marker in (
            "error opening", "server returned", "http error", "not found", "forbidden",
            "unauthorized", "timed out", "timeout", "connection reset", "connection refused",
            "invalid data", "does not contain any stream", "unsupported", "rate limit",
        ))
    ]
    summary = preferred[-1] if preferred else (lines[-1] if lines else "Media processing failed.")
    summary = " ".join(summary.split())
    return summary if len(summary) <= limit else f"{summary[:limit - 1]}…"


def classify_failure(diagnostics: str, default_code: str = "MEDIA_PROCESSING_FAILED") -> IngestionFailure:
    text = redact_diagnostics(diagnostics)
    lowered = text.lower()
    if "not found" in lowered or re.search(r"(?:http error|returned)\s+404\b", lowered):
        return IngestionFailure("SOURCE_NOT_FOUND", "The media sender source returned HTTP 404.")
    if "unauthorized" in lowered or re.search(r"(?:http error|returned)\s+401\b", lowered):
        return IngestionFailure("SOURCE_UNAUTHORIZED", "The media sender source rejected authentication.")
    if "forbidden" in lowered or re.search(r"(?:http error|returned)\s+403\b", lowered):
        return IngestionFailure("SOURCE_FORBIDDEN", "The media sender source denied access.")
    if "rate limit" in lowered or re.search(r"(?:http error|returned)\s+429\b", lowered):
        return IngestionFailure("SOURCE_RATE_LIMITED", "The media sender source is rate-limiting requests.", True)
    if re.search(r"\b5\d\d\b", lowered):
        return IngestionFailure("SOURCE_UNAVAILABLE", "The media sender source is temporarily unavailable.", True)
    if any(marker in lowered for marker in ("timed out", "timeout", "connection reset", "connection refused", "temporarily unavailable")):
        return IngestionFailure("SOURCE_UNREACHABLE", "The media sender source could not be reached.", True)
    if any(marker in lowered for marker in ("invalid data", "does not contain any stream", "unsupported codec", "unsupported format")):
        return IngestionFailure("INVALID_MEDIA_SOURCE", "The source does not contain supported playable media.")
    if any(marker in lowered for marker in ("no such file or directory", "the system cannot find the file", "winerror 2")):
        return IngestionFailure("FFMPEG_UNAVAILABLE", "FFmpeg or a required local executable could not be found.")
    return IngestionFailure(default_code, compact_diagnostics(text), True)


def _diagnostics_dir(override: Optional[str] = None) -> Path:
    if override:
        return Path(override) / "queue-errors"
    from config import config_dir, settings
    temp_root = Path(settings.TEMP_DIR)
    if not temp_root.is_absolute():
        temp_root = Path(config_dir) / temp_root
    return temp_root / "queue-errors"


def write_task_diagnostics(task_id: str, stage: str, diagnostics: str, temp_dir: Optional[str] = None) -> Optional[str]:
    try:
        diagnostics_dir = _diagnostics_dir(temp_dir)
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        target = diagnostics_dir / f"{task_id}.log"
        safe_diagnostics = redact_diagnostics(diagnostics).strip()
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] {stage}\n")
            handle.write(safe_diagnostics or "No diagnostics were emitted.")
            handle.write("\n")
        return str(target)
    except OSError:
        return None


def prune_task_diagnostics(max_age_days: int = 7, temp_dir: Optional[str] = None) -> None:
    diagnostics_dir = _diagnostics_dir(temp_dir)
    if not diagnostics_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=max_age_days)
    for target in diagnostics_dir.glob("*.log"):
        try:
            if datetime.fromtimestamp(target.stat().st_mtime) < cutoff:
                os.remove(target)
        except OSError:
            continue
