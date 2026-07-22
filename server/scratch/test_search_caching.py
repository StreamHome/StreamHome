"""Isolated regression checks for URL-first multi-search caching."""

import asyncio
import os
import tempfile

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from models import Movie
from services.tmdb import tmdb_client
from config import settings


async def main() -> None:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    original_engine = db_module.engine
    original_get = tmdb_client._get
    original_queue = tmdb_client._cache_queue
    original_pending = tmdb_client._cache_pending
    original_media_dir = settings.MEDIA_DIR
    original_enqueue = tmdb_client.enqueue_cache
    original_cache_locally = tmdb_client.cache_media_locally
    try:
        db_module.engine = test_engine
        async with test_engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(test_engine) as db:
            playable = Movie(
                id="m_7", tmdb_id=7, title="Local Seven", description="Local", thumbnail_url="/media/local.jpg",
                banner_url=None, video_url="/media/local.mp4", duration="2h", release_year=2020, type="movie",
                catalog_source="server", availability="available",
            )
            db.add(playable)
            await db.commit()

        async def fake_get(path_name, params=None):
            assert path_name == "/search/multi"
            return {"results": [
                {"id": 42, "media_type": "movie", "title": "Movie Forty Two", "overview": "Movie", "release_date": "2024-01-01", "poster_path": "/movie.jpg", "backdrop_path": "/movie-bg.jpg", "genre_ids": [18], "vote_average": 8.1, "vote_count": 500, "popularity": 50},
                {"id": 8, "media_type": "person", "name": "Not media"},
                {"id": 99, "media_type": "tv", "name": "Series Ninety Nine", "overview": "Series", "first_air_date": "2023-02-03", "poster_path": "/series.jpg", "backdrop_path": None, "genre_ids": [10765], "vote_average": 7.9, "vote_count": 300, "popularity": 40},
                {"id": 100, "media_type": "movie", "title": "Adult", "adult": True},
                {"id": 7, "media_type": "movie", "title": "Remote Seven", "overview": "Must not replace local", "release_date": "2020-01-01", "poster_path": "/remote.jpg", "genre_ids": []},
            ]}

        tmdb_client._get = fake_get
        tmdb_client._cache_queue = asyncio.Queue()
        tmdb_client._cache_pending = set()
        results = await tmdb_client.search_media("mixed")
        assert [item["id"] for item in results] == ["m_42", "tv_99", "m_7"]
        assert results[0]["thumbnail_url"].startswith("https://image.tmdb.org/")
        assert results[1]["type"] == "series"
        assert tmdb_client._cache_queue.qsize() == 2, "every displayed non-local result must be queued"
        async with AsyncSession(test_engine) as db:
            cached = list((await db.exec(select(Movie).where(Movie.catalog_source == "tmdb_cache"))).all())
            assert {movie.id for movie in cached} == {"m_42", "tv_99"}
            assert all(movie.cache_state == "queued" and not movie.video_url for movie in cached)
            local = await db.get(Movie, "m_7")
            assert local and local.title == "Local Seven" and local.video_url == "/media/local.mp4"

        with tempfile.TemporaryDirectory() as media_dir:
            settings.MEDIA_DIR = media_dir
            stale = Movie(
                id="m_404", tmdb_id=404, title="Missing Artwork", description="",
                thumbnail_url="/media/Movies/missing/poster.jpg",
                banner_url="/media/Movies/missing/backdrop.jpg", video_url="", duration="2h",
                release_year=2024, type="movie", catalog_source="tmdb_cache", availability="cached",
                local_thumbnail_url="/media/Movies/missing/poster.jpg",
                local_banner_url="/media/Movies/missing/backdrop.jpg",
                remote_thumbnail_url="https://image.tmdb.org/t/p/w500/poster.jpg",
                remote_banner_url="https://image.tmdb.org/t/p/original/backdrop.jpg",
                cache_state="ready",
            )
            assert tmdb_client._reconcile_cached_artwork(stale)
            assert stale.local_thumbnail_url is None and stale.local_banner_url is None
            assert stale.thumbnail_url == stale.remote_thumbnail_url
            assert stale.banner_url == stale.remote_banner_url

        async with AsyncSession(test_engine) as db:
            failed = Movie(
                id="m_500", tmdb_id=500, title="Retry", description="", thumbnail_url="",
                banner_url="", video_url="", duration="2h", release_year=2024, type="movie",
                catalog_source="tmdb_cache", availability="cached", cache_state="caching",
            )
            db.add(failed)
            await db.commit()
        retry_delay = await tmdb_client._mark_cache_error("m_500", RuntimeError("temporary"))
        assert retry_delay == 900
        async with AsyncSession(test_engine) as db:
            failed = await db.get(Movie, "m_500")
            assert failed and failed.cache_state == "error" and failed.cache_retry_count == 1
            assert failed.cache_next_retry_at and failed.cache_last_error == "RuntimeError: temporary"

        cache_snapshots = []
        async def capture_cache(item, poster, backdrop) -> None:
            cache_snapshots.append((item, poster, backdrop))
        tmdb_client.cache_media_locally = capture_cache
        await tmdb_client._cache_movie_record("m_42")
        assert cache_snapshots and cache_snapshots[0][0]["title"] == "Movie Forty Two"
        async with AsyncSession(test_engine) as db:
            caching = await db.get(Movie, "m_42")
            assert caching and caching.cache_state == "caching"

        enqueued = []
        async def capture_enqueue(movie_id: str) -> None:
            enqueued.append(movie_id)
        tmdb_client.enqueue_cache = capture_enqueue
        await tmdb_client.start_cache_workers()
        assert {"m_42", "tv_99"}.issubset(set(enqueued)), "startup must retain resumable IDs after commit"
        await tmdb_client.stop_cache_workers()
        print("Search caching regression checks passed.")
    finally:
        tmdb_client._get = original_get
        tmdb_client._cache_queue = original_queue
        tmdb_client._cache_pending = original_pending
        tmdb_client.enqueue_cache = original_enqueue
        tmdb_client.cache_media_locally = original_cache_locally
        settings.MEDIA_DIR = original_media_dir
        db_module.engine = original_engine
        await test_engine.dispose()
        for suffix in ("", "-shm", "-wal"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
