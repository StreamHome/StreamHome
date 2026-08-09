import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.ingestion_errors import (
    classify_failure,
    compact_diagnostics,
    sanitize_url,
    write_task_diagnostics,
)
from services.ffmpeg_input import ffmpeg_network_input_options, is_hls_media_source
from services.media_source import (
    MediaSourceError,
    catalog_path_from_storage,
    local_catalog_source_exists,
    local_playback_fingerprint,
    local_video_fingerprint,
)
from services.audio_extractor import apply_primary_audio_language, audio_track_labels
from config import settings


def test_failure_classification() -> None:
    not_found = classify_failure("HTTP error 404 Not Found\nError opening input")
    assert not_found.code == "SOURCE_NOT_FOUND"
    assert not not_found.retryable

    forbidden = classify_failure("Server returned 403 Forbidden")
    assert forbidden.code == "SOURCE_FORBIDDEN"
    assert not forbidden.retryable

    rate_limited = classify_failure("HTTP error 429 Too Many Requests")
    assert rate_limited.code == "SOURCE_RATE_LIMITED"
    assert rate_limited.retryable

    upstream = classify_failure("Server returned 503 Service Unavailable")
    assert upstream.code == "SOURCE_UNAVAILABLE"
    assert upstream.retryable

    timeout = classify_failure("Connection timed out while opening input")
    assert timeout.code == "SOURCE_UNREACHABLE"
    assert timeout.retryable

    unsupported_option = classify_failure("Option extension_picky not found.\nError opening input files: Option not found")
    assert unsupported_option.code == "FFMPEG_OPTION_UNSUPPORTED"
    assert not unsupported_option.retryable


def test_ffmpeg_input_options_are_source_specific() -> None:
    direct_options = ffmpeg_network_input_options("http://127.0.0.1:9000/video.mp4")
    assert "-protocol_whitelist" in direct_options
    assert "-allowed_extensions" not in direct_options
    assert "-extension_picky" not in direct_options

    manifest_options = ffmpeg_network_input_options("https://sender.example/master.m3u8?token=secret")
    assert is_hls_media_source("https://sender.example/master.m3u8?token=secret")
    assert "-allowed_extensions" in manifest_options
    assert "-extension_picky" in manifest_options

    query_manifest_options = ffmpeg_network_input_options("https://sender.example/play?format=m3u8")
    assert "-allowed_extensions" in query_manifest_options

    disguised_manifest_options = ffmpeg_network_input_options(
        "https://sender.example/hls/movie.mp4/txt/master.txt",
        "hls",
    )
    assert is_hls_media_source("https://sender.example/hls/movie.mp4/txt/master.txt", "hls")
    assert "-allowed_extensions" in disguised_manifest_options
    assert "-extension_picky" in disguised_manifest_options
    assert disguised_manifest_options[-2:] == ["-f", "hls"]

    assert ffmpeg_network_input_options("C:/media/video.mp4") == []


def test_storage_paths_become_canonical_media_urls() -> None:
    original_media_dir = settings.MEDIA_DIR
    original_temp_dir = settings.TEMP_DIR
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings.MEDIA_DIR = str(root / "media")
            settings.TEMP_DIR = str(root / "temp")
            movie_file = root / "media" / "Movies" / "Movie_TMDB_1" / "movie.mp4"
            episode_file = root / "temp" / "Series" / "Show_TMDB_2" / "Season_1" / "Episode_1" / "episode.mp4"

            assert catalog_path_from_storage(str(movie_file)) == "/media/Movies/Movie_TMDB_1/movie.mp4"
            assert catalog_path_from_storage(str(episode_file)) == "/media/Series/Show_TMDB_2/Season_1/Episode_1/episode.mp4"
            assert not local_catalog_source_exists("/media/Movies/Movie_TMDB_1/movie.mp4")
            movie_file.parent.mkdir(parents=True)
            movie_file.write_bytes(b"completed-local-media")
            assert local_catalog_source_exists("/media/Movies/Movie_TMDB_1/movie.mp4")
            assert not local_catalog_source_exists("https://media.example.test/movie.mp4")
            try:
                catalog_path_from_storage(str(root / "outside" / "video.mp4"))
            except MediaSourceError:
                pass
            else:
                raise AssertionError("An out-of-storage media path must be rejected")
    finally:
        settings.MEDIA_DIR = original_media_dir
        settings.TEMP_DIR = original_temp_dir


