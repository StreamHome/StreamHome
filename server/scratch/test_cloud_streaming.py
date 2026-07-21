"""Regression checks that cloud compatibility streaming delivers real, ranged bytes."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from db import get_session
from models import Movie
from routes.auth import get_current_user
from routes.stream import router
from services.media_source import ResolvedMediaSource


PAYLOAD = bytes(range(256)) * 8
CATALOG_PATH = "/media/Movies/Test_Cloud_Movie_TMDB_123/Test_Cloud_Movie.mp4"
REMOTE_PATH = "gdrive:media/Movies/Test_Cloud_Movie_TMDB_123/Test_Cloud_Movie.mp4"


class MockDb:
    async def execute(self, _statement):
        class Result:
            @staticmethod
            def scalars():
                class Scalars:
                    @staticmethod
                    def first():
                        return Movie(
                            id="m_test_cloud_movie",
                            title="Test Cloud Movie",
                            description="Cloud range regression",
                            thumbnail_url="",
                            video_url=CATALOG_PATH,
                            duration="1m",
                            release_year=2026,
                        )

                return Scalars()

        return Result()


async def mock_get_session():
    yield MockDb()


async def mock_resolve_media_source(_catalog_path: str) -> ResolvedMediaSource:
    return ResolvedMediaSource(
        catalog_path=CATALOG_PATH,
        relative_path=CATALOG_PATH.removeprefix("/media/"),
        local_path=Path(__file__).with_name("missing-cloud-fixture.mp4"),
        cloud_path=REMOTE_PATH,
        local_exists=False,
        cloud_exists=True,
        cloud_identity="fixture-v1",
    )


async def mock_open_cloud_chunks(_remote_path: str, start: int, length: int):
    async def chunks():
        selected = PAYLOAD[start:start + length]
        for offset in range(0, len(selected), 97):
            await asyncio.sleep(0)
            yield selected[offset:offset + 97]

    return chunks()


class CloudStreamingRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = mock_get_session
        app.dependency_overrides[get_current_user] = lambda: "mock-user"
        cls.client = TestClient(app)

    def request(self, range_value: str | None = None):
        headers = {"Range": range_value} if range_value else {}
        with (
            patch("routes.stream.resolve_media_source", side_effect=mock_resolve_media_source),
            patch("routes.stream.cloud_file_size", return_value=len(PAYLOAD)),
            patch("routes.stream.open_cloud_chunks", side_effect=mock_open_cloud_chunks),
            patch("routes.stream.download_file_from_cloud_task", return_value=None),
        ):
            return self.client.get("/api/stream/m_test_cloud_movie", headers=headers)

    def test_full_cloud_response_contains_declared_bytes(self) -> None:
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PAYLOAD)
        self.assertEqual(response.headers["content-length"], str(len(PAYLOAD)))

    def test_open_and_suffix_ranges_deliver_exact_bytes(self) -> None:
        response = self.request("bytes=500-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, PAYLOAD[500:])
        self.assertEqual(response.headers["content-range"], f"bytes 500-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")

        suffix = self.request("bytes=-73")
        self.assertEqual(suffix.status_code, 206)
        self.assertEqual(suffix.content, PAYLOAD[-73:])

    def test_invalid_range_fails_instead_of_returning_empty_success(self) -> None:
        response = self.request(f"bytes={len(PAYLOAD)}-")
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], f"bytes */{len(PAYLOAD)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
