import os
import time
import json
import shutil
import asyncio
import traceback
import httpx
import aiofiles
import re
from typing import Dict, Any, List, Optional
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from db import engine
from models import DownloadTask, Movie, Episode
from services.ffmpeg import download_and_merge, download_and_cache_metadata_image
from services.tmdb import tmdb_client
from config import settings
from services.logger import logger
from services.media_probe import probe_media_stream, notify_video_sender, probe_completed_media
from services.ingestion_errors import IngestionFailure, IngestionTaskError, prune_task_diagnostics, write_task_diagnostics
from services.rclone import rclone_service
from services.media_source import MediaSourceError, resolve_media_source

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
    def __init__(self):
        self.loop_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.active_tasks = set()
        self.worker_tasks: Dict[str, asyncio.Task] = {}

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
        logger.info("[Queue Manager] Background worker loop stopped.")

    def _worker_finished(self, task_id: str, worker: asyncio.Task) -> None:
        self.active_tasks.discard(task_id)
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
                    task = result.first()
                    
                    if not task:
                        continue
                    
                    task.status = "DOWNLOADING"
                    db.add(task)
                    await db.commit()
                    await db.refresh(task)
                    
                    task_id = task.id
                
                self.active_tasks.add(task_id)
                worker = asyncio.create_task(self._process_task(task_id), name=f"ingestion-{task_id}")
                self.worker_tasks[task_id] = worker
                worker.add_done_callback(lambda future, tid=task_id: self._worker_finished(tid, future))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Queue Manager] Error in worker loop: {e}")

    async def run_rclone_move_dir(self, local_dir: str, remote_subpath: str) -> bool:
        if not rclone_service.executable():
            logger.error("[Queue Manager] Rclone binary not found. Cannot perform cloud upload.")
            return False
        target_remote = f"{settings.RCLONE_REMOTE_PATH}/{remote_subpath.replace('\\', '/')}"
        try:
            upload = await rclone_service.run("copy", local_dir, target_remote, "--retries", "3", timeout=6 * 60 * 60)
            if not upload.ok:
                logger.error(f"[Queue Manager] Drive upload failed: {upload.error_code or 'rclone_failed'}")
                return False
            verification = await rclone_service.run("check", local_dir, target_remote, "--one-way", timeout=60 * 60)
            if not verification.ok:
                logger.error(f"[Queue Manager] Drive upload verification failed: {verification.error_code or 'rclone_failed'}")
                return False
            shutil.rmtree(local_dir, ignore_errors=True)
            return True
        except Exception as e:
            logger.error(f"[Queue Manager] Rclone move subprocess exception: {e}")
            return False

    async def _process_task(self, task_id: str):
        logger.info(f"[Queue Manager] Processing task: {task_id}")
        output_abs_path: Optional[str] = None
        created_artifacts: List[str] = []
        submitted_video_url = ""
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
                video_url = task.video_url
                submitted_video_url = video_url
                audio_url = task.audio_url
                season = task.season
                episode = task.episode
                subtitles_list = task.subtitles
                quality = task.quality
                language = task.language

            # 1. Disk Space Check
            storage_root = settings.TEMP_DIR if settings.STORAGE_ENGINE == "CLOUD" else settings.MEDIA_DIR
            try:
                total, used, free = shutil.disk_usage(storage_root)
                free_gb = free / (1024**3)
                logger.info(f"[Queue Manager] Free space in storage root '{storage_root}': {free_gb:.2f} GB")
                if free_gb < 5.0:
                    raise IngestionTaskError(IngestionFailure("INSUFFICIENT_STORAGE", f"Only {free_gb:.2f} GB is free; ingestion requires at least 5 GB."))
            except IngestionTaskError:
                raise
            except OSError as err:
                diagnostics_path = write_task_diagnostics(task_id, "storage check", repr(err))
                raise IngestionTaskError(IngestionFailure("STORAGE_UNAVAILABLE", "The configured storage root could not be checked.", False, diagnostics_path)) from err

            # 2. Probe Media Stream
            logger.info(f"[Queue Manager] Validating media sender source for task {task_id}...")
            probe_res = None
            for probe_attempt in range(1, 4):
                probe_res = await probe_media_stream(video_url, audio_url, headers, task_id=task_id)
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

            # Save probe details to DB
            async with AsyncSession(engine) as db:
                task_db = await db.get(DownloadTask, task_id)
                if task_db:
                    task_db.has_video = has_video
                    task_db.has_audio = has_audio
                    task_db.scan_quality = scan_quality
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

            raw_title = meta.get("title", f"Media_{tmdb_id}")
            clean_title = "".join(c for c in raw_title if c.isalnum() or c in " .-_")

            if media_type == "movie":
                release_year = meta.get("releaseYear", 2026)
                folder_name = f"{clean_title}_{release_year}_TMDB_{tmdb_id}"
                folder_rel_path = os.path.join(storage_root, "Movies", folder_name)
                filename = f"{clean_title}_{release_year}.mp4"
                output_rel_path = os.path.join(folder_rel_path, filename)
            else:
                season_num = season or 1
                ep_num = episode or 1
                folder_name = f"{clean_title}_TMDB_{tmdb_id}"
                folder_rel_path = os.path.join(storage_root, "Series", folder_name, f"Season_{season_num}", f"Episode_{ep_num}")
                filename = f"{clean_title}_S{season_num:02d}E{ep_num:02d}.mp4"
                output_rel_path = os.path.join(folder_rel_path, filename)
                
            output_abs_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", output_rel_path))
            os.makedirs(os.path.dirname(output_abs_path), exist_ok=True)

            for sub in subtitles_list:
                sub_url = sub.get("url")
                sub_lang = sub.get("language", "en")
                if sub_url and sub_url.startswith("http"):
                    try:
                        async with httpx.AsyncClient() as client:
                            response = await client.get(sub_url, timeout=15.0)
                            if response.status_code == 200:
                                ext = ".srt" if ".srt" in sub_url.lower() else ".vtt"
                                sub_abs_path = os.path.join(os.path.dirname(output_abs_path), f"subtitle_{sub_lang}{ext}")
                                async with aiofiles.open(sub_abs_path, "wb") as f:
                                    await f.write(response.content)
                                created_artifacts.append(sub_abs_path)
                    except Exception:
                        pass

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
                
                success, failure = await download_and_merge(
                    task_id=task_id, video_url=video_url, audio_url=audio_url,
                    headers=headers, output_path=output_abs_path, duration_secs=duration_secs
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

            rclone_success = True
            if success:
                from services.audio_extractor import extract_audio_and_strip_video
                extracted_languages = await extract_audio_and_strip_video(output_abs_path, default_lang=language or "en")
                
                if settings.STORAGE_ENGINE == "CLOUD":
                    async with AsyncSession(engine) as db:
                        task_db = await db.get(DownloadTask, task_id)
                        if task_db:
                            task_db.status = "MOVING_CLOUD"
                            db.add(task_db)
                            await db.commit()
                
                from services.state import update_task_metrics
                update_task_metrics(task_id, 99.9, speed="Uploading", eta="00:00:00", force_write=True)
                
                local_dir = os.path.dirname(output_abs_path)
                if media_type == "movie":
                    remote_subpath = os.path.join("Movies", folder_name)
                else:
                    remote_subpath = os.path.join("Series", folder_name, f"Season_{season}", f"Episode_{episode}")
                
                if settings.STORAGE_ENGINE == "CLOUD":
                    rclone_success = await self.run_rclone_move_dir(local_dir, remote_subpath)
                    
                    if not rclone_success:
                        logger.warning(f"[Queue Manager] Cloud upload failed for task {task_id}! Falling back to local storage.")
                        temp_abs = os.path.normpath(os.path.abspath(settings.TEMP_DIR))
                        media_abs = os.path.normpath(os.path.abspath(settings.MEDIA_DIR))
                        local_dir_norm = os.path.normpath(local_dir)
                        dest_dir = local_dir_norm.replace(temp_abs, media_abs)
                        
                        try:
                            os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
                            if os.path.exists(local_dir):
                                if os.path.exists(dest_dir):
                                    shutil.rmtree(dest_dir, ignore_errors=True)
                                shutil.move(local_dir, dest_dir)
                                logger.info(f"[Queue Manager] Local fallback copy completed for task {task_id}. Files moved to {dest_dir}")
                                rclone_success = True
                        except Exception as fallback_err:
                            logger.error(f"[Queue Manager] Local fallback copy failed for task {task_id}: {fallback_err}")
                else:
                    rclone_success = True

            if not rclone_success:
                raise IngestionTaskError(IngestionFailure("CLOUD_MOVE_FAILED", "Cloud upload and local fallback both failed."))

            async with AsyncSession(engine) as db:
                task = await db.get(DownloadTask, task_id)
                if not task:
                    self.active_tasks.discard(task_id)
                    from services.state import remove_task_metrics
                    remove_task_metrics(task_id)
                    return
                    
                task.status = "COMPLETED"
                task.error_message = None
                db.add(task)
                await db.commit()

                try:
                    await self._catalog_media(db, tmdb_id, media_type, season, episode, meta, output_rel_path, extracted_languages, language, subtitles_list, quality, task.skip_markers)
                except Exception as cat_err:
                    logger.error(f"[Queue Manager] Error cataloging media: {cat_err}")
                created_artifacts.clear()
                    
        except IngestionTaskError as err:
            logger.error(f"[Queue Manager] Task {task_id} failed: {err.failure.display}")
            await self._record_task_failure(task_id, err.failure, submitted_video_url)
        except asyncio.CancelledError:
            await self._record_task_failure(task_id, IngestionFailure("INGESTION_INTERRUPTED", "The task was interrupted by server shutdown."), submitted_video_url)
            raise
        except Exception as err:
            diagnostics_path = write_task_diagnostics(task_id, "queue exception", traceback.format_exc())
            failure = IngestionFailure("QUEUE_INTERNAL_ERROR", f"The queue encountered {type(err).__name__}.", False, diagnostics_path)
            logger.error(f"[Queue Manager] Task {task_id} failed: {failure.display}")
            await self._record_task_failure(task_id, failure, submitted_video_url)
                
        finally:
            for artifact in created_artifacts:
                if os.path.exists(artifact):
                    try:
                        os.remove(artifact)
                    except OSError:
                        pass
            if output_abs_path:
                self._remove_empty_parents(os.path.dirname(output_abs_path))
            self.active_tasks.discard(task_id)
            from services.state import remove_task_metrics
            remove_task_metrics(task_id)

    async def _record_task_failure(self, task_id: str, failure: IngestionFailure, submitted_video_url: str) -> None:
        try:
            async with AsyncSession(engine) as db:
                task = await db.get(DownloadTask, task_id)
                if not task:
                    return
                task.status = "FAILED"
                task.error_message = failure.display
                db.add(task)

                if task.media_type == "movie":
                    movie = await db.get(Movie, f"m_{task.tmdb_id}")
                    if movie and movie.video_url == submitted_video_url:
                        movie.video_url = ""
                        movie.availability = "cached"
                        db.add(movie)
                elif task.season is not None and task.episode is not None:
                    episode = await db.get(Episode, f"ep_{task.tmdb_id}_s{task.season}_e{task.episode}")
                    if episode and episode.video_url == submitted_video_url:
                        episode.video_url = ""
                        db.add(episode)
                    show = await db.get(Movie, f"tv_{task.tmdb_id}")
                    if show:
                        episode_result = await db.exec(select(Episode).where(Episode.movie_id == show.id))
                        has_local_episode = any(item.video_url.startswith("/media/") for item in episode_result.all() if item.video_url)
                        show.availability = "available" if has_local_episode else "cached"
                        db.add(show)
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

    async def _schedule_playback_baseline(self, db: AsyncSession, media_obj: Any) -> None:
        if not getattr(media_obj, "video_url", ""):
            return
        try:
            from services.playback_prep import playback_prep_service

            source = await resolve_media_source(media_obj.video_url)
            if not source.available:
                return
            if media_obj.source_fingerprint != source.fingerprint:
                media_obj.source_fingerprint = source.fingerprint
                db.add(media_obj)
                await db.commit()
            await playback_prep_service.prepare(media_obj.id, media_obj, source, include_remaining=False)
        except Exception as exc:
            logger.warning(
                f"[Queue Manager] Playback baseline scheduling failed for "
                f"{getattr(media_obj, 'id', 'unknown')}: {type(exc).__name__}"
            )

    async def _catalog_media(
        self, db: AsyncSession, tmdb_id: int, media_type: str, season: Optional[int], 
        episode: Optional[int], meta: Dict[str, Any], file_path: str, 
        extracted_languages: Optional[List[str]] = None, language: Optional[str] = None, 
        subtitles_list: Optional[List[Dict[str, str]]] = None, quality: Optional[str] = None,
        skip_markers: Optional[Dict[str, Any]] = None
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
                
            if current_folder_name and current_folder_name != correct_folder_name:
                old_folder_abs = os.path.abspath(os.path.join(server_root, parent_dir_rel, current_folder_name))
                new_folder_abs = os.path.abspath(os.path.join(server_root, parent_dir_rel, correct_folder_name))
                
                if os.path.exists(old_folder_abs) and not os.path.exists(new_folder_abs):
                    logger.info(f"[Queue Manager] Renaming placeholder folder on disk: {current_folder_name} -> {correct_folder_name}")
                    try:
                        os.rename(old_folder_abs, new_folder_abs)
                        file_path = file_path.replace(current_folder_name, correct_folder_name)
                        if media_type == "movie":
                            old_file_name = parts[-1]
                            new_file_name = f"{clean_title}_{release_year}.mp4"
                            old_file_abs = os.path.abspath(os.path.join(new_folder_abs, old_file_name))
                            new_file_abs = os.path.abspath(os.path.join(new_folder_abs, new_file_name))
                            if os.path.exists(old_file_abs):
                                os.rename(old_file_abs, new_file_abs)
                                file_path = os.path.join(parent_dir_rel, correct_folder_name, new_file_name).replace("\\", "/")
                    except Exception as e:
                        logger.error(f"[Queue Manager] Failed to rename folder/file {current_folder_name}: {e}")

            virtual_path = file_path
            if virtual_path.startswith("temp/"):
                virtual_path = "media/" + virtual_path[5:]
            served_url = "/" + virtual_path.replace("\\", "/")
        
        abs_file_path = os.path.abspath(os.path.join(server_root, virtual_path))
        
        if media_type == "movie":
            main_folder_rel = os.path.dirname(virtual_path)
        else:
            main_folder_rel = os.path.dirname(os.path.dirname(os.path.dirname(virtual_path)))
            
        main_folder_abs = os.path.abspath(os.path.join(server_root, main_folder_rel))
        os.makedirs(main_folder_abs, exist_ok=True)
        
        poster_rel = os.path.join(main_folder_rel, "poster.jpg")
        backdrop_rel = os.path.join(main_folder_rel, "backdrop.jpg")
        poster_abs = os.path.abspath(os.path.join(server_root, poster_rel))
        backdrop_abs = os.path.abspath(os.path.join(server_root, backdrop_rel))
        
        local_thumbnail = await download_and_cache_metadata_image(meta.get("thumbnailUrl"), poster_abs)
        local_banner = await download_and_cache_metadata_image(meta.get("bannerUrl"), backdrop_abs)

        # Probe completed media file
        probe_meta = {}
        if file_path:
            probe_meta = await probe_completed_media(abs_file_path)

        probed_duration = probe_meta.get("probed_duration")
        container = probe_meta.get("container")
        codec = probe_meta.get("codec")
        width = probe_meta.get("width")
        height = probe_meta.get("height")
        frame_rate = probe_meta.get("frame_rate")
        source_fingerprint = probe_meta.get("source_fingerprint")
        audio_meta_list = probe_meta.get("audio_metadata", [])
        
        if media_type == "movie":
            movie_id = f"m_{tmdb_id}"
            movie_folder_abs = os.path.dirname(abs_file_path)
            movie_quality = quality or "Source"
            movie_languages = extracted_languages if extracted_languages else ([language] if language else ["en"])
            
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
                            "ext": ".vtt"
                        })

            duration_str = meta.get("duration", "1h 30m")
            skip_data = skip_markers or {}
            
            movie = await db.get(Movie, movie_id)
            if not movie:
                movie = Movie(
                    id=movie_id, title=meta.get("title", f"Movie {tmdb_id}"), description=meta.get("description", ""),
                    thumbnail_url=local_thumbnail or "", banner_url=local_banner or "", video_url=served_url,
                    duration=duration_str, release_year=meta.get("releaseYear", 2026),
                    rating=meta.get("rating", "PG-13"), director=meta.get("director", "Unknown"),
                    original_language=language or meta.get("originalLanguage", "en"), type="movie",
                    vote_average=meta.get("vote_average", 7.5), vote_count=meta.get("vote_count", 100),
                    tmdb_id=int(tmdb_id), catalog_source="server", availability="available",
                    probed_duration=probed_duration, container=container, codec=codec,
                    width=width, height=height, frame_rate=frame_rate,
                    source_fingerprint=source_fingerprint
                )
                movie.genres = meta.get("genres", [])
                movie.cast = meta.get("cast", [])
                movie.quality = movie_quality
                movie.languages = movie_languages
                movie.subtitles = subs_on_disk
                movie.skip_markers = skip_data
                movie.audio_metadata = audio_meta_list
                db.add(movie)
                await db.commit()
                logger.info(f"[Queue Manager] Cataloged new movie: {meta.get('title', 'Unknown Movie')}")
            else:
                movie.title = meta.get("title", movie.title)
                movie.description = meta.get("description", movie.description)
                movie.thumbnail_url = local_thumbnail or movie.thumbnail_url
                movie.banner_url = local_banner or movie.banner_url
                movie.video_url = served_url
                movie.tmdb_id = int(tmdb_id)
                movie.catalog_source = "server"
                movie.availability = "available"
                movie.duration = duration_str
                movie.release_year = meta.get("releaseYear", movie.release_year)
                movie.rating = meta.get("rating", movie.rating)
                movie.director = meta.get("director", movie.director)
                movie.cast = meta.get("cast", movie.cast)
                movie.genres = meta.get("genres", movie.genres)
                movie.original_language = language or meta.get("originalLanguage", movie.original_language)
                movie.quality = movie_quality
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
                movie.audio_metadata = audio_meta_list
                db.add(movie)
                await db.commit()
                logger.info(f"[Queue Manager] Updated existing movie details: {movie.title}")

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
                "language": language or "en",
                "languages": movie_languages,
                "quality": movie_quality,
                "subtitles": subs_on_disk,
                "original_language": language or "en",
                "skip_markers": skip_data,
                "probed_duration": probed_duration,
                "container": container,
                "codec": codec,
                "width": width,
                "height": height,
                "frame_rate": frame_rate,
                "source_fingerprint": source_fingerprint,
                "audio_metadata": audio_meta_list
            }
            with open(movie_metadata_file, "w", encoding="utf-8") as f:
                json.dump(movie_metadata_content, f, indent=2, ensure_ascii=False)
            await self._schedule_playback_baseline(db, movie)
        else:
            show_id = f"tv_{tmdb_id}"
            show = await db.get(Movie, show_id)
            if not show:
                show = Movie(
                    id=show_id, title=meta.get("title", f"Show {tmdb_id}"), description=meta.get("description", ""),
                    thumbnail_url=local_thumbnail or "", banner_url=local_banner or "", video_url="",
                    duration=meta.get("duration", "45m"), release_year=meta.get("releaseYear", 2026),
                    rating=meta.get("rating", "TV-14"), director=meta.get("director", "Various"),
                    original_language=language or meta.get("originalLanguage", "en"), type="series",
                    vote_average=meta.get("vote_average", 7.5), vote_count=meta.get("vote_count", 100),
                    tmdb_id=int(tmdb_id), catalog_source="server", availability="available"
                )
                show.genres = meta.get("genres", [])
                show.cast = meta.get("cast", [])
                db.add(show)
                await db.commit()
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
                show.original_language = language or meta.get("originalLanguage", show.original_language)
                show.vote_average = meta.get("vote_average", show.vote_average)
                show.vote_count = meta.get("vote_count", show.vote_count)
                db.add(show)
                await db.commit()
                logger.info(f"[Queue Manager] Updated TV show: {show.title}")
                
            season_num = season or 1
            episode_num = episode or 1

            ep_quality = quality or "Source"
            ep_languages = extracted_languages if extracted_languages else ([language] if language else ["en"])

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
                            "ext": ".vtt"
                        })
            
            ep_id = f"ep_{tmdb_id}_s{season_num}_e{episode_num}"
            ep_entry = await db.get(Episode, ep_id)
            
            ep_meta = meta.get("episode_detail", {})
            ep_title = ep_meta.get("title", f"Episode {episode_num}")
            ep_desc = ep_meta.get("description", f"Season {season_num}, Episode {episode_num}")
            ep_still_url = ep_meta.get("thumbnailUrl")
            
            ep_thumb_rel = ""
            if ep_still_url:
                ep_thumb_abs = os.path.join(ep_folder_abs, "thumbnail.jpg")
                ep_thumb_rel = await download_and_cache_metadata_image(ep_still_url, ep_thumb_abs)

            ep_poster_abs = os.path.join(ep_folder_abs, "poster.jpg")
            ep_backdrop_abs = os.path.join(ep_folder_abs, "backdrop.jpg")
            
            if poster_abs and os.path.exists(poster_abs) and not os.path.exists(ep_poster_abs):
                shutil.copy(poster_abs, ep_poster_abs)
            elif not os.path.exists(ep_poster_abs) and meta.get("thumbnailUrl"):
                await download_and_cache_metadata_image(meta.get("thumbnailUrl"), ep_poster_abs)
                
            if backdrop_abs and os.path.exists(backdrop_abs) and not os.path.exists(ep_backdrop_abs):
                shutil.copy(backdrop_abs, ep_backdrop_abs)
            elif not os.path.exists(ep_backdrop_abs) and meta.get("bannerUrl"):
                await download_and_cache_metadata_image(meta.get("bannerUrl"), ep_backdrop_abs)
                
            ep_dur_str = ep_meta.get("duration", "45m")
            ep_skip_data = skip_markers or {}
            
            if not ep_entry:
                ep_entry = Episode(
                    id=ep_id, movie_id=show_id, episode_number=episode_num, season_number=season_num,
                    title=ep_title, description=ep_desc, thumbnail_url=ep_thumb_rel or "",
                    video_url=served_url, duration=ep_dur_str,
                    probed_duration=probed_duration, container=container, codec=codec,
                    width=width, height=height, frame_rate=frame_rate,
                    source_fingerprint=source_fingerprint
                )
                ep_entry.quality = ep_quality
                ep_entry.languages = ep_languages
                ep_entry.subtitles = subs_on_disk
                ep_entry.skip_markers = ep_skip_data
                ep_entry.audio_metadata = audio_meta_list
                db.add(ep_entry)
                logger.info(f"[Queue Manager] Cataloged new episode: S{season_num}E{episode_num}")
            else:
                ep_entry.title = ep_title
                ep_entry.description = ep_desc
                ep_entry.thumbnail_url = ep_thumb_rel or ep_entry.thumbnail_url
                ep_entry.video_url = served_url
                ep_entry.duration = ep_dur_str
                ep_entry.quality = ep_quality
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
                ep_entry.audio_metadata = audio_meta_list
                db.add(ep_entry)
                logger.info(f"[Queue Manager] Updated episode: S{season_num}E{episode_num}")

            show.tmdb_id = int(tmdb_id)
            show.catalog_source = "server"
            show.availability = "available"
            db.add(show)
            await db.commit()

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
                "language": language or "en",
                "languages": ep_languages,
                "quality": ep_quality,
                "subtitles": subs_on_disk,
                "original_language": language or "en",
                "skip_markers": ep_skip_data,
                "probed_duration": probed_duration,
                "container": container,
                "codec": codec,
                "width": width,
                "height": height,
                "frame_rate": frame_rate,
                "source_fingerprint": source_fingerprint,
                "audio_metadata": audio_meta_list
            }
            with open(ep_metadata_file, "w", encoding="utf-8") as f:
                json.dump(ep_metadata_content, f, indent=2, ensure_ascii=False)
                
            sync_compatible_metadata_file = os.path.join(ep_metadata_dir, f"metadata_s{season_num}_e{episode_num}.json")
            with open(sync_compatible_metadata_file, "w", encoding="utf-8") as f:
                json.dump(ep_metadata_content, f, indent=2, ensure_ascii=False)
            await self._schedule_playback_baseline(db, ep_entry)

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

    async def sync_media_from_disk(self):
        """
        Scans the media directory for metadata.json files and re-catalogs them into the database if they are missing.
        Also sweeps the database to verify path containment, localhost urls and file existence.
        """
        try:
            server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            media_dir = os.path.join(server_root, "media")
            
            # Sweep DB first to reconcile URLs and check actual file/cloud presence
            logger.info("[Queue Manager Recovery] Sweeping database to reconcile video URLs...")
            async with AsyncSession(engine) as db:
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
                        exists = await verify_media_exists(rel_path)
                        if not exists:
                            logger.warning(f"[Sync Recovery] Movie {m.title} file missing at {rel_path}. Demoting to cached.")
                            m.video_url = ""
                            m.availability = "cached"
                            m.catalog_source = "tmdb_cache"
                            db.add(m)
                        else:
                            m.catalog_source = "server"
                            m.availability = "available"
                            db.add(m)
                            
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
                        exists = await verify_media_exists(rel_path)
                        if not exists:
                            logger.warning(f"[Sync Recovery] Episode {ep.title} file missing at {rel_path}. Clearing video URL.")
                            ep.video_url = ""
                            db.add(ep)
                        else:
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
            
            async with AsyncSession(engine) as db:
                for root, dirs, files in os.walk(media_dir):
                    if os.path.basename(root) != ".metadata":
                        continue
                        
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
                                            
                                    if season is not None and episode is not None:
                                        if os.path.basename(parent_dir).startswith("Episode_"):
                                            ep_dir = parent_dir
                                        else:
                                            ep_dir = os.path.join(parent_dir, f"Season_{season}", f"Episode_{episode}")
                                            
                                        if os.path.exists(ep_dir):
                                            for item in os.listdir(ep_dir):
                                                if item.endswith((".mp4", ".mkv", ".avi", ".mov")):
                                                    video_file_rel = os.path.relpath(os.path.join(ep_dir, item), server_root)
                                                    break
                                else: # movie
                                    try:
                                        for item in os.listdir(parent_dir):
                                            if item.endswith((".mp4", ".mkv", ".avi", ".mov")) and not os.path.isdir(os.path.join(parent_dir, item)):
                                                video_file_rel = os.path.relpath(os.path.join(parent_dir, item), server_root)
                                                break
                                    except Exception:
                                        pass
                                        
                                if video_file_rel:
                                    file_path = video_file_rel.replace("\\", "/")
                                    abs_video_path = os.path.abspath(os.path.join(server_root, file_path))
                                    from services.audio_extractor import extract_audio_and_strip_video
                                    default_language = data.get("language") or data.get("original_language") or "en"
                                    extracted_langs = await extract_audio_and_strip_video(abs_video_path, default_lang=default_language)
                                    if extracted_langs:
                                        data["languages"] = extracted_langs
                                        try:
                                            with open(meta_path, "w", encoding="utf-8") as fw:
                                                json.dump(data, fw, indent=2, ensure_ascii=False)
                                        except Exception:
                                            pass
                                        
                                        if media_type == "movie":
                                            movie_id = f"m_{tmdb_id}"
                                            movie_obj = await db.get(Movie, movie_id)
                                            if movie_obj:
                                                movie_obj.languages = extracted_langs
                                                db.add(movie_obj)
                                                await db.commit()
                                        else:
                                            if season is not None and episode is not None:
                                                ep_id = f"ep_{tmdb_id}_s{season}_e{episode}"
                                                ep_obj = await db.get(Episode, ep_id)
                                                if ep_obj:
                                                    ep_obj.languages = extracted_langs
                                                    db.add(ep_obj)
                                                    await db.commit()

                                    # Probe completed file for rich media metadata if not present in the JSON
                                    if "source_fingerprint" not in data or not data.get("source_fingerprint"):
                                        probe_info = await probe_completed_media(abs_video_path)
                                        if probe_info:
                                            data.update(probe_info)
                                            try:
                                                with open(meta_path, "w", encoding="utf-8") as fw:
                                                    json.dump(data, fw, indent=2, ensure_ascii=False)
                                            except Exception:
                                                pass
                                else:
                                    file_path = None
                                        
                                language = data.get("language") or data.get("original_language")
                                
                                if media_type == "movie":
                                    movie_id = f"m_{tmdb_id}"
                                    existing = await db.get(Movie, movie_id)
                                    if existing and not (existing.title.startswith("Captured ") or "credentials are not set" in (existing.description or "")):
                                        # Update probed metadata even if it exists
                                        if file_path:
                                            abs_video_path = os.path.abspath(os.path.join(server_root, file_path))
                                            probe_info = await probe_completed_media(abs_video_path)
                                            existing.probed_duration = probe_info.get("probed_duration")
                                            existing.container = probe_info.get("container")
                                            existing.codec = probe_info.get("codec")
                                            existing.width = probe_info.get("width")
                                            existing.height = probe_info.get("height")
                                            existing.frame_rate = probe_info.get("frame_rate")
                                            existing.source_fingerprint = probe_info.get("source_fingerprint")
                                            existing.audio_metadata = probe_info.get("audio_metadata", [])
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
                                            # Update probed metadata even if it exists
                                            if file_path:
                                                abs_video_path = os.path.abspath(os.path.join(server_root, file_path))
                                                probe_info = await probe_completed_media(abs_video_path)
                                                existing.probed_duration = probe_info.get("probed_duration")
                                                existing.container = probe_info.get("container")
                                                existing.codec = probe_info.get("codec")
                                                existing.width = probe_info.get("width")
                                                existing.height = probe_info.get("height")
                                                existing.frame_rate = probe_info.get("frame_rate")
                                                existing.source_fingerprint = probe_info.get("source_fingerprint")
                                                existing.audio_metadata = probe_info.get("audio_metadata", [])
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
                                            continue
                                        logger.info(f"[Queue Manager Recovery] Restoring Series TMDB {tmdb_id} from {meta_path}...")
                                        meta = await tmdb_client.fetch_show_metadata(tmdb_id)
                                
                                if not file_path:
                                    data["catalog_source"] = "tmdb_cache"
                                    data["availability"] = "cached"
                                    data["video_url"] = ""
                                    try:
                                        with open(meta_path, "w", encoding="utf-8") as cache_meta_file:
                                            json.dump(data, cache_meta_file, indent=2, ensure_ascii=False)
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
                                    language=language, subtitles_list=data.get("subtitles"),
                                    quality=data.get("quality"), skip_markers=data.get("skip_markers")
                                )
                                count += 1
                                
                            except Exception as e:
                                logger.error(f"[Queue Manager] Error recovering {meta_path}: {e}")
                                
            logger.info(f"[Queue Manager] Disk sync complete. Recovered {count} entries.")
        except Exception as e:
            logger.error(f"[Queue Manager] Critical failure during sync_media_from_disk: {e}")

queue_manager = DownloadQueueManager()
