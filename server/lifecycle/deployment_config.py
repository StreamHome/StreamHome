"""Shared managed deployment settings, without configuration/database imports.

Listener binding, exact public origins and proxy/cookie policy are one startup
snapshot. Secret values, provider policy and boot intent are separate domains.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import ipaddress
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid

from .installation import InstallationEvidenceError, validate_managed_port


DEPLOYMENT_SCHEMA_VERSION = 1
MAX_DEPLOYMENT_BYTES = 16 * 1024
DEFAULT_PROXY_CIDRS = ("127.0.0.1/32", "::1/128")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_FIELDS = frozenset({
    "access_mode", "web_port", "public_origin", "trusted_proxy_cidrs",
    "extra_allowed_origins", "cloudflare_connector_id",
})


class DeploymentConfigError(ValueError):
    code = "invalid_deployment"


class AccessMode(str, Enum):
    LOCAL = "local"
    LAN = "lan"
    REVERSE_PROXY = "reverse_proxy"
    CLOUDFLARE = "cloudflare"


def normalize_public_origin(value: Any) -> str:
    if (
        not isinstance(value, str) or not 1 <= len(value) <= 2048
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
        or any(char in value for char in ("\\", "@", ",", "?", "#", "%"))
    ):
        raise DeploymentConfigError("Enter an HTTP or HTTPS origin without credentials, path or query.")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise DeploymentConfigError("The public origin has an invalid host or port.") from exc
    if (
        parsed.scheme not in {"http", "https"} or not host
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise DeploymentConfigError("Enter an HTTP or HTTPS origin without credentials, path or query.")
    host = host.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in host.split(".")):
            raise DeploymentConfigError("The public origin hostname is invalid; use ASCII or punycode.")
        # Browsers interpret several non-canonical numeric hosts as IPv4. Keep
        # the origin used by security checks identical to the browser's origin.
        last_label = host.rsplit(".", 1)[-1]
        if last_label.isdigit() or re.fullmatch(r"0x[0-9a-f]*", last_label):
            raise DeploymentConfigError("Use a canonical IP address or a DNS hostname.")
    else:
        if address.is_unspecified or address.is_multicast:
            raise DeploymentConfigError("The public origin must identify a reachable host.")
        host = address.compressed
    formatted = f"[{host}]" if ":" in host else host
    default = 80 if parsed.scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default else ""
    return f"{parsed.scheme}://{formatted}{suffix}"


def _loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origins(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise DeploymentConfigError("At most eight additional exact origins are supported.")
    normalized = tuple(normalize_public_origin(origin) for origin in value)
    if len(set(normalized)) != len(normalized):
        raise DeploymentConfigError("Additional allowed origins contain duplicates.")
    return tuple(sorted(normalized))


def _proxy_cidrs(value: Any, mode: AccessMode) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise DeploymentConfigError("Specify between one and sixteen trusted immediate proxy addresses or CIDRs.")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw or len(raw) > 64:
            raise DeploymentConfigError("A trusted proxy address or CIDR is invalid.")
        try:
            network = ipaddress.ip_network(raw, strict=True)
        except ValueError as exc:
            raise DeploymentConfigError("A trusted proxy address or CIDR is invalid.") from exc
        if network.prefixlen == 0 or network.is_multicast or network.network_address.is_unspecified:
            raise DeploymentConfigError("Universal, multicast and unspecified proxy trust is forbidden.")
        if mode is not AccessMode.REVERSE_PROXY and not network.is_loopback:
            raise DeploymentConfigError("This access mode trusts only local loopback proxies.")
        result.append(str(network))
    if len(set(result)) != len(result):
        raise DeploymentConfigError("Trusted proxy addresses contain duplicates.")
    return tuple(sorted(result))


@dataclass(frozen=True)
class DeploymentConfig:
    access_mode: AccessMode
    web_port: int
    public_origin: str
    trusted_proxy_cidrs: tuple[str, ...]
    extra_allowed_origins: tuple[str, ...]
    cloudflare_connector_id: str | None

    @property
    def web_bind(self) -> str:
        return "0.0.0.0" if self.access_mode is AccessMode.LAN else "127.0.0.1"

    @property
    def secure_cookies(self) -> bool:
        return self.public_origin.startswith("https://")

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return tuple(sorted({self.public_origin, *self.extra_allowed_origins}))

    def payload(self) -> dict[str, Any]:
        return {
            "access_mode": self.access_mode.value,
            "web_port": self.web_port,
            "public_origin": self.public_origin,
            "trusted_proxy_cidrs": list(self.trusted_proxy_cidrs),
            "extra_allowed_origins": list(self.extra_allowed_origins),
            "cloudflare_connector_id": self.cloudflare_connector_id,
        }

    def startup_environment(self) -> dict[str, str]:
        # Deployment-only compatibility input. It is generated from a bound
        # revision, never sourced as shell code or treated as independent truth.
        parse_deployment_payload(self.payload())
        return {
            "WEB_PORT": str(self.web_port), "WEB_BIND": self.web_bind,
            "PUBLIC_URL": self.public_origin,
            "ALLOWED_ORIGINS": ",".join(self.allowed_origins),
            "TRUSTED_PROXY_CIDRS": ",".join(self.trusted_proxy_cidrs),
        }


def parse_deployment_payload(payload: Any) -> DeploymentConfig:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise DeploymentConfigError("Deployment settings contain missing or unknown fields.")
    try:
        mode = AccessMode(payload["access_mode"])
    except (ValueError, TypeError) as exc:
        raise DeploymentConfigError("The access mode is unsupported.") from exc
    try:
        port = validate_managed_port(payload["web_port"])
    except InstallationEvidenceError as exc:
        raise DeploymentConfigError("The web port must be 1024–65535, excluding 8000.") from exc
    origin = normalize_public_origin(payload["public_origin"])
    public = urlsplit(origin)
    if mode in {AccessMode.LOCAL, AccessMode.LAN}:
        if ":" in (public.hostname or ""):
            raise DeploymentConfigError("Direct access currently requires an IPv4 address or hostname.")
        if public.scheme != "http" or (public.port or 80) != port:
            raise DeploymentConfigError("Direct access must use HTTP and the selected web port.")
        if mode is AccessMode.LOCAL and public.hostname not in {"localhost", "127.0.0.1"}:
            raise DeploymentConfigError("Local access requires localhost or 127.0.0.1 to match the web listener.")
        if mode is AccessMode.LAN and _loopback_host(public.hostname or ""):
            raise DeploymentConfigError("LAN access requires a LAN address or hostname.")
    elif public.scheme != "https":
        raise DeploymentConfigError("Reverse proxy and Cloudflare access require an HTTPS public origin.")
    connector = payload["cloudflare_connector_id"]
    if mode is AccessMode.CLOUDFLARE:
        try:
            identity = uuid.UUID(connector) if isinstance(connector, str) else None
        except ValueError as exc:
            raise DeploymentConfigError("Select an existing protected Cloudflare connector.") from exc
        if identity is None or identity.int == 0 or str(identity) != connector:
            raise DeploymentConfigError("Select an existing protected Cloudflare connector.")
        if _loopback_host(public.hostname or ""):
            raise DeploymentConfigError("Cloudflare access requires the routed public hostname.")
        try:
            ipaddress.ip_address(public.hostname or "")
        except ValueError:
            pass
        else:
            raise DeploymentConfigError("Cloudflare access requires a routed DNS hostname, not an IP address.")
    elif connector is not None:
        raise DeploymentConfigError("A Cloudflare connector is valid only in Cloudflare mode.")
    extras = _origins(payload["extra_allowed_origins"])
    if origin in extras:
        raise DeploymentConfigError("The public origin is included automatically; do not duplicate it.")
    return DeploymentConfig(mode, port, origin, _proxy_cidrs(payload["trusted_proxy_cidrs"], mode), extras, connector)


def default_deployment() -> DeploymentConfig:
    return parse_deployment_payload({
        "access_mode": "local", "web_port": 3000, "public_origin": "http://localhost:3000",
        "trusted_proxy_cidrs": list(DEFAULT_PROXY_CIDRS), "extra_allowed_origins": [],
        "cloudflare_connector_id": None,
    })


def patch_deployment(current: DeploymentConfig, patch: Mapping[str, Any]) -> DeploymentConfig:
    if not isinstance(patch, dict) or not set(patch) <= _FIELDS:
        raise DeploymentConfigError("The deployment patch contains an unsupported field.")
    payload = current.payload()
    payload.update(patch)
    if "access_mode" in patch and payload["access_mode"] != current.access_mode.value and "public_origin" not in patch:
        raise DeploymentConfigError("Review a public origin when changing access mode.")
    if "web_port" in patch and "public_origin" not in patch and current.access_mode in {AccessMode.LOCAL, AccessMode.LAN}:
        host = urlsplit(current.public_origin).hostname or ""
        formatted = f"[{host}]" if ":" in host else host
        try:
            port = validate_managed_port(patch["web_port"])
        except InstallationEvidenceError as exc:
            raise DeploymentConfigError("The web port must be 1024–65535, excluding 8000.") from exc
        payload["public_origin"] = f"http://{formatted}:{port}"
    return parse_deployment_payload(payload)


def parse_deployment_json(raw: str | bytes) -> DeploymentConfig:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise DeploymentConfigError("Deployment JSON contains duplicate fields.")
            result[key] = value
        return result

    def reject(value: str) -> Any:
        raise DeploymentConfigError("Deployment JSON contains a nonstandard number.")

    try:
        if not isinstance(raw, (str, bytes)) or len(raw) > MAX_DEPLOYMENT_BYTES:
            raise DeploymentConfigError("Deployment JSON exceeds its input bound.")
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > MAX_DEPLOYMENT_BYTES:
            raise DeploymentConfigError("Deployment JSON exceeds its input bound.")
        return parse_deployment_payload(json.loads(encoded.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject))
    except DeploymentConfigError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DeploymentConfigError("Deployment JSON is invalid.") from exc
