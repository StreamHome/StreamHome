import asyncio
import time
import json
import os
import shutil
import subprocess
import math
from fractions import Fraction
from datetime import datetime
from pathlib import Path
from typing import Optional, Type

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import config_dir, settings
from db import engine
from models import Episode, Movie, PlaybackSession
from lifecycle.media_work import media_ownership, settle, stop_process, accepted_commit, supervised_output
from lifecycle.media_publication import MediaMutation, current_mutation
from services.ffmpeg_input import local_media_input_options, native_media_environment
from services.logger import logger
from services.media_probe import probe_completed_media
from services.media_analysis import duration_label, refreshed_analysis
from services.media_analysis import stored_analysis
from services.audio_extractor import audio_languages, primary_embedded_audio_index, retained_primary_audio_language
from services.media_source import MediaSourceError, resolve_media_source
from services.state import register_process, unregister_process
from lifecycle.coordination import AdmissionClosedError, EffectClass, WorkLease, runtime_work


class HEVCActivityInterrupted(RuntimeError):
    """Resume optimization after foreground playback or maintenance admission."""


class HEVCCompressorWorker:
    # Optimization must finish within a day and retain the ingestion volume's
    # emergency free-space margin even when another writer consumes the volume.
    MAX_TRANSCODE_SECONDS = 24 * 3600
    STORAGE_SAFETY_MARGIN_BYTES = 2 * 1024**3

    def __init__(self):
        self.loop_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.active_process: Optional[asyncio.subprocess.Process] = None

    def start(self) -> None:
        if not self.is_running:
            self.is_running = True
            self.loop_task = asyncio.create_task(self._worker_loop(), name="hevc-compressor")
            logger.info("[HEVC Compressor] Background worker loop started.")

    async def stop(self) -> None:
        self.is_running = False
        if self.active_process and self.active_process.returncode is None:
            self.active_process.kill()
            await asyncio.gather(self.active_process.wait(), return_exceptions=True)
        self.active_process = None
        if self.loop_task:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
            self.loop_task = None
        logger.info("[HEVC Compressor] Background worker loop stopped.")

    def _get_cpu_cores(self) -> int:
        try:
            profile_path = os.path.join(config_dir, "system_profile.json")
            if os.path.exists(profile_path):
                with open(profile_path, "r", encoding="utf-8") as profile_file:
                    return max(1, int(json.load(profile_file).get("cpu_cores", 2)))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return os.cpu_count() or 2

    async def _is_server_idle(self) -> bool:
        try:
            async with AsyncSession(engine) as db:
                result = await db.exec(select(PlaybackSession).order_by(PlaybackSession.updated_at.desc()).limit(1))
                last_session = result.first()
                if last_session and last_session.updated_at:
                    last_updated = datetime.fromisoformat(last_session.updated_at.replace("Z", "+00:00"))
                    now = datetime.now(last_updated.tzinfo) if last_updated.tzinfo else datetime.utcnow()
                    return (now - last_updated).total_seconds() >= 15 * 60
        except Exception as error:
            logger.warning(f"[HEVC Compressor] Idle-state check failed: {type(error).__name__}")
        return True

    async def _next_candidate(self) -> tuple[Optional[Type[Movie] | Type[Episode]], Optional[str], Optional[str]]:
        deferred = getattr(self, "deferred_candidates", {})
        now = time.monotonic()
        async with AsyncSession(engine) as db:
            for model in (Movie, Episode):
                query = select(model).where(model.hevc_compressed == False, model.video_url.startswith("/media/"))
                if model is Movie:
                    query = query.where(Movie.availability == "available")
                candidates = (await db.exec(query.order_by(model.id))).all()
                for item in candidates:
                    if deferred.get(item.id, 0) > now:
                        continue
                    source = await resolve_media_source(item.video_url, check_cloud=False)
                    if source.local_exists:
                        deferred[item.id] = now + 3600
                        self.deferred_candidates = deferred
                        return model, item.id, item.video_url
        return None, None, None

    async def _mark_complete(
        self,
        model: Type[Movie] | Type[Episode],
        item_id: str,
        probe: Optional[dict] = None,
        *,
        file_path: Optional[Path] = None,
        original_probe: Optional[dict] = None,
    ) -> None:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            item = await db.get(model, item_id)
            if not item:
                return
            previous_fingerprint = str(item.source_fingerprint or "")
            metadata_path = file_path.parent / ".metadata" / "metadata.json" if file_path is not None else None
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path and metadata_path.exists() else {}
            item.hevc_compressed = True
            if probe:
                original = original_probe or probe
                video_fingerprint = original.get("video_fingerprint")
                retained_audio = []
                retained_analysis = {}
                if video_fingerprint and (
                    stored_analysis(item.analysis_metadata_str).get("video_fingerprint") == video_fingerprint
                    or previous_fingerprint == original.get("source_fingerprint")
                ):
                    retained_audio.append(item.audio_metadata or [])
                    retained_analysis = refreshed_analysis(None, item.analysis_metadata_str)
                if video_fingerprint and (
                    metadata.get("video_fingerprint") == video_fingerprint
                    or metadata.get("source_fingerprint") == original.get("source_fingerprint")
                ):
                    retained_audio.append(metadata.get("audio_metadata") or [])
                    if "sender_quality" not in retained_analysis:
                        retained_analysis = refreshed_analysis(None, metadata.get("analysis"))
                audio = retained_primary_audio_language(list(probe.get("audio_metadata") or []),
                    *retained_audio, same_video=bool(retained_audio))
                item.codec = probe.get("codec") or item.codec
                item.container = probe.get("container") or item.container
                item.probed_duration = probe.get("probed_duration") or item.probed_duration
                item.duration = duration_label(item.probed_duration)
                item.width = probe.get("width") or item.width
                item.height = probe.get("height") or item.height
                item.frame_rate = probe.get("frame_rate") or item.frame_rate
                item.source_fingerprint = probe.get("source_fingerprint") or item.source_fingerprint
                item.audio_metadata = audio
                item.languages = audio_languages(audio) or item.languages
                primary = primary_embedded_audio_index(audio)
                if primary is not None:
                    metadata["language"] = audio[primary]["language"]
                    if audio[primary].get("languageOverride", {}).get("source") == "sender":
                        metadata["original_language"] = audio[primary]["language"]
                        if isinstance(item, Movie):
                            item.original_language = audio[primary]["language"]
                item.quality = probe.get("quality") or item.quality
                item.analysis_metadata_str = json.dumps(refreshed_analysis(probe.get("analysis"), retained_analysis))
                if file_path is not None:
                    from services.media_updates import metadata_payload, persist_portable_metadata, temporary_update_directory
                    if metadata_path.exists():
                        analysis = json.loads(item.analysis_metadata_str)
                        mutation = current_mutation.get()
                        if mutation is None:
                            raise RuntimeError("Portable HEVC measurements require a media operation")
                        metadata.update(metadata_payload(item))
                        metadata["analysis"] = analysis
                        for key in ("quality", "probed_duration", "container", "codec", "width", "height", "frame_rate", "source_fingerprint", "video_fingerprint", "audio_metadata", "stream_manifest"):
                            metadata[key] = probe.get(key)
                        metadata["audio_metadata"] = audio
                        metadata["languages"] = item.languages
                        metadata["duration"] = item.duration
                        source = await resolve_media_source(item.video_url, check_cloud=False)
                        with temporary_update_directory() as directory:
                            # Optimization publishes only the local video. Its
                            # aliases cannot attest to unchanged cloud bytes.
                            await persist_portable_metadata(source, metadata, Path(directory), publish_cloud=False)
            next_fingerprint = str(item.source_fingerprint or "")
            db.add(item)
            mutation = current_mutation.get()
            if mutation is not None:
                await mutation.commit(db)
            else:
                await accepted_commit(db)
        if previous_fingerprint and next_fingerprint and previous_fingerprint != next_fingerprint:
            from services.playback_prep import playback_prep_service
            try:
                reused = playback_prep_service.reuse_verified_playback_cache(item.id, previous_fingerprint, next_fingerprint, item)
                if reused:
                    await playback_prep_service.rebuild_master(item.id, next_fingerprint, item)
            except Exception as error:
                logger.warning("[HEVC Compressor] Accepted optimization cache refresh deferred: %s", type(error).__name__)

    @staticmethod
    def _valid_transcode(original_probe: dict, transcode_probe: dict, original_size: int, transcode_size: int) -> bool:
        def finite(value):
            try:
                number = float(value)
                return number if math.isfinite(number) and not isinstance(value, bool) else None
            except (TypeError, ValueError):
                return None

        def tick(stream):
            try:
                value = float(Fraction(str(stream.get("timeBase") or "0")))
                return value if math.isfinite(value) and value > 0 else 0.0
            except (ValueError, ZeroDivisionError):
                return 0.0

        if transcode_size < 1024 or transcode_size >= original_size:
            return False
        if original_probe.get("valid_video") is False or transcode_probe.get("valid_video") is False:
            return False
        if str(transcode_probe.get("codec") or "").lower() not in {"hevc", "h265"}:
            return False
        original_duration = finite(original_probe.get("probed_duration"))
        transcode_duration = finite(transcode_probe.get("probed_duration"))
        if original_duration is None or transcode_duration is None or original_duration <= 0 or transcode_duration <= 0:
            return False

        original_manifest = list(original_probe.get("stream_manifest") or [])
        transcode_manifest = list(transcode_probe.get("stream_manifest") or [])
        if not original_manifest or len(original_manifest) != len(transcode_manifest):
            return False
        primary = next((index for index, stream in enumerate(original_manifest) if stream.get("type") == "video"), None)
        if primary is None or transcode_manifest[primary].get("type") != "video":
            return False
        original_master, transcode_master = original_manifest[primary], transcode_manifest[primary]
        original_start = finite(original_master.get("startTime"))
        transcode_start = finite(transcode_master.get("startTime"))
        if (original_start is None) != (transcode_start is None):
            return False
        master_tick = max(tick(original_master), tick(transcode_master))
        if original_start is not None and abs(original_start - transcode_start) > 2 * master_tick + 1e-9:
            return False
        # Selected-video duration may include one boundary frame at each side of the
        # conversion. Bound that quantization; never accept a percent-of-film loss.
        original_rate = finite(original_probe.get("frame_rate")) or 0.0
        transcode_rate = finite(transcode_probe.get("frame_rate")) or 0.0
        frame_budget = sum(1 / rate for rate in (original_rate, transcode_rate) if rate > 0)
        duration_tolerance = max(frame_budget, 2 * master_tick)
        if abs(original_duration - transcode_duration) > duration_tolerance + 1e-9:
            return False
        for index, (original, transcoded) in enumerate(zip(original_manifest, transcode_manifest)):
            identity_fields = ("type", "index", "language", "title", "default", "disposition")
            if any(original.get(field) != transcoded.get(field) for field in identity_fields):
                return False
            if index != primary and original.get("codec") != transcoded.get("codec"):
                return False
            before_duration, after_duration = finite(original.get("duration")), finite(transcoded.get("duration"))
            if (before_duration is None) != (after_duration is None):
                return False
            if before_duration is not None:
                stream_budget = max(frame_budget, 2 * max(master_tick, tick(original), tick(transcoded)))
                if before_duration < 0 or after_duration < 0 or abs(before_duration - after_duration) > stream_budget + 1e-9:
                    return False
            before, after = finite(original.get("startTime")), finite(transcoded.get("startTime"))
            if (original.get("startTime") is not None and before is None) or (transcoded.get("startTime") is not None and after is None):
                return False
            if (before is None) != (after is None):
                return False
            if before is not None:
                if original_start is None:
                    return False
                offset_delta = (before - original_start) - (after - transcode_start)
                if abs(offset_delta) > 2 * max(master_tick, tick(original), tick(transcoded)) + 1e-9:
                    return False
        return True

    async def _supervise_transcode(self, process, output: Path, original_size: int, lease: WorkLease) -> int:
        """Stop an ineligible child before relinquishing process/media ownership."""
        async def watch_runtime():
            next_idle_check = 0.0
            while process.returncode is None:
                if lease.cancellation_requested:
                    raise HEVCActivityInterrupted("Maintenance requested optimization cancellation")
                if shutil.disk_usage(output.parent).free <= self.STORAGE_SAFETY_MARGIN_BYTES:
                    raise ValueError("HEVC output reached the media storage safety floor")
                now = time.monotonic()
                if now >= next_idle_check:
                    if not await self._is_server_idle():
                        raise HEVCActivityInterrupted("Foreground playback interrupted optimization")
                    next_idle_check = now + 15.0
                await asyncio.sleep(1.0)

        output_watch = asyncio.create_task(supervised_output(
            process, timeout=self.MAX_TRANSCODE_SECONDS, output_path=output,
            # Every result at least as large as its source is categorically
            # rejected by final validation. Do not encode an already-lost job.
            maximum_bytes=original_size - 1,
        ))
        runtime_watch = asyncio.create_task(watch_runtime())
        watchers = (output_watch, runtime_watch)
        try:
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            await output_watch
            return process.returncode
        finally:
            for task in watchers:
                if not task.done():
                    task.cancel()
            await settle(asyncio.gather(*watchers, return_exceptions=True))
            if process.returncode is None:
                await settle(stop_process(process))

    async def _worker_loop(self) -> None:
        ffmpeg_path = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
        while self.is_running:
            temporary_file: Optional[Path] = None
            rollback_file: Optional[Path] = None
            work_scope = None
            ownership = None
            mutation = None
            active_lease: Optional[WorkLease] = None
            activity_cooldown = False
            try:
                await asyncio.sleep(60.0)
                mode = getattr(settings, "HEVC_COMPRESSION_MODE", "auto")
                if mode == "off" or (mode == "auto" and self._get_cpu_cores() < 4):
                    continue
                if not await self._is_server_idle():
                    continue

                model, item_id, catalog_path = await self._next_candidate()
                if not model or not item_id or not catalog_path:
                    continue
                try:
                    source = await resolve_media_source(catalog_path, check_cloud=False)
                except MediaSourceError as error:
                    logger.warning(f"[HEVC Compressor] Invalid catalog path for {item_id}: {error}")
                    continue
                if not source.local_exists:
                    continue

                work_scope = runtime_work(
                    "hevc.compressor",
                    [
                        EffectClass.DB_WRITE,
                        EffectClass.DURABLE_MEDIA_WRITE,
                        EffectClass.DISPOSABLE_CACHE_WRITE,
                    ],
                )
                try:
                    active_lease = await work_scope.__aenter__()
                except AdmissionClosedError:
                    work_scope = None
                    continue

                ownership = media_ownership(item_id)
                await ownership.__aenter__()
                source = await resolve_media_source(catalog_path, check_cloud=False)
                if not source.local_exists:
                    continue
                file_path = source.local_path
                original_probe = await probe_completed_media(str(file_path))
                if original_probe.get("valid_video") is False or not original_probe.get("probed_duration"):
                    logger.warning(f"[HEVC Compressor] Refusing to transcode unprobeable source: {file_path}")
                    continue
                if str(original_probe.get("codec") or "").lower() in {"hevc", "h265"}:
                    mutation = MediaMutation(item_id)
                    await mutation.__aenter__()
                    await self._mark_complete(model, item_id, original_probe, file_path=file_path)
                    continue

                temporary_file = file_path.with_name(f".{file_path.stem}.hevc-part{file_path.suffix}")
                temporary_file.unlink(missing_ok=True)
                original_size = file_path.stat().st_size
                command = [
                    ffmpeg_path,
                    "-y",
                    "-v",
                    "error",
                    "-copyts",
                    *local_media_input_options(),
                    "-i",
                    str(file_path),
                    "-map",
                    "0",
                    "-map_metadata",
                    "0",
                    "-map_chapters",
                    "0",
                    "-c",
                    "copy",
                    "-c:v:0",
                    "libx265",
                    "-preset",
                    "medium",
                    "-crf",
                    "28",
                    "-threads",
                    "2",
                    "-enc_time_base:v:0",
                    "demux",
                    "-avoid_negative_ts",
                    "disabled",
                    str(temporary_file),
                ]
                # FFmpeg otherwise auto-adds a default stream when the input has
                # none. Preserve every disposition, including forced captions.
                dispositions = []
                for index, stream in enumerate(original_probe.get("stream_manifest") or []):
                    flags = stream.get("disposition") or {"default": int(bool(stream.get("default")))}
                    enabled = "+".join(name for name, value in flags.items() if value)
                    dispositions.extend([f"-disposition:{index}", enabled or "0"])
                command[-1:-1] = dispositions
                logger.info(f"[HEVC Compressor] Starting verified transcode for: {file_path}")
                self.active_process = await asyncio.create_subprocess_exec(
                    *command,
                    env=native_media_environment(),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                process_key = f"hevc:{item_id}"
                register_process(process_key, self.active_process)

                try:
                    return_code = await self._supervise_transcode(self.active_process, temporary_file, original_size, active_lease)
                finally:
                    if self.active_process is not None and self.active_process.returncode is None:
                        await settle(stop_process(self.active_process))
                    unregister_process(process_key)
                    self.active_process = None
                if return_code != 0 or not temporary_file.exists():
                    logger.error(f"[HEVC Compressor] FFmpeg failed for {file_path} with exit code {return_code}.")
                    continue

                transcode_probe = await probe_completed_media(str(temporary_file))
                if not self._valid_transcode(
                    original_probe,
                    transcode_probe,
                    file_path.stat().st_size,
                    temporary_file.stat().st_size,
                ):
                    logger.warning(f"[HEVC Compressor] Validation rejected the transcode for {file_path}; original preserved.")
                    continue

                mutation = MediaMutation(item_id)
                await mutation.__aenter__()
                _, cancelled = await settle(asyncio.to_thread(mutation.capture_local, str(file_path)))
                if cancelled:
                    raise asyncio.CancelledError
                os.replace(temporary_file, file_path)
                from services.media_updates import synchronize_local_asset
                synchronize_local_asset(file_path)
                final_probe = await probe_completed_media(str(file_path))
                original_size = next((mutation.root / e["backup"]).stat().st_size for e in mutation.entries if e["kind"] == "local" and e["path"] == str(file_path))
                if not self._valid_transcode(original_probe, final_probe, original_size, file_path.stat().st_size):
                    raise ValueError("Published HEVC file failed final stream-preservation validation")
                await self._mark_complete(model, item_id, final_probe, file_path=file_path, original_probe=original_probe)
                logger.info(f"[HEVC Compressor] Verified and replaced {file_path}.")
            except asyncio.CancelledError:
                if self.active_process and self.active_process.returncode is None:
                    self.active_process.kill()
                    await asyncio.gather(self.active_process.wait(), return_exceptions=True)
                self.active_process = None
                raise
            except HEVCActivityInterrupted as error:
                activity_cooldown = True
                logger.info("[HEVC Compressor] Optimization deferred: %s", str(error))
            except Exception as error:
                self.active_process = None
                logger.error(f"[HEVC Compressor] Worker error: {type(error).__name__}: {error}")
            finally:
                if temporary_file:
                    temporary_file.unlink(missing_ok=True)
                try:
                    if mutation is not None:
                        await mutation.__aexit__(None, None, None)
                except Exception as error:
                    logger.error("[HEVC Compressor] Original evidence retained for recovery: %s", type(error).__name__)
                finally:
                    if ownership is not None:
                        await ownership.__aexit__(None, None, None)
                    if work_scope is not None:
                        await work_scope.__aexit__(None, None, None)
            if activity_cooldown:
                # Backoff owns neither a child, a media lock nor a writer lease.
                await asyncio.sleep(5 * 60)


hevc_compressor = HEVCCompressorWorker()
