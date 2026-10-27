"""Untrusted source metadata guides admission; it never proves media acceptance."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

MIB = 1024 ** 2
FALLBACK_BYTES_PER_SECOND = 2 * MIB


def positive_number(value, maximum: float = 2 ** 63 - 1) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 < number <= maximum else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


class SourceStorageHints:
    """Keep only numeric maxima from manifests already read by the source relay."""
    def __init__(self):
        self.bit_rate = 0
        self.duration_seconds = 0.0
        self.source_bytes = 0

    def observe_manifest(self, text: str) -> None:
        for line in text.splitlines():
            if line.startswith('#EXT-X-STREAM-INF:'):
                # Peak BANDWIDTH includes the variant's renditions. Average-only
                # declarations are useful estimates when a peak is absent.
                rates = re.findall(r'(?:^|,)(?:AVERAGE-)?BANDWIDTH=(\d{1,12})(?=,|$)', line.split(':', 1)[1])
                self.bit_rate = max(self.bit_rate, *(int(rate) for rate in rates), 0)
        if '#EXT-X-ENDLIST' not in text.splitlines():
            return
        durations = [positive_number(line.split(':', 1)[1].split(',', 1)[0], 86400)
                     for line in text.splitlines() if line.startswith('#EXTINF:')]
        if durations and all(durations):
            self.duration_seconds = max(self.duration_seconds, sum(durations))

    def snapshot(self) -> dict:
        return {'bit_rate': self.bit_rate, 'duration_seconds': self.duration_seconds,
                'source_bytes': self.source_bytes}


@dataclass(frozen=True)
class IngestionEstimate:
    workspace_bytes: int
    preview_bytes: int


def estimate_ingestion(duration_seconds: float, probe: dict, *, separate_audio: bool = False,
                       subtitle_count: int = 0, fallback_bytes_per_second: int = FALLBACK_BYTES_PER_SECOND) -> IngestionEstimate:
    """Estimate one muxed output and one independent preview, without publication.

    Publication admits its measured copy separately. Embedded audio is retained
    in the muxed file; the language probe does not extract another media copy.
    Native preview encoding uses CRF, so neither declared rates nor this estimate
    are ceilings. The workspace supervisor must admit growth before headroom ends.
    """
    video = probe.get('video_storage_hints') or {}
    audio = probe.get('audio_storage_hints') or {}
    duration = max(positive_number(duration_seconds), positive_number(probe.get('video_estimated_duration')),
                   positive_number(video.get('duration_seconds')), positive_number(audio.get('duration_seconds')), 1)

    def source_size(hints, fallback_rate):
        rate = positive_number(hints.get('bit_rate')) / 8
        size = positive_number(hints.get('source_bytes'))
        return max(size, duration * rate) if rate else (size or duration * fallback_rate)

    media = source_size(video, fallback_bytes_per_second)
    if separate_audio:
        media += source_size(audio, 320_000 / 8)
    tracks = max(1, int(positive_number(probe.get('video_audio_tracks'), 1024))) + int(separate_audio)
    # AAC output is 192 kbit/s per mapped track; container/rate uncertainty gets
    # 25% headroom. Subtitle conversion can retain both input and output sidecars.
    workspace = math.ceil((media + duration * tracks * 192_000 / 8) * 1.25)
    workspace += 64 * MIB + max(0, subtitle_count) * 30 * MIB
    preview = math.ceil(duration * (4_000_000 + 160_000) / 8 * 1.25) + 64 * MIB
    return IngestionEstimate(max(256 * MIB, workspace), max(128 * MIB, preview))
