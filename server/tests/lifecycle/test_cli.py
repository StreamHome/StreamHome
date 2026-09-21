from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from lifecycle import cli
from lifecycle import state


class LifecycleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "StreamHome"
        (self.root / "server").mkdir(parents=True)
        for name in ("start.sh", "update.sh"):
            (self.root / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        self.repository_root = Path(__file__).resolve().parents[3]
        self.repository_run = self.repository_root / ".run"
        self.repository_run_before = self.repository_run.exists()

    def tearDown(self) -> None:
        self.assertEqual(self.repository_run.exists(), self.repository_run_before)
        self.temporary.cleanup()

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.repository_root / "server")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "lifecycle.cli", "--root", str(self.root), *arguments],
            cwd=self.repository_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_and_python_api_share_state_and_inspect_is_sanitized(self) -> None:
        operation_id = str(uuid.uuid4())
        claimed = self._run(
            "claim",
            "--operation-type", "update",
            "--owner-kind", "updater",
            "--operation-id", operation_id,
        )
        self.assertEqual(claimed.returncode, cli.EXIT_OK, claimed.stderr)
        payload = json.loads(claimed.stdout)
        self.assertEqual(payload["state"]["generation"], 1)
        direct = state.inspect_state(self.root)
        self.assertEqual(direct.active_operation.operation_id, operation_id)
        capability = state.lifecycle_paths(self.root).credential_path(operation_id).read_text(encoding="ascii").strip()
        inspected = self._run("inspect")
        self.assertEqual(inspected.returncode, cli.EXIT_OK)
        self.assertNotIn(capability, inspected.stdout)
        self.assertNotIn("owner_nonce_hash", inspected.stdout)

    def test_transition_validate_and_complete_use_canonical_credential(self) -> None:
        operation_id = str(uuid.uuid4())
        self.assertEqual(
            self._run(
                "claim", "--operation-type", "update", "--owner-kind", "updater",
                "--operation-id", operation_id,
            ).returncode,
            cli.EXIT_OK,
        )
        validated = self._run(
            "validate-owner", "--operation-id", operation_id, "--generation", "1", "--expected-phase", "reserved"
        )
        self.assertEqual(validated.returncode, cli.EXIT_OK, validated.stderr)
        transitioned = self._run(
            "transition", "--operation-id", operation_id, "--generation", "1",
            "--expected-phase", "reserved", "--next-phase", "exclusive",
        )
        self.assertEqual(transitioned.returncode, cli.EXIT_OK, transitioned.stderr)
        completed = self._run(
            "complete", "--operation-id", operation_id, "--generation", "1", "--expected-phase", "exclusive"
        )
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_validate_owner_can_report_authoritative_phase_for_recovery(self) -> None:
        operation_id = str(uuid.uuid4())
        self.assertEqual(
            self._run(
                "claim", "--operation-type", "update", "--owner-kind", "updater",
                "--operation-id", operation_id,
            ).returncode,
            cli.EXIT_OK,
        )
        self.assertEqual(
            self._run(
                "transition", "--operation-id", operation_id, "--generation", "1",
                "--expected-phase", "reserved", "--next-phase", "recovery_required",
            ).returncode,
            cli.EXIT_OK,
        )
        validated = self._run(
            "validate-owner", "--operation-id", operation_id, "--generation", "1"
        )
        self.assertEqual(validated.returncode, cli.EXIT_OK, validated.stderr)
        payload = json.loads(validated.stdout)
        self.assertEqual(
            payload["state"]["active_operation"]["phase"],
            "recovery_required",
        )
        capability = state.lifecycle_paths(self.root).credential_path(operation_id).read_text(
            encoding="ascii"
        ).strip()
        self.assertNotIn(capability, validated.stdout + validated.stderr)

    def test_complete_reports_cleanup_warning_as_authoritative_success(self) -> None:
        operation_id = str(uuid.uuid4())
        state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
            operation_id=operation_id,
        )
        arguments = SimpleNamespace(
            command="complete",
            root=self.root,
            operation_id=operation_id,
            generation=1,
            expected_phase="reserved",
        )
        with patch.object(
            state,
            "_delete_credential",
            side_effect=state.PersistenceError("simulated cleanup failure"),
        ):
            response = cli._run(arguments)
        self.assertTrue(response["ok"])
        self.assertEqual(response["warning"], "persistence_error")
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_conflict_invalid_arguments_corruption_and_schema_have_stable_exits(self) -> None:
        operation_id = str(uuid.uuid4())
        self.assertEqual(
            self._run(
                "claim", "--operation-type", "update", "--owner-kind", "updater",
                "--operation-id", operation_id,
            ).returncode,
            cli.EXIT_OK,
        )
        conflict = self._run("claim", "--operation-type", "update", "--owner-kind", "updater")
        self.assertEqual(conflict.returncode, cli.EXIT_CONFLICT)
        self.assertEqual(json.loads(conflict.stderr)["error"], "conflict")
        invalid = self._run("claim", "--operation-type", "unsupported", "--owner-kind", "updater")
        self.assertEqual(invalid.returncode, cli.EXIT_INVALID_ARGUMENTS)

        paths = state.lifecycle_paths(self.root)
        paths.state_path.write_text("{", encoding="utf-8")
        paths.state_path.chmod(0o600)
        corrupt = self._run("inspect")
        self.assertEqual(corrupt.returncode, cli.EXIT_CORRUPT_STATE)
        self.assertEqual(json.loads(corrupt.stderr)["error"], "corrupt_state")
        fail_closed = self._run("startup-decision")
        self.assertEqual(fail_closed.returncode, cli.EXIT_CORRUPT_STATE)
        self.assertEqual(json.loads(fail_closed.stdout)["decision"], "fail_closed")

        paths.state_path.write_text(
            json.dumps({"schema_version": 99, "generation": 0, "active_operation": None}),
            encoding="utf-8",
        )
        unsupported = self._run("inspect")
        self.assertEqual(unsupported.returncode, cli.EXIT_UNSUPPORTED_SCHEMA)

    def test_wrong_generation_does_not_delete_credential_or_leak_capability(self) -> None:
        operation_id = str(uuid.uuid4())
        self._run("claim", "--operation-type", "update", "--owner-kind", "updater", "--operation-id", operation_id)
        credential = state.lifecycle_paths(self.root).credential_path(operation_id)
        capability = credential.read_text(encoding="ascii").strip()
        result = self._run(
            "complete", "--operation-id", operation_id, "--generation", "9", "--expected-phase", "reserved"
        )
        self.assertEqual(result.returncode, cli.EXIT_CONFLICT)
        self.assertTrue(credential.exists())
        self.assertNotIn(capability, result.stdout + result.stderr)

    def test_wrong_credential_has_stable_ownership_exit(self) -> None:
        operation_id = str(uuid.uuid4())
        self._run("claim", "--operation-type", "update", "--owner-kind", "updater", "--operation-id", operation_id)
        credential = state.lifecycle_paths(self.root).credential_path(operation_id)
        credential.write_text("z" * 43 + "\n", encoding="ascii")
        credential.chmod(0o600)
        result = self._run(
            "complete", "--operation-id", operation_id, "--generation", "1", "--expected-phase", "reserved"
        )
        self.assertEqual(result.returncode, cli.EXIT_OWNERSHIP_MISMATCH)
        self.assertEqual(json.loads(result.stderr)["error"], "ownership_mismatch")
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)

    def test_startup_decision_is_minimal_and_read_only(self) -> None:
        idle = self._run("startup-decision")
        self.assertEqual(idle.returncode, cli.EXIT_OK)
        self.assertEqual(json.loads(idle.stdout)["decision"], "normal")
        self.assertFalse((self.root / ".run").exists())

    def test_shell_authorization_commands_delegate_restore_decision_to_python(self) -> None:
        self.assertEqual(self._run("startup-allowed").returncode, cli.EXIT_OK)
        self.assertEqual(
            self._run("restore-recovery-needed").returncode,
            cli.EXIT_CONFLICT,
        )
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = claimed.state.active_operation
        self.assertIsNotNone(operation)
        self.assertEqual(
            self._run("restore-recovery-needed").returncode,
            cli.EXIT_OK,
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        self.assertEqual(
            self._run("restore-recovery-needed").returncode,
            cli.EXIT_CONFLICT,
        )
        self.assertEqual(
            self._run("startup-allowed").returncode,
            cli.EXIT_CONFLICT,
        )

    def test_update_recovery_commands_distinguish_resumable_and_terminal_authority(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        self.assertIsNotNone(operation)
        self.assertEqual(self._run("update-recovery-needed").returncode, cli.EXIT_OK)
        self.assertEqual(
            self._run("update-recovery-required").returncode,
            cli.EXIT_CONFLICT,
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        self.assertEqual(
            self._run("update-recovery-needed").returncode,
            cli.EXIT_CONFLICT,
        )
        self.assertEqual(
            self._run("update-recovery-required").returncode,
            cli.EXIT_OK,
        )


if __name__ == "__main__":
    unittest.main()
