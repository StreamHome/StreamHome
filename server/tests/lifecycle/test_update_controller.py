from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lifecycle import state
from lifecycle import update_controller
from scratch import maintenance_server


class UpdateControllerTests(unittest.TestCase):
    authority_schema_version = 1

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "StreamHome"
        repository = Path(__file__).resolve().parents[3]
        (self.root / "server").mkdir(parents=True)
        shutil.copytree(repository / "server" / "lifecycle", self.root / "server" / "lifecycle")
        (self.root / "start.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (self.root / "update.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        self.root.joinpath("start.sh").chmod(0o700)
        self.root.joinpath("update.sh").chmod(0o700)
        self._git("init")
        self._git("config", "user.name", "StreamHome Test")
        self._git("config", "user.email", "streamhome-test@example.invalid")
        self._git("add", ".")
        self._git("commit", "-m", "previous")
        self.previous = self._git("rev-parse", "HEAD")
        self.root.joinpath("update.sh").write_text(
            "#!/usr/bin/env bash\n"
            "test \"${STREAMHOME_LIFECYCLE_CONTROLLER_OWNED:-}\" = true || exit 21\n"
            "test ! -e /proc/$$/fd/198 || exit 22\n"
            "python3 -m lifecycle.update_controller validate-controller "
            "--root \"$STREAMHOME_INSTALL_DIR\" "
            "--operation-id \"$STREAMHOME_LIFECYCLE_OPERATION_ID\" "
            "--generation \"$STREAMHOME_LIFECYCLE_GENERATION\" "
            "--expected-phase \"$STREAMHOME_LIFECYCLE_PHASE\" "
            "--supervisor-pid \"$PPID\" || exit 23\n"
            "sleep 1\n",
            encoding="utf-8",
        )
        self.root.joinpath("update.sh").chmod(0o700)
        self._git("add", "update.sh")
        self._git("commit", "-m", "target")
        self.target = self._git("rev-parse", "HEAD")
        self._git("checkout", "--detach", self.previous)
        if self.authority_schema_version == 2:
            paths = state.lifecycle_paths(self.root)
            state._ensure_lifecycle_directories(paths)
            state._write_state_unlocked(paths, state.LifecycleState(2, 0, None))
        self.claimed = state.claim_operation_with_credential(
            self.root,
            operation_type=state.OperationType.UPDATE,
            owner_kind=state.OwnerKind.UPDATER,
        )
        operation = self.claimed.state.active_operation
        assert operation is not None
        run_dir = self.root / ".run"
        self.journal = run_dir / "update-state.json"
        self.journal.write_text(
            json.dumps(
                {
                    "phase": "queued",
                    "transaction_id": operation.operation_id,
                    "lifecycle_generation": self.claimed.state.generation,
                    "lifecycle_phase": "reserved",
                    "target_commit": self.target,
                    "previous_commit": self.previous,
                    "automatic": False,
                    "install_mode": "when_idle",
                    "update_branch": "main",
                    "runtime_committed": False,
                    "runtime_committed_at": None,
                }
            ),
            encoding="utf-8",
        )
        self.journal.chmod(0o600)

    def test_bound_update_mutation_and_takeover_cannot_retarget_original_execution(self):
        from tests.lifecycle.test_update_execution import journal
        payload = journal(self.previous, self.target)
        operation = self.claimed.state.active_operation.operation_id
        payload['transaction_id'] = operation
        payload['managed_execution']['operation_id'] = operation
        self.journal.write_text(json.dumps(payload))
        update_controller.mutate_update_journal(self.root, operation_id=operation, generation=1,
            expected_lifecycle_phase='reserved', expected_detail_phases=frozenset({'preflight'}),
            changes={'message': 'Same original operation preflight.'})
        saved = self.journal.read_bytes()
        for field in ('target_commit', 'previous_commit'):
            with self.subTest(field=field), self.assertRaises(update_controller.UpdateControllerConflict):
                update_controller.mutate_update_journal(self.root, operation_id=operation, generation=1,
                    expected_lifecycle_phase='reserved', changes={field: 'f'*40})
            self.assertEqual(self.journal.read_bytes(), saved)
        lease, transferred = state.acquire_transferred_controller_lease(self.root, operation_id=operation,
            generation=1, expected_phase=state.LifecyclePhase.RESERVED,
            owner_capability=self.claimed.owner_capability, next_owner_kind=state.OwnerKind.UPDATER)
        try:
            with self.assertRaises(update_controller.UpdateControllerConflict):
                update_controller._record_transferred_generation(self.root, operation_id=operation,
                    previous_generation=1, generation=transferred.state.generation,
                    lifecycle_phase=state.LifecyclePhase.RESERVED,
                    target_commit='f'*40, previous_commit=self.previous)
            self.assertEqual(self.journal.read_bytes(), saved)
            update_controller._record_transferred_generation(self.root, operation_id=operation,
                previous_generation=1, generation=transferred.state.generation,
                lifecycle_phase=state.LifecyclePhase.RESERVED,
                target_commit=self.target, previous_commit=self.previous)
            recorded = update_controller.read_update_journal(self.journal)
            self.assertEqual(recorded['managed_execution'], payload['managed_execution'])
            self.assertEqual(recorded['lifecycle_generation'], transferred.state.generation)
        finally:
            lease.release()

    def test_target_wrapper_materializes_its_same_commit_internal_controller(self) -> None:
        internal=self.root/'scripts/runtime/update.sh'
        internal.parent.mkdir(parents=True)
        expected=b'#!/usr/bin/env bash\nprintf "target-internal\\n"\n'
        internal.write_bytes(expected)
        (self.root/'update.sh').write_text('#!/usr/bin/env bash\n'
            '# StreamHome compatibility entry: scripts/runtime/update.sh\n'
            'exec bash "$(dirname "$0")/scripts/runtime/update.sh" "$@"\n')
        self._git('add','update.sh','scripts/runtime/update.sh')
        self._git('commit','-m','target with internal controller')
        target=self._git('rev-parse','HEAD')
        internal.write_bytes(b'#!/usr/bin/env bash\nprintf "wrong-working-tree\\n"\n')
        operation=self.claimed.state.active_operation.operation_id
        result=update_controller._materialize_target_controller(self.root,operation,target)
        self.assertEqual(result.read_bytes(),expected)
        output=subprocess.check_output(['bash',str(result)],text=True)
        self.assertEqual(output,'target-internal\n')

    def test_old_materializer_can_execute_new_target_wrapper_without_current_tree_dependency(self) -> None:
        source=Path(__file__).resolve().parents[3]
        wrapper=(source/'update.sh').read_bytes()
        internal=self.root/'scripts/runtime/update.sh';internal.parent.mkdir(parents=True)
        expected=b'#!/usr/bin/env bash\nprintf "target-internal %s\\n" "$BASHPID"\n'
        internal.write_bytes(expected);(self.root/'update.sh').write_bytes(wrapper)
        self._git('add','update.sh','scripts/runtime/update.sh')
        self._git('commit','-m','new target for old materializer')
        target=self._git('rev-parse','HEAD')
        internal.write_bytes(b'#!/usr/bin/env bash\nexit 28\n')
        copied=self.root/'.run'/('update-controller.'+self.claimed.state.active_operation.operation_id+'.sh')
        copied.write_bytes(wrapper);copied.chmod(0o700)
        process=subprocess.Popen([str(copied),'--manual-execute',target,self.previous,'main','true',str(self.root)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        output,error=process.communicate(timeout=10)
        self.assertEqual(process.returncode,0,error)
        self.assertEqual(output,'target-internal '+str(process.pid)+'\n')

    def test_target_wrapper_missing_its_committed_controller_is_refused(self) -> None:
        (self.root/'update.sh').write_text('#!/usr/bin/env bash\n'
            '# StreamHome compatibility entry: scripts/runtime/update.sh\nexit 0\n')
        self._git('add','update.sh');self._git('commit','-m','incomplete target wrapper')
        target=self._git('rev-parse','HEAD')
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._materialize_target_controller(self.root,
                self.claimed.state.active_operation.operation_id,target)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def test_old_blank_queued_previous_commit_has_narrow_pre_cutover_adoption(self) -> None:
        payload = json.loads(self.journal.read_text(encoding="utf-8"))
        payload.update(previous_commit="", current_commit=self.previous)
        self.journal.write_text(json.dumps(payload), encoding="utf-8")
        identity = update_controller._recover_identity(self.root)
        self.assertEqual(identity[3], self.previous)
        self.assertTrue(identity[-1])
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RESERVED)
        (self.root / ".run" / "pre-update-database.db").write_bytes(b"ambiguous-checkpoint")
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._recover_identity(self.root)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)

    def test_old_blank_queued_previous_commit_rejects_changed_installed_head(self) -> None:
        payload = json.loads(self.journal.read_text(encoding="utf-8"))
        payload.update(previous_commit="", current_commit=self.target)
        self.journal.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._recover_identity(self.root)

    def _spawn(self, generation: int) -> update_controller.UpdateControllerHandoff:
        log = (self.root / "update.log").open("ab", buffering=0)
        try:
            return update_controller.spawn_update_controller(
                self.root,
                operation_id=self.claimed.state.active_operation.operation_id,
                generation=generation,
                target_commit=self.target,
                previous_commit=self.previous,
                automatic=False,
                install_mode="when_idle",
                update_branch="main",
                log_handle=log,
            )
        finally:
            log.close()

    def test_handoff_rotates_generation_capability_and_owns_controller_lock(self) -> None:
        operation = self.claimed.state.active_operation
        assert operation is not None
        handoff = self._spawn(self.claimed.state.generation)
        try:
            self.assertEqual(handoff.generation, self.claimed.state.generation + 1)
            current = state.inspect_state(self.root)
            self.assertEqual(current.generation, handoff.generation)
            with self.assertRaises(state.LifecycleError):
                state.validate_operation_owner(
                    self.root,
                    operation_id=operation.operation_id,
                    generation=self.claimed.state.generation,
                    expected_phase="reserved",
                    owner_capability=self.claimed.owner_capability,
                )
            with self.assertRaises(state.ControllerLockError):
                state.acquire_controller_lease(
                    self.root,
                    operation_id=operation.operation_id,
                    generation=handoff.generation,
                    expected_phase="reserved",
                    owner_capability=state.load_controller_capability(
                        self.root,
                        operation_id=operation.operation_id,
                        generation=handoff.generation,
                    ),
                )
            command_line = Path(f"/proc/{handoff.process.pid}/cmdline").read_bytes()
            self.assertNotIn(self.claimed.owner_capability.encode("ascii"), command_line)
            process_environment = Path(f"/proc/{handoff.process.pid}/environ").read_bytes()
            self.assertNotIn(self.claimed.owner_capability.encode("ascii"), process_environment)
            current_capability = state.load_controller_capability(
                self.root,
                operation_id=operation.operation_id,
                generation=handoff.generation,
            )
            self.assertNotIn(current_capability.encode("ascii"), process_environment)
            journal = json.loads(self.journal.read_text(encoding="utf-8"))
            self.assertEqual(journal["lifecycle_generation"], handoff.generation)
        finally:
            self.assertEqual(handoff.process.wait(timeout=5), 0)

    def test_takeover_rotates_again_and_stale_generation_cannot_validate(self) -> None:
        first = self._spawn(self.claimed.state.generation)
        first.process.wait(timeout=5)
        first_generation = first.generation
        second = self._spawn(first_generation)
        try:
            self.assertEqual(second.generation, first_generation + 1)
            with self.assertRaises(state.LifecycleError):
                state.validate_operation_with_credential(
                    self.root,
                    operation_id=second.operation_id,
                    generation=first_generation,
                )
        finally:
            self.assertEqual(second.process.wait(timeout=5), 0)

    def test_recovery_required_refuses_automatic_controller(self) -> None:
        operation = self.claimed.state.active_operation
        assert operation is not None
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=self.claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        journal["lifecycle_phase"] = "recovery_required"
        self.journal.write_text(json.dumps(journal), encoding="utf-8")
        self.assertEqual(
            update_controller.main(["recover", "--root", str(self.root)]),
            1,
        )
        self.assertEqual(
            state.inspect_state(self.root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )

    def test_maintenance_does_not_relaunch_recovery_required_update(self) -> None:
        operation = self.claimed.state.active_operation
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=self.claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        journal["lifecycle_phase"] = "recovery_required"
        journal["transaction_id"] = "private-source-secret"
        self.journal.write_text(json.dumps(journal), encoding="utf-8")
        status = maintenance_server.safe_public_status(
            self.journal,
            self.root / ".run" / "update-lease.json",
            self.root,
            operation.operation_id,
            30,
        )
        self.assertEqual(status["phase"], "recovery_required")
        self.assertTrue(status["operatorActionRequired"])
        self.assertEqual(status["error"], "operator_action_required")
        self.assertEqual(status["target"], "")
        self.assertEqual(status["transaction"], operation.operation_id[:12])
        self.assertNotIn("private-source-secret", json.dumps(status))
        self.assertFalse(maintenance_server.recovery_needed(status))
        coordinator = maintenance_server.RecoveryCoordinator(
            self.root,
            self.journal,
            self.root / ".run" / "update-lease.json",
            operation.operation_id,
            30,
            object(),
        )
        with patch.object(maintenance_server.subprocess, "Popen") as launch:
            self.assertFalse(coordinator._queue_recovery(status))
        launch.assert_not_called()

    def test_public_maintenance_projection_never_emits_subordinate_private_values(self) -> None:
        private_values = (
            "/srv/private/database.db",
            "token-super-secret",
            "https://user:secret@example.invalid/release",
            "traceback private line",
            "f" * 40,
            "transaction-private-identifier",
        )
        payload = json.loads(self.journal.read_text(encoding="utf-8"))
        payload.update(
            phase="failed",
            message=private_values[0],
            error=private_values[1],
            diagnostic_id=private_values[3],
            target_commit=private_values[4],
            transaction_id=private_values[5],
            source_url=private_values[2],
        )
        self.journal.write_text(json.dumps(payload), encoding="utf-8")
        status = maintenance_server.safe_public_status(
            self.journal,
            self.root / ".run" / "update-lease.json",
            self.root,
            self.claimed.state.active_operation.operation_id,
            30,
        )
        public = json.dumps(status)
        for private in private_values:
            self.assertNotIn(private, public)
        self.assertEqual(
            status["transaction"], self.claimed.state.active_operation.operation_id[:12]
        )
        self.assertEqual(status["target"], "")
        self.assertEqual(status["diagnostic"], "")

    def test_ambiguous_recovery_journal_enters_recovery_required(self) -> None:
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        journal["phase"] = "failed"
        self.journal.write_text(json.dumps(journal), encoding="utf-8")
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._recover_identity(self.root)
        self.assertEqual(
            state.inspect_state(self.root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )

    def test_transfer_reconciles_a_lagging_subordinate_generation(self) -> None:
        first = self._spawn(self.claimed.state.generation)
        self.assertEqual(first.process.wait(timeout=5), 0)
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        journal["lifecycle_generation"] = self.claimed.state.generation
        self.journal.write_text(json.dumps(journal), encoding="utf-8")
        second = self._spawn(first.generation)
        try:
            reconciled = json.loads(self.journal.read_text(encoding="utf-8"))
            self.assertEqual(reconciled["lifecycle_generation"], second.generation)
        finally:
            self.assertEqual(second.process.wait(timeout=5), 0)

    def test_cutover_permit_is_private_exact_and_contains_no_public_projection_secret(self) -> None:
        operation = self.claimed.state.active_operation
        assert operation is not None
        permit = update_controller.create_cutover_permit(
            self.root, operation.operation_id, self.claimed.state.generation
        )
        payload = json.loads(permit.read_text(encoding="utf-8"))
        self.assertEqual(permit.stat().st_mode & 0o777, 0o600)
        update_controller.validate_cutover_permit(
            self.root,
            operation_id=operation.operation_id,
            generation=self.claimed.state.generation,
            supplied_token=payload["token"],
        )
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller.validate_cutover_permit(
                self.root,
                operation_id=operation.operation_id,
                generation=self.claimed.state.generation,
                supplied_token="wrong",
            )
        self.assertNotIn(payload["token"], json.dumps(state.public_projection(state.inspect_state(self.root))))

    def _move_to_verifying(self) -> tuple[str, int]:
        operation = state.inspect_state(self.root).active_operation
        assert operation is not None
        generation = state.inspect_state(self.root).generation
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=generation,
            expected_phase="reserved",
            next_phase="exclusive",
        )
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=generation,
            expected_phase="exclusive",
            next_phase="verifying",
        )
        payload = json.loads(self.journal.read_text(encoding="utf-8"))
        payload.update(
            {
                "phase": "starting",
                "lifecycle_phase": "verifying",
                "runtime_committed": True,
                "runtime_committed_at": time.time(),
            }
        )
        self.journal.write_text(json.dumps(payload), encoding="utf-8")
        return operation.operation_id, generation

    def test_runtime_committed_is_monotonic_across_later_mutations(self) -> None:
        operation_id, generation = self._move_to_verifying()
        updated = update_controller.mutate_update_journal(
            self.root,
            operation_id=operation_id,
            generation=generation,
            expected_lifecycle_phase="verifying",
            changes={"phase": "recovering", "runtime_committed": True},
        )
        self.assertTrue(updated["runtime_committed"])
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller.mutate_update_journal(
                self.root,
                operation_id=operation_id,
                generation=generation,
                expected_lifecycle_phase="verifying",
                changes={"runtime_committed": False},
            )
        self.assertTrue(json.loads(self.journal.read_text())["runtime_committed"])

    def test_only_unaccepted_recovery_can_enter_original_rollback_path(self) -> None:
        operation, generation = self._move_to_verifying()
        payload = json.loads(self.journal.read_text())
        payload.update(phase='recovering', runtime_committed=False, runtime_committed_at=None)
        self.journal.write_text(json.dumps(payload))
        changes = {'phase': 'rolling_back', 'failed_target': self.target}
        updated = update_controller.mutate_update_journal(self.root, operation_id=operation,
            generation=generation, expected_lifecycle_phase='verifying', changes=changes)
        self.assertEqual(updated['phase'], 'rolling_back')
        payload.update(runtime_committed=True, runtime_committed_at=time.time())
        self.journal.write_text(json.dumps(payload))
        before = self.journal.read_bytes()
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller.mutate_update_journal(self.root, operation_id=operation,
                generation=generation, expected_lifecycle_phase='verifying', changes=changes)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_unreadable_committed_journal_is_never_reconstructed(self) -> None:
        operation_id, generation = self._move_to_verifying()
        for invalid in ("{", "[]"):
            with self.subTest(invalid=invalid):
                self.journal.write_text(invalid, encoding="utf-8")
                with self.assertRaises(update_controller.UpdateControllerPersistenceError):
                    update_controller.mutate_update_journal(
                        self.root,
                        operation_id=operation_id,
                        generation=generation,
                        expected_lifecycle_phase="verifying",
                        changes={"phase": "recovering"},
                    )
                self.assertEqual(self.journal.read_text(encoding="utf-8"), invalid)

    def test_committed_journal_read_failure_preserves_existing_bytes(self) -> None:
        operation_id, generation = self._move_to_verifying()
        before = self.journal.read_bytes()
        with patch.object(Path, "read_text", side_effect=OSError("simulated read failure")):
            with self.assertRaises(update_controller.UpdateControllerPersistenceError):
                update_controller.mutate_update_journal(
                    self.root,
                    operation_id=operation_id,
                    generation=generation,
                    expected_lifecycle_phase="verifying",
                    changes={"phase": "recovering"},
                )
        self.assertEqual(self.journal.read_bytes(), before)

    def test_authority_sensitive_journal_rejects_duplicate_and_nonfinite_json(self) -> None:
        original = json.loads(self.journal.read_text(encoding="utf-8"))
        cases = (
            json.dumps(original)[:-1] + ',"runtime_committed":true}',
            json.dumps(original)[:-1]
            + f',"lifecycle_generation":{self.claimed.state.generation}}}',
            json.dumps(original)[:-1] + ',"phase":"queued"}',
            json.dumps({**original, "unexpected_number": float("nan")}),
            json.dumps({**original, "unexpected_number": float("inf")}),
            json.dumps({**original, "unexpected_number": -float("inf")}),
        )
        for raw in cases:
            with self.subTest(raw=raw[-48:]):
                self.journal.write_text(raw, encoding="utf-8")
                with self.assertRaises(update_controller.UpdateControllerPersistenceError):
                    update_controller._read_update_journal(self.journal)

    def test_committed_journal_short_writes_complete_without_losing_boundary(self) -> None:
        operation_id, generation = self._move_to_verifying()
        original_write = os.write
        write_sizes: list[int] = []

        def short_write(descriptor: int, payload: bytes | memoryview) -> int:
            limited = payload[: max(1, len(payload) // 3)]
            written = original_write(descriptor, limited)
            write_sizes.append(written)
            return written

        with patch.object(update_controller.os, "write", side_effect=short_write):
            updated = update_controller.mutate_update_journal(
                self.root,
                operation_id=operation_id,
                generation=generation,
                expected_lifecycle_phase="verifying",
                changes={"phase": "recovering"},
            )
        self.assertGreater(len(write_sizes), 1)
        self.assertTrue(updated["runtime_committed"])
        self.assertTrue(json.loads(self.journal.read_text())["runtime_committed"])

    def test_failed_committed_journal_persistence_keeps_previous_boundary(self) -> None:
        operation_id, generation = self._move_to_verifying()
        before = self.journal.read_bytes()
        with patch.object(
            update_controller,
            "_private_atomic_write",
            side_effect=update_controller.UpdateControllerPersistenceError("simulated failure"),
        ):
            with self.assertRaises(update_controller.UpdateControllerPersistenceError):
                update_controller.mutate_update_journal(
                    self.root,
                    operation_id=operation_id,
                    generation=generation,
                    expected_lifecycle_phase="verifying",
                    changes={"phase": "recovering"},
                )
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertTrue(json.loads(before)["runtime_committed"])

    def test_stale_generation_cannot_mutate_update_journal(self) -> None:
        operation = self.claimed.state.active_operation
        assert operation is not None
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller.mutate_update_journal(
                self.root,
                operation_id=operation.operation_id,
                generation=self.claimed.state.generation + 1,
                expected_lifecycle_phase="reserved",
                changes={"message": "stale"},
            )
        self.assertNotEqual(
            json.loads(self.journal.read_text(encoding="utf-8")).get("message"),
            "stale",
        )

    def test_semantic_contradiction_matrix_fails_closed(self) -> None:
        operation = self.claimed.state.active_operation
        assert operation is not None
        authority = state.inspect_state(self.root)
        base = json.loads(self.journal.read_text(encoding="utf-8"))
        contradictions = (
            {"phase": "installing"},
            {"runtime_committed": True, "runtime_committed_at": time.time()},
            {"phase": "rolling_back", "failed_target": ""},
            {"lifecycle_generation": authority.generation + 1},
        )
        for changes in contradictions:
            with self.subTest(changes=changes):
                payload = dict(base)
                payload.update(changes)
                with self.assertRaises(update_controller.UpdateControllerConflict):
                    update_controller._validate_update_journal_semantics(
                        payload,
                        authority=authority,
                    )

    def test_shell_descendant_does_not_retain_controller_lock(self) -> None:
        child_pid_path = self.root / ".run" / "descendant.pid"
        self.root.joinpath("update.sh").write_text(
            "#!/usr/bin/env bash\n"
            "setsid sleep 20 </dev/null >/dev/null 2>&1 &\n"
            f"printf '%s\\n' \"$!\" > {str(child_pid_path)!r}\n"
            "sleep 1\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.root.joinpath("update.sh").chmod(0o700)
        self._git("add", "update.sh")
        self._git("commit", "-m", "descendant target")
        descendant_target = self._git("rev-parse", "HEAD")
        self._git("checkout", "--detach", self.previous)
        payload = json.loads(self.journal.read_text(encoding="utf-8"))
        payload["target_commit"] = descendant_target
        self.journal.write_text(json.dumps(payload), encoding="utf-8")
        log = (self.root / "update.log").open("ab", buffering=0)
        try:
            handoff = update_controller.spawn_update_controller(
                self.root,
                operation_id=self.claimed.state.active_operation.operation_id,
                generation=self.claimed.state.generation,
                target_commit=descendant_target,
                previous_commit=self.previous,
                automatic=False,
                install_mode="when_idle",
                update_branch="main",
                log_handle=log,
            )
        finally:
            log.close()
        self.assertEqual(handoff.process.wait(timeout=5), 0)
        child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
        self.assertTrue(Path(f"/proc/{child_pid}").exists())
        capability = state.load_controller_capability(
            self.root,
            operation_id=handoff.operation_id,
            generation=handoff.generation,
        )
        replacement = state.acquire_controller_lease(
            self.root,
            operation_id=handoff.operation_id,
            generation=handoff.generation,
            expected_phase="reserved",
            owner_capability=capability,
        )
        replacement.release()
        try:
            os.kill(child_pid, 15)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    unittest.main()
