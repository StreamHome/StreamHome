"""Immutable, operator-owned permissions for non-secret HLS context headers."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import re
from typing import Mapping
from urllib.parse import urlsplit

Origin = tuple[str, str, int]
MAX_CONFIG_BYTES = 64 * 1024
MAX_SOURCE_ORIGINS = 64
MAX_TARGET_ORIGINS = 32
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ContextPolicyError(ValueError):
    pass


def url_origin(value: str) -> Origin:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host.endswith(".."):
        raise ContextPolicyError("Invalid source origin")
    host = host.removesuffix(".")
    if "%" in host:
        raise ContextPolicyError("Invalid source origin")
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        host = host.encode("idna").decode("ascii").lower()
        # Numeric shorthand and leading-zero forms have inconsistent resolver
        # meanings. Only ipaddress's unambiguous dotted decimal is accepted.
        if all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in host.split(".")):
            raise ContextPolicyError("Invalid source origin")
        if not _HOST.fullmatch(host) or len(host) > 253:
            raise ContextPolicyError("Invalid source origin")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if parsed.scheme not in {"http", "https"} or not host or not 1 <= port <= 65535:
        raise ContextPolicyError("Invalid source origin")
    return parsed.scheme, host, port


def plain_origin(value: str, *, root_path: bool = False) -> Origin:
    if not isinstance(value, str) or len(value) > 2048 or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ) or any(character in value for character in ("\\", "?", "#", "@")):
        raise ContextPolicyError("Invalid plain origin")
    parsed = urlsplit(value)
    if not parsed.netloc or parsed.netloc.endswith(":") or parsed.username or parsed.password or parsed.path not in (("", "/") if root_path else ("",)):
        raise ContextPolicyError("Invalid plain origin")
    return url_origin(value)


@dataclass(frozen=True)
class HLSContextPermissions:
    entries: tuple[tuple[Origin, frozenset[Origin]], ...] = ()

    def targets(self, source: Origin) -> frozenset[Origin]:
        return next((targets for root, targets in self.entries if root == source), frozenset())


def parse_hls_context_permissions(raw: str) -> HLSContextPermissions:
    """Reject ambiguous policy with one safe message, never configuration contents."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError
        values = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(values, dict) or len(values) > MAX_SOURCE_ORIGINS:
            raise ValueError
        entries = []
        sources = set()
        for source, targets in values.items():
            canonical = plain_origin(source)
            if canonical in sources or not isinstance(targets, list) or len(targets) > MAX_TARGET_ORIGINS:
                raise ValueError
            normalized = [plain_origin(target) for target in targets]
            if len(set(normalized)) != len(normalized):
                raise ValueError
            sources.add(canonical)
            entries.append((canonical, frozenset(normalized)))
        return HLSContextPermissions(tuple(entries))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContextPolicyError("Invalid HLS context-header configuration") from None


@dataclass(frozen=True)
class SourceContextPolicy:
    root: Origin
    allowed: frozenset[Origin]
    context: tuple[tuple[str, str], ...]
    submitted_names: frozenset[str]

    @classmethod
    def capture(cls, permissions: HLSContextPermissions, source_url: str,
                headers: Mapping[str, str]) -> SourceContextPolicy:
        root = url_origin(source_url)
        context = []
        submitted = set()
        for name, value in headers.items():
            normalized = name.lower()
            if normalized not in {"origin", "referer"}:
                continue
            if normalized in submitted:
                raise ContextPolicyError("Duplicate source context headers")
            submitted.add(normalized)
            try:
                plain_origin(value, root_path=True)
            except (ValueError, UnicodeError):
                continue  # Private paths remain restricted by the ordinary origin rules.
            context.append((normalized, value))
        return cls(root, permissions.targets(root) | {root}, tuple(context), frozenset(submitted))

    def select(self, headers: Mapping[str, str], previous_url: str, target_url: str) -> dict[str, str]:
        previous, target = url_origin(previous_url), url_origin(target_url)
        if previous[0] == "https" and target[0] == "http":
            raise ContextPolicyError("Media source may not downgrade HTTPS")
        selected = dict(headers)
        if previous != target:
            selected = {name: value for name, value in selected.items()
                        if name.lower() not in {"authorization", "cookie", "origin", "referer"}}
        if target in self.allowed:
            # Restore only immutable non-secret context, never surviving credentials.
            for name, value in self.context:
                selected = {key: item for key, item in selected.items() if key.lower() != name}
                selected[name] = value
        return selected

    def decision(self, target_url: str) -> str:
        target = url_origin(target_url)
        if target == self.root:
            return "source-origin"
        if target not in self.allowed:
            return "unapproved-origin"
        return "approved-context" if self.context else "no-safe-context"
