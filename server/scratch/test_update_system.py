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

    async def test_detached_controller_cannot_have_terminal_state_overwritten_by_queue_handoff(self) -> None:
        target = "e" * 40
        current = "a" * 40
        script = Path(self.temporary.name) / "update.sh"
        script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        update.write_update_state(
            phase="queued",
            message="Queued",
            current_commit=current,
            target_commit=target,
            update_available=False,
            automatic=False,
            install_mode="now",
        )

        class FinishedController:
            async def wait(self) -> int:
                self_status = update.read_update_state()
                if self_status["phase"] != "preflight":
                    raise AssertionError("preflight state must be persisted before the controller is launched")
                update.write_update_state(
                    phase="failed",
                    message="Controller reported a terminal failure.",
                    error="preflight_failed",
                    finished_at=time.time(),
                )
                return 0

        with (
            patch.object(update, "UPDATE_SCRIPT", script),
            patch.object(update.os, "name", "posix"),
            patch.object(update, "current_commit", AsyncMock(return_value=current)),
            patch.object(update, "queued_launch_blockers", AsyncMock(return_value=[])),
            patch.object(
                update.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=FinishedController()),
            ),
        ):
            self.assertTrue(await update.launch_queued_update_if_ready())

        persisted = update.read_update_state()
        self.assertEqual(persisted["phase"], "failed")
        self.assertEqual(persisted["error"], "preflight_failed")

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
