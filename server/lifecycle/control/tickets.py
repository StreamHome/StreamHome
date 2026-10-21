"""Read-only root-issued first-start tickets; never a generic launch grant."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .rpc import ControlError


def read_protected_json(path: Path, *, maximum: int = 32768) -> dict:
    """Root ownership is checked through every parent and the open file."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent = os.open("/", flags | os.O_DIRECTORY)
    try:
        for component in path.parent.parts[1:]:
            descriptor = os.open(component, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = descriptor
            metadata = os.fstat(parent)
            if metadata.st_uid != 0 or metadata.st_mode & 0o7022:
                raise ControlError("control_ticket_untrusted")
        descriptor = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o7133 or before.st_nlink != 1 or before.st_size > maximum:
                raise ControlError("control_ticket_untrusted")
            raw = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_gid, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns, item.st_nlink)
            if len(raw) > maximum or identity(before) != identity(after) or identity(before) != identity(linked):
                raise ControlError("control_ticket_changed")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ControlError("control_ticket_invalid")
                result[key] = value
            return result
        value = json.loads(raw.decode(), object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ControlError("control_ticket_invalid")))
        if not isinstance(value, dict):
            raise ControlError("control_ticket_invalid")
        return value
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ControlError("control_ticket_unavailable") from exc
    finally:
        os.close(parent)


def read_stop_epoch() -> str:
    value = read_protected_json(Path("/etc/streamhome/control/stop-epoch.json"), maximum=4096)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def require_first_start(layout, execution, invocation: str) -> dict:
    from ..management_journal import execution_tuple_sha256, _uuid
    value = read_protected_json(Path("/etc/streamhome/control/first-start.json"))
    fields = {"schema_version", "action", "installation_id", "operation_id", "execution_sha256", "invocation", "boot_id", "stop_epoch", "expires_at_monotonic", "nonce"}
    if (set(value) != fields or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["action"] != "first_start" or value["installation_id"] != layout.descriptor.descriptor.installation_id
            or value["execution_sha256"] != execution_tuple_sha256(execution) or value["invocation"] != invocation
            or not re.fullmatch(r"[0-9a-f]{32}", invocation or "") or not int(invocation, 16)
            or value["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            or value["stop_epoch"] != read_stop_epoch()
            or type(value["expires_at_monotonic"]) not in {int, float}
            or not time.monotonic() < value["expires_at_monotonic"] <= time.monotonic() + 900
            or not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["nonce"])):
        raise ControlError("control_first_start_denied")
    _uuid(value["operation_id"], "first install operation")
    return value
