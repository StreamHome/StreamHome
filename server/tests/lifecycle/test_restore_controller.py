from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from lifecycle import backup_manifest, coordination, recovery, restore_controller, state
from lifecycle.configuration_paths import ConfigurationPathError, resolve_temp_directory
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


def read_marker(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("SELECT value FROM user WHERE id='marker'").fetchone()[0])
    finally:
        connection.close()


class RestoreControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "server" / "backup").mkdir(parents=True)
        (self.root / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.root / "update.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        self.database = self.root / "server" / "database.db"
        self.backup = self.root / "server" / "backup" / "backup_20260921_120000.db"
        create_database(self.database, "before")
        create_database(self.backup, "candidate")
        self.claim = state.claim_operation_with_credential(
            self.root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = self.claim.state.active_operation
        assert operation is not None
        self.operation_id = operation.operation_id
        self.generation = self.claim.state.generation
        self.identity = restore_controller.prevalidate_candidate(self.root, self.backup.name)
        restore_controller.create_restore_journal(
            self.root,
            operation_id=self.operation_id,
            generation=self.generation,
            candidate=self.identity,
        )
        self.paths = restore_controller.restore_paths(self.root, self.operation_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_candidate_prevalidation_is_contained_and_supports_legacy_db(self) -> None:
        self.assertEqual(self.identity.filename, self.backup.name)
        self.assertEqual(self.identity.size, self.backup.stat().st_size)
        self.assertEqual(len(self.identity.sha256), 64)
        with self.assertRaises(restore_controller.RestoreValidationError):
            restore_controller.prevalidate_candidate(self.root, "../database.db")
        outside = self.root / "outside.db"
        create_database(outside, "outside")
        link = self.root / "server" / "backup" / "backup_20260921_120001.db"
        link.symlink_to(outside)
        with self.assertRaises(restore_controller.RestoreValidationError):
            restore_controller.prevalidate_candidate(self.root, link.name)

    def test_candidate_with_sqlite_sidecar_is_rejected(self) -> None:
        Path(f"{self.backup}-wal").write_bytes(b"unrecorded-wal")
        with self.assertRaises(restore_controller.RestoreValidationError):
            restore_controller.prevalidate_candidate(self.root, self.backup.name)

    def test_manifest_backed_candidate_is_validated_before_restore(self) -> None:
        candidate = self.root / "server" / "backup" / "backup_v1_20260921_120001.db"
        create_database(candidate, "manifest-candidate")
        candidate.chmod(0o600)
        backup_manifest.create_manifest(candidate, application_build_id="build123")
        identity = restore_controller.prevalidate_candidate(self.root, candidate.name)
        self.assertEqual(identity.filename, candidate.name)
        self.assertEqual(identity.size, candidate.stat().st_size)

        with candidate.open("ab") as handle:
            handle.write(b"changed")
        with self.assertRaises(restore_controller.RestoreValidationError):
            restore_controller.prevalidate_candidate(self.root, candidate.name)

    def test_new_candidate_missing_manifest_cannot_downgrade_to_legacy(self) -> None:
        candidate = self.root / "server" / "backup" / "backup_v1_20260921_120002.db"
        create_database(candidate, "missing-manifest")
        candidate.chmod(0o600)
        with self.assertRaises(restore_controller.RestoreValidationError):
            restore_controller.prevalidate_candidate(self.root, candidate.name)

    def test_legacy_candidate_with_corrupt_manifest_is_rejected(self) -> None:
        manifest = backup_manifest.manifest_path_for(self.backup)
        manifest.write_text("{", encoding="utf-8")
        manifest.chmod(0o600)
        with self.assertRaises(restore_controller.RestoreValidationError):
            restore_controller.prevalidate_candidate(self.root, self.backup.name)

    def test_candidate_identity_change_during_staging_is_rejected(self) -> None:
        real_copy = restore_controller._copy_sqlite

        def copy_then_change(source: Path, destination: Path, **kwargs) -> None:
            real_copy(source, destination, **kwargs)
            with source.open("ab") as handle:
                handle.write(b"changed-after-copy")

        journal = restore_controller.read_restore_journal(self.root, self.operation_id)
        with mock.patch.object(restore_controller, "_copy_sqlite", side_effect=copy_then_change):
            with self.assertRaises(restore_controller.RestoreValidationError):
                restore_controller.stage_candidate(self.paths, journal)

    def test_corrupt_or_unknown_journal_fails_closed(self) -> None:
        journal_path = self.paths.journal_path
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        payload["unknown"] = True
        journal_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(restore_controller.RestorePersistenceError):
            restore_controller.read_restore_journal(self.root, self.operation_id)
        journal_path.write_text("{", encoding="utf-8")
        with self.assertRaises(restore_controller.RestorePersistenceError):
            restore_controller.read_restore_journal(self.root, self.operation_id)

    def test_semantically_impossible_journal_states_fail_closed(self) -> None:
        valid = json.loads(self.paths.journal_path.read_text(encoding="utf-8"))
        invalid_changes = (
            {"accepted": True, "outcome": "accepted"},
            {"staged_size": 1},
            {"progress": "succeeded", "accepted": True, "outcome": "succeeded"},
            {"activation_state": "completed"},
            {"updated_at": valid["created_at"] - 1},
            {"updated_at": float("nan")},
            {"progress": "recovery_required", "error_code": None},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                payload = {**valid, **changes}
                self.paths.journal_path.write_text(json.dumps(payload), encoding="utf-8")
                self.paths.journal_path.chmod(0o600)
                with self.assertRaises(restore_controller.RestorePersistenceError):
                    restore_controller.read_restore_journal(self.root, self.operation_id)
                self.paths.journal_path.write_text(json.dumps(valid), encoding="utf-8")
                self.paths.journal_path.chmod(0o600)
        with self.assertRaises(restore_controller.RestorePersistenceError):
            restore_controller.update_restore_journal(
                self.root,
                self.operation_id,
                progress="runtime_stopped",
            )
    def test_wal_checkpoint_preserves_committed_data_and_creates_valid_rollback(self) -> None:
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os,sqlite3,sys; "
                    "c=sqlite3.connect(sys.argv[1]); "
                    "c.execute('PRAGMA journal_mode=WAL'); "
                    "c.execute('PRAGMA wal_autocheckpoint=0'); "
                    "c.execute(\"UPDATE user SET value='wal-committed' WHERE id='marker'\"); "
                    "c.commit(); os._exit(0)"
                ),
                str(self.database),
            ],
            check=True,
        )
        self.assertTrue(Path(f"{self.database}-wal").exists())
        size, digest, previous_size, previous_digest = restore_controller.create_rollback_checkpoint(self.paths)
        self.assertGreater(size, 0)
        self.assertEqual(len(digest), 64)
        self.assertGreater(previous_size, 0)
        self.assertEqual(len(previous_digest), 64)
        self.assertEqual(read_marker(self.paths.rollback_path), "wal-committed")
        restore_controller.validate_sqlite_database(self.paths.rollback_path)
        self.assertFalse(Path(f"{self.database}-wal").exists())
        self.assertFalse(Path(f"{self.database}-shm").exists())

    def _prepare_artifacts(self) -> dict:
        rollback_size, rollback_hash, previous_size, previous_hash = restore_controller.create_rollback_checkpoint(self.paths)
        journal = restore_controller.update_restore_journal(
            self.root,
            self.operation_id,
            progress="checkpoint_ready",
            rollback_size=rollback_size,
            rollback_sha256=rollback_hash,
            previous_size=previous_size,
            previous_sha256=previous_hash,
        )
        staged_size, staged_hash = restore_controller.stage_candidate(self.paths, journal)
        return restore_controller.update_restore_journal(
            self.root,
            self.operation_id,
            progress="candidate_staged",
            staged_size=staged_size,
            staged_sha256=staged_hash,
        )

    def test_activation_marker_precedes_atomic_replace_and_source_is_unchanged(self) -> None:
        journal = self._prepare_artifacts()
        source_hash = restore_controller.artifact_identity(self.backup)
        real_replace = os.replace
        observed: list[str] = []

        def replace(source: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
            if Path(destination) == self.database:
                current = restore_controller.read_restore_journal(self.root, self.operation_id)
                observed.append(str(current["activation_state"]))
            real_replace(source, destination)

        with mock.patch.object(restore_controller.os, "replace", side_effect=replace):
            restore_controller.activate_staged_candidate(self.paths, journal)
        final = restore_controller.read_restore_journal(self.root, self.operation_id)
        self.assertEqual(observed, ["started"])
        self.assertEqual(final["activation_state"], "completed")
        self.assertEqual(read_marker(self.database), "candidate")
        self.assertEqual(restore_controller.artifact_identity(self.backup), source_hash)

    def _accept_candidate(self) -> dict:
        journal = self._prepare_artifacts()
        restore_controller.activate_staged_candidate(self.paths, journal)
        for before, after in (("reserved", "exclusive"), ("exclusive", "verifying")):
            state.transition_operation_with_credential(self.root, operation_id=self.operation_id,
                generation=self.generation, expected_phase=before, next_phase=after)
        restore_controller.update_restore_journal(self.root, self.operation_id, progress="verifying_candidate")
        restore_controller.update_restore_journal(self.root, self.operation_id,
            progress="accepted", accepted=True, outcome="accepted")
        return restore_controller.update_restore_journal(self.root, self.operation_id, progress="normal_recovery")

    def test_accepted_database_writes_preserve_lineage_and_operator_continue(self) -> None:
        journal = self._accept_candidate()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("UPDATE user SET value='recovered' WHERE id='marker'")
            connection.execute("CREATE TABLE migration_evidence (id INTEGER)")
            connection.commit()
            self.assertTrue(restore_controller.installed_database_is_recognized(self.paths, journal))
            restore_controller._mark_recovery_required(self.paths, self.generation, "normal_recovery_failed")
            artifacts, accepted, _, actions = recovery._restore_plan(self.root, self.operation_id,
                self.generation, controller_status="inactive", runtime_status="offline")
            self.assertTrue(accepted)
            self.assertEqual(actions, ["continue_recovery"])
            self.assertEqual(artifacts["canonicalDatabase"], "valid")

    def test_same_contents_replacement_and_foreign_provenance_fail_closed(self) -> None:
        journal = self._accept_candidate()
        replacement = self.paths.operation_dir / "foreign.db"
        restore_controller._copy_sqlite(self.database, replacement, source_standalone=False)
        os.replace(replacement, self.database)
        self.assertFalse(restore_controller.installed_database_is_recognized(self.paths, journal))

    def test_restore_rejects_pending_media_receipts_before_candidate_replace(self) -> None:
        journal = self._prepare_artifacts()
        pending = self.root / "server" / "temp" / "media-operations" / "pending"
        pending.mkdir(parents=True)
        (pending / "operation.json").write_text('{"accepted": true}', encoding="utf-8")
        original = restore_controller.artifact_identity(self.database)
        with self.assertRaises(restore_controller.RestoreRecoveryRequired):
            restore_controller.activate_staged_candidate(self.paths, journal)
        self.assertEqual(restore_controller.artifact_identity(self.database), original)
        self.assertEqual(restore_controller.read_restore_journal(self.root, self.operation_id)["activation_state"], "not_started")
        self.assertTrue((pending / "operation.json").is_file())

    def test_rollback_rejects_pending_media_receipts_in_custom_temp_directory(self) -> None:
        journal = self._prepare_artifacts()
        restore_controller.activate_staged_candidate(self.paths, journal)
        custom = self.root / "configured-temp"
        pending = custom / "media-operations" / "pending"
        pending.mkdir(parents=True)
        original = restore_controller.artifact_identity(self.database)
        with mock.patch.dict(os.environ, {"TEMP_DIR": str(custom)}):
            with self.assertRaises(restore_controller.RestoreRecoveryRequired):
                restore_controller.restore_rollback_checkpoint(self.paths,
                    restore_controller.read_restore_journal(self.root, self.operation_id))
        self.assertEqual(restore_controller.artifact_identity(self.database), original)

    def test_temp_path_resolution_matches_environment_and_dotenv_precedence(self) -> None:
        (self.root / ".env").write_text('TEMP_DIR="root-temp" # configured\n', encoding="utf-8")
        (self.root / "server" / ".env").write_text("TEMP_DIR=server-temp\n", encoding="utf-8")
        self.assertEqual(resolve_temp_directory(self.root, environment={}), self.root / "server" / "root-temp")
        self.assertEqual(resolve_temp_directory(self.root, environment={"TEMP_DIR": "environment-temp"}), self.root / "server" / "environment-temp")
        (self.root / ".env").write_text("TEMP_DIR=${UNRESOLVED}/temp\n", encoding="utf-8")
        with self.assertRaises(ConfigurationPathError):
            resolve_temp_directory(self.root, environment={})

    def test_modified_lineage_nonce_fails_closed(self) -> None:
        journal = self._accept_candidate()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE streamhome_restore_lineage SET nonce=?", ("f" * 64,))
        self.assertFalse(restore_controller.installed_database_is_recognized(self.paths, journal))

    def test_completion_intent_hashes_fresh_consistent_wal_snapshot(self) -> None:
        journal = self._accept_candidate()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("UPDATE user SET value='normal-recovery' WHERE id='marker'")
            connection.commit()
            self.assertNotEqual(restore_controller.consistent_database_identity(self.paths),
                (journal["staged_size"], journal["staged_sha256"]))
            restore_controller.record_restore_completion_intent(self.paths, generation=self.generation, outcome="succeeded")
            state.complete_operation_with_credential(self.root, operation_id=self.operation_id,
                generation=self.generation, expected_phase="verifying")
            self.assertTrue(restore_controller.reconcile_restore_completion_intent(self.root, self.operation_id, self.generation))
            self.assertEqual(restore_controller.read_restore_journal(self.root, self.operation_id)["progress"], "succeeded")
        finally:
            connection.close()

    def test_completion_intent_accepts_owned_writes_after_authority_completion(self) -> None:
        self._accept_candidate()
        restore_controller.record_restore_completion_intent(self.paths, generation=self.generation, outcome="succeeded")
        state.complete_operation_with_credential(self.root, operation_id=self.operation_id,
            generation=self.generation, expected_phase="verifying")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE user SET value='normal-post-completion-write' WHERE id='marker'")
        self.assertTrue(restore_controller.reconcile_restore_completion_intent(self.root, self.operation_id, self.generation))

    def test_completion_intent_rejects_mutated_completion_snapshot(self) -> None:
        self._accept_candidate()
        restore_controller.record_restore_completion_intent(self.paths, generation=self.generation, outcome="succeeded")
        state.complete_operation_with_credential(self.root, operation_id=self.operation_id,
            generation=self.generation, expected_phase="verifying")
        with closing(sqlite3.connect(self.paths.operation_dir / "completion.db")) as connection, connection:
            connection.execute("UPDATE user SET value='tampered-completion' WHERE id='marker'")
        self.assertFalse(restore_controller.reconcile_restore_completion_intent(self.root, self.operation_id, self.generation))

    def test_resumed_activated_rollback_is_not_replaced_or_rewound(self) -> None:
        journal = self._prepare_artifacts()
        restore_controller.activate_staged_candidate(self.paths, journal)
        restore_controller.restore_rollback_checkpoint(self.paths,
            restore_controller.read_restore_journal(self.root, self.operation_id))
        inode = self.database.stat().st_ino
        # Runtime I/O is isolated; actual journal transitions, SQLite identities
        # and shared-authority completion execute here.
        with (mock.patch.object(restore_controller, "_stop_supported_runtime"),
              mock.patch.object(restore_controller, "_start_maintenance"),
              mock.patch.object(restore_controller, "_start_backend"),
              mock.patch.object(restore_controller, "_build_id", return_value="test-build"),
              mock.patch.object(restore_controller, "_wait_guarded_ready"),
              mock.patch.object(restore_controller, "_commit_guarded_runtime"),
              mock.patch.object(restore_controller, "_wait_general_ready"),
              mock.patch.object(restore_controller, "_start_web"),
              mock.patch.object(runtime_control, "terminate_owned_service", return_value=True)):
            self.assertEqual(restore_controller._complete_automatic_rollback(self.paths,
                self.generation, self.claim.owner_capability, 3000), 0)
        self.assertEqual(self.database.stat().st_ino, inode)
        self.assertEqual(restore_controller.read_restore_journal(self.root, self.operation_id)["progress"], "rolled_back")

    def test_accepted_rollback_controller_restart_completes_under_verifying(self) -> None:
        journal = self._prepare_artifacts()
        restore_controller.activate_staged_candidate(self.paths, journal)
        restore_controller.restore_rollback_checkpoint(self.paths,
            restore_controller.read_restore_journal(self.root, self.operation_id))
        restore_controller.update_restore_journal(self.root, self.operation_id, progress="verifying_rollback")
        restore_controller.update_restore_journal(self.root, self.operation_id,
            progress="normal_recovery", accepted=True, outcome="rolled_back")
        for before, after in (("reserved", "exclusive"), ("exclusive", "verifying")):
            state.transition_operation_with_credential(self.root, operation_id=self.operation_id,
                generation=self.generation, expected_phase=before, next_phase=after)
        inode = self.database.stat().st_ino
        with (mock.patch.object(restore_controller, "_stop_supported_runtime"),
              mock.patch.object(restore_controller, "_start_maintenance"),
              mock.patch.object(restore_controller, "_start_backend"),
              mock.patch.object(restore_controller, "_build_id", return_value="test-build"),
              mock.patch.object(restore_controller, "_request_json", return_value=(0, {})),
              mock.patch.object(restore_controller, "_wait_guarded_ready"),
              mock.patch.object(restore_controller, "_commit_guarded_runtime"),
              mock.patch.object(restore_controller, "_wait_general_ready"),
              mock.patch.object(restore_controller, "_start_web"),
              mock.patch.object(runtime_control, "port_is_available", return_value=True),
              mock.patch.object(runtime_control, "terminate_owned_service", return_value=True)):
            result = restore_controller._run_controller(self.root, self.operation_id,
                self.generation, self.claim.owner_capability, acknowledgement_fd=None, gate_fd=None)
        self.assertEqual(result, 0)
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(self.database.stat().st_ino, inode)
        self.assertEqual(restore_controller.read_restore_journal(self.root, self.operation_id)["progress"], "rolled_back")

    def test_failed_candidate_can_restore_exact_checkpoint(self) -> None:
        journal = self._prepare_artifacts()
        restore_controller.activate_staged_candidate(self.paths, journal)
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
                    "c.execute('PRAGMA journal_mode=WAL'); "
                    "c.execute(\"UPDATE user SET value='failed-candidate-wal' WHERE id='marker'\"); "
                    "c.commit(); os._exit(0)"
                ),
                str(self.database),
            ],
            check=True,
        )
        self.assertTrue(Path(f"{self.database}-wal").exists())
        active = restore_controller.read_restore_journal(self.root, self.operation_id)
        restore_controller.restore_rollback_checkpoint(self.paths, active)
        self.assertEqual(read_marker(self.database), "before")
        self.assertFalse(Path(f"{self.database}-wal").exists())
        self.assertFalse(Path(f"{self.database}-shm").exists())
        final = restore_controller.read_restore_journal(self.root, self.operation_id)
        self.assertEqual(final["activation_state"], "rollback_completed")

    def test_rollback_replace_crash_is_reconciled_from_exact_activation_identity(self) -> None:
        journal = self._prepare_artifacts()
        restore_controller.activate_staged_candidate(self.paths, journal)
        real_replace = os.replace

        def replace_then_interrupt(source, destination):
            real_replace(source, destination)
            if Path(destination) == self.database and Path(source).name == "rollback-activation.db":
                raise OSError("simulated crash after rollback replace")

        with mock.patch.object(restore_controller.os, "replace", side_effect=replace_then_interrupt):
            with self.assertRaises(OSError):
                restore_controller.restore_rollback_checkpoint(
                    self.paths,
                    restore_controller.read_restore_journal(self.root, self.operation_id),
                )
        interrupted = restore_controller.read_restore_journal(self.root, self.operation_id)
        self.assertEqual(interrupted["activation_state"], "rollback_started")
        recovered = restore_controller._recover_activation_state(self.paths, interrupted)
        self.assertEqual(recovered["activation_state"], "rollback_completed")
        self.assertEqual(read_marker(self.database), "before")

    def test_activation_crash_is_resolved_only_by_artifact_identity(self) -> None:
        journal = self._prepare_artifacts()
        restore_controller.update_restore_journal(
            self.root,
            self.operation_id,
            progress="activation_started",
            activation_state="started",
        )
        recovered = restore_controller._recover_activation_state(
            self.paths, restore_controller.read_restore_journal(self.root, self.operation_id)
        )
        self.assertEqual(recovered["progress"], "candidate_staged")
        self.paths.candidate_path.unlink()
        with self.assertRaises(restore_controller.RestoreRecoveryRequired):
            restore_controller._recover_activation_state(
                self.paths,
                restore_controller.update_restore_journal(
                    self.root,
                    self.operation_id,
                    progress="activation_started",
                    activation_state="started",
                ),
            )

    def test_public_status_omits_candidate_and_artifact_paths(self) -> None:
        projection = restore_controller.public_restore_status(self.root, self.operation_id)
        encoded = json.dumps(projection)
        self.assertNotIn(self.backup.name, encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(self.claim.owner_capability, encoded)

    def test_handoff_argv_contains_no_capability_and_requires_ack(self) -> None:
        captured: dict[str, object] = {}

        class FakeProcess:
            def __init__(self, arguments, **kwargs):
                captured["arguments"] = arguments
                captured["kwargs"] = kwargs
                ack_fd = int(arguments[arguments.index("--ack-fd") + 1])
                self.capability_fd = os.dup(
                    int(arguments[arguments.index("--capability-fd") + 1])
                )
                os.write(
                    ack_fd,
                    json.dumps(
                        {
                            "owned": True,
                            "operation_id": self_operation,
                            "generation": self_generation + 1,
                        }
                    ).encode(),
                )

            def poll(self):
                return None

            def terminate(self):
                os.close(self.capability_fd)
                return None

        self_operation = self.operation_id
        self_generation = self.generation
        fake_lease = state.ControllerLease(
            state.SCHEMA_VERSION,
            self.operation_id,
            self.generation + 1,
            state.OwnerKind.RESTORE_CONTROLLER,
            os.getpid(),
            "1",
        )
        with (
            mock.patch.object(restore_controller.subprocess, "Popen", FakeProcess),
            mock.patch.object(restore_controller.state, "read_controller_lease", return_value=fake_lease),
            mock.patch.object(restore_controller.state, "controller_lease_is_current", return_value=True),
        ):
            handoff = restore_controller.spawn_restore_controller(
                self.root,
                operation_id=self.operation_id,
                generation=self.generation,
                owner_capability=self.claim.owner_capability,
                timeout=0.5,
            )
            handoff.release_after_response()
            os.close(handoff.process.capability_fd)
        arguments = captured["arguments"]
        self.assertNotIn(self.claim.owner_capability, arguments)
        self.assertNotIn("env", captured["kwargs"])

        with (
            mock.patch.object(restore_controller.subprocess, "Popen", FakeProcess),
            mock.patch.object(restore_controller.state, "read_controller_lease", return_value=None),
        ):
            with self.assertRaises(restore_controller.RestoreConflictError):
                restore_controller.spawn_restore_controller(
                    self.root,
                    operation_id=self.operation_id,
                    generation=self.generation,
                    owner_capability=self.claim.owner_capability,
                    timeout=0.5,
                )

    def test_response_gate_requires_exact_signal_and_rejects_timeout_and_eof(self) -> None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"1")
        os.close(write_fd)
        restore_controller._await_response_completion(read_fd, timeout=0.1)

        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        with self.assertRaises(restore_controller.RestoreResponseGateError):
            restore_controller._await_response_completion(read_fd, timeout=0.1)

        read_fd, write_fd = os.pipe()
        try:
            with mock.patch.object(restore_controller.select, "select", return_value=([], [], [])):
                with self.assertRaises(restore_controller.RestoreResponseGateError):
                    restore_controller._await_response_completion(read_fd, timeout=0.01)
        finally:
            os.close(write_fd)

    def test_response_gate_failure_marks_recovery_without_runtime_shutdown(self) -> None:
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        with mock.patch.object(restore_controller, "_stop_supported_runtime") as stop_runtime:
            result = restore_controller._run_controller(
                self.root,
                self.operation_id,
                self.generation,
                self.claim.owner_capability,
                acknowledgement_fd=None,
                gate_fd=read_fd,
            )
        self.assertEqual(result, 1)
        stop_runtime.assert_not_called()
        current = state.inspect_state(self.root)
        self.assertEqual(current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(current.generation, self.generation + 1)
        with self.assertRaises(state.LifecycleConflictError):
            state.validate_operation_owner_current_phase(
                self.root,
                operation_id=self.operation_id,
                generation=self.generation,
                owner_capability=self.claim.owner_capability,
            )
        journal = restore_controller.read_restore_journal(self.root, self.operation_id)
        self.assertEqual(journal["generation"], self.generation + 1)
        self.assertEqual(journal["error_code"], "restore_response_not_confirmed")

    def test_response_gate_timeout_never_starts_runtime_shutdown(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            with (
                mock.patch.object(restore_controller.select, "select", return_value=([], [], [])),
                mock.patch.object(restore_controller, "_stop_supported_runtime") as stop_runtime,
            ):
                result = restore_controller._run_controller(
                    self.root,
                    self.operation_id,
                    self.generation,
                    self.claim.owner_capability,
                    acknowledgement_fd=None,
                    gate_fd=read_fd,
                )
        finally:
            os.close(write_fd)
        self.assertEqual(result, 1)
        stop_runtime.assert_not_called()
        self.assertEqual(
            state.inspect_state(self.root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )

    def test_recovery_required_is_terminal_for_automatic_launchers(self) -> None:
        state.transition_operation_with_credential(
            self.root,
            operation_id=self.operation_id,
            generation=self.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        restore_controller.update_restore_journal(
            self.root,
            self.operation_id,
            progress="recovery_required",
            error_code="operator_action_required",
        )
        with mock.patch.object(restore_controller, "spawn_restore_controller") as spawn:
            self.assertEqual(restore_controller.recover_restore(self.root), 3)
        spawn.assert_not_called()

        coordinator = maintenance_server.RestoreRecoveryCoordinator(
            self.root, self.operation_id, mock.Mock()
        )

        class TwoWaits:
            def __init__(self) -> None:
                self.calls = 0

            def wait(self, timeout: float) -> bool:
                del timeout
                self.calls += 1
                return self.calls > 1

        coordinator.stop_event = TwoWaits()  # type: ignore[assignment]
        with mock.patch.object(maintenance_server.subprocess, "Popen") as popen:
            coordinator.run()
        popen.assert_not_called()

    def test_stale_controller_cannot_mark_or_mutate_new_generation(self) -> None:
        handle, transferred = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=self.operation_id,
            generation=self.generation,
            expected_phase="reserved",
            owner_capability=self.claim.owner_capability,
            next_owner_kind="restore_controller",
        )
        try:
            restore_controller.transfer_restore_journal_generation(
                self.root,
                self.operation_id,
                expected_generation=self.generation,
                next_generation=transferred.state.generation,
            )
            self.assertFalse(
                restore_controller._mark_recovery_required(
                    self.paths,
                    self.generation,
                    "stale_controller_failed",
                )
            )
        finally:
            handle.release()
        journal = restore_controller.read_restore_journal(self.root, self.operation_id)
        self.assertEqual(journal["progress"], "claimed")
        self.assertIsNone(journal["error_code"])

    def test_claim_without_journal_is_safely_aborted_only_before_destructive_work(self) -> None:
        state.complete_operation_with_credential(
            self.root,
            operation_id=self.operation_id,
            generation=self.generation,
            expected_phase="reserved",
        )
        abandoned = state.claim_operation_with_credential(
            self.root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = abandoned.state.active_operation
        self.assertIsNotNone(operation)
        self.assertEqual(
            restore_controller.recover_restore(self.root),
            restore_controller.RECOVERY_RESERVATION_ABORTED,
        )
        current = state.inspect_state(self.root)
        self.assertIsNone(current.active_operation)
        self.assertEqual(current.generation, abandoned.state.generation)

    def test_rollback_failure_enters_durable_recovery_required(self) -> None:
        rollback_size, rollback_hash, previous_size, previous_hash = (
            restore_controller.create_rollback_checkpoint(self.paths)
        )
        restore_controller.update_restore_journal(
            self.root,
            self.operation_id,
            progress="checkpoint_ready",
            rollback_size=rollback_size,
            rollback_sha256=rollback_hash,
            previous_size=previous_size,
            previous_sha256=previous_hash,
        )
        with (
            mock.patch.object(restore_controller, "_await_response_completion"),
            mock.patch.object(restore_controller, "_stop_supported_runtime", side_effect=RuntimeError("stop failed")),
            mock.patch.object(restore_controller, "_complete_automatic_rollback", side_effect=RuntimeError("rollback failed")),
            mock.patch.object(restore_controller, "_start_maintenance"),
        ):
            result = restore_controller._run_controller(
                self.root,
                self.operation_id,
                self.generation,
                self.claim.owner_capability,
                acknowledgement_fd=None,
                gate_fd=os.open(os.devnull, os.O_RDONLY),
            )
        self.assertEqual(result, 1)
        current = state.inspect_state(self.root)
        self.assertEqual(current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        journal = restore_controller.read_restore_journal(self.root, self.operation_id)
        self.assertEqual(journal["progress"], "recovery_required")
        self.assertEqual(journal["error_code"], "restore_rollback_failed")

    def test_late_rollback_verification_failure_preserves_all_artifacts(self) -> None:
        journal = self._prepare_artifacts()
        restore_controller.activate_staged_candidate(self.paths, journal)
        state.transition_operation_with_credential(
            self.root,
            operation_id=self.operation_id,
            generation=self.generation,
            expected_phase="reserved",
            next_phase="exclusive",
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=self.operation_id,
            generation=self.generation,
            expected_phase="exclusive",
            next_phase="verifying",
        )
        with (
            mock.patch.object(restore_controller, "_stop_supported_runtime"),
            mock.patch.object(restore_controller, "_start_maintenance"),
        ):
            restore_controller._enter_fail_closed(
                self.paths,
                self.generation,
                "restore_rollback_failed",
                3000,
            )
        current = state.inspect_state(self.root)
        self.assertEqual(current.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        failed = restore_controller.read_restore_journal(self.root, self.operation_id)
        self.assertEqual(failed["progress"], "recovery_required")
        self.assertTrue(self.paths.rollback_path.is_file())
        self.assertEqual(read_marker(self.database), "candidate")

    def test_snapshot_captured_by_exact_fence_is_rejected_without_cancellation(self) -> None:
        coordinator = coordination.RuntimeCoordinator()
        lease = coordinator.admit("backup.snapshot", [coordination.EffectClass.DATABASE_SNAPSHOT])
        snapshot = coordinator.close_admission(
            list(coordination.EffectClass),
            operation_id=self.operation_id,
            generation=self.generation,
        )
        self.assertTrue(
            restore_controller.captured_work_has_effect(
                snapshot.lease_ids,
                coordinator.active_work(),
                coordination.EffectClass.DATABASE_SNAPSHOT.value,
            )
        )
        self.assertFalse(lease.cancellation_requested)
        coordinator.release_fence(snapshot)
        self.assertFalse(lease.cancellation_requested)
        lease.release()

    def test_accepted_recovery_keeps_maintenance_until_final_runtime_switch(self) -> None:
        journal = self._prepare_artifacts()
        restore_controller.activate_staged_candidate(self.paths, journal)
        state.transition_operation_with_credential(
            self.root,
            operation_id=self.operation_id,
            generation=self.generation,
            expected_phase="reserved",
            next_phase="exclusive",
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=self.operation_id,
            generation=self.generation,
            expected_phase="exclusive",
            next_phase="verifying",
        )
        restore_controller.update_restore_journal(
            self.root,
            self.operation_id,
            progress="verifying_candidate",
        )
        restore_controller.update_restore_journal(
            self.root,
            self.operation_id,
            progress="accepted",
            accepted=True,
            outcome="accepted",
        )
        events: list[str] = []

        class Runtime:
            @staticmethod
            def terminate_owned_service(root, service):
                del root
                events.append(f"stop:{service}")
                return True

            @staticmethod
            def port_is_available(port):
                del port
                return True

        maintenance = mock.Mock()
        maintenance.poll.return_value = None
        with (
            mock.patch.object(restore_controller, "_await_response_completion"),
            mock.patch.object(restore_controller, "_web_port", return_value=3000),
            mock.patch.object(restore_controller, "_build_id", return_value="build"),
            mock.patch.object(restore_controller, "_request_json", return_value=(0, {})),
            mock.patch.object(restore_controller, "_stop_supported_runtime", side_effect=lambda *args: events.append("runtime-stopped")),
            mock.patch.object(restore_controller, "_start_maintenance", side_effect=lambda *args: (events.append("maintenance-started"), maintenance)[1]),
            mock.patch.object(restore_controller, "_start_backend", side_effect=lambda *args: (events.append("backend-started"), (mock.Mock(), "build"))[1]),
            mock.patch.object(restore_controller, "_wait_guarded_ready", side_effect=lambda *args: events.append("guarded-ready")),
            mock.patch.object(restore_controller, "_commit_guarded_runtime", side_effect=lambda *args: events.append("guarded-commit")),
            mock.patch.object(restore_controller, "_wait_general_ready", side_effect=lambda *args: events.append("normal-recovery-complete")),
            mock.patch.object(restore_controller, "_start_web", side_effect=lambda *args: (events.append("web-started"), mock.Mock())[1]),
            mock.patch.object(restore_controller, "_runtime_control", return_value=Runtime()),
        ):
            result = restore_controller._run_controller(
                self.root,
                self.operation_id,
                self.generation,
                self.claim.owner_capability,
                acknowledgement_fd=None,
                gate_fd=os.open(os.devnull, os.O_RDONLY),
            )
        self.assertEqual(result, 0)
        self.assertLess(events.index("maintenance-started"), events.index("normal-recovery-complete"))
        self.assertLess(events.index("normal-recovery-complete"), events.index("stop:maintenance"))
        self.assertLess(events.index("stop:maintenance"), events.index("web-started"))

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "Linux /proc required")
    def test_runtime_service_stop_keeps_foreign_process_alive(self) -> None:
        token = "restore-test-runtime-token"
        environment = {
            **os.environ,
            "STREAMHOME_INSTANCE_ROOT": str(self.root),
            "STREAMHOME_INSTANCE_TOKEN": token,
            "STREAMHOME_SERVICE": "backend",
        }
        owned = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=environment,
            start_new_session=True,
        )
        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            runtime_control.record_service(self.root, "backend", owned.pid, token)
            self.assertTrue(runtime_control.terminate_owned_service(self.root, "backend"))
            owned.wait(timeout=5)
            self.assertIsNone(foreign.poll())
        finally:
            if owned.poll() is None:
                owned.terminate()
                owned.wait(timeout=5)
            if foreign.poll() is None:
                foreign.terminate()
                foreign.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
