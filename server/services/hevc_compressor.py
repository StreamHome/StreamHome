import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Type

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import config_dir, settings
from db import engine
from models import Episode, Movie, PlaybackSession
from services.logger import logger
from services.media_probe import probe_completed_media
from services.media_source import MediaSourceError, resolve_media_source


class HEVCCompressorWorker:
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
        async with AsyncSession(engine) as db:
            movie = (
                await db.exec(
                    select(Movie).where(
                        Movie.hevc_compressed == False,  # noqa: E712
                        Movie.video_url.startswith("/media/"),
                        Movie.availability == "available",
                    ).limit(1)
                )
            ).first()
            if movie:
                return Movie, movie.id, movie.video_url
            episode = (
                await db.exec(
                    select(Episode).where(
                        Episode.hevc_compressed == False,  # noqa: E712
                        Episode.video_url.startswith("/media/"),
                    ).limit(1)
                )
            ).first()
            if episode:
                return Episode, episode.id, episode.video_url
        return None, None, None

    async def _mark_complete(
        self,
        model: Type[Movie] | Type[Episode],
        item_id: str,
        probe: Optional[dict] = None,
    ) -> None:
        async with AsyncSession(engine) as db:
            item = await db.get(model, item_id)
            if not item:
                return
            previous_fingerprint = str(item.source_fingerprint or "")
            item.hevc_compressed = True
            if probe:
                item.codec = probe.get("codec") or item.codec
                item.container = probe.get("container") or item.container
                item.probed_duration = probe.get("probed_duration") or item.probed_duration
                item.width = probe.get("width") or item.width
                item.height = probe.get("height") or item.height
                item.frame_rate = probe.get("frame_rate") or item.frame_rate
                item.source_fingerprint = probe.get("source_fingerprint") or item.source_fingerprint
                item.audio_metadata = probe.get("audio_metadata") or item.audio_metadata
            next_fingerprint = str(item.source_fingerprint or "")
            if previous_fingerprint and next_fingerprint and previous_fingerprint != next_fingerprint:
                from services.playback_prep import playback_prep_service

                reused = playback_prep_service.reuse_verified_playback_cache(
                    item.id,
                    previous_fingerprint,
                    next_fingerprint,
                    item,
                )
                if reused:
                    await playback_prep_service.rebuild_master(item.id, next_fingerprint, item)
                    logger.info(
                        f"[HEVC Compressor] Preserved {len(reused)} verified HLS rendition(s) "
                        f"for {item.id} across source optimization."
                    )
            db.add(item)
            await db.commit()

    @staticmethod
    def _valid_transcode(original_probe: dict, transcode_probe: dict, original_size: int, transcode_size: int) -> bool:
        if transcode_size < 1024 or transcode_size >= original_size:
            return False
        if str(transcode_probe.get("codec") or "").lower() not in {"hevc", "h265"}:
            return False
        original_duration = float(original_probe.get("probed_duration") or 0)
        transcode_duration = float(transcode_probe.get("probed_duration") or 0)
        if original_duration <= 0 or transcode_duration <= 0:
            return False
        tolerance = max(5.0, original_duration * 0.05)
        return abs(original_duration - transcode_duration) <= tolerance

    async def _worker_loop(self) -> None:
        ffmpeg_path = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
        while self.is_running:
            temporary_file: Optional[Path] = None
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

                file_path = source.local_path
                original_probe = await probe_completed_media(str(file_path))
                if str(original_probe.get("codec") or "").lower() in {"hevc", "h265"}:
                    await self._mark_complete(model, item_id, original_probe)
                    continue
                if not original_probe.get("probed_duration"):
                    logger.warning(f"[HEVC Compressor] Refusing to transcode unprobeable source: {file_path}")
                    continue

                temporary_file = file_path.with_name(f".{file_path.stem}.hevc-part.mp4")
                temporary_file.unlink(missing_ok=True)
                command = [
                    ffmpeg_path,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(file_path),
                    "-c:v",
                    "libx265",
                    "-preset",
                    "medium",
                    "-crf",
                    "28",
                    "-c:a",
                    "copy",
                    "-threads",
                    "2",
                    str(temporary_file),
                ]
                logger.info(f"[HEVC Compressor] Starting verified transcode for: {file_path}")
                self.active_process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )

                killed_for_activity = False
                while self.active_process.returncode is None:
                    await asyncio.sleep(15.0)
                    if not await self._is_server_idle():
                        self.active_process.kill()
                        killed_for_activity = True
                        break
                return_code = await self.active_process.wait()
                self.active_process = None
                if killed_for_activity:
                    await asyncio.sleep(5 * 60)
                    continue
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

                os.replace(temporary_file, file_path)
                final_probe = await probe_completed_media(str(file_path))
                await self._mark_complete(model, item_id, final_probe)
                logger.info(f"[HEVC Compressor] Verified and replaced {file_path}.")
            except asyncio.CancelledError:
                if self.active_process and self.active_process.returncode is None:
                    self.active_process.kill()
                    await asyncio.gather(self.active_process.wait(), return_exceptions=True)
                self.active_process = None
                raise
            except Exception as error:
                self.active_process = None
                logger.error(f"[HEVC Compressor] Worker error: {type(error).__name__}: {error}")
            finally:
                if temporary_file:
                    temporary_file.unlink(missing_ok=True)


hevc_compressor = HEVCCompressorWorker()
