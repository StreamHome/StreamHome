from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from lifecycle import bootstrap, coordination, state


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "server").mkdir()
        (self.root / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.root / "update.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _environment(self, **values: str) -> dict[str, str]:
        return {bootstrap.INSTANCE_ROOT_ENV: str(self.root), **values}

    def _claim(self, operation_type: str, owner_kind: str) -> state.ClaimResult:
        return state.claim_operation_with_credential(
            self.root,
            operation_type=operation_type,
            owner_kind=owner_kind,
        )

    def test_bootstrap_import_does_not_import_application_modules(self) -> None:
        forbidden = {"config", "db", "main", "application", "models", "services", "routes"}
        self.assertTrue(forbidden.isdisjoint(sys.modules))

    def test_missing_state_is_normal_and_read_only(self) -> None:
        context = bootstrap.evaluate_startup(self.root, {}, install=False)
        self.assertEqual(context.mode, bootstrap.StartupMode.NORMAL)
        self.assertEqual(context.generation, 0)
        self.assertFalse((self.root / ".run").exists())

    def test_update_verify_requires_exact_identity_and_credential(self) -> None:
        claimed = self._claim("update", "updater")
        operation = claimed.state.active_operation
        context = bootstrap.evaluate_startup(
            self.root,
            self._environment(
                STREAMHOME_LIFECYCLE_STARTUP_MODE="update_verify",
                STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
                STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
            ),
            install=False,
        )
        self.assertEqual(context.mode, bootstrap.StartupMode.UPDATE_VERIFY)
        self.assertTrue(context.guarded_identity_validated)
        self.assertEqual(context.allowed_effects, frozenset({"db_write"}))

    def test_restore_verify_requires_exact_identity_and_credential(self) -> None:
        claimed = self._claim("restore", "restore_controller")
        operation = claimed.state.active_operation
        before = {
            path.relative_to(self.root)
            for path in self.root.rglob("*")
        }
        context = bootstrap.evaluate_startup(
            self.root,
            self._environment(
                STREAMHOME_LIFECYCLE_STARTUP_MODE="restore_verify",
                STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
                STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
            ),
            install=False,
        )
        self.assertEqual(context.mode, bootstrap.StartupMode.RESTORE_VERIFY)
        self.assertTrue(context.guarded_identity_validated)
        self.assertNotIn("playback_stream", context.allowed_effects)
        self.assertNotIn("cloud_write", context.allowed_effects)
        self.assertEqual(
            before,
            {path.relative_to(self.root) for path in self.root.rglob("*")},
        )
        self.assertFalse((self.root / "server" / "media").exists())
        self.assertFalse((self.root / "server" / "temp").exists())

    def test_recovery_required_selects_recovery_only(self) -> None:
        claimed = self._claim("update", "updater")
        operation = claimed.state.active_operation
        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        context = bootstrap.evaluate_startup(
            self.root,
            self._environment(
                STREAMHOME_LIFECYCLE_STARTUP_MODE="recovery_only",
                STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
                STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
            ),
            install=False,
        )
        self.assertEqual(context.mode, bootstrap.StartupMode.RECOVERY_ONLY)
        self.assertFalse(context.imports_application)

    def test_wrong_or_missing_guarded_permit_fails_closed(self) -> None:
        claimed = self._claim("update", "updater")
        operation = claimed.state.active_operation
        missing = bootstrap.evaluate_startup(self.root, self._environment(), install=False)
        wrong = bootstrap.evaluate_startup(
            self.root,
            self._environment(
                STREAMHOME_LIFECYCLE_STARTUP_MODE="update_verify",
                STREAMHOME_LIFECYCLE_OPERATION_ID=str(uuid.uuid4()),
                STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
            ),
            install=False,
        )
        self.assertEqual(missing.mode, bootstrap.StartupMode.FAIL_CLOSED)
        self.assertEqual(wrong.mode, bootstrap.StartupMode.FAIL_CLOSED)
        self.assertEqual(wrong.operation_id, operation.operation_id)

    def test_guarded_mode_rejects_wrong_owner_kind_for_operation_type(self) -> None:
        claimed = self._claim("restore", "updater")
        operation = claimed.state.active_operation
        context = bootstrap.evaluate_startup(
            self.root,
            self._environment(
                STREAMHOME_LIFECYCLE_STARTUP_MODE="restore_verify",
                STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
                STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
            ),
            install=False,
        )
        self.assertEqual(context.mode, bootstrap.StartupMode.FAIL_CLOSED)
        self.assertEqual(context.failure_code, "guarded_permit_mismatch")

    def test_wrong_credential_fails_closed_without_leaking_it(self) -> None:
        claimed = self._claim("update", "updater")
        operation = claimed.state.active_operation
        credential = state.lifecycle_paths(self.root).credential_path(operation.operation_id)
        raw = credential.read_text(encoding="ascii").strip()
        credential.write_text("x" * 43 + "\n", encoding="ascii")
        credential.chmod(0o600)
        context = bootstrap.evaluate_startup(
            self.root,
            self._environment(
                STREAMHOME_LIFECYCLE_STARTUP_MODE="update_verify",
                STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
                STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
            ),
            install=False,
        )
        self.assertEqual(context.mode, bootstrap.StartupMode.FAIL_CLOSED)
        self.assertNotIn(raw, repr(context))
        self.assertNotIn(raw, json.dumps(bootstrap.public_startup_projection(context)))

    def test_corrupt_state_fails_closed(self) -> None:
        paths = state.lifecycle_paths(self.root)
        paths.run_dir.mkdir(mode=0o700)
        paths.lifecycle_dir.mkdir(mode=0o700)
        paths.state_path.write_text("{", encoding="utf-8")
        paths.state_path.chmod(0o600)
        context = bootstrap.evaluate_startup(self.root, self._environment(), install=False)
        self.assertEqual(context.mode, bootstrap.StartupMode.FAIL_CLOSED)
        self.assertEqual(context.failure_code, "corrupt_state")

    def test_first_adoption_requires_positive_journal_proof(self) -> None:
        transaction = str(uuid.uuid4())
        run_dir = self.root / ".run"
        run_dir.mkdir(mode=0o700)
        journal = run_dir / "update-state.json"
        journal.write_text(json.dumps({"transaction_id": transaction}), encoding="utf-8")
        environment = self._environment(
            STREAMHOME_LIFECYCLE_STARTUP_MODE="update_verify",
            STREAMHOME_LIFECYCLE_FIRST_ADOPTION="true",
            STREAMHOME_UPDATE_TRANSACTION=transaction,
            STREAMHOME_UPDATE_COMMIT_TOKEN="not-exposed-by-bootstrap-output",
        )
        accepted = bootstrap.evaluate_startup(self.root, environment, install=False)
        self.assertEqual(accepted.mode, bootstrap.StartupMode.UPDATE_VERIFY)
        journal.write_text("{", encoding="utf-8")
        rejected = bootstrap.evaluate_startup(self.root, environment, install=False)
        self.assertEqual(rejected.mode, bootstrap.StartupMode.FAIL_CLOSED)

    async def test_structured_recovery_distinguishes_completion_and_success(self) -> None:
        context = bootstrap.evaluate_startup(self.root, {}, install=False)
        calls: list[str] = []

        async def success() -> None:
            calls.append("success")

        async def failure() -> None:
            calls.append("failure")
            raise RuntimeError("database_unavailable")

        result = await bootstrap.run_mandatory_recovery(
            context,
            (
                bootstrap.MandatoryRecoveryStep("first", success),
                bootstrap.MandatoryRecoveryStep("second", failure),
                bootstrap.MandatoryRecoveryStep("never", success),
            ),
        )
        self.assertTrue(result.completed)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.completed_steps, ("first",))
        self.assertEqual(result.failed_step, "second")
        self.assertEqual(calls, ["success", "failure"])

    async def test_cancelled_recovery_is_never_success(self) -> None:
        context = bootstrap.evaluate_startup(self.root, {}, install=False)

        async def cancelled() -> None:
            raise asyncio.CancelledError()

        result = await bootstrap.run_mandatory_recovery(
            context,
            (bootstrap.MandatoryRecoveryStep("cancelled", cancelled),),
        )
        self.assertTrue(result.completed)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_code, "cancelled")

    async def test_catalog_failure_cannot_unlock_workers_or_general_readiness(self) -> None:
        context = bootstrap.evaluate_startup(self.root, {}, install=False)
        readiness = bootstrap.StartupReadiness(context)

        async def catalog_failure() -> None:
            raise RuntimeError("private catalog diagnostics are not projected")

        result = await bootstrap.run_mandatory_recovery(
            context,
            (
                bootstrap.MandatoryRecoveryStep(
                    "catalog_and_languages",
                    catalog_failure,
                    frozenset(
                        {
                            bootstrap.ReadinessCapability.CATALOG,
                            bootstrap.ReadinessCapability.PLAYBACK,
                        }
                    ),
                ),
            ),
        )
        readiness.record_recovery(result)
        projection = readiness.projection()
        self.assertTrue(result.completed)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_code, "catalog_and_languages_failed")
        self.assertFalse(projection["workersStarted"])
        self.assertFalse(projection["generalReady"])
        self.assertNotIn("private catalog diagnostics", json.dumps(projection))
        with self.assertRaises(RuntimeError):
            readiness.mark_workers_started()

    async def test_readiness_requires_success_and_workers(self) -> None:
        context = bootstrap.evaluate_startup(self.root, {}, install=False)
        readiness = bootstrap.StartupReadiness(context)

        async def success() -> None:
            return None

        result = await bootstrap.run_mandatory_recovery(
            context,
            (
                bootstrap.MandatoryRecoveryStep(
                    "database",
                    success,
                    frozenset({bootstrap.ReadinessCapability.DATABASE}),
                ),
            ),
        )
        readiness.record_recovery(result)
        self.assertFalse(readiness.projection()["generalReady"])
        readiness.mark_worker_start_failed("worker_start_failed")
        failed_projection = readiness.projection()
        self.assertFalse(failed_projection["generalReady"])
        self.assertEqual(failed_projection["errorCode"], "worker_start_failed")
        readiness.mark_workers_started()
        readiness.mark_general_ready()
        projection = readiness.projection()
        self.assertTrue(projection["generalReady"])
        self.assertIn("database", projection["capabilities"])

    async def test_guarded_readiness_does_not_imply_general_readiness(self) -> None:
        claimed = self._claim("restore", "restore_controller")
        operation = claimed.state.active_operation
        context = bootstrap.evaluate_startup(
            self.root,
            self._environment(
                STREAMHOME_LIFECYCLE_STARTUP_MODE="restore_verify",
                STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
                STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
            ),
            install=False,
        )
        readiness = bootstrap.StartupReadiness(context)

        async def success() -> None:
            return None

        result = await bootstrap.run_mandatory_recovery(
            context,
            (bootstrap.MandatoryRecoveryStep("candidate_database", success),),
        )
        readiness.record_recovery(result)
        readiness.mark_guarded_ready()
        projection = readiness.projection()
        self.assertTrue(projection["guardedReady"])
        self.assertFalse(projection["generalReady"])
        self.assertFalse(projection["workersStarted"])

    async def test_restore_general_readiness_remains_private_until_lifecycle_completion(self) -> None:
        claimed = self._claim("restore", "restore_controller")
        operation = claimed.state.active_operation
        environment = self._environment(
            STREAMHOME_LIFECYCLE_STARTUP_MODE="restore_verify",
            STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
            STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
        )
        context = bootstrap.evaluate_startup(self.root, environment, install=False)
        readiness = bootstrap.StartupReadiness(context)

        async def success() -> None:
            return None

        result = await bootstrap.run_mandatory_recovery(
            context,
            (bootstrap.MandatoryRecoveryStep("normal_recovery", success),),
        )
        readiness.record_recovery(result)
        readiness.mark_workers_started()
        readiness.mark_general_ready()
        pending = bootstrap.current_readiness_projection(readiness, environment)
        self.assertFalse(pending["generalReady"])
        self.assertEqual(pending["errorCode"], "restore_lifecycle_pending")

        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        failed = bootstrap.current_readiness_projection(readiness, environment)
        self.assertFalse(failed["generalReady"])
        coordinator = coordination.RuntimeCoordinator()
        completion_fence = coordinator.close_admission(
            list(coordination.EffectClass),
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
        )
        with self.assertRaises(coordination.AdmissionClosedError):
            coordinator.admit("post-restore.writer", [coordination.EffectClass.DB_WRITE])

        state.complete_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="recovery_required",
        )
        completed = bootstrap.current_readiness_projection(readiness, environment)
        self.assertTrue(completed["generalReady"])
        coordinator.release_fence(completion_fence)
        lease = coordinator.admit("post-restore.writer", [coordination.EffectClass.DB_WRITE])
        lease.release()

    async def test_update_general_readiness_remains_private_until_lifecycle_completion(self) -> None:
        claimed = self._claim("update", "updater")
        operation = claimed.state.active_operation
        environment = self._environment(
            STREAMHOME_LIFECYCLE_STARTUP_MODE="update_verify",
            STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
            STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
        )
        context = bootstrap.evaluate_startup(self.root, environment, install=False)
        readiness = bootstrap.StartupReadiness(context)

        async def success() -> None:
            return None

        result = await bootstrap.run_mandatory_recovery(
            context,
            (bootstrap.MandatoryRecoveryStep("normal_recovery", success),),
        )
        readiness.record_recovery(result)
        readiness.mark_workers_started()
        readiness.mark_general_ready()
        pending = bootstrap.current_readiness_projection(readiness, environment)
        self.assertFalse(pending["generalReady"])
        self.assertEqual(pending["errorCode"], "update_lifecycle_pending")

        state.complete_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
        )
        completed = bootstrap.current_readiness_projection(readiness, environment)
        self.assertTrue(completed["generalReady"])

    async def test_guarded_projection_fails_closed_when_authority_changes(self) -> None:
        claimed = self._claim("restore", "restore_controller")
        operation = claimed.state.active_operation
        environment = self._environment(
            STREAMHOME_LIFECYCLE_STARTUP_MODE="restore_verify",
            STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
            STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
        )
        context = bootstrap.evaluate_startup(self.root, environment, install=False)
        readiness = bootstrap.StartupReadiness(context)

        async def success() -> None:
            return None

        result = await bootstrap.run_mandatory_recovery(
            context,
            (bootstrap.MandatoryRecoveryStep("candidate_database", success),),
        )
        readiness.record_recovery(result)
        readiness.mark_guarded_ready()
        self.assertTrue(
            bootstrap.current_readiness_projection(readiness, environment)["guardedReady"]
        )

        state.transition_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        stale = bootstrap.current_readiness_projection(readiness, environment)
        self.assertFalse(stale["guardedReady"])
        self.assertEqual(stale["errorCode"], "startup_authority_changed")

    async def test_guarded_projection_fails_closed_after_completion_or_replacement(self) -> None:
        claimed = self._claim("update", "updater")
        operation = claimed.state.active_operation
        environment = self._environment(
            STREAMHOME_LIFECYCLE_STARTUP_MODE="update_verify",
            STREAMHOME_LIFECYCLE_OPERATION_ID=operation.operation_id,
            STREAMHOME_LIFECYCLE_GENERATION=str(claimed.state.generation),
        )
        context = bootstrap.evaluate_startup(self.root, environment, install=False)
        readiness = bootstrap.StartupReadiness(context)

        async def success() -> None:
            return None

        result = await bootstrap.run_mandatory_recovery(
            context,
            (bootstrap.MandatoryRecoveryStep("candidate_database", success),),
        )
        readiness.record_recovery(result)
        readiness.mark_guarded_ready()
        state.complete_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
        )
        completed = bootstrap.current_readiness_projection(readiness, environment)
        self.assertFalse(completed["guardedReady"])

        replacement = self._claim("update", "updater")
        replaced = bootstrap.current_readiness_projection(readiness, environment)
        self.assertFalse(replaced["guardedReady"])
        self.assertNotEqual(replacement.state.generation, claimed.state.generation)

    def test_lifespan_revalidation_rejects_changed_authority(self) -> None:
        context = bootstrap.evaluate_startup(self.root, self._environment(), install=False)
        self._claim("update", "updater")
        with self.assertRaises(bootstrap.StartupAuthorityChangedError):
            bootstrap.revalidate_startup_context(context, self._environment())

    async def test_minimal_status_application_supports_lifespan_and_liveness(self) -> None:
        context = bootstrap._failure_context(self.root, "corrupt_state")
        application = bootstrap.MinimalStatusApplication(context)
        messages = iter(
            (
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            )
        )
        sent: list[dict] = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await application({"type": "lifespan"}, receive, send)
        self.assertEqual(
            [message["type"] for message in sent],
            ["lifespan.startup.complete", "lifespan.shutdown.complete"],
        )

        sent.clear()
        await application(
            {"type": "http", "path": "/api/health/live"},
            receive,
            send,
        )
        self.assertEqual(sent[0]["status"], 200)
        self.assertNotIn("owner_nonce", sent[-1]["body"].decode("utf-8"))

    def test_unauthorized_direct_main_import_stays_before_application_import(self) -> None:
        self._claim("update", "updater")
        repository = Path(__file__).resolve().parents[3]
        code = (
            "import sys; import main; "
            "print(type(main.app).__name__); "
            "print('config' in sys.modules, 'db' in sys.modules, 'application' in sys.modules)"
        )
        environment = os.environ.copy()
        environment.update(self._environment())
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(repository / "server")
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repository / "server",
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MinimalStatusApplication", result.stdout)
        self.assertIn("False False False", result.stdout)

    def test_corrupt_state_direct_main_import_stays_before_application_import(self) -> None:
        paths = state.lifecycle_paths(self.root)
        paths.run_dir.mkdir(mode=0o700)
        paths.lifecycle_dir.mkdir(mode=0o700)
        paths.state_path.write_text("{", encoding="utf-8")
        paths.state_path.chmod(0o600)
        repository = Path(__file__).resolve().parents[3]
        environment = os.environ.copy()
        environment.update(self._environment())
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(repository / "server")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys,main; print(type(main.app).__name__); "
                "print('config' in sys.modules, 'db' in sys.modules, 'application' in sys.modules)",
            ],
            cwd=repository / "server",
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MinimalStatusApplication", result.stdout)
        self.assertIn("False False False", result.stdout)

    def test_normal_recovery_runs_behind_live_fail_closed_application(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        source = (repository / "server" / "application.py").read_text(encoding="utf-8")
        lifespan = source[
            source.index("async def lifespan(app: FastAPI)"):
            source.index("class StartupModeGateMiddleware")
        ]
        normal = lifespan[
            lifespan.index("if STARTUP_CONTEXT.mode is StartupMode.NORMAL"):
            lifespan.index("else:", lifespan.index("if STARTUP_CONTEXT.mode is StartupMode.NORMAL"))
        ]
        self.assertIn("revalidate_startup_context(STARTUP_CONTEXT)", normal)
        self.assertIn("asyncio.create_task", normal)
        self.assertIn("_complete_normal_startup(runtime)", normal)
        self.assertNotIn("await run_mandatory_recovery", normal)


if __name__ == "__main__":
    unittest.main()
