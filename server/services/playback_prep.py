from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import weakref
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from config import settings
from services.logger import logger
from services.languages import language_label, normalize_language_tag
from services.media_source import ResolvedMediaSource, playback_source_fingerprint, resolve_media_source
from services.rclone import rclone_service


STANDARD_HEIGHTS = (1080, 720, 480, 360, 240, 144)
BOOTSTRAP_HLS_HEIGHT = 480
PLAYLIST_NAME = "playlist.m3u8"
MASTER_NAME = "master.m3u8"
COMPLETE_MARKER = ".complete"
FOREGROUND_PRIORITY = 0
BACKGROUND_PRIORITY = 100
FAST_HLS_VIDEO_CODECS = {"avc", "avc1", "h264"}
FAST_HLS_AUDIO_CODECS = {"aac", "mp4a"}


@dataclass(frozen=True, slots=True)
class VideoRendition:
    name: str
    label: str
    height: int
    width: int
    bandwidth: int
    original: bool = False


@dataclass(frozen=True, slots=True)
class AudioRendition:
    name: str
    label: str
    language: str
    stream_index: int
    default: bool
    codec: str = ""
    source: str = "embedded"
    file_name: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PlaybackMediaSnapshot:
    """Session-independent media fields safe to retain in background tasks."""

    id: str
    source_fingerprint: Optional[str]
    probed_duration: float
    container: str
    codec: str
    width: int
    height: int
    frame_rate: float
    quality: str
    audio_metadata: tuple[dict[str, Any], ...]
    languages: tuple[str, ...]


class PlaybackPreparationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PlaybackPrepService:
    def __init__(self) -> None:
        self.cache_dir = Path(settings.TEMP_DIR) / "playback_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.active_jobs: dict[str, asyncio.Task[None]] = {}
        self.running_jobs: set[str] = set()
        self.job_priorities: dict[str, int] = {}
        self.job_scheduled_at: dict[str, float] = {}
        self.job_started_at: dict[str, float] = {}
        concurrency = max(1, settings.PLAYBACK_TRANSCODE_CONCURRENCY)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.background_semaphore = asyncio.Semaphore(max(1, concurrency - 1))
        self._master_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._last_touch_at: dict[str, float] = {}

    @staticmethod
    def sanitize_diagnostics(value: str, limit: int = 2400) -> str:
        sanitized = re.sub(r"(?i)(authorization|token|ticket|secret|password)=?[^\s&]+", r"\1=[redacted]", value)
        sanitized = re.sub(r"https?://[^\s]+", "[remote-source]", sanitized)
        return sanitized.strip()[-limit:]

    def get_cache_path(self, media_id: str, fingerprint: str) -> str:
        safe_media = re.sub(r"[^a-zA-Z0-9_.-]", "_", media_id)
        safe_fingerprint = re.sub(r"[^a-fA-F0-9]", "", fingerprint)
        if not safe_fingerprint:
            raise PlaybackPreparationError("INVALID_FINGERPRINT", "The media fingerprint is invalid.")
        return str(self.cache_dir / safe_media / safe_fingerprint)

    def cache_path(self, media_id: str, fingerprint: str) -> Path:
        return Path(self.get_cache_path(media_id, fingerprint)).resolve()

    @staticmethod
    def _link_or_copy(source: str, destination: str) -> str:
        try:
            os.link(source, destination)
            return destination
        except OSError:
            return shutil.copy2(source, destination)

    def _reuse_complete_renditions(
        self,
        media_id: str,
        source_fingerprint: str,
        target_fingerprint: str,
        rendition_names: list[str],
    ) -> list[str]:
        if not source_fingerprint or source_fingerprint == target_fingerprint:
            return []
        source_root = self.cache_path(media_id, source_fingerprint)
        target_root = self.cache_path(media_id, target_fingerprint)
        if not source_root.is_dir():
            return []
        target_root.mkdir(parents=True, exist_ok=True)
        reused: list[str] = []
        for rendition_name in rendition_names:
            source_dir = source_root / rendition_name
            target_dir = target_root / rendition_name
            if target_dir.exists() or not self.rendition_complete(media_id, source_fingerprint, rendition_name):
                continue
            temporary = target_root / f".{rendition_name}.reuse.tmp"
            shutil.rmtree(temporary, ignore_errors=True)
            try:
                shutil.copytree(source_dir, temporary, copy_function=self._link_or_copy)
                try:
                    os.replace(temporary, target_dir)
                except OSError:
                    if not target_dir.exists():
                        raise
                reused.append(rendition_name)
            except OSError as exc:
                logger.warning(
                    f"[Playback Prep] Could not reuse {media_id}/{rendition_name}: "
                    f"{self.sanitize_diagnostics(str(exc), 300)}"
                )
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        return reused

    def reuse_video_renditions(
        self,
        media_id: str,
        source_fingerprint: str,
        target_fingerprint: str,
        media_obj: Any,
    ) -> list[str]:
        """Reuse immutable video-only HLS artifacts after an audio-sidecar change."""

        return self._reuse_complete_renditions(
            media_id,
            source_fingerprint,
            target_fingerprint,
            [rendition.name for rendition in self.video_renditions(media_obj)],
        )

    def reuse_verified_playback_cache(
        self,
        media_id: str,
        source_fingerprint: str,
        target_fingerprint: str,
        media_obj: Any,
    ) -> list[str]:
        """Migrate complete HLS renditions after a verified content-equivalent optimization."""

        rendition_names = [
            *(rendition.name for rendition in self.video_renditions(media_obj)),
            *(rendition.name for rendition in self.audio_renditions(media_obj)),
        ]
        return self._reuse_complete_renditions(
            media_id,
            source_fingerprint,
            target_fingerprint,
            rendition_names,
        )

    @staticmethod
    def snapshot_media(media_obj: Any) -> PlaybackMediaSnapshot:
        if isinstance(media_obj, PlaybackMediaSnapshot):
            return media_obj
        return PlaybackMediaSnapshot(
            id=str(media_obj.id),
            source_fingerprint=str(media_obj.source_fingerprint) if media_obj.source_fingerprint else None,
            probed_duration=max(0.0, float(media_obj.probed_duration or 0)),
            container=str(media_obj.container or ""),
            codec=str(media_obj.codec or ""),
            width=max(0, int(media_obj.width or 0)),
            height=max(0, int(media_obj.height or 0)),
            frame_rate=max(0.0, float(media_obj.frame_rate or 0)),
            quality=str(getattr(media_obj, "quality", "") or "Source"),
            audio_metadata=tuple(dict(item) for item in (media_obj.audio_metadata or [])),
            languages=tuple(str(item) for item in (media_obj.languages or [])),
        )

    @staticmethod
    def _width_for_height(source_width: int, source_height: int, height: int) -> int:
        if source_width > 0 and source_height > 0:
            width = round(source_width * height / source_height)
        else:
            width = round(height * 16 / 9)
        return max(2, width - width % 2)

    def video_renditions(self, media_obj: Any) -> list[VideoRendition]:
        source_height = max(1, int(getattr(media_obj, "height", 0) or 720))
        source_width = max(2, int(getattr(media_obj, "width", 0) or self._width_for_height(0, 0, source_height)))
        renditions = [
            VideoRendition(
                name="video_original",
                label=self._source_quality_label(media_obj, source_height),
                height=source_height,
                width=source_width - source_width % 2,
                bandwidth=max(700_000, min(12_000_000, source_height * 5200)),
                original=True,
            )
        ]
        for height in STANDARD_HEIGHTS:
            if height >= source_height:
                continue
            renditions.append(
                VideoRendition(
                    name=f"video_{height}p",
                    label=f"{height}p",
                    height=height,
                    width=self._width_for_height(source_width, source_height, height),
                    bandwidth=max(350_000, int(height / 1080 * 5_000_000)),
                )
            )
        return renditions

    @staticmethod
    def _source_quality_label(media_obj: Any, source_height: int) -> str:
        catalog_quality = str(getattr(media_obj, "quality", "") or "").strip()
        if catalog_quality and catalog_quality.lower() not in {"source", "unknown", "n/a"}:
            return catalog_quality
        return f"{source_height}p"

    @staticmethod
    def _audio_slug(language: str) -> str:
        return re.sub(r"[^a-z0-9-]", "-", language.lower()).strip("-") or "und"

    def audio_renditions(self, media_obj: Any) -> list[AudioRendition]:
        metadata = list(getattr(media_obj, "audio_metadata", []) or [])
        if not metadata:
            return []

        default_indexes = [int(item.get("index", 0)) for item in metadata if item.get("default")]
        default_index = default_indexes[0] if default_indexes else int(metadata[0].get("index", 0))
        renditions: list[AudioRendition] = []
        for position, item in enumerate(metadata):
            stream_index = int(item.get("index", position))
            language = normalize_language_tag(item.get("language"))
            label = language_label(language, item.get("label"))
            renditions.append(
                AudioRendition(
                    name=f"audio_{stream_index}_{self._audio_slug(language)}",
                    label=label,
                    language=language,
                    stream_index=stream_index,
                    default=stream_index == default_index,
                    codec=str(item.get("codec") or "").lower(),
                    source=str(item.get("source") or "embedded").lower(),
                    file_name=str(item.get("fileName") or item.get("file_name") or "") or None,
                )
            )
        return renditions

    def baseline_video(self, media_obj: Any) -> VideoRendition:
        renditions = self.video_renditions(media_obj)
        codec = str(getattr(media_obj, "codec", "") or "").lower()
        if codec in FAST_HLS_VIDEO_CODECS:
            return renditions[0]
        return next(
            (item for item in renditions if item.height <= BOOTSTRAP_HLS_HEIGHT),
            renditions[-1],
        )

    def playlist_ready(self, media_id: str, fingerprint: str, rendition_name: str) -> bool:
        rendition_dir = self.cache_path(media_id, fingerprint) / rendition_name
        playlist = rendition_dir / PLAYLIST_NAME
        try:
            content = playlist.read_text(encoding="utf-8")
        except OSError:
            return False
        init_match = re.search(r'#EXT-X-MAP:URI="([^"?#]+)"', content)
        segment_references = [
            line.strip().split("?", 1)[0]
            for line in content.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not init_match or not segment_references or "#EXTINF:" not in content:
            return False
        references = [init_match.group(1), *segment_references]
        for reference in references:
            relative = PurePosixPath(reference)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                return False
            asset = rendition_dir / Path(*relative.parts)
            try:
                if not asset.is_file() or asset.stat().st_size <= 0:
                    return False
            except OSError:
                return False
        return True

    def rendition_complete(self, media_id: str, fingerprint: str, rendition_name: str) -> bool:
        rendition_dir = self.cache_path(media_id, fingerprint) / rendition_name
        marker = rendition_dir / COMPLETE_MARKER
        if marker.is_file():
            return True
        playlist = rendition_dir / PLAYLIST_NAME
        try:
            if playlist.is_file() and "#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8"):
                marker.write_text("migrated", encoding="utf-8")
                return True
        except OSError:
            return False
        return False

    def rendition_error(self, media_id: str, fingerprint: str, rendition_name: str) -> Optional[dict[str, str]]:
        error_path = self.cache_path(media_id, fingerprint) / f"rendition-error-{rendition_name}.json"
        if not error_path.is_file():
            return None
        try:
            payload = json.loads(error_path.read_text(encoding="utf-8"))
            return {
                "code": str(payload.get("code", "RENDITION_FAILED")),
                "message": str(payload.get("message", "This rendition could not be prepared.")),
            }
        except (OSError, json.JSONDecodeError):
            return {"code": "RENDITION_FAILED", "message": "This rendition could not be prepared."}

    def rendition_status(self, media_id: str, fingerprint: str, rendition_name: str) -> str:
        if self.rendition_complete(media_id, fingerprint, rendition_name):
            return "ready"
        if self.rendition_error(media_id, fingerprint, rendition_name):
            return "failed"
        if self.playlist_ready(media_id, fingerprint, rendition_name):
            return "streamable"
        key = f"{media_id}:{fingerprint}:{rendition_name}"
        return "preparing" if key in self.active_jobs else "idle"

    def preparation_error(self, media_id: str, fingerprint: str) -> Optional[dict[str, str]]:
        error_path = self.cache_path(media_id, fingerprint) / "preparation-error.json"
        if not error_path.is_file():
            return None
        try:
            payload = json.loads(error_path.read_text(encoding="utf-8"))
            return {"code": str(payload.get("code", "PREPARATION_FAILED")), "message": str(payload.get("message", "Playback preparation failed."))}
        except (OSError, json.JSONDecodeError):
            return {"code": "PREPARATION_FAILED", "message": "Playback preparation failed."}

    def preparation_state(self, media_id: str, fingerprint: str, media_obj: Any) -> str:
        baseline = self.baseline_video(media_obj)
        audios = self.audio_renditions(media_obj)
        default_audio = next((item for item in audios if item.default), audios[0] if audios else None)
        required_names = [baseline.name, *([default_audio.name] if default_audio else [])]
        if any(self.rendition_status(media_id, fingerprint, name) == "failed" for name in required_names):
            return "error"
        video_ready = self.playlist_ready(media_id, fingerprint, baseline.name)
        audio_ready = default_audio is None or self.playlist_ready(media_id, fingerprint, default_audio.name)
        if video_ready and audio_ready and (self.cache_path(media_id, fingerprint) / MASTER_NAME).is_file():
            return "ready"
        return "preparing"

    def required_preparation_error(self, media_id: str, fingerprint: str, media_obj: Any) -> Optional[dict[str, str]]:
        baseline = self.baseline_video(media_obj)
        audios = self.audio_renditions(media_obj)
        default_audio = next((item for item in audios if item.default), audios[0] if audios else None)
        for rendition_name in [baseline.name, *([default_audio.name] if default_audio else [])]:
            failure = self.rendition_error(media_id, fingerprint, rendition_name)
            if failure:
                return failure
        return None

    def preparation_progress(self, media_id: str, fingerprint: str, media_obj: Any) -> dict[str, Any]:
        baseline = self.baseline_video(media_obj)
        audios = self.audio_renditions(media_obj)
        default_audio = next((item for item in audios if item.default), audios[0] if audios else None)
        required_names = [baseline.name, *([default_audio.name] if default_audio else [])]
        required_keys = [f"{media_id}:{fingerprint}:{name}" for name in required_names]
        ready_segments = len(list((self.cache_path(media_id, fingerprint) / baseline.name).glob("*.m4s")))
        failure = self.required_preparation_error(media_id, fingerprint, media_obj)
        state = self.preparation_state(media_id, fingerprint, media_obj)
        if failure or state == "error":
            stage = "failed"
        elif state == "ready":
            stage = "streamable"
        elif required_keys[0] in self.running_jobs:
            stage = "packaging" if self._can_fast_package_video(media_obj, baseline) else "transcoding"
        elif len(required_keys) > 1 and required_keys[1] in self.running_jobs:
            stage = "audio"
        else:
            stage = "queued"

        waiting = [
            key
            for key in sorted(
                (candidate for candidate in self.active_jobs if candidate not in self.running_jobs),
                key=lambda candidate: (
                    self.job_priorities.get(candidate, BACKGROUND_PRIORITY),
                    self.job_scheduled_at.get(candidate, 0),
                ),
            )
        ]
        positions = [waiting.index(key) + 1 for key in required_keys if key in waiting]
        return {
            "stage": stage,
            "queue_position": min(positions) if positions else 0,
            "ready_segments": ready_segments,
            "active_workers": sum(key in self.running_jobs for key in required_keys),
        }

    async def _preempt_background_jobs(self, required_keys: set[str]) -> None:
        cancelled: list[asyncio.Task[None]] = []
        for key, task in list(self.active_jobs.items()):
            if self.job_priorities.get(key, BACKGROUND_PRIORITY) <= FOREGROUND_PRIORITY:
                continue
            if key in required_keys and key in self.running_jobs:
                self.job_priorities[key] = FOREGROUND_PRIORITY
                continue
            parts = key.split(":", 2)
            if len(parts) == 3 and parts[2] != "remaining" and self.playlist_ready(parts[0], parts[1], parts[2]):
                continue
            task.cancel()
            cancelled.append(task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    async def prepare(
        self,
        media_id: str,
        media_obj: Any,
        source: ResolvedMediaSource,
        *,
        include_remaining: bool,
        retry_errors: bool = False,
        foreground: bool = True,
    ) -> str:
        media_obj = self.snapshot_media(media_obj)
        fingerprint = getattr(media_obj, "source_fingerprint", None) or source.fingerprint
        cache_path = self.cache_path(media_id, fingerprint)
        cache_path.mkdir(parents=True, exist_ok=True)
        self.touch(media_id, fingerprint)
        reused_video = self.reuse_video_renditions(
            media_id,
            source.video_fingerprint,
            fingerprint,
            media_obj,
        )
        if reused_video:
            logger.info(
                f"[Playback Prep] Reused {len(reused_video)} video rendition(s) for {media_id} "
                "after an audio-sidecar identity change."
            )
        if retry_errors:
            self.clear_preparation_error(media_id, fingerprint)

        baseline = self.baseline_video(media_obj)
        audios = self.audio_renditions(media_obj)
        default_audio = next((item for item in audios if item.default), audios[0] if audios else None)
        required_keys = {f"{media_id}:{fingerprint}:{baseline.name}"}
        if default_audio:
            required_keys.add(f"{media_id}:{fingerprint}:{default_audio.name}")
        if foreground:
            await self._preempt_background_jobs(required_keys)
        priority = FOREGROUND_PRIORITY if foreground else BACKGROUND_PRIORITY
        self._schedule_video(
            media_id,
            fingerprint,
            source,
            baseline,
            media_obj,
            priority,
            retry_errors=retry_errors,
        )
        if default_audio:
            self._schedule_audio(
                media_id,
                fingerprint,
                source,
                default_audio,
                media_obj,
                priority,
                retry_errors=retry_errors,
            )

        required_failed = any(self.rendition_error(media_id, fingerprint, name) for name in [baseline.name, *([default_audio.name] if default_audio else [])])
        if include_remaining and (retry_errors or not required_failed):
            self._schedule_remaining(media_id, fingerprint, source, media_obj, retry_errors=retry_errors)
        await self.rebuild_master(media_id, fingerprint, media_obj)
        return self.preparation_state(media_id, fingerprint, media_obj)

    async def prioritize_video_rendition(
        self,
        media_id: str,
        media_obj: Any,
        source: ResolvedMediaSource,
        rendition_name: str,
    ) -> str:
        """Move one requested quality ahead of queued background renditions."""

        media_obj = self.snapshot_media(media_obj)
        fingerprint = str(media_obj.source_fingerprint or source.fingerprint)
        rendition = next((item for item in self.video_renditions(media_obj) if item.name == rendition_name), None)
        if not rendition:
            raise PlaybackPreparationError("RENDITION_NOT_FOUND", "The requested quality does not exist for this source.")
        if self.rendition_status(media_id, fingerprint, rendition_name) in {"streamable", "ready"}:
            return self.rendition_status(media_id, fingerprint, rendition_name)

        requested_key = f"{media_id}:{fingerprint}:{rendition_name}"
        await self._preempt_background_jobs({requested_key})
        if self.playlist_ready(media_id, fingerprint, rendition_name):
            await self.rebuild_master(media_id, fingerprint, media_obj)
            return "streamable"
        self._schedule_video(
            media_id,
            fingerprint,
            source,
            rendition,
            media_obj,
            FOREGROUND_PRIORITY,
            retry_errors=True,
        )
        return "preparing"

    async def prioritize_audio_rendition(
        self,
        media_id: str,
        media_obj: Any,
        source: ResolvedMediaSource,
        rendition_name: str,
    ) -> str:
        """Move one requested audio track ahead of background rendition work."""

        media_obj = self.snapshot_media(media_obj)
        fingerprint = str(media_obj.source_fingerprint or source.fingerprint)
        rendition = next((item for item in self.audio_renditions(media_obj) if item.name == rendition_name), None)
        if not rendition:
            raise PlaybackPreparationError("RENDITION_NOT_FOUND", "The requested audio track does not exist for this source.")
        current_status = self.rendition_status(media_id, fingerprint, rendition_name)
        if current_status in {"streamable", "ready"}:
            return current_status

        requested_key = f"{media_id}:{fingerprint}:{rendition_name}"
        await self._preempt_background_jobs({requested_key})
        if self.playlist_ready(media_id, fingerprint, rendition_name):
            await self.rebuild_master(media_id, fingerprint, media_obj)
            return "streamable"
        self._schedule_audio(
            media_id,
            fingerprint,
            source,
            rendition,
            media_obj,
            FOREGROUND_PRIORITY,
            retry_errors=True,
        )
        return "preparing"

    def _schedule_remaining(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        media_obj: Any,
        *,
        retry_errors: bool = False,
    ) -> None:
        key = f"{media_id}:{fingerprint}:remaining"
        if key in self.active_jobs:
            return
        task = asyncio.create_task(
            self._schedule_remaining_after_baseline(
                media_id,
                fingerprint,
                source,
                media_obj,
                retry_errors=retry_errors,
            )
        )
        self._track_job(key, task, BACKGROUND_PRIORITY)

    async def _schedule_remaining_after_baseline(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        media_obj: Any,
        *,
        retry_errors: bool = False,
    ) -> None:
        for _ in range(120):
            if self.playlist_ready(media_id, fingerprint, self.baseline_video(media_obj).name):
                break
            if self.preparation_error(media_id, fingerprint):
                return
            await asyncio.sleep(1)
        else:
            return

        await asyncio.sleep(1)
        baseline_height = self.baseline_video(media_obj).height
        remaining_video = [item for item in self.video_renditions(media_obj) if item.height != baseline_height]
        remaining_video.sort(key=lambda item: (item.height > baseline_height, -item.height if item.height < baseline_height else item.height))
        for audio in self.audio_renditions(media_obj):
            self._schedule_audio(
                media_id,
                fingerprint,
                source,
                audio,
                media_obj,
                BACKGROUND_PRIORITY,
                retry_errors=retry_errors,
            )
        for rendition in remaining_video:
            self._schedule_video(
                media_id,
                fingerprint,
                source,
                rendition,
                media_obj,
                BACKGROUND_PRIORITY,
                retry_errors=retry_errors,
            )

    def _schedule_video(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: VideoRendition,
        media_obj: Any,
        priority: int = FOREGROUND_PRIORITY,
        *,
        retry_errors: bool = False,
    ) -> None:
        self._schedule_job(
            media_id,
            fingerprint,
            rendition.name,
            self._transcode_video(media_id, fingerprint, source, rendition, media_obj),
            priority,
            retry_errors=retry_errors,
        )

    def _schedule_audio(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: AudioRendition,
        media_obj: Any,
        priority: int = FOREGROUND_PRIORITY,
        *,
        retry_errors: bool = False,
    ) -> None:
        self._schedule_job(
            media_id,
            fingerprint,
            rendition.name,
            self._transcode_audio(media_id, fingerprint, source, rendition, media_obj),
            priority,
            retry_errors=retry_errors,
        )

    def _track_job(self, key: str, task: asyncio.Task[None], priority: int) -> None:
        self.active_jobs[key] = task
        self.job_priorities[key] = priority
        self.job_scheduled_at[key] = time.monotonic()

        def finished(completed: asyncio.Task[None]) -> None:
            if self.active_jobs.get(key) is completed:
                self.active_jobs.pop(key, None)
                self.job_priorities.pop(key, None)
                self.job_scheduled_at.pop(key, None)
                self.job_started_at.pop(key, None)

        task.add_done_callback(finished)

    def _schedule_job(
        self,
        media_id: str,
        fingerprint: str,
        rendition_name: str,
        coroutine: Any,
        priority: int,
        *,
        retry_errors: bool = False,
    ) -> None:
        key = f"{media_id}:{fingerprint}:{rendition_name}"
        if self.rendition_complete(media_id, fingerprint, rendition_name) or key in self.active_jobs:
            if key in self.active_jobs:
                self.job_priorities[key] = min(priority, self.job_priorities.get(key, priority))
            if hasattr(coroutine, "close"):
                coroutine.close()
            return
        if self.rendition_error(media_id, fingerprint, rendition_name) and not retry_errors:
            if hasattr(coroutine, "close"):
                coroutine.close()
            return
        if retry_errors:
            self._clear_rendition_error(media_id, fingerprint, rendition_name)
        task = asyncio.create_task(coroutine)
        self._track_job(key, task, priority)

    async def _input_process(self, source: ResolvedMediaSource) -> tuple[str, Optional[asyncio.subprocess.Process]]:
        if source.local_exists:
            return str(source.local_path), None
        if not source.cloud_exists or not source.cloud_path:
            raise PlaybackPreparationError("MEDIA_SOURCE_MISSING", "The media source is no longer available.")
        if not rclone_service.executable():
            raise PlaybackPreparationError("RCLONE_UNAVAILABLE", "Google Drive playback is unavailable because rclone is missing.")
        process = await asyncio.create_subprocess_exec(
            *rclone_service.command("cat", source.cloud_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return "pipe:0", process

    async def _pump_cloud_input(
        self,
        cloud_process: asyncio.subprocess.Process,
        ffmpeg_process: asyncio.subprocess.Process,
    ) -> None:
        assert cloud_process.stdout is not None
        assert ffmpeg_process.stdin is not None
        try:
            while chunk := await cloud_process.stdout.read(256 * 1024):
                ffmpeg_process.stdin.write(chunk)
                await ffmpeg_process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            ffmpeg_process.stdin.close()

    @staticmethod
    def _ffmpeg_executable() -> str:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise PlaybackPreparationError("FFMPEG_UNAVAILABLE", "FFmpeg is not installed or not executable.")
        return executable

    def _hls_output_args(self, target_dir: Path) -> list[str]:
        return [
            "-f", "hls",
            "-hls_time", "4",
            "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", "segment_%05d.m4s",
            PLAYLIST_NAME,
        ]

    @staticmethod
    def _output_signature(target_dir: Path) -> tuple[int, int, int]:
        try:
            files = [path for path in target_dir.iterdir() if path.is_file()]
            return (
                len(files),
                sum(path.stat().st_size for path in files),
                max((path.stat().st_mtime_ns for path in files), default=0),
            )
        except OSError:
            return (0, 0, 0)

    async def _wait_for_ffmpeg_progress(
        self,
        process: asyncio.subprocess.Process,
        target_dir: Path,
    ) -> None:
        stall_seconds = max(1, int(settings.PLAYBACK_JOB_STALL_SECONDS))
        last_signature = await asyncio.to_thread(self._output_signature, target_dir)
        last_activity = time.monotonic()
        wait_task = asyncio.create_task(process.wait())
        try:
            while not wait_task.done():
                await asyncio.wait({wait_task}, timeout=1)
                signature = await asyncio.to_thread(self._output_signature, target_dir)
                if signature != last_signature:
                    last_signature = signature
                    last_activity = time.monotonic()
                if not wait_task.done() and time.monotonic() - last_activity >= stall_seconds:
                    process.kill()
                    await wait_task
                    raise PlaybackPreparationError(
                        "PREPARATION_STALLED",
                        f"FFmpeg produced no HLS output for {stall_seconds} seconds.",
                    )
            await wait_task
        finally:
            if not wait_task.done():
                wait_task.cancel()

    @staticmethod
    async def _read_bounded_stream(
        stream: asyncio.StreamReader,
        limit: int = 2400,
    ) -> bytes:
        """Drain a child stream while retaining only its bounded diagnostic tail."""

        tail = bytearray()
        while chunk := await stream.read(64 * 1024):
            tail.extend(chunk)
            if len(tail) > limit:
                del tail[:-limit]
        return bytes(tail)

    async def _run_ffmpeg_job(
        self,
        media_id: str,
        fingerprint: str,
        rendition_name: str,
        source: ResolvedMediaSource,
        arguments: list[str],
        media_obj: Any,
    ) -> None:
        target_dir = self.cache_path(media_id, fingerprint) / rendition_name
        job_key = f"{media_id}:{fingerprint}:{rendition_name}"
        if self.rendition_complete(media_id, fingerprint, rendition_name):
            return

        async with AsyncExitStack() as capacity:
            if self.job_priorities.get(job_key, BACKGROUND_PRIORITY) > FOREGROUND_PRIORITY:
                await capacity.enter_async_context(self.background_semaphore)
            await capacity.enter_async_context(self.semaphore)
            if self.rendition_complete(media_id, fingerprint, rendition_name):
                return
            self.running_jobs.add(job_key)
            self.job_started_at[job_key] = time.monotonic()
            shutil.rmtree(target_dir, ignore_errors=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            cloud_process: Optional[asyncio.subprocess.Process] = None
            ffmpeg_process: Optional[asyncio.subprocess.Process] = None
            pump_task: Optional[asyncio.Task[None]] = None
            publish_task: Optional[asyncio.Task[None]] = None
            cloud_stderr_task: Optional[asyncio.Task[bytes]] = None
            ffmpeg_stderr_task: Optional[asyncio.Task[bytes]] = None
            try:
                actual_input, cloud_process = await self._input_process(source)
                command = [self._ffmpeg_executable(), "-hide_banner", "-nostdin", "-y", "-i", actual_input, *arguments, *self._hls_output_args(target_dir)]
                logger.info(f"[Playback Prep] Preparing {media_id} rendition {rendition_name}.")
                ffmpeg_process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(target_dir),
                    stdin=asyncio.subprocess.PIPE if cloud_process else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                publish_task = asyncio.create_task(
                    self._publish_when_streamable(media_id, fingerprint, rendition_name, media_obj, ffmpeg_process)
                )
                assert ffmpeg_process.stderr is not None
                ffmpeg_stderr_task = asyncio.create_task(self._read_bounded_stream(ffmpeg_process.stderr))
                if cloud_process:
                    pump_task = asyncio.create_task(self._pump_cloud_input(cloud_process, ffmpeg_process))
                    assert cloud_process.stderr is not None
                    cloud_stderr_task = asyncio.create_task(self._read_bounded_stream(cloud_process.stderr))
                await self._wait_for_ffmpeg_progress(ffmpeg_process, target_dir)
                stderr = await ffmpeg_stderr_task
                if cloud_process:
                    if pump_task:
                        await pump_task
                    try:
                        await asyncio.wait_for(cloud_process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        cloud_process.kill()
                        await cloud_process.wait()
                    cloud_stderr = await cloud_stderr_task
                    if cloud_process.returncode != 0:
                        diagnostics = self.sanitize_diagnostics(cloud_stderr.decode("utf-8", errors="replace"))
                        if diagnostics:
                            logger.error(
                                f"[Playback Prep] {media_id}/{rendition_name} cloud diagnostics: "
                                f"{self.sanitize_diagnostics(diagnostics, 800)}"
                            )
                        raise PlaybackPreparationError(
                            "CLOUD_STREAM_FAILED",
                            "Google Drive stopped delivering the media source.",
                        )
                if ffmpeg_process.returncode != 0:
                    diagnostics = self.sanitize_diagnostics(stderr.decode("utf-8", errors="replace"))
                    if diagnostics:
                        logger.error(
                            f"[Playback Prep] {media_id}/{rendition_name} FFmpeg diagnostics: "
                            f"{self.sanitize_diagnostics(diagnostics, 800)}"
                        )
                    raise PlaybackPreparationError(
                        "FFMPEG_PREPARATION_FAILED",
                        "FFmpeg could not prepare a browser-compatible stream for this rendition.",
                    )
                if not self.playlist_ready(media_id, fingerprint, rendition_name):
                    raise PlaybackPreparationError("EMPTY_RENDITION", "FFmpeg completed without producing playable HLS segments.")
                (target_dir / COMPLETE_MARKER).write_text(str(time.time()), encoding="utf-8")
                self._clear_preparation_error(media_id, fingerprint)
                self._clear_rendition_error(media_id, fingerprint, rendition_name)
                self.touch(media_id, fingerprint)
                await self.rebuild_master(media_id, fingerprint, media_obj)
                await asyncio.to_thread(self.enforce_lru_limits)
            except asyncio.CancelledError:
                shutil.rmtree(target_dir, ignore_errors=True)
                await self.rebuild_master(media_id, fingerprint, media_obj)
                raise
            except PlaybackPreparationError as exc:
                self._write_preparation_error(media_id, fingerprint, exc.code, str(exc))
                self._write_rendition_error(media_id, fingerprint, rendition_name, exc.code, str(exc))
                shutil.rmtree(target_dir, ignore_errors=True)
                await self.rebuild_master(media_id, fingerprint, media_obj)
                logger.error(f"[Playback Prep] {media_id}/{rendition_name} failed ({exc.code}): {self.sanitize_diagnostics(str(exc), 800)}")
            except Exception as exc:
                diagnostics = self.sanitize_diagnostics(str(exc), 800)
                self._write_preparation_error(
                    media_id,
                    fingerprint,
                    "INTERNAL_PREPARATION_ERROR",
                    "Playback preparation encountered an internal server error. Retry this title.",
                )
                self._write_rendition_error(
                    media_id,
                    fingerprint,
                    rendition_name,
                    "INTERNAL_PREPARATION_ERROR",
                    "This rendition encountered an internal preparation error.",
                )
                shutil.rmtree(target_dir, ignore_errors=True)
                await self.rebuild_master(media_id, fingerprint, media_obj)
                logger.error(f"[Playback Prep] {media_id}/{rendition_name} internal failure: {type(exc).__name__}: {diagnostics}")
            finally:
                auxiliary_tasks = [task for task in (publish_task, pump_task, cloud_stderr_task, ffmpeg_stderr_task) if task]
                for task in auxiliary_tasks:
                    if not task.done():
                        task.cancel()
                if auxiliary_tasks:
                    await asyncio.gather(*auxiliary_tasks, return_exceptions=True)
                for process in (ffmpeg_process, cloud_process):
                    if process and process.returncode is None:
                        process.kill()
                        await process.wait()
                self.running_jobs.discard(job_key)
                self.job_started_at.pop(job_key, None)

    async def _publish_when_streamable(
        self,
        media_id: str,
        fingerprint: str,
        rendition_name: str,
        media_obj: Any,
        process: asyncio.subprocess.Process,
    ) -> None:
        while process.returncode is None:
            if self.playlist_ready(media_id, fingerprint, rendition_name):
                self._clear_preparation_error(media_id, fingerprint)
                self._clear_rendition_error(media_id, fingerprint, rendition_name)
                self.touch(media_id, fingerprint)
                await self.rebuild_master(media_id, fingerprint, media_obj)
                return
            await asyncio.sleep(0.25)

    async def _transcode_video(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: VideoRendition,
        media_obj: Any,
    ) -> None:
        if self._can_fast_package_video(media_obj, rendition):
            arguments = ["-map", "0:v:0", "-an", "-c:v", "copy"]
            await self._run_ffmpeg_job(media_id, fingerprint, rendition.name, source, arguments, media_obj)
            return
        bitrate = max(300, rendition.bandwidth // 1000)
        arguments = [
            "-map", "0:v:0",
            "-an",
            "-vf", f"scale={rendition.width}:{rendition.height}:force_original_aspect_ratio=decrease,pad={rendition.width}:{rendition.height}:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-profile:v", "high",
            "-level", "4.1",
            "-pix_fmt", "yuv420p",
            "-crf", "22",
            "-maxrate", f"{bitrate}k",
            "-bufsize", f"{bitrate * 2}k",
            "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
        ]
        await self._run_ffmpeg_job(media_id, fingerprint, rendition.name, source, arguments, media_obj)

    @staticmethod
    def _can_fast_package_video(media_obj: Any, rendition: VideoRendition) -> bool:
        codec = str(getattr(media_obj, "codec", "") or "").lower()
        return rendition.original and codec in FAST_HLS_VIDEO_CODECS

    def _external_audio_path(self, source: ResolvedMediaSource, audio: AudioRendition) -> Optional[Path]:
        audio_dir = source.local_path.parent / "audio"
        if not audio_dir.is_dir():
            return None
        supported = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
        files = sorted(item for item in audio_dir.iterdir() if item.is_file() and item.suffix.lower() in supported)
        if audio.file_name:
            exact = audio_dir / audio.file_name
            if exact.is_file() and exact.suffix.lower() in supported:
                return exact
        language_match = next((item for item in files if normalize_language_tag(item.stem) == audio.language), None)
        if language_match:
            return language_match
        return files[audio.stream_index] if audio.source == "external" and 0 <= audio.stream_index < len(files) else None

    @staticmethod
    def _external_audio_source(source: ResolvedMediaSource, audio: AudioRendition, local_path: Optional[Path]) -> Optional[ResolvedMediaSource]:
        if local_path:
            return ResolvedMediaSource(
                catalog_path=source.catalog_path,
                relative_path=source.relative_path,
                local_path=local_path,
                cloud_path=None,
                local_exists=True,
                cloud_exists=False,
            )
        if audio.source != "external" or not audio.file_name or not source.cloud_path:
            return None
        if audio.file_name in {".", ".."} or "/" in audio.file_name or "\\" in audio.file_name:
            return None
        remote_parent = source.cloud_path.rsplit("/", 1)[0]
        remote_path = f"{remote_parent}/audio/{audio.file_name}"
        return ResolvedMediaSource(
            catalog_path=source.catalog_path,
            relative_path=source.relative_path,
            local_path=source.local_path.parent / "audio" / audio.file_name,
            cloud_path=remote_path,
            local_exists=False,
            cloud_exists=True,
            cloud_identity=remote_path,
        )

    async def _transcode_audio(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: AudioRendition,
        media_obj: Any,
    ) -> None:
        external_source = self._external_audio_source(source, rendition, self._external_audio_path(source, rendition))
        if external_source:
            arguments = ["-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
            await self._run_ffmpeg_job(media_id, fingerprint, rendition.name, external_source, arguments, media_obj)
            return
        if rendition.codec in FAST_HLS_AUDIO_CODECS:
            arguments = ["-map", f"0:a:{rendition.stream_index}", "-vn", "-c:a", "copy"]
            await self._run_ffmpeg_job(media_id, fingerprint, rendition.name, source, arguments, media_obj)
            return
        arguments = [
            "-map", f"0:a:{rendition.stream_index}",
            "-vn",
            "-c:a", "aac",
            "-b:a", "160k",
            "-ac", "2",
        ]
        await self._run_ffmpeg_job(media_id, fingerprint, rendition.name, source, arguments, media_obj)

    @staticmethod
    def _measured_playlist_bandwidth(playlist: Path) -> Optional[tuple[int, int]]:
        """Return peak and average bit rates from the fragments currently in a media playlist."""

        try:
            lines = playlist.read_text(encoding="utf-8").splitlines()
            samples: list[tuple[int, float]] = []
            pending_duration: Optional[float] = None
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#EXTINF:"):
                    pending_duration = float(stripped.split(":", 1)[1].split(",", 1)[0])
                    continue
                if not stripped or stripped.startswith("#") or pending_duration is None:
                    continue
                fragment = playlist.parent / Path(*PurePosixPath(stripped).parts)
                if fragment.is_file() and pending_duration > 0:
                    samples.append((fragment.stat().st_size, pending_duration))
                pending_duration = None
            if not samples:
                return None
            peak = max(round(size * 8 / duration) for size, duration in samples)
            total_size = sum(size for size, _ in samples)
            total_duration = sum(duration for _, duration in samples)
            average = round(total_size * 8 / total_duration)
            return max(1, peak), max(1, average)
        except (OSError, ValueError, ZeroDivisionError):
            return None

    async def rebuild_master(self, media_id: str, fingerprint: str, media_obj: Any) -> Optional[Path]:
        cache_path = self.cache_path(media_id, fingerprint)
        lock_key = f"{media_id}:{fingerprint}"
        lock = self._master_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            videos = [item for item in self.video_renditions(media_obj) if self.playlist_ready(media_id, fingerprint, item.name)]
            audios = [item for item in self.audio_renditions(media_obj) if self.playlist_ready(media_id, fingerprint, item.name)]
            if not videos:
                return None

            lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
            measured_audio = [
                measurement
                for audio in audios
                if (measurement := await asyncio.to_thread(
                    self._measured_playlist_bandwidth,
                    cache_path / audio.name / PLAYLIST_NAME,
                )) is not None
            ]
            audio_peak = max((measurement[0] for measurement in measured_audio), default=0)
            audio_average = max((measurement[1] for measurement in measured_audio), default=0)
            if audios:
                for audio in audios:
                    attributes = [
                        "TYPE=AUDIO",
                        'GROUP-ID="audio"',
                        f'NAME="{audio.label.replace(chr(34), "")}"',
                        f'LANGUAGE="{audio.language}"',
                        f"DEFAULT={'YES' if audio.default else 'NO'}",
                        "AUTOSELECT=YES",
                        f'URI="{audio.name}/{PLAYLIST_NAME}"',
                    ]
                    lines.append(f"#EXT-X-MEDIA:{','.join(attributes)}")
            for video in videos:
                measured = await asyncio.to_thread(
                    self._measured_playlist_bandwidth,
                    cache_path / video.name / PLAYLIST_NAME,
                )
                video_peak, video_average = measured or (video.bandwidth, int(video.bandwidth * 0.82))
                peak_bandwidth = video_peak + audio_peak
                average_bandwidth = video_average + audio_average
                attributes = [
                    f"BANDWIDTH={peak_bandwidth}",
                    f"AVERAGE-BANDWIDTH={average_bandwidth}",
                    f"RESOLUTION={video.width}x{video.height}",
                    f'NAME="{video.label}"',
                ]
                if audios:
                    attributes.append('AUDIO="audio"')
                lines.extend([f"#EXT-X-STREAM-INF:{','.join(attributes)}", f"{video.name}/{PLAYLIST_NAME}"])

            target = cache_path / MASTER_NAME
            temporary = target.with_suffix(".tmp")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(temporary, target)
            return target

    def _write_preparation_error(self, media_id: str, fingerprint: str, code: str, message: str) -> None:
        target = self.cache_path(media_id, fingerprint) / "preparation-error.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"code": code, "message": self.sanitize_diagnostics(message)}), encoding="utf-8")
        os.replace(temporary, target)

    def _write_rendition_error(self, media_id: str, fingerprint: str, rendition_name: str, code: str, message: str) -> None:
        target = self.cache_path(media_id, fingerprint) / f"rendition-error-{rendition_name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"code": code, "message": self.sanitize_diagnostics(message)}),
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def _clear_preparation_error(self, media_id: str, fingerprint: str) -> None:
        (self.cache_path(media_id, fingerprint) / "preparation-error.json").unlink(missing_ok=True)

    def clear_preparation_error(self, media_id: str, fingerprint: str) -> None:
        self._clear_preparation_error(media_id, fingerprint)

    def _clear_rendition_error(self, media_id: str, fingerprint: str, rendition_name: str) -> None:
        (self.cache_path(media_id, fingerprint) / f"rendition-error-{rendition_name}.json").unlink(missing_ok=True)

    def touch(self, media_id: str, fingerprint: str) -> None:
        path = self.cache_path(media_id, fingerprint)
        path.mkdir(parents=True, exist_ok=True)
        key = str(path)
        now = time.monotonic()
        if now - self._last_touch_at.get(key, 0) < 30:
            return
        try:
            os.utime(path, None)
            self._last_touch_at[key] = now
            if len(self._last_touch_at) > 4096:
                cutoff = now - 24 * 60 * 60
                self._last_touch_at = {
                    cached_path: touched_at
                    for cached_path, touched_at in self._last_touch_at.items()
                    if touched_at >= cutoff
                }
        except OSError:
            pass

    def cancel_media(self, media_id: str, fingerprint: Optional[str] = None) -> None:
        prefix = f"{media_id}:{fingerprint}:" if fingerprint else f"{media_id}:"
        for key, task in list(self.active_jobs.items()):
            if key.startswith(prefix):
                task.cancel()

    async def shutdown(self, timeout: float = 8.0) -> None:
        """Cancel adaptive preparation and wait a bounded time for child cleanup."""

        tasks = list(self.active_jobs.values())
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=max(0.1, timeout))
        if pending:
            logger.error(
                f"[Playback Prep] {len(pending)} preparation task(s) did not stop "
                f"within {timeout:.1f} seconds; lifecycle cleanup will terminate their process group."
            )

    def recover_interrupted_outputs(self) -> None:
        if not self.cache_dir.exists():
            return
        for path in self.cache_dir.rglob("*.tmp"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        for playlist in self.cache_dir.rglob(PLAYLIST_NAME):
            rendition_dir = playlist.parent
            marker = rendition_dir / COMPLETE_MARKER
            if marker.is_file():
                continue
            try:
                if "#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8"):
                    marker.write_text("migrated", encoding="utf-8")
                    continue
            except OSError:
                pass
            shutil.rmtree(rendition_dir, ignore_errors=True)

    async def reconcile_catalog_cache_identities(self) -> None:
        from db import engine
        from models import Episode, Movie
        from sqlmodel import select
        from sqlmodel.ext.asyncio.session import AsyncSession

        self.recover_interrupted_outputs()
        async with AsyncSession(engine, expire_on_commit=False) as db:
            movies = (await db.exec(select(Movie).where(Movie.video_url != ""))).all()
            episodes = (await db.exec(select(Episode).where(Episode.video_url != ""))).all()
            for media_obj in [*movies, *episodes]:
                try:
                    source = await resolve_media_source(media_obj.video_url)
                    if not source.available:
                        continue
                    fingerprint = playback_source_fingerprint(source, media_obj.audio_metadata or [])
                    if media_obj.source_fingerprint != fingerprint:
                        if media_obj.source_fingerprint:
                            self.cancel_media(media_obj.id, media_obj.source_fingerprint)
                        media_obj.source_fingerprint = fingerprint
                        db.add(media_obj)
                except Exception as exc:
                    logger.warning(f"[Playback Prep] Cache identity reconciliation skipped for {media_obj.id}: {self.sanitize_diagnostics(str(exc), 400)}")
            await db.commit()

    async def schedule_catalog_baselines(self) -> None:
        """Compatibility entry point; startup reconciles cache identity without queuing FFmpeg work."""

        await self.reconcile_catalog_cache_identities()

    def enforce_lru_limits(self) -> None:
        limit_bytes = int(settings.PLAYBACK_CACHE_GB * 1024 * 1024 * 1024)
        if limit_bytes <= 0 or not self.cache_dir.exists():
            return
        active_roots = {
            str(self.cache_path(parts[0], parts[1]))
            for key in self.active_jobs
            if len(parts := key.split(":")) >= 3
        }
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for media_dir in self.cache_dir.iterdir():
            if not media_dir.is_dir():
                continue
            for fingerprint_dir in media_dir.iterdir():
                if not fingerprint_dir.is_dir():
                    continue
                size = sum(item.stat().st_size for item in fingerprint_dir.rglob("*") if item.is_file())
                total += size
                entries.append((fingerprint_dir.stat().st_mtime, size, fingerprint_dir))
        if total <= limit_bytes:
            return
        for _, size, path in sorted(entries, key=lambda item: item[0]):
            if total <= limit_bytes:
                break
            if str(path.resolve()) in active_roots:
                continue
            shutil.rmtree(path, ignore_errors=True)
            total -= size


playback_prep_service = PlaybackPrepService()
