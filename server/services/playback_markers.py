"""Defensive interpretation of persisted markers for playback completion."""

from __future__ import annotations

import math
import re


CREDITS_EXIT_LEAD_SECONDS = 10.0
_DECIMAL_TIMESTAMP = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$", re.IGNORECASE)


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not _DECIMAL_TIMESTAMP.fullmatch(value):
            return None
    elif not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def is_credits_exit_position(markers: object, position: object, duration: object) -> bool:
    """Use separate credits intervals and a known measured media duration.

    Callers own movie/run/event authorization. Persisted legacy numeric strings
    are supported without accepting boolean, null or nondecimal coercions.
    """
    clock, limit = _timestamp(position), _timestamp(duration)
    if clock is None or limit is None or limit <= 0 or not isinstance(markers, dict):
        return False
    clock = min(clock, limit)
    for kind, ranges in markers.items():
        if not isinstance(kind, str) or kind.casefold() != "credits" or not isinstance(ranges, list):
            continue
        for marker in ranges:
            if not isinstance(marker, dict):
                continue
            start, end = _timestamp(marker.get("start")), _timestamp(marker.get("end"))
            if start is None or end is None or end <= start or start >= limit:
                continue
            if max(0.0, start - CREDITS_EXIT_LEAD_SECONDS) <= clock < min(end, limit):
                return True
    return False
