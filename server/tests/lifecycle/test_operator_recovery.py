from __future__ import annotations

import ast
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from lifecycle import recovery, restore_controller, state
from scratch import maintenance_server, runtime_control


def create_database(path: Path, marker: str) -> None:
    connection = sqlite3.connect(path)
    try:
        for table in ("user", "movie", "authsession"):
            connection.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO user VALUES ('marker', ?)", (marker,))
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


class RecoveryPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "StreamHome"
        (self.root / "server" / "backup").mkdir(parents=True)
        (self.root / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (self.root / "update.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        self.database = self.root / "server" / "database.db"
        self.backup = self.root / "server" / "backup" / "backup_20260921_120000.db"
        create_database(self.database, "before")
        create_database(self.backup, "candidate")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _restore_recovery(self) -> tuple[str, int, Path]:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = claimed.state.active_operation
        assert operation is not None
        identity = restore_controller.prevalidate_candidate(self.root, self.backup.name)
        restore_controller.create_restore_journal(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            candidate=identity,
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        restore_controller.update_restore_journal(
            self.root,
            operation.operation_id,
            progress="recovery_required",
            error_code="operator_action_required",
        )
        return operation.operation_id, claimed.state.generation, restore_controller.restore_paths(
            self.root, operation.operation_id
        ).journal_path

    def test_exact_recovery_required_operation_and_generation_are_required(self) -> None:
        operation_id, generation, journal_path = self._restore_recovery()
        before_state = state.lifecycle_paths(self.root).state_path.read_bytes()
        before_journal = journal_path.read_bytes()
        with self.assertRaises(recovery.RecoveryPlanningConflict):
            recovery.plan_recovery(
                self.root,
                operation_id="00000000-0000-4000-8000-000000000001",
                generation=generation,
            )
        with self.assertRaises(recovery.RecoveryPlanningConflict):
            recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation + 1,
            )
        recovery.plan_recovery(
            self.root,
            operation_id=operation_id,
            generation=generation,
        )
        self.assertEqual(state.lifecycle_paths(self.root).state_path.read_bytes(), before_state)
        self.assertEqual(journal_path.read_bytes(), before_journal)

    def test_restore_pre_acceptance_reports_only_evidence_supported_actions(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
            )
        self.assertEqual(plan["operationType"], "restore")
        self.assertEqual(plan["lifecyclePhase"], "recovery_required")
        self.assertIn("safe_abort", plan["permittedActions"])
        self.assertIn("continue_recovery", plan["permittedActions"])
        self.assertNotIn("rollback", plan["permittedActions"])
        self.assertTrue(plan["operatorActionRequired"])
        self.assertTrue(plan["readOnly"])

    def test_restore_post_acceptance_never_offers_rollback(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = claimed.state.active_operation
        assert operation is not None
        identity = restore_controller.prevalidate_candidate(self.root, self.backup.name)
        restore_controller.create_restore_journal(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            candidate=identity,
        )
        paths = restore_controller.restore_paths(self.root, operation.operation_id)
        rollback_size, rollback_hash, previous_size, previous_hash = (
            restore_controller.create_rollback_checkpoint(paths)
        )
        journal = restore_controller.update_restore_journal(
            self.root,
            operation.operation_id,
            progress="checkpoint_ready",
            rollback_size=rollback_size,
            rollback_sha256=rollback_hash,
            previous_size=previous_size,
            previous_sha256=previous_hash,
        )
        staged_size, staged_hash = restore_controller.stage_candidate(paths, journal)
        journal = restore_controller.update_restore_journal(
            self.root,
            operation.operation_id,
            progress="candidate_staged",
            staged_size=staged_size,
            staged_sha256=staged_hash,
        )
        restore_controller.activate_staged_candidate(paths, journal)
        restore_controller.update_restore_journal(
            self.root, operation.operation_id, progress="verifying_candidate"
        )
        restore_controller.update_restore_journal(
            self.root,
            operation.operation_id,
            progress="accepted",
            accepted=True,
            outcome="accepted",
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="exclusive",
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="exclusive",
            next_phase="verifying",
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="verifying",
            next_phase="recovery_required",
        )
        restore_controller.update_restore_journal(
            self.root,
            operation.operation_id,
            progress="recovery_required",
            error_code="normal_recovery_failed",
        )
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
            )
        self.assertEqual(plan["accepted"], True)
        self.assertEqual(plan["permittedActions"], ["continue_recovery"])

    def test_corrupt_restore_evidence_offers_no_mutating_action_or_private_data(self) -> None:
        operation_id, generation, journal_path = self._restore_recovery()
        journal_path.write_text("{", encoding="utf-8")
        journal_path.chmod(0o600)
        plan = recovery.plan_recovery(
            self.root,
            operation_id=operation_id,
            generation=generation,
        )
        self.assertEqual(plan["permittedActions"], [])
        self.assertEqual(plan["artifacts"], {"journal": "invalid"})
        encoded = json.dumps(plan)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(self.backup.name, encoded)
        self.assertNotIn("sha256", encoded.lower())

    def test_restore_journal_generation_mismatch_offers_no_action(self) -> None:
        operation_id, generation, journal_path = self._restore_recovery()
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        payload["generation"] = generation + 1
        journal_path.write_text(json.dumps(payload), encoding="utf-8")
        journal_path.chmod(0o600)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
            )
        self.assertEqual(plan["permittedActions"], [])
        self.assertEqual(plan["reasonCode"], "restore_evidence_invalid")

    def test_active_controller_suppresses_all_operator_actions(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_controller_status", return_value="active"):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
            )
        self.assertEqual(plan["permittedActions"], [])

    def test_unknown_controller_or_runtime_evidence_suppresses_all_actions(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_controller_status", return_value="unknown"):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
            )
        self.assertEqual(plan["permittedActions"], [])
        self.assertEqual(plan["controllerStatus"], "unknown")
        with mock.patch.object(recovery, "_runtime_status", return_value="unknown"):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
            )
        self.assertEqual(plan["permittedActions"], [])
        self.assertEqual(plan["runtimeStatus"], "unknown")

    def test_held_controller_lock_without_current_lease_is_unknown(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        lock_path = state.lifecycle_paths(self.root).controller_lock_path
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(
                recovery._controller_status(self.root, operation_id, generation),
                "unknown",
            )
            with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
                plan = recovery.plan_recovery(
                    self.root, operation_id=operation_id, generation=generation
                )
            self.assertEqual(plan["permittedActions"], [])
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_stale_lease_with_held_controller_lock_is_unknown(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        with state.acquire_controller_lease(
            self.root,
            operation_id=operation_id,
            generation=generation,
            expected_phase="recovery_required",
            owner_capability=capability,
        ):
            with mock.patch.object(
                state, "controller_lease_is_current", return_value=False
            ):
                self.assertEqual(
                    recovery._controller_status(self.root, operation_id, generation),
                    "unknown",
                )

    def test_missing_restore_journal_planning_and_classification_create_nothing(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = claimed.state.active_operation
        assert operation is not None
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )

        def snapshot() -> dict[str, bytes | None]:
            return {
                str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob("*")
            }

        before = snapshot()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
            )
        self.assertEqual(plan["permittedActions"], [])
        self.assertEqual(snapshot(), before)
        self.assertFalse(
            restore_controller.restore_paths(self.root, operation.operation_id).operation_dir.exists()
        )

        state.complete_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="recovery_required",
            expected_operation_type="restore",
        )
        before = snapshot()
        classification = recovery.classify_retained_artifacts(
            self.root, operation_id=operation.operation_id
        )
        self.assertEqual(classification["classification"], "ambiguous")
        self.assertEqual(snapshot(), before)

    def test_runtime_status_requires_positive_process_and_port_evidence(self) -> None:
        run_dir = self.root / ".run"
        run_dir.mkdir(mode=0o700, exist_ok=True)
        record_path = run_dir / "backend.runtime.json"
        record_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "root": str(self.root),
                    "service": "backend",
                    "token": "test-token",
                    "pid": 424242,
                    "start_ticks": "100",
                }
            ),
            encoding="utf-8",
        )
        record_path.chmod(0o600)
        owned = SimpleNamespace(start_ticks="100")
        with (
            mock.patch.object(runtime_control, "inspect_process", return_value=owned),
            mock.patch.object(runtime_control, "record_matches", return_value=True),
        ):
            self.assertEqual(recovery._runtime_status(self.root), "active")

        reused = SimpleNamespace(start_ticks="101")
        with (
            mock.patch.object(runtime_control, "inspect_process", return_value=reused),
            mock.patch.object(recovery, "_verified_process_snapshot", return_value={}),
            mock.patch.object(restore_controller, "_web_port", return_value=3000),
            mock.patch.object(runtime_control, "port_is_available", return_value=True),
        ):
            self.assertEqual(recovery._runtime_status(self.root), "offline")

        record_path.unlink()
        with mock.patch.object(recovery, "_verified_process_snapshot", return_value=None):
            self.assertEqual(recovery._runtime_status(self.root), "unknown")

        foreign = SimpleNamespace(pid=999, start_ticks="1")
        with (
            mock.patch.object(
                recovery, "_verified_process_snapshot", return_value={999: foreign}
            ),
            mock.patch.object(runtime_control, "discover_owned", return_value={}),
            mock.patch.object(restore_controller, "_web_port", return_value=3000),
            mock.patch.object(runtime_control, "port_is_available", return_value=False),
        ):
            self.assertEqual(recovery._runtime_status(self.root), "unknown")

        record_path.write_text("{}", encoding="utf-8")
        record_path.chmod(0o600)
        self.assertEqual(recovery._runtime_status(self.root), "unknown")

    def test_unresolved_and_ambiguous_evidence_is_never_cleanup_eligible(self) -> None:
        operation_id, _, journal_path = self._restore_recovery()
        active = recovery.classify_retained_artifacts(
            self.root, operation_id=operation_id
        )
        self.assertEqual(active["classification"], "recovery_required")
        self.assertFalse(active["cleanupEligible"])
        state.lifecycle_paths(self.root).state_path.unlink()
        journal_path.write_text("{", encoding="utf-8")
        ambiguous = recovery.classify_retained_artifacts(
            self.root, operation_id=operation_id
        )
        self.assertEqual(ambiguous["classification"], "ambiguous")
        self.assertFalse(ambiguous["cleanupEligible"])

    def test_proven_terminal_restore_evidence_is_cleanup_eligible_without_deletion(self) -> None:
        operation_id, generation, journal_path = self._restore_recovery()
        authority_path = state.lifecycle_paths(self.root).state_path
        authority_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": generation,
                    "active_operation": None,
                }
            ),
            encoding="utf-8",
        )
        authority_path.chmod(0o600)
        before = journal_path.read_bytes()
        with mock.patch.object(
            restore_controller,
            "inspect_restore_journal",
            return_value={"progress": "succeeded", "generation": generation},
        ):
            classification = recovery.classify_retained_artifacts(
                self.root, operation_id=operation_id
            )
        self.assertEqual(classification["classification"], "resolved_terminal")
        self.assertTrue(classification["cleanupEligible"])
        self.assertEqual(journal_path.read_bytes(), before)
        authority_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": generation + 1,
                    "active_operation": None,
                }
            ),
            encoding="utf-8",
        )
        authority_path.chmod(0o600)
        with mock.patch.object(
            restore_controller,
            "inspect_restore_journal",
            return_value={"progress": "succeeded", "generation": generation},
        ):
            stale = recovery.classify_retained_artifacts(
                self.root, operation_id=operation_id
            )
        self.assertEqual(stale["classification"], "stale_resolved")
        self.assertTrue(stale["cleanupEligible"])

    def test_operator_action_record_schema_is_strict_and_side_effect_free(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        payload = {
            "schema_version": 1,
            "operation_id": operation_id,
            "generation": generation,
            "operation_type": "restore",
            "requested_action": "continue_recovery",
            "created_at": "2026-09-21T12:00:00Z",
            "evidence_identity": "plan-v1:abc123",
            "status": "planned",
            "result": None,
        }
        self.assertEqual(
            recovery.parse_operator_action_record(payload).requested_action,
            "continue_recovery",
        )
        encoded = json.dumps(payload)
        self.assertEqual(
            recovery.parse_operator_action_json(encoded).operation_id,
            operation_id,
        )
        duplicate = encoded[:-1] + ',"schema_version":1}'
        with self.assertRaises(recovery.RecoveryPlanningError):
            recovery.parse_operator_action_json(duplicate)
        with self.assertRaises(recovery.RecoveryPlanningError):
            recovery.parse_operator_action_record({**payload, "capability": "secret"})
        self.assertFalse(any(self.root.rglob("*operator*")))

    def test_maintenance_projection_requires_operator_action_without_auto_retry_claim(self) -> None:
        operation_id, _, _ = self._restore_recovery()
        status = maintenance_server.safe_restore_status(self.root, operation_id)
        self.assertTrue(status["operatorActionRequired"])
        self.assertEqual(status["phase"], "recovery_required")
        self.assertFalse(maintenance_server.recovery_needed(status))
        self.assertNotIn(self.backup.name, json.dumps(status))


class UpdateRecoveryPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "StreamHome"
        repository = Path(__file__).resolve().parents[3]
        (self.root / "server").mkdir(parents=True)
        shutil.copytree(repository / "server" / "lifecycle", self.root / "server" / "lifecycle")
        (self.root / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (self.root / "update.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        self._git("init")
        self._git("config", "user.name", "StreamHome Test")
        self._git("config", "user.email", "streamhome-test@example.invalid")
        self._git("add", ".")
        self._git("commit", "-m", "previous")
        self.previous = self._git("rev-parse", "HEAD")
        (self.root / "target.txt").write_text("target\n", encoding="utf-8")
        self._git("add", "target.txt")
        self._git("commit", "-m", "target")
        self.target = self._git("rev-parse", "HEAD")
        self._git("checkout", "--detach", self.previous)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _claim_update(self, *, committed: bool) -> tuple[str, int, Path]:
        claim = state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claim.state.active_operation
        assert operation is not None
        if committed:
            state.transition_operation_with_credential(
                self.root,
                operation_id=operation.operation_id,
                generation=claim.state.generation,
                expected_phase="reserved",
                next_phase="exclusive",
            )
            state.transition_operation_with_credential(
                self.root,
                operation_id=operation.operation_id,
                generation=claim.state.generation,
                expected_phase="exclusive",
                next_phase="verifying",
            )
            self._git("checkout", "--detach", self.target)
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claim.state.generation,
            expected_phase="verifying" if committed else "reserved",
            next_phase="recovery_required",
        )
        journal = self.root / ".run" / "update-state.json"
        journal.write_text(
            json.dumps(
                {
                    "phase": "starting" if committed else "queued",
                    "transaction_id": operation.operation_id,
                    "lifecycle_generation": claim.state.generation,
                    "lifecycle_phase": "recovery_required",
                    "target_commit": self.target,
                    "previous_commit": self.previous,
                    "runtime_committed": committed,
                    "runtime_committed_at": time.time() if committed else None,
                    "error": "operator_action_required",
                }
            ),
            encoding="utf-8",
        )
        journal.chmod(0o600)
        return operation.operation_id, claim.state.generation, journal

    def test_update_pre_commit_plan_can_offer_abort(self) -> None:
        operation_id, generation, _ = self._claim_update(committed=False)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertIn("safe_abort", plan["permittedActions"])
        self.assertFalse(plan["runtimeCommitted"])

    def test_update_post_commit_plan_never_offers_rollback(self) -> None:
        operation_id, generation, _ = self._claim_update(committed=True)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertTrue(plan["runtimeCommitted"])
        self.assertEqual(plan["permittedActions"], ["continue_recovery"])
        self.assertNotIn("rollback", plan["permittedActions"])

    def test_contradictory_update_evidence_offers_no_action(self) -> None:
        operation_id, generation, journal = self._claim_update(committed=False)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload["lifecycle_generation"] = generation + 1
        journal.write_text(json.dumps(payload), encoding="utf-8")
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertEqual(plan["permittedActions"], [])
        self.assertEqual(plan["reasonCode"], "update_evidence_invalid")

    def test_update_rollback_is_never_offered_without_bound_checkpoint_identity(self) -> None:
        operation_id, generation, journal = self._claim_update(committed=False)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload.update(
            phase="rolling_back",
            failed_target=self.target,
        )
        journal.write_text(json.dumps(payload), encoding="utf-8")
        checkpoint = self.root / ".run" / "pre-update-database.db"
        for evidence in ("missing", "valid_foreign", "stale_foreign"):
            with self.subTest(evidence=evidence):
                checkpoint.unlink(missing_ok=True)
                if evidence != "missing":
                    create_database(checkpoint, evidence)
                with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
                    plan = recovery.plan_recovery(
                        self.root,
                        operation_id=operation_id,
                        generation=generation,
                    )
                self.assertNotIn("rollback", plan["permittedActions"])

    def test_duplicate_and_nonfinite_update_journal_fail_planning_and_retention_closed(self) -> None:
        operation_id, generation, journal = self._claim_update(committed=False)
        original = journal.read_text(encoding="utf-8").rstrip()
        authority_path = state.lifecycle_paths(self.root).state_path
        active_authority = authority_path.read_bytes()
        corruptions = (
            original[:-1] + ',"runtime_committed":true}',
            original[:-1] + f',"lifecycle_generation":{generation}}}',
            original[:-1] + ',"phase":"recovering"}',
            original[:-1] + ',"invalid_number":NaN}',
            original[:-1] + ',"invalid_number":Infinity}',
            original[:-1] + ',"invalid_number":-Infinity}',
        )
        for raw in corruptions:
            with self.subTest(raw=raw[-48:]):
                journal.write_text(raw, encoding="utf-8")
                with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
                    plan = recovery.plan_recovery(
                        self.root, operation_id=operation_id, generation=generation
                    )
                self.assertEqual(plan["permittedActions"], [])
                self.assertEqual(plan["artifacts"], {"journal": "invalid"})

                authority_payload = json.loads(authority_path.read_text(encoding="utf-8"))
                authority_payload["active_operation"] = None
                authority_path.write_text(json.dumps(authority_payload), encoding="utf-8")
                authority_path.chmod(0o600)
                classification = recovery.classify_retained_artifacts(
                    self.root, operation_id=operation_id
                )
                self.assertEqual(classification["classification"], "ambiguous")
                self.assertFalse(classification["cleanupEligible"])
                authority_path.write_bytes(active_authority)
                authority_path.chmod(0o600)


class LegacyRestoreContainmentTests(unittest.TestCase):
    def test_legacy_cli_has_no_live_restore_import_or_call(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        source = (repository / "server" / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("restore_backup", imported)
        self.assertNotIn("restore_backup", calls)
        self.assertIn("Restore is unavailable in this legacy CLI", source)

    def test_backup_service_uses_manifest_pair_for_create_delete_and_prune(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        source = (repository / "server" / "services" / "backup.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        attributes = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "backup_manifest"
        }
        self.assertIn("create_manifest", attributes)
        self.assertIn("delete_backup_pair", attributes)
        self.assertIn("validate_backup_pair", attributes)
        self.assertIn("backup_v1_", source)


if __name__ == "__main__":
    unittest.main()