def test_local_recovery_fingerprint_detects_video_and_audio_changes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        media_path = Path(temp_dir) / "movie.mp4"
        audio_dir = media_path.parent / "audio"
        audio_path = audio_dir / "en.mp3"
        media_path.write_bytes(b"video-v1")
        audio_dir.mkdir()
        audio_path.write_bytes(b"audio-v1")
        audio_stat = audio_path.stat()
        audio_metadata = [{
            "source": "external",
            "fileName": audio_path.name,
            "fileSize": audio_stat.st_size,
            "modifiedAt": audio_stat.st_mtime_ns,
        }]

        expected = local_playback_fingerprint(media_path, audio_metadata)
        expected_video = local_video_fingerprint(media_path)
        assert local_playback_fingerprint(media_path, audio_metadata) == expected
        assert local_video_fingerprint(media_path) == expected_video

        media_path.write_bytes(b"video-v2-expanded")
        assert local_playback_fingerprint(media_path, audio_metadata) != expected
        assert local_video_fingerprint(media_path) != expected_video

        video_updated = local_playback_fingerprint(media_path, audio_metadata)
        video_identity = local_video_fingerprint(media_path)
        audio_path.write_bytes(b"audio-v2-expanded")
        assert local_playback_fingerprint(media_path, audio_metadata) != video_updated
        assert local_video_fingerprint(media_path) == video_identity


def test_audio_track_labels_are_stable_across_reingestion() -> None:
    streams = [
        {"tags": {"language": "eng"}},
        {"tags": {"language": "eng"}},
        {"tags": {"language": "und"}},
    ]
    assert audio_track_labels(streams, "en") == ["en", "en_1", "track_2"]
    assert audio_track_labels([{"tags": {}}], "en") == ["en"]
    assert audio_track_labels(streams, "tr", override_primary=True) == ["tr", "en", "track_2"]
    corrected = apply_primary_audio_language(
        [{"index": 0, "language": "eng", "label": "English", "default": True}],
        "tr",
    )
    assert corrected[0]["language"] == "tr"
    assert corrected[0]["label"] == "TR"


def test_compact_and_redacted_diagnostics() -> None:
    verbose = """ffmpeg version 8.1
configuration: --enable-everything
Error opening input file https://user:secret@example.com/video.m3u8?token=top-secret.
Error opening input files: Server returned 404 Not Found
"""
    summary = compact_diagnostics(verbose)
    assert "\n" not in summary
    assert "configuration" not in summary
    assert len(summary) <= 280
    assert sanitize_url("https://user:secret@example.com/video.m3u8?token=top-secret#part") == "https://example.com/video.m3u8"


def test_diagnostics_file_redacts_secrets() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = write_task_diagnostics(
            "queue-test",
            "ffmpeg",
            "Failed https://sender.example/video.m3u8?token=secret-value and returned 404",
            temp_dir=temp_dir,
        )
        assert path is not None
        content = Path(path).read_text(encoding="utf-8")
        assert "secret-value" not in content
        assert "https://sender.example/video.m3u8" in content
        assert "returned 404" in content


