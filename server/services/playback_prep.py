from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import weakref
import uuid
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Iterator, Optional

from config import settings
from services.logger import logger
from services.languages import language_label, normalize_language_tag
from services.media_timing import canonical_audio_filter_chain
from services.ffmpeg_input import generated_hls_input_options, local_media_input_options, native_media_environment
from services.media_source import ResolvedMediaSource, clear_cloud_object_cache, cloud_read_enabled, playback_source_fingerprint, resolve_media_source, resolve_related_source
from services.rclone import rclone_service
from services.state import register_process, unregister_process
from lifecycle.coordination import (
    AdmissionClosedError,
    EffectClass,
    run_cancellation_safe_thread,
    runtime_work,
)
from lifecycle.media_work import MediaRecoveryRequired, media_ownership, require_resolved_media, settle, supervised_output
from services.storage_capacity import StorageCapacityError, storage_capacity
from lifecycle.storage_retention import retain_existing_tree


STANDARD_HEIGHTS = (1080, 720, 480, 360, 240, 144)
BOOTSTRAP_HLS_HEIGHT = 480
PLAYLIST_NAME = "playlist.m3u8"
MASTER_NAME = "master.m3u8"
COMPLETE_MARKER = ".complete"
VIDEO_VERIFIED_MARKER = ".verified-v2"
AUDIO_VERIFIED_MARKER = ".verified-audio-v5"
RETAINED_INVALID_MARKER = ".retained-invalid"
FOREGROUND_PRIORITY = 0
BACKGROUND_PRIORITY = 100
FAST_HLS_VIDEO_CODECS = {"avc", "avc1", "h264"}
MAX_HLS_START_DELTA_SECONDS = 0.1
MAX_HLS_END_DELTA_SECONDS = 0.25


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
    absolute_stream_index: bool
    default: bool
    codec: str = ""
    source: str = "embedded"
    file_name: Optional[str] = None
    timeline_offset: float = 0.0
    duration: float = 0.0


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


@dataclass(frozen=True, slots=True)
class PlaylistReadiness:
    ready: bool = False
    duration: float = 0.0
    segment_count: int = 0


_readiness_snapshot: ContextVar[Optional[dict[tuple[int, str], PlaylistReadiness]]] = ContextVar(
    "playback_readiness_snapshot", default=None,
)


