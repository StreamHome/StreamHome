"""Interactive end-to-end smoke test for StreamHome media ingestion.

The ingestion API intentionally accepts only HTTP(S) media sources. When a local
file is selected, this utility exposes exactly that file through a short-lived,
range-capable loopback HTTP server and keeps it alive until the queue finishes.

Run from any directory:

    python server/scratch/test_ingest_stream.py --video C:\\path\\to\\video.mp4

The script reads the same root and server ``.env`` files as StreamHome. It never
contains, prints, or persists authentication secrets.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit

import httpx
from dotenv import load_dotenv


SERVER_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVER_DIR.parent
DATABASE_PATH = (SERVER_DIR / "database.db").resolve()
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
DEFAULT_INTRODB_API_URL = "https://api.theintrodb.org/v3"
CONSOLE_PREFIX = "[Ingestion Test]"
TERMINAL_TASK_STATES = {"COMPLETED", "FAILED"}
SEGMENT_TYPES = ("intro", "recap", "credits", "preview")
END_OF_VIDEO_SENTINEL_MS = 86_400_000


def load_environment() -> None:
    """Load configuration in the same order used by ``server/config.py``."""

    load_dotenv(REPOSITORY_ROOT / ".env", override=False)
    load_dotenv(SERVER_DIR / ".env", override=False)


def is_http_url(value: str) -> bool:
    try:
        return urlsplit(value).scheme.lower() in {"http", "https"}
    except ValueError:
        return False


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def existing_video_or_url(value: str) -> str:
    value = value.strip().strip('"')
    if is_http_url(value):
        return value
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Media file does not exist: {path}")
    return str(path)


def http_headers_for_tmdb(read_access_token: str, api_key: str) -> tuple[dict[str, str], dict[str, Any]]:
    headers = {"Accept": "application/json", "User-Agent": "StreamHome-Ingestion-Test/1.0"}
    params: dict[str, Any] = {}
    if read_access_token:
        headers["Authorization"] = f"Bearer {read_access_token}"
    elif api_key:
        params["api_key"] = api_key
    else:
        raise RuntimeError("TMDB_READ_ACCESS_TOKEN or TMDB_API_KEY is required for search.")
    return headers, params


def search_tmdb(query: str, read_access_token: str, api_key: str) -> dict[str, Any] | None:
    """Search TMDB multi-search and let the operator select a movie or series."""

    headers, params = http_headers_for_tmdb(read_access_token, api_key)
    params.update({"query": query, "include_adult": "false", "language": "en-US", "page": 1})
    print(f"{CONSOLE_PREFIX} Searching TMDB for {query!r}...")

    try:
        response = httpx.get(
            "https://api.themoviedb.org/3/search/multi",
            headers=headers,
            params=params,
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        print(f"{CONSOLE_PREFIX} TMDB search failed: {error}")
        return None

    results = [
        item
        for item in response.json().get("results", [])
        if item.get("media_type") in {"movie", "tv"} and item.get("adult") is not True
    ]
    if not results:
        print(f"{CONSOLE_PREFIX} TMDB returned no movie or TV results.")
        return None

    print("\nTMDB search results")
    for index, item in enumerate(results, start=1):
        title = item.get("title") or item.get("name") or "Untitled"
        release_date = item.get("release_date") or item.get("first_air_date") or ""
        year = release_date[:4] or "unknown year"
        label = "Movie" if item["media_type"] == "movie" else "TV"
        print(f"  {index:>2}. {title} ({year}) [{label}] — TMDB {item['id']}")

    selection = input("Select a result number (blank cancels): ").strip()
    if not selection.isdigit() or not 1 <= int(selection) <= len(results):
        print(f"{CONSOLE_PREFIX} Search selection cancelled.")
        return None

    selected = results[int(selection) - 1]
    return {
        "id": int(selected["id"]),
        "media_type": str(selected["media_type"]),
        "title": selected.get("title") or selected.get("name") or f"TMDB {selected['id']}",
    }


def ffprobe_executable() -> str | None:
    bundled_names = ("ffprobe.exe", "ffprobe") if os.name == "nt" else ("ffprobe", "ffprobe.exe")
    for name in bundled_names:
        bundled = REPOSITORY_ROOT / "bin" / name
        if bundled.is_file():
            return str(bundled)
    return shutil.which("ffprobe")


def probe_duration_ms(source: str) -> int | None:
    """Return media duration for TheIntroDB release matching when possible."""

    executable = ffprobe_executable()
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                source,
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=45,
        )
        duration_seconds = float(completed.stdout.strip())
        if duration_seconds > 0:
            return round(duration_seconds * 1000)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def _milliseconds(value: Any, fallback_seconds: Any = None) -> int | None:
    if value is not None:
        try:
            return round(float(value))
        except (TypeError, ValueError):
            return None
    if fallback_seconds is not None:
        try:
            return round(float(fallback_seconds) * 1000)
        except (TypeError, ValueError):
            return None
    return None


def normalize_introdb_markers(data: Mapping[str, Any], duration_ms: int | None) -> dict[str, list[dict[str, float]]]:
    """Convert TheIntroDB v3 millisecond segments into StreamHome seconds."""

    normalized: dict[str, list[dict[str, float]]] = {segment_type: [] for segment_type in SEGMENT_TYPES}
    for segment_type in SEGMENT_TYPES:
        raw_segments = data.get(segment_type)
        if not isinstance(raw_segments, list):
            continue
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, Mapping):
                continue
            start_ms = _milliseconds(raw_segment.get("start_ms"), raw_segment.get("start"))
            end_ms = _milliseconds(raw_segment.get("end_ms"), raw_segment.get("end"))
            if end_ms is None and segment_type == "credits":
                end_ms = duration_ms or END_OF_VIDEO_SENTINEL_MS
            if start_ms is None or end_ms is None or start_ms < 0 or end_ms <= start_ms:
                continue
            normalized[segment_type].append(
                {"start": round(start_ms / 1000, 3), "end": round(end_ms / 1000, 3)}
            )
    return normalized


def fetch_introdb_markers(
    tmdb_id: int,
    media_type: str,
    season: int | None,
    episode: int | None,
    duration_ms: int | None,
    api_url: str,
    api_key: str,
) -> dict[str, list[dict[str, float]]]:
    """Fetch skip segments from TheIntroDB v3 without making ingestion depend on it."""

    empty = {segment_type: [] for segment_type in SEGMENT_TYPES}
    params: dict[str, Any] = {"tmdb_id": tmdb_id}
    if media_type == "tv":
        params.update({"season": season, "episode": episode})
    if duration_ms:
        params["duration_ms"] = duration_ms

    user_agent = "StreamHome-Ingestion-Test/1.0"
    headers = {"Accept": "application/json", "User-Agent": user_agent, "X-User-Agent": user_agent}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/media",
            params=params,
            headers=headers,
            timeout=12.0,
        )
        if response.status_code in {404, 422}:
            print(f"{CONSOLE_PREFIX} TheIntroDB has no matching markers for this release.")
            return empty
        if response.status_code == 429:
            print(f"{CONSOLE_PREFIX} TheIntroDB rate limit reached; continuing without skip markers.")
            return empty
        response.raise_for_status()
        markers = normalize_introdb_markers(response.json(), duration_ms)
        marker_count = sum(len(items) for items in markers.values())
        print(f"{CONSOLE_PREFIX} Loaded {marker_count} skip marker(s) from TheIntroDB.")
        return markers
    except (httpx.HTTPError, ValueError) as error:
        print(f"{CONSOLE_PREFIX} TheIntroDB unavailable ({error}); continuing without skip markers.")
        return empty


class _RangeRequestHandler(BaseHTTPRequestHandler):
    server_version = "StreamHomeLocalMedia/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def media_server(self) -> "_LocalMediaHTTPServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        self._serve(send_body=False)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._serve(send_body=True)

    def _serve(self, send_body: bool) -> None:
        request_path = urlsplit(self.path).path
        if request_path != self.media_server.route:
            self.send_error(404)
            return

        file_path = self.media_server.file_path
        file_size = file_path.stat().st_size
        start = 0
        end = max(0, file_size - 1)
        status_code = 200
        range_header = self.headers.get("Range")
        if range_header:
            parsed_range = self._parse_range(range_header, file_size)
            if parsed_range is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed_range
            status_code = 206

        content_length = max(0, end - start + 1)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(status_code)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        if status_code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Connection", "close")
        self.end_headers()

        if not send_body or content_length == 0:
            return
        with file_path.open("rb") as source:
            source.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    @staticmethod
    def _parse_range(header: str, file_size: int) -> tuple[int, int] | None:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
        if not match or file_size <= 0:
            return None
        first, last = match.groups()
        if not first and not last:
            return None
        if not first:
            suffix_length = int(last)
            if suffix_length <= 0:
                return None
            return max(0, file_size - suffix_length), file_size - 1
        start = int(first)
        end = int(last) if last else file_size - 1
        if start >= file_size or start > end:
            return None
        return start, min(end, file_size - 1)


class _LocalMediaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], file_path: Path, route: str):
        self.file_path = file_path
        self.route = route
        super().__init__(address, _RangeRequestHandler)


class LocalMediaBridge(AbstractContextManager["LocalMediaBridge"]):
    """Expose one local file to the locally running StreamHome backend."""

    def __init__(self, file_path: Path, host: str = "127.0.0.1"):
        self.file_path = file_path.resolve()
        token = uuid.uuid4().hex
        self.route = f"/streamhome-ingest/{token}/{quote(self.file_path.name)}"
        self.server = _LocalMediaHTTPServer((host, 0), self.file_path, self.route)
        self.thread = threading.Thread(target=self.server.serve_forever, name="local-media-bridge", daemon=True)
        self.host = host

    @property
    def url(self) -> str:
        port = int(self.server.server_address[1])
        return f"http://{self.host}:{port}{self.route}"

    def __enter__(self) -> "LocalMediaBridge":
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@dataclass(frozen=True)
class TaskResult:
    status: str
    title: str
    error_message: str | None


def read_task(task_id: str) -> TaskResult | None:
    if not DATABASE_PATH.is_file():
        return None
    with sqlite3.connect(DATABASE_PATH, timeout=5) as connection:
        row = connection.execute(
            "SELECT status, COALESCE(title, ''), error_message FROM downloadtask WHERE id = ?",
            (task_id,),
        ).fetchone()
    if not row:
        return None
    return TaskResult(status=str(row[0]), title=str(row[1]), error_message=row[2])


def wait_for_task(task_id: str, timeout_seconds: int) -> TaskResult:
    deadline = time.monotonic() + timeout_seconds
    previous_status: str | None = None
    while time.monotonic() < deadline:
        task = read_task(task_id)
        if task:
            if task.status != previous_status:
                print(f"{CONSOLE_PREFIX} Queue state: {task.status}")
                previous_status = task.status
            if task.status.upper() in TERMINAL_TASK_STATES:
                return task
        time.sleep(1)
    raise TimeoutError(f"Task {task_id} did not finish within {timeout_seconds} seconds.")


def catalog_snapshot(tmdb_id: int, media_type: str, season: int | None, episode: int | None) -> dict[str, Any] | None:
    media_id = f"m_{tmdb_id}" if media_type == "movie" else f"tv_{tmdb_id}"
    with sqlite3.connect(DATABASE_PATH, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        movie = connection.execute(
            """
            SELECT id, title, description, thumbnail_url, banner_url, availability,
                   video_url, skip_markers_str
            FROM movie WHERE id = ?
            """,
            (media_id,),
        ).fetchone()
        if not movie:
            return None
        snapshot: dict[str, Any] = dict(movie)
        if media_type == "tv" and season is not None and episode is not None:
            episode_id = f"ep_{tmdb_id}_s{season}_e{episode}"
            episode_row = connection.execute(
                """
                SELECT id, title, description, thumbnail_url, video_url, skip_markers_str
                FROM episode WHERE id = ?
                """,
                (episode_id,),
            ).fetchone()
            snapshot["episode"] = dict(episode_row) if episode_row else None
    return snapshot


def verify_catalog(tmdb_id: int, media_type: str, season: int | None, episode: int | None) -> bool:
    snapshot = catalog_snapshot(tmdb_id, media_type, season, episode)
    if not snapshot:
        print(f"{CONSOLE_PREFIX} Catalog verification failed: no canonical media row was created.")
        return False

    title = str(snapshot.get("title") or "")
    description = str(snapshot.get("description") or "")
    artwork = snapshot.get("thumbnail_url") or snapshot.get("banner_url")
    video_url = str((snapshot.get("episode") or snapshot).get("video_url") or "")
    marker_source = (snapshot.get("episode") or snapshot).get("skip_markers_str") or "{}"
    try:
        marker_count = sum(len(items) for items in json.loads(marker_source).values() if isinstance(items, list))
    except (AttributeError, json.JSONDecodeError):
        marker_count = 0

    print(f"{CONSOLE_PREFIX} Catalog title: {title or '(missing)'}")
    print(f"{CONSOLE_PREFIX} TMDB description: {'stored' if description else 'missing'}")
    print(f"{CONSOLE_PREFIX} TMDB artwork: {'stored' if artwork else 'missing'}")
    print(f"{CONSOLE_PREFIX} TheIntroDB markers stored: {marker_count}")
    print(f"{CONSOLE_PREFIX} Playable media URL: {video_url or '(missing)'}")

    canonical_title = bool(title and title != f"TMDB {tmdb_id}")
    playable = video_url.startswith("/media/")
    return canonical_title and bool(description or artwork) and playable


def build_payload(
    tmdb_id: int,
    media_type: str,
    video_url: str,
    season: int | None,
    episode: int | None,
    quality: str,
    language: str,
    skip_markers: Mapping[str, Sequence[Mapping[str, float]]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "video_url": video_url,
        "quality": quality,
        "language": language,
        "headers": {"User-Agent": "StreamHome-Ingestion-Test/1.0"},
        "skip_markers": {name: list(markers) for name, markers in skip_markers.items()},
    }
    if media_type == "tv":
        if season is None or episode is None:
            raise ValueError("TV ingestion requires both season and episode.")
        payload["season"] = season
        payload["episode"] = episode
    return payload


def submit_ingestion(backend_url: str, api_token: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    endpoint = f"{backend_url.rstrip('/')}/api/add-movie"
    response = httpx.post(
        endpoint,
        json=dict(payload),
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    if response.status_code != 201:
        detail = response.text
        try:
            detail = json.dumps(response.json(), ensure_ascii=False)
        except ValueError:
            pass
        raise RuntimeError(f"Server rejected the request ({response.status_code}): {detail}")
    return response.json()


def prompt_media_identity(args: argparse.Namespace) -> tuple[int, str, int | None, int | None]:
    tmdb_id = args.tmdb_id
    media_type = args.media_type
    if tmdb_id is None:
        query = args.query or input("Movie/TV title to search (blank for manual TMDB ID): ").strip()
        if query:
            selected = search_tmdb(
                query,
                os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip(),
                os.getenv("TMDB_API_KEY", "").strip(),
            )
            if selected:
                tmdb_id = selected["id"]
                media_type = selected["media_type"]
                print(f"{CONSOLE_PREFIX} Selected {selected['title']} (TMDB {tmdb_id}).")
        if tmdb_id is None:
            tmdb_id = positive_integer(input("TMDB ID: ").strip())

    if media_type is None:
        choice = input("Media type [movie/tv] (default movie): ").strip().lower()
        media_type = "tv" if choice in {"tv", "series", "2"} else "movie"
    media_type = "tv" if media_type in {"tv", "series"} else "movie"

    season = args.season
    episode = args.episode
    if media_type == "tv":
        season = season or positive_integer(input("Season number: ").strip())
        episode = episode or positive_integer(input("Episode number: ").strip())
    return int(tmdb_id), media_type, season, episode


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=os.getenv("BACKEND_URL", DEFAULT_BACKEND_URL))
    parser.add_argument("--video", help="Local media path or direct HTTP(S) media URL")
    parser.add_argument("--query", help="TMDB search query")
    parser.add_argument("--tmdb-id", type=positive_integer)
    parser.add_argument("--media-type", choices=("movie", "tv", "series"))
    parser.add_argument("--season", type=positive_integer)
    parser.add_argument("--episode", type=positive_integer)
    parser.add_argument("--quality", default="1080p")
    parser.add_argument("--language", default="en")
    parser.add_argument("--timeout", type=positive_integer, default=3600, help="Queue completion timeout in seconds")
    parser.add_argument("--no-wait", action="store_true", help="Return after the API accepts an HTTP(S) source")
    parser.add_argument("--skip-introdb", action="store_true", help="Do not query TheIntroDB")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    load_environment()
    args = parse_arguments(argv)
    api_token = os.getenv("API_BEARER_TOKEN", "").strip()
    if not api_token:
        print(f"{CONSOLE_PREFIX} API_BEARER_TOKEN is missing from the StreamHome environment.")
        return 2

    try:
        tmdb_id, media_type, season, episode = prompt_media_identity(args)
        raw_source = args.video or input("Local video path or HTTP(S) video URL: ").strip()
        source = existing_video_or_url(raw_source)
    except (ValueError, argparse.ArgumentTypeError) as error:
        print(f"{CONSOLE_PREFIX} Invalid input: {error}")
        return 2

    local_path = None if is_http_url(source) else Path(source)
    if local_path and urlsplit(args.server_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        print(f"{CONSOLE_PREFIX} A local file can only be bridged to a locally running backend.")
        return 2
    if local_path and args.no_wait:
        print(f"{CONSOLE_PREFIX} --no-wait cannot be used with a local file; the HTTP bridge must stay alive.")
        return 2

    duration_ms = probe_duration_ms(str(local_path)) if local_path else None
    if duration_ms:
        print(f"{CONSOLE_PREFIX} Detected duration: {duration_ms / 1000:.3f} seconds.")

    if args.skip_introdb:
        markers = {segment_type: [] for segment_type in SEGMENT_TYPES}
    else:
        markers = fetch_introdb_markers(
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            duration_ms=duration_ms,
            api_url=os.getenv("THEINTRODB_API_URL", DEFAULT_INTRODB_API_URL),
            api_key=os.getenv("THEINTRODB_API_KEY", "").strip(),
        )

    def execute(video_url: str) -> int:
        payload = build_payload(
            tmdb_id=tmdb_id,
            media_type=media_type,
            video_url=video_url,
            season=season,
            episode=episode,
            quality=args.quality,
            language=args.language,
            skip_markers=markers,
        )
        print(f"{CONSOLE_PREFIX} Submitting {media_type} TMDB {tmdb_id} to {args.server_url.rstrip('/')}...")
        try:
            response = submit_ingestion(args.server_url, api_token, payload)
        except (httpx.HTTPError, RuntimeError) as error:
            print(f"{CONSOLE_PREFIX} Request failed: {error}")
            return 1

        task_id = str(response.get("taskId") or "")
        print(f"{CONSOLE_PREFIX} Accepted as task {task_id} ({response.get('title', 'unknown title')}).")
        if args.no_wait:
            return 0
        if not DATABASE_PATH.is_file():
            print(f"{CONSOLE_PREFIX} Cannot monitor the task because {DATABASE_PATH} is unavailable.")
            return 1
        try:
            result = wait_for_task(task_id, args.timeout)
        except TimeoutError as error:
            print(f"{CONSOLE_PREFIX} {error}")
            return 1
        if result.status.upper() != "COMPLETED":
            print(f"{CONSOLE_PREFIX} Ingestion failed: {result.error_message or 'No diagnostic was recorded.'}")
            return 1
        print(f"{CONSOLE_PREFIX} Ingestion completed successfully.")
        if not verify_catalog(tmdb_id, media_type, season, episode):
            print(f"{CONSOLE_PREFIX} Catalog verification did not meet the smoke-test contract.")
            return 1
        print(f"{CONSOLE_PREFIX} TMDB metadata, skip markers, and playable catalog data verified.")
        return 0

    if local_path:
        with LocalMediaBridge(local_path) as bridge:
            print(f"{CONSOLE_PREFIX} Serving the selected file through a temporary loopback URL.")
            return execute(bridge.url)
    return execute(source)


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        print(f"\n{CONSOLE_PREFIX} Cancelled by user.")
        raise SystemExit(130)
