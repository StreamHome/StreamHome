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


if __name__ == "__main__":
    test_failure_classification()
    test_compact_and_redacted_diagnostics()
    test_diagnostics_file_redacts_secrets()
    test_queue_contracts()
    print("Queue failure handling regression checks passed.")
