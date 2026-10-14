"""Read-only display of the process-local clock used by maintenance windows."""
from __future__ import annotations

from datetime import datetime


def server_clock_snapshot(current: datetime | None = None) -> dict[str, str]:
    """Report local wall time without changing timezone or scheduling policy."""
    local = current if current is not None else datetime.now().astimezone()
    if local.utcoffset() is None:
        raise ValueError("A timezone-aware clock value is required")
    offset = local.strftime("%z")
    offset_label = f"UTC{offset[:3]}:{offset[3:5]}"
    if len(offset) > 5:
        offset_label += f":{offset[5:]}"
    return {
        "time_zone": local.tzname() or "Server local time",
        "utc_offset": offset_label,
        "local_time": local.strftime("%Y-%m-%d %H:%M:%S"),
    }
