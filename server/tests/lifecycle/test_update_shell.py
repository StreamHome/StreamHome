from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from lifecycle import state


class UpdateShellLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.repository_root = Path(__file__).resolve().parents[3]
        script = (self.repository_root / "update.sh").read_text(encoding="utf-8")
        prefix, marker, _ = script.partition('case "${1:-}" in')
        self.assertTrue(marker)
        self.library = self.temp / "update-functions.sh"
        self.library.write_text(prefix, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _shell(self, script: str, *, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if environment:
            env.update(environment)
        return subprocess.run(
            ["bash", "-c", f'source "$1"\n{script}', "bash", str(self.library)],
            cwd=self.repository_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _git(self, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def _commit(self, repository: Path, message: str) -> str:
        self._git(repository, "add", ".")
        self._git(repository, "commit", "-m", message)
        return self._git(repository, "rev-parse", "HEAD")

    def _support(self, repository: Path, commit: str, *, environment: dict[str, str] | None = None) -> str:
        result = self._shell(
            f'ROOT_DIR={shlex_quote(str(repository))}\n'
            f'OLD_COMMIT={shlex_quote(commit)}\n'
            "installed_release_lifecycle_support",
            environment=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_support_detection_distinguishes_absent_present_and_partial(self) -> None:
        repository = self.temp / "history"
        repository.mkdir()
        self._git(repository, "init")
        self._git(repository, "config", "user.name", "StreamHome Test")
        self._git(repository, "config", "user.email", "streamhome-test@example.invalid")
        (repository / "marker").write_text("pre-phase1\n", encoding="utf-8")
        absent = self._commit(repository, "pre phase one")

        lifecycle = repository / "server" / "lifecycle"
        lifecycle.mkdir(parents=True)
        (lifecycle / "state.py").write_text("# state\n", encoding="utf-8")
        partial = self._commit(repository, "partial phase one")

        (lifecycle / "cli.py").write_text("# cli\n", encoding="utf-8")
        present = self._commit(repository, "phase one")

        self.assertEqual(self._support(repository, absent), "PRE_PHASE1_CONFIRMED")
        self.assertEqual(self._support(repository, partial), "INDETERMINATE")
        self.assertEqual(self._support(repository, present), "PHASE1_CONFIRMED")

    def test_support_detection_fails_closed_for_missing_object_and_repository(self) -> None:
        repository = self.temp / "repository"
        repository.mkdir()
        self._git(repository, "init")
        self.assertEqual(self._support(repository, "f" * 40), "INDETERMINATE")

        not_a_repository = self.temp / "not-a-repository"
        not_a_repository.mkdir()
        self.assertEqual(self._support(not_a_repository, "a" * 40), "INDETERMINATE")

    def test_support_detection_fails_closed_when_git_cat_file_fails(self) -> None:
        repository = self.temp / "repository"
        repository.mkdir()
        fake_bin = self.temp / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/usr/bin/env bash\nexit 2\n", encoding="utf-8")
        fake_git.chmod(0o755)
        environment = {"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
        self.assertEqual(
            self._support(repository, "a" * 40, environment=environment),
            "INDETERMINATE",
        )

    def _make_lifecycle_installation(self) -> Path:
        root = self.temp / f"installation-{uuid.uuid4()}"
        (root / "server").mkdir(parents=True)
        shutil.copytree(self.repository_root / "server" / "lifecycle", root / "server" / "lifecycle")
        shutil.copy2(self.repository_root / "update.sh", root / "update.sh")
        (root / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        return root

    def _journal(self, root: Path, operation_id: str, generation: int, phase: str = "reserved") -> Path:
        run_dir = root / ".run"
        run_dir.mkdir(mode=0o700, exist_ok=True)
        path = run_dir / "update-state.json"
        path.write_text(
            json.dumps(
                {
                    "transaction_id": operation_id,
                    "lifecycle_generation": generation,
                    "lifecycle_phase": phase,
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _lifecycle_variables(self, root: Path, operation_id: str, generation: int, status: Path) -> str:
        return "\n".join(
            (
                f"ROOT_DIR={shlex_quote(str(root))}",
                f"RUN_DIR={shlex_quote(str(root / '.run'))}",
                f"STATUS_FILE={shlex_quote(str(status))}",
                f"TRANSACTION_ID={shlex_quote(operation_id)}",
                f"LIFECYCLE_OPERATION_ID={shlex_quote(operation_id)}",
                f"LIFECYCLE_GENERATION={generation}",
                "LIFECYCLE_PHASE=reserved",
                "LIFECYCLE_ENABLED=true",
                "LIFECYCLE_HANDOFF_REQUESTED=false",
            )
        )

    def test_recovery_transition_updates_shared_authority_and_journal(self) -> None:
        root = self._make_lifecycle_installation()
        claimed = state.claim_operation_with_credential(
            root, operation_type="update", owner_kind="updater"
        )
        operation = claimed.state.active_operation
        journal = self._journal(root, operation.operation_id, claimed.state.generation)
        result = self._shell(
            self._lifecycle_variables(root, operation.operation_id, claimed.state.generation, journal)
            + "\nmark_lifecycle_recovery_required",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            state.inspect_state(root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )
        self.assertEqual(json.loads(journal.read_text())["lifecycle_phase"], "recovery_required")

    def test_recovery_transition_survives_journal_reconciliation_failure(self) -> None:
        root = self._make_lifecycle_installation()
        claimed = state.claim_operation_with_credential(
            root, operation_type="update", owner_kind="updater"
        )
        operation = claimed.state.active_operation
        invalid_status = root / ".run" / "journal-is-a-directory"
        invalid_status.mkdir()
        result = self._shell(
            self._lifecycle_variables(
                root, operation.operation_id, claimed.state.generation, invalid_status
            )
            + "\nmark_lifecycle_recovery_required",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            state.inspect_state(root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )

    def test_recovery_uses_authoritative_phase_and_never_moves_it_backward(self) -> None:
        root = self._make_lifecycle_installation()
        claimed = state.claim_operation_with_credential(
            root, operation_type="update", owner_kind="updater"
        )
        operation = claimed.state.active_operation
        state.transition_operation_with_credential(
            root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        journal = self._journal(root, operation.operation_id, claimed.state.generation)
        result = self._shell(
            self._lifecycle_variables(root, operation.operation_id, claimed.state.generation, journal)
            + "\ninitialize_lifecycle_authority\n"
            + 'printf "phase=%s enabled=%s\\n" "$LIFECYCLE_PHASE" "$LIFECYCLE_ENABLED"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("phase=recovery_required enabled=true", result.stdout)
        self.assertEqual(json.loads(journal.read_text())["lifecycle_phase"], "recovery_required")
        self.assertEqual(
            state.inspect_state(root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


if __name__ == "__main__":
    unittest.main()
