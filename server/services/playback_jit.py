from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from config import settings
from services.logger import logger
from services.media_source import ResolvedMediaSource
from services.playback_prep import AudioRendition, VideoRendition, playback_prep_service
from services.playback_source import PlaybackSourceFailure, source_reader
from services.state import register_process, unregister_process


@dataclass(frozen=True, slots=True)
class AdaptiveSegment:
    path: Path
    content_type: str = "video/mp2t"
    cache_hit: bool = False


class AdaptiveDeliveryFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PlaybackJITService:
    """Time-indexed adaptive delivery that prepares only the requested playback window."""

    def __init__(self) -> None:
        self.cache_dir = Path(settings.TEMP_DIR).resolve() / "playback-jit"
        self.segment_seconds = int(settings.PLAYBACK_SEGMENT_SECONDS)
        self.window_segments = int(settings.PLAYBACK_WINDOW_SEGMENTS)
        self.semaphore = asyncio.Semaphore(settings.PLAYBACK_TRANSCODE_CONCURRENCY)
        self.active_jobs: dict[str, asyncio.Task[None]] = {}
        self._last_access: dict[str, float] = {}

    @staticmethod
    def duration_seconds(media_obj: Any) -> float:
        probed = float(getattr(media_obj, "probed_duration", 0) or 0)
        if probed > 0:
            return probed
        value = str(getattr(media_obj, "duration", "") or "").strip().lower()
        if not value:
            return 0.0
        try:
            if ":" in value:
                parts = [float(part) for part in value.split(":")]
                if len(parts) == 3:
                    return parts[0] * 3600 + parts[1] * 60 + parts[2]
                if len(parts) == 2:
                    return parts[0] * 60 + parts[1]
            hours = 0.0
            minutes = 0.0
            if "h" in value:
                hour, value = value.split("h", 1)
                hours = float(hour.strip())
            if "m" in value:
                minutes = float(value.split("m", 1)[0].strip())
            elif value.strip():
                minutes = float(value.strip())
            return hours * 3600 + minutes * 60
        except ValueError:
            return 0.0

    def cache_path(self, media_id: str, fingerprint: str) -> Path:
        safe_media = "".join(character for character in media_id if character.isalnum() or character in "_-")
        safe_fingerprint = "".join(character for character in fingerprint if character.isalnum() or character in "_-")
        if not safe_media or not safe_fingerprint:
            raise AdaptiveDeliveryFailure("INVALID_CACHE_IDENTITY", "The adaptive cache identity is invalid.")
        return self.cache_dir / safe_media / safe_fingerprint

    def master_manifest(self, media_id: str, ticket: str, media_obj: Any) -> str:
        encoded_media = quote(media_id, safe="")
        encoded_ticket = quote(ticket, safe="")
        videos = playback_prep_service.video_renditions(media_obj)
        audios = playback_prep_service.audio_renditions(media_obj)
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for audio in audios:
            uri = (
                f"/api/playback/jit/{encoded_media}/{quote(audio.name, safe='')}/playlist.m3u8"
                f"?ticket={encoded_ticket}"
            )
            attributes = [
                "TYPE=AUDIO",
                'GROUP-ID="audio"',
                f'NAME="{audio.label.replace(chr(34), "")}"',
                f'LANGUAGE="{audio.language}"',
                f"DEFAULT={'YES' if audio.default else 'NO'}",
                "AUTOSELECT=YES",
                f'URI="{uri}"',
            ]
            lines.append(f"#EXT-X-MEDIA:{','.join(attributes)}")
        for video in videos:
            attributes = [
                f"BANDWIDTH={max(200_000, video.bandwidth)}",
                f"AVERAGE-BANDWIDTH={max(180_000, round(video.bandwidth * 0.82))}",
                f"RESOLUTION={video.width}x{video.height}",
                f'NAME="{video.label}"',
            ]
            if audios:
                attributes.append('AUDIO="audio"')
            uri = (
                f"/api/playback/jit/{encoded_media}/{quote(video.name, safe='')}/playlist.m3u8"
                f"?ticket={encoded_ticket}"
            )
            lines.extend([f"#EXT-X-STREAM-INF:{','.join(attributes)}", uri])
        return "\n".join(lines) + "\n"

    def media_manifest(self, media_id: str, rendition_id: str, ticket: str, media_obj: Any) -> str:
        duration = self.duration_seconds(media_obj)
        if duration <= 0:
            raise AdaptiveDeliveryFailure("MEDIA_DURATION_UNKNOWN", "The media duration is unavailable.")
        segment_count = max(1, math.ceil(duration / self.segment_seconds))
        encoded_media = quote(media_id, safe="")
        encoded_rendition = quote(rendition_id, safe="")
        encoded_ticket = quote(ticket, safe="")
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            f"#EXT-X-TARGETDURATION:{self.segment_seconds}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for index in range(segment_count):
            if index > 0 and index % self.window_segments == 0:
                lines.append("#EXT-X-DISCONTINUITY")
            start = index * self.segment_seconds
            segment_duration = min(float(self.segment_seconds), max(0.001, duration - start))
            lines.append(f"#EXTINF:{segment_duration:.3f},")
            lines.append(
                f"/api/playback/jit/{encoded_media}/{encoded_rendition}/segment_{index:05d}.ts"
                f"?ticket={encoded_ticket}"
            )
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def rendition(
        self,
        media_obj: Any,
        rendition_id: str,
    ) -> tuple[Optional[VideoRendition], Optional[AudioRendition]]:
        video = next(
            (item for item in playback_prep_service.video_renditions(media_obj) if item.name == rendition_id),
            None,
        )
        audio = next(
            (item for item in playback_prep_service.audio_renditions(media_obj) if item.name == rendition_id),
            None,
        )
        if not video and not audio:
            raise AdaptiveDeliveryFailure("RENDITION_NOT_FOUND", "The requested rendition does not exist.")
        return video, audio

    async def segment(
        self,
        media_id: str,
        fingerprint: str,
        rendition_id: str,
        segment_index: int,
        ticket: str,
        source: ResolvedMediaSource,
        media_obj: Any,
    ) -> AdaptiveSegment:
        if segment_index < 0:
            raise AdaptiveDeliveryFailure("SEGMENT_NOT_FOUND", "The requested segment does not exist.")
        duration = self.duration_seconds(media_obj)
        if duration <= 0 or segment_index * self.segment_seconds >= duration:
            raise AdaptiveDeliveryFailure("SEGMENT_NOT_FOUND", "The requested segment is outside the media timeline.")
        video, audio = self.rendition(media_obj, rendition_id)
        root = self.cache_path(media_id, fingerprint) / rendition_id
        target = root / f"segment_{segment_index:05d}.ts"
        if target.is_file() and target.stat().st_size > 0:
            self._touch(target)
            return AdaptiveSegment(target, cache_hit=True)

        window_start = (segment_index // self.window_segments) * self.window_segments
        key = f"{media_id}:{fingerprint}:{rendition_id}:{window_start}"
        task = self.active_jobs.get(key)
        if not task:
            snapshot = playback_prep_service.snapshot_media(media_obj)
            task = asyncio.create_task(
                self._generate_window(
                    media_id,
                    fingerprint,
                    rendition_id,
                    window_start,
                    ticket,
                    source,
                    snapshot,
                    video,
                    audio,
                )
            )
            self.active_jobs[key] = task

            def finished(completed: asyncio.Task[None]) -> None:
                if self.active_jobs.get(key) is completed:
                    self.active_jobs.pop(key, None)

            task.add_done_callback(finished)
        deadline = time.monotonic() + settings.PLAYBACK_SEGMENT_WAIT_SECONDS
        while not target.is_file() or target.stat().st_size <= 0:
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                break
            if time.monotonic() >= deadline:
                raise AdaptiveDeliveryFailure(
                    "SEGMENT_GENERATION_TIMEOUT",
                    "The requested playback position took too long to prepare.",
                )
            await asyncio.sleep(0.05)
        if not target.is_file() or target.stat().st_size <= 0:
            error_path = root / f"window_{window_start:05d}.error.json"
            if error_path.is_file():
                try:
                    payload = json.loads(error_path.read_text(encoding="utf-8"))
                    raise AdaptiveDeliveryFailure(
                        str(payload.get("code") or "SEGMENT_GENERATION_FAILED"),
                        str(payload.get("message") or "The requested playback segment could not be generated."),
                    )
                except json.JSONDecodeError:
                    pass
            raise AdaptiveDeliveryFailure(
                "SEGMENT_GENERATION_FAILED",
                "The requested playback segment could not be generated.",
            )
        self._touch(target)
        return AdaptiveSegment(target, cache_hit=False)

    def _touch(self, target: Path) -> None:
        now = time.monotonic()
        key = str(target.parent)
        if now - self._last_access.get(key, 0) < 15:
            return
        self._last_access[key] = now
        try:
            os.utime(target.parent, None)
        except OSError:
            pass

    async def _generate_window(
        self,
        media_id: str,
        fingerprint: str,
        rendition_id: str,
        window_start: int,
        ticket: str,
        source: ResolvedMediaSource,
        media_obj: Any,
        video: Optional[VideoRendition],
        audio: Optional[AudioRendition],
    ) -> None:
        root = self.cache_path(media_id, fingerprint) / rendition_id
        root.mkdir(parents=True, exist_ok=True)
        marker = root / f"window_{window_start:05d}.complete"
        if marker.is_file():
            return
        error_path = root / f"window_{window_start:05d}.error.json"
        error_path.unlink(missing_ok=True)
        start_seconds = window_start * self.segment_seconds
        remaining = max(0.0, self.duration_seconds(media_obj) - start_seconds)
        window_duration = min(float(self.segment_seconds * self.window_segments), remaining)
        if window_duration <= 0:
            raise AdaptiveDeliveryFailure("SEGMENT_NOT_FOUND", "The requested segment is outside the media timeline.")

        selected_source = source
        source_id = "main"
        if audio and audio.source == "external":
            external_path = playback_prep_service._external_audio_path(source, audio)
            external = playback_prep_service._external_audio_source(source, audio, external_path)
            if not external:
                raise AdaptiveDeliveryFailure("AUDIO_SOURCE_MISSING", "The selected dubbing source is unavailable.")
            selected_source = external
            source_id = audio.name

        reader = source_reader(selected_source, loopback_url=settings.PLAYBACK_LOOPBACK_URL)
        actual_input = reader.ffmpeg_input(media_id, ticket, source_id)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AdaptiveDeliveryFailure("FFMPEG_UNAVAILABLE", "FFmpeg is not installed or executable.")

        output_playlist = root / f"window_{window_start:05d}.m3u8"
        command = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            actual_input,
            "-t",
            f"{window_duration:.3f}",
        ]
        if video:
            bitrate = max(300, video.bandwidth // 1000)
            command.extend([
                "-map", "0:v:0",
                "-an",
                "-vf",
                f"scale={video.width}:{video.height}:force_original_aspect_ratio=decrease,pad={video.width}:{video.height}:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-profile:v", "high",
                "-level", "4.1",
                "-pix_fmt", "yuv420p",
                "-crf", "22",
                "-maxrate", f"{bitrate}k",
                "-bufsize", f"{bitrate * 2}k",
                "-sc_threshold", "0",
                "-force_key_frames", f"expr:gte(t,n_forced*{self.segment_seconds})",
            ])
        elif audio:
            stream_index = 0 if audio.source == "external" else audio.stream_index
            command.extend([
                "-map", f"0:a:{stream_index}",
                "-vn",
                "-c:a", "aac",
                "-b:a", "160k",
                "-ac", "2",
            ])
        command.extend([
            "-output_ts_offset", f"{start_seconds:.3f}",
            "-f", "hls",
            "-hls_time", str(self.segment_seconds),
            "-hls_list_size", "0",
            "-hls_flags", "independent_segments+temp_file",
            "-start_number", str(window_start),
            "-hls_segment_filename", str(root / "segment_%05d.ts"),
            str(output_playlist),
        ])

        started_at = time.monotonic()
        process: asyncio.subprocess.Process | None = None
        process_key = ""
        try:
            async with self.semaphore:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                process_key = f"playback-jit:{process.pid}:{media_id}:{rendition_id}:{window_start}"
                register_process(process_key, process)
                _, stderr = await process.communicate()
                if process.returncode != 0:
                    diagnostics = stderr.decode("utf-8", errors="replace")[-1200:]
                    logger.error(
                        f"[Playback JIT] {media_id}/{rendition_id}/{window_start} failed: {diagnostics}"
                    )
                    raise AdaptiveDeliveryFailure(
                        "SEGMENT_GENERATION_FAILED",
                        "FFmpeg could not generate the requested playback window.",
                    )
            marker.write_text(str(time.time()), encoding="utf-8")
            logger.info(
                f"[Playback JIT] Generated {media_id}/{rendition_id} at {start_seconds:.1f}s "
                f"in {time.monotonic() - started_at:.2f}s."
            )
            await asyncio.to_thread(self.enforce_lru_limits)
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except (AdaptiveDeliveryFailure, PlaybackSourceFailure) as exc:
            error_path.write_text(
                json.dumps({"code": getattr(exc, "code", "SEGMENT_GENERATION_FAILED"), "message": str(exc)}),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error(
                f"[Playback JIT] Internal failure for {media_id}/{rendition_id}/{window_start}: "
                f"{type(exc).__name__}: {exc}"
            )
            error_path.write_text(
                json.dumps({
                    "code": "INTERNAL_SEGMENT_ERROR",
                    "message": "The playback window encountered an internal server error.",
                }),
                encoding="utf-8",
            )
        finally:
            if process_key:
                unregister_process(process_key)

    def enforce_lru_limits(self) -> None:
        limit_bytes = int(settings.PLAYBACK_CACHE_GB * 1024 * 1024 * 1024)
        if limit_bytes <= 0 or not self.cache_dir.is_dir():
            return
        active_roots = {
            str(self.cache_path(parts[0], parts[1]))
            for key in list(self.active_jobs)
            if len(parts := key.split(":")) >= 4
        }
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for media_dir in self.cache_dir.iterdir():
            if not media_dir.is_dir():
                continue
            for fingerprint_dir in media_dir.iterdir():
                if not fingerprint_dir.is_dir():
                    continue
                size = sum(path.stat().st_size for path in fingerprint_dir.rglob("*") if path.is_file())
                total += size
                entries.append((fingerprint_dir.stat().st_mtime, size, fingerprint_dir))
        for _, size, path in sorted(entries, key=lambda entry: entry[0]):
            if total <= limit_bytes:
                break
            if str(path.resolve()) in active_roots:
                continue
            shutil.rmtree(path, ignore_errors=True)
            total -= size

    async def shutdown(self) -> None:
        tasks = list(self.active_jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


playback_jit_service = PlaybackJITService()
