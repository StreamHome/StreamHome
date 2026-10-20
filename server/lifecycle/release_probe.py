"""Isolated release discovery, without application imports or installed Git writes.

Remote objects, refs and Git configuration exist only in one owned temporary
repository. Different commits are never called eligible until ancestry, local
failed-target policy and configured signature requirements have been satisfied.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Callable

from .command_runner import CommandUnavailableError, run_bounded
from .install_discovery import CandidateHint, DiscoveryError, ExistingInstallation, OFFICIAL_REPOSITORY, inspect_candidate


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FINGERPRINT = re.compile(r"^(?:[A-F0-9]{40}|[A-F0-9]{64})$")


class ProbeStatus(str, Enum):
    IDENTICAL = "up_to_date"
    ELIGIBLE = "update_available"
    DIFFERENT_UNVERIFIED = "different_unverified"
    AHEAD = "ahead_of_release"
    DIVERGED = "diverged"
    POLICY_REJECTED = "policy_rejected"
    UNAVAILABLE = "check_unavailable"
    STALE = "stale_retry"


@dataclass(frozen=True)
class ReleasePolicy:
    branch: str = "main"
    require_signed_commits: bool = False
    trusted_signers: tuple[str, ...] = ()
    failed_target: str | None = None

    def validate(self) -> None:
        if (
            not isinstance(self.branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}", self.branch)
            or ".." in self.branch or self.branch.endswith(("/", ".", ".lock"))
            or any(not part or part.startswith(".") or part.endswith(".lock") for part in self.branch.split("/"))
            or type(self.require_signed_commits) is not bool
        ):
            raise DiscoveryError("The configured release branch or signature policy is invalid.")
        if not isinstance(self.trusted_signers, tuple) or len(self.trusted_signers) > 64:
            raise DiscoveryError("The configured release signers are invalid.")
        if any(not isinstance(value, str) or not _FINGERPRINT.fullmatch(value) for value in self.trusted_signers):
            raise DiscoveryError("The configured release signers are invalid.")
        if len(set(self.trusted_signers)) != len(self.trusted_signers):
            raise DiscoveryError("The configured release signers are invalid.")
        if self.failed_target is not None and (not isinstance(self.failed_target, str) or not _COMMIT.fullmatch(self.failed_target)):
            raise DiscoveryError("The suppressed failed target is invalid.")

    @property
    def identity(self) -> str:
        self.validate()
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ProbeResult:
    status: ProbeStatus
    installed_commit: str
    target_commit: str | None
    reason: str


SnapshotReader = Callable[[], tuple[ExistingInstallation, ReleasePolicy]]
# The qualified signer adapter must use a separately bound public trust snapshot,
# without executing installed Git/GPG configuration or changing its keyrings.
SignatureVerifier = Callable[[Path, str, str, ReleasePolicy], bool]


def probe_release(
    installation: ExistingInstallation, policy: ReleasePolicy, *,
    read_current: SnapshotReader, verify_signatures: SignatureVerifier | None = None,
) -> ProbeResult:
    policy.validate()
    result = ProbeResult(ProbeStatus.UNAVAILABLE, installation.commit, None, "The official release source is unavailable; no update or reinstall was performed.")
    try:
        # Recheck all source/identity evidence before trusting an installed hash.
        before = inspect_candidate(CandidateHint(installation.path, installation.owner_uid, "probe"), mode=installation.mode)
        if before != installation or read_current() != (installation, policy):
            return ProbeResult(ProbeStatus.STALE, installation.commit, None, "Installation or release policy changed; retry discovery.")
        with tempfile.TemporaryDirectory(prefix="streamhome-release-probe-") as directory:
            scratch = Path(directory)
            environment = {
                "PATH": "/usr/bin:/bin", "HOME": str(scratch), "LC_ALL": "C", "LANG": "C",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                "GIT_ALLOW_PROTOCOL": "https", "GIT_OPTIONAL_LOCKS": "0",
            }

            def git(*arguments, timeout=30.0):
                return run_bounded(
                    ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                     "-c", "credential.helper=", "-c", "protocol.file.allow=never", *arguments],
                    cwd=scratch, environment=environment, timeout=timeout,
                )

            if git("init", "--bare", "--quiet").returncode:
                raise CommandUnavailableError("Disposable release repository could not be initialized.")
            reference = "refs/heads/" + policy.branch
            remote = git("ls-remote", "--refs", "--exit-code", OFFICIAL_REPOSITORY, reference)
            rows = remote.stdout.decode("ascii").strip().splitlines()
            if remote.returncode or len(rows) != 1:
                raise CommandUnavailableError("The configured official release reference is unavailable.")
            fields = rows[0].split()
            if len(fields) != 2 or fields[1] != reference or not _COMMIT.fullmatch(fields[0]):
                raise CommandUnavailableError("The official release reference is ambiguous.")
            target = fields[0]
            if target == installation.commit:
                result = ProbeResult(ProbeStatus.IDENTICAL, installation.commit, target, "The installed commit matches the configured official release.")
            else:
                result = ProbeResult(ProbeStatus.DIFFERENT_UNVERIFIED, installation.commit, target, "A different release exists; update eligibility has not been verified.")
                fetch = git("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", OFFICIAL_REPOSITORY, reference + ":refs/probe/release")
                resolved = git("rev-parse", "--verify", "refs/probe/release^{commit}")
                if fetch.returncode or resolved.returncode:
                    raise CommandUnavailableError("Release history is unavailable.")
                if resolved.stdout.decode("ascii").strip() != target:
                    result = ProbeResult(ProbeStatus.STALE, installation.commit, target, "The release reference changed during discovery; retry.")
                else:
                    if git("cat-file", "-e", installation.commit + "^{commit}").returncode:
                        git("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", OFFICIAL_REPOSITORY, installation.commit)
                    if git("cat-file", "-e", installation.commit + "^{commit}").returncode == 0:
                        forward = git("merge-base", "--is-ancestor", installation.commit, target)
                        backward = git("merge-base", "--is-ancestor", target, installation.commit)
                        if forward.returncode not in {0, 1} or backward.returncode not in {0, 1}:
                            raise CommandUnavailableError("Release ancestry is unavailable.")
                        if forward.returncode == 0:
                            if target == policy.failed_target:
                                result = ProbeResult(ProbeStatus.POLICY_REJECTED, installation.commit, target, "This exact release previously failed; automatic retry remains suppressed.")
                            elif policy.require_signed_commits and (not policy.trusted_signers or verify_signatures is None):
                                result = ProbeResult(ProbeStatus.DIFFERENT_UNVERIFIED, installation.commit, target, "Signature trust could not be verified in isolation; the release is not approved for update.")
                            elif policy.require_signed_commits and not verify_signatures(scratch, installation.commit, target, policy):
                                result = ProbeResult(ProbeStatus.POLICY_REJECTED, installation.commit, target, "The commit range does not satisfy the configured signature policy.")
                            else:
                                result = ProbeResult(ProbeStatus.ELIGIBLE, installation.commit, target, "A verified eligible update is available. Use the installed application's supported Updates workflow; the installer made no changes.")
                        elif backward.returncode == 0:
                            result = ProbeResult(ProbeStatus.AHEAD, installation.commit, target, "The installation is ahead of the configured release; no downgrade was performed.")
                        else:
                            result = ProbeResult(ProbeStatus.DIVERGED, installation.commit, target, "The installation and configured release have diverged; no update was performed.")
    except (CommandUnavailableError, DiscoveryError, OSError, UnicodeError, ValueError):
        # Do not print raw command errors or URLs/configuration from Git output.
        result = ProbeResult(ProbeStatus.UNAVAILABLE, installation.commit, result.target_commit, "The official release check could not be completed; no update or reinstall was performed.")
    try:
        current, current_policy = read_current()
        inspected = inspect_candidate(CandidateHint(installation.path, installation.owner_uid, "probe"), mode=installation.mode)
        if current != installation or inspected != installation or current_policy.identity != policy.identity:
            return ProbeResult(ProbeStatus.STALE, installation.commit, result.target_commit, "Installation or release policy changed during discovery; retry.")
    except (DiscoveryError, OSError, ValueError):
        return ProbeResult(ProbeStatus.STALE, installation.commit, result.target_commit, "Installation identity became unavailable during discovery; retry.")
    return result