def test_queue_contracts() -> None:
    queue_source = Path(__file__).parents[1].joinpath("services", "queue.py").read_text(encoding="utf-8")
    ffmpeg_source = Path(__file__).parents[1].joinpath("services", "ffmpeg.py").read_text(encoding="utf-8")
    route_source = Path(__file__).parents[1].joinpath("routes", "queue.py").read_text(encoding="utf-8")
    assert "if not last_failure.retryable" in queue_source
    assert "raise IngestionTaskError(probe_res[\"failure\"])" in queue_source
    assert "await queue_manager.stop()" in Path(__file__).parents[1].joinpath("main.py").read_text(encoding="utf-8")
    assert "Running threaded exec command" not in ffmpeg_source
    assert "traceback.print_exc" not in ffmpeg_source
    assert ".part{output_ext" in ffmpeg_source
    assert "preserve_local_media" in route_source
    assert "preserve_local_episode" in route_source
    process_source = queue_source[queue_source.index("    async def _process_task"):queue_source.index("    async def _record_task_failure")]
    catalog_position = process_source.index("await self._catalog_media")
    preparation_commit_position = process_source.index("await db.commit()", catalog_position)
    playback_index_position = process_source.index("await self._prepare_ingested_media", preparation_commit_position)
    cloud_upload_position = process_source.index("await self.run_rclone_move_dir", playback_index_position)
    publication_position = process_source.index("await self._finalize_catalog_media", cloud_upload_position)
    completion_position = process_source.index('task.status = "COMPLETED"', cloud_upload_position)
    assert catalog_position < preparation_commit_position < playback_index_position < cloud_upload_position
    assert cloud_upload_position < publication_position < completion_position
    assert "finalize=False" in process_source
    assert "adaptive segments will be generated at the requested timestamp" in queue_source
    assert "wait_until_fully_prepared" not in queue_source
    assert "PLAYBACK_CACHE_MIGRATION_FAILED" not in queue_source
    assert "Rclone can take minutes or hours" in process_source
    assert "CATALOG_UPDATE_FAILED" in queue_source
    vibe_source = Path(__file__).parents[1].joinpath("services", "vibe_analysis.py").read_text(encoding="utf-8")
    analyze_source = vibe_source[vibe_source.index("    async def analyze_entity"):]
    assert "AsyncSession(engine, expire_on_commit=False)" in analyze_source


def test_catalog_recovery_releases_sqlite_writes_between_media_entries() -> None:
    queue_source = Path(__file__).parents[1].joinpath("services", "queue.py").read_text(encoding="utf-8")
    recovery_source = queue_source[
        queue_source.index("    async def sync_media_from_disk"):
        queue_source.index("\nqueue_manager = DownloadQueueManager()")
    ]

    movie_reconciliation_commit = recovery_source.index(
        "# Commit movie reconciliation before the episode query"
    )
    episode_query = recovery_source.index("# Reconcile Episodes")
    sweep_session = recovery_source.index("async with AsyncSession(engine, expire_on_commit=False) as db:")
    assert sweep_session < movie_reconciliation_commit
    assert "await db.commit()" in recovery_source[movie_reconciliation_commit:episode_query]

    catalog_call = recovery_source.index("await self._catalog_media")
    recovered_count = recovery_source.index("count += 1", catalog_call)
    assert "await db.commit()" in recovery_source[catalog_call:recovered_count]

    recovery_error = recovery_source.index("except Exception as e:", catalog_call)
    recovery_log = recovery_source.index("logger.error", recovery_error)
    assert "await db.rollback()" in recovery_source[recovery_error:recovery_log]

    existing_movie = recovery_source.index(
        'if existing and not (existing.title.startswith("Captured ")'
    )
    restore_movie = recovery_source.index(
        'logger.info(f"[Queue Manager Recovery] Restoring Movie',
        existing_movie,
    )
    assert "await db.commit()" in recovery_source[existing_movie:restore_movie]
    assert "existing.video_url = catalog_path_from_storage(abs_video_path)" in recovery_source[existing_movie:restore_movie]
    assert "existing.preview_task_id = None" in recovery_source[existing_movie:restore_movie]
    assert 'existing.availability = "available"' in recovery_source[existing_movie:restore_movie]

    existing_episode = recovery_source.index(
        'if existing and not (existing.title.startswith("Episode ")'
    )
    restore_episode = recovery_source.index(
        'logger.info(f"[Queue Manager Recovery] Restoring Episode',
        existing_episode,
    )
    assert "await db.commit()" in recovery_source[existing_episode:restore_episode]
    assert "existing.video_url = catalog_path_from_storage(abs_video_path)" in recovery_source[existing_episode:restore_episode]
    assert "existing.preview_task_id = None" in recovery_source[existing_episode:restore_episode]

    existing_series = recovery_source.index(
        'if existing and not (existing.title.startswith("Captured ")',
        existing_episode,
    )
    restore_series = recovery_source.index(
        'logger.info(f"[Queue Manager Recovery] Restoring Series',
        existing_series,
    )
    assert "await db.rollback()" in recovery_source[existing_series:restore_series]

    fingerprint_check = recovery_source.index("current_recovery_fingerprint = local_playback_fingerprint")
    audio_extraction = recovery_source.index("extracted_langs = await extract_audio_and_strip_video")
    completed_probe = recovery_source.index("probe_info = await probe_completed_media(abs_video_path)")
    assert fingerprint_check < audio_extraction < completed_probe
    assert "stored_video_fingerprint != current_video_fingerprint" in recovery_source[fingerprint_check:audio_extraction]
    assert recovery_source.count("await probe_completed_media(abs_video_path)") == 1


