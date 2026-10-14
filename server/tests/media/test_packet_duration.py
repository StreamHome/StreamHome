"""Native selected packet endpoints remain authoritative when MKV tags lie."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from services import media_probe
from services.media_probe import probe_completed_media, probe_media_stream
from services.state import ACTIVE_PROCESSES
from tests.media import test_analysis_provenance as fixtures


class SelectedPacketDuration(unittest.IsolatedAsyncioTestCase):
    def packet_duration(self, path, stream="v:0"):
        result = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", stream,
            "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(path)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        packets = json.loads(result.stdout)["packets"]
        starts = [float(packet["pts_time"]) for packet in packets]
        end = max(float(packet["pts_time"]) + float(packet["duration_time"]) for packet in packets)
        return min(starts), end - min(starts)

    async def assert_measured(self, path):
        start, elapsed = self.packet_duration(path)
        final = await probe_completed_media(str(path))
        source = await probe_media_stream(str(path))
        self.assertTrue(final["valid_video"], final)
        self.assertEqual(final["duration_provenance"], "selected_stream_packets")
        self.assertAlmostEqual(final["probed_duration"], elapsed, places=5)
        self.assertAlmostEqual(source["video_duration"], elapsed, places=5)
        self.assertAlmostEqual(final["stream_manifest"][0]["startTime"], start, places=5)
        self.assertAlmostEqual(final["stream_manifest"][0]["duration"], elapsed, places=5)
        self.assertIsNone(final["analysis"]["estimated_duration"])
        return final

    async def test_optional_missing_tags_cannot_promote_long_audio_container_estimate(self):
        with tempfile.TemporaryDirectory(prefix="packet-no-tag-") as directory:
            for epoch in (0, 10):
                with self.subTest(epoch=epoch):
                    path = Path(directory) / f"video-{epoch}.mkv"
                    await fixtures.make_video(path, secondary="audio", epoch=epoch)
                    path.write_bytes(path.read_bytes().replace(b"DURATION", b"DURATIOX"))
                    final = await self.assert_measured(path)
                    self.assertAlmostEqual(final["probed_duration"], 1, places=5)
                    self.assertGreater(final["audio_metadata"][0]["duration"], 2.9)
                    self.assertFalse(final["analysis"]["direct_timeline_compatible"])

    async def test_stale_short_and_long_mkv_tags_are_not_selected_duration_evidence(self):
        with tempfile.TemporaryDirectory(prefix="packet-stale-tag-") as directory:
            path = Path(directory) / "video.mkv"
            command = [shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=320x180:rate=4:duration=3", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", str(path)]
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            original = path.read_bytes()
            self.assertIn(b"00:00:03.000000000", original)
            for tag in (b"00:00:01.000000000", b"00:00:09.000000000"):
                with self.subTest(tag=tag):
                    path.write_bytes(original.replace(b"00:00:03.000000000", tag))
                    final = await self.assert_measured(path)
                    self.assertEqual(final["probed_duration"], 3)

    async def test_multiple_video_endpoints_are_measured_independently_without_tags(self):
        with tempfile.TemporaryDirectory(prefix="packet-multi-video-") as directory:
            path = Path(directory) / "video.mkv"
            await fixtures.make_video(path)
            path.write_bytes(path.read_bytes().replace(b"DURATION", b"DURATIOX"))
            final = await self.assert_measured(path)
            self.assertEqual(final["probed_duration"], 1)
            self.assertEqual(final["stream_manifest"][1]["duration"], 3)
            self.assertFalse(final["analysis"]["direct_timeline_compatible"])

    async def test_vfr_packet_endpoint_has_no_constant_frame_rate_guess(self):
        with tempfile.TemporaryDirectory(prefix="packet-vfr-") as directory:
            path = Path(directory) / "video.mkv"
            command = [shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3", "-vf", "select='not(mod(n,3))+not(mod(n,7))'", "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", str(path)]
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            path.write_bytes(path.read_bytes().replace(b"DURATION", b"DURATIOX"))
            await self.assert_measured(path)

    async def test_packet_output_limit_reaps_child_and_withholds_acceptance(self):
        with tempfile.TemporaryDirectory(prefix="packet-output-bound-") as directory:
            path = Path(directory) / "video.mkv"
            await fixtures.make_video(path)
            real_supervisor = media_probe.supervised_output
            async def supervise(process, **kwargs):
                if kwargs.get("stdout_limit") == 64 * 1024 * 1024:
                    kwargs["stdout_limit"] = 32
                return await real_supervisor(process, **kwargs)
            with patch.object(media_probe, "supervised_output", supervise):
                final = await probe_completed_media(str(path))
            self.assertFalse(final["valid_video"])
            self.assertEqual(final["analysis"]["duration_provenance"], "unknown")
            self.assertIsNone(final["analysis"]["probed_duration"])
            self.assertGreater(final["analysis"]["estimated_duration"], 0)
            self.assertFalse(final["analysis"]["direct_timeline_compatible"])
            self.assertFalse(any(key.startswith("packet-endpoint-probe:") for key in ACTIVE_PROCESSES))

    async def test_packet_timeout_reaps_child_and_rejects_unmeasured_selected_duration(self):
        with tempfile.TemporaryDirectory(prefix="packet-timeout-bound-") as directory:
            path = Path(directory) / "video.mkv"
            await fixtures.make_video(path)
            real_supervisor = media_probe.supervised_output
            async def supervise(process, **kwargs):
                if kwargs.get("stdout_limit") == 64 * 1024 * 1024:
                    kwargs["timeout"] = 0.000001
                return await real_supervisor(process, **kwargs)
            with patch.object(media_probe, "supervised_output", supervise):
                final = await probe_completed_media(str(path))
            self.assertFalse(final["valid_video"])
            self.assertIsNone(final["analysis"]["probed_duration"])
            self.assertFalse(any(key.startswith("packet-endpoint-probe:") for key in ACTIVE_PROCESSES))
