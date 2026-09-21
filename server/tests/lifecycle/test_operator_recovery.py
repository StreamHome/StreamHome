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
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from lifecycle import recovery, restore_controller, state, update_controller
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

    def _transferred_restore_action(
        self, *, phase: str, transfer_journal: bool = True,
        status: str = "executing", result: str | None = None,
    ) -> tuple[str, int, int, recovery.OperatorActionRecord]:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        paths = recovery._new_action_paths(self.root, operation_id, generation)
        capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        lease, transferred = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=operation_id,
            generation=generation,
            expected_phase="recovery_required",
            owner_capability=capability,
            next_owner_kind="restore_controller",
        )
        next_generation = transferred.state.generation
        if transfer_journal:
            restore_controller.transfer_restore_journal_generation(
                self.root, operation_id,
                expected_generation=generation,
                next_generation=next_generation,
            )
        record = recovery.OperatorActionRecord(
            1, operation_id, generation, "restore", "continue_recovery",
            "2026-09-21T12:00:00Z", plan["evidenceIdentity"], status,
            next_generation if status == "executing" else None, result,
        )
        recovery._write_action_record(paths.record_path, record)
        if phase in {"exclusive", "verifying"}:
            state.transition_operation_with_credential(
                self.root, operation_id=operation_id, generation=next_generation,
                expected_phase="recovery_required", next_phase="exclusive",
            )
        if phase == "verifying":
            state.transition_operation_with_credential(
                self.root, operation_id=operation_id, generation=next_generation,
                expected_phase="exclusive", next_phase="verifying",
            )
        lease.release()
        return operation_id, generation, next_generation, record

    def test_dead_restore_operator_normalizes_exclusive_and_verifying_only(self) -> None:
        for phase in ("exclusive", "verifying"):
            with self.subTest(phase=phase):
                if phase == "verifying":
                    self.tearDown()
                    self.setUp()
                operation_id, _, generation, _ = self._transferred_restore_action(
                    phase=phase
                )
                self.assertEqual(restore_controller.recover_restore(self.root), 3)
                current = state.inspect_state(self.root)
                self.assertEqual(current.generation, generation)
                self.assertEqual(
                    current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED
                )
                with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
                    plan = recovery.plan_recovery(
                        self.root, operation_id=operation_id, generation=generation
                    )
                self.assertTrue(plan["permittedActions"])
                self.assertIsNotNone(state.inspect_state(self.root).active_operation)

    def test_dead_operator_takeover_rotates_generation_and_capability_again(self) -> None:
        operation_id, _, generation, _ = self._transferred_restore_action(
            phase="exclusive"
        )
        stale_capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertTrue(recovery.normalize_interrupted_operator(
            self.root, operation_id, generation
        ))
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            result = recovery.execute_recovery(
                self.root, operation_id=operation_id, generation=generation,
                action="safe_abort", evidence_identity=plan["evidenceIdentity"],
            )
        self.assertEqual(result["controllerGeneration"], generation + 1)
        self.assertEqual(state.inspect_state(self.root).generation, generation + 1)
        with self.assertRaises(state.LifecycleError):
            state.validate_operation_owner_current_phase(
                self.root, operation_id=operation_id, generation=generation,
                owner_capability=stale_capability,
            )

    def test_live_or_stale_restore_operator_cannot_normalize(self) -> None:
        operation_id, original, generation, record = self._transferred_restore_action(
            phase="exclusive"
        )
        controller_lock = state._open_locked_file(
            state.lifecycle_paths(self.root).controller_lock_path, nonblocking=True
        )
        try:
            self.assertFalse(recovery.normalize_interrupted_operator(
                self.root, operation_id, generation
            ))
        finally:
            fcntl.flock(controller_lock, fcntl.LOCK_UN)
            os.close(controller_lock)
        self.assertFalse(recovery.normalize_interrupted_operator(
            self.root, operation_id, original
        ))
        path = recovery._new_action_paths(self.root, operation_id, original).record_path
        recovery._write_action_record(
            path,
            recovery.OperatorActionRecord(
                record.schema_version, record.operation_id, record.generation,
                record.operation_type, record.requested_action, record.created_at,
                record.evidence_identity, "failed", None, "operator_execution_failed",
            ),
        )
        self.assertFalse(recovery.normalize_interrupted_operator(
            self.root, operation_id, generation
        ))
        self.assertEqual(
            state.inspect_state(self.root).active_operation.phase,
            state.LifecyclePhase.EXCLUSIVE,
        )

    def test_failed_transfer_before_restore_journal_write_is_exactly_bridgeable(self) -> None:
        operation_id, original, generation, _ = self._transferred_restore_action(
            phase="recovery_required", transfer_journal=False,
            status="failed", result="operator_execution_failed",
        )
        self.assertEqual(restore_controller.recover_restore(self.root), 3)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertIn("continue_recovery", plan["permittedActions"])
        self.assertEqual(
            restore_controller.inspect_restore_journal(self.root, operation_id)["generation"],
            original,
        )
        with self.assertRaises(recovery.RecoveryPlanningConflict):
            recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation + 1
            )
        with mock.patch.object(recovery, "_runtime_status", return_value="active"):
            changed = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertEqual(changed["permittedActions"], [])

    def test_failed_transfer_bridge_rejects_changed_or_unrelated_restore_evidence(self) -> None:
        operation_id, original, generation, _ = self._transferred_restore_action(
            phase="recovery_required", transfer_journal=False,
            status="failed", result="operator_execution_failed",
        )
        path = next((
            state.lifecycle_paths(self.root).lifecycle_dir
            / "operator-actions" / operation_id
        ).glob("action.*.json"))
        record = recovery.read_operator_action_record(path)
        recovery._write_action_record(
            path,
            recovery.OperatorActionRecord(
                record.schema_version, operation_id, original, "restore", "rollback",
                record.created_at, record.evidence_identity, "failed", None,
                "operator_execution_failed",
            ),
        )
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            unrelated = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertEqual(unrelated["permittedActions"], [])
        recovery._write_action_record(path, record)
        connection = sqlite3.connect(self.backup)
        try:
            connection.execute("UPDATE user SET value = 'different candidate'")
            connection.commit()
        finally:
            connection.close()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            changed = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertEqual(changed["permittedActions"], [])

    def _restore_ready_for_completion(self) -> tuple[str, int, restore_controller.RestorePaths]:
        claimed = state.claim_operation_with_credential(
            self.root, operation_type="restore", owner_kind="restore_controller"
        )
        operation = claimed.state.active_operation
        assert operation is not None
        identity = restore_controller.prevalidate_candidate(self.root, self.backup.name)
        restore_controller.create_restore_journal(
            self.root, operation_id=operation.operation_id,
            generation=claimed.state.generation, candidate=identity,
        )
        paths = restore_controller.restore_paths(self.root, operation.operation_id)
        rollback_size, rollback_hash, previous_size, previous_hash = (
            restore_controller.create_rollback_checkpoint(paths)
        )
        journal = restore_controller.update_restore_journal(
            self.root, operation.operation_id, progress="checkpoint_ready",
            rollback_size=rollback_size, rollback_sha256=rollback_hash,
            previous_size=previous_size, previous_sha256=previous_hash,
        )
        staged_size, staged_hash = restore_controller.stage_candidate(paths, journal)
        journal = restore_controller.update_restore_journal(
            self.root, operation.operation_id, progress="candidate_staged",
            staged_size=staged_size, staged_sha256=staged_hash,
        )
        restore_controller.activate_staged_candidate(paths, journal)
        restore_controller.update_restore_journal(
            self.root, operation.operation_id, progress="verifying_candidate"
        )
        restore_controller.update_restore_journal(
            self.root, operation.operation_id, progress="accepted",
            accepted=True, outcome="accepted",
        )
        state.transition_operation_with_credential(
            self.root, operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved", next_phase="exclusive",
        )
        state.transition_operation_with_credential(
            self.root, operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="exclusive", next_phase="verifying",
        )
        restore_controller.update_restore_journal(
            self.root, operation.operation_id, progress="normal_recovery"
        )
        return operation.operation_id, claimed.state.generation, paths

    def test_restore_completion_intent_reconciles_only_after_exact_completion(self) -> None:
        operation_id, generation, paths = self._restore_ready_for_completion()
        self.assertFalse(restore_controller.reconcile_restore_completion_intent(
            self.root, operation_id, generation
        ))
        restore_controller.record_restore_completion_intent(
            paths, generation=generation, outcome="succeeded"
        )
        self.assertFalse(restore_controller.reconcile_restore_completion_intent(
            self.root, operation_id, generation
        ))
        state.complete_operation_with_credential(
            self.root, operation_id=operation_id, generation=generation,
            expected_phase="verifying", expected_operation_type="restore",
        )
        self.assertEqual(
            restore_controller.inspect_restore_journal(self.root, operation_id)["progress"],
            "normal_recovery",
        )
        newer = state.claim_operation_with_credential(
            self.root, operation_type="update", owner_kind="updater"
        )
        state.complete_operation_with_credential(
            self.root,
            operation_id=newer.state.active_operation.operation_id,
            generation=newer.state.generation,
            expected_phase="reserved", expected_operation_type="update",
        )
        self.assertTrue(restore_controller.reconcile_restore_completion_intent(
            self.root, operation_id, generation
        ))
        self.assertEqual(
            restore_controller.inspect_restore_journal(self.root, operation_id)["progress"],
            "succeeded",
        )

    def test_restore_completion_final_write_failure_is_retryable_and_stale_intent_refused(self) -> None:
        operation_id, generation, paths = self._restore_ready_for_completion()
        restore_controller.record_restore_completion_intent(
            paths, generation=generation, outcome="succeeded"
        )
        state.complete_operation_with_credential(
            self.root, operation_id=operation_id, generation=generation,
            expected_phase="verifying", expected_operation_type="restore",
        )
        with mock.patch.object(
            restore_controller, "update_restore_journal",
            side_effect=restore_controller.RestorePersistenceError("injected"),
        ):
            self.assertFalse(restore_controller.reconcile_restore_completion_intent(
                self.root, operation_id, generation
            ))
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(
            restore_controller.inspect_restore_journal(self.root, operation_id)["progress"],
            "normal_recovery",
        )
        marker = paths.operation_dir / "completion-intent.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["generation"] = generation + 1
        marker.write_text(json.dumps(payload), encoding="utf-8")
        self.assertFalse(restore_controller.reconcile_restore_completion_intent(
            self.root, operation_id, generation
        ))
        payload["generation"] = generation
        marker.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(restore_controller.reconcile_restore_completion_intent(
            self.root, operation_id, generation
        ))

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
        forged = recovery._new_action_paths(
            self.root, operation.operation_id, claimed.state.generation
        )
        recovery._write_action_record(
            forged.record_path,
            recovery.OperatorActionRecord(
                1, operation.operation_id, claimed.state.generation,
                "restore", "rollback", "2026-09-21T12:00:00Z",
                plan["evidenceIdentity"], "planned", None, None,
            ),
        )
        with self.assertRaises(recovery.RecoveryEvidenceStale):
            recovery.execute_recovery(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
                action="rollback",
                evidence_identity=plan["evidenceIdentity"],
            )

    def test_execute_accepted_restore_continue_uses_canonical_controller(self) -> None:
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
        runtime = SimpleNamespace(
            terminate_owned_service=lambda *_args: True,
            port_is_available=lambda *_args: True,
        )
        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            mock.patch.object(restore_controller, "_web_port", return_value=3000),
            mock.patch.object(restore_controller, "_stop_supported_runtime"),
            mock.patch.object(restore_controller, "_start_maintenance", return_value=object()),
            mock.patch.object(restore_controller, "_start_backend"),
            mock.patch.object(restore_controller, "_wait_guarded_ready"),
            mock.patch.object(restore_controller, "_commit_guarded_runtime"),
            mock.patch.object(restore_controller, "_wait_general_ready"),
            mock.patch.object(restore_controller, "_start_web"),
            mock.patch.object(restore_controller, "_request_json", return_value=(503, {})),
            mock.patch.object(restore_controller, "_build_id", return_value="test-build"),
            mock.patch.object(restore_controller, "_runtime_control", return_value=runtime),
        ):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
            )
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
                action="continue_recovery",
                evidence_identity=plan["evidenceIdentity"],
            )
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        terminal = restore_controller.inspect_restore_journal(
            self.root, operation.operation_id
        )
        self.assertEqual(terminal["progress"], "succeeded")
        self.assertEqual(result["resultCode"], "operator_recovery_completed")

    def test_execute_preacceptance_restore_rollback_uses_canonical_controller(self) -> None:
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
        restore_controller.update_restore_journal(
            self.root,
            operation.operation_id,
            progress="checkpoint_ready",
            rollback_size=rollback_size,
            rollback_sha256=rollback_hash,
            previous_size=previous_size,
            previous_sha256=previous_hash,
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
            error_code="restore_controller_failed",
        )
        runtime = SimpleNamespace(terminate_owned_service=lambda *_args: True)
        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            mock.patch.object(restore_controller, "_web_port", return_value=3000),
            mock.patch.object(restore_controller, "_stop_supported_runtime"),
            mock.patch.object(restore_controller, "_start_maintenance"),
            mock.patch.object(restore_controller, "_start_backend"),
            mock.patch.object(restore_controller, "_wait_guarded_ready"),
            mock.patch.object(restore_controller, "_commit_guarded_runtime"),
            mock.patch.object(restore_controller, "_wait_general_ready"),
            mock.patch.object(restore_controller, "_start_web"),
            mock.patch.object(restore_controller, "_build_id", return_value="test-build"),
            mock.patch.object(restore_controller, "_runtime_control", return_value=runtime),
        ):
            plan = recovery.plan_recovery(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
            )
            self.assertIn("rollback", plan["permittedActions"])
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
                action="rollback",
                evidence_identity=plan["evidenceIdentity"],
            )
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        terminal = restore_controller.inspect_restore_journal(
            self.root, operation.operation_id
        )
        self.assertEqual(terminal["progress"], "rolled_back")
        self.assertEqual(result["resultCode"], "operator_recovery_completed")

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

    def test_controller_lock_contention_rejects_stale_plan_without_transfer(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
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
            with (
                mock.patch.object(recovery, "_runtime_status", return_value="offline"),
                self.assertRaises(recovery.RecoveryEvidenceStale),
            ):
                recovery.execute_recovery(
                    self.root,
                    operation_id=operation_id,
                    generation=generation,
                    action="safe_abort",
                    evidence_identity=plan["evidenceIdentity"],
                )
        current = state.inspect_state(self.root)
        self.assertEqual(current.generation, generation)
        self.assertEqual(
            current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED
        )

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
            "controller_generation": None,
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
        with self.assertRaises(recovery.RecoveryPlanningError):
            recovery.parse_operator_action_record({**payload, "schema_version": 2})
        with self.assertRaises(recovery.RecoveryPlanningError):
            recovery.parse_operator_action_record(
                {**payload, "status": "failed", "result": None}
            )
        self.assertFalse(any(self.root.rglob("*operator*")))

    def test_execute_restore_safe_abort_rotates_owner_and_cleans_exact_reservation(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        old_capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
                action="safe_abort",
                evidence_identity=plan["evidenceIdentity"],
            )
        authority = state.inspect_state(self.root)
        self.assertIsNone(authority.active_operation)
        self.assertEqual(authority.generation, generation + 1)
        self.assertEqual(result["controllerGeneration"], generation + 1)
        self.assertEqual(result["resultCode"], "operator_safe_abort_completed")
        self.assertTrue(result["cleanupCompleted"])
        self.assertFalse(
            restore_controller.restore_paths(self.root, operation_id).operation_dir.exists()
        )
        with self.assertRaises(state.LifecycleError):
            state.validate_operation_owner_current_phase(
                self.root,
                operation_id=operation_id,
                generation=generation,
                owner_capability=old_capability,
            )
        records = tuple(
            (
                state.lifecycle_paths(self.root).lifecycle_dir
                / "operator-actions"
                / operation_id
            ).glob("action.*.json")
        )
        self.assertEqual(len(records), 1)
        record = recovery.read_operator_action_record(records[0])
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.controller_generation, generation + 1)
        raw_record = records[0].read_text(encoding="utf-8")
        self.assertNotIn(old_capability, raw_record)
        self.assertNotIn(str(self.root), raw_record)
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(records[0].parent.stat().st_mode & 0o777, 0o700)

    def test_execute_rejects_stale_evidence_without_persisting_intent(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        restore_controller.update_restore_journal(
            self.root,
            operation_id,
            progress="recovery_required",
            error_code="normal_recovery_failed",
        )
        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            self.assertRaises(recovery.RecoveryEvidenceStale),
        ):
            recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
                action="safe_abort",
                evidence_identity=plan["evidenceIdentity"],
            )
        self.assertFalse(
            (
                state.lifecycle_paths(self.root).lifecycle_dir
                / "operator-actions"
                / operation_id
            ).exists()
        )

    def test_execute_rejects_changed_exact_artifact_identity_even_when_status_stays_valid(self) -> None:
        operation_id, generation, journal_path = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        connection = sqlite3.connect(self.backup)
        try:
            connection.execute(
                "UPDATE user SET value='different-candidate' WHERE id='marker'"
            )
            connection.commit()
        finally:
            connection.close()
        candidate_size, candidate_hash = restore_controller.artifact_identity(
            self.backup
        )
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        payload["candidate_size"] = candidate_size
        payload["candidate_sha256"] = candidate_hash
        payload["updated_at"] = time.time()
        journal_path.write_text(json.dumps(payload), encoding="utf-8")
        journal_path.chmod(0o600)
        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            self.assertRaises(recovery.RecoveryEvidenceStale),
        ):
            recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
                action="safe_abort",
                evidence_identity=plan["evidenceIdentity"],
            )

    def test_cleanup_failure_does_not_resurrect_authority_and_can_be_retried(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            mock.patch.object(recovery, "_cleanup_after_resolution", return_value=False),
        ):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
                action="safe_abort",
                evidence_identity=plan["evidenceIdentity"],
            )
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertFalse(result["cleanupCompleted"])
        self.assertEqual(result["resultCode"], "operator_cleanup_pending")
        retried = recovery.cleanup_resolved_recovery(
            self.root,
            operation_id=operation_id,
            generation=generation + 1,
        )
        self.assertTrue(retried["cleanupCompleted"])
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_cleanup_pending_refuses_newer_active_lifecycle_operation(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            mock.patch.object(recovery, "_cleanup_after_resolution", return_value=False),
        ):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            recovery.execute_recovery(
                self.root, operation_id=operation_id, generation=generation,
                action="safe_abort", evidence_identity=plan["evidenceIdentity"],
            )
        newer = state.claim_operation_with_credential(
            self.root, operation_type="update", owner_kind="updater"
        )
        with self.assertRaises(recovery.RecoveryPlanningConflict):
            recovery.cleanup_resolved_recovery(
                self.root, operation_id=operation_id, generation=generation + 1
            )
        self.assertEqual(
            state.inspect_state(self.root).active_operation.operation_type,
            state.OperationType.UPDATE,
        )
        state.complete_operation_with_credential(
            self.root,
            operation_id=newer.state.active_operation.operation_id,
            generation=newer.state.generation,
            expected_phase="reserved", expected_operation_type="update",
        )
        retried = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation + 1
        )
        self.assertTrue(retried["cleanupCompleted"])

    def test_duplicate_simultaneous_execute_has_one_winner(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            barrier = threading.Barrier(3)
            outcomes: list[str] = []

            def execute() -> None:
                barrier.wait()
                try:
                    recovery.execute_recovery(
                        self.root, operation_id=operation_id, generation=generation,
                        action="safe_abort", evidence_identity=plan["evidenceIdentity"],
                    )
                    outcomes.append("success")
                except recovery.RecoveryPlanningError:
                    outcomes.append("rejected")

            threads = [threading.Thread(target=execute) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["success", "rejected"])

    def test_old_action_replay_against_newer_generation_is_rejected(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            recovery.execute_recovery(
                self.root, operation_id=operation_id, generation=generation,
                action="safe_abort", evidence_identity=plan["evidenceIdentity"],
            )
        newer = state.claim_operation_with_credential(
            self.root, operation_type="update", owner_kind="updater"
        )
        with self.assertRaises(recovery.RecoveryPlanningConflict):
            recovery.execute_recovery(
                self.root, operation_id=operation_id, generation=generation,
                action="safe_abort", evidence_identity=plan["evidenceIdentity"],
            )
        self.assertEqual(state.inspect_state(self.root).generation, newer.state.generation)

    def test_crash_after_cleanup_reconciles_audit_without_resurrecting_authority(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        replace_record = recovery._replace_action_record

        def fail_final_audit(*args, **kwargs):
            if kwargs.get("result") == "operator_safe_abort_completed":
                raise recovery.RecoveryExecutionError("injected final audit failure")
            return replace_record(*args, **kwargs)

        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            mock.patch.object(
                recovery, "_replace_action_record", side_effect=fail_final_audit
            ),
        ):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            with self.assertRaises(recovery.RecoveryExecutionError):
                recovery.execute_recovery(
                    self.root,
                    operation_id=operation_id,
                    generation=generation,
                    action="safe_abort",
                    evidence_identity=plan["evidenceIdentity"],
                )
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertFalse(
            restore_controller.restore_paths(
                self.root, operation_id
            ).operation_dir.exists()
        )
        reconciled = recovery.cleanup_resolved_recovery(
            self.root,
            operation_id=operation_id,
            generation=generation + 1,
        )
        self.assertTrue(reconciled["cleanupCompleted"])
        self.assertEqual(
            reconciled["resultCode"], "operator_safe_abort_completed"
        )

    def test_operator_dispatch_failure_retains_recovery_required_and_never_auto_retries(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            with (
                mock.patch.object(
                    restore_controller,
                    "resume_restore_journal_for_operator",
                    side_effect=restore_controller.RestoreConflictError(
                        "injected dispatch failure"
                    ),
                ),
                self.assertRaises(recovery.RecoveryExecutionError),
            ):
                recovery.execute_recovery(
                    self.root,
                    operation_id=operation_id,
                    generation=generation,
                    action="safe_abort",
                    evidence_identity=plan["evidenceIdentity"],
                )
        current = state.inspect_state(self.root)
        self.assertEqual(current.generation, generation + 1)
        self.assertEqual(
            current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED
        )
        self.assertEqual(restore_controller.recover_restore(self.root), 3)
        records = tuple(
            (
                state.lifecycle_paths(self.root).lifecycle_dir
                / "operator-actions"
                / operation_id
            ).glob("action.*.json")
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(
            recovery.read_operator_action_record(records[0]).status, "failed"
        )

    def test_interrupted_operator_generation_blocks_automatic_restore_recovery(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        state.qualify_lifecycle_filesystem(self.root)
        paths = recovery._new_action_paths(self.root, operation_id, generation)
        record = recovery.OperatorActionRecord(
            1,
            operation_id,
            generation,
            "restore",
            "continue_recovery",
            "2026-09-21T12:00:00Z",
            plan["evidenceIdentity"],
            "planned",
            None,
            None,
        )
        recovery._write_action_record(paths.record_path, record)
        capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        lease, transferred = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=operation_id,
            generation=generation,
            expected_phase="recovery_required",
            owner_capability=capability,
            next_owner_kind="restore_controller",
        )
        lease.release()
        next_generation = transferred.state.generation
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation_id,
            generation=next_generation,
            expected_phase="recovery_required",
            next_phase="exclusive",
        )
        self.assertTrue(
            recovery.operator_action_blocks_automatic_recovery(
                self.root, operation_id, next_generation
            )
        )
        self.assertEqual(restore_controller.recover_restore(self.root), 3)

    def test_takeover_reconciles_transfer_before_restore_journal_and_rotates_again(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            first_plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        state.qualify_lifecycle_filesystem(self.root)
        paths = recovery._new_action_paths(self.root, operation_id, generation)
        recovery._write_action_record(
            paths.record_path,
            recovery.OperatorActionRecord(
                1,
                operation_id,
                generation,
                "restore",
                "safe_abort",
                "2026-09-21T12:00:00Z",
                first_plan["evidenceIdentity"],
                "planned",
                None,
                None,
            ),
        )
        capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        lease, transferred = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=operation_id,
            generation=generation,
            expected_phase="recovery_required",
            owner_capability=capability,
            next_owner_kind="restore_controller",
        )
        lease.release()
        interrupted_generation = transferred.state.generation
        journal = restore_controller.inspect_restore_journal(self.root, operation_id)
        self.assertEqual(journal["generation"], generation)

        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            second_plan = recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=interrupted_generation,
            )
            self.assertEqual(second_plan["permittedActions"], ["safe_abort"])
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=interrupted_generation,
                action="safe_abort",
                evidence_identity=second_plan["evidenceIdentity"],
            )
        self.assertEqual(
            result["controllerGeneration"], interrupted_generation + 1
        )
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_takeover_after_journal_transfer_before_action_record_rotates_again(self) -> None:
        operation_id, generation, _ = self._restore_recovery()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            first_plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        state.qualify_lifecycle_filesystem(self.root)
        paths = recovery._new_action_paths(self.root, operation_id, generation)
        recovery._write_action_record(
            paths.record_path,
            recovery.OperatorActionRecord(
                1,
                operation_id,
                generation,
                "restore",
                "safe_abort",
                "2026-09-21T12:00:00Z",
                first_plan["evidenceIdentity"],
                "planned",
                None,
                None,
            ),
        )
        capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        lease, transferred = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=operation_id,
            generation=generation,
            expected_phase="recovery_required",
            owner_capability=capability,
            next_owner_kind="restore_controller",
        )
        interrupted_generation = transferred.state.generation
        restore_controller.transfer_restore_journal_generation(
            self.root,
            operation_id,
            expected_generation=generation,
            next_generation=interrupted_generation,
        )
        lease.release()
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            second_plan = recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=interrupted_generation,
            )
            self.assertEqual(second_plan["permittedActions"], ["safe_abort"])
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=interrupted_generation,
                action="safe_abort",
                evidence_identity=second_plan["evidenceIdentity"],
            )
        self.assertEqual(result["controllerGeneration"], interrupted_generation + 1)
        self.assertIsNone(state.inspect_state(self.root).active_operation)

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

    def _transferred_update_action(
        self, *, phase: str, transfer_journal: bool = True,
        status: str = "executing", result: str | None = None,
    ) -> tuple[str, int, int, Path]:
        operation_id, generation, journal_path = self._claim_update(committed=False)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        paths = recovery._new_action_paths(self.root, operation_id, generation)
        capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        lease, transferred = state.acquire_transferred_controller_lease(
            self.root, operation_id=operation_id, generation=generation,
            expected_phase="recovery_required", owner_capability=capability,
            next_owner_kind="updater",
        )
        next_generation = transferred.state.generation
        if transfer_journal:
            update_controller._record_transferred_generation(
                self.root, operation_id=operation_id,
                previous_generation=generation, generation=next_generation,
                lifecycle_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
                target_commit=self.target, previous_commit=self.previous,
            )
        recovery._write_action_record(
            paths.record_path,
            recovery.OperatorActionRecord(
                1, operation_id, generation, "update", "safe_abort",
                "2026-09-21T12:00:00Z", plan["evidenceIdentity"], status,
                next_generation if status == "executing" else None, result,
            ),
        )
        if phase in {"exclusive", "verifying"}:
            state.transition_operation_with_credential(
                self.root, operation_id=operation_id, generation=next_generation,
                expected_phase="recovery_required", next_phase="exclusive",
            )
        if phase == "verifying":
            state.transition_operation_with_credential(
                self.root, operation_id=operation_id, generation=next_generation,
                expected_phase="exclusive", next_phase="verifying",
            )
        if phase in {"exclusive", "verifying"}:
            payload = update_controller.read_update_journal(journal_path)
            payload["phase"] = "starting"
            payload["lifecycle_phase"] = phase
            journal_path.write_text(json.dumps(payload), encoding="utf-8")
            journal_path.chmod(0o600)
            self._git("checkout", "--detach", self.target)
        lease.release()
        return operation_id, generation, next_generation, journal_path

    def test_dead_update_operator_normalizes_exclusive(self) -> None:
        operation_id, _, generation, journal = self._transferred_update_action(
            phase="exclusive"
        )
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._recover_identity(self.root)
        current = state.inspect_state(self.root)
        self.assertEqual(current.generation, generation)
        self.assertEqual(current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(
            update_controller.read_update_journal(journal)["lifecycle_phase"],
            "recovery_required",
        )
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertIn("continue_recovery", plan["permittedActions"])

    def test_dead_update_operator_normalizes_verifying(self) -> None:
        operation_id, _, generation, _ = self._transferred_update_action(
            phase="verifying"
        )
        self.assertTrue(recovery.normalize_interrupted_operator(
            self.root, operation_id, generation
        ))
        self.assertEqual(
            state.inspect_state(self.root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertIn("continue_recovery", plan["permittedActions"])

    def test_failed_transfer_before_update_journal_write_is_exactly_bridgeable(self) -> None:
        operation_id, original, generation, journal = self._transferred_update_action(
            phase="recovery_required", transfer_journal=False,
            status="failed", result="operator_execution_failed",
        )
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._recover_identity(self.root)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertIn("safe_abort", plan["permittedActions"])
        self.assertEqual(
            update_controller.read_update_journal(journal)["lifecycle_generation"],
            original,
        )
        with self.assertRaises(recovery.RecoveryPlanningConflict):
            recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation + 1
            )

    def test_update_pre_commit_plan_can_offer_abort(self) -> None:
        operation_id, generation, _ = self._claim_update(committed=False)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertIn("safe_abort", plan["permittedActions"])
        self.assertFalse(plan["runtimeCommitted"])

    def test_update_safe_abort_requires_positive_runtime_offline_proof(self) -> None:
        operation_id, generation, _ = self._claim_update(committed=False)
        with mock.patch.object(recovery, "_runtime_status", return_value="active"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertNotIn("safe_abort", plan["permittedActions"])

    def test_execute_update_safe_abort_rotates_owner_and_preserves_audit(self) -> None:
        operation_id, generation, journal = self._claim_update(committed=False)
        old_capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
                action="safe_abort",
                evidence_identity=plan["evidenceIdentity"],
            )
        authority = state.inspect_state(self.root)
        self.assertIsNone(authority.active_operation)
        self.assertEqual(authority.generation, generation + 1)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(payload["phase"], "failed")
        self.assertFalse(payload["runtime_committed"])
        self.assertEqual(payload["lifecycle_generation"], generation + 1)
        self.assertTrue(result["cleanupCompleted"])
        with self.assertRaises(state.LifecycleError):
            state.validate_operation_owner_current_phase(
                self.root,
                operation_id=operation_id,
                generation=generation,
                owner_capability=old_capability,
            )

    def test_execute_postcommit_update_continue_uses_canonical_controller(self) -> None:
        operation_id, generation, journal = self._claim_update(committed=True)

        class CompletedUpdateProcess:
            def __init__(process_self) -> None:
                process_self.returncode: int | None = None

            def wait(process_self, timeout: float | None = None) -> int:
                del timeout
                current = state.inspect_state(self.root)
                assert current.active_operation is not None
                if current.active_operation.phase is state.LifecyclePhase.EXCLUSIVE:
                    state.transition_operation_with_credential(
                        self.root,
                        operation_id=operation_id,
                        generation=current.generation,
                        expected_phase="exclusive",
                        next_phase="verifying",
                    )
                update_controller.mutate_update_journal(
                    self.root,
                    operation_id=operation_id,
                    generation=current.generation,
                    expected_lifecycle_phase="verifying",
                    expected_detail_phases=frozenset({"recovering"}),
                    changes={"phase": "succeeded", "error": ""},
                )
                state.complete_operation_with_credential(
                    self.root,
                    operation_id=operation_id,
                    generation=current.generation,
                    expected_phase="verifying",
                    expected_operation_type="update",
                )
                process_self.returncode = 0
                return 0

            def poll(process_self) -> int | None:
                return process_self.returncode

            def terminate(process_self) -> None:
                process_self.returncode = 1

            def kill(process_self) -> None:
                process_self.returncode = 1

        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            mock.patch.object(recovery, "_git_commit_exists", return_value=True),
            mock.patch.object(recovery, "_installed_commit", return_value=self.target),
            mock.patch.object(
                recovery, "_durable_recovery_evidence", return_value="exact-evidence"
            ),
            mock.patch.object(state, "qualify_lifecycle_filesystem"),
            mock.patch.object(state, "qualify_local_filesystem"),
            mock.patch.object(
                update_controller.subprocess,
                "Popen",
                side_effect=lambda *_args, **_kwargs: CompletedUpdateProcess(),
            ),
        ):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            self.assertEqual(plan["permittedActions"], ["continue_recovery"])
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=generation,
                action="continue_recovery",
                evidence_identity=plan["evidenceIdentity"],
            )
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        terminal = update_controller.read_update_journal(journal)
        self.assertEqual(terminal["phase"], "succeeded")
        self.assertTrue(terminal["runtime_committed"])
        self.assertEqual(result["resultCode"], "operator_recovery_completed")

    def _execute_update_with_shell_exit(
        self, *, return_code: int, final_phase: str = "succeeded",
        complete_authority: bool = True, wrong_transaction: bool = False,
        installed_commit: str | None = None,
    ) -> tuple[bool, str, int, Path]:
        operation_id, generation, journal = self._claim_update(committed=True)

        class CompletedUpdateProcess:
            def __init__(process_self) -> None:
                process_self.returncode: int | None = None

            def wait(process_self, timeout: float | None = None) -> int:
                del timeout
                current = state.inspect_state(self.root)
                assert current.active_operation is not None
                if final_phase == "succeeded":
                    update_controller.mutate_update_journal(
                        self.root, operation_id=operation_id,
                        generation=current.generation,
                        expected_lifecycle_phase="verifying",
                        expected_detail_phases=frozenset({"recovering"}),
                        changes={"phase": "succeeded", "error": ""},
                    )
                if complete_authority:
                    state.complete_operation_with_credential(
                        self.root, operation_id=operation_id,
                        generation=current.generation,
                        expected_phase="verifying", expected_operation_type="update",
                    )
                if wrong_transaction:
                    payload = update_controller.read_update_journal(journal)
                    payload["transaction_id"] = "00000000-0000-4000-8000-000000000001"
                    journal.write_text(json.dumps(payload), encoding="utf-8")
                process_self.returncode = return_code
                return return_code

            def poll(process_self) -> int | None:
                return process_self.returncode

            def terminate(process_self) -> None:
                process_self.returncode = 1

            def kill(process_self) -> None:
                process_self.returncode = 1

        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            mock.patch.object(recovery, "_git_commit_exists", return_value=True),
            mock.patch.object(
                recovery, "_installed_commit",
                return_value=installed_commit or self.target,
            ),
            mock.patch.object(recovery, "_durable_recovery_evidence", return_value="exact-evidence"),
            mock.patch.object(state, "qualify_lifecycle_filesystem"),
            mock.patch.object(state, "qualify_local_filesystem"),
            mock.patch.object(
                update_controller.subprocess, "Popen",
                side_effect=lambda *_args, **_kwargs: CompletedUpdateProcess(),
            ),
        ):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            try:
                recovery.execute_recovery(
                    self.root, operation_id=operation_id, generation=generation,
                    action="continue_recovery",
                    evidence_identity=plan["evidenceIdentity"],
                )
                succeeded = True
            except recovery.RecoveryExecutionError:
                succeeded = False
        return succeeded, operation_id, generation, journal

    def test_update_exit_twelve_requires_verified_success_postconditions(self) -> None:
        cases = (
            (12, "succeeded", True, False, None, True),
            (12, "succeeded", False, False, None, False),
            (12, "recovering", True, False, None, False),
            (12, "succeeded", True, True, None, False),
            (12, "succeeded", True, False, self.previous, False),
            (0, "succeeded", True, False, None, True),
            (7, "succeeded", True, False, None, False),
        )
        for index, (code, phase, complete, wrong_transaction, installed, expected) in enumerate(cases):
            with self.subTest(code=code, phase=phase, complete=complete,
                              wrong_transaction=wrong_transaction):
                if index:
                    self.tearDown()
                    self.setUp()
                succeeded, _, _, _ = self._execute_update_with_shell_exit(
                    return_code=code, final_phase=phase,
                    complete_authority=complete,
                    wrong_transaction=wrong_transaction,
                    installed_commit=installed,
                )
                self.assertEqual(succeeded, expected)

    def test_update_cleanup_requalifies_staging_and_retries_without_reopening_authority(self) -> None:
        operation_id, _, generation, journal = self._transferred_update_action(
            phase="recovery_required"
        )
        staging = self.root.parent / ".streamhome-update.operator-test"
        staging.mkdir(mode=0o700)
        (staging / "retained.txt").write_text("retain until qualified", encoding="utf-8")
        payload = update_controller.read_update_journal(journal)
        payload["phase"] = "failed"
        payload["staging_dir"] = str(staging)
        journal.write_text(json.dumps(payload), encoding="utf-8")
        state.complete_operation_with_credential(
            self.root, operation_id=operation_id, generation=generation,
            expected_phase="recovery_required", expected_operation_type="update",
        )
        paths = state.lifecycle_paths(self.root).lifecycle_dir / "operator-actions" / operation_id
        record_path = next(paths.glob("action.*.json"))
        record = recovery.read_operator_action_record(record_path)
        recovery._replace_action_record(
            record_path, record, status="succeeded",
            controller_generation=generation, result="operator_cleanup_pending",
        )
        original_qualification = state.qualify_local_filesystem
        checked: list[Path] = []

        def unsupported_staging(path: Path, **kwargs: object) -> object:
            checked.append(Path(path))
            if Path(path) == staging:
                raise state.UnsupportedFilesystemError("injected changed mount")
            return original_qualification(path, **kwargs)

        with mock.patch.object(
            state, "qualify_local_filesystem", side_effect=unsupported_staging
        ):
            pending = recovery.cleanup_resolved_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertIn(staging, checked)
        self.assertFalse(pending["cleanupCompleted"])
        self.assertTrue(staging.exists())
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        retained = self.root.parent / ".streamhome-update.retained"
        staging.rename(retained)
        outside = self.root.parent / "unrelated"
        outside.mkdir()
        (outside / "must-remain.txt").write_text("safe", encoding="utf-8")
        staging.symlink_to(outside, target_is_directory=True)
        blocked = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertFalse(blocked["cleanupCompleted"])
        self.assertTrue((outside / "must-remain.txt").exists())
        staging.unlink()
        retained.rename(staging)
        completed = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertTrue(completed["cleanupCompleted"])
        self.assertFalse(staging.exists())
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def _pending_update_cleanup(
        self, staging: Path
    ) -> tuple[str, int, Path, Path, Path]:
        operation_id, _, generation, journal = self._transferred_update_action(
            phase="recovery_required"
        )
        payload = update_controller.read_update_journal(journal)
        payload["phase"] = "failed"
        payload["staging_dir"] = str(staging)
        journal.write_text(json.dumps(payload), encoding="utf-8")
        journal.chmod(0o600)
        state.complete_operation_with_credential(
            self.root, operation_id=operation_id, generation=generation,
            expected_phase="recovery_required", expected_operation_type="update",
        )
        action_dir = (
            state.lifecycle_paths(self.root).lifecycle_dir
            / "operator-actions" / operation_id
        )
        record_path = next(action_dir.glob("action.*.json"))
        record = recovery.read_operator_action_record(record_path)
        recovery._replace_action_record(
            record_path, record, status="succeeded",
            controller_generation=generation, result="operator_cleanup_pending",
        )
        _, _, permit = update_controller._update_paths(
            self.root, operation_id, generation
        )
        controller = self.root / ".run" / f"update-controller.{operation_id}.sh"
        for path in (permit, controller):
            path.write_text("retained until cleanup", encoding="utf-8")
            path.chmod(0o600)
        return operation_id, generation, permit, controller, record_path

    def test_update_cleanup_staging_symlink_rejects_without_any_deletion(self) -> None:
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "keep").write_text("safe", encoding="utf-8")
        staging = self.root.parent / ".streamhome-update.symlink"
        staging.symlink_to(outside, target_is_directory=True)
        operation_id, generation, permit, controller, action_record = (
            self._pending_update_cleanup(staging)
        )
        result = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertFalse(result["cleanupCompleted"])
        self.assertTrue((outside / "keep").exists())
        self.assertTrue(staging.is_symlink())
        self.assertTrue(permit.exists())
        self.assertTrue(controller.exists())
        self.assertEqual(
            recovery.read_operator_action_record(action_record).result,
            "operator_cleanup_pending",
        )
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_update_cleanup_outside_staging_rejects_without_any_deletion(self) -> None:
        staging = self.root.parent / "not-an-owned-update-staging"
        staging.mkdir()
        (staging / "keep").write_text("safe", encoding="utf-8")
        operation_id, generation, permit, controller, action_record = (
            self._pending_update_cleanup(staging)
        )
        result = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertFalse(result["cleanupCompleted"])
        self.assertTrue((staging / "keep").exists())
        self.assertTrue(permit.exists())
        self.assertTrue(controller.exists())
        self.assertTrue(action_record.exists())

    def test_update_cleanup_invalid_staging_metadata_deletes_nothing(self) -> None:
        staging = self.root.parent / ".streamhome-update.invalid-metadata"
        staging.mkdir()
        operation_id, generation, permit, controller, action_record = (
            self._pending_update_cleanup(staging)
        )
        journal = self.root / ".run" / "update-state.json"
        payload = update_controller.read_update_journal(journal)
        payload["staging_dir"] = False
        journal.write_text(json.dumps(payload), encoding="utf-8")
        result = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertFalse(result["cleanupCompleted"])
        self.assertTrue(staging.exists())
        self.assertTrue(permit.exists())
        self.assertTrue(controller.exists())
        self.assertTrue(action_record.exists())

    def test_update_cleanup_qualification_failure_deletes_nothing(self) -> None:
        staging = self.root.parent / ".streamhome-update.unqualified"
        staging.mkdir()
        operation_id, generation, permit, controller, action_record = (
            self._pending_update_cleanup(staging)
        )
        original = state.qualify_local_filesystem

        def reject_staging(path: Path, **kwargs: object) -> object:
            if Path(path) == staging:
                raise state.UnsupportedFilesystemError("injected mount change")
            return original(path, **kwargs)

        with mock.patch.object(
            state, "qualify_local_filesystem", side_effect=reject_staging
        ):
            result = recovery.cleanup_resolved_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertFalse(result["cleanupCompleted"])
        self.assertTrue(staging.exists())
        self.assertTrue(permit.exists())
        self.assertTrue(controller.exists())
        self.assertTrue(action_record.exists())
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_update_cleanup_symlinked_auxiliary_target_deletes_nothing(self) -> None:
        staging = self.root.parent / ".streamhome-update.auxiliary"
        staging.mkdir()
        outside = self.root.parent / "outside-file"
        outside.write_text("safe", encoding="utf-8")
        operation_id, generation, permit, controller, action_record = (
            self._pending_update_cleanup(staging)
        )
        controller.unlink()
        controller.symlink_to(outside)
        result = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertFalse(result["cleanupCompleted"])
        self.assertTrue(staging.exists())
        self.assertTrue(permit.exists())
        self.assertTrue(controller.is_symlink())
        self.assertTrue(outside.exists())
        self.assertTrue(action_record.exists())

    def test_update_cleanup_valid_targets_are_deleted_and_audit_retained(self) -> None:
        staging = self.root.parent / ".streamhome-update.valid"
        staging.mkdir()
        operation_id, generation, permit, controller, action_record = (
            self._pending_update_cleanup(staging)
        )
        result = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertTrue(result["cleanupCompleted"])
        self.assertFalse(staging.exists())
        self.assertFalse(permit.exists())
        self.assertFalse(controller.exists())
        self.assertTrue(action_record.exists())
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_update_cleanup_unlink_failure_keeps_retryable_debt(self) -> None:
        staging = self.root.parent / ".streamhome-update.retry"
        staging.mkdir()
        operation_id, generation, permit, controller, action_record = (
            self._pending_update_cleanup(staging)
        )
        original_unlink = Path.unlink

        def fail_controller_unlink(path: Path, *args: object, **kwargs: object) -> None:
            if path == controller:
                raise OSError("injected deletion failure")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_controller_unlink):
            pending = recovery.cleanup_resolved_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        self.assertFalse(pending["cleanupCompleted"])
        self.assertFalse(staging.exists())
        self.assertFalse(permit.exists())
        self.assertTrue(controller.exists())
        self.assertEqual(
            recovery.read_operator_action_record(action_record).result,
            "operator_cleanup_pending",
        )
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        retried = recovery.cleanup_resolved_recovery(
            self.root, operation_id=operation_id, generation=generation
        )
        self.assertTrue(retried["cleanupCompleted"])
        self.assertFalse(controller.exists())

    def test_failed_postcommit_update_continue_remains_recovery_required(self) -> None:
        operation_id, generation, journal = self._claim_update(committed=True)

        class FailedUpdateProcess:
            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return 1

            def poll(self) -> int:
                return 1

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        with (
            mock.patch.object(recovery, "_runtime_status", return_value="offline"),
            mock.patch.object(recovery, "_git_commit_exists", return_value=True),
            mock.patch.object(recovery, "_installed_commit", return_value=self.target),
            mock.patch.object(
                recovery, "_durable_recovery_evidence", return_value="exact-evidence"
            ),
            mock.patch.object(state, "qualify_lifecycle_filesystem"),
            mock.patch.object(state, "qualify_local_filesystem"),
            mock.patch.object(
                update_controller.subprocess,
                "Popen",
                side_effect=lambda *_args, **_kwargs: FailedUpdateProcess(),
            ),
        ):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            with self.assertRaises(recovery.RecoveryExecutionError):
                recovery.execute_recovery(
                    self.root,
                    operation_id=operation_id,
                    generation=generation,
                    action="continue_recovery",
                    evidence_identity=plan["evidenceIdentity"],
                )
        current = state.inspect_state(self.root)
        self.assertEqual(
            current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED
        )
        payload = update_controller.read_update_journal(journal)
        self.assertTrue(payload["runtime_committed"])
        self.assertNotIn(payload["phase"], {"rolling_back", "rolled_back"})

    def test_execute_update_rollback_is_refused_without_exact_evidence(self) -> None:
        operation_id, generation, _ = self._claim_update(committed=False)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
            forged = recovery._new_action_paths(self.root, operation_id, generation)
            recovery._write_action_record(
                forged.record_path,
                recovery.OperatorActionRecord(
                    1, operation_id, generation, "update", "rollback",
                    "2026-09-21T12:00:00Z", plan["evidenceIdentity"],
                    "planned", None, None,
                ),
            )
            with self.assertRaises(recovery.RecoveryEvidenceStale):
                recovery.execute_recovery(
                    self.root,
                    operation_id=operation_id,
                    generation=generation,
                    action="rollback",
                    evidence_identity=plan["evidenceIdentity"],
                )
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)

    def test_takeover_reconciles_transfer_before_update_journal_and_rotates_again(self) -> None:
        operation_id, generation, journal_path = self._claim_update(committed=False)
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            first_plan = recovery.plan_recovery(
                self.root, operation_id=operation_id, generation=generation
            )
        state.qualify_lifecycle_filesystem(self.root)
        paths = recovery._new_action_paths(self.root, operation_id, generation)
        recovery._write_action_record(
            paths.record_path,
            recovery.OperatorActionRecord(
                1,
                operation_id,
                generation,
                "update",
                "safe_abort",
                "2026-09-21T12:00:00Z",
                first_plan["evidenceIdentity"],
                "planned",
                None,
                None,
            ),
        )
        capability = state.load_controller_capability(
            self.root, operation_id=operation_id, generation=generation
        )
        lease, transferred = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=operation_id,
            generation=generation,
            expected_phase="recovery_required",
            owner_capability=capability,
            next_owner_kind="updater",
        )
        lease.release()
        interrupted_generation = transferred.state.generation
        self.assertEqual(
            json.loads(journal_path.read_text(encoding="utf-8"))[
                "lifecycle_generation"
            ],
            generation,
        )
        with mock.patch.object(recovery, "_runtime_status", return_value="offline"):
            second_plan = recovery.plan_recovery(
                self.root,
                operation_id=operation_id,
                generation=interrupted_generation,
            )
            self.assertEqual(second_plan["permittedActions"], ["safe_abort"])
            result = recovery.execute_recovery(
                self.root,
                operation_id=operation_id,
                generation=interrupted_generation,
                action="safe_abort",
                evidence_identity=second_plan["evidenceIdentity"],
            )
        self.assertEqual(
            result["controllerGeneration"], interrupted_generation + 1
        )
        self.assertIsNone(state.inspect_state(self.root).active_operation)

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
