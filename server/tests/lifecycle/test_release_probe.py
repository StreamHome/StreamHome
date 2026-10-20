from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lifecycle import release_probe as probe
from lifecycle.command_runner import CommandResult, CommandUnavailableError, run_bounded
from lifecycle.install_discovery import OFFICIAL_REPOSITORY, inspect_candidate
from tests.lifecycle.test_install_discovery import make_candidate


class ReleaseProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.remote = self.parent / "official-fixture"
        self.remote.mkdir()
        self.environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.parent), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        self.git("init", "--quiet", "-b", "main")
        self.first = self.commit("first")
        self.second = self.commit("second")
        self.hint = make_candidate(self.parent / "installed", commit=self.first)
        self.installation = inspect_candidate(self.hint)
        self.policy = probe.ReleasePolicy()
        self.scratch = set()
        self.initial = self.snapshot()

    def git(self, *args):
        return subprocess.check_output(["/usr/bin/git", *args], cwd=self.remote, env=self.environment, stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self, text):
        (self.remote / "marker").write_text(text)
        self.git("add", "marker")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", text)
        return self.git("rev-parse", "HEAD")

    def snapshot(self):
        return {str(path.relative_to(self.hint.path)): path.read_bytes() for path in self.hint.path.rglob("*") if path.is_file()}

    def read_current(self):
        return inspect_candidate(self.hint), self.policy

    def runner(self, arguments, **kwargs):
        self.assertNotEqual(kwargs["cwd"], self.hint.path)
        self.scratch.add(kwargs["cwd"])
        self.assertNotIn("STREAMHOME_INSTANCE_ROOT", kwargs["environment"])
        # The production source remains a fixed HTTPS official repository. This
        # test-only transport redirects it into a disposable local Git fixture.
        rewritten = [str(self.remote) if value == OFFICIAL_REPOSITORY else "protocol.file.allow=always" if value == "protocol.file.allow=never" else value for value in arguments]
        return run_bounded(rewritten, **{**kwargs, "environment": {**kwargs["environment"], "GIT_ALLOW_PROTOCOL": "file"}})

    def check(self, **kwargs):
        with patch.object(probe, "run_bounded", side_effect=self.runner):
            result = probe.probe_release(self.installation, self.policy, read_current=self.read_current, **kwargs)
        self.assertEqual(self.snapshot(), self.initial)
        self.assertTrue(all(not path.exists() for path in self.scratch))
        return result

    def test_ancestry_verified_update_changes_no_installed_files_or_refs(self) -> None:
        result = self.check()
        self.assertIs(result.status, probe.ProbeStatus.ELIGIBLE)
        self.assertEqual((result.installed_commit, result.target_commit), (self.first, self.second))
        self.assertNotIn("streamhome update", result.reason)

    def test_identical_target_requires_no_fetch(self) -> None:
        self.git("reset", "--hard", self.first)
        calls = []
        original = self.runner
        def record(arguments, **kwargs):
            calls.append(arguments)
            return original(arguments, **kwargs)
        with patch.object(probe, "run_bounded", side_effect=record):
            result = probe.probe_release(self.installation, self.policy, read_current=self.read_current)
        self.assertIs(result.status, probe.ProbeStatus.IDENTICAL)
        self.assertFalse(any("fetch" in call for call in calls))

    def test_failed_target_is_not_cleared_or_retried(self) -> None:
        self.policy = replace(self.policy, failed_target=self.second)
        result = self.check()
        self.assertIs(result.status, probe.ProbeStatus.POLICY_REJECTED)
        self.assertEqual(self.policy.failed_target, self.second)

    def test_signed_policy_without_qualified_verifier_remains_unverified(self) -> None:
        self.policy = replace(self.policy, require_signed_commits=True, trusted_signers=("A" * 40,))
        self.assertIs(self.check().status, probe.ProbeStatus.DIFFERENT_UNVERIFIED)
        self.assertIs(self.check(verify_signatures=lambda *args: False).status, probe.ProbeStatus.POLICY_REJECTED)
        self.assertIs(self.check(verify_signatures=lambda *args: True).status, probe.ProbeStatus.ELIGIBLE)

    def test_network_failure_cannot_mean_up_to_date(self) -> None:
        with patch.object(probe, "run_bounded", side_effect=CommandUnavailableError("private error must not be printed")):
            result = probe.probe_release(self.installation, self.policy, read_current=self.read_current)
        self.assertIs(result.status, probe.ProbeStatus.UNAVAILABLE)
        self.assertNotIn("private error", result.reason)
        self.assertEqual(self.snapshot(), self.initial)

    def test_bad_remote_output_is_unavailable_and_never_interpreted_as_commands(self) -> None:
        original = self.runner
        def invalid(arguments, **kwargs):
            if "ls-remote" in arguments:
                return CommandResult(0, b"$(malicious) refs/heads/main\n", b"")
            return original(arguments, **kwargs)
        with patch.object(probe, "run_bounded", side_effect=invalid):
            result = probe.probe_release(self.installation, self.policy, read_current=self.read_current)
        self.assertIs(result.status, probe.ProbeStatus.UNAVAILABLE)
        self.assertNotIn("malicious", result.reason)

    def test_changed_installation_or_release_policy_reports_stale(self) -> None:
        calls = 0
        def changed():
            nonlocal calls
            calls += 1
            return self.installation, self.policy if calls == 1 else replace(self.policy, branch="next")
        with patch.object(probe, "run_bounded", side_effect=self.runner):
            result = probe.probe_release(self.installation, self.policy, read_current=changed)
        self.assertIs(result.status, probe.ProbeStatus.STALE)

    def test_divergence_and_ahead_are_distinct_from_eligible_update(self) -> None:
        self.git("branch", "installed", self.second)
        self.git("reset", "--hard", self.first)
        (self.hint.path / ".git/refs/heads/main").write_text(self.second + "\n")
        self.installation = inspect_candidate(self.hint)
        self.initial = self.snapshot()
        self.assertIs(self.check().status, probe.ProbeStatus.AHEAD)
        self.commit("divergent")
        self.assertIs(self.check().status, probe.ProbeStatus.DIVERGED)

    def test_policy_does_not_accept_arbitrary_refs_or_weak_signer_identifiers(self) -> None:
        for branch in ("../main", "main:refs/heads/x", "-main", "main.lock", "foo//main", "foo/.hidden"):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                replace(self.policy, branch=branch).validate()
        for signers in (("short",), ([],), ("A" * 40, "A" * 40)):
            with self.subTest(signers=signers), self.assertRaises(ValueError):
                replace(self.policy, trusted_signers=signers).validate()


if __name__ == "__main__":
    unittest.main()
