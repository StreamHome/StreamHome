"""Immutable submitted context for automatically followed media sources."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

Origin = tuple[str, str, int]
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


def _referer_origin(value: str) -> str:
    """Project a page URL to its origin without disclosing its private components."""
    if not isinstance(value, str) or len(value) > 4096 or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ) or "\\" in value:
        raise ContextPolicyError("Invalid source referer")
    parsed = urlsplit(value)
    if not parsed.netloc or parsed.netloc.endswith(":") or parsed.username is not None or parsed.password is not None:
        raise ContextPolicyError("Invalid source referer")
    scheme, host, port = url_origin(value)
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    return urlunsplit((scheme, authority, "/", "", ""))


@dataclass(frozen=True)
class SourceContextPolicy:
    root: Origin
    context: tuple[tuple[str, str], ...]
    source_context: tuple[tuple[str, str], ...]

    @classmethod
    def capture(cls, source_url: str, headers: Mapping[str, str]) -> SourceContextPolicy:
        root = url_origin(source_url)
        context = []
        source_context = []
        submitted = set()
        for name, value in headers.items():
            normalized = name.lower()
            if normalized not in {"origin", "referer"}:
                continue
            if normalized in submitted:
                raise ContextPolicyError("Duplicate source context headers")
            submitted.add(normalized)
            try:
                if normalized == "origin":
                    plain_origin(value)
                    projected = value
                else:
                    projected = _referer_origin(value)
            except (ValueError, UnicodeError):
                continue
            context.append((normalized, projected))
            source_context.append((normalized, value))
        return cls(root, tuple(context), tuple(source_context))

    def select(self, headers: Mapping[str, str], previous_url: str, target_url: str) -> dict[str, str]:
        previous, target = url_origin(previous_url), url_origin(target_url)
        if previous[0] == "https" and target[0] == "http":
            raise ContextPolicyError("Media source may not downgrade HTTPS")
        selected = dict(headers)
        if previous == target:
            return selected
        selected = {name: value for name, value in selected.items()
                    if name.lower() not in {"authorization", "cookie", "origin", "referer"}}
        # Restore submitted context automatically inside this relay only. Full
        # page context can return solely to the original source, never to a CDN.
        for name, value in self.source_context if target == self.root else self.context:
            selected[name] = value
        return selected

    def decision(self, target_url: str) -> str:
        target = url_origin(target_url)
        if target == self.root:
            return "source-origin"
        return "automatic-context" if self.context else "no-safe-context"
