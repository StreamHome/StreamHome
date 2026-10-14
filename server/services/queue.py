import os
import time
import json
import shutil
import asyncio
import traceback
import httpx
import aiofiles
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlsplit
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from db import engine
from models import DownloadTask, Movie, Episode
from services.ffmpeg import download_and_merge, download_and_cache_metadata_image
from services.tmdb import tmdb_client
from config import settings
from services.logger import logger
from services.media_analysis import duration_label, refreshed_analysis, current_duration_analysis, stored_analysis, positive_number
from services.media_probe import probe_media_stream, notify_video_sender, probe_completed_media
from services.ingestion_errors import IngestionFailure, IngestionTaskError, prune_task_diagnostics, write_task_diagnostics
from services.ingest_preview import ingest_preview_service
from services.rclone import rclone_service
from services.ingestion_security import UnsafeIngestionSource, validate_headers, validate_url, validated_stream_request
from services.media_source import (
    MediaSourceError,
    ResolvedMediaSource,
    catalog_path_from_storage,
    local_catalog_source_exists,
    local_path_for,
    local_playback_fingerprint,
    local_video_fingerprint,
    playback_source_fingerprint,
    resolve_media_source,
)
from services.vibe_analysis import VIBE_ANALYSIS_VERSION, compute_trope_vectors, vibe_analysis_manager
from services.audio_extractor import (apply_primary_audio_language, extract_audio_and_strip_video, normalize_language_code,
    audio_languages, primary_embedded_audio_index, retained_primary_audio_language)
from lifecycle.media_work import MediaRecoveryRequired, media_ownership, settle
from lifecycle.media_publication import MediaMutation
from lifecycle.coordination import (
    AdmissionClosedError,
    EffectClass,
    run_cancellation_safe_thread,
    runtime_work,
)


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    """Durably replace a JSON document without exposing a partial file to recovery."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _portable_owner(data: Any, path: Path) -> Optional[tuple]:
    """Identify a recovery owner using the same legacy episode fallbacks as sync."""
    if not isinstance(data, dict) or data.get("media_type") not in {"movie", "series", "tv"}:
        return None
    try:
        tmdb_id = int(data.get("tmdb_id"))
        if tmdb_id <= 0 or isinstance(data.get("tmdb_id"), bool):
            return None
        if data["media_type"] == "movie":
            return ("movie", tmdb_id)
        season, episode = data.get("season"), data.get("episode")
        if season is None or episode is None:
            match = re.fullmatch(r"metadata_s([1-9][0-9]*)_e([1-9][0-9]*)\.json", path.name)
            if match:
                season, episode = map(int, match.groups())
            elif path.parent.parent.name.startswith("Episode_") and path.parent.parent.parent.name.startswith("Season_"):
                season = int(path.parent.parent.parent.name.removeprefix("Season_"))
                episode = int(path.parent.parent.name.removeprefix("Episode_"))
        if season is None and episode is None:
            return ("show", tmdb_id)
        if isinstance(season, bool) or isinstance(episode, bool) or int(season) <= 0 or int(episode) <= 0:
            return None
        return ("episode", tmdb_id, int(season), int(episode))
    except (TypeError, ValueError):
        return None


def _portable_episode_directory(owner: tuple, path: Path) -> Optional[Path]:
    _, _, season, episode = owner
    directory = path.parent.parent
    if directory.name == f"Episode_{episode}" and directory.parent.name == f"Season_{season}":
        return directory
    if re.fullmatch(rf"metadata_s{season}_e{episode}\.json", path.name):
        return directory / f"Season_{season}" / f"Episode_{episode}"
    return None


def _portable_episode_source(data: Any, path: Path) -> Optional[tuple]:
    """Bind compatible episode locations to one owner and one physical source."""
    owner = _portable_owner(data, path)
    if owner is None or owner[0] != "episode":
        return None
    _, _, season, episode = owner
    for field, expected in (("tmdb_id", owner[1]), ("season", season), ("episode", episode)):
        value = data.get(field)
        if value is None and field != "tmdb_id":
            continue
        if type(value) is not int and (not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value)):
            return None
        if int(value) != expected:
            return None
    episode_directory = _portable_episode_directory(owner, path)
    if episode_directory is None:
        return None
    try:
        video_url = data.get("video_url")
        if video_url:
            source = local_path_for(video_url)
            if source.parent != episode_directory.resolve():
                return None
        else:
            videos = [item for item in episode_directory.iterdir() if item.is_file()
                and item.suffix.lower() in {".mp4", ".mkv", ".avi", ".mov"}]
            if len(videos) != 1:
                return None
            source = videos[0].resolve()
        return owner, str(source)
    except (OSError, ValueError, TypeError):
        return None


def _portable_media_source(data: Any, path: Path, accepted_sources: Optional[Dict[tuple, str]] = None) -> Optional[tuple]:
    owner = _portable_owner(data, path)
    if owner is None:
        return None
    if owner[0] != "movie":
        key = _portable_episode_source(data, path)
        directory = _portable_episode_directory(owner, path) if owner[0] == "episode" else None
    else:
        value = data.get("tmdb_id")
        if type(value) is not int and (not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value)):
            return None
        directory = path.parent.parent
        try:
            if data.get("video_url"):
                source = local_path_for(data["video_url"])
                if source.parent != directory.resolve():
                    return None
                key = owner, str(source)
            else:
                videos = [item for item in directory.iterdir() if item.is_file()
                    and item.suffix.lower() in {".mp4", ".mkv", ".avi", ".mov"}]
                key = (owner, str(videos[0].resolve())) if len(videos) == 1 else None
        except (OSError, ValueError, TypeError):
            return None
    # A declaration outside the supported location is never reinterpreted.
    # Only an absent or physically missing contained declaration may fall back
    # to the DB's independently proved current source in this same location.
    if data.get("video_url") and key is None:
        return None
    if directory is not None and (key is None or not Path(key[1]).is_file()):
        fallback = (accepted_sources or {}).get(owner)
        if fallback:
            source = Path(fallback)
            if source.is_file() and source.parent == directory.resolve():
                return owner, str(source)
    return key


def _recovery_metadata_files(root: str, files: List[str]) -> List[str]:
    """Canonical same-owner metadata wins regardless of filesystem entry order."""
    canonical_owner = None
    canonical_source = None
    canonical = Path(root) / "metadata.json"
    if "metadata.json" in files:
        try:
            canonical_data = json.loads(canonical.read_text(encoding="utf-8"))
            canonical_owner = _portable_owner(canonical_data, canonical)
            canonical_source = _portable_media_source(canonical_data, canonical)
        except (OSError, ValueError):
            pass
    selected = []
    for name in sorted(files, key=lambda name: (name != "metadata.json", name)):
        if not name.endswith(".json"):
            continue
        if name != "metadata.json" and canonical_owner is not None:
            try:
                path = Path(root) / name
                data = json.loads(path.read_text(encoding="utf-8"))
                if _portable_owner(data, path) == canonical_owner and (canonical_owner[0] == "show"
                    or canonical_source is not None and _portable_media_source(data, path) == canonical_source):
                    continue
            except (OSError, ValueError):
                pass
        selected.append(name)
    return selected


def _recovery_metadata_inventory(media_dir: str, accepted_sources: Optional[Dict[tuple, str]] = None,
    *, ambiguous_sources: Optional[Dict[tuple, set[str]]] = None) -> List[tuple[str, List[str]]]:
    """Select canonicals before any alias can author reconstructed DB state."""
    inventory = [(root, files) for root, _, files in os.walk(media_dir) if os.path.basename(root) == ".metadata"]
    accepted_sources = accepted_sources or {}
    canonicals = {}
    source_owners = {}
    for owner, source in accepted_sources.items():
        if source in source_owners and source_owners[source] != owner:
            raise MediaRecoveryRequired("Conflicting accepted recovery owners")
        source_owners[source] = owner
    for root, files in inventory:
        if "metadata.json" not in files:
            continue
        path = Path(root) / "metadata.json"
        try:
            key = _portable_media_source(json.loads(path.read_text(encoding="utf-8")), path, accepted_sources)
            if key is not None and (key[1] not in source_owners or source_owners[key[1]] == key[0]):
                canonicals[key] = str(path)
                source_owners[key[1]] = key[0]
        except (OSError, ValueError):
            pass
    selected = []
    owner_sources = {}
    for root, files in sorted(inventory):
        names = []
        for name in _recovery_metadata_files(root, files):
            path = Path(root) / name
            data = None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                owner = _portable_owner(data, path)
                key = _portable_media_source(data, path, accepted_sources)
            except (OSError, ValueError):
                key, owner = None, None
            if isinstance(data, dict) and data.get("media_type") in {"series", "tv"} and owner is None:
                continue
            if owner in accepted_sources and key is None:
                continue
            if owner is not None and owner[0] == "episode" and key is None:
                if data.get("video_url"):
                    # Contradictory explicit identity cannot be reinterpreted
                    # by the older directory-based video discovery below.
                    continue
                directory = _portable_episode_directory(owner, path)
                if directory and directory.is_dir() and sum(item.is_file() and item.suffix.lower()
                    in {".mp4", ".mkv", ".avi", ".mov"} for item in directory.iterdir()) > 1:
                    raise MediaRecoveryRequired("Ambiguous episode recovery sources")
            if owner is not None and owner[0] == "movie":
                if key is None and data.get("video_url"):
                    continue
                directory = path.parent.parent
                videos = [item for item in directory.iterdir() if item.is_file()
                    and item.suffix.lower() in {".mp4", ".mkv", ".avi", ".mov"}]
                if key is None and len(videos) > 1:
                    raise MediaRecoveryRequired("Ambiguous movie recovery sources")
                if key is not None and data.get("video_url") and not Path(key[1]).is_file() and videos:
                    raise MediaRecoveryRequired("Declared movie recovery source is missing")
            if key is not None and key[1] in source_owners and source_owners[key[1]] != key[0]:
                continue
            if key is not None and key[0] in accepted_sources and accepted_sources[key[0]] != key[1]:
                continue
            if key is not None and key in canonicals and canonicals[key] != str(path):
                continue
            if key is not None:
                owner_sources.setdefault(key[0], set()).add(key[1])
            names.append(name)
        selected.append((root, names))
    ambiguous = {owner: sources for owner, sources in owner_sources.items() if len(sources) > 1}
    if ambiguous_sources is not None:
        ambiguous_sources.update(ambiguous)
    elif ambiguous:
        raise MediaRecoveryRequired("Ambiguous episode recovery sources")
    return selected


_CLOUD_BUNDLE_FIELDS = ("skip_markers", "subtitles", "audio_metadata", "languages", "quality", "duration",
    "probed_duration", "container", "codec", "width", "height", "frame_rate", "source_fingerprint", "analysis")
_MEASURED_BUNDLE_FIELDS = ("quality", "duration", "probed_duration", "container", "codec", "width", "height", "frame_rate")


def _catalog_bundle(item: Movie | Episode) -> Dict[str, Any]:
    bundle = {field: getattr(item, field) for field in _CLOUD_BUNDLE_FIELDS if field != "analysis"}
    bundle.update(video_url=item.video_url, analysis=stored_analysis(item.analysis_metadata_str))
    if not isinstance(bundle["audio_metadata"], list) or not all(isinstance(track, dict) for track in bundle["audio_metadata"]):
        bundle["audio_metadata"] = []
    return bundle


def _cloud_bundle_differs(bundle: Dict[str, Any], portable: Dict[str, Any]) -> bool:
    return bundle["video_url"] != portable.get("video_url") or any(
        bundle[field] != (stored_analysis(portable.get(field)) if field == "analysis"
            else portable.get(field) or duration_label(portable.get("probed_duration")) if field == "duration"
            else portable.get(field))
        for field in _CLOUD_BUNDLE_FIELDS)


def _current_cloud_bundle(bundle: Dict[str, Any], observed: ResolvedMediaSource, *, portable: bool = False) -> bool:
    if not observed.video_fingerprint:
        return False
    try:
        if local_path_for(bundle.get("video_url")) != observed.local_path:
            return False
    except (TypeError, ValueError):
        return False
    audio = bundle.get("audio_metadata")
    if not isinstance(audio, list) or not all(isinstance(track, dict) for track in audio):
        audio = []
    analysis = stored_analysis(bundle.get("analysis"))
    return (analysis.get("video_fingerprint") == observed.video_fingerprint
        or portable and bundle.get("video_fingerprint") == observed.video_fingerprint
        or bool(bundle.get("source_fingerprint")) and bundle["source_fingerprint"] == playback_source_fingerprint(observed, audio))


def _select_cloud_bundle(db_bundle: Optional[Dict[str, Any]], portable: Dict[str, Any], observed: ResolvedMediaSource) -> Dict[str, Any]:
    db_current = bool(db_bundle and _current_cloud_bundle(db_bundle, observed))
    portable_current = _current_cloud_bundle(portable, observed, portable=True)
    selected = dict(db_bundle if db_current else portable if portable_current else {})
    audio = selected.get("audio_metadata")
    if not isinstance(audio, list) or not all(isinstance(track, dict) for track in audio):
        audio = []
    selected["audio_metadata"] = audio
    analysis = stored_analysis(selected.get("analysis"))
    if db_current and "sender_quality" not in refreshed_analysis(None, analysis) and portable_current:
        analysis = refreshed_analysis(analysis, portable.get("analysis"))
    else:
        analysis = refreshed_analysis(analysis, analysis)
    analysis["video_fingerprint"] = observed.video_fingerprint
    fingerprint = playback_source_fingerprint(observed, audio)
    analysis["source_fingerprint"] = fingerprint
    measurements = dict(selected)
    measured_analysis = analysis
    if not current_duration_analysis(measured_analysis) and db_current and portable_current:
        fallback = stored_analysis(portable.get("analysis"))
        fallback["video_fingerprint"] = observed.video_fingerprint
        if current_duration_analysis(fallback):
            measurements, measured_analysis = portable, refreshed_analysis(fallback, analysis)
    valid_measurement = (current_duration_analysis(measured_analysis)
        and positive_number(measured_analysis.get("probed_duration")) is not None
        and type(measured_analysis.get("width")) is int and measured_analysis["width"] > 0
        and type(measured_analysis.get("height")) is int and measured_analysis["height"] > 0)
    if valid_measurement:
        for field in _MEASURED_BUNDLE_FIELDS:
            selected[field] = measurements.get(field)
        selected.update(width=measured_analysis["width"], height=measured_analysis["height"],
            quality=measured_analysis.get("quality") or "Source", probed_duration=measured_analysis["probed_duration"],
            duration=duration_label(measured_analysis["probed_duration"]))
        analysis = refreshed_analysis(measured_analysis, analysis)
        analysis.update(video_fingerprint=observed.video_fingerprint, source_fingerprint=fingerprint)
    else:
        analysis = refreshed_analysis(None, analysis)
        analysis.update(video_fingerprint=observed.video_fingerprint, source_fingerprint=fingerprint)
        selected.update({field: None for field in _MEASURED_BUNDLE_FIELDS})
        selected.update(quality="Source", duration="Unknown")
    selected.update(video_url=observed.catalog_path, analysis=analysis, source_fingerprint=fingerprint)
    for field, default in (("skip_markers", {}), ("subtitles", []), ("languages", [])):
        selected[field] = selected.get(field) or default
    return {field: selected[field] for field in (*_CLOUD_BUNDLE_FIELDS, "video_url")}


def _apply_catalog_bundle(item: Movie | Episode, bundle: Dict[str, Any]) -> None:
    for field in _CLOUD_BUNDLE_FIELDS:
        if field == "analysis":
            item.analysis_metadata_str = json.dumps(bundle[field])
        elif field == "languages":
            # An explicitly selected unknown bundle stays unknown; the legacy
            # property setter otherwise changes [] into an English assertion.
            item.languages_str = json.dumps(bundle[field])
        else:
            setattr(item, field, bundle[field])
    item.video_url = bundle["video_url"]


def _retained_subtitles(directory: str, tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Retain accepted track identity only for present, contained subtitle assets."""
    retained = []
    root = Path(directory).resolve()
    for track in tracks:
        if not isinstance(track, dict):
            continue
        name = track.get("fileName")
        if not isinstance(name, str) or not re.fullmatch(r"subtitle_[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.vtt", name):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            continue
        retained.append({key: value for key, value in track.items()
            if key in {"trackId", "language", "ext", "fileName", "label"}})
    return retained


