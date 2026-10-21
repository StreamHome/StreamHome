"""Read-only legacy discovery from publisher-authenticated, unprivileged code."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..install_discovery import CandidateHint, DiscoveryError, DiscoveryStatus, discover_installations, inspect_candidate
from ..release_inspection import read_policy
from ..release_probe import probe_release
from .rpc import ControlError


def inspect_legacy(paths: list[str]) -> dict:
    if os.geteuid() == 0 or not 1 <= len(paths) <= 4:
        raise ControlError("control_discovery_must_be_unprivileged")
    hints = tuple(CandidateHint(Path(path), os.geteuid(), "explicit") for path in paths)
    discovered = discover_installations(hints)
    if discovered.status is not DiscoveryStatus.EXISTING:
        return {"status": discovered.status.value, "reason": discovered.reason}
    installation = discovered.installation

    def snapshot():
        candidate = inspect_candidate(CandidateHint(installation.path, installation.owner_uid, "explicit"))
        if candidate is None:
            raise DiscoveryError("The existing installation became unavailable.")
        return candidate, read_policy(installation.path)

    try:
        candidate, policy = snapshot()
        result = probe_release(candidate, policy, read_current=snapshot)
        return {"status": result.status.value, "reason": result.reason}
    except (OSError, ValueError):
        return {"status": "check_unavailable", "reason": "The installed release policy could not be verified; no update or reinstall was performed."}


def main(paths: list[str]) -> int:
    print(json.dumps(inspect_legacy(paths), sort_keys=True))
    return 0
