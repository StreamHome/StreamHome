"""Bounded, redacted journal output from fixed StreamHome unit selectors."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess
import sys
import unicodedata

from ..diagnostics import redact_diagnostic
from .rpc import ControlError


MAX_RECORD = 65536
MAX_MESSAGE = 8192
COMPONENTS = {
    "runtime": ("streamhome.service",),
    "operations": ("streamhome-operation@*.service", "streamhome-prepare@*.service", "streamhome-startup@*.service"),
    "installation": ("streamhome-provision@*.service",),
}
LEVELS = {"error": "3", "warning": "4", "info": "6", "debug": "7"}


def journal_arguments(*, lines: int, follow: bool, component: str, level: str) -> list[str]:
    if (type(lines) is not int or not 1 <= lines <= 1000 or type(follow) is not bool
            or component not in {*COMPONENTS, "all"} or level not in LEVELS):
        raise ControlError("control_log_options_invalid")
    units = tuple(unit for group in COMPONENTS.values() for unit in group) if component == "all" else COMPONENTS[component]
    arguments = ["/usr/bin/journalctl", "--no-pager", "--quiet", "--output=json",
        "--output-fields=MESSAGE,__REALTIME_TIMESTAMP", "--lines=" + str(lines), "--priority=" + LEVELS[level]]
    arguments.extend("--unit=" + unit for unit in units)
    if follow:
        arguments.append("--follow")
    return arguments


def render_record(raw: bytes) -> str:
    """Never fall back to printing unparsed or binary journal data."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return "[StreamHome] Invalid journal record omitted."
    if not isinstance(value, dict) or not isinstance(value.get("MESSAGE"), str):
        return "[StreamHome] Binary or missing journal message omitted."
    message = redact_diagnostic(value["MESSAGE"])
    # Journal messages can contain terminal escapes, bidirectional controls and
    # embedded line breaks. Render one inert line per record after redaction.
    message = "".join(" " if unicodedata.category(character).startswith("C") else character for character in message)
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE] + " [truncated]"
    stamp = value.get("__REALTIME_TIMESTAMP")
    if isinstance(stamp, str) and stamp.isascii() and stamp.isdigit() and len(stamp) <= 17:
        try:
            timestamp = datetime.fromtimestamp(int(stamp) / 1_000_000, tz=timezone.utc).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            pass
        else:
            return timestamp + " " + message
    return message


def read_logs(*, lines: int, follow: bool, component: str = "runtime", level: str = "debug") -> int:
    arguments = journal_arguments(lines=lines, follow=follow, component=component, level=level)
    # OS journal permissions apply to the invoking account; no application or
    # environment code is imported and no privilege transition is requested.
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    try:
        while raw := process.stdout.readline(MAX_RECORD + 1):
            if len(raw) > MAX_RECORD:
                while raw and not raw.endswith(b"\n"):
                    raw = process.stdout.readline(MAX_RECORD + 1)
                print("[StreamHome] Oversized journal record omitted.", flush=True)
                continue
            print(render_record(raw), flush=True)
        code = process.wait(timeout=5)
        if code:
            print("[StreamHome] Journal reading failed. Check journal access permissions.", file=sys.stderr)
        return 0 if code == 0 else 2
    except KeyboardInterrupt:
        return 130
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
