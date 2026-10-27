"""Source estimates must not become media evidence or fixed multi-copy ceilings."""
import unittest

from services.ingestion_estimate import MIB, SourceStorageHints, estimate_ingestion


class EstimateTests(unittest.TestCase):
    def test_hls_peak_rate_and_complete_duration_replace_the_old_43_gib_request(self):
        hints = SourceStorageHints()
        hints.observe_manifest('#EXTM3U\n#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=2000000,BANDWIDTH=2654996,RESOLUTION=1920x1080\nchild.m3u8\n')
        hints.observe_manifest('#EXTM3U\n#EXTINF:3202,\none.ts\n#EXTINF:3202,\ntwo.ts\n#EXT-X-ENDLIST\n')
        estimate = estimate_ingestion(6420, {'video_storage_hints': hints.snapshot(), 'video_audio_tracks': 2})
        self.assertEqual(hints.duration_seconds, 6404)
        self.assertEqual(hints.bit_rate, 2654996)
        self.assertLess(estimate.workspace_bytes + estimate.preview_bytes, 9 * 1024**3)
        self.assertGreater(estimate.workspace_bytes, 2 * 1024**3)

    def test_live_incomplete_invalid_and_repeated_playlists_do_not_invent_duration(self):
        hints = SourceStorageHints()
        for text in ('#EXTINF:4,\nx.ts', '#EXTINF:nan,\nx.ts\n#EXT-X-ENDLIST',
                     '#EXTINF:-1,\nx.ts\n#EXT-X-ENDLIST', '#EXTINF:inf,\nx.ts\n#EXT-X-ENDLIST'):
            hints.observe_manifest(text)
        self.assertEqual(hints.duration_seconds, 0)
        valid = '#EXTINF:5,\nx.ts\n#EXT-X-ENDLIST'
        hints.observe_manifest(valid); hints.observe_manifest(valid)
        self.assertEqual(hints.duration_seconds, 5)

    def test_missing_rates_use_conservative_fallback_and_external_audio_is_included(self):
        unknown = estimate_ingestion(3600, {})
        self.assertGreater(unknown.workspace_bytes, 3600 * 2 * MIB)
        self.assertLess(unknown.workspace_bytes, 12 * 1024**3)
        base = {'video_storage_hints': {'bit_rate': 2_000_000}, 'video_audio_tracks': 2}
        self.assertGreater(estimate_ingestion(3600, base, separate_audio=True).workspace_bytes,
                           estimate_ingestion(3600, base).workspace_bytes)
        self.assertEqual(estimate_ingestion(3600, base, subtitle_count=2).workspace_bytes,
                         estimate_ingestion(3600, base).workspace_bytes + 60 * MIB)

    def test_known_direct_size_and_bad_numeric_hints(self):
        known = estimate_ingestion(3600, {'video_storage_hints': {'source_bytes': 1024**3}})
        self.assertLess(known.workspace_bytes, 2 * 1024**3)
        for value in (None, 'nan', float('inf'), -1, {}, 10**400):
            estimate = estimate_ingestion(60, {'video_storage_hints': {'bit_rate': value, 'source_bytes': value}})
            self.assertGreaterEqual(estimate.workspace_bytes, 256 * MIB)
