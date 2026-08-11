import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.hevc_compressor import HEVCCompressorWorker
from services.playback_prep import PlaybackPrepService


def _probe(codec: str, *, duration: float = 120.0, audio_codec: str = "aac") -> dict:
    return {
        "codec": codec,
        "probed_duration": duration,
        "stream_manifest": [
            {
                "index": 0,
                "type": "video",
                "codec": codec,
                "language": "und",
                "title": "",
                "default": True,
            },
            {
                "index": 1,
                "type": "audio",
                "codec": audio_codec,
                "language": "en",
                "title": "English",
                "default": True,
            },
            {
                "index": 2,
                "type": "audio",
                "codec": "ac3",
                "language": "tr",
                "title": "Turkish",
                "default": False,
            },
            {
                "index": 3,
                "type": "subtitle",
                "codec": "subrip",
                "language": "en",
                "title": "English",
                "default": False,
            },
        ],
    }


def test_hevc_validation_requires_complete_stream_preservation() -> None:
    original = _probe("h264")
    valid = _probe("hevc")
    assert HEVCCompressorWorker._valid_transcode(original, valid, 10_000, 7_000)

    missing_audio = _probe("hevc")
    missing_audio["stream_manifest"] = missing_audio["stream_manifest"][:-2] + missing_audio["stream_manifest"][-1:]
    assert not HEVCCompressorWorker._valid_transcode(original, missing_audio, 10_000, 7_000)

    changed_language = _probe("hevc")
    changed_language["stream_manifest"][2]["language"] = "und"
    assert not HEVCCompressorWorker._valid_transcode(original, changed_language, 10_000, 7_000)

    truncated = _probe("hevc", duration=90.0)
    assert not HEVCCompressorWorker._valid_transcode(original, truncated, 10_000, 7_000)


def test_embedded_audio_uses_absolute_ffmpeg_stream_identity() -> None:
    service = PlaybackPrepService()
    media = type(
        "Media",
        (),
        {
            "audio_metadata": [
                {
                    "index": 0,
                    "streamIndex": 3,
                    "codec": "aac",
                    "language": "en",
                    "default": True,
                }
            ]
        },
    )()
    rendition = service.audio_renditions(media)[0]
    assert rendition.stream_index == 3
    assert rendition.absolute_stream_index


def test_ingestion_and_hevc_commands_preserve_stream_contracts() -> None:
    root = Path(__file__).parents[1]
    ffmpeg_source = (root / "services" / "ffmpeg.py").read_text(encoding="utf-8")
    hevc_source = (root / "services" / "hevc_compressor.py").read_text(encoding="utf-8")
    audio_source = (root / "services" / "audio_extractor.py").read_text(encoding="utf-8")
    assert '"-shortest"' not in ffmpeg_source
    assert '"-map", "0:a?"' in ffmpeg_source
    assert '"-map", "0:s?"' in ffmpeg_source
    assert '"-c:s", "mov_text"' in ffmpeg_source
    assert '"-map",\n                    "0"' in hevc_source
    assert '"-c",\n                    "copy"' in hevc_source
    assert "os.link(file_path, rollback_file)" in hevc_source
    assert "without generating lossy sidecars" in audio_source
    assert '"libmp3lame"' not in audio_source


if __name__ == "__main__":
    test_hevc_validation_requires_complete_stream_preservation()
    test_embedded_audio_uses_absolute_ffmpeg_stream_identity()
    test_ingestion_and_hevc_commands_preserve_stream_contracts()
    print("Media preservation regression checks passed.")
