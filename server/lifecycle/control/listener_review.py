"""Bounded no-reuse listener preflight; closing sockets grant no ownership."""
from __future__ import annotations

import time

from ..deployment_config import DeploymentConfig, DeploymentConfigError
from .privileged import run_worker
from .rpc import ControlError
from .tickets import read_stop_epoch


class PortPreflightError(DeploymentConfigError):
    def __init__(self, *, closing: bool):
        self.code = 'control_web_port_closing' if closing else 'control_web_port_conflict'
        super().__init__('Closing TCP connections still reserve this web port. Wait and retry, or choose another port.' if closing
            else 'The requested web port cannot be bound. Choose another port or inspect its owner; no process was stopped.')


def check_requested_listener(config: DeploymentConfig, *, web: dict | None, invocation: str | None) -> list[str]:
    epoch = read_stop_epoch()
    deadline = time.monotonic() + 65
    announced = False
    while True:
        if read_stop_epoch() != epoch:
            raise ControlError('control_operator_stopped')
        try:
            result = run_worker(['check-listener'], payload={'deployment': config.payload(), 'web': web, 'invocation': invocation})
        except ControlError as exc:
            raise PortPreflightError(closing=False) from exc
        if (result.get('port') != config.web_port or result.get('bind') != config.web_bind
                or type(result.get('available')) is not bool):
            raise DeploymentConfigError('The listener preflight result changed. Review the configuration again.')
        if read_stop_epoch() != epoch:
            raise ControlError('control_operator_stopped')
        if result['available']:
            return [f'Listener preflight passed: {config.web_bind}:{config.web_port}. External routing is not yet verified.']
        closing = result.get('reason') == 'closing_connections'
        if not closing or time.monotonic() >= deadline:
            raise PortPreflightError(closing=closing)
        if not announced:
            print('[StreamHome] Waiting up to 65 seconds for closing TCP connections to release the requested port ...', flush=True)
            announced = True
        # Every retry uses the same no-reuse kernel bind/listen probe. A live or
        # bound foreign socket remains a conflict, including after TIME_WAIT.
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
