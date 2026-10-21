"""Bounded public main selection before first-install OS/account effects."""
from __future__ import annotations

import json
import os
import re
import urllib.request

from .rpc import ControlError


URL = "https://api.github.com/repos/StreamHome/StreamHome/git/ref/heads/main"


def select_public_source() -> dict:
    if os.geteuid() == 0:
        raise ControlError("control_source_probe_must_be_unprivileged")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(URL, headers={"User-Agent": "StreamHome-control/1", "Accept": "application/vnd.github+json"})
    with opener.open(request, timeout=20) as response:
        if response.geturl() != URL:
            raise ControlError("control_source_revision_invalid")
        raw = response.read(32769)
    if len(raw) > 32768:
        raise ControlError("control_source_revision_invalid")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("ref") != "refs/heads/main" or not isinstance(value.get("object"), dict)
            or value["object"].get("type") != "commit" or not isinstance(value["object"].get("sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["object"]["sha"])):
        raise ControlError("control_source_revision_invalid")
    return {"application_commit": value["object"]["sha"]}
