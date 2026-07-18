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


async def main() -> None:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    original_engine = db_module.engine
    original_get = tmdb_client._get
    original_queue = tmdb_client._cache_queue
    original_pending = tmdb_client._cache_pending
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
        print("Search caching regression checks passed.")
    finally:
        tmdb_client._get = original_get
        tmdb_client._cache_queue = original_queue
        tmdb_client._cache_pending = original_pending
        db_module.engine = original_engine
        await test_engine.dispose()
        for suffix in ("", "-shm", "-wal"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
