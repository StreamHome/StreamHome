from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from services.media_timing import canonical_audio_filter_chain


class MediaTimingRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg is unavailable")

    def _first_flash_time(self) -> float:
        frame_rate = 25
        width = 64
        height = 64
        result = subprocess.run(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel", "error",
                "-f", "lavfi",
                "-i", (
                    "color=black:size=64x64:rate=25:duration=2,"
                    "drawbox=color=white:t=fill:enable='between(t,1,1.12)'"
                ),
                "-pix_fmt", "gray",
                "-f", "rawvideo",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        )
        frame_size = width * height
        frames = [result.stdout[index:index + frame_size] for index in range(0, len(result.stdout), frame_size)]
        first_flash = next(index for index, frame in enumerate(frames) if frame and sum(frame) / len(frame) > 200)
        return first_flash / frame_rate

    def _first_impulse_time(self, timeline_offset: float) -> float:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "impulse.wav"
            subprocess.run(
                [
                    str(self.ffmpeg),
                    "-hide_banner",
                    "-loglevel", "error",
                    "-y",
                    "-f", "lavfi",
                    "-i", "aevalsrc=if(between(t\\,1.2\\,1.3)\\,0.9\\,0):s=48000:d=2",
                    "-af", ",".join(canonical_audio_filter_chain(timeline_offset, 2)),
                    "-c:a", "pcm_s16le",
                    str(target),
                ],
                check=True,
                capture_output=True,
            )
            with wave.open(str(target), "rb") as audio:
                sample_rate = audio.getframerate()
                sample_width = audio.getsampwidth()
                self.assertEqual(sample_width, 2)
                payload = audio.readframes(audio.getnframes())
            samples = memoryview(payload).cast("h")
            first_impulse = next(index for index, sample in enumerate(samples) if abs(sample) > 1_000)
            return first_impulse / sample_rate

    def test_content_markers_align_within_eighty_milliseconds(self) -> None:
        flash_time = self._first_flash_time()
        aligned_impulse_time = self._first_impulse_time(-0.2)
        shifted_impulse_time = self._first_impulse_time(0.0)

        self.assertLessEqual(abs(aligned_impulse_time - flash_time), 0.08)
        self.assertGreater(abs(shifted_impulse_time - flash_time), 0.08)


if __name__ == "__main__":
    unittest.main()
