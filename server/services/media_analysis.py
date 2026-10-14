"""Technical measurements of the selected video; bitrate is bits per second."""
from __future__ import annotations

import math
import json
from fractions import Fraction


def positive_number(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed > 0 else None
    except (ValueError, TypeError):
        return None


def resolution_label(width, height) -> str:
    """Classify the raster's long edge, retaining exact cropped/portrait dimensions."""
    if not positive_number(width) or not positive_number(height):
        return "Source"
    long_edge = max(int(width), int(height))
    for edge, label in ((3840, "2160p"), (2560, "1440p"), (1920, "1080p"),
                        (1280, "720p"), (854, "480p")):
        if long_edge >= edge:
            return label
    return f"{min(int(width), int(height))}p"


def stream_duration(stream: dict, container: dict) -> tuple[float | None, str]:
    """Use inspected packet endpoints; optional tags are estimates, never evidence."""
    measured = positive_number(stream.get("_packet_elapsed"))
    if measured is not None and stream.get("_packet_endpoint_measured") is True:
        return measured, "selected_stream_packets"
    formats = set(str(container.get("format_name") or "").lower().split(","))
    # A declared non-Matroska duration can help describe an uninspected source,
    # but completed-media acceptance separately requires endpoint measurement.
    if not formats.intersection({"matroska", "webm"}):
        declared = positive_number(stream.get("duration"))
        if declared is not None:
            return declared, "selected_stream"
        try:
            declared = positive_number(float(stream.get("duration_ts")) * float(Fraction(str(stream.get("time_base")))))
            if declared is not None:
                return declared, "selected_stream_ticks"
        except (ValueError, TypeError, ZeroDivisionError):
            pass
    fallback = positive_number(container.get("duration"))
    if fallback is not None:
        try:
            fallback = positive_number(fallback - float(stream.get("start_time") or 0))
        except (ValueError, TypeError):
            fallback = None
    return (fallback, "container_fallback") if fallback is not None else (None, "unknown")


def stored_analysis(value: dict | str | None) -> dict:
    """Read existing analysis without assigning an origin to absent information."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {}
    return value if isinstance(value, dict) else {}


def current_duration_analysis(value: dict | str | None) -> bool:
    analysis = stored_analysis(value)
    return (
        analysis.get("measurement_version") == 2
        and type(analysis.get("direct_timeline_compatible")) is bool
        and analysis.get("duration_provenance") == "selected_stream_packets"
        and bool(analysis.get("video_fingerprint"))
    )


def direct_timeline_compatible(video: dict, streams: list[dict], container: dict, analysis: dict) -> bool:
    """Prove raw browser media agrees with the selected video playback timeline."""
    if sum(stream.get("codec_type") == "video" for stream in streams) != 1:
        return False
    if not video.get("_packet_start_measured") or analysis.get("duration_provenance") not in {
        "selected_stream_packets"
    }:
        return False
    duration = positive_number(analysis.get("probed_duration"))
    container_duration = positive_number(container.get("duration"))
    if duration is None or container_duration is None:
        return False
    def tick(stream):
        try:
            return positive_number(float(Fraction(str(stream.get("time_base")))))
        except (ValueError, TypeError, ZeroDivisionError):
            return None
    video_tick = tick(video)
    if video_tick is None:
        return False
    try:
        start = float(video.get("start_time"))
        if not math.isfinite(start) or abs(start) > 1e-6:
            return False
    except (ValueError, TypeError):
        return False
    # Allow one declared tick, capped at a millisecond, or ffprobe's six-decimal
    # precision. A coarse timebase must not authorize a real extra second/tail.
    tolerance = max(min(video_tick, 0.001), 1e-6)
    if abs(container_duration - duration) > tolerance + 1e-9:
        return False
    for stream in streams:
        if stream.get("codec_type") != "audio":
            continue
        elapsed, origin = stream_duration(stream, container)
        audio_tick = tick(stream)
        if elapsed is None or audio_tick is None or origin in {"container_fallback", "unknown"}:
            return False
        try:
            endpoint = float(stream.get("start_time")) + elapsed
            if not math.isfinite(endpoint) or abs(endpoint - duration) > max(tolerance, min(audio_tick, 0.001)) + 1e-9:
                return False
        except (ValueError, TypeError):
            return False
    return True


def refreshed_analysis(measurements: dict | None, previous: dict | str | None) -> dict:
    """Refresh technical values while retaining only an actual historical claim."""
    previous = stored_analysis(previous)
    analysis = dict(measurements) if isinstance(measurements, dict) else {}
    analysis.pop("sender_quality", None)
    claim = previous.get("sender_quality")
    if isinstance(claim, str) and claim.strip():
        analysis["sender_quality"] = claim
    return analysis


def measured_analysis(video: dict, container: dict) -> dict:
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    duration, duration_provenance = stream_duration(video, container)
    measured = duration_provenance == "selected_stream_packets"
    return {
        "quality": resolution_label(width, height),
        "quality_provenance": "final_probe",
        "width": width, "height": height,
        "video_stream_index": int(video.get("index") or 0),
        "video_bitrate": positive_number(video.get("bit_rate")),
        "container_bitrate": positive_number(container.get("bit_rate")),
        "bitrate_unit": "bits_per_second",
        "probed_duration": duration if measured else None,
        "duration_provenance": duration_provenance if measured else "unknown",
        "estimated_duration": duration if not measured else None,
        "estimated_duration_provenance": duration_provenance if not measured else None,
        "measurement_version": 2,
    }


def duration_label(seconds) -> str:
    measured = positive_number(seconds)
    if measured is None:
        return "Unknown"
    total = max(1, round(measured))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return " ".join(part for part in (f"{hours}h" if hours else "",
                                     f"{minutes}m" if minutes else "",
                                     f"{seconds}s" if seconds else "") if part)
