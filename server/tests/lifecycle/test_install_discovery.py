from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lifecycle import install_discovery as discovery


def make_candidate(root: Path, *, branch="main", commit="a" * 40) -> discovery.CandidateHint:
    root.mkdir(mode=0o755)
    (root / ".git/refs/heads").mkdir(parents=True)
    (root / ".git/config").write_text('[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = https://github.com/StreamHome/StreamHome.git\n')
    if branch is None:
        (root / ".git/HEAD").write_text(commit + "\n")
    else:
        (root / ".git/HEAD").write_text("ref: refs/heads/" + branch + "\n")
        reference = root / ".git/refs/heads" / branch
        reference.parent.mkdir(exist_ok=True, parents=True)
        reference.write_text(commit + "\n")
    (root / "server").mkdir()
    (root / "web").mkdir()
    for name in ("start.sh", "update.sh", "setup.sh", "server/config.py", "web/package.json"):
        (root / name).write_text("This fixture must never be executed.\n")
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return discovery.CandidateHint(root, os.getuid(), "original_user_home")


class InstallDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.hint = make_candidate(self.parent / "StreamHome")

    def test_existing_install_is_read_only_and_absence_is_explicit(self) -> None:
        before = {str(path): path.read_bytes() for path in self.hint.path.rglob("*") if path.is_file()}
        result = discovery.discover_installations((self.hint,))
        self.assertIs(result.status, discovery.DiscoveryStatus.EXISTING)
        self.assertEqual(result.installation.commit, "a" * 40)
        self.assertEqual(result.installation.mode, "legacy")
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.hint.path.rglob("*") if path.is_file()})
        result = discovery.discover_installations((replace(self.hint, path=self.parent / "missing"),))
        self.assertIs(result.status, discovery.DiscoveryStatus.ABSENT)

    def test_explicit_or_registered_missing_location_blocks_second_install(self) -> None:
        for source in ("explicit", "registered_unit"):
            with self.subTest(source=source):
                result = discovery.discover_installations((replace(self.hint, path=self.parent / "missing", source=source),))
                self.assertIs(result.status, discovery.DiscoveryStatus.UNAVAILABLE)

    def test_multiple_candidates_are_ambiguous_and_identical_hints_deduplicate(self) -> None:
        other = make_candidate(self.parent / "custom")
        self.assertIs(discovery.discover_installations((self.hint, other)).status, discovery.DiscoveryStatus.AMBIGUOUS)
        self.assertIs(discovery.discover_installations((self.hint, self.hint)).status, discovery.DiscoveryStatus.EXISTING)

    def test_partial_and_foreign_checkout_are_not_empty_destinations(self) -> None:
        (self.hint.path / "server/config.py").unlink()
        self.assertIs(discovery.discover_installations((self.hint,)).status, discovery.DiscoveryStatus.UNAVAILABLE)
        (self.hint.path / "server/config.py").write_text("fixture")
        config = self.hint.path / ".git/config"
        config.write_text(config.read_text().replace("github.com/StreamHome/StreamHome", "evil.example/StreamHome/StreamHome"))
        self.assertIs(discovery.discover_installations((self.hint,)).status, discovery.DiscoveryStatus.UNAVAILABLE)

    def test_private_development_and_managed_worktrees_are_never_adopted(self) -> None:
        for branch in ("dev", "codex/experiment"):
            hint = make_candidate(self.parent / branch.replace("/", "-"), branch=branch)
            self.assertIs(discovery.discover_installations((hint,)).status, discovery.DiscoveryStatus.UNAVAILABLE)
        (self.hint.path / "AGENTS.md").write_text("private")
        (self.hint.path / "memory-bank").mkdir()
        self.assertIs(discovery.discover_installations((self.hint,)).status, discovery.DiscoveryStatus.UNAVAILABLE)

    def test_symlinks_ownership_and_insecure_modes_fail_closed(self) -> None:
        alias = self.parent / "alias"
        alias.symlink_to(self.hint.path, target_is_directory=True)
        self.assertIs(discovery.discover_installations((replace(self.hint, path=alias),)).status, discovery.DiscoveryStatus.UNAVAILABLE)
        self.assertIs(discovery.discover_installations((replace(self.hint, owner_uid=os.getuid() + 1),)).status, discovery.DiscoveryStatus.UNAVAILABLE)
        (self.hint.path / ".git/config").chmod(0o666)
        self.assertIs(discovery.discover_installations((self.hint,)).status, discovery.DiscoveryStatus.UNAVAILABLE)

    def test_packed_main_and_detached_commits_are_supported_without_git_execution(self) -> None:
        (self.hint.path / ".git/refs/heads/main").unlink()
        (self.hint.path / ".git/packed-refs").write_text("# pack-refs with: peeled\n" + "b" * 40 + " refs/heads/main\n")
        (self.hint.path / ".git/packed-refs").chmod(0o644)
        self.assertEqual(discovery.inspect_candidate(self.hint).commit, "b" * 40)
        (self.hint.path / ".git/HEAD").write_text("c" * 40 + "\n")
        self.assertIsNone(discovery.inspect_candidate(self.hint).branch)
        self.assertEqual(discovery.inspect_candidate(self.hint).commit, "c" * 40)

    def test_home_comes_from_passwd_not_home_environment(self) -> None:
        class Record:
            pw_dir = "/verified/home"
            pw_name = "owner"
            pw_gid = 2001
        with patch.object(discovery.os, "getuid", return_value=2000), patch.object(discovery.os, "geteuid", return_value=2000), patch.object(discovery.pwd, "getpwuid", return_value=Record()), patch.dict(os.environ, {"HOME": "/wrong"}, clear=True):
            self.assertEqual(discovery.original_user_home().path, Path("/verified/home/StreamHome"))
        with patch.object(discovery.os, "geteuid", return_value=0), patch.object(discovery.pwd, "getpwuid", return_value=Record()), patch.dict(os.environ, {"SUDO_UID": "2000", "SUDO_GID": "2001", "SUDO_USER": "owner"}, clear=True):
            self.assertEqual(discovery.original_user_home().owner_uid, 2000)
        with patch.object(discovery.os, "geteuid", return_value=0), patch.dict(os.environ, {}, clear=True), self.assertRaises(discovery.DiscoveryError):
            discovery.original_user_home()


if __name__ == "__main__":
    unittest.main()
