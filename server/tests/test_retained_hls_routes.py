"""Retained bytes must not become an unverified or private playback endpoint."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from routes import playback
from services.playback_prep import playback_prep_service


class RetainedHLSRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch = patch.object(playback_prep_service, 'cache_dir', Path(self.directory.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.root = playback_prep_service.cache_path('retained', 'abcd')
        self.root.mkdir(parents=True)
        for name in ['_run_cache_roots', '_run_cache_renditions', '_active_cache_leases', '_rendition_cache_leases']:
            state = patch.object(playback_prep_service, name, {})
            state.start()
            self.addCleanup(state.stop)

    def accepted_rendition(self, name):
        target = self.root / name
        target.mkdir()
        (target / 'init.mp4').write_bytes(b'init')
        (target / 'segment.m4s').write_bytes(b'accepted segment')
        (target / 'playlist.m3u8').write_text('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2,\nsegment.m4s\n#EXT-X-ENDLIST\n')
        (target / '.complete').write_text('1')
        (target / playback_prep_service._verified_marker_name(name)).write_text('1')
        return target

    async def test_master_binds_advertised_bytes_before_response_yields(self):
        self.accepted_rendition('video_original')
        self.accepted_rendition('audio_0')
        (self.root / 'master.m3u8').write_text('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio_0/playlist.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="audio"\nvideo_original/playlist.m3u8\n')
        payload = {'fingerprint': 'abcd', 'run_id': 'actual-run'}
        with patch.object(playback, 'validate_playback_ticket', AsyncMock(return_value=(payload, None, None))), patch.object(playback, 'playback_response_lease', return_value=Mock()):
            response = await playback.serve_master_manifest('retained', 'synthetic-ticket', None, None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(playback_prep_service._run_cache_renditions['actual-run'], {'video_original', 'audio_0'})
        playback_prep_service._rendition_cache_leases.clear()
        with self.assertRaisesRegex(Exception, 'still in use'):
            playback_prep_service._start_rendition_candidate('retained', 'abcd', 'video_original')

    async def test_asset_binds_its_exact_rendition_before_response_yields(self):
        self.accepted_rendition('video_original')
        payload = {'fingerprint': 'abcd', 'run_id': 'asset-run'}
        with patch.object(playback, 'validate_playback_ticket', AsyncMock(return_value=(payload, None, None))), patch.object(playback, 'playback_response_lease', return_value=Mock()):
            response = await playback.serve_hls_asset('retained', 'video_original/segment.m4s', 'synthetic-ticket', None)
        self.assertEqual(response.status_code, 200)
        self.assertIn('video_original', playback_prep_service._run_cache_renditions['asset-run'])

    async def test_selected_verified_quality_absent_from_stale_master_cannot_return_empty_playlist(self):
        self.accepted_rendition('video_original')
        self.accepted_rendition('video_480p')
        (self.root / 'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nvideo_original/playlist.m3u8\n')
        payload = {'fingerprint': 'abcd', 'run_id': 'selected-run'}
        with patch.object(playback, 'validate_playback_ticket', AsyncMock(return_value=(payload, None, None))), patch.object(playback, 'playback_response_lease', return_value=Mock()), patch.object(playback, 'rendition_contract', return_value=[SimpleNamespace(id='video_original'), SimpleNamespace(id='video_480p')]), patch.object(playback_prep_service, 'touch') as touch:
            with self.assertRaises(HTTPException) as raised:
                await playback.serve_master_manifest('retained', 'synthetic-ticket', None, 'video_480p')
            self.assertEqual(raised.exception.status_code, 404)
            touch.assert_not_called()
        self.assertEqual(playback_prep_service._run_cache_renditions, {})

    async def test_failed_master_read_does_not_lease_unserved_failed_cache(self):
        (self.root / 'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nvideo_original/playlist.m3u8\n')
        payload = {'fingerprint': 'abcd', 'run_id': 'new-run'}
        with patch.object(playback, 'validate_playback_ticket', AsyncMock(return_value=(payload, None, None))), patch.object(playback, 'playback_response_lease', return_value=Mock()), patch.object(playback_prep_service, 'touch') as touch:
            with self.assertRaises(HTTPException) as raised:
                await playback.serve_master_manifest('retained', 'synthetic-ticket', None, None)
            self.assertEqual(raised.exception.status_code, 425)
            touch.assert_not_called()
        self.assertEqual(playback_prep_service._run_cache_renditions, {})

    async def test_hidden_archived_and_source_bytes_never_serve(self):
        for relative in ['.sources/source/original.mp4', '.video_original.retained-old/init.mp4', 'video_original/.retained-invalid']:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'private retained bytes')
            with patch.object(playback, 'validate_playback_ticket', AsyncMock(return_value=({'fingerprint': 'abcd'}, None, None))):
                with self.assertRaises(HTTPException) as raised:
                    await playback.serve_hls_asset('retained', relative, 'synthetic-ticket', None)
            self.assertIn(raised.exception.status_code, [403, 404])
            self.assertEqual(path.read_bytes(), b'private retained bytes')

    def test_master_rejects_recursive_master_child(self):
        content = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nmaster.m3u8\n'
        with patch.object(playback_prep_service, 'retained_asset_available', return_value=True):
            with self.assertRaises(HTTPException) as raised:
                playback.retained_master_playlist(content, 'retained', 'abcd')
        self.assertEqual(raised.exception.status_code, 425)

    def test_master_never_advertises_dangling_audio_group(self):
        content = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio_0/playlist.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="audio"\nvideo_original/playlist.m3u8\n'
        with patch.object(playback_prep_service, 'retained_asset_available', side_effect=lambda media, fingerprint, uri: uri.startswith('video_')):
            with self.assertRaises(HTTPException) as raised:
                playback.retained_master_playlist(content, 'retained', 'abcd')
        self.assertEqual(raised.exception.status_code, 425)

    def test_master_keeps_valid_av_and_omits_retired_quality(self):
        content = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio_0/playlist.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="audio"\nvideo_original/playlist.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=50,AUDIO="audio"\nvideo_480p/playlist.m3u8\n'
        with patch.object(playback_prep_service, 'retained_asset_available', side_effect=lambda media, fingerprint, uri: uri != 'video_480p/playlist.m3u8'), patch.object(playback_prep_service, 'playlist_ready', return_value=True):
            rendered = playback.retained_master_playlist(content, 'retained', 'abcd')
        self.assertIn('audio_0/playlist.m3u8', rendered)
        self.assertIn('video_original/playlist.m3u8', rendered)
        self.assertNotIn('video_480p', rendered)
