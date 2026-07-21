from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from config import settings
from services.logger import logger
from services.media_source import ResolvedMediaSource, resolve_media_source
from services.rclone import rclone_service


STANDARD_HEIGHTS = (1080, 720, 480, 360, 240)
PLAYLIST_NAME = "playlist.m3u8"
MASTER_NAME = "master.m3u8"


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


class PlaybackPreparationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PlaybackPrepService:
    def __init__(self) -> None:
        self.cache_dir = Path(settings.TEMP_DIR) / "playback_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.active_jobs: dict[str, asyncio.Task[None]] = {}
        self.semaphore = asyncio.Semaphore(max(1, settings.PLAYBACK_TRANSCODE_CONCURRENCY))
        self._master_locks: dict[str, asyncio.Lock] = {}

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
                label="Original",
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
            language = str(item.get("language") or "und").lower()
            label = str(item.get("label") or language.upper())
            renditions.append(
                AudioRendition(
                    name=f"audio_{stream_index}_{self._audio_slug(language)}",
                    label=label,
                    language=language,
                    stream_index=stream_index,
                    default=stream_index == default_index,
                )
            )
        return renditions

    def baseline_video(self, media_obj: Any) -> VideoRendition:
        renditions = self.video_renditions(media_obj)
        return next((item for item in renditions if item.height == 720), renditions[0])

    def playlist_ready(self, media_id: str, fingerprint: str, rendition_name: str) -> bool:
        return (self.cache_path(media_id, fingerprint) / rendition_name / PLAYLIST_NAME).is_file()

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
        video_ready = self.playlist_ready(media_id, fingerprint, baseline.name)
        audio_ready = default_audio is None or self.playlist_ready(media_id, fingerprint, default_audio.name)
        if video_ready and audio_ready and (self.cache_path(media_id, fingerprint) / MASTER_NAME).is_file():
            return "ready"
        return "error" if self.preparation_error(media_id, fingerprint) else "preparing"

    async def prepare(
        self,
        media_id: str,
        media_obj: Any,
        source: ResolvedMediaSource,
        *,
        include_remaining: bool,
    ) -> str:
        fingerprint = getattr(media_obj, "source_fingerprint", None) or source.fingerprint
        cache_path = self.cache_path(media_id, fingerprint)
        cache_path.mkdir(parents=True, exist_ok=True)
        self.touch(media_id, fingerprint)

        baseline = self.baseline_video(media_obj)
        audios = self.audio_renditions(media_obj)
        default_audio = next((item for item in audios if item.default), audios[0] if audios else None)
        self._schedule_video(media_id, fingerprint, source, baseline, media_obj)
        if default_audio:
            self._schedule_audio(media_id, fingerprint, source, default_audio, media_obj)

        if include_remaining:
            self._schedule_remaining(media_id, fingerprint, source, media_obj)
        await self.rebuild_master(media_id, fingerprint, media_obj)
        return self.preparation_state(media_id, fingerprint, media_obj)

    def _schedule_remaining(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        media_obj: Any,
    ) -> None:
        key = f"{media_id}:{fingerprint}:remaining"
        if key in self.active_jobs:
            return
        task = asyncio.create_task(self._schedule_remaining_after_baseline(media_id, fingerprint, source, media_obj))
        self.active_jobs[key] = task
        task.add_done_callback(lambda _: self.active_jobs.pop(key, None))

    async def _schedule_remaining_after_baseline(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        media_obj: Any,
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
        for rendition in self.video_renditions(media_obj):
            self._schedule_video(media_id, fingerprint, source, rendition, media_obj)
        for audio in self.audio_renditions(media_obj):
            self._schedule_audio(media_id, fingerprint, source, audio, media_obj)

    def _schedule_video(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: VideoRendition,
        media_obj: Any,
    ) -> None:
        self._schedule_job(
            media_id,
            fingerprint,
            rendition.name,
            self._transcode_video(media_id, fingerprint, source, rendition, media_obj),
        )

    def _schedule_audio(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: AudioRendition,
        media_obj: Any,
    ) -> None:
        self._schedule_job(
            media_id,
            fingerprint,
            rendition.name,
            self._transcode_audio(media_id, fingerprint, source, rendition, media_obj),
        )

    def _schedule_job(self, media_id: str, fingerprint: str, rendition_name: str, coroutine: Any) -> None:
        target = self.cache_path(media_id, fingerprint) / rendition_name / PLAYLIST_NAME
        key = f"{media_id}:{fingerprint}:{rendition_name}"
        if target.is_file() or key in self.active_jobs:
            if hasattr(coroutine, "close"):
                coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        self.active_jobs[key] = task
        task.add_done_callback(lambda _: self.active_jobs.pop(key, None))

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

    def _hls_output_args(self, temporary_dir: Path) -> list[str]:
        return [
            "-f", "hls",
            "-hls_time", "4",
            "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", "segment_%05d.m4s",
            PLAYLIST_NAME,
        ]

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
        temporary_dir = target_dir.with_name(f".{target_dir.name}.tmp")
        if (target_dir / PLAYLIST_NAME).is_file():
            return

        async with self.semaphore:
            if (target_dir / PLAYLIST_NAME).is_file():
                return
            shutil.rmtree(temporary_dir, ignore_errors=True)
            temporary_dir.mkdir(parents=True, exist_ok=True)
            cloud_process: Optional[asyncio.subprocess.Process] = None
            ffmpeg_process: Optional[asyncio.subprocess.Process] = None
            pump_task: Optional[asyncio.Task[None]] = None
            cloud_stderr_task: Optional[asyncio.Task[bytes]] = None
            ffmpeg_stderr_task: Optional[asyncio.Task[bytes]] = None
            try:
                actual_input, cloud_process = await self._input_process(source)
                command = [self._ffmpeg_executable(), "-hide_banner", "-nostdin", "-y", "-i", actual_input, *arguments, *self._hls_output_args(temporary_dir)]
                logger.info(f"[Playback Prep] Preparing {media_id} rendition {rendition_name}.")
                ffmpeg_process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(temporary_dir),
                    stdin=asyncio.subprocess.PIPE if cloud_process else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if cloud_process:
                    pump_task = asyncio.create_task(self._pump_cloud_input(cloud_process, ffmpeg_process))
                    assert cloud_process.stderr is not None
                    assert ffmpeg_process.stderr is not None
                    cloud_stderr_task = asyncio.create_task(cloud_process.stderr.read())
                    ffmpeg_stderr_task = asyncio.create_task(ffmpeg_process.stderr.read())
                    await pump_task
                    await ffmpeg_process.wait()
                    stderr = await ffmpeg_stderr_task
                    try:
                        await asyncio.wait_for(cloud_process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        cloud_process.kill()
                        await cloud_process.wait()
                    cloud_stderr = await cloud_stderr_task
                    if cloud_process.returncode != 0:
                        diagnostics = self.sanitize_diagnostics(cloud_stderr.decode("utf-8", errors="replace"))
                        raise PlaybackPreparationError("CLOUD_STREAM_FAILED", diagnostics or "Google Drive stopped delivering the media source.")
                else:
                    _, stderr = await ffmpeg_process.communicate()
                if ffmpeg_process.returncode != 0:
                    diagnostics = self.sanitize_diagnostics(stderr.decode("utf-8", errors="replace"))
                    raise PlaybackPreparationError("FFMPEG_PREPARATION_FAILED", diagnostics or "FFmpeg could not prepare this rendition.")
                if not (temporary_dir / PLAYLIST_NAME).is_file() or not any(temporary_dir.glob("*.m4s")):
                    raise PlaybackPreparationError("EMPTY_RENDITION", "FFmpeg completed without producing playable HLS segments.")
                shutil.rmtree(target_dir, ignore_errors=True)
                os.replace(temporary_dir, target_dir)
                self._clear_preparation_error(media_id, fingerprint)
                self.touch(media_id, fingerprint)
                await self.rebuild_master(media_id, fingerprint, media_obj)
                self.enforce_lru_limits()
            except asyncio.CancelledError:
                raise
            except PlaybackPreparationError as exc:
                self._write_preparation_error(media_id, fingerprint, exc.code, str(exc))
                logger.error(f"[Playback Prep] {media_id}/{rendition_name} failed ({exc.code}): {self.sanitize_diagnostics(str(exc), 800)}")
            except Exception as exc:
                message = self.sanitize_diagnostics(str(exc), 800)
                self._write_preparation_error(media_id, fingerprint, "PREPARATION_FAILED", message)
                logger.error(f"[Playback Prep] {media_id}/{rendition_name} failed: {message}")
            finally:
                if pump_task and not pump_task.done():
                    pump_task.cancel()
                if cloud_stderr_task and not cloud_stderr_task.done():
                    cloud_stderr_task.cancel()
                if ffmpeg_stderr_task and not ffmpeg_stderr_task.done():
                    ffmpeg_stderr_task.cancel()
                for process in (ffmpeg_process, cloud_process):
                    if process and process.returncode is None:
                        process.kill()
                        await process.wait()
                shutil.rmtree(temporary_dir, ignore_errors=True)

    async def _transcode_video(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: VideoRendition,
        media_obj: Any,
    ) -> None:
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

    def _external_audio_path(self, source: ResolvedMediaSource, audio: AudioRendition) -> Optional[Path]:
        audio_dir = source.local_path.parent / "audio"
        if not audio_dir.is_dir():
            return None
        supported = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
        files = sorted(item for item in audio_dir.iterdir() if item.is_file() and item.suffix.lower() in supported)
        language_match = next((item for item in files if item.stem.lower() == audio.language), None)
        if language_match:
            return language_match
        return files[audio.stream_index] if 0 <= audio.stream_index < len(files) else None

    async def _transcode_audio(
        self,
        media_id: str,
        fingerprint: str,
        source: ResolvedMediaSource,
        rendition: AudioRendition,
        media_obj: Any,
    ) -> None:
        external = self._external_audio_path(source, rendition)
        if external:
            external_source = ResolvedMediaSource(
                catalog_path=source.catalog_path,
                relative_path=source.relative_path,
                local_path=external,
                cloud_path=None,
                local_exists=True,
                cloud_exists=False,
            )
            arguments = ["-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
            await self._run_ffmpeg_job(media_id, fingerprint, rendition.name, external_source, arguments, media_obj)
            return
        arguments = [
            "-map", f"0:a:{rendition.stream_index}",
            "-vn",
            "-c:a", "aac",
            "-b:a", "160k",
            "-ac", "2",
        ]
        await self._run_ffmpeg_job(media_id, fingerprint, rendition.name, source, arguments, media_obj)

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
                attributes = [
                    f"BANDWIDTH={video.bandwidth}",
                    f"AVERAGE-BANDWIDTH={int(video.bandwidth * 0.82)}",
                    f"RESOLUTION={video.width}x{video.height}",
                    'CODECS="avc1.640028,mp4a.40.2"' if audios else 'CODECS="avc1.640028"',
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

    def _clear_preparation_error(self, media_id: str, fingerprint: str) -> None:
        (self.cache_path(media_id, fingerprint) / "preparation-error.json").unlink(missing_ok=True)

    def touch(self, media_id: str, fingerprint: str) -> None:
        path = self.cache_path(media_id, fingerprint)
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.utime(path, None)
        except OSError:
            pass

    def cancel_media(self, media_id: str, fingerprint: Optional[str] = None) -> None:
        prefix = f"{media_id}:{fingerprint}:" if fingerprint else f"{media_id}:"
        for key, task in list(self.active_jobs.items()):
            if key.startswith(prefix):
                task.cancel()

    def recover_interrupted_outputs(self) -> None:
        if not self.cache_dir.exists():
            return
        for path in self.cache_dir.rglob("*.tmp"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    async def schedule_catalog_baselines(self) -> None:
        from db import engine
        from models import Episode, Movie
        from sqlmodel import select
        from sqlmodel.ext.asyncio.session import AsyncSession

        self.recover_interrupted_outputs()
        async with AsyncSession(engine) as db:
            movies = (await db.exec(select(Movie).where(Movie.video_url != ""))).all()
            episodes = (await db.exec(select(Episode).where(Episode.video_url != ""))).all()
            for media_obj in [*movies, *episodes]:
                try:
                    source = await resolve_media_source(media_obj.video_url)
                    if not source.available:
                        continue
                    fingerprint = source.fingerprint
                    if media_obj.source_fingerprint != fingerprint:
                        media_obj.source_fingerprint = fingerprint
                        db.add(media_obj)
                    await self.prepare(media_obj.id, media_obj, source, include_remaining=False)
                except Exception as exc:
                    logger.warning(f"[Playback Prep] Baseline scheduling skipped for {media_obj.id}: {self.sanitize_diagnostics(str(exc), 400)}")
            await db.commit()

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
