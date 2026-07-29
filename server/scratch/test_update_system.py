from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from config import settings
from services import state
from services import update


class UpdateSystemTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name) / ".run"
        self.status_path = self.run_dir / "update-state.json"
        self.log_path = Path(self.temporary.name) / "update.log"
        self.patches = [
            patch.object(update, "RUN_DIR", self.run_dir),
            patch.object(update, "STATUS_PATH", self.status_path),
            patch.object(update, "LOG_PATH", self.log_path),
        ]
        for active_patch in self.patches:
            active_patch.start()
        self.original_settings = (
            settings.SETUP_COMPLETE,
            settings.AUTO_UPDATE_ENABLED,
            settings.UPDATE_IDLE_MINUTES,
            settings.UPDATE_MAINTENANCE_START,
            settings.UPDATE_MAINTENANCE_END,
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
            settings.UPDATE_IDLE_MINUTES,
            settings.UPDATE_MAINTENANCE_START,
            settings.UPDATE_MAINTENANCE_END,
        ) = self.original_settings
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
        self.assertTrue(any("active media process" in blocker for blocker in blockers))
        self.assertTrue(any("playback, ingestion, or download" in blocker for blocker in blockers))

        state.record_browser_presence("session-1", False)
        state.LAST_HTTP_ACTIVITY_TIMESTAMP = time.time() - 11 * 60
        state.ACTIVE_HTTP_REQUESTS = 0
        state.ACTIVE_PROCESSES.clear()
        with patch.object(update, "is_database_idle", AsyncMock(return_value=True)):
            self.assertEqual(await update.idle_blockers(), [])

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

    async def test_immediate_mode_starts_preflight_without_idle_but_keeps_protected_handoff_blockers(self) -> None:
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

        with patch.object(update, "idle_blockers", AsyncMock(return_value=["1 active browser session"])) as idle:
            self.assertEqual(await update.queued_launch_blockers(queued), [])
            idle.assert_not_awaited()

        with patch.object(update, "protected_cutover_blockers", AsyncMock(return_value=["1 active ingestion or download task"])):
            self.assertEqual(
                await update.update_handoff_blockers("now"),
                ["1 active ingestion or download task"],
            )

        with patch.object(update, "idle_blockers", AsyncMock(return_value=["1 active browser session"])):
            self.assertEqual(
                await update.update_handoff_blockers("when_idle"),
                ["1 active browser session"],
            )

        with self.assertRaisesRegex(RuntimeError, "invalid_install_mode"):
            await update.queue_update(automatic=False, install_mode="force")

    async def test_target_release_controller_is_launched_directly_in_a_new_session(self) -> None:
        target = "e" * 40
        current = "a" * 40
        update.write_update_state(
            phase="queued",
            message="Queued",
            current_commit=current,
            target_commit=target,
            update_available=False,
            automatic=False,
            install_mode="now",
        )

        async def fake_git(args: list[str], cwd: Path = update.WORKSPACE_ROOT):
            del cwd
            if args == ["show", f"{target}:update.sh"]:
                return 0, "#!/usr/bin/env bash\nprintf 'target controller\\n'", ""
            raise AssertionError(f"Unexpected Git command: {args}")

        with (
            patch.object(update.os, "name", "posix"),
            patch.object(update, "current_commit", AsyncMock(return_value=current)),
            patch.object(update, "queued_launch_blockers", AsyncMock(return_value=[])),
            patch.object(update, "run_git_cmd", fake_git),
            patch.object(
                update.asyncio,
                "create_subprocess_exec",
                AsyncMock(),
            ) as launch,
        ):
            self.assertTrue(await update.launch_queued_update_if_ready())

        persisted = update.read_update_state()
        self.assertEqual(persisted["phase"], "preflight")
        launch.assert_awaited_once()
        arguments = launch.await_args.args
        self.assertEqual(arguments[0], "bash")
        self.assertEqual(arguments[2], "--execute")
        self.assertEqual(arguments[3], target)
        self.assertEqual(arguments[4], current)
        self.assertEqual(arguments[5], "false")
        self.assertEqual(Path(arguments[6]).parent, self.run_dir)
        self.assertEqual(Path(arguments[7]), update.WORKSPACE_ROOT)
        self.assertTrue(str(arguments[1]).endswith(".sh"))
        self.assertEqual(launch.await_args.kwargs["start_new_session"], True)
        detached_environment = launch.await_args.kwargs["env"]
        self.assertNotIn("STREAMHOME_INSTANCE_ROOT", detached_environment)
        self.assertNotIn("STREAMHOME_INSTANCE_TOKEN", detached_environment)
        self.assertNotIn("STREAMHOME_SERVICE", detached_environment)
        controller = Path(arguments[1])
        self.assertIn("target controller", controller.read_text(encoding="utf-8"))

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
            patch.object(update, "local_web_ready", AsyncMock(return_value=True)),
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
            patch.object(update, "local_web_ready", AsyncMock(return_value=False)),
            patch.object(update, "cleanup_reconciled_transaction") as cleanup,
        ):
            self.assertFalse(await update.reconcile_orphaned_update())
            cleanup.assert_not_called()

        with (
            patch.object(update, "update_lock_active", return_value=False),
            patch.object(update, "current_commit", AsyncMock(return_value=target)),
            patch.object(update, "local_web_ready", AsyncMock(return_value=True)),
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
            patch.object(update, "local_web_ready", AsyncMock(return_value=True)) as ready,
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
            patch.object(update, "WORKSPACE_ROOT", Path(self.temporary.name)),
            patch.object(update, "update_lock_active", return_value=False),
            patch.object(update, "orphaned_controller_stale", return_value=True),
            patch.object(update, "current_commit", AsyncMock(return_value=target)),
            patch.object(update, "local_web_ready", AsyncMock(return_value=False)),
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
        self.assertTrue(any("active media process" in blocker for blocker in blockers))
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


if __name__ == "__main__":
    unittest.main()