def _retained_external_audio(measured: List[Dict[str, Any]], *bundles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep accepted sidecar details only when its physical identity is unchanged."""
    def identity(track):
        return (track.get("fileName"), track.get("fileSize"), track.get("modifiedAt"))
    result = []
    for track in measured:
        retained = track
        if track.get("source") == "external" and all(identity(track)):
            for bundle in bundles:
                match = next((previous for previous in bundle if previous.get("source") == "external"
                    and identity(previous) == identity(track)), None)
                if match is not None:
                    retained = dict(match)
                    break
        result.append(retained)
    return result


def _write_recovery_metadata(path: str, data: Dict[str, Any]) -> None:
    """Refresh canonical recovery aliases without touching another catalog owner."""
    from services.media_updates import portable_metadata_names
    primary = Path(path)
    owner = _portable_owner(data, primary)
    destinations = [primary]
    if primary.name == "metadata.json":
        for name in portable_metadata_names(data):
            candidate = primary.with_name(name)
            if candidate == primary:
                continue
            if candidate.exists():
                try:
                    if _portable_owner(json.loads(candidate.read_text(encoding="utf-8")), candidate) != owner:
                        continue
                except (OSError, ValueError):
                    continue
            destinations.append(candidate)
    for destination in destinations:
        _atomic_write_json(str(destination), data)


def srt_to_vtt(srt_path: str, vtt_path: str) -> bool:
    """
    Converts a SubRip (.srt) subtitle file to a WebVTT (.vtt) file.
    Atomically ensures UTF-8 encoding.
    """
    try:
        content = ""
        for encoding in ["utf-8", "latin-1", "cp1252"]:
            try:
                with open(srt_path, "r", encoding=encoding) as f:
                    content = f.read()
                break
            except UnicodeDecodeError:
                continue
        if not content:
            with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                
        # Fix timestamps: commas to periods
        fixed_content = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", content)
        if fixed_content.startswith("\ufeff"):
            fixed_content = fixed_content[1:]
            
        if not fixed_content.strip().startswith("WEBVTT"):
            fixed_content = "WEBVTT\n\n" + fixed_content
            
        temporary_path = f"{vtt_path}.tmp"
        with open(temporary_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(fixed_content)
        os.replace(temporary_path, vtt_path)
        return True
    except Exception as e:
        try:
            os.remove(f"{vtt_path}.tmp")
        except OSError:
            pass
        logger.error(f"[Subtitles] Exception converting SRT to VTT: {e}")
        return False

async def verify_media_exists(rel_path: str) -> bool:
    """
    Checks if physical media exists either locally on disk or inside rclone cloud storage.
    """
    if not rel_path:
        return False
    candidate = rel_path.replace("\\", "/")
    if candidate.startswith("media/"):
        candidate = f"/{candidate}"
    try:
        return (await resolve_media_source(candidate)).available
    except MediaSourceError:
        return False


class DownloadQueueManager:
    MINIMUM_TASK_RESERVATION_BYTES = 5 * 1024**3
    STORAGE_SAFETY_MARGIN_BYTES = 2 * 1024**3
    ESTIMATED_MEDIA_BYTES_PER_SECOND = 2 * 1024**2

    def __init__(self):
        self.loop_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.active_tasks = set()
        self.worker_tasks: Dict[str, asyncio.Task] = {}
        self.cancelled_tasks: set[str] = set()
        self.active_media_keys: Dict[str, str] = {}
        self.storage_reservations: Dict[str, int] = {}

    def start(self):
        if not self.is_running:
            self.is_running = True
            prune_task_diagnostics()
            self.loop_task = asyncio.create_task(self._worker_loop())
            logger.info("[Queue Manager] Background worker loop started.")

    async def stop(self):
        self.is_running = False
        if self.loop_task:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
            self.loop_task = None
        workers = list(self.worker_tasks.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.worker_tasks.clear()
        self.active_tasks.clear()
        self.active_media_keys.clear()
        self.storage_reservations.clear()
        self.cancelled_tasks.clear()
        logger.info("[Queue Manager] Background worker loop stopped.")

    async def cancel_task(self, task_id: str) -> tuple[bool, bool]:
        """Stops both the ingestion coroutine and any registered child process."""
        from services.state import cancel_and_kill_process

        self.cancelled_tasks.add(task_id)
        process_killed = await cancel_and_kill_process(task_id)
        worker = self.worker_tasks.get(task_id)
        worker_cancelled = bool(worker and not worker.done())
        if worker_cancelled and worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.cancelled_tasks.discard(task_id)
        return worker_cancelled, process_killed

    def _worker_finished(self, task_id: str, worker: asyncio.Task) -> None:
        self.active_tasks.discard(task_id)
        self.active_media_keys.pop(task_id, None)
        self.storage_reservations.pop(task_id, None)
        self.worker_tasks.pop(task_id, None)
        if worker.cancelled():
            return
        exception = worker.exception()
        if exception:
            logger.error(f"[Queue Manager] Worker {task_id} exited unexpectedly: {type(exception).__name__}")

    async def _worker_loop(self):
        os.makedirs(settings.MEDIA_DIR, exist_ok=True)
        os.makedirs(settings.TEMP_DIR, exist_ok=True)
        
        while self.is_running:
            try:
                await asyncio.sleep(2.0)
                
                if len(self.active_tasks) >= 2:
                    continue
                
                async with AsyncSession(engine) as db:
                    stmt = select(DownloadTask).where(DownloadTask.status == "PENDING").order_by(DownloadTask.created_at)
                    result = await db.exec(stmt)
                    tasks = result.all()
                    active_keys = set(self.active_media_keys.values())
                    task = next(
                        (
                            candidate
                            for candidate in tasks
                            if self._media_identity_key(
                                candidate.tmdb_id,
                                candidate.media_type,
                                candidate.season,
                                candidate.episode,
                            ) not in active_keys
                        ),
                        None,
                    )
                    
                    if not task:
                        continue
                    
                    task_id = task.id
                
                self.active_tasks.add(task_id)
                self.active_media_keys[task_id] = self._media_identity_key(
                    task.tmdb_id,
                    task.media_type,
                    task.season,
                    task.episode,
                )
                worker = asyncio.create_task(self._process_task(task_id), name=f"ingestion-{task_id}")
                self.worker_tasks[task_id] = worker
                worker.add_done_callback(lambda future, tid=task_id: self._worker_finished(tid, future))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Queue Manager] Error in worker loop: {e}")

    @staticmethod
    def _media_identity_key(
        tmdb_id: int,
        media_type: str,
        season: Optional[int],
        episode: Optional[int],
    ) -> str:
        if media_type == "movie":
            return f"movie:{tmdb_id}"
        return f"episode:{tmdb_id}:{season or 1}:{episode or 1}"

    def _reserve_storage(self, task_id: str, requested_bytes: int) -> None:
        reservation = max(self.MINIMUM_TASK_RESERVATION_BYTES, int(requested_bytes))
        other_reserved = sum(
            amount for reserved_task, amount in self.storage_reservations.items() if reserved_task != task_id
        )
        roots = {Path(settings.TEMP_DIR).resolve(), Path(settings.MEDIA_DIR).resolve()}
        for root in roots:
            try:
                root.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(root).free
            except OSError as exc:
                raise IngestionTaskError(
                    IngestionFailure(
                        "STORAGE_UNAVAILABLE",
                        f"The configured storage root could not be checked ({type(exc).__name__}).",
                    )
                ) from exc
            required = other_reserved + reservation + self.STORAGE_SAFETY_MARGIN_BYTES
            logger.info(
                f"[Queue Manager] Storage capacity for '{root}': "
                f"{free / 1024**3:.2f} GB free, {required / 1024**3:.2f} GB reserved/required."
            )
            if free < required:
                raise IngestionTaskError(
                    IngestionFailure(
                        "INSUFFICIENT_STORAGE",
                        f"Only {free / 1024**3:.2f} GB is free in {root}; "
                        f"active and planned ingestion requires {required / 1024**3:.2f} GB.",
                    )
                )
        self.storage_reservations[task_id] = reservation

    @staticmethod
    def _assert_directory_within(path: str, root: str, label: str) -> Path:
        candidate = Path(path).resolve()
        expected_root = Path(root).resolve()
        try:
            candidate.relative_to(expected_root)
        except ValueError as exc:
            raise ValueError(f"{label} is outside its configured storage root") from exc
        return candidate

    async def _process_task(self, task_id: str):
        effects = {
            EffectClass.DB_WRITE,
            EffectClass.DURABLE_MEDIA_WRITE,
            EffectClass.DISPOSABLE_CACHE_WRITE,
        }
        if settings.STORAGE_ENGINE == "CLOUD":
            effects.add(EffectClass.CLOUD_WRITE)
        try:
            async with runtime_work("ingestion.worker", effects):
                async with AsyncSession(engine) as db:
                    task = await db.get(DownloadTask, task_id)
                    if not task or task.status != "PENDING":
                        return
                    task.status = "DOWNLOADING"
                    db.add(task)
                    await db.commit()
                await self._process_admitted_task(task_id)
        except AdmissionClosedError:
            # The durable PENDING row remains available for a future admission reopen.
            logger.info(f"[Queue Manager] Deferred task {task_id} because runtime admission is closed.")

    async def _process_admitted_task(self, task_id: str):
        logger.info(f"[Queue Manager] Processing task: {task_id}")
        output_abs_path: Optional[str] = None
        catalog_abs_path: Optional[str] = None
        staging_task_root = Path(settings.TEMP_DIR).resolve() / "ingestion" / task_id
        created_artifacts: List[str] = []
        submitted_video_url = ""
        uploaded_to_cloud = False
        task_completed = False
        mutation = None
        ownership = None
        try:
            async with AsyncSession(engine) as db:
                task = await db.get(DownloadTask, task_id)
                if not task:
                    self.active_tasks.discard(task_id)
                    from services.state import remove_task_metrics
                    remove_task_metrics(task_id)
                    return
                
                tmdb_id = task.tmdb_id
                media_type = task.media_type
                headers = task.headers
                audio_headers = task.audio_headers
                video_url = task.video_url
                submitted_video_url = video_url
                audio_url = task.audio_url
                video_source_type = task.video_source_type
                audio_source_type = task.audio_source_type
                season = task.season
                episode = task.episode
                subtitles_list = task.subtitles
                quality = task.quality
                language = task.language
                private_source_allowed = task.private_source_allowed

            try:
                headers = validate_headers(headers)
                audio_headers = validate_headers(audio_headers)
                validation_client = "127.0.0.1" if private_source_allowed else ""
                await validate_url(video_url, client_address=validation_client)
                await validate_url(audio_url, client_address=validation_client)
                for subtitle in subtitles_list:
                    await validate_url(subtitle.get("url"), client_address=validation_client)
                    subtitle["headers"] = validate_headers(subtitle.get("headers")) if subtitle.get("headers") is not None else headers
            except UnsafeIngestionSource as exc:
                raise IngestionTaskError(IngestionFailure("UNSAFE_SOURCE", str(exc), False)) from exc

            # 1. Disk Space Check
            storage_root = settings.TEMP_DIR
            try:
                self._reserve_storage(task_id, self.MINIMUM_TASK_RESERVATION_BYTES)
            except IngestionTaskError:
                raise
            except OSError as err:
                diagnostics_path = write_task_diagnostics(task_id, "storage check", repr(err))
                raise IngestionTaskError(IngestionFailure("STORAGE_UNAVAILABLE", "The configured storage root could not be checked.", False, diagnostics_path)) from err

            # 2. Probe Media Stream
            logger.info(f"[Queue Manager] Validating media sender source for task {task_id}...")
            probe_res = None
            for probe_attempt in range(1, 4):
                await validate_url(video_url, client_address=validation_client)
                await validate_url(audio_url, client_address=validation_client)
                probe_res = await probe_media_stream(
                    video_url,
                    audio_url,
                    headers,
                    audio_headers=audio_headers,
                    attempt=probe_attempt,
                    task_id=task_id,
                    video_source_type=video_source_type,
                    audio_source_type=audio_source_type,
                    client_address=validation_client,
                )
                probe_failure = probe_res.get("failure")
                if not probe_failure or not probe_failure.retryable or probe_attempt == 3:
                    break
                backoff = 3 * probe_attempt
                logger.warning(f"[Queue Manager] Source validation attempt {probe_attempt}/3 failed: {probe_failure.display}. Retrying in {backoff}s.")
                await asyncio.sleep(backoff)

            assert probe_res is not None
            has_video = probe_res["has_video"]
            has_audio = probe_res["has_audio"]
            scan_quality = probe_res["scan_quality"]
            video_source_type = probe_res["video_source_type"]
            audio_source_type = probe_res["audio_source_type"]

            # Save probe details to DB
            async with AsyncSession(engine) as db:
                task_db = await db.get(DownloadTask, task_id)
                if task_db:
                    task_db.has_video = has_video
                    task_db.has_audio = has_audio
                    task_db.scan_quality = scan_quality
                    task_db.video_source_type = video_source_type
                    task_db.audio_source_type = audio_source_type
                    db.add(task_db)
                    await db.commit()

            # 3. Notify Video Sender API
            asyncio.create_task(notify_video_sender(
                task_id=task_id,
                tmdb_id=tmdb_id,
                has_video=has_video,
                has_audio=has_audio,
                quality=scan_quality
            ))

            if probe_res.get("failure"):
                raise IngestionTaskError(probe_res["failure"])
            if not has_video:
                raise IngestionTaskError(IngestionFailure("INVALID_MEDIA_SOURCE", "The media sender source contains no video stream."))

            duration_secs = 3600.0
            try:
                if media_type == "movie":
                    meta = await tmdb_client.fetch_movie_metadata(tmdb_id)
                    duration_secs = self._parse_duration_to_seconds(meta.get("duration", "1h 30m"))
                else:
                    meta = await tmdb_client.fetch_show_metadata(tmdb_id)
                    duration_secs = self._parse_duration_to_seconds(meta.get("duration", "45m"))
                    if season is not None and episode is not None:
                        try:
                            ep_meta = await tmdb_client.fetch_episode_metadata(tmdb_id, season, episode)
                            meta["episode_detail"] = ep_meta
                            if ep_meta.get("duration"):
                                duration_secs = self._parse_duration_to_seconds(ep_meta["duration"])
                        except Exception as ep_err:
                            logger.error(f"[Queue Manager] Error fetching episode metadata: {ep_err}")
            except Exception as e:
                logger.error(f"[Queue Manager] Error fetching TMDB metadata: {e}")
                meta = {}

            probed_duration = float(probe_res.get("video_duration") or 0)
            if probed_duration > 0:
                duration_secs = probed_duration
            estimated_bytes = (
                int(duration_secs * self.ESTIMATED_MEDIA_BYTES_PER_SECOND)
                + self.STORAGE_SAFETY_MARGIN_BYTES
            )
            self._reserve_storage(task_id, estimated_bytes)

            raw_title = meta.get("title", f"Media_{tmdb_id}")
            clean_title = "".join(c for c in raw_title if c.isalnum() or c in " .-_")

            if media_type == "movie":
                release_year = meta.get("releaseYear", 2026)
                folder_name = f"{clean_title}_{release_year}_TMDB_{tmdb_id}"
                folder_rel_path = os.path.join(storage_root, "ingestion", task_id, "Movies", folder_name)
                filename = f"{clean_title}_{release_year}.mp4"
                output_rel_path = os.path.join(folder_rel_path, filename)
                catalog_abs_path = os.path.join(settings.MEDIA_DIR, "Movies", folder_name, filename)
            else:
                season_num = season or 1
                ep_num = episode or 1
                folder_name = f"{clean_title}_TMDB_{tmdb_id}"
                folder_rel_path = os.path.join(
                    storage_root,
                    "ingestion",
                    task_id,
                    "Series",
                    folder_name,
                    f"Season_{season_num}",
                    f"Episode_{ep_num}",
                )
                filename = f"{clean_title}_S{season_num:02d}E{ep_num:02d}.mp4"
                output_rel_path = os.path.join(folder_rel_path, filename)
                catalog_abs_path = os.path.join(
                    settings.MEDIA_DIR,
                    "Series",
                    folder_name,
                    f"Season_{season_num}",
                    f"Episode_{ep_num}",
                    filename,
                )
                
            output_abs_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", output_rel_path))
            os.makedirs(os.path.dirname(output_abs_path), exist_ok=True)
            created_artifacts.append(output_abs_path)

            for sub in subtitles_list:
                sub_url = sub.get("url")
                sub_lang = sub.get("language", "en")
                if sub_url and sub_url.startswith("http"):
                    try:
                        subtitle_headers = {
                            key: value
                            for key, value in sub["headers"].items()
                            if key.lower() not in {"range", "content-length"}
                        }
                        ext = ".srt" if ".srt" in sub_url.lower() else ".vtt"
                        sub_abs_path = os.path.join(os.path.dirname(output_abs_path), f"subtitle_{sub_lang}{ext}")
                        temporary_subtitle = f"{sub_abs_path}.part"
                        total_bytes = 0
                        response: Optional[httpx.Response] = None
                        async with httpx.AsyncClient(follow_redirects=False, timeout=15.0) as client:
                            response = await validated_stream_request(
                                client,
                                sub_url,
                                headers=subtitle_headers,
                                client_address=validation_client,
                            )
                            try:
                                response.raise_for_status()
                                async with aiofiles.open(temporary_subtitle, "wb") as subtitle_file:
                                    async for chunk in response.aiter_bytes():
                                        total_bytes += len(chunk)
                                        if total_bytes > 10 * 1024 * 1024:
                                            raise ValueError("Subtitle response exceeds the 10 MiB limit")
                                        await subtitle_file.write(chunk)
                            finally:
                                await response.aclose()
                        os.replace(temporary_subtitle, sub_abs_path)
                        created_artifacts.append(sub_abs_path)
                    except Exception as subtitle_error:
                        if "temporary_subtitle" in locals() and os.path.exists(temporary_subtitle):
                            os.remove(temporary_subtitle)
                        logger.warning(
                            f"[Subtitles] Could not download {sub_lang} subtitle for task {task_id}: "
                            f"host={urlsplit(sub_url).hostname or 'unknown'} "
                            f"status={getattr(locals().get('response'), 'status_code', 0)} "
                            f"error={type(subtitle_error).__name__}"
                        )

            # Convert any SRT subtitles to WebVTT
            for sub in subtitles_list:
                sub_lang = sub.get("language", "en")
                srt_path = os.path.join(os.path.dirname(output_abs_path), f"subtitle_{sub_lang}.srt")
                vtt_path = os.path.join(os.path.dirname(output_abs_path), f"subtitle_{sub_lang}.vtt")
                if os.path.exists(srt_path):
                    if srt_to_vtt(srt_path, vtt_path):
                        try:
                            os.rename(srt_path, srt_path + ".bak")
                            created_artifacts.append(vtt_path)
                            if srt_path in created_artifacts:
                                created_artifacts.remove(srt_path)
                            created_artifacts.append(srt_path + ".bak")
                        except Exception:
                            pass
                    else:
                        created_artifacts.append(srt_path)

            # 4. Retry Loop for Download & Merge
            max_retries = 3
            success = False
            last_failure = IngestionFailure("MEDIA_PROCESSING_FAILED", "The media source could not be processed.", True)
            extracted_languages = []
            
            for attempt in range(1, max_retries + 1):
                logger.info(f"[Queue Manager] Starting download/merge (Attempt {attempt}/{max_retries}) for task {task_id}...")
                if attempt > 1:
                    from services.state import update_task_metrics
                    update_task_metrics(task_id, 0.0, speed=f"Retrying ({attempt}/{max_retries})...", eta="00:00:00", force_write=True)
                
                await validate_url(video_url, client_address=validation_client)
                await validate_url(audio_url, client_address=validation_client)
                success, failure = await download_and_merge(
                    task_id=task_id, video_url=video_url, audio_url=audio_url,
                    headers=headers, audio_headers=audio_headers, output_path=output_abs_path, duration_secs=duration_secs,
                    attempt=attempt,
                    video_source_type=video_source_type, audio_source_type=audio_source_type,
                    audio_timeline_offset=float(probe_res.get("audio_timeline_offset") or 0.0),
                    client_address=validation_client,
                )
                
                if success:
                    break
                
                last_failure = failure or last_failure
                logger.warning(f"[Queue Manager] Ingestion attempt {attempt}/{max_retries} failed for task {task_id}: {last_failure.display}")
                
                if not last_failure.retryable:
                    logger.info(f"[Queue Manager] Task {task_id} has a permanent source failure; no retry scheduled.")
                    break
                if attempt < max_retries:
                    backoff = 5 * (2 ** (attempt - 1))
                    logger.info(f"[Queue Manager] Backing off for {backoff} seconds before next retry...")
                    await asyncio.sleep(backoff)

            if not success:
                raise IngestionTaskError(last_failure)

            if success:
                extracted_languages = await extract_audio_and_strip_video(
                    output_abs_path,
                    default_lang=language or "en",
                    override_primary=bool(language),
                )
                
                if settings.STORAGE_ENGINE == "CLOUD":
                    async with AsyncSession(engine) as db:
                        task_db = await db.get(DownloadTask, task_id)
                        if task_db:
                            task_db.status = "MOVING_CLOUD"
                            db.add(task_db)
                            await db.commit()
                
            local_dir = os.path.dirname(output_abs_path)
            uploaded_to_cloud = False
            cloud_ingestion = settings.STORAGE_ENGINE == "CLOUD"
            finalized_media_id = ""
            finalized_media_type = "movie" if media_type == "movie" else "episode"
            media_id = f"m_{tmdb_id}" if media_type == "movie" else f"ep_{tmdb_id}_s{season or 1}_e{episode or 1}"
            ownership = media_ownership(media_id, *([f"tv_{tmdb_id}"] if media_type != "movie" else []))
            await ownership.__aenter__()
            async with AsyncSession(engine, expire_on_commit=False) as db:
                task = await db.get(DownloadTask, task_id)
                if not task:
                    return
                await self._catalog_media(
                    db, tmdb_id, media_type, season, episode, meta, output_rel_path,
                    extracted_languages, language, subtitles_list, quality, task.skip_markers,
                    finalize=True, catalog_file_path=catalog_abs_path,
                )
                model = Movie if media_type == "movie" else Episode
                candidate = await db.get(model, media_id)
                candidate_data = candidate.model_dump()
                show_data = None
                if media_type != "movie":
                    show_data = (await db.get(Movie, f"tv_{tmdb_id}")).model_dump()
                await db.rollback()
            candidate = model.model_validate(candidate_data)
            mutation = MediaMutation(media_id)
            await mutation.__aenter__()
            from services.state import update_task_metrics
            update_task_metrics(task_id, 99.0, speed="Publishing verified media", eta="00:00:00", force_write=True)
            if cloud_ingestion:
                remote_subpath = (os.path.join("Movies", folder_name) if media_type == "movie" else
                                  os.path.join("Series", folder_name, f"Season_{season or 1}", f"Episode_{episode or 1}"))
                # Complete each bounded remote operation before observing cancellation.
                await mutation.publish_cloud_directory(local_dir, remote_subpath)
                uploaded_to_cloud = True
                from services.media_source import clear_cloud_object_cache
                clear_cloud_object_cache()
                source = await resolve_media_source(candidate.video_url, require_cloud=True)
                if not source.cloud_exists:
                    raise IngestionTaskError(IngestionFailure("CLOUD_MOVE_FAILED", "The verified uploaded media is unavailable."))
                if source.local_exists:
                    # A retained local fallback must receive the same accepted bytes.
                    _, cancelled = await settle(asyncio.to_thread(mutation.publish_directory, local_dir, os.path.dirname(catalog_abs_path)))
                    if cancelled:
                        raise asyncio.CancelledError
                    source = await resolve_media_source(candidate.video_url, require_cloud=True)
                candidate_data["source_fingerprint"] = playback_source_fingerprint(source, candidate.audio_metadata)
            else:
                _, cancelled = await settle(asyncio.to_thread(mutation.publish_directory, local_dir, os.path.dirname(catalog_abs_path)))
                # Fingerprints include mtime/inode where appropriate; use the published bytes.
                candidate_data["source_fingerprint"] = local_playback_fingerprint(Path(catalog_abs_path), candidate.audio_metadata)
                if cancelled:
                    raise asyncio.CancelledError
            from services.media_updates import atomic_json_write, upload_cloud_file
            portable = Path(local_dir) / ".metadata" / "metadata.json" if cloud_ingestion else Path(catalog_abs_path).parent / ".metadata" / "metadata.json"
            payload = json.loads(portable.read_text(encoding="utf-8"))
            payload["source_fingerprint"] = candidate_data["source_fingerprint"]
            accepted_video_fingerprint = source.video_fingerprint if cloud_ingestion else local_video_fingerprint(Path(catalog_abs_path))
            payload["video_fingerprint"] = accepted_video_fingerprint
            accepted_analysis = json.loads(candidate_data["analysis_metadata_str"])
            accepted_analysis.update(video_fingerprint=accepted_video_fingerprint, source_fingerprint=candidate_data["source_fingerprint"])
            candidate_data["analysis_metadata_str"] = json.dumps(accepted_analysis)
            payload["analysis"] = accepted_analysis
            if cloud_ingestion and source.local_exists:
                local_portable = Path(catalog_abs_path).parent / ".metadata" / "metadata.json"
                mutation.capture_local(str(local_portable))
                atomic_json_write(local_portable, payload)
            if not cloud_ingestion:
                mutation.capture_local(str(portable))
            atomic_json_write(portable, payload)
            if cloud_ingestion:
                await upload_cloud_file(portable, f"{source.cloud_path.rsplit('/', 1)[0]}/.metadata/metadata.json")
            if media_type != "movie":
                compatible = portable.with_name(f"metadata_s{season or 1}_e{episode or 1}.json")
                if not cloud_ingestion:
                    mutation.capture_local(str(compatible))
                atomic_json_write(compatible, payload)
                if cloud_ingestion:
                    await upload_cloud_file(compatible, f"{source.cloud_path.rsplit('/', 1)[0]}/.metadata/{compatible.name}")
            async with AsyncSession(engine, expire_on_commit=False) as db:
                task = await db.get(DownloadTask, task_id)
                if not task:
                    raise RuntimeError("Publication task disappeared before acceptance")
                if show_data is not None:
                    await db.merge(Movie.model_validate(show_data))
                finalized_media = await db.merge(model.model_validate(candidate_data))
                task.status = "COMPLETED"
                task.error_message = None
                db.add(task)
                finalized_media_id = media_id
                try:
                    await mutation.commit(db)
                finally:
                    task_completed = mutation.accepted
                created_artifacts.clear()

            update_task_metrics(task_id, 100.0, speed="Finished", eta="00:00:00", force_write=True)
            logger.info(f"[Queue Manager] Task completed successfully after publication acceptance: {task_id}")
            await self._schedule_playback_baseline(finalized_media_id, finalized_media_type)
            analysis_type = "movie" if media_type == "movie" else "episode"
            analysis_id = f"m_{tmdb_id}" if media_type == "movie" else f"ep_{tmdb_id}_s{season or 1}_e{episode or 1}"
            try:
                await vibe_analysis_manager.enqueue(analysis_type, analysis_id)
            except Exception as analysis_error:
                logger.warning(
                    f"[Queue Manager] Vibe analysis scheduling failed for {analysis_id}: "
                    f"{type(analysis_error).__name__}"
                )
            if uploaded_to_cloud and os.path.exists(local_dir):
                shutil.rmtree(local_dir)
                    
        except IngestionTaskError as err:
            logger.error(f"[Queue Manager] Task {task_id} failed: {err.failure.display}")
            await self._record_task_failure(task_id, err.failure, submitted_video_url)
        except asyncio.CancelledError:
            if task_id not in self.cancelled_tasks:
                await self._record_task_failure(task_id, IngestionFailure("INGESTION_INTERRUPTED", "The task was interrupted by server shutdown."), submitted_video_url)
            raise
        except Exception as err:
            diagnostics_path = write_task_diagnostics(task_id, "queue exception", traceback.format_exc())
            failure = IngestionFailure("QUEUE_INTERNAL_ERROR", f"The queue encountered {type(err).__name__}.", False, diagnostics_path)
            logger.error(f"[Queue Manager] Task {task_id} failed: {failure.display}")
            await self._record_task_failure(task_id, failure, submitted_video_url)
                
        finally:
            recovery_pending = False
            if mutation is not None:
                try:
                    await mutation.__aexit__(None, None, None)
                except BaseException as rollback_error:
                    recovery_pending = True
                    logger.error("[Queue Manager] Media publication evidence retained for recovery: %s", type(rollback_error).__name__)
            if ownership is not None:
                await ownership.__aexit__(None, None, None)
            if recovery_pending:
                created_artifacts.clear()
            for artifact in created_artifacts:
                if os.path.exists(artifact):
                    try:
                        os.remove(artifact)
                    except OSError:
                        pass
            if output_abs_path:
                self._remove_empty_parents(os.path.dirname(output_abs_path))
            if staging_task_root.exists() and not recovery_pending:
                try:
                    await run_cancellation_safe_thread(shutil.rmtree, staging_task_root)
                except OSError as staging_cleanup_error:
                    logger.warning(
                        f"[Queue Manager] Could not remove staging directory for task {task_id}: "
                        f"{type(staging_cleanup_error).__name__}"
                    )
            self.active_tasks.discard(task_id)
            self.active_media_keys.pop(task_id, None)
            self.storage_reservations.pop(task_id, None)
            from services.state import remove_task_metrics
            remove_task_metrics(task_id)

    async def _record_task_failure(self, task_id: str, failure: IngestionFailure, submitted_video_url: str) -> None:
        try:
            async with AsyncSession(engine) as db:
                task = await db.get(DownloadTask, task_id)
                if not task:
                    return
                if task.status == "COMPLETED":
                    logger.warning(
                        f"[Queue Manager] Ignoring late failure for completed task {task_id}: "
                        f"{failure.code}"
                    )
                    return
                ingest_preview_service.mark_error(task_id, failure.code, failure.message)
                task.status = "FAILED"
                task.error_message = failure.display
                db.add(task)

                await db.commit()
        except Exception as db_err:
            logger.error(f"[Queue Manager] Failed to persist task {task_id} failure: {type(db_err).__name__}")

    @staticmethod
    def _remove_empty_parents(start_dir: str) -> None:
        current = os.path.abspath(start_dir)
        roots = [os.path.abspath(settings.MEDIA_DIR), os.path.abspath(settings.TEMP_DIR)]
        try:
            root = next((candidate for candidate in roots if os.path.commonpath([current, candidate]) == candidate), None)
        except ValueError:
            return
        if not root:
            return
        while current != root:
            try:
                os.rmdir(current)
            except OSError:
                break
            current = os.path.dirname(current)

    async def _schedule_playback_baseline(self, media_id: str, media_type: str) -> None:
        """Refresh source identity and schedule warming without affecting ingestion success."""

        try:
            from services.playback_prep import playback_prep_service

            model = Movie if media_type == "movie" else Episode
            async with AsyncSession(engine, expire_on_commit=False) as db:
                media_obj = await db.get(model, media_id)
                if not media_obj or not media_obj.video_url:
                    return
                source = await resolve_media_source(media_obj.video_url)
                if not source.available:
                    return
                fingerprint = playback_source_fingerprint(source, media_obj.audio_metadata or [])
                if media_obj.source_fingerprint != fingerprint:
                    media_obj.source_fingerprint = fingerprint
                    db.add(media_obj)
                    await db.commit()
                await playback_prep_service.prepare(
                    media_id,
                    media_obj,
                    source,
                    include_remaining=True,
                    retry_errors=True,
                    foreground=False,
                )
            logger.info(f"[Queue Manager] Adaptive playback warming scheduled for {media_id}.")
        except Exception as exc:
            logger.warning(
                f"[Queue Manager] Playback baseline scheduling failed for "
                f"{media_id or 'unknown'}: {type(exc).__name__}"
            )

    async def _catalog_media(
        self, db: AsyncSession, tmdb_id: int, media_type: str, season: Optional[int], 
        episode: Optional[int], meta: Dict[str, Any], file_path: str, 
        extracted_languages: Optional[List[str]] = None, language: Optional[str] = None, 
        subtitles_list: Optional[List[Dict[str, Any]]] = None, quality: Optional[str] = None,
        skip_markers: Optional[Dict[str, Any]] = None,
        *,
        finalize: bool = True,
        preview_task_id: Optional[str] = None,
        catalog_file_path: Optional[str] = None,
        retained_audio_metadata: Optional[List[Dict[str, Any]]] = None,
        retained_video_fingerprint: Optional[str] = None,
        retained_subtitle_metadata: Optional[List[Dict[str, Any]]] = None,
    ):
        server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        
        raw_title = meta.get("title", f"Media_{tmdb_id}")
        clean_title = "".join(c for c in raw_title if c.isalnum() or c in " .-_")
        
        if media_type == "movie":
            release_year = meta.get("releaseYear", 2026)
            correct_folder_name = f"{clean_title}_{release_year}_TMDB_{tmdb_id}"
            parent_dir_rel = "media/Movies"
        else:
            correct_folder_name = f"{clean_title}_TMDB_{tmdb_id}"
            parent_dir_rel = "media/Series"
            
        current_folder_name = None
        served_url = ""
        virtual_path = ""
        if file_path:
            parts = file_path.replace("\\", "/").split("/")
            if media_type == "movie" and len(parts) >= 2:
                current_folder_name = parts[-2]
            elif (media_type == "series" or media_type == "tv") and len(parts) >= 4:
                current_folder_name = parts[-4]
                
            file_candidate = os.path.abspath(file_path) if os.path.isabs(file_path) else os.path.abspath(os.path.join(server_root, file_path))
            catalog_candidate = catalog_file_path or file_candidate
            served_url = catalog_path_from_storage(catalog_candidate)
            virtual_path = served_url.lstrip("/")

        abs_file_path = os.path.abspath(file_candidate) if file_path else ""
        
        if media_type == "movie":
            main_folder_rel = os.path.dirname(virtual_path)
        else:
            main_folder_rel = os.path.dirname(virtual_path)
            
        if media_type == "movie":
            main_folder_abs = os.path.dirname(abs_file_path)
        else:
            main_folder_abs = os.path.dirname(abs_file_path)
        os.makedirs(main_folder_abs, exist_ok=True)
        
        poster_abs = os.path.join(main_folder_abs, "poster.jpg")
        backdrop_abs = os.path.join(main_folder_abs, "backdrop.jpg")
        
        local_thumbnail = await download_and_cache_metadata_image(meta.get("thumbnailUrl"), poster_abs)
        local_banner = await download_and_cache_metadata_image(meta.get("bannerUrl"), backdrop_abs)
        if os.path.exists(poster_abs):
            local_thumbnail = f"/{os.path.join(main_folder_rel, 'poster.jpg').replace(os.sep, '/')}"
        if os.path.exists(backdrop_abs):
            local_banner = f"/{os.path.join(main_folder_rel, 'backdrop.jpg').replace(os.sep, '/')}"

        ep_thumb_rel = ""
        if media_type != "movie":
            ep_thumb_abs = os.path.join(os.path.dirname(abs_file_path), "thumbnail.jpg")
            ep_still_url = (meta.get("episode_detail") or {}).get("thumbnailUrl")
            if ep_still_url:
                await download_and_cache_metadata_image(ep_still_url, ep_thumb_abs)
            if os.path.exists(ep_thumb_abs):
                ep_thumb_rel = f"/{os.path.join(os.path.dirname(virtual_path), 'thumbnail.jpg').replace(os.sep, '/')}"

        # Probe completed media file
        probe_meta = {}
        if file_path:
            probe_meta = await probe_completed_media(abs_file_path)

        if file_path and not probe_meta.get("valid_video"):
            raise IngestionTaskError(IngestionFailure("MEDIA_PROBE_FAILED", "The completed media has no valid measurable video stream."))
        analysis = refreshed_analysis(probe_meta.get("analysis"), {"sender_quality": quality})
        probed_duration = probe_meta.get("probed_duration")
        container = probe_meta.get("container")
        codec = probe_meta.get("codec")
        width = probe_meta.get("width")
        height = probe_meta.get("height")
        frame_rate = probe_meta.get("frame_rate")
        source_fingerprint = probe_meta.get("source_fingerprint")
        video_fingerprint = probe_meta.get("video_fingerprint")
        selected_language = normalize_language_code(language, "en") if language else None
        audio_meta_list = apply_primary_audio_language(probe_meta.get("audio_metadata", []), selected_language)
        if retained_audio_metadata:
            audio_meta_list = retained_primary_audio_language(audio_meta_list, retained_audio_metadata,
                same_video=bool(video_fingerprint) and retained_video_fingerprint == video_fingerprint)
            if video_fingerprint and retained_video_fingerprint == video_fingerprint:
                audio_meta_list = _retained_external_audio(audio_meta_list, retained_audio_metadata)
        primary = primary_embedded_audio_index(audio_meta_list)
        if primary is not None and audio_meta_list[primary].get("languageOverride", {}).get("source") == "sender":
            selected_language = audio_meta_list[primary]["language"]
        
        if media_type == "movie":
            movie_id = f"m_{tmdb_id}"
            movie_folder_abs = os.path.dirname(abs_file_path)
            movie_quality = probe_meta.get("quality") or "Source"
            movie_languages = audio_languages(audio_meta_list) or extracted_languages or ([selected_language] if selected_language else ["en"])
            
            subs_on_disk = []
            if subtitles_list:
                for sub in subtitles_list:
                    sub_lang = sub.get("language", "en")
                    srt_abs = os.path.join(movie_folder_abs, f"subtitle_{sub_lang}.srt")
                    vtt_abs = os.path.join(movie_folder_abs, f"subtitle_{sub_lang}.vtt")
                    if os.path.exists(srt_abs) and not os.path.exists(vtt_abs):
                        srt_to_vtt(srt_abs, vtt_abs)
                    if os.path.exists(vtt_abs):
                        subs_on_disk.append({
                            "language": sub_lang,
                            "ext": ".vtt",
                            "fileName": f"subtitle_{sub_lang}.vtt",
                        })

            if retained_subtitle_metadata is not None and video_fingerprint and retained_video_fingerprint == video_fingerprint:
                subs_on_disk = _retained_subtitles(movie_folder_abs, retained_subtitle_metadata)
            duration_str = duration_label(probed_duration)
            skip_data = skip_markers or {}
            
            movie = await db.get(Movie, movie_id)
            if not movie:
                movie = Movie(
                    id=movie_id, title=meta.get("title", f"Movie {tmdb_id}"), description=meta.get("description", ""),
                    thumbnail_url=local_thumbnail or "", banner_url=local_banner or "", video_url=served_url,
                    duration=duration_str, release_year=meta.get("releaseYear", 2026),
                    rating=meta.get("rating", "PG-13"), director=meta.get("director", "Unknown"),
                    original_language=selected_language or meta.get("originalLanguage", "en"), type="movie",
                    vote_average=meta.get("vote_average", 7.5), vote_count=meta.get("vote_count", 100),
                    tmdb_id=int(tmdb_id), catalog_source="server", availability="available" if finalize else "processing",
                    probed_duration=probed_duration, container=container, codec=codec,
                    width=width, height=height, frame_rate=frame_rate,
                    source_fingerprint=source_fingerprint,
                    hevc_compressed=str(codec or "").lower() in {"hevc", "h265"},
                )
                movie.genres = meta.get("genres", [])
                movie.cast = meta.get("cast", [])
                movie.crew = meta.get("crew", [])
                movie.keywords = meta.get("keywords", [])
                movie.collection_name = meta.get("collectionName") or meta.get("collection_name")
                movie.trope_vectors = meta.get("tropeVectors") or meta.get("trope_vectors") or compute_trope_vectors(movie.genres, movie.keywords, movie.description)
                movie.quality = movie_quality
                movie.analysis_metadata_str = json.dumps(analysis)
                movie.languages = movie_languages
                movie.subtitles = subs_on_disk
                movie.skip_markers = skip_data
                movie.audio_metadata = audio_meta_list
                movie.preview_task_id = None if finalize else preview_task_id
                db.add(movie)
                await db.flush()
                logger.info(f"[Queue Manager] Cataloged new movie: {meta.get('title', 'Unknown Movie')}")
            else:
                movie.title = meta.get("title", movie.title)
                movie.description = meta.get("description", movie.description)
                movie.thumbnail_url = local_thumbnail or movie.thumbnail_url
                movie.banner_url = local_banner or movie.banner_url
                movie.video_url = served_url
                movie.tmdb_id = int(tmdb_id)
                movie.catalog_source = "server"
                movie.availability = "available" if finalize else "processing"
                movie.duration = duration_str
                movie.release_year = meta.get("releaseYear", movie.release_year)
                movie.rating = meta.get("rating", movie.rating)
                movie.director = meta.get("director", movie.director)
                movie.cast = meta.get("cast", movie.cast)
                movie.genres = meta.get("genres", movie.genres)
                movie.crew = meta.get("crew", movie.crew)
                movie.keywords = meta.get("keywords", movie.keywords)
                movie.collection_name = meta.get("collectionName") or meta.get("collection_name") or movie.collection_name
                movie.trope_vectors = meta.get("tropeVectors") or meta.get("trope_vectors") or compute_trope_vectors(movie.genres, movie.keywords, movie.description)
                movie.original_language = selected_language or meta.get("originalLanguage", movie.original_language)
                movie.quality = movie_quality
                movie.analysis_metadata_str = json.dumps(analysis)
                movie.languages = movie_languages
                movie.subtitles = subs_on_disk
                movie.skip_markers = skip_data
                movie.vote_average = meta.get("vote_average", movie.vote_average)
                movie.vote_count = meta.get("vote_count", movie.vote_count)
                
                movie.probed_duration = probed_duration
                movie.container = container
                movie.codec = codec
                movie.width = width
                movie.height = height
                movie.frame_rate = frame_rate
                movie.source_fingerprint = source_fingerprint
                movie.preview_task_id = None if finalize else preview_task_id
                movie.hevc_compressed = str(codec or "").lower() in {"hevc", "h265"}
                movie.audio_metadata = audio_meta_list
                movie.vibe_analysis_status = "queued" if subs_on_disk else "unavailable"
                movie.vibe_analysis_version = 0 if subs_on_disk else VIBE_ANALYSIS_VERSION
                db.add(movie)
                await db.flush()
                logger.info(f"[Queue Manager] Updated existing movie details: {movie.title}")

            movie.vibe_analysis_status = "queued" if subs_on_disk else "unavailable"
            movie.vibe_analysis_version = 0 if subs_on_disk else VIBE_ANALYSIS_VERSION
            db.add(movie)
            await db.flush()

            movie_metadata_dir = os.path.join(movie_folder_abs, ".metadata")
            os.makedirs(movie_metadata_dir, exist_ok=True)
            movie_metadata_file = os.path.join(movie_metadata_dir, "metadata.json")

            movie_metadata_content = {
                "tmdb_id": int(tmdb_id),
                "media_type": "movie",
                "title": meta.get("title", movie.title),
                "description": meta.get("description", movie.description),
                "release_year": int(meta.get("releaseYear", movie.release_year)),
                "video_url": served_url,
                "catalog_source": "server",
                "availability": "available",
                "language": audio_meta_list[primary]["language"] if primary is not None else selected_language or "en",
                "languages": movie_languages,
                "quality": movie_quality,
                "subtitles": subs_on_disk,
                "original_language": movie.original_language,
                "skip_markers": skip_data,
                "analysis": analysis,
                "probed_duration": probed_duration,
                "container": container,
                "codec": codec,
                "width": width,
                "height": height,
                "frame_rate": frame_rate,
                "source_fingerprint": source_fingerprint,
                "video_fingerprint": video_fingerprint,
                "audio_metadata": audio_meta_list,
                "stream_manifest": probe_meta.get("stream_manifest", []),
                "genres": movie.genres,
                "cast": movie.cast,
                "director": movie.director,
                "crew": movie.crew,
                "keywords": movie.keywords,
                "collection_name": movie.collection_name,
                "trope_vectors": movie.trope_vectors,
                "vibe_analysis_status": movie.vibe_analysis_status,
                "vibe_analysis_version": movie.vibe_analysis_version,
            }
            _atomic_write_json(movie_metadata_file, movie_metadata_content)
        else:
            show_id = f"tv_{tmdb_id}"
            show = await db.get(Movie, show_id)
            if not show:
                show = Movie(
                    id=show_id, title=meta.get("title", f"Show {tmdb_id}"), description=meta.get("description", ""),
                    thumbnail_url=local_thumbnail or "", banner_url=local_banner or "", video_url="",
                    duration=meta.get("duration", "45m"), release_year=meta.get("releaseYear", 2026),
                    rating=meta.get("rating", "TV-14"), director=meta.get("director", "Various"),
                    original_language=selected_language or meta.get("originalLanguage", "en"), type="series",
                    vote_average=meta.get("vote_average", 7.5), vote_count=meta.get("vote_count", 100),
                    tmdb_id=int(tmdb_id), catalog_source="server", availability="available" if finalize else "processing"
                )
                show.genres = meta.get("genres", [])
                show.cast = meta.get("cast", [])
                show.crew = meta.get("crew", [])
                show.keywords = meta.get("keywords", [])
                show.collection_name = meta.get("collectionName") or meta.get("collection_name")
                show.trope_vectors = meta.get("tropeVectors") or meta.get("trope_vectors") or compute_trope_vectors(show.genres, show.keywords, show.description)
                db.add(show)
                logger.info(f"[Queue Manager] Cataloged new TV show: {meta.get('title', 'Unknown Show')}")
            else:
                show.title = meta.get("title", show.title)
                show.description = meta.get("description", show.description)
                show.thumbnail_url = local_thumbnail or show.thumbnail_url
                show.banner_url = local_banner or show.banner_url
                show.duration = meta.get("duration", show.duration)
                show.release_year = meta.get("releaseYear", show.release_year)
                show.rating = meta.get("rating", show.rating)
                show.director = meta.get("director", show.director)
                show.cast = meta.get("cast", show.cast)
                show.genres = meta.get("genres", show.genres)
                show.crew = meta.get("crew", show.crew)
                show.keywords = meta.get("keywords", show.keywords)
                show.collection_name = meta.get("collectionName") or meta.get("collection_name") or show.collection_name
                show.trope_vectors = meta.get("tropeVectors") or meta.get("trope_vectors") or compute_trope_vectors(show.genres, show.keywords, show.description)
                show.original_language = selected_language or meta.get("originalLanguage", show.original_language)
                show.vote_average = meta.get("vote_average", show.vote_average)
                show.vote_count = meta.get("vote_count", show.vote_count)
                db.add(show)
                logger.info(f"[Queue Manager] Updated TV show: {show.title}")
                
            season_num = season or 1
            episode_num = episode or 1

            ep_quality = probe_meta.get("quality") or "Source"
            ep_languages = audio_languages(audio_meta_list) or extracted_languages or ([selected_language] if selected_language else ["en"])

            ep_folder_abs = os.path.dirname(abs_file_path)
            subs_on_disk = []
            if subtitles_list:
                for sub in subtitles_list:
                    sub_lang = sub.get("language", "en")
                    srt_abs = os.path.join(ep_folder_abs, f"subtitle_{sub_lang}.srt")
                    vtt_abs = os.path.join(ep_folder_abs, f"subtitle_{sub_lang}.vtt")
                    if os.path.exists(srt_abs) and not os.path.exists(vtt_abs):
                        srt_to_vtt(srt_abs, vtt_abs)
                    if os.path.exists(vtt_abs):
                        subs_on_disk.append({
                            "language": sub_lang,
                            "ext": ".vtt",
                            "fileName": f"subtitle_{sub_lang}.vtt",
                        })
            
            if retained_subtitle_metadata is not None and video_fingerprint and retained_video_fingerprint == video_fingerprint:
                subs_on_disk = _retained_subtitles(ep_folder_abs, retained_subtitle_metadata)
            ep_id = f"ep_{tmdb_id}_s{season_num}_e{episode_num}"
            ep_entry = await db.get(Episode, ep_id)
            
            ep_meta = meta.get("episode_detail", {})
            ep_title = ep_meta.get("title", f"Episode {episode_num}")
            ep_desc = ep_meta.get("description", f"Season {season_num}, Episode {episode_num}")
            ep_still_url = ep_meta.get("thumbnailUrl")
            
            ep_dur_str = duration_label(probed_duration)
            ep_skip_data = skip_markers or {}
            
            if not ep_entry:
                ep_entry = Episode(
                    id=ep_id, movie_id=show_id, episode_number=episode_num, season_number=season_num,
                    title=ep_title, description=ep_desc, thumbnail_url=ep_thumb_rel or "",
                    video_url=served_url, duration=ep_dur_str,
                    probed_duration=probed_duration, container=container, codec=codec,
                    width=width, height=height, frame_rate=frame_rate,
                    source_fingerprint=source_fingerprint,
                    hevc_compressed=str(codec or "").lower() in {"hevc", "h265"},
                )
                ep_entry.quality = ep_quality
                ep_entry.analysis_metadata_str = json.dumps(analysis)
                ep_entry.languages = ep_languages
                ep_entry.subtitles = subs_on_disk
                ep_entry.skip_markers = ep_skip_data
                ep_entry.audio_metadata = audio_meta_list
                ep_entry.preview_task_id = None if finalize else preview_task_id
                ep_entry.vibe_analysis_status = "queued" if subs_on_disk else "unavailable"
                ep_entry.vibe_analysis_version = 0 if subs_on_disk else VIBE_ANALYSIS_VERSION
                db.add(ep_entry)
                logger.info(f"[Queue Manager] Cataloged new episode: S{season_num}E{episode_num}")
            else:
                ep_entry.title = ep_title
                ep_entry.description = ep_desc
                ep_entry.thumbnail_url = ep_thumb_rel or ep_entry.thumbnail_url
                ep_entry.video_url = served_url
                ep_entry.duration = ep_dur_str
                ep_entry.quality = ep_quality
                ep_entry.analysis_metadata_str = json.dumps(analysis)
                ep_entry.languages = ep_languages
                ep_entry.subtitles = subs_on_disk
                ep_entry.skip_markers = ep_skip_data
                
                ep_entry.probed_duration = probed_duration
                ep_entry.container = container
                ep_entry.codec = codec
                ep_entry.width = width
                ep_entry.height = height
                ep_entry.frame_rate = frame_rate
                ep_entry.source_fingerprint = source_fingerprint
                ep_entry.preview_task_id = None if finalize else preview_task_id
                ep_entry.hevc_compressed = str(codec or "").lower() in {"hevc", "h265"}
                ep_entry.audio_metadata = audio_meta_list
                ep_entry.vibe_analysis_status = "queued" if subs_on_disk else "unavailable"
                ep_entry.vibe_analysis_version = 0 if subs_on_disk else VIBE_ANALYSIS_VERSION
                db.add(ep_entry)
                logger.info(f"[Queue Manager] Updated episode: S{season_num}E{episode_num}")

            show.tmdb_id = int(tmdb_id)
            show.catalog_source = "server"
            show.availability = "available" if finalize or show.availability == "available" else "processing"
            db.add(show)
            await db.flush()

            ep_metadata_dir = os.path.join(ep_folder_abs, ".metadata")
            os.makedirs(ep_metadata_dir, exist_ok=True)
            
            ep_metadata_file = os.path.join(ep_metadata_dir, "metadata.json")

            ep_metadata_content = {
                "tmdb_id": int(tmdb_id),
                "media_type": "series",
                "title": ep_title,
                "description": ep_desc,
                "season": season_num,
                "episode": episode_num,
                "video_url": served_url,
                "catalog_source": "server",
                "availability": "available",
                "language": audio_meta_list[primary]["language"] if primary is not None else selected_language or "en",
                "languages": ep_languages,
                "quality": ep_quality,
                "subtitles": subs_on_disk,
                "original_language": show.original_language,
                "skip_markers": ep_skip_data,
                "analysis": analysis,
                "probed_duration": probed_duration,
                "container": container,
                "codec": codec,
                "width": width,
                "height": height,
                "frame_rate": frame_rate,
                "source_fingerprint": source_fingerprint,
                "video_fingerprint": video_fingerprint,
                "audio_metadata": audio_meta_list,
                "stream_manifest": probe_meta.get("stream_manifest", []),
                "genres": show.genres,
                "cast": show.cast,
                "director": show.director,
                "crew": show.crew,
                "keywords": show.keywords,
                "collection_name": show.collection_name,
                "trope_vectors": show.trope_vectors,
                "vibe_analysis_status": ep_entry.vibe_analysis_status,
                "vibe_analysis_version": ep_entry.vibe_analysis_version,
            }
            _atomic_write_json(ep_metadata_file, ep_metadata_content)
                
            sync_compatible_metadata_file = os.path.join(ep_metadata_dir, f"metadata_s{season_num}_e{episode_num}.json")
            _atomic_write_json(sync_compatible_metadata_file, ep_metadata_content)

    def _parse_duration_to_seconds(self, duration_str: str) -> float:
        total_seconds = 0.0
        try:
            if "h" in duration_str:
                parts = duration_str.split("h")
                total_seconds += float(parts[0].strip()) * 3600.0
                if "m" in parts[1]:
                    total_seconds += float(parts[1].replace("m", "").strip()) * 60.0
            elif "m" in duration_str:
                total_seconds += float(duration_str.replace("m", "").strip()) * 60.0
            else:
                total_seconds = float(duration_str)
        except Exception:
            return 3600.0
        return total_seconds if total_seconds > 0 else 3600.0

    async def _restore_cloud_catalog_entry(
        self,
        db: AsyncSession,
        data: Dict[str, Any],
        meta: Dict[str, Any],
        season: Optional[int],
        episode: Optional[int],
        *, verified_source_fingerprint: Optional[str] = None,
        selected_bundle: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Rebuild a playable catalog row from verified cloud-side portable metadata."""
        tmdb_id = int(data["tmdb_id"])
        video_url = str(data["video_url"])
        codec = data.get("codec")
        measured_duration = duration_label(data.get("probed_duration"))
        common_probe = {
            "probed_duration": data.get("probed_duration"),
            "container": data.get("container"),
            "codec": codec,
            "width": data.get("width"),
            "height": data.get("height"),
            "frame_rate": data.get("frame_rate"),
            "analysis_metadata_str": json.dumps(refreshed_analysis(data.get("analysis"), data.get("analysis"))),
            "source_fingerprint": verified_source_fingerprint,
        }
        if data.get("media_type") == "movie":
            folder_url = video_url.rsplit("/", 1)[0]
            movie = await db.get(Movie, f"m_{tmdb_id}") or Movie(
                id=f"m_{tmdb_id}",
                title=meta.get("title", data.get("title", f"Movie {tmdb_id}")),
                description=meta.get("description", data.get("description", "")),
                thumbnail_url=f"{folder_url}/poster.jpg",
                banner_url=f"{folder_url}/backdrop.jpg",
                video_url=video_url,
                duration=meta.get("duration", "2h"),
                release_year=int(meta.get("releaseYear", data.get("release_year", 0)) or 0),
                rating=meta.get("rating"),
                director=meta.get("director") or data.get("director"),
                type="movie",
                tmdb_id=tmdb_id,
            )
            movie.title = meta.get("title", data.get("title", movie.title))
            movie.description = meta.get("description", data.get("description", movie.description))
            movie.thumbnail_url = f"{folder_url}/poster.jpg"
            movie.banner_url = f"{folder_url}/backdrop.jpg"
            movie.video_url = video_url
            movie.catalog_source = "server"
            movie.availability = "available"
            movie.quality = data.get("quality") or "Source"
            movie.duration = measured_duration if measured_duration != "Unknown" else (data.get("duration") or movie.duration)
            movie.languages = data.get("languages") or [data.get("language") or "en"]
            movie.subtitles = data.get("subtitles") or []
            movie.skip_markers = data.get("skip_markers") or {}
            movie.genres = meta.get("genres", data.get("genres", []))
            movie.cast = meta.get("cast", data.get("cast", []))
            movie.crew = meta.get("crew", data.get("crew", []))
            movie.keywords = meta.get("keywords", data.get("keywords", []))
            movie.collection_name = meta.get("collectionName") or data.get("collection_name")
            movie.trope_vectors = data.get("trope_vectors") or compute_trope_vectors(movie.genres, movie.keywords, movie.description)
            movie.audio_metadata = data.get("audio_metadata") or []
            movie.hevc_compressed = str(codec or "").lower() in {"hevc", "h265"}
            for key, value in common_probe.items():
                setattr(movie, key, value)
            if selected_bundle is not None:
                _apply_catalog_bundle(movie, selected_bundle)
            db.add(movie)
            await db.flush()
            return

        if season is None or episode is None:
            raise ValueError("Cloud episode metadata is missing season or episode")
        show_folder_url = video_url.rsplit("/", 1)[0]
        show_id = f"tv_{tmdb_id}"
        show = await db.get(Movie, show_id) or Movie(
            id=show_id,
            title=meta.get("title", f"Show {tmdb_id}"),
            description=meta.get("description", ""),
            thumbnail_url=f"{show_folder_url}/poster.jpg",
            banner_url=f"{show_folder_url}/backdrop.jpg",
            video_url="",
            duration=meta.get("duration", "45m"),
            release_year=int(meta.get("releaseYear", 0) or 0),
            rating=meta.get("rating"),
            director=meta.get("director"),
            type="series",
            tmdb_id=tmdb_id,
        )
        show.catalog_source = "server"
        show.availability = "available"
        show.genres = meta.get("genres", data.get("genres", []))
        show.cast = meta.get("cast", data.get("cast", []))
        show.crew = meta.get("crew", data.get("crew", []))
        show.keywords = meta.get("keywords", data.get("keywords", []))
        show.trope_vectors = data.get("trope_vectors") or compute_trope_vectors(show.genres, show.keywords, show.description)
        db.add(show)
        await db.flush()

        episode_folder_url = video_url.rsplit("/", 1)[0]
        ep_meta = meta.get("episode_detail", {})
        ep_id = f"ep_{tmdb_id}_s{season}_e{episode}"
        ep_entry = await db.get(Episode, ep_id) or Episode(
            id=ep_id,
            movie_id=show_id,
            episode_number=episode,
            season_number=season,
            title=ep_meta.get("title", data.get("title", f"Episode {episode}")),
            description=ep_meta.get("description", data.get("description", "")),
            thumbnail_url=f"{episode_folder_url}/thumbnail.jpg",
            video_url=video_url,
            duration=ep_meta.get("duration", "45m"),
        )
        ep_entry.video_url = video_url
        ep_entry.quality = data.get("quality") or "Source"
        ep_entry.duration = measured_duration if measured_duration != "Unknown" else (data.get("duration") or ep_entry.duration)
        ep_entry.languages = data.get("languages") or [data.get("language") or "en"]
        ep_entry.subtitles = data.get("subtitles") or []
        ep_entry.skip_markers = data.get("skip_markers") or {}
        ep_entry.audio_metadata = data.get("audio_metadata") or []
        ep_entry.hevc_compressed = str(codec or "").lower() in {"hevc", "h265"}
        if selected_bundle is not None:
            _apply_catalog_bundle(ep_entry, selected_bundle)
        for key, value in common_probe.items():
            setattr(ep_entry, key, value)
            db.add(ep_entry)
            await db.flush()

    async def _reconcile_post_commit_failures(self) -> int:
        """Repair tasks that were published before the legacy post-commit ORM failure."""

        repaired: List[tuple[str, str]] = []
        async with AsyncSession(engine, expire_on_commit=False) as db:
            statement = select(DownloadTask).where(DownloadTask.status == "FAILED")
            tasks = (await db.exec(statement)).all()
            for task in tasks:
                error_message = str(task.error_message or "")
                if "MissingGreenlet" not in error_message:
                    continue
                if task.media_type == "movie":
                    media_id = f"m_{task.tmdb_id}"
                    media_type = "movie"
                    media_obj = await db.get(Movie, media_id)
                elif task.season is not None and task.episode is not None:
                    media_id = f"ep_{task.tmdb_id}_s{task.season}_e{task.episode}"
                    media_type = "episode"
                    media_obj = await db.get(Episode, media_id)
                else:
                    continue
                if not media_obj or not local_catalog_source_exists(str(media_obj.video_url or "")):
                    continue
                task.status = "COMPLETED"
                task.error_message = None
                if getattr(media_obj, "preview_task_id", None) == task.id:
                    media_obj.preview_task_id = None
                    db.add(media_obj)
                db.add(task)
                repaired.append((media_id, media_type))
            if repaired:
                await db.commit()

        for media_id, media_type in repaired:
            await self._schedule_playback_baseline(media_id, media_type)
            try:
                await vibe_analysis_manager.enqueue(media_type, media_id)
            except Exception as analysis_error:
                logger.warning(
                    f"[Queue Manager Recovery] Vibe analysis scheduling failed for {media_id}: "
                    f"{type(analysis_error).__name__}"
                )
        if repaired:
            logger.info(
                f"[Queue Manager Recovery] Reconciled {len(repaired)} task(s) that were "
                "published before a post-commit failure."
            )
        return len(repaired)

    async def sync_media_from_disk(self, *, raise_on_critical_failure: bool = False):
        """
        Scans the media directory for metadata.json files and re-catalogs them into the database if they are missing.
        Also sweeps the database to verify path containment, localhost urls and file existence.
        """
        try:
            server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            media_dir = os.path.abspath(settings.MEDIA_DIR)

            cloud_inventory: Optional[set[str]] = None
            if settings.STORAGE_ENGINE == "CLOUD" and rclone_service.executable():
                metadata_sync = await rclone_service.run(
                    "copy",
                    settings.RCLONE_REMOTE_PATH,
                    media_dir,
                    "--include",
                    "**/.metadata/*.json",
                    "--include",
                    "**/poster.jpg",
                    "--include",
                    "**/backdrop.jpg",
                    "--include",
                    "**/thumbnail.jpg",
                    timeout=30 * 60,
                )
                if not metadata_sync.ok:
                    logger.warning(
                        f"[Queue Manager Recovery] Cloud metadata sync failed: "
                        f"{metadata_sync.error_code or 'rclone_failed'}"
                    )
                inventory_result = await rclone_service.run(
                    "lsf",
                    settings.RCLONE_REMOTE_PATH,
                    "--recursive",
                    "--files-only",
                    timeout=30 * 60,
                    output_limit=None,
                )
                if inventory_result.ok:
                    cloud_inventory = {
                        path.strip().replace("\\", "/").lstrip("/")
                        for path in inventory_result.stdout.splitlines()
                        if path.strip()
                    }
                    logger.info(
                        f"[Queue Manager Recovery] Loaded one cloud inventory containing "
                        f"{len(cloud_inventory)} file(s)."
                    )
                else:
                    logger.warning(
                        "[Queue Manager Recovery] Cloud inventory unavailable; "
                        "cloud-only catalog rows will be preserved without demotion."
                    )

            def recovery_source_exists(catalog_path: str) -> Optional[bool]:
                if local_catalog_source_exists(catalog_path):
                    return True
                if settings.STORAGE_ENGINE != "CLOUD":
                    return False
                if cloud_inventory is None:
                    return None
                normalized = catalog_path.replace("\\", "/").lstrip("/")
                if normalized.startswith("media/"):
                    normalized = normalized[len("media/"):]
                return normalized in cloud_inventory
            
            # Sweep DB first to reconcile URLs and check actual file/cloud presence
            logger.info("[Queue Manager Recovery] Sweeping database to reconcile video URLs...")
            async with AsyncSession(engine, expire_on_commit=False) as db:
                # Reconcile Movies
                movie_stmt = select(Movie)
                movies = (await db.exec(movie_stmt)).all()
                for m in movies:
                    if m.video_url:
                        # Convert localhost URL to canonical path
                        if "localhost" in m.video_url or "127.0.0.1" in m.video_url:
                            match = re.search(r"/media/(.*)", m.video_url)
                            if match:
                                m.video_url = f"/media/{match.group(1)}"
                        
                        rel_path = m.video_url.lstrip("/")
                        exists = recovery_source_exists(m.video_url)
                        if exists is False:
                            logger.warning(f"[Sync Recovery] Movie {m.title} file missing at {rel_path}. Demoting to cached.")
                            m.video_url = ""
                            m.availability = "cached"
                            m.catalog_source = "tmdb_cache"
                            db.add(m)
                        elif exists is True:
                            m.catalog_source = "server"
                            m.availability = "available"
                            db.add(m)

                # Commit movie reconciliation before the episode query can
                # autoflush dirty rows. Episode existence checks may perform
                # slow cloud I/O and must never inherit a SQLite write lock.
                await db.commit()

                # Reconcile Episodes
                ep_stmt = select(Episode)
                episodes = (await db.exec(ep_stmt)).all()
                for ep in episodes:
                    if ep.video_url:
                        if "localhost" in ep.video_url or "127.0.0.1" in ep.video_url:
                            match = re.search(r"/media/(.*)", ep.video_url)
                            if match:
                                ep.video_url = f"/media/{match.group(1)}"
                        
                        rel_path = ep.video_url.lstrip("/")
                        exists = recovery_source_exists(ep.video_url)
                        if exists is False:
                            logger.warning(f"[Sync Recovery] Episode {ep.title} file missing at {rel_path}. Clearing video URL.")
                            ep.video_url = ""
                            db.add(ep)
                        elif exists is True:
                            db.add(ep)

                playable_series_ids = {episode.movie_id for episode in episodes if episode.video_url}
                for movie in movies:
                    if movie.type != "series":
                        continue
                    if movie.id in playable_series_ids:
                        movie.catalog_source = "server"
                        movie.availability = "available"
                    else:
                        movie.catalog_source = "tmdb_cache"
                        movie.availability = "cached"
                    db.add(movie)

                await db.commit()

            if not os.path.exists(media_dir):
                logger.info(f"[Queue Manager] No media directory found at {media_dir}. Skipping sync.")
                return

            logger.info("[Queue Manager] Starting disk-to-database recovery sync...")
            count = 0
            accepted_sources = {}
            accepted_cloud_fingerprints = {}
            snapshots = []
            async with AsyncSession(engine) as authority_db:
                rows = [*(await authority_db.exec(select(Episode))).all(), *(await authority_db.exec(select(Movie))).all()]
                for existing in rows:
                    try:
                        episode_row = isinstance(existing, Episode)
                        match = re.fullmatch(r"tv_([1-9][0-9]*)" if episode_row else r"m_([1-9][0-9]*)",
                            existing.movie_id if episode_row else existing.id)
                        source = local_path_for(existing.video_url)
                        if not match:
                            continue
                        owner = (("episode", int(match.group(1)), existing.season_number, existing.episode_number)
                            if episode_row else ("movie", int(match.group(1))))
                        snapshots.append((owner, source, _catalog_bundle(existing)))
                    except (OSError, ValueError, TypeError):
                        continue
            # Detached primitive snapshots keep provider observations outside
            # even the DB read session, before any catalog reconstruction writes.
            cloud_candidates = {}
            for owner, source, bundle in snapshots:
                try:
                    if source.is_file():
                        if (bundle["analysis"].get("video_fingerprint") == local_video_fingerprint(source)
                            or bundle["source_fingerprint"] == local_playback_fingerprint(source, bundle["audio_metadata"])):
                            accepted_sources[owner] = str(source)
                    elif settings.STORAGE_ENGINE == "CLOUD":
                        cloud_candidates[owner] = bundle
                except (OSError, ValueError, TypeError):
                    continue
            ambiguous_sources = {}
            preliminary_inventory = _recovery_metadata_inventory(media_dir, accepted_sources, ambiguous_sources=ambiguous_sources)
            cloud_records = {}
            observation_owners = set(ambiguous_sources)
            for root, names in preliminary_inventory:
                for name in names:
                    path = Path(root) / name
                    try:
                        portable = json.loads(path.read_text(encoding="utf-8"))
                        key = _portable_media_source(portable, path)
                    except (OSError, ValueError):
                        continue
                    if key is None or Path(key[1]).is_file() or settings.STORAGE_ENGINE != "CLOUD":
                        continue
                    cloud_records.setdefault(key[0], []).append((str(path), portable))
                    candidate = cloud_candidates.get(key[0])
                    if candidate is None or _cloud_bundle_differs(candidate, portable):
                        observation_owners.add(key[0])
            cloud_bundles = {}
            for owner in sorted(observation_owners):
                candidate = cloud_candidates.get(owner)
                records = cloud_records.get(owner, [])
                if candidate is None and owner in ambiguous_sources:
                    raise MediaRecoveryRequired("Ambiguous media recovery sources")
                if candidate is None and not records:
                    continue
                catalog_path = candidate["video_url"] if candidate else records[0][1]["video_url"]
                try:
                    observed = await asyncio.wait_for(resolve_media_source(catalog_path,
                        require_cloud=True, refresh_cloud=True), timeout=35)
                    available = not observed.local_exists and observed.cloud_exists and bool(observed.video_fingerprint)
                    current = bool(available and candidate and _current_cloud_bundle(candidate, observed))
                except (OSError, ValueError, TypeError, asyncio.TimeoutError):
                    available, current = False, False
                if not available or owner in ambiguous_sources and not current:
                    raise MediaRecoveryRequired("Ambiguous media recovery sources")
                if current:
                    accepted_sources[owner] = str(observed.local_path)
                for path, portable in records:
                    if local_path_for(portable["video_url"]) != observed.local_path:
                        if current:
                            continue
                        observed = await asyncio.wait_for(resolve_media_source(portable["video_url"],
                            require_cloud=True, refresh_cloud=True), timeout=35)
                        if observed.local_exists or not observed.cloud_exists or not observed.video_fingerprint:
                            raise MediaRecoveryRequired("Cloud recovery source could not be verified")
                    selected = _select_cloud_bundle(candidate, portable, observed)
                    cloud_bundles[(owner, str(observed.local_path))] = selected
                    if current:
                        accepted_cloud_fingerprints[owner] = selected["source_fingerprint"]
                    if any(portable.get(field) != selected[field] for field in _CLOUD_BUNDLE_FIELDS):
                        portable_current = _current_cloud_bundle(portable, observed, portable=True)
                        portable.update(selected)
                        portable["video_fingerprint"] = observed.video_fingerprint
                        primary = primary_embedded_audio_index(selected["audio_metadata"])
                        portable["language"] = selected["audio_metadata"][primary].get("language") if primary is not None else None
                        if not portable_current:
                            portable.pop("stream_manifest", None)
                        _write_recovery_metadata(path, portable)
            # Identical, nonambiguous existing bundles are left intact; this is
            # not a new source proof and does not require per-library cloud I/O.
            for owner, records in cloud_records.items():
                candidate = cloud_candidates.get(owner)
                if candidate is None:
                    continue
                for _, portable in records:
                    key = (owner, str(local_path_for(portable["video_url"])))
                    if key not in cloud_bundles and not _cloud_bundle_differs(candidate, portable):
                        cloud_bundles[key] = candidate
            metadata_inventory = _recovery_metadata_inventory(media_dir, accepted_sources)
            
            async with AsyncSession(engine) as db:
                for root, files in metadata_inventory:
                    for file in files:
                        if file.endswith(".json"):
                            meta_path = os.path.join(root, file)
                            try:
                                with open(meta_path, "r", encoding="utf-8") as f:
                                    data = json.load(f)
                                    
                                tmdb_id = data.get("tmdb_id")
                                media_type = data.get("media_type")
                                
                                if not tmdb_id or not media_type:
                                    continue
                                cloud_bundle = None
                                if data.get("video_url"):
                                    owner = _portable_owner(data, Path(meta_path))
                                    cloud_bundle = cloud_bundles.get((owner, str(local_path_for(data["video_url"]))))
                                    if cloud_bundle is not None:
                                        data.update(cloud_bundle)
                                    
                                parent_dir = os.path.dirname(root)
                                video_file_rel = None
                                
                                season = None
                                episode = None
                                if media_type == "series" or media_type == "tv":
                                    if data.get("season") is not None and data.get("episode") is not None:
                                        season = int(data.get("season"))
                                        episode = int(data.get("episode"))
                                    elif file.startswith("metadata_s") and "_e" in file:
                                        try:
                                            parts = file.replace("metadata_s", "").replace(".json", "").split("_e")
                                            season = int(parts[0])
                                            episode = int(parts[1])
                                        except Exception:
                                            pass
                                    if season is None or episode is None:
                                        try:
                                            root_parts = root.replace("\\", "/").split("/")
                                            if len(root_parts) >= 3 and root_parts[-3].startswith("Season_") and root_parts[-2].startswith("Episode_"):
                                                season = int(root_parts[-3].replace("Season_", ""))
                                                episode = int(root_parts[-2].replace("Episode_", ""))
                                        except Exception:
                                            pass
                                            
                                selected_source_changed = False
                                selection = _portable_media_source(data, Path(meta_path), accepted_sources)
                                if selection is not None:
                                    selected_path = Path(selection[1])
                                    if selected_path.is_file():
                                        video_file_rel = os.path.relpath(selected_path, server_root)
                                        selected_url = catalog_path_from_storage(str(selected_path))
                                        selected_source_changed = data.get("video_url") != selected_url
                                        data["video_url"] = selected_url

                                probe_info: Dict[str, Any] = {}
                                retained_subtitles = None
                                if video_file_rel:
                                    file_path = video_file_rel.replace("\\", "/")
                                    abs_video_path = os.path.abspath(os.path.join(server_root, file_path))
                                    portable_audio = data.get("audio_metadata")
                                    if not isinstance(portable_audio, list) or not all(isinstance(track, dict) for track in portable_audio):
                                        portable_audio = []
                                    existing_media = None
                                    if media_type == "movie":
                                        existing_media = await db.get(Movie, f"m_{tmdb_id}")
                                    elif season is not None and episode is not None:
                                        existing_media = await db.get(Episode, f"ep_{tmdb_id}_s{season}_e{episode}")
                                    db_audio = existing_media.audio_metadata if existing_media else []
                                    if not isinstance(db_audio, list) or not all(isinstance(track, dict) for track in db_audio):
                                        db_audio = []
                                    try:
                                        current_video_fingerprint = local_video_fingerprint(Path(abs_video_path))
                                        current_recovery_fingerprint = local_playback_fingerprint(
                                            Path(abs_video_path),
                                            portable_audio,
                                        )
                                        current_db_fingerprint = local_playback_fingerprint(Path(abs_video_path), db_audio)
                                    except OSError:
                                        current_video_fingerprint = ""
                                        current_recovery_fingerprint = ""
                                        current_db_fingerprint = ""
                                    # Each accepted bundle owns its own source proof. Portable
                                    # metadata cannot authorize stale or unbound catalog audio.
                                    retained_audio = []
                                    current_db_bundle = bool(existing_media and current_video_fingerprint and (
                                        stored_analysis(existing_media.analysis_metadata_str).get("video_fingerprint") == current_video_fingerprint
                                        or (current_db_fingerprint and existing_media.source_fingerprint == current_db_fingerprint)
                                    ))
                                    current_portable_bundle = bool(current_video_fingerprint and (
                                        data.get("video_fingerprint") == current_video_fingerprint
                                        or stored_analysis(data.get("analysis")).get("video_fingerprint") == current_video_fingerprint
                                        or (current_recovery_fingerprint and data.get("source_fingerprint") == current_recovery_fingerprint)
                                    ))
                                    retained_analysis = {}
                                    retained_mutables_changed = False
                                    if current_db_bundle:
                                        retained_audio.append(db_audio)
                                        retained_analysis = refreshed_analysis(None, existing_media.analysis_metadata_str)
                                        # Accepted server mutables on this unchanged video must
                                        # not regress when a legacy portable copy is stale.
                                        for field in ("skip_markers", "subtitles"):
                                            value = getattr(existing_media, field)
                                            retained_mutables_changed |= data.get(field) != value
                                            data[field] = value
                                    if current_portable_bundle:
                                        retained_audio.append(portable_audio)
                                        if "sender_quality" not in retained_analysis:
                                            retained_analysis = refreshed_analysis(None, data.get("analysis"))
                                    if current_db_bundle or current_portable_bundle:
                                        retained_subtitles = data.get("subtitles")
                                        if not isinstance(retained_subtitles, list):
                                            retained_subtitles = None
                                    manifest = data.get("stream_manifest")
                                    complete_portable_audio = isinstance(manifest, list) and bool(manifest) and all(isinstance(track, dict) for track in manifest)
                                    if complete_portable_audio:
                                        measured_indexes = {track.get("index") for track in manifest if track.get("type") == "audio"}
                                        portable_indexes = {track.get("streamIndex", track.get("stream_index")) for track in portable_audio
                                            if str(track.get("source") or "embedded").lower() == "embedded"}
                                        complete_portable_audio = measured_indexes == portable_indexes
                                    retained_portable_audio = retained_primary_audio_language(
                                        portable_audio, *retained_audio, same_video=bool(retained_audio))
                                    metadata_changed = retained_mutables_changed or selected_source_changed
                                    if (
                                        current_video_fingerprint
                                        and current_recovery_fingerprint
                                        and current_recovery_fingerprint == str(data.get("source_fingerprint") or "")
                                        and current_duration_analysis(data.get("analysis"))
                                        and complete_portable_audio
                                        and retained_portable_audio == portable_audio
                                    ):
                                        probe_info = data
                                        analysis = refreshed_analysis(data.get("analysis"), retained_analysis)
                                        if analysis != data.get("analysis"):
                                            data["analysis"] = analysis
                                            metadata_changed = True
                                        if current_video_fingerprint and data.get("video_fingerprint") != current_video_fingerprint:
                                            data["video_fingerprint"] = current_video_fingerprint
                                            metadata_changed = True
                                    else:
                                        stored_video_fingerprint = str(data.get("video_fingerprint") or "")
                                        if (
                                            stored_video_fingerprint
                                            and current_video_fingerprint
                                            and stored_video_fingerprint != current_video_fingerprint
                                        ):
                                            from services.audio_extractor import extract_audio_and_strip_video
                                            default_language = data.get("language") or data.get("original_language") or "en"
                                            extracted_langs = await extract_audio_and_strip_video(abs_video_path, default_lang=default_language)
                                            if extracted_langs:
                                                data["languages"] = extracted_langs

                                                if media_type == "movie":
                                                    movie_id = f"m_{tmdb_id}"
                                                    movie_obj = await db.get(Movie, movie_id)
                                                    if movie_obj:
                                                        movie_obj.languages = extracted_langs
                                                        db.add(movie_obj)
                                                        await db.commit()
                                                elif season is not None and episode is not None:
                                                    ep_id = f"ep_{tmdb_id}_s{season}_e{episode}"
                                                    ep_obj = await db.get(Episode, ep_id)
                                                    if ep_obj:
                                                        ep_obj.languages = extracted_langs
                                                        db.add(ep_obj)
                                                        await db.commit()

                                        probe_info = await probe_completed_media(abs_video_path)
                                        if probe_info:
                                            probe_info["audio_metadata"] = retained_primary_audio_language(
                                                probe_info.get("audio_metadata") or [], *retained_audio,
                                                same_video=bool(retained_audio) and probe_info.get("video_fingerprint") == current_video_fingerprint)
                                            if probe_info.get("video_fingerprint") == current_video_fingerprint:
                                                probe_info["audio_metadata"] = _retained_external_audio(probe_info["audio_metadata"], *retained_audio)
                                            data["languages"] = audio_languages(probe_info["audio_metadata"]) or data.get("languages") or []
                                            primary = primary_embedded_audio_index(probe_info["audio_metadata"])
                                            if primary is not None:
                                                data["language"] = probe_info["audio_metadata"][primary]["language"]
                                            probe_info["analysis"] = refreshed_analysis(probe_info.get("analysis"), retained_analysis)
                                            data.update(probe_info)
                                            metadata_changed = True
                                    if metadata_changed:
                                        try:
                                            _write_recovery_metadata(meta_path, data)
                                        except Exception:
                                            pass
                                else:
                                    file_path = None
                                        
                                language = data.get("language") or data.get("original_language")
                                
                                if media_type == "movie":
                                    movie_id = f"m_{tmdb_id}"
                                    existing = await db.get(Movie, movie_id)
                                    if existing and not (existing.title.startswith("Captured ") or "credentials are not set" in (existing.description or "")):
                                        if cloud_bundle is not None:
                                            _apply_catalog_bundle(existing, cloud_bundle)
                                        existing.crew = data.get("crew", existing.crew)
                                        existing.keywords = data.get("keywords", existing.keywords)
                                        existing.collection_name = data.get("collection_name", existing.collection_name)
                                        existing.trope_vectors = data.get("trope_vectors", existing.trope_vectors) or compute_trope_vectors(existing.genres, existing.keywords, existing.description)
                                        existing.dialogue_wpm = data.get("dialogue_wpm", existing.dialogue_wpm)
                                        existing.dialogue_word_count = int(data.get("dialogue_word_count", existing.dialogue_word_count) or 0)
                                        existing.dialogue_language = data.get("dialogue_language", existing.dialogue_language)
                                        existing.dialogue_confidence = float(data.get("dialogue_confidence", existing.dialogue_confidence) or 0.0)
                                        existing.vibe_analysis_status = data.get("vibe_analysis_status", existing.vibe_analysis_status)
                                        existing.vibe_analysis_version = int(data.get("vibe_analysis_version", existing.vibe_analysis_version) or 0)
                                        existing.vibe_analyzed_at = data.get("vibe_analyzed_at", existing.vibe_analyzed_at)
                                        if file_path:
                                            existing.video_url = catalog_path_from_storage(abs_video_path)
                                            existing.catalog_source = "server"
                                            existing.availability = "available"
                                            existing.preview_task_id = None
                                            db.add(existing)
                                        if file_path and probe_info:
                                            existing.probed_duration = probe_info.get("probed_duration")
                                            existing.duration = duration_label(existing.probed_duration)
                                            existing.container = probe_info.get("container")
                                            existing.codec = probe_info.get("codec")
                                            existing.width = probe_info.get("width")
                                            existing.height = probe_info.get("height")
                                            existing.frame_rate = probe_info.get("frame_rate")
                                            existing.quality = probe_info.get("quality") or existing.quality
                                            existing.analysis_metadata_str = json.dumps(refreshed_analysis(probe_info.get("analysis"), probe_info.get("analysis")))
                                            existing.source_fingerprint = probe_info.get("source_fingerprint")
                                            existing.audio_metadata = probe_info.get("audio_metadata", [])
                                            existing.languages = audio_languages(existing.audio_metadata) or existing.languages
                                            db.add(existing)
                                        await db.commit()
                                        continue
                                    logger.info(f"[Queue Manager Recovery] Restoring Movie TMDB {tmdb_id} from {meta_path}...")
                                    meta = await tmdb_client.fetch_movie_metadata(tmdb_id)
                                    
                                else: # series
                                    if season is not None and episode is not None:
                                        ep_id = f"ep_{tmdb_id}_s{season}_e{episode}"
                                        existing = await db.get(Episode, ep_id)
                                        if existing and not (existing.title.startswith("Episode ") or "stream." in (existing.description or "")):
                                            if cloud_bundle is not None:
                                                _apply_catalog_bundle(existing, cloud_bundle)
                                            existing.dialogue_wpm = data.get("dialogue_wpm", existing.dialogue_wpm)
                                            existing.dialogue_word_count = int(data.get("dialogue_word_count", existing.dialogue_word_count) or 0)
                                            existing.dialogue_language = data.get("dialogue_language", existing.dialogue_language)
                                            existing.dialogue_confidence = float(data.get("dialogue_confidence", existing.dialogue_confidence) or 0.0)
                                            existing.vibe_analysis_status = data.get("vibe_analysis_status", existing.vibe_analysis_status)
                                            existing.vibe_analysis_version = int(data.get("vibe_analysis_version", existing.vibe_analysis_version) or 0)
                                            existing.vibe_analyzed_at = data.get("vibe_analyzed_at", existing.vibe_analyzed_at)
                                            if file_path:
                                                existing.video_url = catalog_path_from_storage(abs_video_path)
                                                existing.preview_task_id = None
                                                show = await db.get(Movie, existing.movie_id)
                                                if show:
                                                    show.catalog_source = "server"
                                                    show.availability = "available"
                                                    db.add(show)
                                                db.add(existing)
                                            if file_path and probe_info:
                                                existing.probed_duration = probe_info.get("probed_duration")
                                                existing.duration = duration_label(existing.probed_duration)
                                                existing.container = probe_info.get("container")
                                                existing.codec = probe_info.get("codec")
                                                existing.width = probe_info.get("width")
                                                existing.height = probe_info.get("height")
                                                existing.frame_rate = probe_info.get("frame_rate")
                                                existing.quality = probe_info.get("quality") or existing.quality
                                                existing.analysis_metadata_str = json.dumps(refreshed_analysis(probe_info.get("analysis"), probe_info.get("analysis")))
                                                existing.source_fingerprint = probe_info.get("source_fingerprint")
                                                existing.audio_metadata = probe_info.get("audio_metadata", [])
                                                existing.languages = audio_languages(existing.audio_metadata) or existing.languages
                                                db.add(existing)
                                            await db.commit()
                                            continue
                                        logger.info(f"[Queue Manager Recovery] Restoring Episode S{season}E{episode} (TMDB {tmdb_id}) from {meta_path}...")
                                        meta = await tmdb_client.fetch_show_metadata(tmdb_id)
                                        ep_meta = await tmdb_client.fetch_episode_metadata(tmdb_id, season, episode)
                                        meta["episode_detail"] = ep_meta
                                    else:
                                        show_id = f"tv_{tmdb_id}"
                                        existing = await db.get(Movie, show_id)
                                        if existing and not (existing.title.startswith("Captured ") or "credentials are not set" in (existing.description or "")):
                                            await db.rollback()
                                            continue
                                        logger.info(f"[Queue Manager Recovery] Restoring Series TMDB {tmdb_id} from {meta_path}...")
                                        meta = await tmdb_client.fetch_show_metadata(tmdb_id)
                                
                                cloud_video_url = data.get("video_url")
                                if (
                                    not file_path
                                    and isinstance(cloud_video_url, str)
                                    and cloud_video_url.startswith("/media/")
                                    and recovery_source_exists(cloud_video_url) is True
                                ):
                                    owner = _portable_owner(data, Path(meta_path))
                                    await self._restore_cloud_catalog_entry(db, data, meta, season, episode,
                                        verified_source_fingerprint=(cloud_bundle["source_fingerprint"] if cloud_bundle
                                            else accepted_cloud_fingerprints.get(owner)), selected_bundle=cloud_bundle)
                                    await db.commit()
                                    count += 1
                                    continue

                                if not file_path:
                                    data["catalog_source"] = "tmdb_cache"
                                    data["availability"] = "cached"
                                    data["video_url"] = ""
                                    try:
                                        _atomic_write_json(meta_path, data)
                                    except Exception as metadata_err:
                                        logger.error(f"[Queue Manager] Failed to annotate cached metadata {meta_path}: {metadata_err}")
                                    cached_id = f"m_{tmdb_id}" if media_type == "movie" else f"tv_{tmdb_id}"
                                    cached = await db.get(Movie, cached_id)
                                    if not cached:
                                        relative_meta = os.path.relpath(meta_path, media_dir).replace("\\", "/").split("/")
                                        library = "Movies" if media_type == "movie" else "Series"
                                        folder = relative_meta[1] if len(relative_meta) > 1 else ""
                                        base_url = f"/media/{library}/{folder}" if folder else ""
                                        cached = Movie(
                                            id=cached_id,
                                            tmdb_id=int(tmdb_id),
                                            title=meta.get("title", data.get("title", f"TMDB {tmdb_id}")),
                                            description=meta.get("description", data.get("description", "")),
                                            thumbnail_url=f"{base_url}/poster.jpg" if base_url else meta.get("thumbnailUrl", ""),
                                            banner_url=f"{base_url}/backdrop.jpg" if base_url else meta.get("bannerUrl", ""),
                                            video_url="",
                                            duration=meta.get("duration", "45m" if media_type != "movie" else "2h"),
                                            release_year=int(meta.get("releaseYear", data.get("release_year", 0)) or 0),
                                            rating=meta.get("rating"),
                                            director=meta.get("director"),
                                            type="movie" if media_type == "movie" else "series",
                                            vote_average=float(meta.get("vote_average", 0.0) or 0.0),
                                            vote_count=int(meta.get("vote_count", 0) or 0),
                                            catalog_source="tmdb_cache",
                                            availability="cached",
                                            cached_at=time.time(),
                                            metadata_refreshed_at=time.time(),
                                        )
                                        cached.genres = meta.get("genres", data.get("genres", []))
                                        cached.cast = meta.get("cast", data.get("cast", []))
                                        cached.crew = meta.get("crew", data.get("crew", []))
                                        cached.keywords = meta.get("keywords", data.get("keywords", []))
                                        cached.collection_name = meta.get("collectionName") or data.get("collection_name")
                                        cached.trope_vectors = meta.get("tropeVectors") or data.get("trope_vectors") or compute_trope_vectors(cached.genres, cached.keywords, cached.description)
                                    elif cached.availability != "available":
                                        cached.catalog_source = "tmdb_cache"
                                        cached.availability = "cached"
                                    db.add(cached)
                                    await db.commit()
                                    count += 1
                                    continue

                                # Re-run cataloging with physical video.
                                await self._catalog_media(
                                    db, tmdb_id, media_type, season, episode, meta, file_path,
                                    subtitles_list=data.get("subtitles"),
                                    retained_audio_metadata=data.get("audio_metadata"),
                                    retained_video_fingerprint=data.get("video_fingerprint"),
                                    retained_subtitle_metadata=retained_subtitles,
                                    quality=refreshed_analysis(None, data.get("analysis")).get("sender_quality"), skip_markers=data.get("skip_markers")
                                )
                                # _catalog_media intentionally flushes without
                                # committing so ingestion can publish atomically.
                                # Recovery owns a separate entry transaction and
                                # must release its write lock before probing the
                                # next media file.
                                await db.commit()
                                count += 1
                                
                            except Exception as e:
                                await db.rollback()
                                logger.error(f"[Queue Manager] Error recovering {meta_path}: {e}")
                                
            reconciled_tasks = await self._reconcile_post_commit_failures()
            logger.info(
                f"[Queue Manager] Disk sync complete. Recovered {count} entries and "
                f"reconciled {reconciled_tasks} published task(s)."
            )
        except Exception as e:
            logger.error(f"[Queue Manager] Critical failure during sync_media_from_disk: {e}")
            if raise_on_critical_failure:
                raise

queue_manager = DownloadQueueManager()
