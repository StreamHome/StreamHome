"""Side-effect-free installer/configure drafts using the shared deployment schema.

The caller supplies a reviewed configuration snapshot and read-only diagnostics.
Returning a review is not publication, lifecycle admission or restart authority.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Callable, Iterable, Sequence
import uuid

from .deployment_config import (
    AccessMode, DEFAULT_PROXY_CIDRS, DeploymentConfig, DeploymentConfigError,
    normalize_public_origin, parse_deployment_payload, patch_deployment,
)
from .installation import InstallationEvidenceError, validate_managed_port
from .terminal import Console


class BackToMenu(RuntimeError):
    pass


def _back(value: str) -> str:
    if value.lower() == "back":
        raise BackToMenu()
    return value


def _port(value: str) -> int:
    _back(value)
    try:
        return validate_managed_port(value)
    except InstallationEvidenceError as exc:
        raise DeploymentConfigError(str(exc)) from exc


def _origin(value: str) -> str:
    return normalize_public_origin(_back(value))


def _csv(value: str) -> list[str]:
    _back(value)
    if value.lower() == "none":
        return []
    return [part.strip() for part in value.split(",")]


@dataclass(frozen=True)
class ConfigurationDraft:
    deployment: DeploymentConfig
    start_at_boot: bool


@dataclass(frozen=True)
class ConfigurationReview:
    before: ConfigurationDraft
    after: ConfigurationDraft
    activate_deployment: bool

    @property
    def restart_required(self) -> bool:
        return self.before.deployment != self.after.deployment or self.activate_deployment


Diagnostics = Callable[[DeploymentConfig], Iterable[str]]


def _access(
    console: Console, current: DeploymentConfig, connectors: Sequence[str],
    connector_draft: Callable[[Console],str] | None = None,
) -> DeploymentConfig:
    console.write("Access modes: local (this machine), lan, reverse_proxy, cloudflare (existing tunnel).")
    mode = console.value("Access mode (or back)", current.access_mode.value, lambda value: AccessMode(_back(value)))
    payload = current.payload()
    payload["access_mode"] = mode.value
    payload["cloudflare_connector_id"] = None
    if mode is not AccessMode.REVERSE_PROXY:
        payload["trusted_proxy_cidrs"] = list(DEFAULT_PROXY_CIDRS)
    if mode is AccessMode.LOCAL:
        origin = f"http://localhost:{current.web_port}"
    else:
        default = current.public_origin if mode is current.access_mode else ""
        console.write("Enter the exact public origin, including scheme and web port for direct LAN access.")
        origin = console.value("Public origin (or back)", default, _origin)
    payload["public_origin"] = origin
    if mode is AccessMode.REVERSE_PROXY:
        payload["trusted_proxy_cidrs"] = console.value(
            "Trusted immediate proxy CIDRs, comma-separated (or back)",
            ",".join(current.trusted_proxy_cidrs), _csv,
        )
    if mode is AccessMode.CLOUDFLARE:
        if not connectors and connector_draft is None:
            raise DeploymentConfigError("No protected existing Cloudflare connector is available; configure one first.")
        console.write("Available protected connector references: " + ", ".join(connectors))
        if connector_draft is not None:
            console.write("Choose 'new' to prepare an existing tunnel token file; registration waits for final confirmation.")

        def connector(value: str) -> str:
            if _back(value)=='new' and connector_draft is not None:
                return str(uuid.UUID(connector_draft(console)))
            if value not in connectors:
                raise DeploymentConfigError("Choose an available protected connector reference.")
            return value

        payload["cloudflare_connector_id"] = console.value(
            "Existing connector reference (or back)",
            current.cloudflare_connector_id if current.cloudflare_connector_id in connectors else 'new' if connector_draft is not None and not connectors else "", connector,
        )
        console.write("Route the provider hostname to this existing tunnel; local health and external routing are checked separately.")
    return parse_deployment_payload(payload)


def show_draft(console: Console, draft: ConfigurationDraft, *, title: str) -> None:
    console.write(title)
    config = draft.deployment
    console.write(f"  Access mode: {config.access_mode.value}")
    console.write(f"  Web listener: {config.web_bind}:{config.web_port}")
    console.write(f"  Public origin: {config.public_origin}")
    console.write("  Allowed origins: " + ", ".join(config.allowed_origins))
    console.write("  Trusted immediate proxies: " + ", ".join(config.trusted_proxy_cidrs))
    console.write(f"  HTTPS cookies: {'Yes' if config.secure_cookies else 'No'}")
    console.write(f"  Existing Cloudflare connector: {config.cloudflare_connector_id or 'None'}")
    console.write(f"  Start at boot: {'Yes' if draft.start_at_boot else 'No'}")


def _review(
    console: Console, before: ConfigurationDraft, after: ConfigurationDraft, *,
    running: bool, diagnostics: Diagnostics, active_deployment: DeploymentConfig | None = None,
) -> ConfigurationReview | None:
    if before == after:
        if running and active_deployment is not None and active_deployment != after.deployment:
            for message in diagnostics(after.deployment):
                console.write(message)
            show_draft(console, replace(before, deployment=active_deployment), title="Currently active configuration:")
            show_draft(console, after, title="Saved configuration to activate:")
            if console.confirm("Restart StreamHome to apply this saved deployment?"):
                return ConfigurationReview(before, after, True)
            console.write("The saved deployment remains pending.")
            return None
        console.write("No changes; configuration and services are unchanged.")
        return None
    if before.deployment != after.deployment:
        for message in diagnostics(after.deployment):
            console.write(message)
    show_draft(console, before, title="Current configuration:")
    show_draft(console, after, title="Proposed configuration:")
    deployment_changed = before.deployment != after.deployment
    if deployment_changed:
        console.write("Access changes require controlled activation; saving alone does not change the active endpoint.")
    if not console.confirm("Apply these changes?"):
        console.write("Cancelled; configuration and services are unchanged.")
        return None
    activate = False
    if deployment_changed:
        question = "Restart StreamHome to apply these changes?" if running else "Apply these settings at the next supported start?"
        activate = console.confirm(question)
        if not activate:
            console.write("Save only: the requested deployment will remain pending; the active endpoint is unchanged.")
    return ConfigurationReview(before, after, activate)


def configure_wizard(
    console: Console, current: ConfigurationDraft, *, running: bool,
    diagnostics: Diagnostics, connectors: Sequence[str] = (),
    active_deployment: DeploymentConfig | None = None,
    connector_draft: Callable[[Console],str] | None = None,
) -> ConfigurationReview | None:
    draft = current
    console.write("StreamHome configuration. Enter keeps a displayed value; Back abandons the current edit.")
    if active_deployment is not None and active_deployment != current.deployment:
        show_draft(console, replace(current, deployment=active_deployment), title="Currently active deployment:")
        console.write("The saved deployment below is pending; its endpoint is not active yet.")
    while True:
        show_draft(console, draft, title="Draft:")
        console.write("1 Web port | 2 Access mode / public origin | 3 Additional origins | 4 Trusted proxies")
        console.write("5 Start at boot | 6 Connection diagnostics | 7 Review and apply | 0 Cancel")
        choice = console.choice("Choose", tuple(str(number) for number in range(8)), "7")
        try:
            if choice == "0":
                console.write("Cancelled; configuration and services are unchanged.")
                return None
            if choice == "1":
                port = console.value("Web port (or back)", str(draft.deployment.web_port), _port)
                draft = replace(draft, deployment=patch_deployment(draft.deployment, {"web_port": port}))
            elif choice == "2":
                draft = replace(draft, deployment=_access(console, draft.deployment, connectors,connector_draft))
            elif choice == "3":
                origins = console.value(
                    "Additional exact origins; 'none' clears (or back)",
                    ",".join(draft.deployment.extra_allowed_origins) or "none", _csv,
                )
                draft = replace(draft, deployment=patch_deployment(draft.deployment, {"extra_allowed_origins": origins}))
            elif choice == "4":
                cidrs = console.value("Trusted immediate proxy CIDRs (or back)", ",".join(draft.deployment.trusted_proxy_cidrs), _csv)
                draft = replace(draft, deployment=patch_deployment(draft.deployment, {"trusted_proxy_cidrs": cidrs}))
            elif choice == "5":
                console.write(f"Currently selected: {'Yes' if draft.start_at_boot else 'No'}")
                draft = replace(draft, start_at_boot=console.confirm("Start StreamHome at boot?"))
            elif choice == "6":
                for message in diagnostics(draft.deployment):
                    console.write(message)
            elif choice == "7":
                return _review(console, current, draft, running=running, diagnostics=diagnostics, active_deployment=active_deployment)
        except BackToMenu:
            console.write("Edit abandoned.")
        except DeploymentConfigError as exc:
            console.write(str(exc))


def installation_wizard(
    console: Console, *, diagnostics: Diagnostics, connectors: Sequence[str] = (),
    connector_draft: Callable[[Console],str] | None = None,
) -> ConfigurationDraft | None:
    from .deployment_config import default_deployment

    console.write("StreamHome installs application files in /opt/streamhome, configuration in /etc/streamhome and state in /var/lib/streamhome.")
    config = default_deployment()
    try:
        while True:
            try:
                port = console.value("Web port (or back)", str(config.web_port), _port)
                config = patch_deployment(config, {"web_port": port})
                config = _access(console, config, connectors,connector_draft)
                for message in diagnostics(config):
                    console.write(message)
                break
            except DeploymentConfigError as exc:
                console.write(str(exc))
        draft = ConfigurationDraft(config, console.confirm("Start StreamHome at boot?"))
        show_draft(console, draft, title="Installation settings:")
        return draft if console.confirm("Install StreamHome with these settings?") else None
    except BackToMenu:
        console.write("Cancelled; nothing was installed.")
        return None


@dataclass(frozen=True)
class NoninteractiveConfigurationRequest:
    expected_desired_revision: str
    review: ConfigurationReview


def parse_installation_request(payload: Any) -> ConfigurationDraft:
    """Non-TTY input must include every setting and explicit installation consent."""
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "deployment", "start_at_boot", "install"}:
        raise DeploymentConfigError("Noninteractive installation requires explicit deployment, boot intent and install consent.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise DeploymentConfigError("The installation request format is unsupported.")
    if type(payload["start_at_boot"]) is not bool or payload["install"] is not True:
        raise DeploymentConfigError("Noninteractive installation requires explicit boolean boot intent and install=true.")
    return ConfigurationDraft(parse_deployment_payload(payload["deployment"]), payload["start_at_boot"])


def parse_configuration_request(payload: Any, current: ConfigurationDraft, *, active_deployment: DeploymentConfig | None = None) -> NoninteractiveConfigurationRequest:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "expected_desired_revision", "deployment_patch", "start_at_boot", "apply", "activate_deployment",
    }:
        raise DeploymentConfigError("The noninteractive configuration request has missing or unknown fields.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise DeploymentConfigError("The configuration request format is unsupported.")
    identity = payload["expected_desired_revision"]
    try:
        revision = uuid.UUID(identity) if isinstance(identity, str) else None
    except ValueError as exc:
        raise DeploymentConfigError("An exact expected deployment revision is required.") from exc
    if revision is None or not revision.int or str(revision) != identity:
        raise DeploymentConfigError("An exact expected deployment revision is required.")
    if payload["apply"] is not True or type(payload["activate_deployment"]) is not bool or type(payload["start_at_boot"]) is not bool:
        raise DeploymentConfigError("Explicit boolean apply, activation and boot intent are required.")
    after = ConfigurationDraft(patch_deployment(current.deployment, payload["deployment_patch"]), payload["start_at_boot"])
    if payload["activate_deployment"] and after.deployment == current.deployment and active_deployment in {None, after.deployment}:
        raise DeploymentConfigError("An unchanged deployment cannot request a restart through this edit.")
    return NoninteractiveConfigurationRequest(identity, ConfigurationReview(current, after, payload["activate_deployment"]))


def parse_request_json(raw: bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DeploymentConfigError("The request contains duplicate fields.")
            result[key] = value
        return result

    def reject(value):
        raise DeploymentConfigError("The request contains a nonstandard number.")

    if not isinstance(raw, bytes) or not raw or len(raw) > 32 * 1024:
        raise DeploymentConfigError("The request has an invalid size.")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DeploymentConfigError("The request is invalid JSON.") from exc
    if not isinstance(result, dict):
        raise DeploymentConfigError("The request must be an object.")
    return result