def test_database_writing_workers_wait_for_catalog_recovery() -> None:
    main_source = Path(__file__).parents[1].joinpath("main.py").read_text(encoding="utf-8")
    assert "catalog_recovery_complete = asyncio.Event()" in main_source
    recovery_source = main_source[
        main_source.index("    async def recover_catalog_and_playback()"):
        main_source.index("    async def start_runtime_workers()")
    ]
    catalog_sync = recovery_source.index("await queue_manager.sync_media_from_disk()")
    catalog_barrier = recovery_source.index("catalog_recovery_complete.set()")
    vibe_start = recovery_source.index("await vibe_analysis_manager.start()")
    playback_warming = recovery_source.index("await playback_prep_service.schedule_catalog_baselines()")
    assert catalog_sync < catalog_barrier < vibe_start < playback_warming
    assert "finally:\n                catalog_recovery_complete.set()" in recovery_source

    worker_sections = {
        "runtime workers": ("    async def start_runtime_workers()", "    async def daily_backup_worker()"),
        "daily backup": ("    async def daily_backup_worker()", "    background_tasks.append(asyncio.create_task(daily_backup_worker()"),
        "automatic updates": ("    async def guarded_automatic_update_worker()", "    background_tasks.append(asyncio.create_task(guarded_automatic_update_worker()"),
        "recommendations": ("    async def guarded_recommendation_worker()", "    recommendation_task = asyncio.create_task"),
        "playback reaper": ("    async def guarded_playback_reaper()", "    reaper_task = asyncio.create_task"),
    }
    for label, (start_marker, end_marker) in worker_sections.items():
        section = main_source[
            main_source.index(start_marker):
            main_source.index(end_marker, main_source.index(start_marker))
        ]
        assert "await catalog_recovery_complete.wait()" in section, label


if __name__ == "__main__":
    test_failure_classification()
    test_ffmpeg_input_options_are_source_specific()
    test_storage_paths_become_canonical_media_urls()
    test_local_recovery_fingerprint_detects_video_and_audio_changes()
    test_audio_track_labels_are_stable_across_reingestion()
    test_compact_and_redacted_diagnostics()
    test_diagnostics_file_redacts_secrets()
    test_queue_contracts()
    test_catalog_recovery_releases_sqlite_writes_between_media_entries()
    test_database_writing_workers_wait_for_catalog_recovery()
    print("Queue failure handling regression checks passed.")
