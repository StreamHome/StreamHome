import os
import time
import json
import shutil
import asyncio
import httpx
import aiofiles
from typing import Dict, Any, List, Optional
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from db import engine
from models import DownloadTask, Movie, Episode
from services.ffmpeg import download_and_merge, download_and_cache_metadata_image
from services.tmdb import tmdb_client
from config import settings
from services.logger import logger
from services.media_probe import probe_media_stream, notify_video_sender

class DownloadQueueManager:
    def __init__(self):
        self.loop_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.active_tasks = set()

    def start(self):
        if not self.is_running:
            self.is_running = True
            self.loop_task = asyncio.create_task(self._worker_loop())
            logger.info("[Queue Manager] Background worker loop started.")

    def stop(self):
        self.is_running = False
        if self.loop_task:
            self.loop_task.cancel()
            logger.info("[Queue Manager] Background worker loop stopped.")

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
                t = asyncio.create_task(self._process_task(task_id))
                t.add_done_callback(lambda fut, tid=task_id: self.active_tasks.discard(tid))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Queue Manager] Error in worker loop: {e}")

    async def run_rclone_move_dir(self, local_dir: str, remote_subpath: str) -> bool:
        rclone_path = shutil.which("rclone")
        if not rclone_path:
            workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            rclone_exe = "rclone.exe" if os.name == "nt" else "rclone"
            fallback_path = os.path.join(workspace_root, "bin", rclone_exe)
            if os.path.exists(fallback_path):
                rclone_path = fallback_path
                
        if not rclone_path:
            logger.error("[Queue Manager] Rclone binary not found. Cannot perform cloud upload.")
            return False
        
        target_remote = f"{settings.RCLONE_REMOTE_PATH}/{remote_subpath.replace('\\', '/')}"
        cmd = [rclone_path, "move", local_dir, target_remote, "--cleanup", "--retries", "3"]
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            return process.returncode == 0
        except Exception as e:
            logger.error(f"[Queue Manager] Rclone move subprocess exception: {e}")
            return False

    async def _process_task(self, task_id: str):
        logger.info(f"[Queue Manager] Processing task: {task_id}")
        
        try:
            async with AsyncSession(engine) as db:
                task = await db.get(DownloadTask, task_id)
                if not task:
                    self.active_tasks.discard(task_id)
                    from services.state import remove_task_metrics
                    remove_task_metrics(task_id)
                    return
                
                # Verileri ham olarak dışarı alıyoruz (Veritabanı çökmesini engellemek için)
                tmdb_id = task.tmdb_id
                media_type = task.media_type
                headers = task.headers
                video_url = task.video_url
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
                    raise Exception(f"Insufficient disk space on host: {free_gb:.2f} GB free (requires min 5.0 GB).")
            except Exception as e:
                logger.error(f"[Queue Manager] Disk check failed or space insufficient: {e}")
                raise

            # 2. Probe Media Stream
            logger.info(f"[Queue Manager] Probing media streams for task {task_id}...")
            probe_res = await probe_media_stream(video_url, audio_url, headers)
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

            # 3. Notify Video Sender API (Non-blocking background task)
            asyncio.create_task(notify_video_sender(
                task_id=task_id,
                tmdb_id=tmdb_id,
                has_video=has_video,
                has_audio=has_audio,
                quality=scan_quality
            ))

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
                    except Exception:
                        pass

            # 4. Retry Loop for Download & Merge
            max_retries = 3
            success = False
            last_error = "Unknown download failure."
            extracted_languages = []
            
            for attempt in range(1, max_retries + 1):
                logger.info(f"[Queue Manager] Starting download/merge (Attempt {attempt}/{max_retries}) for task {task_id}...")
                if attempt > 1:
                    from services.state import update_task_metrics
                    update_task_metrics(task_id, 0.0, speed=f"Retrying ({attempt}/{max_retries})...", eta="00:00:00")
                
                # download_and_merge returns (bool, str) representing (success, error_reason)
                success, error_reason = await download_and_merge(
                    task_id=task_id, video_url=video_url, audio_url=audio_url,
                    headers=headers, output_path=output_abs_path, duration_secs=duration_secs
                )
                
                if success:
                    break
                
                last_error = error_reason or "FFmpeg process failed."
                logger.warning(f"[Queue Manager] Ingestion attempt {attempt} failed for task {task_id}: {last_error}")
                
                if attempt < max_retries:
                    backoff = 5 * (2 ** (attempt - 1))
                    logger.info(f"[Queue Manager] Backing off for {backoff} seconds before next retry...")
                    await asyncio.sleep(backoff)

            rclone_success = True
            if success:
                # Run audio extraction and video stripping
                from services.audio_extractor import extract_audio_and_strip_video
                extracted_languages = await extract_audio_and_strip_video(output_abs_path)
                
                if settings.STORAGE_ENGINE == "CLOUD":
                    async with AsyncSession(engine) as db:
                        task_db = await db.get(DownloadTask, task_id)
                        if task_db:
                            task_db.status = "MOVING_CLOUD"
                            db.add(task_db)
                            await db.commit()
                
                from services.state import update_task_metrics
                update_task_metrics(task_id, 99.9, speed="Uploading", eta="00:00:00")
                
                local_dir = os.path.dirname(output_abs_path)
                remote_subpath = os.path.join("Movies" if media_type == "movie" else "Series", folder_name)
                
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
                            # Treat fallback success as completion
                            rclone_success = True
                    except Exception as fallback_err:
                        logger.error(f"[Queue Manager] Local fallback copy failed for task {task_id}: {fallback_err}")

            async with AsyncSession(engine) as db:
                task = await db.get(DownloadTask, task_id)
                if not task:
                    self.active_tasks.discard(task_id)
                    from services.state import remove_task_metrics
                    remove_task_metrics(task_id)
                    return
                    
                if success and rclone_success:
                    task.status = "COMPLETED"
                    task.error_message = None
                    db.add(task)
                    await db.commit()
                    
                    try:
                        await self._catalog_media(db, tmdb_id, media_type, season, episode, meta, output_rel_path, language, subtitles_list, quality)
                    except Exception as cat_err:
                        logger.error(f"[Queue Manager] Error cataloging media: {cat_err}")
                else:
                    task.status = "FAILED"
                    task.error_message = last_error if not rclone_success else "Cloud upload (rclone) failed."
                    db.add(task)
                    await db.commit()
                    
        except Exception as err:
            logger.error(f"[Queue Manager] Silent background exception caught for task {task_id}: {err}")
            try:
                async with AsyncSession(engine) as db:
                    task = await db.get(DownloadTask, task_id)
                    if task:
                        task.status = "FAILED"
                        task.error_message = str(err)
                        db.add(task)
                        await db.commit()
            except Exception as db_err:
                logger.error(f"[Queue Manager] Failed to update crashed task {task_id} state to FAILED: {db_err}")
                
        finally:
            self.active_tasks.discard(task_id)
        from services.state import remove_task_metrics
        remove_task_metrics(task_id)

    async def _catalog_media(
        self, db: AsyncSession, tmdb_id: int, media_type: str, season: Optional[int], 
        episode: Optional[int], meta: Dict[str, Any], file_path: str, 
        language: Optional[str] = None, subtitles_list: Optional[List[Dict[str, str]]] = None,
        quality: Optional[str] = None
    ):
        server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        
        # Get correct folder name
        raw_title = meta.get("title", f"Media_{tmdb_id}")
        clean_title = "".join(c for c in raw_title if c.isalnum() or c in " .-_")
        
        if media_type == "movie":
            release_year = meta.get("releaseYear", 2026)
            correct_folder_name = f"{clean_title}_{release_year}_TMDB_{tmdb_id}"
            parent_dir_rel = "media/Movies"
        else:
            correct_folder_name = f"{clean_title}_TMDB_{tmdb_id}"
            parent_dir_rel = "media/Series"
            
        # Parse current folder name from file_path
        parts = file_path.replace("\\", "/").split("/")
        current_folder_name = None
        if media_type == "movie" and len(parts) >= 2:
            current_folder_name = parts[-2]
        elif (media_type == "series" or media_type == "tv") and len(parts) >= 4:
            current_folder_name = parts[-4]
            
        if current_folder_name and current_folder_name != correct_folder_name:
            old_folder_abs = os.path.abspath(os.path.join(server_root, parent_dir_rel, current_folder_name))
            new_folder_abs = os.path.abspath(os.path.join(server_root, parent_dir_rel, correct_folder_name))
            
            if os.path.exists(old_folder_abs) and not os.path.exists(new_folder_abs):
                print(f"[Queue Manager] Renaming placeholder folder on disk: {current_folder_name} -> {correct_folder_name}")
                try:
                    os.rename(old_folder_abs, new_folder_abs)
                    file_path = file_path.replace(current_folder_name, correct_folder_name)
                    # For movies, also rename the filename to match the corrected title
                    if media_type == "movie":
                        old_file_name = parts[-1]
                        new_file_name = f"{clean_title}_{release_year}.mp4"
                        old_file_abs = os.path.join(new_folder_abs, old_file_name)
                        new_file_abs = os.path.join(new_folder_abs, new_file_name)
                        if os.path.exists(old_file_abs) and not os.path.exists(new_file_abs):
                            os.rename(old_file_abs, new_file_abs)
                            file_path = os.path.join(parent_dir_rel, correct_folder_name, new_file_name).replace("\\", "/")
                except Exception as e:
                    print(f"[Queue Manager] Error renaming folder: {e}")

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
        
        # Save poster.jpg and backdrop.jpg directly to the Movie / Series folder
        poster_rel = os.path.join(main_folder_rel, "poster.jpg")
        backdrop_rel = os.path.join(main_folder_rel, "backdrop.jpg")
        poster_abs = os.path.abspath(os.path.join(server_root, poster_rel))
        backdrop_abs = os.path.abspath(os.path.join(server_root, backdrop_rel))
        
        local_thumbnail = await download_and_cache_metadata_image(meta.get("thumbnailUrl"), poster_abs)
        local_banner = await download_and_cache_metadata_image(meta.get("bannerUrl"), backdrop_abs)
        
        if media_type == "movie":
            movie_id = f"m_{tmdb_id}"

            # Prepare metadata values for both DB and metadata.json
            movie_folder_abs = os.path.dirname(abs_file_path)
            movie_quality = quality or "Source"
            movie_languages = extracted_languages if extracted_languages else ([language] if language else ["en"])
            subs_on_disk = []
            if subtitles_list:
                for sub in subtitles_list:
                    sub_lang = sub.get("language", "en")
                    for ext in [".vtt", ".srt"]:
                        sub_file_name = f"subtitle_{sub_lang}{ext}"
                        if os.path.exists(os.path.join(movie_folder_abs, sub_file_name)):
                            subs_on_disk.append({
                                "language": sub_lang,
                                "ext": ext
                            })
                            break

            movie = await db.get(Movie, movie_id)
            if not movie:
                movie = Movie(
                    id=movie_id, title=meta.get("title", f"Movie {tmdb_id}"), description=meta.get("description", ""),
                    thumbnail_url=local_thumbnail or "", banner_url=local_banner or "", video_url=served_url,
                    duration=meta.get("duration", "1h 30m"), release_year=meta.get("releaseYear", 2026),
                    rating=meta.get("rating", "PG-13"), director=meta.get("director", "Unknown"),
                    original_language=language or meta.get("originalLanguage", "en"), type="movie",
                    vote_average=meta.get("vote_average", 7.5), vote_count=meta.get("vote_count", 100)
                )
                movie.genres = meta.get("genres", [])
                movie.cast = meta.get("cast", [])
                movie.quality = movie_quality
                movie.languages = movie_languages
                movie.subtitles = subs_on_disk
                db.add(movie)
                await db.commit()
                print(f"[Queue Manager] Cataloged new movie: {meta.get('title', 'Unknown Movie')}")
            else:
                movie.title = meta.get("title", movie.title)
                movie.description = meta.get("description", movie.description)
                movie.thumbnail_url = local_thumbnail or movie.thumbnail_url
                movie.banner_url = local_banner or movie.banner_url
                movie.video_url = served_url
                movie.duration = meta.get("duration", movie.duration)
                movie.release_year = meta.get("releaseYear", movie.release_year)
                movie.rating = meta.get("rating", movie.rating)
                movie.director = meta.get("director", movie.director)
                movie.cast = meta.get("cast", movie.cast)
                movie.genres = meta.get("genres", movie.genres)
                movie.original_language = language or meta.get("originalLanguage", movie.original_language)
                movie.quality = movie_quality
                movie.languages = movie_languages
                movie.subtitles = subs_on_disk
                movie.vote_average = meta.get("vote_average", movie.vote_average)
                movie.vote_count = meta.get("vote_count", movie.vote_count)
                db.add(movie)
                await db.commit()
                print(f"[Queue Manager] Updated existing movie details: {movie.title}")

            # Create movie's local .metadata/metadata.json
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
                "language": language or "en",
                "languages": movie_languages,
                "quality": movie_quality,
                "subtitles": subs_on_disk,
                "original_language": language or "en"
            }
            with open(movie_metadata_file, "w", encoding="utf-8") as f:
                json.dump(movie_metadata_content, f, indent=2, ensure_ascii=False)
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
                    vote_average=meta.get("vote_average", 7.5), vote_count=meta.get("vote_count", 100)
                )
                show.genres = meta.get("genres", [])
                show.cast = meta.get("cast", [])
                db.add(show)
                await db.commit()
                print(f"[Queue Manager] Cataloged new TV show: {meta.get('title', 'Unknown Show')}")
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
                print(f"[Queue Manager] Updated TV show: {show.title}")
                
            season_num = season or 1
            episode_num = episode or 1

            ep_quality = quality or "Source"
            ep_languages = extracted_languages if extracted_languages else ([language] if language else ["en"])

            ep_folder_abs = os.path.dirname(abs_file_path)
            subs_on_disk = []
            if subtitles_list:
                for sub in subtitles_list:
                    sub_lang = sub.get("language", "en")
                    for ext in [".vtt", ".srt"]:
                        sub_file_name = f"subtitle_{sub_lang}{ext}"
                        if os.path.exists(os.path.join(ep_folder_abs, sub_file_name)):
                            subs_on_disk.append({
                                "language": sub_lang,
                                "ext": ext
                            })
                            break
            
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

            # Save poster, backdrop, and still image inside each episode folder
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
                
            if not ep_entry:
                ep_entry = Episode(
                    id=ep_id, movie_id=show_id, episode_number=episode_num, season_number=season_num,
                    title=ep_title, description=ep_desc, thumbnail_url=ep_thumb_rel or "",
                    video_url=served_url, duration=ep_meta.get("duration", "45m")
                )
                ep_entry.quality = ep_quality
                ep_entry.languages = ep_languages
                ep_entry.subtitles = subs_on_disk
                db.add(ep_entry)
                await db.commit()
                print(f"[Queue Manager] Cataloged new episode: S{season_num}E{episode_num}")
            else:
                ep_entry.title = ep_title
                ep_entry.description = ep_desc
                ep_entry.thumbnail_url = ep_thumb_rel or ep_entry.thumbnail_url
                ep_entry.video_url = served_url
                ep_entry.duration = ep_meta.get("duration", ep_entry.duration)
                ep_entry.quality = ep_quality
                ep_entry.languages = ep_languages
                ep_entry.subtitles = subs_on_disk
                db.add(ep_entry)
                await db.commit()
                print(f"[Queue Manager] Updated episode: S{season_num}E{episode_num}")

            # Create episode's local .metadata/metadata.json
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
                "language": language or "en",
                "languages": ep_languages,
                "quality": ep_quality,
                "subtitles": subs_on_disk,
                "original_language": language or "en"
            }
            with open(ep_metadata_file, "w", encoding="utf-8") as f:
                json.dump(ep_metadata_content, f, indent=2, ensure_ascii=False)
                
            # For compatibility with older sync mechanisms expecting files starting with metadata_s
            sync_compatible_metadata_file = os.path.join(ep_metadata_dir, f"metadata_s{season_num}_e{episode_num}.json")
            with open(sync_compatible_metadata_file, "w", encoding="utf-8") as f:
                json.dump(ep_metadata_content, f, indent=2, ensure_ascii=False)

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
        This acts as an automatic recovery system in case the database is deleted.
        """
        try:
            server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            media_dir = os.path.join(server_root, "media")
            
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
                                    # 1. Try to get from json data keys first
                                    if data.get("season") is not None and data.get("episode") is not None:
                                        season = int(data.get("season"))
                                        episode = int(data.get("episode"))
                                    # 2. Try parsing filename metadata_sX_eY.json
                                    elif file.startswith("metadata_s") and "_e" in file:
                                        try:
                                            parts = file.replace("metadata_s", "").replace(".json", "").split("_e")
                                            season = int(parts[0])
                                            episode = int(parts[1])
                                        except Exception:
                                            pass
                                    # 3. Try parsing from root folder if inside Episode / Season folder
                                    if season is None or episode is None:
                                        try:
                                            root_parts = root.replace("\\", "/").split("/")
                                            # If root is inside an Episode folder like .../Season_1/Episode_2/.metadata
                                            if len(root_parts) >= 3 and root_parts[-3].startswith("Season_") and root_parts[-2].startswith("Episode_"):
                                                season = int(root_parts[-3].replace("Season_", ""))
                                                episode = int(root_parts[-2].replace("Episode_", ""))
                                        except Exception:
                                            pass
                                            
                                    if season is not None and episode is not None:
                                        # Determine the episode directory where video file should reside
                                        # Style A: .metadata is inside the Episode folder itself
                                        if os.path.basename(parent_dir).startswith("Episode_"):
                                            ep_dir = parent_dir
                                        else:
                                            # Style B: .metadata is in the main show folder
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
                                    # Always check and extract audio if not yet done
                                    abs_video_path = os.path.abspath(os.path.join(server_root, file_path))
                                    from services.audio_extractor import extract_audio_and_strip_video
                                    extracted_langs = await extract_audio_and_strip_video(abs_video_path)
                                    if extracted_langs:
                                        data["languages"] = extracted_langs
                                        try:
                                            with open(meta_path, "w", encoding="utf-8") as fw:
                                                json.dump(data, fw, indent=2, ensure_ascii=False)
                                        except Exception:
                                            pass
                                        
                                        # Also update the existing DB records if they are already in the DB
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
                                else:
                                    # If no physical video file exists on disk, do not restore this placeholder/discovery cache
                                    continue
                                        
                                language = data.get("language") or data.get("original_language")
                                
                                # Recheck database to avoid unnecessary re-cataloging if already exists
                                if media_type == "movie":
                                    movie_id = f"m_{tmdb_id}"
                                    existing = await db.get(Movie, movie_id)
                                    if existing and not (existing.title.startswith("Captured ") or "credentials are not set" in (existing.description or "")):
                                        continue
                                    logger.info(f"[Queue Manager Recovery] Restoring Movie TMDB {tmdb_id} from {meta_path}...")
                                    meta = await tmdb_client.fetch_movie_metadata(tmdb_id)
                                    
                                else: # series
                                    if season is not None and episode is not None:
                                        ep_id = f"ep_{tmdb_id}_s{season}_e{episode}"
                                        existing = await db.get(Episode, ep_id)
                                        if existing and not (existing.title.startswith("Episode ") or "stream." in (existing.description or "")):
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
                                
                                # Re-run cataloging (which will map original files and cache settings correctly)
                                await self._catalog_media(db, tmdb_id, media_type, season, episode, meta, file_path, language)
                                count += 1
                                
                            except Exception as e:
                                logger.error(f"[Queue Manager] Error recovering {meta_path}: {e}")
                                
            logger.info(f"[Queue Manager] Disk sync complete. Recovered {count} entries.")
        except Exception as e:
            logger.error(f"[Queue Manager] Critical failure during sync_media_from_disk: {e}")

queue_manager = DownloadQueueManager()