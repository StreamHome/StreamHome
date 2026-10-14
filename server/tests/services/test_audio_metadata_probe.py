"""Native audio metadata stays bounded without modifying embedded dubbing.

Run through run_isolated.py; every file and subprocess is synthetic/disposable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from lifecycle.media_work import supervised_output
from services import audio_extractor, state
from services.ffmpeg_input import local_media_input_options, native_media_environment


class AudioMetadataProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ffmpeg, self.ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        self.assertTrue(self.ffmpeg and self.ffprobe, "Native media tools are required")
        self.process_baseline = dict(state.ACTIVE_PROCESSES)

    async def asyncTearDown(self):
        self.assertEqual(state.ACTIVE_PROCESSES, self.process_baseline)
        self.temporary.cleanup()

    async def fixture(self, extension="mp4", languages=("eng", "tur")):
        destination = self.root / ("synthetic." + extension)
        command = [self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=160x90:rate=5:duration=12"]
        for frequency in (440, 880)[:len(languages)]:
            command += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100:duration=12"]
        command += ["-map", "0:v:0"]
        for index in range(len(languages)):
            command += ["-map", f"{index + 1}:a:0"]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac"]
        for index, language in enumerate(languages):
            command += [f"-metadata:s:a:{index}", f"language={language}", f"-disposition:a:{index}",
                        "default" if index == len(languages) - 1 else "0"]
        process = await asyncio.create_subprocess_exec(*command, str(destination), env=native_media_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await supervised_output(process, timeout=20)
        self.assertEqual(process.returncode, 0, stderr.decode())
        return destination

    async def inspect(self, path):
        process = await asyncio.create_subprocess_exec(self.ffprobe, "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index,codec_name:stream_tags=language:stream_disposition=default", "-of", "json",
            *local_media_input_options(), str(path), env=native_media_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await supervised_output(process, timeout=10)
        self.assertEqual(process.returncode, 0, stderr.decode())
        return json.loads(stdout)["streams"]

    def media_snapshot(self):
        result = {}
        for path in self.root.rglob("*"):
            if path.is_file():
                stat = path.stat()
                result[str(path.relative_to(self.root))] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_ino, stat.st_size, stat.st_mtime_ns
                )
        return result

    async def bounded_probe(self, path, **kwargs):
        observed = []
        async def measure(process, **options):
            # A tiny diagnostic budget exposes accidental packet/frame output
            # with a short fixture while retaining the real pipe supervisor.
            stdout, stderr = await supervised_output(process, stdout_limit=2048, **options)
            observed.append((len(stdout), json.loads(stdout)))
            return stdout, stderr
        with patch.object(audio_extractor, "supervised_output", measure):
            result = await audio_extractor.extract_audio_and_strip_video(str(path), **kwargs)
        return result, observed

    async def test_native_mp4_and_matroska_metadata_stays_small_and_preserves_both_tracks(self):
        for extension in ("mp4", "mkv"):
            with self.subTest(container=extension):
                path = await self.fixture(extension)
                before, streams = self.media_snapshot(), await self.inspect(path)
                labels, observations = await self.bounded_probe(path)
                self.assertEqual(labels, ["en", "tr"])
                self.assertEqual(len(observations), 1)
                size, payload = observations[0]
                self.assertLess(size, 2048)
                self.assertTrue({"packets", "frames", "packets_and_frames"}.isdisjoint(payload))
                self.assertEqual(len(payload["streams"]), 2)
                self.assertEqual([s["index"] for s in payload["streams"]], [1, 2])
                self.assertEqual([s["disposition"]["default"] for s in payload["streams"]], [0, 1])
                self.assertEqual(await self.inspect(path), streams)
                self.assertEqual(self.media_snapshot(), before)

    async def test_submitted_language_applies_only_to_default_track_without_rewriting_media(self):
        path = await self.fixture()
        before, streams = self.media_snapshot(), await self.inspect(path)
        labels, _ = await self.bounded_probe(path, default_lang="ja", override_primary=True)
        self.assertEqual(labels, ["en", "ja"])
        self.assertEqual(await self.inspect(path), streams)
        self.assertEqual(self.media_snapshot(), before)

    async def test_duplicate_languages_keep_both_streams_and_unique_identities(self):
        path = await self.fixture(languages=("tur", "tur"))
        before = self.media_snapshot()
        labels, observations = await self.bounded_probe(path)
        self.assertEqual(labels, ["tr", "tr_1"])
        self.assertEqual(len(observations[0][1]["streams"]), 2)
        self.assertEqual(self.media_snapshot(), before)

    async def test_untagged_tracks_keep_default_and_secondary_fallbacks(self):
        path = await self.fixture(languages=("und", "und"))
        before = self.media_snapshot()
        labels, _ = await self.bounded_probe(path, default_lang="en")
        self.assertEqual(labels, ["track_0", "en"])
        self.assertEqual(self.media_snapshot(), before)

    async def test_video_without_audio_is_a_successful_empty_result(self):
        path = await self.fixture(languages=())
        before = self.media_snapshot()
        labels, observations = await self.bounded_probe(path)
        self.assertEqual(labels, [])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0][1].get("streams"), [])
        self.assertEqual(self.media_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
