"""Unprivileged draft review from the already authenticated package contents."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from ..configure_wizard import installation_wizard, parse_installation_request, parse_request_json
from ..deployment_config import DeploymentConfigError
from ..port_check import ListenerCheckError, check_web_port
from ..terminal import UserCancelled, open_console
from .rpc import ControlError
from .source_selection import select_public_source


def main(arguments: list[str]) -> int:
    if os.geteuid() == 0 or len(arguments) > 1:
        raise ControlError("control_review_must_be_unprivileged")
    def diagnostics(config):
        try:
            check_web_port(config.web_bind, config.web_port)
        except ListenerCheckError as exc:
            raise DeploymentConfigError(str(exc)) from exc
        return [f"Listener preflight passed: {config.web_bind}:{config.web_port}. External routing is not yet verified."]
    if arguments:
        descriptor = os.open(Path(arguments[0]), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ControlError("control_request_invalid")
            draft = parse_installation_request(parse_request_json(handle.read(32769)))
        diagnostics(draft.deployment)
    else:
        try:
            with open_console() as console:
                draft = installation_wizard(console, diagnostics=diagnostics)
        except UserCancelled:
            draft = None
    if draft is None:
        print(json.dumps({"cancelled": True}))
        return 0
    source = select_public_source()
    print(json.dumps({"cancelled": False, "application_commit": source["application_commit"], "request": {
        "schema_version": 1, "deployment": draft.deployment.payload(), "start_at_boot": draft.start_at_boot, "install": True,
    }}, sort_keys=True, allow_nan=False))
    return 0
