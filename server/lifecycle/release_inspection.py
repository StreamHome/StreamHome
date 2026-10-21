"""Dependency-independent, read-only managed release/policy inspection."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import stat

from .configuration_store import _decode
from .execution_state import read_active_execution
from .install_discovery import CandidateHint, DiscoveryError, inspect_candidate
from .layout import layout_for_root
from .release_probe import ProbeResult, ProbeStatus, ReleasePolicy, probe_release


def private_optional_bytes(path: Path, *, maximum: int = 128 * 1024) -> bytes | None:
    if not path.is_absolute() or path.parent.resolve(strict=False) != path.parent:
        raise DiscoveryError("Release policy input has an aliased parent directory.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_mode & 0o7177
                or before.st_nlink != 1 or before.st_size > maximum):
            raise DiscoveryError("Release policy input is not trusted private evidence.")
        raw = handle.read(maximum + 1)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_gid, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns, item.st_nlink)
        if len(raw) > maximum or identity(before) != identity(os.fstat(handle.fileno())) or identity(before) != identity(path.lstat()):
            raise DiscoveryError("Release policy input changed during inspection.")
        return raw


def literal_update_settings(raw: bytes | None) -> dict[str, str]:
    """Never evaluate dotenv interpolation, shell substitutions or unknown keys."""
    if raw is None:
        return {}
    result = {}
    for line in raw.decode("utf-8").splitlines():
        match = re.match(r"^\s*(?:export\s+)?(UPDATE_BRANCH|UPDATE_REQUIRE_SIGNED_COMMITS|UPDATE_TRUSTED_SIGNERS)\s*=\s*(.*?)\s*$", line)
        if match is None:
            continue
        name, text = match.groups()
        if name in result or any(marker in text for marker in ("$", "`", "\\")):
            raise DiscoveryError("The release policy requires a literal, unambiguous value.")
        values = shlex.split(text, comments=True, posix=True)
        if len(values) > 1:
            raise DiscoveryError("The release policy contains an ambiguous value.")
        result[name] = values[0] if values else ""
    return result


def read_policy(root: Path) -> ReleasePolicy:
    layout = layout_for_root(root)
    values = literal_update_settings(private_optional_bytes(layout.configuration / ".env"))
    overriding = layout.configuration / "runtime.env" if layout.managed else root / ".env"
    values.update(literal_update_settings(private_optional_bytes(overriding)))
    require = values.get("UPDATE_REQUIRE_SIGNED_COMMITS", "false").strip().lower()
    if require not in {"true", "1", "yes", "false", "0", "no", ""}:
        raise DiscoveryError("The signed-release policy is ambiguous.")
    failed_target = None
    raw = private_optional_bytes(layout.durable_runtime / "update-state.json", maximum=65536)
    if raw is not None:
        value = _decode(raw)
        if not isinstance(value, dict) or "failed_target" not in value:
            raise DiscoveryError("The failed-release suppression policy is unavailable.")
        failed_target = value["failed_target"]
        if failed_target in (None, ""):
            failed_target = None
        elif not isinstance(failed_target, str) or not re.fullmatch(r"[0-9a-f]{40}", failed_target):
            raise DiscoveryError("The failed-release suppression policy is invalid.")
    policy = ReleasePolicy(values.get("UPDATE_BRANCH", "main").strip() or "main", require in {"true", "1", "yes"},
        tuple(part.strip().upper() for part in values.get("UPDATE_TRUSTED_SIGNERS", "").split(",") if part.strip()), failed_target)
    policy.validate()
    return policy


def inspect_release(root: Path) -> ProbeResult:
    record = read_active_execution(root)
    layout = layout_for_root(root)
    def snapshot():
        candidate = inspect_candidate(CandidateHint(root, layout.descriptor.descriptor.service_uid, "descriptor"), mode="managed")
        if candidate is None or candidate.commit != read_active_execution(root).execution.application_commit:
            raise DiscoveryError("Installed source no longer matches its accepted runtime.")
        return candidate, read_policy(root)
    try:
        installation, policy = snapshot()
        # A signed-policy installation never falls back to unsigned eligibility
        # when its independent public trust snapshot is unavailable.
        return probe_release(installation, policy, read_current=snapshot)
    except (OSError, ValueError):
        return ProbeResult(ProbeStatus.UNAVAILABLE, record.execution.application_commit, None,
            "The official release check is unavailable; no update or reinstall was performed.")
