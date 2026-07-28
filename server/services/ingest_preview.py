from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from config import settings


PLAYLIST_NAME = "playlist.m3u8"
TASK_ID_RE = re.compile(r"^[a-zA-Z0-9-]{8,64}$")
EXTINF_RE = re.compile(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
MINIMUM_START_BUFFER_SECONDS = 12.0
PREVIEW_RETENTION_SECONDS = 24 * 60 * 60


class IngestPreviewError(ValueError):
    pass


class IngestPreviewService:
    def __init__(self) -> None:
        self.root = Path(settings.TEMP_DIR).resolve() / "ingest_preview"
        self.root.mkdir(parents=True, exist_ok=True)

    def task_path(self, task_id: str) -> Path:
        if not TASK_ID_RE.fullmatch(task_id):
            raise IngestPreviewError("The ingestion preview task identifier is invalid.")
        return (self.root / task_id).resolve()

    def playlist_path(self, task_id: str) -> Path:
        return self.task_path(task_id) / PLAYLIST_NAME

    def fingerprint(self, task_id: str) -> str:
        self.task_path(task_id)
        return hashlib.sha256(f"streamhome-ingest-preview:{task_id}".encode("utf-8")).hexdigest()[:32]

    def prepare(self, task_id: str, duration_seconds: float) -> Path:
        target = self.task_path(task_id)
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        self._write_state(
            task_id,
            {
                "phase": "preparing",
                "duration_seconds": max(0.0, float(duration_seconds)),
                "processed_seconds": 0.0,
                "speed_multiplier": 0.0,
                "error_code": "",
                "error_message": "",
                "updated_at": time.time(),
            },
        )
        self.cleanup_expired(exclude={task_id})
        return target

    def update_progress(
        self,
        task_id: str,
        *,
        processed_seconds: float,
        speed_multiplier: float,
        duration_seconds: float,
    ) -> None:
        state = self._read_state(task_id)
        state.update(
            {
                "phase": "streaming",
                "duration_seconds": max(float(state.get("duration_seconds") or 0), float(duration_seconds or 0)),
                "processed_seconds": max(float(state.get("processed_seconds") or 0), float(processed_seconds or 0)),
                "speed_multiplier": max(0.0, float(speed_multiplier or 0)),
                "updated_at": time.time(),
            }
        )
        self._write_state(task_id, state)

    def mark_complete(self, task_id: str) -> None:
        state = self._read_state(task_id)
        state.update({"phase": "complete", "updated_at": time.time()})
        self._write_state(task_id, state)

    def mark_error(self, task_id: str, code: str, message: str) -> None:
        state = self._read_state(task_id)
        state.update(
            {
                "phase": "error",
                "error_code": str(code or "PREVIEW_FAILED")[:64],
                "error_message": str(message or "Play-while-downloading preview failed.")[:400],
                "updated_at": time.time(),
            }
        )
        self._write_state(task_id, state)

    def status(self, task_id: str) -> dict[str, Any]:
        state = self._read_state(task_id)
        playlist = self.playlist_path(task_id)
        content = ""
        try:
            content = playlist.read_text(encoding="utf-8") if playlist.is_file() else ""
        except OSError:
            content = ""
        buffered_seconds = sum(float(value) for value in EXTINF_RE.findall(content))
        complete = "#EXT-X-ENDLIST" in content
        duration = max(0.0, float(state.get("duration_seconds") or 0))
        speed = max(0.0, float(state.get("speed_multiplier") or 0))
        effective_speed = speed * 0.9
        required_buffer = MINIMUM_START_BUFFER_SECONDS
        if duration > 0 and effective_speed < 1:
            required_buffer = max(required_buffer, duration * (1 - effective_speed))
        if duration > 0:
            required_buffer = min(duration, required_buffer)
        has_segments = bool(re.search(r"^[^#\s].+\.m4s(?:\?.*)?$", content, re.MULTILINE))
        ready = has_segments and (complete or buffered_seconds >= required_buffer)
        if state.get("phase") == "error":
            phase = "error"
        elif ready:
            phase = "ready"
        else:
            phase = "preparing"
        return {
            "phase": phase,
            "buffered_seconds": buffered_seconds,
            "required_buffer_seconds": required_buffer,
            "duration_seconds": duration,
            "speed_multiplier": speed,
            "complete": complete,
            "error_code": str(state.get("error_code") or ""),
            "error_message": str(state.get("error_message") or ""),
        }

    def safe_asset(self, task_id: str, relative_path: str) -> Path:
        root = self.task_path(task_id)
        parts = PurePosixPath(relative_path).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise IngestPreviewError("The ingestion preview path is invalid.")
        target = (root / Path(*parts)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise IngestPreviewError("The ingestion preview path escapes its task directory.") from exc
        return target

    def touch(self, task_id: str) -> None:
        target = self.task_path(task_id)
        try:
            os.utime(target, None)
        except OSError:
            pass

    def remove(self, task_id: str) -> None:
        shutil.rmtree(self.task_path(task_id), ignore_errors=True)

    def cleanup_expired(self, *, exclude: Optional[set[str]] = None) -> None:
        excluded = exclude or set()
        cutoff = time.time() - PREVIEW_RETENTION_SECONDS
        if not self.root.is_dir():
            return
        for candidate in self.root.iterdir():
            if not candidate.is_dir() or candidate.name in excluded:
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    shutil.rmtree(candidate, ignore_errors=True)
            except OSError:
                continue

    def _state_path(self, task_id: str) -> Path:
        return self.task_path(task_id) / "state.json"

    def _read_state(self, task_id: str) -> dict[str, Any]:
        target = self._state_path(task_id)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    def _write_state(self, task_id: str, payload: dict[str, Any]) -> None:
        target = self._state_path(task_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, target)


ingest_preview_service = IngestPreviewService()
