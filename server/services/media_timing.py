from __future__ import annotations

import math


def canonical_audio_filter_chain(timeline_offset: float = 0.0, duration: float = 0.0) -> list[str]:
    """Rebase audio to zero and apply one authoritative timeline offset."""

    try:
        offset = float(timeline_offset)
    except (TypeError, ValueError):
        offset = 0.0
    if not math.isfinite(offset):
        offset = 0.0
    try:
        media_duration = float(duration)
    except (TypeError, ValueError):
        media_duration = 0.0
    if not math.isfinite(media_duration):
        media_duration = 0.0
    media_duration = max(0.0, media_duration)
    filters = ["asetpts=PTS-STARTPTS"]
    if offset > 0:
        filters.append(f"adelay={round(offset * 1000)}:all=1")
    elif offset < 0:
        filters.extend([f"atrim=start={abs(offset):.6f}", "asetpts=PTS-STARTPTS"])
    filters.append("aresample=async=1:first_pts=0")
    if media_duration > 0:
        filters.extend(["apad", f"atrim=duration={media_duration:.6f}"])
    return filters
