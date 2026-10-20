"""Existing-installation evidence without imports, Git writes or shell execution.

Discovery never adopts, relocates, repairs or installs anything. The installer
must stop on invalid, inaccessible or ambiguous evidence. Root descriptor and
effective unit metadata are resolved by the protected entry point, not inferred
from an arbitrary environment variable.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path
import pwd
import re
import stat

from .installation import DescriptorSnapshot, InstallationStatus


OFFICIAL_REPOSITORY = "https://github.com/StreamHome/StreamHome.git"
OFFICIAL_REMOTES = frozenset({
    "https://github.com/StreamHome/StreamHome", OFFICIAL_REPOSITORY,
    "git@github.com:StreamHome/StreamHome.git",
    "https://github.com/WaqSea/StreamHome", "https://github.com/WaqSea/StreamHome.git",
    "git@github.com:WaqSea/StreamHome.git",
})
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")


class DiscoveryError(ValueError):
    pass


class DiscoveryStatus(str, Enum):
    ABSENT = "absent"
    EXISTING = "existing"
    PROVISIONING = "provisioning"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CandidateHint:
    path: Path
    owner_uid: int
    source: str


@dataclass(frozen=True)
class ExistingInstallation:
    path: Path
    owner_uid: int
    commit: str
    branch: str | None
    mode: str
    evidence_sha256: str


@dataclass(frozen=True)
class DiscoveryResult:
    status: DiscoveryStatus
    installation: ExistingInstallation | None
    reason: str


def original_user_home() -> CandidateHint:
    """Use kernel identity, or a complete sudo identity corroborated by passwd."""
    uid = os.getuid()
    if os.geteuid() == 0:
        sudo_fields = (os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID"), os.environ.get("SUDO_USER"))
        if any(value is not None for value in sudo_fields):
            raw_uid, raw_gid, username = sudo_fields
            if not raw_uid or not raw_uid.isascii() or not raw_uid.isdigit() or not raw_gid or not raw_gid.isascii() or not raw_gid.isdigit():
                raise DiscoveryError("The original sudo identity is inconsistent.")
            uid = int(raw_uid)
            record = pwd.getpwuid(uid)
            if uid <= 0 or record.pw_name != username or record.pw_gid != int(raw_gid):
                raise DiscoveryError("The original sudo identity is inconsistent.")
        else:
            raise DiscoveryError("Supply --existing-install when invoking discovery directly as root.")
    record = pwd.getpwuid(uid)
    home = Path(record.pw_dir)
    if not home.is_absolute():
        raise DiscoveryError("The original user's home is unavailable.")
    return CandidateHint(home / "StreamHome", uid, "original_user_home")


def _directory(path: Path, uid: int) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid or metadata.st_mode & 0o7022:
        raise DiscoveryError("An installation directory has unsafe ownership or permissions.")


def _read(root: Path, relative: str, uid: int, *, maximum: int = 64 * 1024) -> bytes:
    path = root
    parts = Path(relative).parts
    if not parts or any(part in {".", "..", "/"} for part in parts):
        raise DiscoveryError("Installation evidence has an invalid path.")
    for part in parts[:-1]:
        path /= part
        _directory(path, uid)
    descriptor = os.open(path / parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_mode & 0o7022 or before.st_nlink != 1 or before.st_size > maximum:
            raise DiscoveryError("Installation evidence is not a trusted regular file.")
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
        linked = (path / parts[-1]).lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_uid, item.st_size, item.st_mtime_ns, item.st_ctime_ns, item.st_nlink)
        if len(raw) > maximum or identity(before) != identity(after) or identity(before) != identity(linked):
            raise DiscoveryError("Installation evidence changed during inspection.")
        return raw


def inspect_candidate(hint: CandidateHint, *, mode: str = "legacy") -> ExistingInstallation | None:
    root = hint.path
    if not root.is_absolute() or ".." in root.parts:
        raise DiscoveryError("Use an absolute canonical existing-installation path.")
    try:
        root.lstat()
    except FileNotFoundError:
        return None
    try:
        if root.resolve(strict=True) != root:
            raise DiscoveryError("Installation discovery does not follow path aliases.")
        _directory(root, hint.owner_uid)
        # Inspect source identity as data; no candidate-owned interpreter, hooks,
        # Git configuration, .env or Python module is executed.
        _directory(root / ".git", hint.owner_uid)
        config_raw = _read(root, ".git/config", hint.owner_uid)
        config = configparser.RawConfigParser(strict=True)
        config.read_string(config_raw.decode("utf-8"))
        if config.get('remote "origin"', "url").rstrip("/") not in OFFICIAL_REMOTES:
            raise DiscoveryError("The existing checkout is not an official StreamHome installation.")
        head = _read(root, ".git/HEAD", hint.owner_uid, maximum=512).decode("ascii").strip()
        branch = None
        evidence = [config_raw, head.encode("ascii")]
        if head.startswith("ref: refs/heads/"):
            branch = head.removeprefix("ref: refs/heads/")
            if not _BRANCH.fullmatch(branch) or ".." in branch or any(part.startswith(".") or part.endswith(".") or not part for part in branch.split("/")):
                raise DiscoveryError("The existing branch reference is invalid.")
            if branch == "dev" or branch.startswith("codex/") or (root / "AGENTS.md").exists() and (root / "memory-bank").exists():
                raise DiscoveryError("A private development checkout is not a production installation.")
            reference = "refs/heads/" + branch
            try:
                commit_raw = _read(root, ".git/" + reference, hint.owner_uid, maximum=512)
                head = commit_raw.decode("ascii").strip()
                evidence.append(commit_raw)
            except FileNotFoundError:
                packed = _read(root, ".git/packed-refs", hint.owner_uid, maximum=4 * 1024 * 1024)
                matches = [line.split()[0] for line in packed.decode("ascii").splitlines() if line and not line.startswith(("#", "^")) and line.split()[1:] == [reference]]
                if len(matches) != 1:
                    raise DiscoveryError("The installed commit cannot be identified.")
                head = matches[0]
                evidence.append(packed)
        elif (root / "AGENTS.md").exists() and (root / "memory-bank").exists():
            raise DiscoveryError("A private development checkout is not a production installation.")
        if not _COMMIT.fullmatch(head):
            raise DiscoveryError("The installed commit cannot be identified.")
        for marker in ("start.sh", "update.sh", "setup.sh", "server/config.py", "web/package.json"):
            evidence.append(_read(root, marker, hint.owner_uid, maximum=1024 * 1024))
        digest = hashlib.sha256()
        for raw in evidence:
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
        return ExistingInstallation(root, hint.owner_uid, head, branch, mode, digest.hexdigest())
    except DiscoveryError:
        raise
    except (OSError, UnicodeError, ValueError, configparser.Error) as exc:
        raise DiscoveryError("Existing installation evidence is incomplete or unavailable; nothing was changed.") from exc


def discover_installations(
    hints: tuple[CandidateHint, ...], *, descriptor: DescriptorSnapshot | None = None,
) -> DiscoveryResult:
    if descriptor is not None:
        installed = descriptor.descriptor
        hints = (CandidateHint(installed.paths.application, installed.service_uid, "descriptor"), *hints)
    found: dict[Path, ExistingInstallation] = {}
    try:
        for hint in hints:
            managed = descriptor is not None and hint.path == descriptor.descriptor.paths.application
            candidate = inspect_candidate(hint, mode="managed" if managed else "legacy")
            if candidate is not None:
                previous = found.get(candidate.path)
                if previous is not None and (previous.owner_uid != candidate.owner_uid or previous.commit != candidate.commit):
                    raise DiscoveryError("Installation identity changed during discovery.")
                found[candidate.path] = candidate
            elif hint.source in {"descriptor", "registered_unit", "explicit"}:
                raise DiscoveryError("A registered or explicitly selected installation is unavailable.")
    except DiscoveryError as exc:
        return DiscoveryResult(DiscoveryStatus.UNAVAILABLE, None, str(exc))
    if len(found) > 1:
        return DiscoveryResult(DiscoveryStatus.AMBIGUOUS, None, "Multiple StreamHome installations were found; select and diagnose the intended installation before continuing.")
    if not found:
        return DiscoveryResult(DiscoveryStatus.ABSENT, None, "No installation was found in the inspected locations.")
    candidate = next(iter(found.values()))
    status = DiscoveryStatus.EXISTING
    if descriptor is not None and descriptor.descriptor.status is InstallationStatus.PROVISIONING:
        status = DiscoveryStatus.PROVISIONING
    return DiscoveryResult(status, candidate, "StreamHome is already installed; discovery did not reinstall, adopt or update it.")
