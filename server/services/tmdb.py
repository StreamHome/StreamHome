import httpx
import os
import json
import asyncio
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import unquote, urlparse
from config import settings
from services.logger import logger
from services.vibe_analysis import compute_trope_vectors, relevant_crew

GENRES_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics"
}

class TMDBClient:
    def __init__(self):
        self.api_key = settings.TMDB_API_KEY
        self.read_access_token = settings.TMDB_READ_ACCESS_TOKEN
        self.base_url = "https://api.themoviedb.org/3"
        self._cache_semaphore = asyncio.Semaphore(2)
        self._img_semaphore = asyncio.Semaphore(4)
        self._request_semaphore = asyncio.Semaphore(4)
        self._client: Optional[httpx.AsyncClient] = None
        self._cache_queue: Optional[asyncio.Queue] = None
        self._cache_pending: set[str] = set()
        self._cache_workers: List[asyncio.Task] = []
        self._cache_retry_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _local_artwork_exists(url: Optional[str]) -> bool:
        """Resolve only contained /media URLs; never trust a database path directly."""
        if not url or not url.startswith("/media/"):
            return False
        relative = unquote(urlparse(url).path[len("/media/"):]).replace("/", os.sep)
        media_root = Path(settings.MEDIA_DIR).resolve()
        candidate = (media_root / relative).resolve()
        try:
            candidate.relative_to(media_root)
        except ValueError:
            return False
        return candidate.is_file()

    def _reconcile_cached_artwork(self, movie) -> bool:
        """Drop stale local pointers while retaining remote artwork as the live fallback."""
        changed = False
        thumbnail_candidate = movie.local_thumbnail_url or (movie.thumbnail_url if movie.thumbnail_url.startswith("/media/") else None)
        banner_candidate = movie.local_banner_url or (movie.banner_url if (movie.banner_url or "").startswith("/media/") else None)
        missing_thumbnail = bool(thumbnail_candidate) and not self._local_artwork_exists(thumbnail_candidate)
        missing_banner = bool(banner_candidate) and not self._local_artwork_exists(banner_candidate)
        if missing_thumbnail:
            movie.local_thumbnail_url = None
            if movie.thumbnail_url.startswith("/media/"):
                movie.thumbnail_url = movie.remote_thumbnail_url or ""
            changed = True
        if missing_banner:
            movie.local_banner_url = None
            if (movie.banner_url or "").startswith("/media/"):
                movie.banner_url = movie.remote_banner_url or ""
            changed = True
        return changed

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def start_cache_workers(self):
        if self._cache_workers:
            return
        from sqlmodel import select
        from sqlmodel.ext.asyncio.session import AsyncSession
        from db import engine
        from models import Movie

        self._cache_queue = asyncio.Queue()
        async with AsyncSession(engine) as db:
            result = await db.exec(select(Movie).where(Movie.catalog_source == "tmdb_cache"))
            now = time.time()
            resumable_ids: List[str] = []
            deferred: List[tuple[str, float]] = []
            reconciled = 0
            for movie in result.all():
                stale_artwork = self._reconcile_cached_artwork(movie)
                if stale_artwork:
                    reconciled += 1
                interrupted = movie.cache_state in {"queued", "caching"}
                retry_due = (
                    movie.cache_state == "error"
                    and int(movie.cache_retry_count or 0) < 6
                    and (movie.cache_next_retry_at is None or movie.cache_next_retry_at <= now)
                )
                retry_later = (
                    movie.cache_state == "error"
                    and int(movie.cache_retry_count or 0) < 6
                    and movie.cache_next_retry_at is not None
                    and movie.cache_next_retry_at > now
                )
                missing_remote_cache = stale_artwork and bool(movie.remote_thumbnail_url or movie.remote_banner_url)
                if interrupted or retry_due or missing_remote_cache:
                    movie.cache_state = "queued"
                    resumable_ids.append(movie.id)
                elif retry_later:
                    deferred.append((movie.id, movie.cache_next_retry_at - now))
                db.add(movie)
            if resumable_ids or reconciled:
                await db.commit()
        self._cache_workers = [asyncio.create_task(self._cache_worker(index)) for index in range(2)]
        for movie_id in resumable_ids:
            await self.enqueue_cache(movie_id)
        for movie_id, delay in deferred:
            self._schedule_cache_retry(movie_id, delay)
        if reconciled or resumable_ids:
            logger.info(
                f"[TMDB Cache] Reconciled {reconciled} cached artwork records; "
                f"queued {len(resumable_ids)} resumable jobs."
            )

    async def stop_cache_workers(self):
        workers, self._cache_workers = self._cache_workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        retry_tasks, self._cache_retry_tasks = self._cache_retry_tasks, set()
        for task in retry_tasks:
            task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        self._cache_pending.clear()
        self._cache_queue = None

    async def enqueue_cache(self, movie_id: str):
        if not self._cache_queue or movie_id in self._cache_pending:
            return
        self._cache_pending.add(movie_id)
        await self._cache_queue.put(movie_id)

    def _schedule_cache_retry(self, movie_id: str, delay: float) -> None:
        async def delayed_enqueue() -> None:
            await asyncio.sleep(max(0.0, delay))
            await self.enqueue_cache(movie_id)

        task = asyncio.create_task(delayed_enqueue(), name=f"tmdb-cache-retry-{movie_id}")
        self._cache_retry_tasks.add(task)
        task.add_done_callback(self._cache_retry_tasks.discard)

    async def _cache_worker(self, worker_index: int):
        assert self._cache_queue is not None
        while True:
            movie_id = await self._cache_queue.get()
            try:
                for attempt in range(3):
                    try:
                        await self._cache_movie_record(movie_id)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        if attempt == 2:
                            retry_delay = await self._mark_cache_error(movie_id, error)
                            if retry_delay is not None:
                                self._schedule_cache_retry(movie_id, retry_delay)
                            logger.error(f"[TMDB Cache Worker {worker_index}] {movie_id} failed: {error}")
                        else:
                            await asyncio.sleep(0.75 * (2 ** attempt))
            finally:
                self._cache_pending.discard(movie_id)
                self._cache_queue.task_done()

    async def _mark_cache_error(self, movie_id: str, error: Exception) -> Optional[float]:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from db import engine
        from models import Movie
        async with AsyncSession(engine) as db:
            movie = await db.get(Movie, movie_id)
            if movie and movie.catalog_source == "tmdb_cache":
                retry_count = int(movie.cache_retry_count or 0) + 1
                retry_delay = min(24 * 60 * 60, 15 * 60 * (2 ** max(0, retry_count - 1)))
                movie.cache_state = "error"
                movie.cache_retry_count = retry_count
                movie.cache_next_retry_at = time.time() + retry_delay if retry_count < 6 else None
                movie.cache_last_error = f"{type(error).__name__}: {error}"[:500]
                db.add(movie)
                await db.commit()
                return retry_delay if retry_count < 6 else None
        return None

    async def _cache_movie_record(self, movie_id: str):
        from sqlmodel.ext.asyncio.session import AsyncSession
        from db import engine
        from models import Movie
        async with AsyncSession(engine) as db:
            movie = await db.get(Movie, movie_id)
            if not movie or movie.catalog_source != "tmdb_cache":
                return
            movie.cache_state = "caching"
            item = {
                "tmdb_id": movie.tmdb_id,
                "title": movie.title,
                "description": movie.description,
                "genres": movie.genres,
                "duration": movie.duration,
                "release_year": movie.release_year,
                "rating": movie.rating,
                "cast": movie.cast,
                "director": movie.director,
                "crew": movie.crew,
                "type": movie.type,
                "vote_average": movie.vote_average,
                "vote_count": movie.vote_count,
                "popularity": movie.popularity,
                "original_language": movie.original_language,
                "keywords": movie.keywords,
                "collection_name": movie.collection_name,
                "trope_vectors": movie.trope_vectors,
            }
            poster = movie.remote_thumbnail_url or (movie.thumbnail_url if movie.thumbnail_url.startswith("http") else "")
            backdrop = movie.remote_banner_url or (movie.banner_url if movie.banner_url and movie.banner_url.startswith("http") else "")
            db.add(movie)
            await db.commit()
        await self.cache_media_locally(item, poster, backdrop)

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if not self.api_key and not self.read_access_token:
            return None
        
        if params is None:
            params = {}
        
        # Build request headers and params based on available credentials
        headers = {"Accept": "application/json"}
        if self.read_access_token:
            # v4 Bearer Token authentication (preferred)
            headers["Authorization"] = f"Bearer {self.read_access_token}"
        else:
            # v3 API key authentication (legacy fallback)
            params["api_key"] = self.api_key
        
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        async with self._request_semaphore:
            for attempt in range(3):
                try:
                    response = await self._client.get(f"{self.base_url}{path}", params=params, headers=headers)
                    if response.status_code == 200:
                        return response.json()
                    if response.status_code == 429:
                        retry_after = min(30.0, max(1.0, float(response.headers.get("Retry-After", "2"))))
                        await asyncio.sleep(retry_after)
                        continue
                    if response.status_code >= 500 and attempt < 2:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    logger.error(f"[TMDB Client] API Error {response.status_code}: {response.text[:500]}")
                    return None
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    logger.error(f"[TMDB Client] Exception querying TMDB: {e}")
                except Exception as e:
                    logger.error(f"[TMDB Client] Exception querying TMDB: {e}")
                    return None
        return None

    async def fetch_movie_metadata(self, tmdb_id: int) -> Dict[str, Any]:
        """Fetch movie details and crew credits from TMDB."""
        data = await self._get(f"/movie/{tmdb_id}", params={"append_to_response": "credits,release_dates,keywords"})
        
        if not data:
            # High-quality fallback metadata
            return {
                "title": f"Captured Movie (TMDB ID: {tmdb_id})",
                "description": "Auto-ingested movie stream. Full description is unavailable because TMDB API credentials are not set or the media was not found.",
                "thumbnailUrl": "",
                "bannerUrl": "",
                "genres": ["Action", "Sci-Fi"],
                "duration": "1h 30m",
                "releaseYear": 2026,
                "rating": "PG-13",
                "director": "Unknown Director",
                "crew": [],
                "cast": ["Unknown Actor"],
                "vote_average": 7.5,
                "vote_count": 100
            }

        # Parse duration
        runtime = data.get("runtime", 0)
        duration_str = f"{runtime // 60}h {runtime % 60}m" if runtime else "1h 45m"
        
        # Parse release year
        release_date = data.get("release_date", "")
        release_year = int(release_date.split("-")[0]) if release_date else 2026

        # Parse genres
        genres = [g.get("name") for g in data.get("genres", [])]
        if not genres:
            genres = ["General"]

        # Parse poster and backdrop
        poster_path = data.get("poster_path")
        backdrop_path = data.get("backdrop_path")
        thumbnail_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
        banner_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else ""

        # Parse US certification rating
        rating = "PG-13"
        release_results = data.get("release_dates", {}).get("results", [])
        for res in release_results:
            if res.get("iso_3166_1") == "US":
                for release_date_item in res.get("release_dates", []):
                    cert = release_date_item.get("certification")
                    if cert:
                        rating = cert
                        break

        # Parse directors, writers, screenplay credits, and cast.
        director = "Unknown Director"
        cast_list = []
        credits = data.get("credits", {})
        
        if credits:
            # Director
            for crew_member in credits.get("crew", []):
                if crew_member.get("job") == "Director":
                    director = crew_member.get("name")
                    break
            # Cast
            cast_list = [actor.get("name") for actor in credits.get("cast", [])[:5]]
        crew = relevant_crew(credits)

        return {
            "title": data.get("title") or f"Movie {tmdb_id}",
            "description": data.get("overview") or "No overview available.",
            "thumbnailUrl": thumbnail_url,
            "bannerUrl": banner_url,
            "genres": genres,
            "duration": duration_str,
            "releaseYear": release_year,
            "rating": rating,
            "director": director,
            "crew": crew,
            "cast": cast_list or ["Unknown Actor"],
            "originalLanguage": data.get("original_language", "en"),
            "vote_average": data.get("vote_average", 7.5),
            "vote_count": data.get("vote_count", 100),
            "keywords": [item.get("name") for item in data.get("keywords", {}).get("keywords", [])[:12] if item.get("name")],
            "collectionName": (data.get("belongs_to_collection") or {}).get("name"),
            "tropeVectors": compute_trope_vectors(genres, [item.get("name") for item in data.get("keywords", {}).get("keywords", []) if item.get("name")], data.get("overview") or "")
        }

    async def fetch_show_metadata(self, tmdb_id: int) -> Dict[str, Any]:
        """Fetch TV Show metadata."""
        data = await self._get(f"/tv/{tmdb_id}", params={"append_to_response": "credits,content_ratings,keywords"})
        
        if not data:
            # Fallback
            return {
                "title": f"Captured Show (TMDB ID: {tmdb_id})",
                "description": "Auto-ingested TV series stream. Full description is unavailable because TMDB API credentials are not set.",
                "thumbnailUrl": "",
                "bannerUrl": "",
                "genres": ["Drama", "Mystery"],
                "duration": "45m",
                "releaseYear": 2026,
                "rating": "TV-MA",
                "director": "Various Directors",
                "crew": [],
                "cast": ["Cast Member"],
                "vote_average": 7.5,
                "vote_count": 100
            }

        first_air_date = data.get("first_air_date", "")
        release_year = int(first_air_date.split("-")[0]) if first_air_date else 2026
        genres = [g.get("name") for g in data.get("genres", [])]
        if not genres:
            genres = ["Drama"]

        poster_path = data.get("poster_path")
        backdrop_path = data.get("backdrop_path")
        thumbnail_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
        banner_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else ""

        # Episode duration estimate
        episode_run_times = data.get("episode_run_time", [])
        avg_run_time = episode_run_times[0] if episode_run_times else 45
        duration_str = f"{avg_run_time}m"

        # Parse US TV content rating
        rating = "TV-14"
        rating_results = data.get("content_ratings", {}).get("results", [])
        for res in rating_results:
            if res.get("iso_3166_1") == "US":
                rating = res.get("rating", rating)
                break

        cast_list = []
        credits = data.get("credits", {})
        if credits:
            cast_list = [actor.get("name") for actor in credits.get("cast", [])[:5]]

        created_by = data.get("created_by", [])
        director = created_by[0].get("name") if created_by else "Unknown Creator"
        crew = relevant_crew(credits, created_by)

        return {
            "title": data.get("name") or f"Show {tmdb_id}",
            "description": data.get("overview") or "No overview available.",
            "thumbnailUrl": thumbnail_url,
            "bannerUrl": banner_url,
            "genres": genres,
            "duration": duration_str,
            "releaseYear": release_year,
            "rating": rating,
            "director": director,
            "crew": crew,
            "cast": cast_list or ["Unknown Actor"],
            "originalLanguage": data.get("original_language", "en"),
            "vote_average": data.get("vote_average", 7.5),
            "vote_count": data.get("vote_count", 100),
            "keywords": [item.get("name") for item in data.get("keywords", {}).get("results", [])[:12] if item.get("name")],
            "collectionName": None,
            "tropeVectors": compute_trope_vectors(genres, [item.get("name") for item in data.get("keywords", {}).get("results", []) if item.get("name")], data.get("overview") or "")
        }

    async def fetch_episode_metadata(self, tmdb_id: int, season: int, episode: int) -> Dict[str, Any]:
        """Fetch TV Show episode specific metadata."""
        data = await self._get(f"/tv/{tmdb_id}/season/{season}/episode/{episode}")
        
        if not data:
            # Fallback
            return {
                "title": f"Episode {episode}",
                "description": f"Season {season}, Episode {episode} stream.",
                "thumbnailUrl": "",
                "duration": "45m"
            }

        still_path = data.get("still_path")
        thumbnail_url = f"https://image.tmdb.org/t/p/w300{still_path}" if still_path else ""

        runtime = data.get("runtime", 0)
        duration_str = f"{runtime}m" if runtime else "45m"

        return {
            "title": data.get("name") or f"Episode {episode}",
            "description": data.get("overview") or f"Season {season}, Episode {episode} description.",
            "thumbnailUrl": thumbnail_url,
            "duration": duration_str
        }

    async def cache_media_locally(self, item_dict: Dict[str, Any], raw_poster_url: str, raw_backdrop_url: str):
        """Cache truthful title metadata/artwork in the existing media tree, never video."""
        async with self._cache_semaphore:
            try:
                import time
                from sqlmodel.ext.asyncio.session import AsyncSession
                from db import engine
                from models import Movie
                from services.ffmpeg import download_and_cache_metadata_image

                tmdb_id = int(item_dict.get("tmdb_id"))
                media_type = "series" if item_dict.get("type") in ("series", "tv") else "movie"
                if not item_dict.get("cast") or not item_dict.get("crew") or not item_dict.get("keywords") or (item_dict.get("director") or "").casefold() in {"", "various", "unknown"}:
                    details = await (self.fetch_show_metadata(tmdb_id) if media_type == "series" else self.fetch_movie_metadata(tmdb_id))
                    item_dict = {**item_dict, **{key: value for key, value in details.items() if value not in (None, "", [])}}
                title = item_dict.get("title", f"Media_{tmdb_id}")
                release_year = int(item_dict.get("release_year") or 0)
                movie_id = f"tv_{tmdb_id}" if media_type == "series" else f"m_{tmdb_id}"
                async with AsyncSession(engine) as db:
                    existing_server_media = await db.get(Movie, movie_id)
                    if existing_server_media and existing_server_media.catalog_source == "server":
                        return movie_id
                clean_title = "".join(c for c in title if c.isalnum() or c in " .-_")
                server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                library_name = "Series" if media_type == "series" else "Movies"
                library_abs = os.path.join(server_root, settings.MEDIA_DIR, library_name)
                suffix = f"_TMDB_{tmdb_id}"
                matching = next((name for name in os.listdir(library_abs) if name.endswith(suffix) and os.path.isdir(os.path.join(library_abs, name))), None) if os.path.isdir(library_abs) else None
                folder_name = matching or (f"{clean_title}_TMDB_{tmdb_id}" if media_type == "series" else f"{clean_title}_{release_year}_TMDB_{tmdb_id}")
                folder_rel = os.path.join(settings.MEDIA_DIR, library_name, folder_name)
                folder_abs = os.path.abspath(os.path.join(server_root, folder_rel))
                metadata_dir = os.path.join(folder_abs, ".metadata")
                os.makedirs(metadata_dir, exist_ok=True)

                poster_abs = os.path.join(folder_abs, "poster.jpg")
                backdrop_abs = os.path.join(folder_abs, "backdrop.jpg")

                async def safe_download(url: str, destination: str):
                    if not url or not url.startswith("http") or os.path.exists(destination):
                        return
                    async with self._img_semaphore:
                        await download_and_cache_metadata_image(url, destination)

                await asyncio.gather(
                    safe_download(raw_poster_url, poster_abs),
                    safe_download(raw_backdrop_url, backdrop_abs),
                )
                missing_assets = [
                    label for label, url, path in (
                        ("poster", raw_poster_url, poster_abs),
                        ("backdrop", raw_backdrop_url, backdrop_abs),
                    ) if url and not os.path.exists(path)
                ]
                if missing_assets:
                    raise RuntimeError(f"TMDB artwork download failed: {', '.join(missing_assets)}")

                metadata_content = {
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "title": title,
                    "description": item_dict.get("description", ""),
                    "release_year": release_year,
                    "genres": item_dict.get("genres", []),
                    "cast": item_dict.get("cast", []),
                    "director": item_dict.get("director"),
                    "crew": item_dict.get("crew", []),
                    "original_language": item_dict.get("original_language", "en"),
                    "catalog_source": "tmdb_cache",
                    "availability": "cached",
                    "video_url": "",
                    "quality": item_dict.get("quality", "Source"),
                    "languages": item_dict.get("languages", []),
                    "subtitles": item_dict.get("subtitles", []),
                    "keywords": item_dict.get("keywords", []),
                    "collection_name": item_dict.get("collection_name") or item_dict.get("collectionName"),
                    "trope_vectors": item_dict.get("trope_vectors") or item_dict.get("tropeVectors", []),
                    "vibe_analysis_version": 1,
                }
                metadata_file = os.path.join(metadata_dir, "metadata.json")
                with open(metadata_file, "w", encoding="utf-8") as file:
                    json.dump(metadata_content, file, indent=2, ensure_ascii=False)

                local_poster = f"/media/{library_name}/{folder_name}/poster.jpg" if os.path.exists(poster_abs) else (raw_poster_url or "")
                local_backdrop = f"/media/{library_name}/{folder_name}/backdrop.jpg" if os.path.exists(backdrop_abs) else (raw_backdrop_url or "")
                now = time.time()
                async with AsyncSession(engine) as db:
                    movie = await db.get(Movie, movie_id)
                    if not movie:
                        movie = Movie(
                            id=movie_id,
                            tmdb_id=tmdb_id,
                            title=title,
                            description=item_dict.get("description", ""),
                            thumbnail_url=local_poster,
                            banner_url=local_backdrop,
                            video_url="",
                            duration=item_dict.get("duration", "45m" if media_type == "series" else "2h"),
                            release_year=release_year,
                            rating=item_dict.get("rating"),
                            director=item_dict.get("director"),
                            type=media_type,
                            vote_average=float(item_dict.get("vote_average") or 0.0),
                            vote_count=int(item_dict.get("vote_count") or 0),
                            popularity=float(item_dict.get("popularity") or 0.0),
                            catalog_source="tmdb_cache",
                            availability="cached",
                            cached_at=now,
                            metadata_refreshed_at=now,
                            remote_thumbnail_url=raw_poster_url or None,
                            remote_banner_url=raw_backdrop_url or None,
                            local_thumbnail_url=f"/media/{library_name}/{folder_name}/poster.jpg" if raw_poster_url else None,
                            local_banner_url=f"/media/{library_name}/{folder_name}/backdrop.jpg" if raw_backdrop_url else None,
                            cache_state="ready",
                            catalog_enrichment_version=1,
                        )
                        movie.genres = item_dict.get("genres", [])
                        movie.cast = item_dict.get("cast", [])
                        movie.keywords = item_dict.get("keywords", [])
                        movie.crew = item_dict.get("crew", [])
                        movie.trope_vectors = item_dict.get("trope_vectors") or item_dict.get("tropeVectors") or compute_trope_vectors(movie.genres, movie.keywords, movie.description)
                        movie.collection_name = item_dict.get("collection_name") or item_dict.get("collectionName")
                    elif movie.availability != "available":
                        movie.title = title
                        movie.description = item_dict.get("description", movie.description)
                        movie.thumbnail_url = local_poster or movie.thumbnail_url
                        movie.banner_url = local_backdrop or movie.banner_url
                        movie.genres = item_dict.get("genres", movie.genres)
                        movie.cast = item_dict.get("cast", movie.cast)
                        movie.director = item_dict.get("director", movie.director)
                        movie.vote_average = float(item_dict.get("vote_average") or movie.vote_average or 0.0)
                        movie.vote_count = int(item_dict.get("vote_count") or movie.vote_count or 0)
                        movie.popularity = float(item_dict.get("popularity") or movie.popularity or 0.0)
                        movie.catalog_source = "tmdb_cache"
                        movie.availability = "cached"
                        movie.cached_at = movie.cached_at or now
                        movie.metadata_refreshed_at = now
                        movie.remote_thumbnail_url = raw_poster_url or movie.remote_thumbnail_url
                        movie.remote_banner_url = raw_backdrop_url or movie.remote_banner_url
                        movie.local_thumbnail_url = f"/media/{library_name}/{folder_name}/poster.jpg" if raw_poster_url else movie.local_thumbnail_url
                        movie.local_banner_url = f"/media/{library_name}/{folder_name}/backdrop.jpg" if raw_backdrop_url else movie.local_banner_url
                        movie.cache_state = "ready"
                        movie.cache_retry_count = 0
                        movie.cache_next_retry_at = None
                        movie.cache_last_error = None
                        movie.catalog_enrichment_version = 1
                        movie.keywords = item_dict.get("keywords", movie.keywords)
                        movie.crew = item_dict.get("crew", movie.crew)
                        movie.trope_vectors = item_dict.get("trope_vectors") or item_dict.get("tropeVectors") or compute_trope_vectors(movie.genres, movie.keywords, movie.description)
                        movie.collection_name = item_dict.get("collection_name") or item_dict.get("collectionName") or movie.collection_name
                    db.add(movie)
                    await db.commit()
                logger.info(f"[TMDB Client] In-place recommendation cache complete for: {title}")
                return movie_id
            except Exception as e:
                logger.error(f"[TMDB Client] Error caching {item_dict.get('title')}: {e}")
                raise

    async def discover_media(self, category: str, media_type: str = "movie", profile_id: Optional[str] = None, cache_limit: Optional[int] = None) -> List[Dict[str, Any]]:
        import asyncio
        from services.recommendation import get_profile_preferences
        
        is_tv = media_type.lower() in ("series", "tv")
        
        params = {"sort_by": "popularity.desc", "include_adult": "false"}
        
        is_trending = category.lower() == "trending"
        
        if profile_id and is_trending:
            # Personalize trending using top genres and actors
            prefs = await get_profile_preferences(profile_id)
            if prefs["genre"]:
                # Map our genre strings to TMDB IDs
                preferred_genres = {value.casefold() for value in prefs["genre"]}
                genre_ids = [str(k) for k, v in GENRES_MAP.items() if v.casefold() in preferred_genres]
                if genre_ids:
                    params["with_genres"] = "|".join(genre_ids[:3])  # OR top 3 genres
            if prefs["actor"]:
                # TMDB needs person IDs, but we only have names. A robust implementation would map names to IDs.
                # For this StreamHome scope, we pass them if we happen to have IDs, but TMDB /discover doesn't take names directly.
                # We will rely heavily on genres for TMDB discovery, while local sorting uses both actors and genres.
                pass
                
        if is_tv:
            path = "/discover/tv"
            local_prefix = "Series"
            if not is_trending:
                genre_id = 10759 if "action" in category.lower() else 10765
                params["with_genres"] = str(genre_id)
        else:
            path = "/discover/movie"
            local_prefix = "Movies"
            if not is_trending:
                genre_id = 28 if "action" in category.lower() else 878
                params["with_genres"] = str(genre_id)
                
        # Fetch multiple pages if trending to ensure diverse genres
        pages_to_fetch = 3 if is_trending else 1
        all_results = []
        
        for page in range(1, pages_to_fetch + 1):
            page_params = {**params, "page": page}
            data = await self._get(path, params=page_params)
            if data and data.get("results"):
                all_results.extend(data.get("results", []))

        
        raw_results = []
        cache_jobs = []
        if not all_results:
            return []
            
        # Parse output from TMDB results
        for item in all_results:
            poster_path = item.get("poster_path")
            backdrop_path = item.get("backdrop_path")
            raw_poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
            raw_backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else ""
            
            tmdb_id = item.get("id")
            title = item.get("name") if is_tv else item.get("title")
            if not title:
                title = item.get("title") or item.get("name") or "Unknown Title"
                
            release_date = item.get("first_air_date") if is_tv else item.get("release_date")
            if release_date:
                try:
                    from datetime import datetime
                    rel_dt = datetime.strptime(release_date, "%Y-%m-%d").date()
                    if rel_dt > datetime.now().date():
                        logger.info(f"[TMDB Client] Skipping unreleased media '{title}' (Release Date: {release_date})")
                        continue
                except ValueError:
                    pass
            release_year = int(release_date.split("-")[0]) if release_date else 2026
            clean_title = "".join(c for c in title if c.isalnum() or c in " .-_")
            
            folder_name = f"{clean_title}_TMDB_{tmdb_id}" if is_tv else f"{clean_title}_{release_year}_TMDB_{tmdb_id}"
            
            # Use TMDB URLs directly until local downloads complete
            local_thumbnail_url = raw_poster_url if raw_poster_url else f"/media/{local_prefix}/{folder_name}/poster.jpg"
            local_banner_url = raw_backdrop_url if raw_backdrop_url else f"/media/{local_prefix}/{folder_name}/backdrop.jpg"
            
            genre_ids = item.get("genre_ids", [])
            genres = [GENRES_MAP.get(gid) for gid in genre_ids if gid in GENRES_MAP]
            if not genres:
                genres = ["Trending"]
                
            result_item = {
                "id": f"discover_{tmdb_id}",
                "tmdb_id": tmdb_id,
                "title": title,
                "description": item.get("overview", ""),
                "thumbnail_url": local_thumbnail_url,
                "banner_url": local_banner_url,
                "genres": genres,
                "duration": "45m" if is_tv else "2h 10m",
                "release_year": release_year,
                "rating": "TV-14" if is_tv else "PG-13",
                "vote_average": item.get("vote_average", 7.5),
                "vote_count": item.get("vote_count", 1000),
                "director": "Various",
                "cast": [],
                "type": "series" if is_tv else "movie",
                "popularity": item.get("popularity", 0.0),
                "original_language": item.get("original_language", "en"),
                "source": "tmdb_cache",
                "availability": "cached",
            }
            
            raw_results.append(result_item)
            
            effective_limit = cache_limit if cache_limit is not None else 12
            if len(raw_results) <= effective_limit:
                cache_jobs.append(self.cache_media_locally(result_item, raw_poster_url, raw_backdrop_url))
            
        if cache_jobs:
            await asyncio.gather(*cache_jobs, return_exceptions=False)
        return raw_results[:cache_limit] if cache_limit is not None else raw_results

    async def search_media(self, query: str) -> List[Dict[str, Any]]:
        """Return movie/series search results immediately and queue their metadata/artwork cache."""
        data = await self._get("/search/multi", params={"query": query, "include_adult": "false", "page": 1})
        
        raw_results = []
        if not data or not data.get("results"):
            return []
            
        for item in data.get("results", []):
            media_type = item.get("media_type")
            if media_type not in {"movie", "tv"} or item.get("adult") is True:
                continue
            is_tv = media_type == "tv"
            poster_path = item.get("poster_path")
            backdrop_path = item.get("backdrop_path")
            raw_poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
            raw_backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else ""
            
            tmdb_id = item.get("id")
            title = item.get("name") if is_tv else item.get("title")
            title = title or item.get("title") or item.get("name") or "Unknown Title"
            release_date = item.get("first_air_date") if is_tv else item.get("release_date")
            try:
                release_year = int(release_date.split("-")[0]) if release_date else 0
            except (TypeError, ValueError):
                release_year = 0
            clean_title = "".join(c for c in title if c.isalnum() or c in " .-_")
            
            folder_name = f"{clean_title}_TMDB_{tmdb_id}" if is_tv else f"{clean_title}_{release_year}_TMDB_{tmdb_id}"
            library_name = "Series" if is_tv else "Movies"
            
            # Use TMDB URLs directly until local downloads complete
            expected_thumbnail_url = f"/media/{library_name}/{folder_name}/poster.jpg" if raw_poster_url else None
            expected_banner_url = f"/media/{library_name}/{folder_name}/backdrop.jpg" if raw_backdrop_url else None
            
            genre_ids = item.get("genre_ids", [])
            genres = [GENRES_MAP.get(gid) for gid in genre_ids if gid in GENRES_MAP]
            if not genres:
                genres = ["Action", "Sci-Fi"]

            result_item = {
                "id": f"tv_{tmdb_id}" if is_tv else f"m_{tmdb_id}",
                "tmdb_id": tmdb_id,
                "title": title,
                "description": item.get("overview", ""),
                "thumbnail_url": raw_poster_url,
                "banner_url": raw_backdrop_url,
                "genres": genres,
                "duration": "45m" if is_tv else "2h 10m",
                "release_year": release_year,
                "rating": "TV-14" if is_tv else "PG-13",
                "vote_average": item.get("vote_average", 7.5),
                "vote_count": item.get("vote_count", 1000),
                "director": "Various",
                "cast": [],
                "type": "series" if is_tv else "movie",
                "popularity": item.get("popularity", 0.0),
                "original_language": item.get("original_language", "en"),
                "source": "tmdb_cache",
                "availability": "cached",
                "remote_thumbnail_url": raw_poster_url or None,
                "remote_banner_url": raw_backdrop_url or None,
                "local_thumbnail_url": expected_thumbnail_url,
                "local_banner_url": expected_banner_url,
                "cache_state": "queued",
            }
            
            raw_results.append(result_item)

        return await self._upsert_search_results(raw_results)

    async def discover_related_media(self, tmdb_id: int, media_type: str, limit: int = 12) -> List[Dict[str, Any]]:
        """Cache a bounded mix of TMDB recommendations and similar titles for one seed."""
        is_tv = media_type in {"series", "tv"}
        api_type = "tv" if is_tv else "movie"
        payloads = await asyncio.gather(
            self._get(f"/{api_type}/{tmdb_id}/recommendations", params={"page": 1}),
            self._get(f"/{api_type}/{tmdb_id}/similar", params={"page": 1}),
        )
        seen: set[int] = set()
        items: List[Dict[str, Any]] = []
        for payload in payloads:
            for item in (payload or {}).get("results", []):
                related_id = item.get("id")
                if not related_id or related_id == tmdb_id or related_id in seen or item.get("adult") is True:
                    continue
                seen.add(related_id)
                poster_path, backdrop_path = item.get("poster_path"), item.get("backdrop_path")
                poster = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
                backdrop = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else ""
                title = (item.get("name") if is_tv else item.get("title")) or item.get("title") or item.get("name") or "Unknown Title"
                date = item.get("first_air_date") if is_tv else item.get("release_date")
                try:
                    year = int(date.split("-")[0]) if date else 0
                except (TypeError, ValueError):
                    year = 0
                clean_title = "".join(char for char in title if char.isalnum() or char in " .-_")
                library = "Series" if is_tv else "Movies"
                folder = f"{clean_title}_TMDB_{related_id}" if is_tv else f"{clean_title}_{year}_TMDB_{related_id}"
                items.append({
                    "id": f"tv_{related_id}" if is_tv else f"m_{related_id}", "tmdb_id": related_id,
                    "title": title, "description": item.get("overview", ""), "thumbnail_url": poster,
                    "banner_url": backdrop, "genres": [GENRES_MAP[gid] for gid in item.get("genre_ids", []) if gid in GENRES_MAP] or ["Discovery"],
                    "duration": "45m" if is_tv else "2h", "release_year": year,
                    "rating": "TV-14" if is_tv else "PG-13", "vote_average": item.get("vote_average", 0),
                    "vote_count": item.get("vote_count", 0), "director": "Various", "cast": [],
                    "type": "series" if is_tv else "movie", "popularity": item.get("popularity", 0),
                    "original_language": item.get("original_language", "en"), "source": "tmdb_cache", "availability": "cached",
                    "remote_thumbnail_url": poster or None, "remote_banner_url": backdrop or None,
                    "local_thumbnail_url": f"/media/{library}/{folder}/poster.jpg" if poster else None,
                    "local_banner_url": f"/media/{library}/{folder}/backdrop.jpg" if backdrop else None,
                    "cache_state": "queued",
                })
                if len(items) >= limit:
                    return await self._upsert_search_results(items)
        return await self._upsert_search_results(items)

    async def _upsert_search_results(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        import time
        from sqlmodel.ext.asyncio.session import AsyncSession
        from db import engine
        from models import Movie

        queued: List[str] = []
        response: List[Dict[str, Any]] = []
        async with AsyncSession(engine) as db:
            for item in results:
                movie = await db.get(Movie, item["id"])
                if movie and movie.catalog_source == "server":
                    if movie.tmdb_id is None:
                        movie.tmdb_id = item["tmdb_id"]
                        db.add(movie)
                    response.append(self._discover_from_movie(movie))
                    continue
                is_new = movie is None
                if movie is None:
                    movie = Movie(
                        id=item["id"], tmdb_id=item["tmdb_id"], title=item["title"],
                        description=item["description"], thumbnail_url=item["thumbnail_url"],
                        banner_url=item["banner_url"], video_url="", duration=item["duration"],
                        release_year=item["release_year"], rating=item["rating"], director=item["director"],
                        type=item["type"], original_language=item["original_language"],
                        vote_average=float(item["vote_average"] or 0), vote_count=int(item["vote_count"] or 0),
                        popularity=float(item["popularity"] or 0), catalog_source="tmdb_cache",
                        availability="cached", cached_at=time.time(), cache_state="queued",
                    )
                movie.tmdb_id = item["tmdb_id"]
                movie.title = item["title"]
                movie.description = item["description"] or movie.description
                movie.genres = item["genres"]
                movie.release_year = item["release_year"]
                movie.type = item["type"]
                movie.original_language = item["original_language"]
                movie.vote_average = float(item["vote_average"] or 0)
                movie.vote_count = int(item["vote_count"] or 0)
                movie.popularity = float(item["popularity"] or 0)
                movie.remote_thumbnail_url = item["remote_thumbnail_url"] or movie.remote_thumbnail_url
                movie.remote_banner_url = item["remote_banner_url"] or movie.remote_banner_url
                movie.local_thumbnail_url = movie.local_thumbnail_url or item["local_thumbnail_url"]
                movie.local_banner_url = movie.local_banner_url or item["local_banner_url"]
                if not movie.thumbnail_url or movie.thumbnail_url.startswith("http"):
                    movie.thumbnail_url = item["thumbnail_url"] or movie.thumbnail_url
                if not movie.banner_url or movie.banner_url.startswith("http"):
                    movie.banner_url = item["banner_url"] or movie.banner_url
                movie.catalog_source = "tmdb_cache"
                movie.availability = "cached"
                if is_new or movie.cache_state in {None, "queued", "caching", "error"}:
                    movie.cache_state = "queued"
                    if movie.cache_retry_count >= 6:
                        movie.cache_retry_count = 0
                    movie.cache_next_retry_at = None
                    movie.cache_last_error = None
                    queued.append(movie.id)
                db.add(movie)
                response.append(self._discover_from_movie(movie))
            await db.commit()
        for movie_id in queued:
            await self.enqueue_cache(movie_id)
        return response

    @staticmethod
    def _discover_from_movie(movie) -> Dict[str, Any]:
        return {
            "id": movie.id, "tmdb_id": movie.tmdb_id, "title": movie.title,
            "description": movie.description, "thumbnail_url": movie.thumbnail_url,
            "banner_url": movie.banner_url, "genres": movie.genres, "duration": movie.duration,
            "release_year": movie.release_year, "rating": movie.rating,
            "vote_average": movie.vote_average or 0, "vote_count": movie.vote_count or 0,
            "director": movie.director, "cast": movie.cast, "type": movie.type,
            "source": movie.catalog_source, "availability": movie.availability,
            "remote_thumbnail_url": movie.remote_thumbnail_url,
            "remote_banner_url": movie.remote_banner_url,
            "local_thumbnail_url": movie.local_thumbnail_url,
            "local_banner_url": movie.local_banner_url,
            "cache_state": movie.cache_state,
        }

tmdb_client = TMDBClient()
