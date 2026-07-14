import httpx
import os
import json
import asyncio
from typing import Dict, Any, List, Optional
from config import settings
from services.logger import logger

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
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(f"{self.base_url}{path}", params=params, headers=headers, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
                logger.error(f"[TMDB Client] API Error {response.status_code}: {response.text}")
            except Exception as e:
                logger.error(f"[TMDB Client] Exception querying TMDB: {e}")
        return None

    async def fetch_movie_metadata(self, tmdb_id: int) -> Dict[str, Any]:
        """Fetch movie details and crew credits from TMDB."""
        data = await self._get(f"/movie/{tmdb_id}", params={"append_to_response": "credits,release_dates"})
        
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

        # Parse director and cast
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
            "cast": cast_list or ["Unknown Actor"],
            "originalLanguage": data.get("original_language", "en"),
            "vote_average": data.get("vote_average", 7.5),
            "vote_count": data.get("vote_count", 100)
        }

    async def fetch_show_metadata(self, tmdb_id: int) -> Dict[str, Any]:
        """Fetch TV Show metadata."""
        data = await self._get(f"/tv/{tmdb_id}", params={"append_to_response": "credits,content_ratings"})
        
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
            "cast": cast_list or ["Unknown Actor"],
            "originalLanguage": data.get("original_language", "en"),
            "vote_average": data.get("vote_average", 7.5),
            "vote_count": data.get("vote_count", 100)
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
        """
        Asynchronously creates directories, downloads posters/backdrops, and writes a local registry file
        so that discovered/recommended media has its assets saved locally in a portable, self-contained way.
        Uses a semaphore to ensure caching tasks are processed sequentially and don't flood the network.
        """
        async with self._cache_semaphore:
            try:
                tmdb_id = item_dict.get("tmdb_id")
                media_type = item_dict.get("type", "movie")
                title = item_dict.get("title", f"Media_{tmdb_id}")
                release_year = item_dict.get("release_year", 2026)
                
                clean_title = "".join(c for c in title if c.isalnum() or c in " .-_")
                server_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                storage_root = settings.MEDIA_DIR
                
                # Image download rate limiting helper
                from services.ffmpeg import download_and_cache_metadata_image
                async def safe_download_image(url, dest_path):
                    async with self._img_semaphore:
                        return await download_and_cache_metadata_image(url, dest_path)
                
                if media_type == "movie":
                    folder_name = f"{clean_title}_{release_year}_TMDB_{tmdb_id}"
                    folder_rel_path = os.path.join(storage_root, "Movies", folder_name)
                    folder_abs_path = os.path.abspath(os.path.join(server_root, folder_rel_path))
                    
                    # Write registry metadata.json
                    metadata_dir = os.path.join(folder_abs_path, ".metadata")
                    metadata_file = os.path.join(metadata_dir, "metadata.json")
                    
                    if os.path.exists(metadata_file):
                        return

                    os.makedirs(folder_abs_path, exist_ok=True)
                    os.makedirs(metadata_dir, exist_ok=True)
                    
                    # Save metadata if it doesn't exist
                    metadata_content = {
                        "tmdb_id": int(tmdb_id),
                        "media_type": media_type,
                        "title": title,
                        "release_year": int(release_year),
                        "original_language": item_dict.get("original_language", "en"),
                        "video_url": ""
                    }
                    with open(metadata_file, "w", encoding="utf-8") as f:
                        json.dump(metadata_content, f, indent=2, ensure_ascii=False)
                            
                    poster_abs = os.path.join(folder_abs_path, "poster.jpg")
                    backdrop_abs = os.path.join(folder_abs_path, "backdrop.jpg")
                    
                    tasks = []
                    if raw_poster_url and raw_poster_url.startswith("http"):
                        tasks.append(safe_download_image(raw_poster_url, poster_abs))
                    if raw_backdrop_url and raw_backdrop_url.startswith("http"):
                        tasks.append(safe_download_image(raw_backdrop_url, backdrop_abs))
                        
                    if tasks:
                        await asyncio.gather(*tasks)
                        
                else: # TV series/show
                    folder_name = f"{clean_title}_TMDB_{tmdb_id}"
                    series_rel_path = os.path.join(storage_root, "Series", folder_name)
                    series_abs_path = os.path.abspath(os.path.join(server_root, series_rel_path))
                    
                    series_metadata_dir = os.path.join(series_abs_path, ".metadata")
                    series_metadata_file = os.path.join(series_metadata_dir, "metadata.json")
                    
                    if os.path.exists(series_metadata_file):
                        return

                    os.makedirs(series_abs_path, exist_ok=True)
                    os.makedirs(series_metadata_dir, exist_ok=True)
                            
                    series_poster_abs = os.path.join(series_abs_path, "poster.jpg")
                    series_backdrop_abs = os.path.join(series_abs_path, "backdrop.jpg")
                    
                    tasks = []
                    if raw_poster_url and raw_poster_url.startswith("http"):
                        tasks.append(safe_download_image(raw_poster_url, series_poster_abs))
                    if raw_backdrop_url and raw_backdrop_url.startswith("http"):
                        tasks.append(safe_download_image(raw_backdrop_url, series_backdrop_abs))
                        
                    if tasks:
                        await asyncio.gather(*tasks)
                        
                    # Fetch full show details to find seasons and episodes
                    show_data = await self._get(f"/tv/{tmdb_id}")
                    if show_data:
                        from datetime import datetime
                        today = datetime.now().date()
                        seasons = show_data.get("seasons", [])
                        active_seasons = []
                        for s in seasons:
                            if s.get("season_number", 0) <= 0:
                                continue
                            air_date = s.get("air_date")
                            if air_date:
                                try:
                                    air_dt = datetime.strptime(air_date, "%Y-%m-%d").date()
                                    if air_dt > today:
                                        logger.info(f"[TMDB Client] Skipping unreleased Season {s.get('season_number')} (Air Date: {air_date})")
                                        continue
                                except ValueError:
                                    pass
                            active_seasons.append(s)
                        if not active_seasons and seasons:
                            active_seasons = seasons
                            
                        for s in active_seasons:
                            season_num = s.get("season_number", 1)
                            
                            season_data = await self._get(f"/tv/{tmdb_id}/season/{season_num}")
                            if not season_data or not season_data.get("episodes"):
                                continue
                                
                            episodes = season_data.get("episodes", [])
                            season_image_tasks = []
                            
                            for ep in episodes:
                                ep_air_date = ep.get("air_date")
                                if ep_air_date:
                                    try:
                                        ep_air_dt = datetime.strptime(ep_air_date, "%Y-%m-%d").date()
                                        if ep_air_dt > today:
                                            continue
                                    except ValueError:
                                        pass
                                episode_num = ep.get("episode_number", 1)
                                ep_title = ep.get("name", f"Episode {episode_num}")
                                ep_desc = ep.get("overview", "")
                                
                                logger.info(f"[TMDB Client] Caching {title}: Season {season_num}, Episode {episode_num} ({ep_title})...")
                                
                                ep_folder_rel = os.path.join(storage_root, "Series", folder_name, f"Season_{season_num}", f"Episode_{episode_num}")
                                ep_folder_abs = os.path.abspath(os.path.join(server_root, ep_folder_rel))
                                os.makedirs(ep_folder_abs, exist_ok=True)
                                
                                ep_metadata_dir = os.path.join(ep_folder_abs, ".metadata")
                                os.makedirs(ep_metadata_dir, exist_ok=True)
                                ep_metadata_file = os.path.join(ep_metadata_dir, "metadata.json")
                                
                                ep_served_url = f"/media/Series/{folder_name}/Season_{season_num}/Episode_{episode_num}/{clean_title}_S{season_num:02d}E{episode_num:02d}.mp4"
                                
                                still_path = ep.get("still_path")
                                ep_thumb_url = f"https://image.tmdb.org/t/p/w500{still_path}" if still_path else ""
                                
                                ep_metadata_content = {
                                    "tmdb_id": int(tmdb_id),
                                    "media_type": "series",
                                    "title": ep_title,
                                    "description": ep_desc,
                                    "season": season_num,
                                    "episode": episode_num,
                                    "video_url": ep_served_url,
                                    "thumbnailUrl": ep_thumb_url,
                                    "language": item_dict.get("original_language", "en"),
                                    "original_language": item_dict.get("original_language", "en")
                                }
                                
                                with open(ep_metadata_file, "w", encoding="utf-8") as f:
                                    json.dump(ep_metadata_content, f, indent=2, ensure_ascii=False)
                                    
                                sync_compatible_metadata_file = os.path.join(ep_metadata_dir, f"metadata_s{season_num}_e{episode_num}.json")
                                with open(sync_compatible_metadata_file, "w", encoding="utf-8") as f:
                                    json.dump(ep_metadata_content, f, indent=2, ensure_ascii=False)
                                
                                ep_poster_abs = os.path.join(ep_folder_abs, "poster.jpg")
                                ep_backdrop_abs = os.path.join(ep_folder_abs, "backdrop.jpg")
                                ep_thumb_abs = os.path.join(ep_folder_abs, "thumbnail.jpg")
                                
                                if raw_poster_url:
                                    season_image_tasks.append(safe_download_image(raw_poster_url, ep_poster_abs))
                                if raw_backdrop_url:
                                    season_image_tasks.append(safe_download_image(raw_backdrop_url, ep_backdrop_abs))
                                if still_path:
                                    ep_still_url = f"https://image.tmdb.org/t/p/w500{still_path}"
                                    season_image_tasks.append(safe_download_image(ep_still_url, ep_thumb_abs))
                                        
                            if season_image_tasks:
                                logger.info(f"[TMDB Client] Downloading assets for {title}: Season {season_num} (contains {len(episodes)} episodes)...")
                                await asyncio.gather(*season_image_tasks)
                                logger.info(f"[TMDB Client] Completed caching for {title}: Season {season_num}.")
                        
                        # Write registry metadata.json ONLY on successful completion of all loops
                        series_metadata_content = {
                            "tmdb_id": int(tmdb_id),
                            "media_type": media_type,
                            "title": title,
                            "release_year": int(release_year),
                            "original_language": item_dict.get("original_language", "en"),
                            "video_url": ""
                        }
                        with open(series_metadata_file, "w", encoding="utf-8") as f:
                            json.dump(series_metadata_content, f, indent=2, ensure_ascii=False)
                            
                logger.info(f"[TMDB Client] Local caching complete for: {title}")
            except Exception as e:
                logger.error(f"[TMDB Client] Error in background caching task for {item_dict.get('title')}: {e}")

    async def discover_media(self, category: str, media_type: str = "movie") -> List[Dict[str, Any]]:
        import asyncio
        is_tv = media_type.lower() in ("series", "tv")
        
        if is_tv:
            genre_id = 10759 if "action" in category.lower() else 10765  # Action & Adventure, Sci-Fi & Fantasy
            path = "/discover/tv"
            local_prefix = "Series"
        else:
            genre_id = 28 if "action" in category.lower() else 878      # Action, Sci-Fi
            path = "/discover/movie"
            local_prefix = "Movies"
            
        params = {
            "with_genres": str(genre_id),
            "sort_by": "popularity.desc"
        }
        data = await self._get(path, params=params)
        
        raw_results = []
        if not data or not data.get("results"):
            return []
            
        # Parse output from TMDB results
        for item in data.get("results", []):
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
                genres = ["Action" if "action" in category.lower() else "Sci-Fi"]
                
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
                "type": "series" if is_tv else "movie"
            }
            
            raw_results.append(result_item)
            
            # Start background caching task
            asyncio.create_task(self.cache_media_locally(result_item, raw_poster_url, raw_backdrop_url))
            
        return raw_results

    async def search_media(self, query: str) -> List[Dict[str, Any]]:
        """Search movies from TMDB matching a text query."""
        import asyncio
        data = await self._get("/search/movie", params={"query": query, "include_adult": "false"})
        
        raw_results = []
        if not data or not data.get("results"):
            return []
            
        for item in data.get("results", []):
            poster_path = item.get("poster_path")
            backdrop_path = item.get("backdrop_path")
            raw_poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
            raw_backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else ""
            
            tmdb_id = item.get("id")
            title = item.get("title") or item.get("name")
            release_date = item.get("release_date")
            release_year = int(release_date.split("-")[0]) if release_date else 2026
            clean_title = "".join(c for c in title if c.isalnum() or c in " .-_")
            
            folder_name = f"{clean_title}_{release_year}_TMDB_{tmdb_id}"
            
            # Use TMDB URLs directly until local downloads complete
            local_thumbnail_url = raw_poster_url if raw_poster_url else f"/media/Movies/{folder_name}/poster.jpg"
            local_banner_url = raw_backdrop_url if raw_backdrop_url else f"/media/Movies/{folder_name}/backdrop.jpg"
            
            genre_ids = item.get("genre_ids", [])
            genres = [GENRES_MAP.get(gid) for gid in genre_ids if gid in GENRES_MAP]
            if not genres:
                genres = ["Action", "Sci-Fi"]

            result_item = {
                "id": f"discover_{tmdb_id}",
                "tmdb_id": tmdb_id,
                "title": title,
                "description": item.get("overview", ""),
                "thumbnail_url": local_thumbnail_url,
                "banner_url": local_banner_url,
                "genres": genres,
                "duration": "2h 10m",
                "release_year": release_year,
                "rating": "PG-13",
                "vote_average": item.get("vote_average", 7.5),
                "vote_count": item.get("vote_count", 1000),
                "director": "Various",
                "cast": [],
                "type": "movie"
            }
            
            raw_results.append(result_item)
            
            # Start background caching task
            asyncio.create_task(self.cache_media_locally(result_item, raw_poster_url, raw_backdrop_url))
            
        return raw_results

tmdb_client = TMDBClient()
