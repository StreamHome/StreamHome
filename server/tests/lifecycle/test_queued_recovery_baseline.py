"""Actual Git/producer/recovery and pre-cutover baseline regression boundaries."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from lifecycle import bootstrap, recovery, state, update_controller
from lifecycle.coordination import EffectClass, RuntimeCoordinator, use_runtime_coordinator_for_testing
from services import update


class QueuedRecoveryBaselineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-queued-baseline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "StreamHome"
        (self.root / "server").mkdir(parents=True)
        (self.root / ".gitignore").write_text(".run/\nupdate.log\n")
        for name in ("start.sh", "update.sh"):
            (self.root / name).write_text("#!/usr/bin/env bash\nexit 0\n")
            (self.root / name).chmod(0o700)
        (self.root / "release").write_text("A\n")
        self.git("init")
        self.git("config", "user.name", "Synthetic Update Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic installed A")
        self.previous = self.git("rev-parse", "HEAD")
        (self.root / "release").write_text("B\n")
        self.git("add", "release")
        self.git("commit", "-m", "Synthetic target B")
        self.target = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", self.previous)
        (self.root / "release").write_text("C\n")
        self.git("add", "release")
        self.git("commit", "-m", "Synthetic independent installed C")
        self.changed = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", self.previous)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # The fixture has no runtime. Do not probe canonical ports 8000/3000 to
        # infer that fact; retain actual authority, Git, CLI and execution paths.
        self.stack.enter_context(patch.object(recovery, "_runtime_status", return_value="offline"))
        original_git = update.run_git_cmd

        async def fixture_git(args, cwd=None, timeout_seconds=120):
            # Keep real subprocess/Git parsing. Only the installation argument is
            # redirected to this nested disposable synthetic repository.
            return await original_git(args, cwd=self.root, timeout_seconds=timeout_seconds)

        for name, value in {
            "WORKSPACE_ROOT": self.root,
            "RUN_DIR": self.root / ".run",
            "STATUS_PATH": self.root / ".run/update-state.json",
            "STATUS_LOCK_PATH": self.root / ".run/update-state.lock",
            "LOG_PATH": self.root / "update.log",
            "START_SCRIPT": self.root / "start.sh",
            "UPDATE_SCRIPT": self.root / "update.sh",
            "UPDATE_QUEUE_LOCK": asyncio.Lock(),
            "UPDATE_CUTOVER_LOCK": asyncio.Lock(),
            "run_git_cmd": fixture_git,
        }.items():
            self.stack.enter_context(patch.object(update, name, value))
        self.journal = self.root / ".run/update-state.json"

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    async def queue(self) -> dict:
        update.write_update_state(
            phase="update_available", target_commit=self.target, update_available=True,
        )
        queued = await update.queue_update(automatic=False)
        self.assertEqual(queued["previous_commit"], self.previous)
        self.assertEqual(queued["current_commit"], self.previous)
        self.assertEqual(self.git("status", "--porcelain"), "")
        return queued

    @contextmanager
    def inert_controller(self):
        """Intercept only native updater launch; real Git Popen remains active."""
        launches = []
        original_popen = subprocess.Popen

        class InertProcess:
            def wait(self, **kwargs):
                return 0

            def poll(self):
                return 0

        def popen(command, *args, **kwargs):
            name = Path(str(command[0])).name
            if name == "update.sh" or name.startswith("update-controller."):
                launches.append(command)
                return InertProcess()
            return original_popen(command, *args, **kwargs)

        with patch.object(update_controller.subprocess, "Popen", side_effect=popen):
            yield launches

    def controller(self, identity) -> int:
        operation_id, generation, target, previous, automatic, mode, branch, queued = identity
        return update_controller._run_controller(
            self.root, operation_id=operation_id, generation=generation,
            target_commit=target, previous_commit=previous, automatic=automatic,
            install_mode=mode, update_branch=branch, acknowledgement_fd=None,
            recovery=not queued,
        )

    def assert_rejected_evidence(self, before):
        original = json.loads(before)
        normalized = update_controller.read_update_journal(self.journal)
        self.assertEqual(normalized["lifecycle_phase"], "recovery_required")
        original["lifecycle_phase"] = "recovery_required"
        self.assertEqual(normalized, original)

    async def test_actual_queued_recovery_rejects_changed_clean_head_before_popen(self) -> None:
        queued = await self.queue()
        self.git("checkout", "--detach", self.changed)
        before = self.journal.read_bytes()
        with self.inert_controller() as launches:
            self.assertEqual(update_controller.main(["recover", "--root", str(self.root)]), 1)
        self.assertEqual(launches, [])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assert_rejected_evidence(before)
        authority = state.inspect_state(self.root)
        self.assertEqual(authority.active_operation.operation_id, queued["transaction_id"])
        self.assertEqual(authority.generation, queued["lifecycle_generation"])
        self.assertFalse(list((self.root / ".run").glob("update-controller.*.sh")))
        self.assertFalse((self.root / ".run/pre-update-database.db").exists())

    async def test_unchanged_actual_baseline_reaches_inert_target_controller(self) -> None:
        await self.queue()
        with self.inert_controller() as launches:
            self.assertEqual(update_controller.main(["recover", "--root", str(self.root)]), 0)
        self.assertEqual(len(launches), 1)
        self.assertIn("--manual-execute", launches[0])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.previous)

    async def test_unreadable_installed_head_rejects_recovery_preserving_evidence(self) -> None:
        queued = await self.queue()
        before = self.journal.read_bytes()
        (self.root / ".git/HEAD").write_text("invalid HEAD\n")
        with self.inert_controller() as launches:
            self.assertEqual(update_controller.main(["recover", "--root", str(self.root)]), 1)
        self.assertEqual(launches, [])
        self.assert_rejected_evidence(before)
        self.assertEqual(state.inspect_state(self.root).generation, queued["lifecycle_generation"])
        self.assertEqual((self.root / ".git/HEAD").read_text(), "invalid HEAD\n")

    async def test_change_after_identity_admission_rejects_before_owner_transfer(self) -> None:
        queued = await self.queue()
        identity = update_controller._recover_identity(self.root)
        before = self.journal.read_bytes()
        self.git("checkout", "--detach", self.changed)
        with self.inert_controller() as launches:
            with self.assertRaises(update_controller.UpdateControllerConflict):
                self.controller(identity)
        self.assertEqual(launches, [])
        self.assert_rejected_evidence(before)
        self.assertEqual(state.inspect_state(self.root).generation, queued["lifecycle_generation"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)

    async def test_change_during_target_materialization_rejects_before_popen(self) -> None:
        queued = await self.queue()
        identity = update_controller._recover_identity(self.root)
        original = update_controller._materialize_target_controller

        def materialize(*args, **kwargs):
            path = original(*args, **kwargs)
            self.git("checkout", "--detach", self.changed)
            return path

        with self.inert_controller() as launches, patch.object(
            update_controller, "_materialize_target_controller", side_effect=materialize,
        ):
            with self.assertRaises(update_controller.UpdateControllerConflict):
                self.controller(identity)
        self.assertEqual(launches, [])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)
        payload = update_controller.read_update_journal(self.journal)
        self.assertEqual(payload["previous_commit"], self.previous)
        self.assertEqual(payload["target_commit"], self.target)
        self.assertEqual(payload["transaction_id"], queued["transaction_id"])
        self.assertEqual(payload["lifecycle_generation"], state.inspect_state(self.root).generation)
        self.assertFalse(payload["runtime_committed"])

    async def test_accepted_target_recovery_keeps_target_instead_of_requiring_old_head(self) -> None:
        queued = await self.queue()
        for previous, following in (("reserved", "exclusive"), ("exclusive", "verifying")):
            state.transition_operation_with_credential(
                self.root, operation_id=queued["transaction_id"],
                generation=queued["lifecycle_generation"],
                expected_phase=previous, next_phase=following,
            )
        payload = json.loads(self.journal.read_text())
        payload.update(phase="recovering", lifecycle_phase="verifying",
                       runtime_committed=True, runtime_committed_at=time.time(),
                       current_commit=self.target)
        self.journal.write_text(json.dumps(payload))
        self.git("checkout", "--detach", self.target)
        identity = update_controller._recover_identity(self.root)
        self.assertFalse(identity[-1])
        with self.inert_controller() as launches:
            self.assertEqual(self.controller(identity), 0)
        self.assertEqual(len(launches), 1)
        self.assertIn("--recover-interrupted", launches[0])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)
        self.assertTrue(update_controller.read_update_journal(self.journal)["runtime_committed"])

    async def test_legacy_blank_previous_has_only_unchanged_current_head_adoption(self) -> None:
        await self.queue()
        payload = json.loads(self.journal.read_text())
        payload["previous_commit"] = ""
        self.journal.write_text(json.dumps(payload))
        self.assertEqual(update_controller._recover_identity(self.root)[3], self.previous)
        before = self.journal.read_bytes()
        self.git("checkout", "--detach", self.changed)
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._recover_identity(self.root)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(json.loads(before)["previous_commit"], "")

    async def preflight(self):
        queued = await self.queue()
        update.write_authoritative_update_state(
            operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
            expected_lifecycle_phase="reserved", expected_detail_phases=frozenset({"queued"}),
            phase="preflight",
        )
        update.write_authoritative_update_state(
            operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
            expected_lifecycle_phase="reserved", expected_detail_phases=frozenset({"preflight"}),
            phase="waiting_for_idle",
        )
        update_controller.create_cutover_permit(
            self.root, queued["transaction_id"], queued["lifecycle_generation"],
        )
        _, token = update_controller._read_cutover_permit(
            self.root, queued["transaction_id"], queued["lifecycle_generation"],
        )
        return queued, token

    async def cutover(self, queued, token):
        return await update.prepare_update_cutover(
            operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
            cutover_token=token,
        )

    def assert_admission_open(self, coordinator):
        lease = coordinator.admit("test.after-rejection", [EffectClass.DB_WRITE])
        lease.release()

    async def test_cutover_rejects_changed_baseline_before_closing_admission(self) -> None:
        queued, token = await self.preflight()
        self.git("checkout", "--detach", self.changed)
        before = self.journal.read_bytes()
        coordinator = RuntimeCoordinator()
        with use_runtime_coordinator_for_testing(coordinator), patch.object(
            coordinator, "close_admission", wraps=coordinator.close_admission,
        ) as close:
            with self.assertRaisesRegex(RuntimeError, "invalid_installed_commit"):
                await self.cutover(queued, token)
        close.assert_not_called()
        self.assert_admission_open(coordinator)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RESERVED)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)

    async def test_cutover_rechecks_actual_head_after_real_drain_and_reopens_on_change(self) -> None:
        queued, token = await self.preflight()
        before = self.journal.read_bytes()
        coordinator = RuntimeCoordinator()
        lease = coordinator.admit("test.outstanding-writer", [EffectClass.DB_WRITE])
        closed = asyncio.Event()
        original_close = coordinator.close_admission

        def close(*args, **kwargs):
            snapshot = original_close(*args, **kwargs)
            closed.set()
            return snapshot

        with use_runtime_coordinator_for_testing(coordinator), patch.object(
            coordinator, "close_admission", side_effect=close,
        ):
            task = asyncio.create_task(self.cutover(queued, token))
            await asyncio.wait_for(closed.wait(), 5)
            self.assertFalse(task.done())
            self.git("checkout", "--detach", self.changed)
            lease.release()
            with self.assertRaisesRegex(RuntimeError, "invalid_installed_commit"):
                await task
        self.assert_admission_open(coordinator)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RESERVED)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)

    async def test_unchanged_preflight_cutover_advances_exact_authority(self) -> None:
        queued, token = await self.preflight()
        coordinator = RuntimeCoordinator()
        with use_runtime_coordinator_for_testing(coordinator):
            snapshot = await self.cutover(queued, token)
        self.assertEqual(snapshot.fence.operation_id, queued["transaction_id"])
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.EXCLUSIVE)
        self.assertEqual(update_controller.read_update_journal(self.journal)["phase"], "stopping")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.previous)

    async def test_foreign_cutover_token_never_reaches_git_or_fence(self) -> None:
        queued, token = await self.preflight()
        before = self.journal.read_bytes()
        with patch.object(update, "current_commit", wraps=update.current_commit) as head:
            with self.assertRaises(update_controller.UpdateControllerConflict):
                await self.cutover(queued, token + "invalid")
        head.assert_not_called()
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RESERVED)

    async def test_changed_head_has_reachable_explicit_operator_cli_abort_preserving_c(self) -> None:
        queued = await self.queue()
        self.git("checkout", "--detach", self.changed)
        self.assertEqual(update_controller.main(["recover", "--root", str(self.root)]), 1)
        self.assertFalse(bootstrap.evaluate_startup(self.root, {}).imports_application)
        # The actual CLI planner works while normal startup traffic is blocked.
        process = subprocess.run(
            [str(Path(__file__).resolve().parents[3] / "venv/bin/python"),
             "-c", "import sys; from lifecycle import recovery; "
             "recovery._runtime_status = lambda root: 'offline'; "
             "raise SystemExit(recovery.main(sys.argv[1:]))", "--root", str(self.root), "plan",
             "--operation-id", queued["transaction_id"],
             "--generation", str(queued["lifecycle_generation"])],
            check=True, text=True, capture_output=True,
        )
        plan = json.loads(process.stdout)
        self.assertEqual(plan["permittedActions"], ["safe_abort"])
        self.assertFalse(plan["runtimeCommitted"])
        output = io.StringIO()
        with self.inert_controller() as launches, redirect_stdout(output):
            code = recovery.main([
                "--root", str(self.root), "execute",
                "--operation-id", queued["transaction_id"],
                "--generation", str(queued["lifecycle_generation"]),
                "--action", "safe_abort", "--evidence-identity", plan["evidenceIdentity"],
            ])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["resultCode"], "operator_safe_abort_completed")
        self.assertEqual(launches, [])
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertTrue(bootstrap.evaluate_startup(self.root, {}).imports_application)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)
        self.assertEqual(self.git("status", "--porcelain"), "")
        retained = update_controller.read_update_journal(self.journal)
        self.assertEqual(retained["previous_commit"], self.previous)
        self.assertEqual(retained["target_commit"], self.target)
        self.assertEqual(retained["transaction_id"], queued["transaction_id"])
        self.assertEqual(retained["error"], "operator_safe_abort")

    async def test_ambiguous_launch_or_effect_evidence_never_offers_changed_head_abort(self) -> None:
        queued = await self.queue()
        self.git("checkout", "--detach", self.changed)
        self.assertEqual(update_controller.main(["recover", "--root", str(self.root)]), 1)
        original = self.journal.read_bytes()
        adverse_fields = (
            {"staging_dir": str(self.root.parent / ".streamhome-update.unknown")},
            {"web_artifacts_swapped": True}, {"web_dependencies_swapped": True},
            {"started_at": time.time()}, {"controller_pid": 123456},
            {"controller_start_ticks": "123"}, {"current_commit": self.changed},
            {"staging_dir": {}}, {"controller_start_ticks": {}},
            {"phase": "preflight"},
        )
        for changes in adverse_fields:
            with self.subTest(changes=changes):
                payload = json.loads(original)
                payload.update(changes)
                self.journal.write_text(json.dumps(payload))
                plan = recovery.plan_recovery(
                    self.root, operation_id=queued["transaction_id"],
                    generation=queued["lifecycle_generation"],
                )
                self.assertEqual(plan["permittedActions"], [])
        self.journal.write_bytes(original)
        for name in ("pre-update-database.db", "update-transaction.json",
                     f"update-controller.{queued['transaction_id']}.sh"):
            with self.subTest(artifact=name):
                path = self.root / ".run" / name
                path.write_bytes(b"ambiguous evidence")
                plan = recovery.plan_recovery(
                    self.root, operation_id=queued["transaction_id"],
                    generation=queued["lifecycle_generation"],
                )
                self.assertEqual(plan["permittedActions"], [])
                path.unlink()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)

    async def test_operator_abort_stale_installed_identity_never_releases_claim(self) -> None:
        queued = await self.queue()
        self.git("checkout", "--detach", self.changed)
        self.assertEqual(update_controller.main(["recover", "--root", str(self.root)]), 1)
        plan = recovery.plan_recovery(
            self.root, operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
        )
        self.git("checkout", "--detach", self.previous)
        with self.assertRaises(recovery.RecoveryEvidenceStale):
            recovery.execute_recovery(
                self.root, operation_id=queued["transaction_id"],
                generation=queued["lifecycle_generation"], action="safe_abort",
                evidence_identity=plan["evidenceIdentity"],
            )
        self.assertEqual(state.inspect_state(self.root).active_operation.operation_id, queued["transaction_id"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.previous)

    async def test_current_controller_or_unknown_runtime_keeps_abort_fail_closed(self) -> None:
        queued = await self.queue()
        self.git("checkout", "--detach", self.changed)
        self.assertEqual(update_controller.main(["recover", "--root", str(self.root)]), 1)
        capability = state.load_controller_capability(
            self.root, operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
        )
        lease = state.acquire_controller_lease(
            self.root, operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
            expected_phase="recovery_required", owner_capability=capability,
        )
        try:
            plan = recovery.plan_recovery(
                self.root, operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
            )
            self.assertEqual(plan["controllerStatus"], "active")
            self.assertEqual(plan["permittedActions"], [])
        finally:
            lease.release()
        with patch.object(recovery, "_runtime_status", return_value="unknown"):
            plan = recovery.plan_recovery(
                self.root, operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
            )
        self.assertEqual(plan["permittedActions"], [])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)

    async def test_failed_phase_normalization_preserves_original_evidence_and_blocks_abort(self) -> None:
        queued = await self.queue()
        self.git("checkout", "--detach", self.changed)
        before = self.journal.read_bytes()
        with patch.object(update_controller, "_private_atomic_write", side_effect=OSError("synthetic journal outage")):
            self.assertEqual(update_controller.main(["recover", "--root", str(self.root)]), 1)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        plan = recovery.plan_recovery(
            self.root, operation_id=queued["transaction_id"], generation=queued["lifecycle_generation"],
        )
        self.assertEqual(plan["permittedActions"], [])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.changed)

    async def test_unreadable_head_at_cutover_keeps_admission_and_authority_reserved(self) -> None:
        queued, token = await self.preflight()
        before = self.journal.read_bytes()
        (self.root / ".git/HEAD").write_text("invalid HEAD\n")
        coordinator = RuntimeCoordinator()
        with use_runtime_coordinator_for_testing(coordinator):
            with self.assertRaisesRegex(RuntimeError, "invalid_installed_commit"):
                await self.cutover(queued, token)
        self.assert_admission_open(coordinator)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RESERVED)


if __name__ == "__main__":
    unittest.main()
