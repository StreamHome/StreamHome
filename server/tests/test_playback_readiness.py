"""Disposable filesystem regressions for response-scoped HLS readiness."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.playback_prep import PlaybackPrepService, VIDEO_VERIFIED_MARKER


class PlaybackReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="playback-readiness-")
        self.addCleanup(self.directory.cleanup)
        self.service = PlaybackPrepService()
        self.service.cache_dir = Path(self.directory.name)
        self.rendition = self.service.cache_path("m_fixture", "abcd") / "video_original"
        self.rendition.mkdir(parents=True)
        self.playlist(["segment_00000.m4s"])
        (self.rendition / "init.mp4").write_bytes(b"init")
        (self.rendition / "segment_00000.m4s").write_bytes(b"fragment")
        (self.rendition / ".complete").write_text("1")
        (self.rendition / VIDEO_VERIFIED_MARKER).write_text("1")

    def playlist(self, references):
        (self.rendition / "playlist.m3u8").write_text(
            '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
            + "".join(f"#EXTINF:4,\n{reference}\n" for reference in references)
            + "#EXT-X-ENDLIST\n"
        )

    def test_one_response_scans_each_playlist_once_and_next_response_rechecks_files(self):
        with patch.object(self.service, "_read_playlist_readiness", wraps=self.service._read_playlist_readiness) as read:
            with self.service.readiness_snapshot():
                self.assertTrue(self.service.rendition_complete("m_fixture", "abcd", "video_original"))
                self.assertTrue(self.service.playlist_ready("m_fixture", "abcd", "video_original"))
                self.assertEqual(self.service.rendition_seekable_until("m_fixture", "abcd", "video_original"), 4)
                self.assertEqual(read.call_count, 1)
            (self.rendition / "segment_00000.m4s").unlink()
            with self.service.readiness_snapshot():
                self.assertFalse(self.service.rendition_complete("m_fixture", "abcd", "video_original"))
            self.assertEqual(read.call_count, 2)

    def test_missing_empty_and_unsafe_assets_are_rejected(self):
        for reference in ("missing.m4s", "empty.m4s", "../outside.m4s", "/outside.m4s"):
            with self.subTest(reference=reference):
                (self.rendition / "empty.m4s").write_bytes(b"")
                self.playlist([reference])
                self.assertFalse(self.service.playlist_ready("m_fixture", "abcd", "video_original"))

    def test_nested_relative_assets_and_marker_versions_remain_supported(self):
        (self.rendition / "nested").mkdir()
        (self.rendition / "nested/fragment.m4s").write_bytes(b"fragment")
        self.playlist(["nested/fragment.m4s"])
        self.assertTrue(self.service.rendition_complete("m_fixture", "abcd", "video_original"))
        (self.rendition / VIDEO_VERIFIED_MARKER).unlink()
        (self.rendition / ".verified-v1").write_text("1")
        self.assertFalse(self.service.rendition_complete("m_fixture", "abcd", "video_original"))

    def test_failed_snapshot_does_not_hide_newly_published_fragments(self):
        self.playlist(["later.m4s"])
        with self.service.readiness_snapshot():
            self.assertFalse(self.service.playlist_ready("m_fixture", "abcd", "video_original"))
        (self.rendition / "later.m4s").write_bytes(b"published")
        with self.service.readiness_snapshot():
            self.assertTrue(self.service.playlist_ready("m_fixture", "abcd", "video_original"))

    def test_long_playlist_is_scanned_once_for_all_response_consumers(self):
        references = [f"segment_{index:05}.m4s" for index in range(1542)]
        for reference in references:
            (self.rendition / reference).write_bytes(b"fragment")
        self.playlist(references)
        with patch.object(self.service, "_read_playlist_readiness", wraps=self.service._read_playlist_readiness) as read:
            with self.service.readiness_snapshot():
                for _ in range(8):
                    self.assertEqual(self.service.rendition_status("m_fixture", "abcd", "video_original"), "ready")
                self.assertEqual(self.service.rendition_seekable_until("m_fixture", "abcd", "video_original"), 6168)
                self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
