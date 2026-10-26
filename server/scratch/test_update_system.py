from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import settings
from lifecycle import state as lifecycle_state
from lifecycle import update_controller
from lifecycle.coordination import EffectClass, RuntimeCoordinator
from routes import update as update_route
from services import state
from services import update


REAL_CURRENT_COMMIT = update.current_commit
REAL_RUN_GIT_CMD = update.run_git_cmd


class UpdateSystemTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary.name) / "StreamHome"
        (self.workspace_root / "server").mkdir(parents=True)
        self.run_dir = self.workspace_root / ".run"
        self.status_path = self.run_dir / "update-state.json"
        self.status_lock_path = self.run_dir / "update-state.lock"
        self.log_path = self.workspace_root / "update.log"
        for script_name in ("start.sh", "update.sh"):
            (self.workspace_root / script_name).write_text(
                "#!/usr/bin/env bash\n",
                encoding="utf-8",
            )
        self.patches = [
            patch.object(update, "RUN_DIR", self.run_dir),
            patch.object(update, "STATUS_PATH", self.status_path),
            patch.object(update, "STATUS_LOCK_PATH", self.status_lock_path),
            patch.object(update, "LOG_PATH", self.log_path),
            patch.object(update, "WORKSPACE_ROOT", self.workspace_root),
            patch.object(update, "START_SCRIPT", self.workspace_root / "start.sh"),
            patch.object(update, "UPDATE_SCRIPT", self.workspace_root / "update.sh"),
            patch.object(update, "current_commit", AsyncMock(return_value="a" * 40)),
            patch.object(update, "UPDATE_QUEUE_LOCK", asyncio.Lock()),
            patch.object(update, "UPDATE_CUTOVER_LOCK", asyncio.Lock()),
        ]
        for active_patch in self.patches:
            active_patch.start()
        self.original_settings = (
            settings.SETUP_COMPLETE,
            settings.AUTO_UPDATE_ENABLED,
            settings.UPDATE_CHECK_INTERVAL_HOURS,
            settings.UPDATE_IDLE_MINUTES,
            settings.UPDATE_MAINTENANCE_START,
            settings.UPDATE_MAINTENANCE_END,
            settings.UPDATE_REQUIRE_SIGNED_COMMITS,
            settings.UPDATE_TRUSTED_SIGNERS,
        )
        self.original_update_guard = (
            state.MAINTENANCE_MODE,
            state.MAINTENANCE_REASON,
            state.UPDATE_COMMIT_TOKEN,
            state.UPDATE_TRANSACTION_ID,
        )
        state.ACTIVE_HTTP_REQUESTS = 0
        state.LAST_HTTP_ACTIVITY_TIMESTAMP = 0
        state.ACTIVE_PROCESSES.clear()
        state.BROWSER_PRESENCE.clear()
        update.queue_manager.active_tasks.clear()
        settings.SETUP_COMPLETE = True

    def tearDown(self) -> None:
        (
            settings.SETUP_COMPLETE,
            settings.AUTO_UPDATE_ENABLED,
            settings.UPDATE_CHECK_INTERVAL_HOURS,
            settings.UPDATE_IDLE_MINUTES,
            settings.UPDATE_MAINTENANCE_START,
            settings.UPDATE_MAINTENANCE_END,
            settings.UPDATE_REQUIRE_SIGNED_COMMITS,
            settings.UPDATE_TRUSTED_SIGNERS,
        ) = self.original_settings
        (
            state.MAINTENANCE_MODE,
            state.MAINTENANCE_REASON,
            state.UPDATE_COMMIT_TOKEN,
            state.UPDATE_TRANSACTION_ID,
        ) = self.original_update_guard
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temporary.cleanup()

    def test_update_state_is_atomic_and_log_tail_is_bounded(self) -> None:
        written = update.write_update_state(
            phase="update_available",
            message="Ready",
            current_commit="a" * 40,
            target_commit="b" * 40,
            update_available=True,
        )
        self.assertEqual(written["phase"], "update_available")
        self.assertEqual(json.loads(self.status_path.read_text(encoding="utf-8"))["target_commit"], "b" * 40)
        self.assertEqual(list(self.run_dir.glob("*.tmp")), [])

        self.log_path.write_text("\n".join(f"line-{number}" for number in range(250)), encoding="utf-8")
        tail = update.read_update_log(80)
        self.assertEqual(len(tail), 80)
        self.assertEqual(tail[-1], "line-249")

    async def test_checking_phase_blocks_installing_a_stale_target(self) -> None:
        update.write_update_state(
            phase="checking",
            target_commit="b" * 40,
            update_available=True,
        )
        with self.assertRaisesRegex(RuntimeError, "update_in_progress"):
            await update.queue_update(automatic=False)

    async def test_active_preflight_cancellation_creates_a_transaction_request(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="update",
            owner_kind="updater",
        )
        transaction = claimed.state.active_operation.operation_id
        update.write_update_state(
            phase="preflight",
            target_commit="b" * 40,
            transaction_id=transaction,
            lifecycle_generation=claimed.state.generation,
            lifecycle_phase="reserved",
        )
        result = await update.cancel_queued_update()
        self.assertEqual(result["phase"], "preflight")
        self.assertIn("Cancellation requested", result["message"])
        self.assertTrue((self.run_dir / f"update-cancel.{transaction}.requested").is_file())

    async def test_signed_policy_verifies_every_commit_against_the_allowlist(self) -> None:
        current = "a" * 40
        first = "b" * 40
        target = "c" * 40
        settings.UPDATE_REQUIRE_SIGNED_COMMITS = True
        settings.UPDATE_TRUSTED_SIGNERS = "TRUSTED-FINGERPRINT"

        async def fake_git(args: list[str], *unused_args, **unused_kwargs):
            if args[:2] == ["rev-list", "--reverse"]:
                return 0, f"{first}\n{target}", ""
            if args[0] == "verify-commit":
                return 0, "", ""
            if args[:3] == ["show", "-s", "--format=%GF"]:
                return 0, "TRUSTED-FINGERPRINT", ""
            raise AssertionError(f"Unexpected Git command: {args}")

        with patch.object(update, "run_git_cmd", AsyncMock(side_effect=fake_git)) as git:
            valid, error = await update.verify_update_commit_range(current, target)
        self.assertTrue(valid, error)
        verified = [call.args[0][1] for call in git.await_args_list if call.args[0][0] == "verify-commit"]
        self.assertEqual(verified, [first, target])

    def test_local_runtime_probe_rejects_a_web_listener_with_the_wrong_build(self) -> None:
        target = "b" * 40

        class FakeResponse:
            def __init__(self, body: bytes, headers: dict[str, str]) -> None:
                self.status = 200
                self.body = body
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *unused_args):
                return False

            def read(self) -> bytes:
                return self.body

        class FakeOpener:
            def __init__(self) -> None:
                self.responses = iter(
                    [
                        FakeResponse(
                            json.dumps(
                                {
                                    "status": "ready",
                                    "buildId": target[:12],
                                    "updateTransaction": "tx-probe",
                                }
                            ).encode(),
                            {},
                        ),
                        FakeResponse(b"<html></html>", {"X-StreamHome-Web-Build": "wrong-build"}),
                    ]
                )

            def open(self, *unused_args, **unused_kwargs):
                return next(self.responses)

        with patch.object(update.urllib.request, "build_opener", return_value=FakeOpener()):
            self.assertFalse(update._probe_local_runtime(target, "tx-probe"))

    async def test_runtime_commit_is_durable_before_maintenance_is_released(self) -> None:
        transaction = "commit-transaction"
        target = "c" * 40
        token = "commit-token"
        state.MAINTENANCE_MODE = True
        state.MAINTENANCE_REASON = "guarded"
        state.UPDATE_COMMIT_TOKEN = token
        state.UPDATE_TRANSACTION_ID = transaction

        def persist_commit(**changes):
            self.assertTrue(state.MAINTENANCE_MODE)
            self.assertTrue(changes["runtime_committed"])
            return changes

        with (
            patch.object(
                update_route,
                "read_update_state_strict",
                return_value={
                    "phase": "starting",
                    "transaction_id": transaction,
                    "target_commit": target,
                    "lifecycle_generation": 7,
                    "lifecycle_phase": "verifying",
                },
            ),
            patch.object(
                update_route,
                "write_authoritative_update_state",
                side_effect=persist_commit,
            ) as persisted,
            patch.object(settings, "BUILD_ID", target[:12]),
            patch.object(update_route, "require_current_guarded_authority"),
            patch.object(update_route, "get_startup_readiness", return_value=object()),
            patch.object(
                update_route,
                "current_readiness_projection",
                return_value={"guardedReady": True},
            ),
            patch.object(
                update_route,
                "require_startup_context",
                return_value=SimpleNamespace(
                    generation=7,
                    lifecycle_phase="verifying",
                    operation_type="update",
                    owner_kind="updater",
                ),
            ),
        ):
            result = await update_route.commit_updated_runtime(
                update_route.UpdateCommitRequest(transaction_id=transaction, target_commit=target),
                SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
                token,
            )

        self.assertTrue(result["committed"])
        self.assertTrue(result["recoveryPending"])
        persisted.assert_called_once()

    async def test_protected_commit_never_mutates_before_current_guarded_readiness(self) -> None:
        transaction = str(uuid.uuid4())
        target = "c" * 40
        state.UPDATE_COMMIT_TOKEN = "commit-token"
        state.UPDATE_TRANSACTION_ID = transaction
        state.MAINTENANCE_MODE = True
        with (
            patch.object(update_route, "require_current_guarded_authority"),
            patch.object(update_route, "get_startup_readiness", return_value=object()),
            patch.object(
                update_route,
                "current_readiness_projection",
                return_value={"guardedReady": False},
            ),
            patch.object(update_route, "read_update_state_strict") as read_journal,
            patch.object(update_route, "write_authoritative_update_state") as mutate_journal,
            patch.object(
                update_route,
                "require_startup_context",
                return_value=SimpleNamespace(
                    generation=7,
                    lifecycle_phase="verifying",
                    operation_type="update",
                    owner_kind="updater",
                ),
            ),
        ):
            with self.assertRaises(update_route.HTTPException):
                await update_route.commit_updated_runtime(
                    update_route.UpdateCommitRequest(
                        transaction_id=transaction,
                        target_commit=target,
                    ),
                    SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
                    "commit-token",
                )
        read_journal.assert_not_called()
        mutate_journal.assert_not_called()
        self.assertTrue(state.MAINTENANCE_MODE)
        self.assertEqual(state.UPDATE_TRANSACTION_ID, transaction)
        self.assertEqual(state.UPDATE_COMMIT_TOKEN, "commit-token")

    async def test_stale_guarded_authority_cannot_mutate_update_journal(self) -> None:
        transaction = "failed-recovery-transaction"
        target = "d" * 40
        token = "failed-recovery-token"
        state.MAINTENANCE_MODE = True
        state.MAINTENANCE_REASON = "guarded"
        state.UPDATE_COMMIT_TOKEN = token
        state.UPDATE_TRANSACTION_ID = transaction

        with (
            patch.object(update_route, "read_update_state_strict") as read_journal,
            patch.object(update_route, "write_authoritative_update_state") as persisted,
            patch.object(settings, "BUILD_ID", target[:12]),
            patch.object(
                update_route,
                "require_current_guarded_authority",
                side_effect=update_route.StartupAuthorityChangedError(
                    "stale guarded authority"
                ),
            ),
            patch.object(
                update_route,
                "require_startup_context",
                return_value=SimpleNamespace(
                    generation=7,
                    lifecycle_phase="verifying",
                    operation_type="update",
                    owner_kind="updater",
                ),
            ),
        ):
            with self.assertRaises(update_route.HTTPException) as raised:
                await update_route.commit_updated_runtime(
                    update_route.UpdateCommitRequest(
                        transaction_id=transaction,
                        target_commit=target,
                    ),
                    SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
                    token,
                )

        self.assertEqual(raised.exception.status_code, 503)
        read_journal.assert_not_called()
        persisted.assert_not_called()
        self.assertEqual(state.UPDATE_TRANSACTION_ID, transaction)
        self.assertEqual(state.UPDATE_COMMIT_TOKEN, token)

    async def test_protected_commit_requires_verifying_lifecycle_phase(self) -> None:
        transaction = str(uuid.uuid4())
        target = "d" * 40
        token = "wrong-phase-token"
        state.UPDATE_COMMIT_TOKEN = token
        state.UPDATE_TRANSACTION_ID = transaction
        with (
            patch.object(update_route, "require_current_guarded_authority"),
            patch.object(update_route, "get_startup_readiness", return_value=object()),
            patch.object(
                update_route,
                "current_readiness_projection",
                return_value={"guardedReady": True},
            ),
            patch.object(
                update_route,
                "read_update_state_strict",
                return_value={
                    "phase": "starting",
                    "transaction_id": transaction,
                    "target_commit": target,
                    "lifecycle_generation": 7,
                    "lifecycle_phase": "exclusive",
                },
            ),
            patch.object(update_route, "write_authoritative_update_state") as mutate_journal,
            patch.object(settings, "BUILD_ID", target[:12]),
            patch.object(
                update_route,
                "require_startup_context",
                return_value=SimpleNamespace(
                    generation=7,
                    lifecycle_phase="exclusive",
                    operation_type="update",
                    owner_kind="updater",
                ),
            ),
        ):
            with self.assertRaises(update_route.HTTPException):
                await update_route.commit_updated_runtime(
                    update_route.UpdateCommitRequest(
                        transaction_id=transaction,
                        target_commit=target,
                    ),
                    SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
                    token,
                )
        mutate_journal.assert_not_called()

    async def test_indeterminate_commit_persistence_keeps_guarded_runtime_closed(self) -> None:
        transaction = "indeterminate-commit-transaction"
        target = "e" * 40
        token = "indeterminate-commit-token"
        state.MAINTENANCE_MODE = True
        state.MAINTENANCE_REASON = "guarded"
        state.UPDATE_COMMIT_TOKEN = token
        state.UPDATE_TRANSACTION_ID = transaction

        with (
            patch.object(
                update_route,
                "read_update_state_strict",
                return_value={
                    "phase": "starting",
                    "transaction_id": transaction,
                    "target_commit": target,
                    "lifecycle_generation": 7,
                    "lifecycle_phase": "verifying",
                },
            ),
            patch.object(
                update_route,
                "write_authoritative_update_state",
                side_effect=OSError("simulated directory fsync uncertainty"),
            ),
            patch.object(settings, "BUILD_ID", target[:12]),
            patch.object(update_route, "require_current_guarded_authority"),
            patch.object(update_route, "get_startup_readiness", return_value=object()),
            patch.object(
                update_route,
                "current_readiness_projection",
                return_value={"guardedReady": True},
            ),
            patch.object(
                update_route,
                "require_startup_context",
                return_value=SimpleNamespace(
                    generation=7,
                    lifecycle_phase="verifying",
                    operation_type="update",
                    owner_kind="updater",
                ),
            ),
        ):
            with self.assertRaises(update_route.HTTPException) as raised:
                await update_route.commit_updated_runtime(
                    update_route.UpdateCommitRequest(
                        transaction_id=transaction,
                        target_commit=target,
                    ),
                    SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
                    token,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertTrue(state.MAINTENANCE_MODE)
        self.assertEqual(state.UPDATE_TRANSACTION_ID, transaction)
        self.assertEqual(state.UPDATE_COMMIT_TOKEN, token)

    def test_maintenance_window_supports_daytime_and_overnight_ranges(self) -> None:
        settings.UPDATE_MAINTENANCE_START = ""
        settings.UPDATE_MAINTENANCE_END = ""
        self.assertTrue(update.maintenance_window_open(datetime(2026, 7, 28, 12, 0)))

        settings.UPDATE_MAINTENANCE_START = "02:00"
        settings.UPDATE_MAINTENANCE_END = "04:00"
        self.assertTrue(update.maintenance_window_open(datetime(2026, 7, 28, 3, 0)))
        self.assertFalse(update.maintenance_window_open(datetime(2026, 7, 28, 5, 0)))

        settings.UPDATE_MAINTENANCE_START = "23:00"
        settings.UPDATE_MAINTENANCE_END = "02:00"
        self.assertTrue(update.maintenance_window_open(datetime(2026, 7, 28, 23, 30)))
        self.assertTrue(update.maintenance_window_open(datetime(2026, 7, 29, 1, 30)))
        self.assertFalse(update.maintenance_window_open(datetime(2026, 7, 29, 12, 0)))

    async def test_idle_detection_fails_closed_for_browser_http_and_media_activity(self) -> None:
        settings.UPDATE_IDLE_MINUTES = 10
        state.record_browser_presence("session-1", True)
        state.LAST_HTTP_ACTIVITY_TIMESTAMP = time.time()
        state.ACTIVE_HTTP_REQUESTS = 1
        state.ACTIVE_PROCESSES["task-1"] = object()  # type: ignore[assignment]

        with patch.object(update, "is_database_idle", AsyncMock(return_value=False)):
            blockers = await update.idle_blockers()

        self.assertTrue(any("browser session" in blocker for blocker in blockers))
        self.assertTrue(any("active API request" in blocker for blocker in blockers))
        self.assertTrue(any("required idle minutes" in blocker for blocker in blockers))
        self.assertTrue(any("active media operation" in blocker for blocker in blockers))
        self.assertTrue(any("playback, ingestion, or download" in blocker for blocker in blockers))

        state.record_browser_presence("session-1", False)
        state.LAST_HTTP_ACTIVITY_TIMESTAMP = time.time() - 11 * 60
        state.ACTIVE_HTTP_REQUESTS = 0
        state.ACTIVE_PROCESSES.clear()
        with patch.object(update, "is_database_idle", AsyncMock(return_value=True)):
            self.assertEqual(await update.idle_blockers(), [])

    def test_managed_updates_refuse_a_concurrent_public_installer(self) -> None:
        workspace = Path(self.temporary.name) / "StreamHome"
        workspace.mkdir(exist_ok=True)
        lock_path = workspace.with_name(f"{workspace.name}.install.lock")
        lock_path.mkdir()
        (lock_path / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")

        with patch.object(update, "WORKSPACE_ROOT", workspace):
            self.assertTrue(update.install_lock_active())

    async def test_status_reports_the_checked_out_commit_over_stale_persisted_state(self) -> None:
        actual = "c" * 40
        update.write_update_state(
            phase="failed",
            message="Previous update failed",
            current_commit="a" * 40,
            target_commit="b" * 40,
            error="pre_activation_interrupted",
        )

        with (
            patch.object(update_route, "current_commit", AsyncMock(return_value=actual)),
            patch.object(update_route, "idle_blockers", AsyncMock(return_value=[])),
            patch.object(update_route, "read_update_log", return_value=[]),
            patch.object(update_route, "update_lock_active", return_value=False),
        ):
            response = await update_route._status_response()

        self.assertEqual(response.current_commit, actual)

    async def test_failed_target_requires_an_explicit_manual_retry(self) -> None:
        target = "b" * 40
        update.write_update_state(
            phase="failed",
            message="Suppressed",
            current_commit="a" * 40,
            target_commit=target,
            update_available=False,
            failed_target=target,
            error="failed_target_suppressed",
        )
        with self.assertRaisesRegex(RuntimeError, "failed_target_suppressed"):
            await update.queue_update(automatic=True)

        queued = await update.queue_update(automatic=False, allow_failed_target=True)
        self.assertEqual(queued["phase"], "queued")
        self.assertFalse(queued["automatic"])

    async def test_immediate_mode_ignores_viewer_idle_but_waits_for_protected_work(self) -> None:
        target = "d" * 40
        update.write_update_state(
            phase="update_available",
            message="Ready",
            current_commit="a" * 40,
            target_commit=target,
            update_available=True,
        )
        queued = await update.queue_update(automatic=False, install_mode="now")
        self.assertEqual(queued["install_mode"], "now")
        self.assertIn("Immediate update requested", queued["message"])

        with (
            patch.object(update, "idle_blockers", AsyncMock(return_value=["1 active browser session"])) as idle,
            patch.object(update, "protected_cutover_blockers", AsyncMock(return_value=[])) as protected,
        ):
            self.assertEqual(await update.queued_launch_blockers(queued), [])
            idle.assert_not_awaited()
            protected.assert_awaited_once()

        with patch.object(update, "protected_cutover_blockers", AsyncMock(return_value=["1 active ingestion or download task"])):
            self.assertEqual(
                await update.queued_launch_blockers(queued),
                ["1 active ingestion or download task"],
            )

        with self.assertRaisesRegex(RuntimeError, "invalid_install_mode"):
            await update.queue_update(automatic=False, install_mode="force")

    async def test_queue_reserves_exact_shared_lifecycle_ownership(self) -> None:
        target = "d" * 40
        update.write_update_state(
            phase="update_available",
            target_commit=target,
            update_available=True,
            previous_commit="f" * 40,
            staging_dir="/stale/path",
            diagnostic_id="stale-diagnostic",
            recovery_requested_at=123.0,
        )
        queued = await update.queue_update(automatic=False)
        authority = lifecycle_state.inspect_state(self.workspace_root)
        operation = authority.active_operation
        self.assertIsNotNone(operation)
        self.assertEqual(operation.operation_type, lifecycle_state.OperationType.UPDATE)
        self.assertEqual(operation.operation_id, queued["transaction_id"])
        self.assertEqual(authority.generation, queued["lifecycle_generation"])
        self.assertEqual(queued["lifecycle_phase"], "reserved")
        self.assertEqual(queued["previous_commit"], "a" * 40)
        self.assertEqual(queued["staging_dir"], "")
        self.assertEqual(queued["diagnostic_id"], "")
        self.assertIsNone(queued["recovery_requested_at"])
        credential = lifecycle_state.lifecycle_paths(self.workspace_root).credential_path(operation.operation_id)
        raw_capability = credential.read_text(encoding="ascii").strip()
        self.assertNotIn(raw_capability, self.status_path.read_text(encoding="utf-8"))

    async def test_real_queue_producer_survives_restart_before_controller_launch(self) -> None:
        def git(*arguments: str) -> str:
            return subprocess.run(["git", "-C", str(self.workspace_root), *arguments],
                check=True, capture_output=True, text=True).stdout.strip()
        git("init")
        git("config", "user.name", "Isolated Test")
        git("config", "user.email", "isolated@example.invalid")
        git("add", "start.sh", "update.sh")
        git("commit", "-m", "Initial isolated installation")
        previous = git("rev-parse", "HEAD")
        (self.workspace_root / "target.txt").write_text("candidate\n", encoding="utf-8")
        git("add", "target.txt")
        git("commit", "-m", "Isolated target")
        target = git("rev-parse", "HEAD")
        git("checkout", "--detach", previous)
        update.write_update_state(phase="update_available", target_commit=target, update_available=True)
        with (patch.object(update, "current_commit", REAL_CURRENT_COMMIT),
              patch.object(update, "run_git_cmd", new=lambda args, **kwargs: REAL_RUN_GIT_CMD(args, cwd=self.workspace_root, **kwargs))):
            queued = await update.queue_update(automatic=False)
        self.assertEqual(queued["previous_commit"], previous)
        self.assertEqual(queued["current_commit"], previous)
        recovered = update_controller._recover_identity(self.workspace_root)
        self.assertEqual(recovered[:4], (queued["transaction_id"], queued["lifecycle_generation"], target, previous))
        self.assertTrue(recovered[-1])
        self.assertEqual(lifecycle_state.inspect_state(self.workspace_root).active_operation.phase,
            lifecycle_state.LifecyclePhase.RESERVED)

    async def test_queued_launch_rejects_changed_installed_baseline(self) -> None:
        update.write_update_state(phase="update_available", target_commit="b" * 40, update_available=True)
        queued = await update.queue_update(automatic=False)
        self.assertEqual(queued["previous_commit"], "a" * 40)
        with (patch.object(update, "current_commit", AsyncMock(return_value="c" * 40)),
              patch.object(update, "queued_launch_blockers", AsyncMock(return_value=[])),
              patch.object(update, "spawn_update_controller") as launch):
            self.assertFalse(await update.launch_queued_update_if_ready())
        launch.assert_not_called()
        self.assertEqual(update.read_update_state()["error"], "invalid_update_state")

    async def test_new_exact_update_transaction_may_reset_prior_commit_boundary(self) -> None:
        previous_transaction = str(uuid.uuid4())
        target = "d" * 40
        update.write_update_state(
            phase="succeeded",
            transaction_id=previous_transaction,
            target_commit="c" * 40,
            runtime_committed=True,
            runtime_committed_at=time.time(),
        )
        update.write_update_state(
            phase="update_available",
            target_commit=target,
            update_available=True,
        )
        self.assertTrue(update.read_update_state_strict()["runtime_committed"])
        queued = await update.queue_update(automatic=False)
        self.assertNotEqual(queued["transaction_id"], previous_transaction)
        self.assertFalse(queued["runtime_committed"])
        authority = lifecycle_state.inspect_state(self.workspace_root)
        self.assertEqual(authority.active_operation.operation_id, queued["transaction_id"])

    async def test_competing_lifecycle_operation_blocks_update_reservation(self) -> None:
        update.write_update_state(
            phase="update_available",
            target_commit="d" * 40,
            update_available=True,
        )
        lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="update",
            owner_kind="updater",
        )
        with self.assertRaisesRegex(RuntimeError, "lifecycle_conflict"):
            await update.queue_update(automatic=False)

    async def test_cancel_releases_only_its_exact_lifecycle_operation(self) -> None:
        update.write_update_state(
            phase="update_available",
            target_commit="d" * 40,
            update_available=True,
        )
        queued = await update.queue_update(automatic=False)
        cancelled = await update.cancel_queued_update()
        self.assertEqual(cancelled["phase"], "update_available")
        authority = lifecycle_state.inspect_state(self.workspace_root)
        self.assertIsNone(authority.active_operation)
        self.assertEqual(authority.generation, queued["lifecycle_generation"])

    async def test_cancel_cannot_release_an_exactly_referenced_restore_operation(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = claimed.state.active_operation
        payload = update._default_status()
        payload.update(
            {
                "phase": "queued",
                "target_commit": "d" * 40,
                "transaction_id": operation.operation_id,
                "lifecycle_generation": claimed.state.generation,
                "lifecycle_phase": "reserved",
            }
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "update_not_cancellable"):
            await update.cancel_queued_update()
        current = lifecycle_state.inspect_state(self.workspace_root)
        self.assertEqual(current.active_operation.operation_type, lifecycle_state.OperationType.RESTORE)
        self.assertEqual(current.active_operation.operation_id, operation.operation_id)

    async def test_discovery_cannot_overwrite_unresolved_shared_authority(self) -> None:
        update.write_update_state(
            phase="succeeded",
            target_commit="d" * 40,
            runtime_committed=True,
            runtime_committed_at=time.time(),
        )
        before = self.status_path.read_bytes()
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        with (
            self.assertRaisesRegex(RuntimeError, "update_in_progress"),
            patch.object(update, "current_commit", AsyncMock()) as current_commit,
        ):
            await update.check_for_update_details()
        current_commit.assert_not_awaited()
        self.assertEqual(self.status_path.read_bytes(), before)
        self.assertEqual(
            lifecycle_state.inspect_state(self.workspace_root).active_operation.operation_id,
            claimed.state.active_operation.operation_id,
        )

    async def test_automatic_worker_cannot_overwrite_unresolved_shared_authority(self) -> None:
        settings.AUTO_UPDATE_ENABLED = True
        settings.UPDATE_CHECK_INTERVAL_HOURS = 1
        update.write_update_state(
            phase="succeeded",
            target_commit="d" * 40,
            runtime_committed=True,
            runtime_committed_at=time.time(),
            last_checked_at=0,
        )
        before = self.status_path.read_bytes()
        lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        stop = asyncio.Event()
        task = asyncio.create_task(update.automatic_update_worker(stop, initial_delay_seconds=0))
        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(self.status_path.read_bytes(), before)

    async def test_cancel_during_controller_owned_preflight_signals_without_releasing(self) -> None:
        update.write_update_state(
            phase="update_available",
            target_commit="d" * 40,
            update_available=True,
        )
        queued = await update.queue_update(automatic=False)
        with patch.object(update, "shared_update_controller_active", return_value=True):
            cancelled = await update.cancel_queued_update()
        self.assertEqual(cancelled["phase"], "queued")
        self.assertTrue(
            (self.run_dir / f"update-cancel.{queued['transaction_id']}.requested").is_file()
        )
        self.assertIsNotNone(lifecycle_state.inspect_state(self.workspace_root).active_operation)

    def test_update_reconciliation_never_releases_restore_authority(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        update.cleanup_reconciled_transaction(
            {
                "transaction_id": claimed.state.active_operation.operation_id,
                "lifecycle_generation": claimed.state.generation,
                "lifecycle_phase": "reserved",
            }
        )
        authority = lifecycle_state.inspect_state(self.workspace_root)
        self.assertEqual(authority.active_operation.operation_type, lifecycle_state.OperationType.RESTORE)

    async def test_cutover_snapshot_is_not_cancelled_and_exact_fence_is_reopened(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        update.write_update_state(
            phase="waiting_for_idle",
            previous_commit="a" * 40,
            transaction_id=operation.operation_id,
            lifecycle_generation=claimed.state.generation,
            lifecycle_phase="reserved",
        )
        permit = update_controller.create_cutover_permit(
            self.workspace_root,
            operation.operation_id,
            claimed.state.generation,
        )
        token = json.loads(permit.read_text(encoding="utf-8"))["token"]
        coordinator = RuntimeCoordinator()
        snapshot_lease = coordinator.admit("backup.snapshot", [EffectClass.DATABASE_SNAPSHOT])
        with (
            patch.object(update, "get_runtime_coordinator", return_value=coordinator),
            patch.object(update, "UPDATE_CUTOVER_DRAIN_SECONDS", 0.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "cutover_busy"):
                await update.prepare_update_cutover(
                    operation_id=operation.operation_id,
                    generation=claimed.state.generation,
                    cutover_token=token,
                )
        self.assertFalse(snapshot_lease.cancellation_requested)
        admitted = coordinator.admit("post-rejection.write", [EffectClass.DB_WRITE])
        admitted.release()
        snapshot_lease.release()

    async def test_cutover_and_cancellation_have_one_serialized_winner(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        update.write_update_state(
            phase="waiting_for_idle",
            target_commit="d" * 40,
            previous_commit="a" * 40,
            runtime_committed=False,
            transaction_id=operation.operation_id,
            lifecycle_generation=claimed.state.generation,
            lifecycle_phase="reserved",
        )
        permit = update_controller.create_cutover_permit(
            self.workspace_root,
            operation.operation_id,
            claimed.state.generation,
        )
        token = json.loads(permit.read_text(encoding="utf-8"))["token"]
        coordinator = RuntimeCoordinator()
        await update.UPDATE_CUTOVER_LOCK.acquire()
        with (
            patch.object(update, "shared_update_controller_active", return_value=True),
            patch.object(update, "get_runtime_coordinator", return_value=coordinator),
        ):
            cancel_task = asyncio.create_task(update.cancel_queued_update())
            await asyncio.sleep(0)
            cutover_task = asyncio.create_task(
                update.prepare_update_cutover(
                    operation_id=operation.operation_id,
                    generation=claimed.state.generation,
                    cutover_token=token,
                )
            )
            await asyncio.sleep(0)
            update.UPDATE_CUTOVER_LOCK.release()
            cancelled = await cancel_task
            with self.assertRaisesRegex(RuntimeError, "update_cancelled"):
                await cutover_task
        self.assertIn("Cancellation requested", cancelled["message"])
        self.assertEqual(
            lifecycle_state.inspect_state(self.workspace_root).active_operation.phase,
            lifecycle_state.LifecyclePhase.RESERVED,
        )

    async def test_orphaned_fenced_runtime_is_not_completed_while_still_alive(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        update.write_update_state(
            phase="waiting_for_idle",
            target_commit="d" * 40,
            previous_commit="c" * 40,
            failed_target="",
            runtime_committed=False,
            transaction_id=operation.operation_id,
            lifecycle_generation=claimed.state.generation,
            lifecycle_phase="reserved",
            controller_pid=999999,
            controller_start_ticks="1",
        )
        lifecycle_state.transition_operation_with_credential(
            self.workspace_root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="exclusive",
        )
        update.write_authoritative_update_state(
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_lifecycle_phase="exclusive",
            expected_detail_phases=frozenset({"waiting_for_idle"}),
            phase="stopping",
        )

        async def runtime_ready(commit: str, unused_transaction: str = "") -> bool:
            return commit == "c" * 40

        with (
            patch.object(update, "current_commit", AsyncMock(return_value="c" * 40)),
            patch.object(update, "local_runtime_ready", AsyncMock(side_effect=runtime_ready)),
            patch.object(update, "orphaned_controller_stale", return_value=True),
            patch.object(update.asyncio, "create_subprocess_exec", AsyncMock()) as launch,
        ):
            self.assertTrue(await update.reconcile_orphaned_update())
        launch.assert_awaited_once()
        current = lifecycle_state.inspect_state(self.workspace_root)
        self.assertIsNotNone(current.active_operation)
        self.assertEqual(current.active_operation.phase, lifecycle_state.LifecyclePhase.EXCLUSIVE)
        self.assertEqual(update.read_update_state()["phase"], "recovering")

    async def test_cutover_winner_makes_later_cancellation_non_cancellable(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        update.write_update_state(
            phase="waiting_for_idle",
            target_commit="d" * 40,
            previous_commit="a" * 40,
            runtime_committed=False,
            transaction_id=operation.operation_id,
            lifecycle_generation=claimed.state.generation,
            lifecycle_phase="reserved",
        )
        permit = update_controller.create_cutover_permit(
            self.workspace_root,
            operation.operation_id,
            claimed.state.generation,
        )
        token = json.loads(permit.read_text(encoding="utf-8"))["token"]
        coordinator = RuntimeCoordinator()
        await update.UPDATE_CUTOVER_LOCK.acquire()
        with (
            patch.object(update, "shared_update_controller_active", return_value=True),
            patch.object(update, "get_runtime_coordinator", return_value=coordinator),
        ):
            cutover_task = asyncio.create_task(
                update.prepare_update_cutover(
                    operation_id=operation.operation_id,
                    generation=claimed.state.generation,
                    cutover_token=token,
                )
            )
            await asyncio.sleep(0)
            cancel_task = asyncio.create_task(update.cancel_queued_update())
            await asyncio.sleep(0)
            update.UPDATE_CUTOVER_LOCK.release()
            snapshot = await cutover_task
            with self.assertRaisesRegex(RuntimeError, "update_not_cancellable"):
                await cancel_task
        self.assertFalse(
            (self.run_dir / f"update-cancel.{operation.operation_id}.requested").exists()
        )
        self.assertEqual(
            lifecycle_state.inspect_state(self.workspace_root).active_operation.phase,
            lifecycle_state.LifecyclePhase.EXCLUSIVE,
        )
        coordinator.release_fence(snapshot)

    def test_indeterminate_journal_write_preserves_an_adopted_claim(self) -> None:
        claimed = update._reserve_update_lifecycle()
        operation = claimed.active_operation
        update.write_update_state(
            transaction_id=operation.operation_id,
            lifecycle_generation=claimed.generation,
            lifecycle_phase="reserved",
        )
        update._release_unjournaled_update_claim(claimed)
        self.assertEqual(
            lifecycle_state.inspect_state(self.workspace_root).active_operation.operation_id,
            operation.operation_id,
        )

    def test_unjournaled_claim_is_released_after_definite_write_failure(self) -> None:
        claimed = update._reserve_update_lifecycle()
        payload = update._default_status()
        payload.update(
            {
                "transaction_id": str(uuid.uuid4()),
                "lifecycle_generation": 999,
                "lifecycle_phase": "reserved",
            }
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(payload), encoding="utf-8")
        update._release_unjournaled_update_claim(claimed)
        self.assertIsNone(lifecycle_state.inspect_state(self.workspace_root).active_operation)

    def test_unreadable_or_corrupt_journal_preserves_unjournaled_claim(self) -> None:
        for failure in ("unreadable", "corrupt"):
            with self.subTest(failure=failure):
                claimed = update._reserve_update_lifecycle()
                if failure == "unreadable":
                    context = patch.object(
                        update,
                        "_read_update_state_strict_unlocked",
                        side_effect=update.UpdateJournalReadError("simulated read failure"),
                    )
                else:
                    self.run_dir.mkdir(parents=True, exist_ok=True)
                    self.status_path.write_text("{", encoding="utf-8")
                    context = patch.object(
                        update,
                        "_read_update_state_strict_unlocked",
                        wraps=update._read_update_state_strict_unlocked,
                    )
                with context:
                    update._release_unjournaled_update_claim(claimed)
                self.assertEqual(
                    lifecycle_state.inspect_state(self.workspace_root).active_operation.operation_id,
                    claimed.active_operation.operation_id,
                )
                lifecycle_state.complete_operation_with_credential(
                    self.workspace_root,
                    operation_id=claimed.active_operation.operation_id,
                    generation=claimed.generation,
                    expected_phase="reserved",
                )
                self.status_path.unlink(missing_ok=True)

    def test_post_replace_journal_fsync_uncertainty_preserves_adopted_claim(self) -> None:
        claimed = update._reserve_update_lifecycle()
        operation = claimed.active_operation
        with patch.object(update.os, "fsync", side_effect=[None, OSError("simulated directory fsync failure")]):
            with self.assertRaises(OSError):
                update.write_update_state(
                    transaction_id=operation.operation_id,
                    lifecycle_generation=claimed.generation,
                    lifecycle_phase="reserved",
                )
        update._release_unjournaled_update_claim(claimed)
        self.assertEqual(
            lifecycle_state.inspect_state(self.workspace_root).active_operation.operation_id,
            operation.operation_id,
        )

    async def test_wrong_generation_and_terminal_detail_never_clear_shared_authority(self) -> None:
        update.write_update_state(
            phase="update_available",
            target_commit="d" * 40,
            update_available=True,
        )
        queued = await update.queue_update(automatic=False)
        payload = update.read_update_state_strict()
        payload["phase"] = "succeeded"
        self.status_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNotNone(lifecycle_state.inspect_state(self.workspace_root).active_operation)
        wrong = dict(queued)
        wrong["lifecycle_generation"] += 1
        self.assertFalse(update._release_update_lifecycle({"phase": "succeeded"}))
        partial = dict(queued)
        partial["lifecycle_phase"] = ""
        self.assertFalse(update._release_update_lifecycle(partial))
        with self.assertRaises(lifecycle_state.LifecycleConflictError):
            update._release_update_lifecycle(wrong)
        self.assertIsNotNone(lifecycle_state.inspect_state(self.workspace_root).active_operation)

    def test_recovery_required_update_releases_only_with_recorded_phase(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        lifecycle_state.transition_operation_with_credential(
            self.workspace_root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        status = {
            "transaction_id": operation.operation_id,
            "lifecycle_generation": claimed.state.generation,
            "lifecycle_phase": "recovery_required",
        }
        self.assertTrue(update._release_update_lifecycle(status))
        self.assertIsNone(lifecycle_state.inspect_state(self.workspace_root).active_operation)

    def test_cleanup_only_failure_does_not_make_lifecycle_release_fail(self) -> None:
        claimed = lifecycle_state.claim_operation_with_credential(
            self.workspace_root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        status = {
            "transaction_id": operation.operation_id,
            "lifecycle_generation": claimed.state.generation,
            "lifecycle_phase": "reserved",
        }
        with patch.object(
            lifecycle_state,
            "_delete_credential",
            side_effect=lifecycle_state.PersistenceError("simulated cleanup failure"),
        ):
            self.assertTrue(update._release_update_lifecycle(status))
        self.assertIsNone(lifecycle_state.inspect_state(self.workspace_root).active_operation)

    def test_first_phase_one_adoption_is_explicitly_legacy_protected(self) -> None:
        script = (Path(__file__).resolve().parents[2] / "scripts/runtime/update.sh").read_text(encoding="utf-8")
        self.assertIn("installed_release_lifecycle_support", script)
        self.assertIn("PRE_PHASE1_CONFIRMED", script)
        self.assertIn("PHASE1_CONFIRMED", script)
        self.assertIn("INDETERMINATE", script)
        self.assertIn("writes no shared lifecycle state", script)
        self.assertIn('git -C "$ROOT_DIR" cat-file -e "$OLD_COMMIT^{commit}"', script)

    async def test_official_update_script_is_validated_and_launched_through_fenced_controller(self) -> None:
        target = "e" * 40
        current = "a" * 40
        update_script = self.workspace_root / "update.sh"
        update_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        update_script.chmod(0o644)
        update.write_update_state(
            phase="update_available",
            current_commit=current,
            target_commit=target,
            update_available=True,
        )
        queued = await update.queue_update(automatic=False, install_mode="now")
        handoff = SimpleNamespace(
            process=SimpleNamespace(pid=4321),
            operation_id=queued["transaction_id"],
            generation=queued["lifecycle_generation"] + 1,
        )
        controller_leases = []

        def transfer_controller(*unused_args, **unused_kwargs):
            lease, transferred = lifecycle_state.acquire_transferred_controller_lease(
                self.workspace_root,
                operation_id=queued["transaction_id"],
                generation=queued["lifecycle_generation"],
                expected_phase="reserved",
                owner_capability=lifecycle_state.load_controller_capability(
                    self.workspace_root,
                    operation_id=queued["transaction_id"],
                    generation=queued["lifecycle_generation"],
                ),
                next_owner_kind="updater",
            )
            controller_leases.append(lease)
            self.assertEqual(transferred.state.generation, handoff.generation)
            update_controller._record_transferred_generation(self.workspace_root,
                operation_id=queued["transaction_id"], previous_generation=queued["lifecycle_generation"],
                generation=handoff.generation, lifecycle_phase=lifecycle_state.LifecyclePhase.RESERVED,
                target_commit=target, previous_commit=current)
            update_controller.mutate_update_journal(self.workspace_root,
                operation_id=queued["transaction_id"], generation=handoff.generation,
                expected_lifecycle_phase="reserved", changes={"controller_pid": 4321})
            return handoff

        try:
            with (
                patch.object(update.os, "name", "posix"),
                patch.object(update, "UPDATE_SCRIPT", update_script),
                patch.object(update, "current_commit", AsyncMock(return_value=current)),
                patch.object(update, "queued_launch_blockers", AsyncMock(return_value=[])),
                patch.object(update, "verify_update_commit_range", AsyncMock(return_value=(True, ""))),
                patch.object(update.os, "chmod") as chmod,
                patch.object(update, "spawn_update_controller", side_effect=transfer_controller) as launch,
            ):
                self.assertTrue(await update.launch_queued_update_if_ready())
        finally:
            for lease in controller_leases:
                lease.release()

        persisted = update.read_update_state()
        self.assertEqual(persisted["phase"], "queued")
        self.assertEqual(persisted["controller_pid"], 4321)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.kwargs["operation_id"], queued["transaction_id"])
        self.assertEqual(launch.call_args.kwargs["generation"], queued["lifecycle_generation"])
        updater_chmod = next(call for call in chmod.call_args_list if Path(call.args[0]) == update_script)
        self.assertEqual(updater_chmod.args[1] & 0o111, 0o111)
        self.assertEqual(update.read_update_state()["lifecycle_generation"], handoff.generation)

    async def test_orphaned_target_is_reconciled_without_rollback_when_both_services_are_healthy(self) -> None:
        target = "f" * 40
        previous = "a" * 40
        update.write_update_state(
            phase="starting",
            message="Starting",
            current_commit=target,
            target_commit=target,
            previous_commit=previous,
            transaction_id="tx-healthy-target",
            updated_at=time.time() - 60,
        )
        with (
            patch.object(update, "update_lock_active", return_value=False),
            patch.object(update, "orphaned_controller_stale", return_value=True),
            patch.object(update, "current_commit", AsyncMock(return_value=target)),
            patch.object(update, "local_runtime_ready", AsyncMock(return_value=True)),
            patch.object(update, "commit_guarded_runtime", AsyncMock(return_value=True)),
            patch.object(update, "cleanup_reconciled_transaction") as cleanup,
        ):
            self.assertTrue(await update.reconcile_orphaned_update())

        persisted = update.read_update_state()
        self.assertEqual(persisted["phase"], "succeeded")
        self.assertEqual(persisted["current_commit"], target)
        cleanup.assert_called_once()

    async def test_manually_recovered_failed_target_is_reconciled_only_after_local_health(self) -> None:
        target = "f" * 40
        update.write_update_state(
            phase="rollback_failed",
            message="Rollback failed",
            current_commit="a" * 40,
            target_commit=target,
            previous_commit="a" * 40,
            transaction_id="tx-manual-recovery",
            failed_target=target,
            error="rollback_failed",
        )
        with (
            patch.object(update, "update_lock_active", return_value=False),
            patch.object(update, "current_commit", AsyncMock(return_value=target)),
            patch.object(update, "local_runtime_ready", AsyncMock(return_value=False)),
            patch.object(update, "cleanup_reconciled_transaction") as cleanup,
        ):
            self.assertFalse(await update.reconcile_orphaned_update())
            cleanup.assert_not_called()

        with (
            patch.object(update, "update_lock_active", return_value=False),
            patch.object(update, "current_commit", AsyncMock(return_value=target)),
            patch.object(update, "local_runtime_ready", AsyncMock(return_value=True)),
            patch.object(update, "commit_guarded_runtime", AsyncMock(return_value=True)),
            patch.object(update, "cleanup_reconciled_transaction") as cleanup,
        ):
            self.assertTrue(await update.reconcile_orphaned_update())

        persisted = update.read_update_state()
        self.assertEqual(persisted["phase"], "succeeded")
        self.assertEqual(persisted["current_commit"], target)
        self.assertEqual(persisted["failed_target"], "")
        self.assertEqual(persisted["error"], "")
        self.assertIn("manually recovered", persisted["message"])
        cleanup.assert_called_once()

    async def test_non_lifecycle_failure_is_never_reclassified_as_a_success(self) -> None:
        target = "f" * 40
        update.write_update_state(
            phase="failed",
            message="Fetch failed",
            current_commit=target,
            target_commit=target,
            failed_target=target,
            error="update_fetch_failed",
        )
        with (
            patch.object(update, "update_lock_active", return_value=False),
            patch.object(update, "current_commit", AsyncMock(return_value=target)),
            patch.object(update, "local_runtime_ready", AsyncMock(return_value=True)) as ready,
        ):
            self.assertFalse(await update.reconcile_orphaned_update())
        ready.assert_not_awaited()
        self.assertEqual(update.read_update_state()["phase"], "failed")

    async def test_orphaned_unhealthy_cutover_queues_one_detached_recovery(self) -> None:
        target = "f" * 40
        previous = "a" * 40
        start_script = Path(self.temporary.name) / "start.sh"
        start_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        update.write_update_state(
            phase="installing",
            message="Installing",
            current_commit=target,
            target_commit=target,
            previous_commit=previous,
            transaction_id="tx-unhealthy-target",
            updated_at=time.time() - 60,
        )

        with (
            patch.object(update, "START_SCRIPT", start_script),
            patch.object(update, "update_lock_active", return_value=False),
            patch.object(update, "orphaned_controller_stale", return_value=True),
            patch.object(update, "current_commit", AsyncMock(return_value=target)),
            patch.object(update, "local_runtime_ready", AsyncMock(return_value=False)),
            patch.object(update.asyncio, "create_subprocess_exec", AsyncMock()) as launch,
        ):
            self.assertTrue(await update.reconcile_orphaned_update())
            self.assertFalse(await update.reconcile_orphaned_update())

        launch.assert_awaited_once()
        persisted = update.read_update_state()
        self.assertEqual(persisted["phase"], "recovering")
        self.assertEqual(persisted["error"], "controller_lost")
        self.assertTrue((self.run_dir / "update-recovery.tx-unhealthy-target.requested").is_file())

    async def test_immediate_cutover_ignores_viewers_and_idle_grace_but_not_mutating_work(self) -> None:
        settings.UPDATE_IDLE_MINUTES = 10
        state.record_browser_presence("session-now", True)
        state.LAST_HTTP_ACTIVITY_TIMESTAMP = time.time()
        with patch.object(update, "is_database_idle", AsyncMock(return_value=False)):
            self.assertTrue(any("browser session" in blocker for blocker in await update.idle_blockers()))
        self.assertEqual(await update.protected_cutover_blockers(), [])

        state.ACTIVE_HTTP_REQUESTS = 1
        state.ACTIVE_PROCESSES["media-1"] = object()  # type: ignore[assignment]
        update.queue_manager.active_tasks.add("download-1")
        blockers = await update.protected_cutover_blockers()
        self.assertTrue(any("active API request" in blocker for blocker in blockers))
        self.assertTrue(any("active media operation" in blocker for blocker in blockers))
        self.assertTrue(any("ingestion or download" in blocker for blocker in blockers))

    async def test_update_check_reports_fetch_failure_instead_of_no_update(self) -> None:
        current = "a" * 40

        async def fake_git(args: list[str], cwd: Path = update.WORKSPACE_ROOT):
            del cwd
            if args == ["rev-parse", "HEAD"]:
                return 0, current, ""
            if args == ["rev-parse", "--is-shallow-repository"]:
                return 0, "false", ""
            if args == ["fetch", "--prune", "origin", settings.UPDATE_BRANCH]:
                return 1, "", "network unavailable"
            raise AssertionError(f"Unexpected Git command: {args}")

        with (
            patch.object(update, "initialize_remote", AsyncMock(return_value=True)),
            patch.object(update, "is_git_clean", AsyncMock(return_value=True)),
            patch.object(update, "run_git_cmd", fake_git),
        ):
            result = await update.check_for_update_details()

        self.assertEqual(result["phase"], "failed")
        self.assertEqual(result["error"], "update_fetch_failed")
        self.assertFalse(result["update_available"])

    async def test_worker_never_launches_a_suppressed_failed_target(self) -> None:
        target = "c" * 40
        settings.AUTO_UPDATE_ENABLED = True
        update.write_update_state(
            phase="failed",
            message="Suppressed",
            current_commit="a" * 40,
            target_commit=target,
            update_available=False,
            failed_target=target,
            error="failed_target_suppressed",
            last_checked_at=0,
        )
        stop = asyncio.Event()

        async def check_once():
            stop.set()
            return update.read_update_state()

        with (
            patch.object(update, "check_for_update_details", AsyncMock(side_effect=check_once)),
            patch.object(update, "launch_queued_update_if_ready", AsyncMock()) as launch,
        ):
            await update.automatic_update_worker(stop, initial_delay_seconds=0)

        launch.assert_not_awaited()

    async def test_update_housekeeping_never_releases_unrelated_maintenance(self) -> None:
        for automatic_updates in (False, True):
            for phase in ("idle", "failed", "succeeded", "rolled_back"):
                with self.subTest(automatic_updates=automatic_updates, phase=phase):
                    settings.AUTO_UPDATE_ENABLED = automatic_updates
                    state.MAINTENANCE_MODE = True
                    state.MAINTENANCE_REASON = "Restore owns maintenance."
                    state.UPDATE_TRANSACTION_ID = ""
                    update.write_update_state(
                        phase=phase,
                        message="Terminal update state",
                        update_available=False,
                        last_checked_at=time.time(),
                    )
                    stop = asyncio.Event()
                    reconcile_calls = 0

                    async def reconcile_once() -> bool:
                        nonlocal reconcile_calls
                        reconcile_calls += 1
                        if reconcile_calls > 1:
                            stop.set()
                        return False

                    with (
                        patch.object(update, "reconcile_orphaned_update", AsyncMock(side_effect=reconcile_once)),
                        patch.object(update, "check_for_update_details", AsyncMock()) as check,
                        patch.object(update, "launch_queued_update_if_ready", AsyncMock()) as launch,
                    ):
                        await update.automatic_update_worker(stop, initial_delay_seconds=0)

                    self.assertTrue(state.MAINTENANCE_MODE)
                    self.assertEqual(state.MAINTENANCE_REASON, "Restore owns maintenance.")
                    check.assert_not_awaited()
                    launch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