class PlaybackPrepService:
    def __init__(self) -> None:
        self.cache_dir = Path(settings.TEMP_DIR) / "playback_cache"
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
        self._active_cache_leases: dict[str, float] = {}
        self._cloud_spool_locks: dict[str, asyncio.Lock] = {}
        self._run_cache_roots: dict[str, tuple[str, str]] = {}
        self._run_cache_renditions: dict[str, set[str]] = {}
        self._rendition_cache_leases: dict[str, float] = {}
        self._update_quiesced = False

    def initialize_storage(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def protect_run(self, run_id: str, media_id: str, fingerprint: str) -> None:
        """Track run identity without claiming any not-yet-delivered rendition."""
        if not fingerprint:
            return
        identity = (str(media_id), fingerprint)
        if self._run_cache_roots.get(run_id) != identity:
            self._run_cache_renditions[run_id] = set()
        self._run_cache_roots[run_id] = identity
        self._run_cache_renditions.setdefault(run_id, set())

    def protect_rendition(self, run_id: str, media_id: str, fingerprint: str, rendition_name: str) -> None:
        """Claim exact advertised/delivered bytes before an HTTP response yields."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", rendition_name):
            return
        if self._run_cache_roots.get(run_id) != (str(media_id), fingerprint):
            self.protect_run(run_id, media_id, fingerprint)
        self._run_cache_renditions.setdefault(run_id, set()).add(rendition_name)
        path = self.cache_path(media_id, fingerprint) / rendition_name
        self._rendition_cache_leases[str(path)] = time.monotonic() + 120

    def release_run(self, run_id: str) -> None:
        self._run_cache_roots.pop(run_id, None)
        self._run_cache_renditions.pop(run_id, None)

    async def reconcile_run_protections(self) -> None:
        from db import engine
        from models import PlaybackRun
        from sqlmodel import select
        from sqlmodel.ext.asyncio.session import AsyncSession

        async with AsyncSession(engine) as db:
            runs = (await db.exec(select(PlaybackRun).where(PlaybackRun.lifecycle_state == "active"))).all()
        identities = {
            run.id: (str(run.episode_id or run.movie_id), str(run.source_fingerprint))
            for run in runs if run.source_kind == "catalog" and run.source_fingerprint
        }
        previous = self._run_cache_roots
        self._run_cache_roots = identities
        self._run_cache_renditions = {
            run_id: self._run_cache_renditions.get(run_id, set())
            for run_id, identity in identities.items() if previous.get(run_id) == identity
        }
        for run_id, (media_id, fingerprint) in identities.items():
            if run_id in self._run_cache_renditions:
                continue
            # A persisted pre-start run may have mounted any legacy rendition.
            # No receipt proves which paths were exposed before this process.
            root = self.cache_path(media_id, fingerprint)
            self._run_cache_renditions[run_id] = {
                path.name for path in root.iterdir()
                if path.is_dir() and re.fullmatch(r"[a-zA-Z0-9_-]+", path.name)
            } if root.is_dir() else set()

    async def is_media_protected(self, media_id: str) -> bool:
        return any(owner == media_id for owner, _ in self._run_cache_roots.values()) or any(
            key.startswith(f"{media_id}:") for key in self.active_jobs
        )

    def enqueue_cache_miss(self, media_obj: Any, source: ResolvedMediaSource, *, retry: bool = False) -> None:
        """Queue the approved same-generation exception; HTTP never runs FFmpeg."""
        from services.storage_inventory import hybrid_policy_active

        if not hybrid_policy_active() or self._update_quiesced:
            return
        media = self.snapshot_media(media_obj)
        fingerprint = str(media.source_fingerprint or "")
        if not fingerprint or playback_source_fingerprint(source, media.audio_metadata) != fingerprint:
            return
        if self.preparation_state(media.id, fingerprint, media) == "ready":
            return
        if not retry and self.required_preparation_error(media.id, fingerprint, media):
            return
        key = f"{media.id}:{fingerprint}:cache-miss"
        if key in self.active_jobs:
            return

        async def prepare_accepted() -> None:
            try:
                await self.prepare(media.id, media, source, include_remaining=False, retry_errors=retry, foreground=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", "PREPARATION_FAILED")
                self._write_preparation_error(media.id, fingerprint, code, "Playback preparation could not start. Retry this title.")

        self._track_job(key, asyncio.create_task(prepare_accepted(), name=f"playback-cache-miss:{media.id}"), FOREGROUND_PRIORITY)

    async def ensure_offline_baseline(self, media_obj: Any, source: ResolvedMediaSource, *, timeout: float = 6 * 3600) -> bool:
        """Finish verified baseline video/default audio before an offline-ready claim."""
        media = self.snapshot_media(media_obj)
        fingerprint = str(media.source_fingerprint or "")
        if not fingerprint or playback_source_fingerprint(source, media.audio_metadata) != fingerprint:
            return False
        baseline = self.baseline_video(media)
        audios = self.audio_renditions(media)
        default = next((audio for audio in audios if audio.default), audios[0] if audios else None)
        names = [baseline.name, *([default.name] if default else [])]
        await self.prepare(media.id, media, source, include_remaining=False, retry_errors=True, foreground=False)
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if all(self.rendition_complete(media.id, fingerprint, name) for name in names):
                return True
            if self.required_preparation_error(media.id, fingerprint, media):
                return False
            tasks = [task for key, task in self.active_jobs.items() if key.startswith(f"{media.id}:{fingerprint}:") and not task.done()]
            if not tasks:
                return False
            await asyncio.wait(tasks, timeout=min(1.0, max(0.0, deadline - time.monotonic())))
        self.cancel_media(media.id, fingerprint)
        tasks = [task for key, task in self.active_jobs.items() if key.startswith(f"{media.id}:{fingerprint}:")]
        if tasks:
            await settle(asyncio.gather(*tasks, return_exceptions=True))
        return False

    async def cold_recovery_ready(self, media_obj: Any, source: ResolvedMediaSource) -> bool:
        """Prove the existing full-spool recovery path for this exact generation."""
        from services.media_analysis import current_duration_analysis, stored_analysis

        analysis = stored_analysis(getattr(media_obj, "analysis_metadata_str", None))
        fingerprint = str(getattr(media_obj, "source_fingerprint", "") or "")
        if (not source.available or not current_duration_analysis(analysis)
                or analysis.get("video_fingerprint") != source.video_fingerprint
                or analysis.get("source_fingerprint") != fingerprint
                or playback_source_fingerprint(source, media_obj.audio_metadata or []) != fingerprint
                or not shutil.which("ffmpeg") or not shutil.which("ffprobe")):
            return False
        baseline = self.baseline_video(media_obj)
        audios = self.audio_renditions(media_obj)
        default = next((audio for audio in audios if audio.default), audios[0] if audios else None)
        names = [baseline.name, *([default.name] if default else [])]
        if not all(self.rendition_complete(str(media_obj.id), fingerprint, name) for name in names):
            return False
        try:
            source_bytes = source.local_path.stat().st_size if source.local_exists else int(source.cloud_size or 0)
            if source_bytes <= 0:
                return False
            peak = source_bytes + sum(self.rendition_capacity_bytes(media_obj, name) for name in names)
            if default and default.source == "external":
                external = await self._external_audio_source(source, default, self._external_audio_path(source, default))
                if external is None:
                    return False
                peak += external.local_path.stat().st_size if external.local_exists else int(external.cloud_size or 0)
            async with runtime_work("playback.cold-recovery-proof", [EffectClass.DB_WRITE]):
                async with storage_capacity.allocation(f"playback-cold-proof:{media_obj.id}", peak, self.cache_dir, track_materialized=False):
                    return True
        except (StorageCapacityError, PlaybackPreparationError, AdmissionClosedError, OSError):
            return False

    @property
    def update_quiesced(self) -> bool:
        """Report whether managed update handoff has paused cache preparation."""

        return self._update_quiesced

    def resume_after_update_quiesce(self) -> None:
        """Allow cache preparation again when a pre-cutover update is cancelled."""

        self._update_quiesced = False

    async def quiesce_for_update(self, timeout: float = 8.0) -> int:
        """Stop cancellable cache warming before a managed update cutover."""

        self._update_quiesced = True
        tasks = [task for task in self.active_jobs.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if not tasks:
            return 0
        _, pending = await asyncio.wait(tasks, timeout=max(0.1, timeout))
        return len(pending)

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
            temporary = target_root / f".{rendition_name}.reuse-{uuid.uuid4().hex}.tmp"
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
            # Failed or ambiguous copies remain beside their generation.
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

    def _audio_rendition_name(
        self,
        *,
        source_kind: str,
        stream_index: int,
        position: int,
        language: str,
        file_name: Optional[str],
    ) -> str:
        source_slug = "external" if source_kind == "external" else "embedded"
        identity = json.dumps(
            {
                "source": source_slug,
                "streamIndex": stream_index,
                "position": position,
                "fileName": str(file_name or "").casefold(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        return f"audio_{source_slug}_{stream_index}_{identity_hash}_{self._audio_slug(language)}"

    def audio_renditions(self, media_obj: Any) -> list[AudioRendition]:
        metadata = list(getattr(media_obj, "audio_metadata", []) or [])
        if not metadata:
            return []

        default_positions = [position for position, item in enumerate(metadata) if item.get("default")]
        default_position = default_positions[0] if default_positions else 0
        renditions: list[AudioRendition] = []
        for position, item in enumerate(metadata):
            source_kind = str(item.get("source") or "embedded").lower()
            absolute_stream_index = source_kind == "embedded" and "streamIndex" in item
            stream_index = int(
                item.get("streamIndex", item.get("index", position))
                if source_kind == "embedded"
                else item.get("index", position)
            )
            language = normalize_language_tag(item.get("language"))
            label = language_label(language, item.get("label"))
            file_name = str(item.get("fileName") or item.get("file_name") or "") or None
            renditions.append(
                AudioRendition(
                    name=self._audio_rendition_name(
                        source_kind=source_kind,
                        stream_index=stream_index,
                        position=position,
                        language=language,
                        file_name=file_name,
                    ),
                    label=label,
                    language=language,
                    stream_index=stream_index,
                    absolute_stream_index=absolute_stream_index,
                    default=position == default_position,
                    codec=str(item.get("codec") or "").lower(),
                    source=source_kind,
                    file_name=file_name,
                    timeline_offset=float(item.get("timelineOffset") or 0.0),
                    duration=max(0.0, float(item.get("duration") or 0.0)),
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

    def _start_rendition_candidate(self, media_id: str, fingerprint: str, rendition_name: str) -> Path:
        """Keep every prior generation before opening a fresh exclusive output."""
        root = self.cache_path(media_id, fingerprint)
        target = root / rendition_name
        if target.exists():
            protected = any(
                identity == (str(media_id), fingerprint)
                and rendition_name in self._run_cache_renditions.get(run_id, set())
                for run_id, identity in self._run_cache_roots.items()
            )
            leased = self._rendition_cache_leases.get(str(target), 0) > time.monotonic()
            if protected or leased:
                raise PlaybackPreparationError(
                    "RETAINED_CACHE_IN_USE",
                    "The retained playback generation is still in use. Retry after closing its players.",
                )
            retain_existing_tree(target)
        root.mkdir(parents=True, exist_ok=True)
        candidate = root / f".{rendition_name}.candidate-{uuid.uuid4().hex}"
        candidate.mkdir(exist_ok=False)
        candidate.rename(target)
        return target

    @staticmethod
    def _invalidate_retained_rendition(target: Path) -> None:
        # Acceptance may have completed inside a cancellation-safe thread even
        # when its awaiting coroutine did not receive the acknowledgement.
        verified = target / PlaybackPrepService._verified_marker_name(target.name)
        if (target / COMPLETE_MARKER).is_file() and verified.is_file():
            return
        # Eligibility changes never remove cache or subtitle bytes.
        with (target / RETAINED_INVALID_MARKER).open("w", encoding="utf-8") as marker:
            marker.write("unaccepted")
            marker.flush()
            os.fsync(marker.fileno())

    @staticmethod
    def _accept_rendition(target: Path, rendition_name: str) -> None:
        """Persist accepted output before cancellable post-completion work."""
        for asset in target.rglob("*"):
            if asset.is_file() and not asset.is_symlink():
                with asset.open("rb") as stream:
                    os.fsync(stream.fileno())
        for name in (PlaybackPrepService._verified_marker_name(rendition_name), COMPLETE_MARKER):
            with (target / name).open("w", encoding="utf-8") as marker:
                marker.write("1")
                marker.flush()
                os.fsync(marker.fileno())
        if os.name != "nt":
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def retained_asset_available(self, media_id: str, fingerprint: str, path: str) -> bool:
        """Historical and private cache paths cannot become a playback API."""
        relative = PurePosixPath(path)
        parts = relative.parts
        if relative.is_absolute() or not parts or any(part.startswith(".") or part in {"", ".."} for part in parts):
            return False
        if len(parts) == 1:
            return parts[0] == MASTER_NAME
        rendition_name = parts[0]
        target = self.cache_path(media_id, fingerprint) / rendition_name
        if (target / RETAINED_INVALID_MARKER).exists():
            return False
        key = f"{media_id}:{fingerprint}:{rendition_name}"
        # Segment delivery must remain O(1): completed receipt markers establish
        # accepted eligibility; manifests/readiness validate the full playlist.
        accepted = ((target / COMPLETE_MARKER).is_file()
                    and (target / self._verified_marker_name(rendition_name)).is_file())
        return accepted or key in self.running_jobs

    @contextmanager
    def readiness_snapshot(self) -> Iterator[None]:
        """Deduplicate reads within one response, never across requests or writers."""
        token = _readiness_snapshot.set({})
        try:
            yield
        finally:
            _readiness_snapshot.reset(token)

    def playlist_readiness(self, media_id: str, fingerprint: str, rendition_name: str) -> PlaylistReadiness:
        rendition_dir = self.cache_path(media_id, fingerprint) / rendition_name
        snapshot = _readiness_snapshot.get()
        key = (id(self), str(rendition_dir))
        if snapshot is not None and key in snapshot:
            return snapshot[key]
        result = self._read_playlist_readiness(rendition_dir)
        if snapshot is not None:
            snapshot[key] = result
        return result

    @staticmethod
    def _read_playlist_readiness(rendition_dir: Path) -> PlaylistReadiness:
        playlist = rendition_dir / PLAYLIST_NAME
        if (rendition_dir / RETAINED_INVALID_MARKER).exists():
            return PlaylistReadiness()
        try:
            content = playlist.read_text(encoding="utf-8")
        except OSError:
            return PlaylistReadiness()
        duration = sum(float(value) for value in re.findall(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?)", content, re.MULTILINE))
        init_match = re.search(r'#EXT-X-MAP:URI="([^"?#]+)"', content)
        segment_references = [
            line.strip().split("?", 1)[0]
            for line in content.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        info = PlaylistReadiness(False, duration, len(segment_references))
        if not init_match or not segment_references or "#EXTINF:" not in content:
            return info
        references = [init_match.group(1), *segment_references]
        try:
            # DirEntry reuses file-type information; each flat asset needs one stat.
            with os.scandir(rendition_dir) as directory:
                entries = {entry.name: entry for entry in directory}
        except OSError:
            return info
        for reference in references:
            relative = PurePosixPath(reference)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                return info
            try:
                if len(relative.parts) == 1:
                    entry = entries.get(reference)
                    if entry is None or not entry.is_file() or entry.stat().st_size <= 0:
                        return info
                    continue
                asset = rendition_dir / Path(*relative.parts)
                if not asset.is_file() or asset.stat().st_size <= 0:
                    return info
            except OSError:
                return info
        return PlaylistReadiness(True, duration, len(segment_references))

    def playlist_ready(self, media_id: str, fingerprint: str, rendition_name: str) -> bool:
        return self.playlist_readiness(media_id, fingerprint, rendition_name).ready

    def rendition_complete(self, media_id: str, fingerprint: str, rendition_name: str) -> bool:
        rendition_dir = self.cache_path(media_id, fingerprint) / rendition_name
        marker = rendition_dir / COMPLETE_MARKER
        verified = rendition_dir / self._verified_marker_name(rendition_name)
        if (rendition_dir / RETAINED_INVALID_MARKER).exists():
            return False
        if marker.is_file() and verified.is_file() and self.playlist_ready(media_id, fingerprint, rendition_name):
            return True
        return False

    @staticmethod
    def _verified_marker_name(rendition_name: str) -> str:
        return AUDIO_VERIFIED_MARKER if rendition_name.startswith("audio_") else VIDEO_VERIFIED_MARKER

    def rendition_verified(self, media_id: str, fingerprint: str, rendition_name: str) -> bool:
        return self.rendition_complete(media_id, fingerprint, rendition_name)

    def playback_ready(self, media_id: str, fingerprint: str, media_obj: Any) -> bool:
        baseline = self.baseline_video(media_obj)
        audios = self.audio_renditions(media_obj)
        default_audio = next((item for item in audios if item.default), audios[0] if audios else None)
        required_names = [baseline.name, *([default_audio.name] if default_audio else [])]
        return (
            all(self.rendition_verified(media_id, fingerprint, name) for name in required_names)
            and (self.cache_path(media_id, fingerprint) / MASTER_NAME).is_file()
        )

    def _verify_rendition_output(self, target_dir: Path, rendition_name: str) -> None:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise PlaybackPreparationError("FFPROBE_UNAVAILABLE", "FFprobe is required to verify adaptive playback output.")
        playlist = target_dir / PLAYLIST_NAME
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                *generated_hls_input_options(),
                "-show_entries", "stream=codec_type,codec_name,profile,sample_rate,channels,start_time",
                "-of", "json",
                str(playlist),
            ],
            check=False,
            env=native_media_environment(),
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            diagnostics = self.sanitize_diagnostics(result.stderr, 800)
            raise PlaybackPreparationError(
                "RENDITION_VERIFICATION_FAILED",
                f"FFprobe rejected the prepared adaptive rendition. {diagnostics}".strip(),
            )
        try:
            streams = json.loads(result.stdout).get("streams", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise PlaybackPreparationError(
                "RENDITION_VERIFICATION_FAILED",
                "FFprobe returned invalid adaptive rendition metadata.",
            ) from exc
        expected_type = "audio" if rendition_name.startswith("audio_") else "video"
        if not any(str(stream.get("codec_type")) == expected_type for stream in streams):
            raise PlaybackPreparationError(
                "RENDITION_VERIFICATION_FAILED",
                f"The prepared adaptive rendition contains no {expected_type} stream.",
            )
        if expected_type == "audio":
            audio_stream = next(stream for stream in streams if str(stream.get("codec_type")) == "audio")
            if (
                str(audio_stream.get("codec_name") or "").lower() != "aac"
                or str(audio_stream.get("profile") or "").upper() != "LC"
                or str(audio_stream.get("sample_rate") or "") != "48000"
                or str(audio_stream.get("channels") or "") != "2"
            ):
                raise PlaybackPreparationError(
                    "RENDITION_VERIFICATION_FAILED",
                    "The prepared adaptive audio does not match the AAC-LC 48 kHz stereo contract.",
                )
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise PlaybackPreparationError(
                    "FFMPEG_UNAVAILABLE",
                    "FFmpeg is required to verify adaptive audio output.",
                )
            decode_result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-v", "error",
                    "-xerror",
                    *generated_hls_input_options(),
                    "-i", str(playlist),
                    "-map", "0:a:0",
                    "-f", "null",
                    "-",
                ],
                check=False,
                env=native_media_environment(),
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if decode_result.returncode != 0:
                diagnostics = self.sanitize_diagnostics(decode_result.stderr, 800)
                raise PlaybackPreparationError(
                    "RENDITION_VERIFICATION_FAILED",
                    f"FFmpeg rejected the prepared adaptive audio. {diagnostics}".strip(),
                )

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

    def rendition_seekable_until(self, media_id: str, fingerprint: str, rendition_name: str) -> float:
        if _readiness_snapshot.get() is not None:
            return self.playlist_readiness(media_id, fingerprint, rendition_name).duration
        # Preparation progress only needs durations, without validating every asset.
        playlist = self.cache_path(media_id, fingerprint) / rendition_name / PLAYLIST_NAME
        try:
            return sum(float(value) for value in re.findall(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?)", playlist.read_text(encoding="utf-8"), re.MULTILINE))
        except (OSError, ValueError):
            return 0.0

    def fully_prepared(self, media_id: str, fingerprint: str, media_obj: Any) -> bool:
        names = [
            *(rendition.name for rendition in self.video_renditions(media_obj)),
            *(rendition.name for rendition in self.audio_renditions(media_obj)),
        ]
        return bool(names) and all(self.rendition_complete(media_id, fingerprint, name) for name in names)

    def switching_ready(self, media_id: str, fingerprint: str, media_obj: Any) -> bool:
        names = [
            *(rendition.name for rendition in self.video_renditions(media_obj)),
            *(rendition.name for rendition in self.audio_renditions(media_obj)),
        ]
        return bool(names) and all(self.playlist_ready(media_id, fingerprint, name) for name in names)

    def full_preparation_error(self, media_id: str, fingerprint: str, media_obj: Any) -> Optional[dict[str, str]]:
        names = [
            *(rendition.name for rendition in self.video_renditions(media_obj)),
            *(rendition.name for rendition in self.audio_renditions(media_obj)),
        ]
        for name in names:
            failure = self.rendition_error(media_id, fingerprint, name)
            if failure:
                return failure
        return self.preparation_error(media_id, fingerprint)

    async def wait_until_fully_prepared(
        self,
        media_id: str,
        fingerprint: str,
        media_obj: Any,
    ) -> bool:
        """Wait for every scheduled rendition without polling the filesystem aggressively."""

        media_obj = self.snapshot_media(media_obj)
        prefix = f"{media_id}:{fingerprint}:"
        while not self.fully_prepared(media_id, fingerprint, media_obj):
            expected_names = [
                *(rendition.name for rendition in self.video_renditions(media_obj)),
                *(rendition.name for rendition in self.audio_renditions(media_obj)),
            ]
            if any(self.rendition_error(media_id, fingerprint, name) for name in expected_names):
                return False
            relevant_tasks = [
                task
                for key, task in self.active_jobs.items()
                if key.startswith(prefix) and not task.done()
            ]
            if not relevant_tasks:
                return False
            await asyncio.wait(relevant_tasks, timeout=1, return_when=asyncio.FIRST_COMPLETED)
        return True

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
        required = [baseline.name, *([default_audio.name] if default_audio else [])]
        return self.preparation_error(media_id, fingerprint) if not all(
            self.playlist_ready(media_id, fingerprint, name) for name in required
        ) else None

    def preparation_progress(self, media_id: str, fingerprint: str, media_obj: Any) -> dict[str, Any]:
        baseline = self.baseline_video(media_obj)
        audios = self.audio_renditions(media_obj)
        default_audio = next((item for item in audios if item.default), audios[0] if audios else None)
        required_names = [baseline.name, *([default_audio.name] if default_audio else [])]
        required_keys = [f"{media_id}:{fingerprint}:{name}" for name in required_names]
        baseline_readiness = self.playlist_readiness(media_id, fingerprint, baseline.name)
        ready_segments = baseline_readiness.segment_count if baseline_readiness.ready else len(list(
            (self.cache_path(media_id, fingerprint) / baseline.name).glob("*.m4s")
        ))
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

        active_jobs = dict(self.active_jobs)
        running_jobs = set(self.running_jobs)
        priorities = dict(self.job_priorities)
        scheduled_at = dict(self.job_scheduled_at)
        waiting = [
            key
            for key in sorted(
                (candidate for candidate in active_jobs if candidate not in running_jobs),
                key=lambda candidate: (
                    priorities.get(candidate, BACKGROUND_PRIORITY),
                    scheduled_at.get(candidate, 0),
                ),
            )
        ]
        positions = [waiting.index(key) + 1 for key in required_keys if key in waiting]
        return {
            "stage": stage,
            "queue_position": min(positions) if positions else 0,
            "ready_segments": ready_segments,
            "active_workers": sum(key in running_jobs for key in required_keys),
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
        if self._update_quiesced:
            return self.preparation_state(media_id, fingerprint, media_obj)
        try:
            async with runtime_work(
                "playback.preparation-request",
                [EffectClass.DISPOSABLE_CACHE_WRITE],
            ):
                return await self._prepare_admitted(
                    media_id,
                    media_obj,
                    source,
                    fingerprint=fingerprint,
                    include_remaining=include_remaining,
                    retry_errors=retry_errors,
                    foreground=foreground,
                )
        except AdmissionClosedError:
            # Cache preparation is disposable and may be requested again later.
            return self.preparation_state(media_id, fingerprint, media_obj)

    async def _prepare_admitted(
        self,
        media_id: str,
        media_obj: Any,
        source: ResolvedMediaSource,
        *,
        fingerprint: str,
        include_remaining: bool,
        retry_errors: bool,
        foreground: bool,
    ) -> str:
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
        if self._update_quiesced:
            return
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
        try:
            async with runtime_work(
                "playback.remaining-renditions",
                [EffectClass.DISPOSABLE_CACHE_WRITE],
            ):
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
        except AdmissionClosedError:
            # Remaining renditions are disposable and can be scheduled again.
            return

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
        if self._update_quiesced:
            if hasattr(coroutine, "close"):
                coroutine.close()
            return
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

    async def _input_process(
        self,
        source: ResolvedMediaSource,
        media_id: str,
        fingerprint: str,
    ) -> tuple[str, Optional[asyncio.subprocess.Process]]:
        if source.local_exists:
            return str(source.local_path), None
        if not source.cloud_exists or not source.cloud_path:
            raise PlaybackPreparationError("MEDIA_SOURCE_MISSING", "The media source is no longer available.")
        if not rclone_service.executable():
            raise PlaybackPreparationError("RCLONE_UNAVAILABLE", "Google Drive playback is unavailable because rclone is missing.")
        if not rclone_service.cloud_write_available() and rclone_service.cloud_health().get("state") == "unauthorized":
            raise PlaybackPreparationError("CLOUD_UNAUTHORIZED", "Google Drive authorization must be renewed.")

        extension = Path(source.cloud_path).suffix or ".media"
        source_key = hashlib.sha256(source.cloud_path.encode("utf-8")).hexdigest()[:24]
        spool_path = self.cache_path(media_id, fingerprint) / ".sources" / source_key / f"original{extension}"
        lock_key = str(spool_path)
        lock = self._cloud_spool_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            expected_size = int(source.cloud_size or 0)
            if expected_size > 0 and spool_path.is_file() and spool_path.stat().st_size == expected_size:
                return str(spool_path), None
            spool_path.parent.mkdir(parents=True, exist_ok=True)
            if expected_size <= 0:
                raise PlaybackPreparationError("CLOUD_SIZE_INVALID", "A verified source size is required before staging adaptive playback.")
            # Use a private download generation so archived copies and ambiguous
            # legacy partials never count as this job's materialized bytes.
            retain_existing_tree(spool_path)
            candidate_dir = spool_path.parent / f".spool-{uuid.uuid4().hex}"
            candidate_dir.mkdir(exist_ok=False)
            candidate_spool = candidate_dir / spool_path.name
            try:
                async with storage_capacity.allocation(f"playback-spool:{media_id}", expected_size, candidate_dir) as reservation:
                    copying = asyncio.create_task(rclone_service.copyto_atomic(source.cloud_path, str(candidate_spool), timeout=6 * 60 * 60))
                    written = 0
                    try:
                        while not copying.done():
                            await asyncio.wait({copying}, timeout=0.25)
                            size = await run_cancellation_safe_thread(self._spool_size, candidate_spool)
                            if size > expected_size:
                                raise PlaybackPreparationError("CLOUD_SPOOL_OVERSIZED", "The cloud source exceeded its admitted size.")
                            written = max(written, size)
                            await storage_capacity.progress(reservation, written)
                        result = await copying
                    finally:
                        if not copying.done():
                            copying.cancel()
                        await settle(asyncio.gather(copying, return_exceptions=True))
                        # This generation and all ambiguous partials stay retained.
                    if not result.ok:
                        raise PlaybackPreparationError(
                            result.error_code or "CLOUD_SPOOL_FAILED",
                            "Google Drive could not stage a seekable playback source.",
                        )
                    if not candidate_spool.is_file() or candidate_spool.stat().st_size != expected_size:
                        raise PlaybackPreparationError(
                            "CLOUD_SPOOL_TRUNCATED",
                            "Google Drive staged an incomplete playback source.",
                        )
                    if spool_path.exists():
                        raise PlaybackPreparationError("CLOUD_SPOOL_CONFLICT", "A retained source already occupies the playback spool path.")
                    candidate_spool.rename(spool_path)
            except StorageCapacityError as exc:
                raise PlaybackPreparationError(exc.code, "Local storage cannot admit the complete source spool for adaptive playback.") from exc
        return str(spool_path), None

    @staticmethod
    def _spool_size(spool_path: Path) -> int:
        seen: set[tuple[int, int]] = set()
        total = 0
        for path in [spool_path, *spool_path.parent.glob(f".{spool_path.name}.*")]:
            try:
                info = path.stat()
                identity = (info.st_dev, info.st_ino)
                if stat.S_ISREG(info.st_mode) and identity not in seen:
                    seen.add(identity)
                    total += info.st_size
            except FileNotFoundError:
                continue
        return total

    @asynccontextmanager
    async def _owned_job_input(
        self,
        media_id: str,
        fingerprint: str,
        rendition_name: str,
        requested_source: ResolvedMediaSource,
    ) -> AsyncIterator[tuple[str, Optional[asyncio.subprocess.Process], tuple[int, ...]]]:
        """Bind queued work to current catalog authority and immutable input bytes."""
        from db import engine
        from models import Episode, Movie
        from sqlmodel.ext.asyncio.session import AsyncSession

        descriptor: Optional[int] = None
        ownership = media_ownership(media_id)
        held = False
        try:
            await ownership.__aenter__()
            held = True
            try:
                require_resolved_media(media_id)
            except MediaRecoveryRequired as exc:
                raise PlaybackPreparationError("MEDIA_RECOVERY_REQUIRED", "Media publication must finish before preparing playback.") from exc
            # A capacity wait may span an entire replacement. Do not keep a DB
            # snapshot while waiting for ownership or staging a cloud source.
            async with AsyncSession(engine) as db:
                item = await db.get(Movie, media_id)
                if item is None:
                    item = await db.get(Episode, media_id)
                if item is None or str(item.source_fingerprint or "") != fingerprint:
                    raise PlaybackPreparationError("PLAYBACK_SOURCE_CHANGED", "The queued playback source changed.")
                catalog_path = item.video_url
                current_media = self.snapshot_media(item)
            # Cloud identity caches cannot establish which bytes a queued job
            # will download after an earlier publication has completed.
            if cloud_read_enabled():
                clear_cloud_object_cache()
            source = await resolve_media_source(catalog_path)
            if (not source.available or source.catalog_path != requested_source.catalog_path
                    or playback_source_fingerprint(source, current_media.audio_metadata) != fingerprint):
                raise PlaybackPreparationError("PLAYBACK_SOURCE_CHANGED", "The queued playback source changed.")
            audio = next((track for track in self.audio_renditions(current_media)
                          if track.name == rendition_name), None)
            if audio and audio.source == "external":
                source = await self._external_audio_source(source, audio, self._external_audio_path(source, audio))
                if source is None:
                    raise PlaybackPreparationError("MEDIA_SOURCE_MISSING", "The selected audio source is no longer available.")
            if source.local_exists and sys.platform.startswith("linux"):
                path = source.local_path
                if path.is_symlink() or not path.resolve().is_relative_to(Path(settings.MEDIA_DIR).resolve()):
                    raise PlaybackPreparationError("MEDIA_SOURCE_INVALID", "The playback source is outside the media root.")
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise PlaybackPreparationError("MEDIA_SOURCE_INVALID", "The playback source is not a regular file.")
                # The child inherits this exact inode even if publication later
                # renames its old directory or replaces a dubbing sidecar.
                actual_input, process = f"/proc/self/fd/{descriptor}", None
                pass_fds = (descriptor,)
            else:
                actual_input, process = await self._input_process(source, media_id, fingerprint)
                pass_fds = ()
            if not source.local_exists or descriptor is not None:
                await ownership.__aexit__(None, None, None)
                held = False
            # Platforms without inherited Linux descriptors retain ownership
            # until FFmpeg exits; cloud jobs now read their private spool file.
            yield actual_input, process, pass_fds
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if held:
                await ownership.__aexit__(None, None, None)

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
            "-avoid_negative_ts", "disabled",
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
        *,
        reservation: Optional[str] = None,
        maximum_bytes: Optional[int] = None,
    ) -> None:
        stall_seconds = max(1, int(settings.PLAYBACK_JOB_STALL_SECONDS))
        last_signature = await run_cancellation_safe_thread(self._output_signature, target_dir)
        written_peak = last_signature[1]
        last_activity = time.monotonic()
        wait_task = asyncio.create_task(process.wait())
        try:
            while not wait_task.done():
                await asyncio.wait({wait_task}, timeout=1)
                signature = await run_cancellation_safe_thread(self._output_signature, target_dir)
                if maximum_bytes is not None and signature[1] > maximum_bytes:
                    process.kill()
                    await wait_task
                    raise PlaybackPreparationError("STORAGE_RESERVATION_EXCEEDED", "Adaptive output exceeded its admitted storage capacity.")
                if reservation:
                    written_peak = max(written_peak, signature[1])
                    await storage_capacity.progress(reservation, written_peak)
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

    def rendition_capacity_bytes(self, media_obj: Any, rendition_name: str) -> int:
        """Admit a bounded ceiling, then stop the actual writer on overrun."""
        import math

        duration = float(getattr(media_obj, "probed_duration", 0.0) or 0.0)
        if not math.isfinite(duration) or duration <= 0:
            raise PlaybackPreparationError("MEDIA_DURATION_UNVERIFIED", "Adaptive preparation requires a verified duration.")
        video = next((item for item in self.video_renditions(media_obj) if item.name == rendition_name), None)
        rate = video.bandwidth if video else 256_000
        return max(2 * 1024 * 1024, int(duration * rate / 8 * 1.5) + 2 * 1024 * 1024)

    async def _run_ffmpeg_job(
        self,
        media_id: str,
        fingerprint: str,
        rendition_name: str,
        source: ResolvedMediaSource,
        arguments: list[str],
        media_obj: Any,
    ) -> None:
        try:
            async with runtime_work(
                "playback.preparation",
                [EffectClass.DISPOSABLE_CACHE_WRITE],
            ):
                await self._run_admitted_ffmpeg_job(
                    media_id,
                    fingerprint,
                    rendition_name,
                    source,
                    arguments,
                    media_obj,
                )
        except AdmissionClosedError:
            # Cache preparation is disposable and may be scheduled again later.
            return

    async def _decoded_audio_offset(self, actual_input: str, rendition: AudioRendition, pass_fds: tuple[int, ...]) -> float:
        """Measure decoded content origins, after codec priming/edit-list trimming.

        A negative AAC packet is encoder preroll, not necessarily audible content.
        This bounded probe reads the same immutable input inherited by the encoder.
        Standalone inputs retain their independent zero-relative-offset contract.
        """
        if rendition.source == "external":
            return 0.0
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise PlaybackPreparationError("FFPROBE_UNAVAILABLE", "FFprobe is required to align adaptive audio.")
        process = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", *local_media_input_options(),
            "-read_intervals", "%+#256", "-show_frames",
            "-show_entries", "frame=media_type,stream_index,best_effort_timestamp_time",
            "-of", "json", actual_input,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=native_media_environment(),
            **({"pass_fds": pass_fds} if pass_fds else {}),
        )
        key = f"playback-decoded-timing:{id(process)}"
        register_process(key, process)
        try:
            stdout, _ = await supervised_output(process, timeout=10, stdout_limit=4 * 1024 * 1024)
        finally:
            unregister_process(key)
        try:
            if process.returncode != 0:
                raise ValueError("probe failed")
            frames = json.loads(stdout).get("frames", [])
            starts: dict[tuple[str, int], float] = {}
            for frame in frames:
                timestamp = float(frame["best_effort_timestamp_time"])
                if not (-1e12 < timestamp < 1e12):
                    continue
                identity = (str(frame["media_type"]), int(frame["stream_index"]))
                starts[identity] = min(starts.get(identity, timestamp), timestamp)
            video = next(value for (kind, _), value in sorted(starts.items()) if kind == "video")
            audios = sorted((index, value) for (kind, index), value in starts.items() if kind == "audio")
            audio = (next(value for index, value in audios if index == rendition.stream_index)
                     if rendition.absolute_stream_index else audios[rendition.stream_index][1])
            return audio - video
        except (ValueError, TypeError, KeyError, IndexError, StopIteration) as exc:
            raise PlaybackPreparationError("AUDIO_TIMELINE_UNVERIFIED", "Decoded audio and video origins could not be verified.") from exc

    async def _run_admitted_ffmpeg_job(
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
            self.job_started_at[job_key] = time.monotonic()
            cloud_process: Optional[asyncio.subprocess.Process] = None
            ffmpeg_process: Optional[asyncio.subprocess.Process] = None
            pump_task: Optional[asyncio.Task[None]] = None
            publish_task: Optional[asyncio.Task[None]] = None
            cloud_stderr_task: Optional[asyncio.Task[bytes]] = None
            ffmpeg_stderr_task: Optional[asyncio.Task[bytes]] = None
            accepted = False
            candidate_started = False
            try:
                self._start_rendition_candidate(media_id, fingerprint, rendition_name)
                candidate_started = True
                self.running_jobs.add(job_key)
                maximum_bytes = self.rendition_capacity_bytes(media_obj, rendition_name)
                reservation = await capacity.enter_async_context(storage_capacity.allocation(
                    f"playback-output:{media_id}:{rendition_name}"[:128], maximum_bytes, target_dir
                ))
                actual_input, cloud_process, input_descriptors = await capacity.enter_async_context(
                    self._owned_job_input(media_id, fingerprint, rendition_name, source)
                )
                audio = next((item for item in self.audio_renditions(media_obj) if item.name == rendition_name), None)
                if audio is not None and "-af" in arguments:
                    offset = await self._decoded_audio_offset(actual_input, audio, input_descriptors)
                    arguments = list(arguments)
                    arguments[arguments.index("-af") + 1] = ",".join(canonical_audio_filter_chain(
                        offset, max(0.0, float(getattr(media_obj, "probed_duration", 0.0) or 0.0))))
                command = [self._ffmpeg_executable(), "-hide_banner", "-nostdin", "-y", *local_media_input_options(), "-i", actual_input, *arguments, *self._hls_output_args(target_dir)]
                logger.info(f"[Playback Prep] Preparing {media_id} rendition {rendition_name}.")
                ffmpeg_process = await asyncio.create_subprocess_exec(
                    *command,
                    env=native_media_environment(),
                    cwd=str(target_dir),
                    stdin=asyncio.subprocess.PIPE if cloud_process else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    **({"pass_fds": input_descriptors} if input_descriptors else {}),
                )
                register_process(f"playback:{job_key}", ffmpeg_process)
                publish_task = asyncio.create_task(
                    self._publish_when_streamable(media_id, fingerprint, rendition_name, media_obj, ffmpeg_process)
                )
                assert ffmpeg_process.stderr is not None
                ffmpeg_stderr_task = asyncio.create_task(self._read_bounded_stream(ffmpeg_process.stderr))
                if cloud_process:
                    pump_task = asyncio.create_task(self._pump_cloud_input(cloud_process, ffmpeg_process))
                    assert cloud_process.stderr is not None
                    cloud_stderr_task = asyncio.create_task(self._read_bounded_stream(cloud_process.stderr))
                await self._wait_for_ffmpeg_progress(ffmpeg_process, target_dir, reservation=reservation, maximum_bytes=maximum_bytes)
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
                await run_cancellation_safe_thread(self._verify_rendition_output, target_dir, rendition_name)
                await run_cancellation_safe_thread(self._accept_rendition, target_dir, rendition_name)
                accepted = True
                self._clear_preparation_error(media_id, fingerprint)
                self._clear_rendition_error(media_id, fingerprint, rendition_name)
                self.touch(media_id, fingerprint)
                await self.rebuild_master(media_id, fingerprint, media_obj)
                await self.enforce_lru_limits_admitted()
                elapsed = max(0.0, time.monotonic() - self.job_started_at.get(job_key, time.monotonic()))
                logger.info(
                    f"[Playback Prep] Prepared {media_id}/{rendition_name} in {elapsed:.2f}s; "
                    f"seekable={self.rendition_seekable_until(media_id, fingerprint, rendition_name):.2f}s."
                )
            except asyncio.CancelledError:
                if candidate_started and not accepted:
                    self._invalidate_retained_rendition(target_dir)
                await self.rebuild_master(media_id, fingerprint, media_obj)
                raise
            except (PlaybackPreparationError, StorageCapacityError) as exc:
                self._write_preparation_error(media_id, fingerprint, exc.code, str(exc))
                self._write_rendition_error(media_id, fingerprint, rendition_name, exc.code, str(exc))
                if candidate_started and not accepted:
                    self._invalidate_retained_rendition(target_dir)
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
                if candidate_started and not accepted:
                    self._invalidate_retained_rendition(target_dir)
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
                unregister_process(f"playback:{job_key}")
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
            "-vf", f"setpts=PTS-STARTPTS,scale={rendition.width}:{rendition.height}:force_original_aspect_ratio=decrease,pad={rendition.width}:{rendition.height}:(ow-iw)/2:(oh-ih)/2",
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
        del media_obj, rendition
        # Stream copy cannot insert keyframes at the configured four-second HLS
        # boundaries, so it must not advertise independent segments.
        return False

    def _external_audio_path(self, source: ResolvedMediaSource, audio: AudioRendition) -> Optional[Path]:
        audio_dir = source.local_path.parent / "audio"
        if not audio_dir.is_dir():
            return None
        supported = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
        files = sorted(item for item in audio_dir.iterdir() if item.is_file() and not item.is_symlink() and item.suffix.lower() in supported)
        if audio.file_name:
            if Path(audio.file_name).name != audio.file_name or "\\" in audio.file_name:
                return None
            exact = audio_dir / audio.file_name
            return exact if exact in files else None
        language_match = next((item for item in files if normalize_language_tag(item.stem) == audio.language), None)
        if language_match:
            return language_match
        return files[audio.stream_index] if audio.source == "external" and 0 <= audio.stream_index < len(files) else None

    @staticmethod
    async def _external_audio_source(source: ResolvedMediaSource, audio: AudioRendition, local_path: Optional[Path]) -> Optional[ResolvedMediaSource]:
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
        observed = await resolve_related_source(source, f"audio/{audio.file_name}")
        return observed if observed.available else None

    async def _transcode_audio(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: AudioRendition,
        media_obj: Any,
    ) -> None:
        def normalized_audio_arguments(stream_map: str, timeline_offset: float = 0.0) -> list[str]:
            media_duration = max(0.0, float(getattr(media_obj, "probed_duration", 0.0) or 0.0))
            filters = canonical_audio_filter_chain(timeline_offset, media_duration)
            return [
                "-map", stream_map,
                "-vn",
                "-af", ",".join(filters),
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "192k",
                "-ar", "48000",
                "-ac", "2",
            ]

        # Keep the accepted video as queue authority. _owned_job_input resolves
        # this track independently only after checking that authority again.
        if rendition.source == "external":
            arguments = normalized_audio_arguments("0:a:0", rendition.timeline_offset)
            await self._run_ffmpeg_job(media_id, fingerprint, rendition.name, source, arguments, media_obj)
            return
        stream_map = (
            f"0:{rendition.stream_index}"
            if rendition.absolute_stream_index
            else f"0:a:{rendition.stream_index}"
        )
        arguments = normalized_audio_arguments(stream_map, rendition.timeline_offset)
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

    def _probe_playlist_timeline(self, playlist: Path, expected_type: str) -> tuple[float, float]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise PlaybackPreparationError("FFPROBE_UNAVAILABLE", "FFprobe is required to verify adaptive playback output.")
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                *generated_hls_input_options(),
                "-show_entries", "stream=codec_type,start_time,duration:format=duration",
                "-of", "json", str(playlist),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=native_media_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            raise PlaybackPreparationError(
                "RENDITION_VERIFICATION_FAILED",
                "FFprobe could not compare the prepared adaptive timelines.",
            )
        try:
            payload = json.loads(result.stdout)
            stream = next(item for item in payload.get("streams", []) if str(item.get("codec_type")) == expected_type)
            start = float(stream.get("start_time") or 0.0)
            duration = float(stream.get("duration") or (payload.get("format") or {}).get("duration") or 0.0)
        except (json.JSONDecodeError, AttributeError, StopIteration, TypeError, ValueError) as exc:
            raise PlaybackPreparationError(
                "RENDITION_VERIFICATION_FAILED",
                "FFprobe returned incomplete adaptive timeline metadata.",
            ) from exc
        return start, start + max(0.0, duration)

    def _verify_master_timeline(self, cache_path: Path, videos: list[VideoRendition], audios: list[AudioRendition]) -> None:
        if not videos or not audios:
            return
        video_timelines = [self._probe_playlist_timeline(cache_path / item.name / PLAYLIST_NAME, "video") for item in videos]
        audio_timelines = [self._probe_playlist_timeline(cache_path / item.name / PLAYLIST_NAME, "audio") for item in audios]
        for video_start, video_end in video_timelines:
            for audio_start, audio_end in audio_timelines:
                if abs(audio_start - video_start) > MAX_HLS_START_DELTA_SECONDS:
                    raise PlaybackPreparationError(
                        "RENDITION_TIMELINE_MISMATCH",
                        "Prepared adaptive audio and video do not share the canonical playback epoch.",
                    )
                if video_end > video_start and audio_end > audio_start and abs(audio_end - video_end) > MAX_HLS_END_DELTA_SECONDS:
                    raise PlaybackPreparationError(
                        "RENDITION_TIMELINE_MISMATCH",
                        "Prepared adaptive audio and video do not share the canonical playback duration.",
                    )

    async def rebuild_master(self, media_id: str, fingerprint: str, media_obj: Any) -> Optional[Path]:
        cache_path = self.cache_path(media_id, fingerprint)
        lock_key = f"{media_id}:{fingerprint}"
        lock = self._master_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            videos = [item for item in self.video_renditions(media_obj) if self.rendition_verified(media_id, fingerprint, item.name)]
            audios = [item for item in self.audio_renditions(media_obj) if self.rendition_verified(media_id, fingerprint, item.name)]
            if not videos:
                retain_existing_tree(cache_path / MASTER_NAME)
                return None
            await run_cancellation_safe_thread(self._verify_master_timeline, cache_path, videos, audios)

            lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
            measured_audio = [
                measurement
                for audio in audios
                if (measurement := await run_cancellation_safe_thread(
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
                measured = await run_cancellation_safe_thread(
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
        self._active_cache_leases[key] = now + 120
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

        self._update_quiesced = True
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
        """Quarantine incomplete legacy artifacts without deleting any bytes."""
        if not self.cache_dir.exists():
            return
        for playlist in self.cache_dir.glob("*/*/*/playlist.m3u8"):
            rendition_dir = playlist.parent
            if any(part.startswith(".") for part in playlist.relative_to(self.cache_dir).parts):
                continue
            marker = rendition_dir / COMPLETE_MARKER
            verified = rendition_dir / self._verified_marker_name(rendition_dir.name)
            if marker.is_file() and verified.is_file():
                continue
            self._invalidate_retained_rendition(rendition_dir)

    async def reconcile_catalog_cache_identities(self) -> None:
        from db import engine
        from models import Episode, Movie
        from sqlmodel import select
        from sqlmodel.ext.asyncio.session import AsyncSession

        self.recover_interrupted_outputs()
        async with AsyncSession(engine) as db:
            movies = (await db.exec(select(Movie.id).where(Movie.video_url != ""))).all()
            episodes = (await db.exec(select(Episode.id).where(Episode.video_url != ""))).all()
        targets = [(Movie, media_id) for media_id in movies] + [(Episode, media_id) for media_id in episodes]
        for model, media_id in targets:
            try:
                async with media_ownership(media_id):
                    require_resolved_media(media_id)
                    async with AsyncSession(engine) as db:
                        media_obj = await db.get(model, media_id)
                        if media_obj is None or not media_obj.video_url:
                            continue
                        catalog_path = media_obj.video_url
                        previous_fingerprint = str(media_obj.source_fingerprint or "")
                        audio_metadata = list(media_obj.audio_metadata or [])
                    source = await resolve_media_source(catalog_path)
                    if not source.available:
                        continue
                    fingerprint = playback_source_fingerprint(source, audio_metadata)
                    if previous_fingerprint == fingerprint:
                        continue
                    async with AsyncSession(engine) as db:
                        media_obj = await db.get(model, media_id)
                        if (media_obj is None or media_obj.video_url != catalog_path
                                or str(media_obj.source_fingerprint or "") != previous_fingerprint):
                            continue
                        if previous_fingerprint:
                            self.cancel_media(media_id, previous_fingerprint)
                        media_obj.source_fingerprint = fingerprint
                        db.add(media_obj)
                        await db.commit()
            except Exception as exc:
                logger.warning(f"[Playback Prep] Cache identity reconciliation skipped for {media_id}: {self.sanitize_diagnostics(str(exc), 400)}")

    async def schedule_catalog_baselines(self) -> None:
        """Prepare the complete rendition set for existing catalog media in bounded order."""

        from db import engine
        from models import Episode, Movie, PlaybackSession
        from sqlmodel import select
        from sqlmodel.ext.asyncio.session import AsyncSession

        if self._update_quiesced:
            return
        await self.reconcile_catalog_cache_identities()
        from services.storage_inventory import hybrid_policy_active
        if hybrid_policy_active():
            # Hybrid storage rebuilds only the requested accepted generation;
            # startup must not spool or regenerate the whole remote library.
            return
        async with AsyncSession(engine, expire_on_commit=False) as db:
            sessions = (await db.exec(select(PlaybackSession).order_by(PlaybackSession.updated_at.desc()))).all()
            recent_ids = [str(item.episode_id or item.movie_id) for item in sessions]
            movies = (await db.exec(select(Movie).where(Movie.video_url != ""))).all()
            episodes = (await db.exec(select(Episode).where(Episode.video_url != ""))).all()
            media_items = [*movies, *episodes]

        recent_order = {media_id: index for index, media_id in enumerate(dict.fromkeys(recent_ids))}
        media_items.sort(key=lambda item: (recent_order.get(str(item.id), len(recent_order)), str(item.id)))
        for media_obj in media_items:
            if self._update_quiesced:
                return
            try:
                source = await resolve_media_source(media_obj.video_url)
                if not source.available:
                    continue
                fingerprint = playback_source_fingerprint(source, media_obj.audio_metadata or [])
                media_obj.source_fingerprint = fingerprint
                if self.fully_prepared(media_obj.id, fingerprint, media_obj):
                    await self.rebuild_master(media_obj.id, fingerprint, media_obj)
                    continue
                await self.prepare(
                    media_obj.id,
                    media_obj,
                    source,
                    include_remaining=True,
                    retry_errors=True,
                    foreground=False,
                )
                prepared = await self.wait_until_fully_prepared(media_obj.id, fingerprint, media_obj)
                if self._update_quiesced:
                    return
                if not prepared:
                    logger.warning(f"[Playback Prep] Catalog warming did not complete for {media_obj.id}.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[Playback Prep] Catalog warming skipped {getattr(media_obj, 'id', 'unknown')}: "
                    f"{self.sanitize_diagnostics(str(exc), 400)}"
                )

    async def enforce_lru_limits_admitted(self) -> None:
        """Compatibility hook: retained cache is never automatically evicted."""
        return

    def enforce_lru_limits(self, eligible_roots: Optional[set[str]] = None) -> None:
        """Legacy eligibility never grants permission to delete retained cache."""
        return


playback_prep_service = PlaybackPrepService()
