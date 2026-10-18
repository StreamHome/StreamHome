from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lifecycle import state as lifecycle_state
from scratch.maintenance_server import (
    RecoveryCoordinator,
    MaintenanceServer,
    controller_is_current,
    maintenance_page,
    recovery_needed,
    safe_public_status,
)


class MaintenanceRecoveryTests(unittest.TestCase):
    def qualified_root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "server").mkdir()
        (root / ".run").mkdir(mode=0o700)
        for name in ("start.sh", "update.sh"):
            script = root / name
            script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            script.chmod(0o700)
        return root

    def claim_operation(
        self,
        root: Path,
        operation_type: lifecycle_state.OperationType = lifecycle_state.OperationType.UPDATE,
        phase: lifecycle_state.LifecyclePhase = lifecycle_state.LifecyclePhase.EXCLUSIVE,
    ) -> lifecycle_state.LifecycleState:
        claimed = lifecycle_state.claim_operation_with_credential(
            root,
            operation_type=operation_type,
            owner_kind=(
                lifecycle_state.OwnerKind.UPDATER
                if operation_type is lifecycle_state.OperationType.UPDATE
                else lifecycle_state.OwnerKind.RESTORE_CONTROLLER
            ),
        )
        return lifecycle_state.transition_operation_with_credential(
            root,
            operation_id=claimed.state.active_operation.operation_id,
            generation=claimed.state.generation,
            expected_phase=lifecycle_state.LifecyclePhase.RESERVED,
            next_phase=phase,
        )

    def test_controller_lease_requires_matching_transaction_identity_and_fresh_heartbeat(self) -> None:
        lease = {
            "transaction_id": "tx-one",
            "controller_pid": 123,
            "controller_start_ticks": "456",
            "heartbeat_at": 1_000,
        }
        with patch("scratch.maintenance_server.process_is_alive", return_value=True):
            self.assertTrue(controller_is_current(lease, "tx-one", 30, now=1_010))
            self.assertFalse(controller_is_current(lease, "tx-two", 30, now=1_010))
            self.assertFalse(controller_is_current(lease, "tx-one", 30, now=1_031))

    def test_public_status_changes_abandoned_active_phase_to_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.qualified_root(temporary)
            run_dir = root / ".run"
            authority = self.claim_operation(root)
            operation_id = authority.active_operation.operation_id
            state_path = run_dir / "update-state.json"
            lease_path = run_dir / "update-lease.json"
            state_path.write_text(
                json.dumps(
                    {
                        "phase": "starting",
                        "message": "Starting services.",
                        "transaction_id": "tx-abandoned",
                        "target_commit": "a" * 40,
                        "started_at": time.time() - 12,
                    }
                ),
                encoding="utf-8",
            )
            lease_path.write_text(
                json.dumps(
                    {
                        "transaction_id": "tx-abandoned",
                        "controller_pid": 999_999,
                        "heartbeat_at": time.time() - 60,
                    }
                ),
                encoding="utf-8",
            )

            status = safe_public_status(state_path, lease_path, root, operation_id, 30)

            self.assertEqual(status["phase"], "recovering")
            self.assertFalse(status["controllerActive"])
            self.assertTrue(recovery_needed(status))
            rendered = maintenance_page(status).decode("utf-8")
            self.assertIn("Recovering an interrupted update", rendered)
            self.assertEqual(status["transaction"], operation_id[:12])
            self.assertIn(operation_id[:12], rendered)
            self.assertNotIn(operation_id, rendered)
            self.assertNotIn("tx-abandoned", rendered)
            self.assertNotIn("tx-abandone", rendered)

    def test_recovery_handoff_is_single_use_and_persists_visible_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.qualified_root(temporary)
            run_dir = root / ".run"
            state_path = run_dir / "update-state.json"
            lease_path = run_dir / "update-lease.json"
            state_path.write_text(
                json.dumps({"phase": "installing", "transaction_id": "tx-recover-once"}),
                encoding="utf-8",
            )
            server = SimpleNamespace(shutdown=Mock())
            coordinator = RecoveryCoordinator(root, state_path, lease_path, "tx-recover-once", 30, server)
            status = {
                "phase": "recovering",
                "controllerActive": False,
                "transaction": "tx-recover-once",
            }

            with patch("scratch.maintenance_server.subprocess.Popen") as popen:
                self.assertTrue(coordinator._queue_recovery(status))
                self.assertFalse(coordinator._queue_recovery(status))

            popen.assert_called_once()
            self.assertEqual(Path(popen.call_args.args[0][0]), root / "start.sh")
            server.shutdown.assert_called_once()
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["phase"], "recovering")
            self.assertEqual(persisted["error"], "controller_lost")
            self.assertTrue((run_dir / "update-recovery.tx-recover-once.requested").is_file())

    def test_canonical_handoff_uses_full_internal_identity_with_short_public_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.qualified_root(temporary)
            authority = self.claim_operation(root)
            operation_id = authority.active_operation.operation_id
            state_path = root / ".run/update-state.json"
            lease_path = root / ".run/update-lease.json"
            state_path.write_text(
                json.dumps({"phase": "starting", "transaction_id": operation_id}),
                encoding="utf-8",
            )
            status = safe_public_status(state_path, lease_path, root, operation_id, 30)
            self.assertTrue(recovery_needed(status))
            self.assertEqual(status["transaction"], operation_id[:12])
            server = SimpleNamespace(shutdown=Mock())
            coordinator = RecoveryCoordinator(root, state_path, lease_path, operation_id, 30, server)

            with patch("scratch.maintenance_server.subprocess.Popen") as popen:
                self.assertTrue(coordinator._queue_recovery(status))
                self.assertFalse(coordinator._queue_recovery(status))

            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0], [str(root / "start.sh")])
            server.shutdown.assert_called_once()
            self.assertTrue((root / f".run/update-recovery.{operation_id}.requested").is_file())
            self.assertFalse((root / f".run/update-recovery.{operation_id[:12]}.requested").exists())
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["phase"], "recovering")
            self.assertEqual(lifecycle_state.inspect_state(root), authority)
            public_after = safe_public_status(state_path, lease_path, root, operation_id, 30)
            self.assertEqual(public_after["transaction"], operation_id[:12])
            self.assertNotIn(operation_id, json.dumps(public_after))

    def test_canonical_handoff_rejects_missing_prefix_foreign_restore_and_terminal_identity(self) -> None:
        for case in ("missing", "prefix", "foreign", "restore", "recovery_required"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = self.qualified_root(temporary)
                authority = self.claim_operation(
                    root,
                    operation_type=(
                        lifecycle_state.OperationType.RESTORE
                        if case == "restore"
                        else lifecycle_state.OperationType.UPDATE
                    ),
                    phase=(
                        lifecycle_state.LifecyclePhase.RECOVERY_REQUIRED
                        if case == "recovery_required"
                        else lifecycle_state.LifecyclePhase.EXCLUSIVE
                    ),
                )
                operation_id = authority.active_operation.operation_id
                internal_id = {
                    "missing": "",
                    "prefix": operation_id[:12],
                    "foreign": "00000000-0000-4000-8000-000000000000",
                }.get(case, operation_id)
                state_path = root / ".run/update-state.json"
                lease_path = root / ".run/update-lease.json"
                original = json.dumps({"phase": "starting", "transaction_id": operation_id})
                state_path.write_text(original, encoding="utf-8")
                server = SimpleNamespace(shutdown=Mock())
                coordinator = RecoveryCoordinator(root, state_path, lease_path, internal_id, 30, server)
                # Even a full identity supplied through public status is not authority.
                status = {"phase": "recovering", "controllerActive": False, "transaction": operation_id}

                with patch("scratch.maintenance_server.subprocess.Popen") as popen:
                    self.assertFalse(coordinator._queue_recovery(status))

                popen.assert_not_called()
                server.shutdown.assert_not_called()
                self.assertEqual(state_path.read_text(encoding="utf-8"), original)
                self.assertEqual(lifecycle_state.inspect_state(root), authority)
                self.assertEqual(list((root / ".run").glob("update-recovery.*.requested")), [])

    def test_previous_operation_coordinator_cannot_handoff_a_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.qualified_root(temporary)
            previous = self.claim_operation(root)
            previous_id = previous.active_operation.operation_id
            state_path = root / ".run/update-state.json"
            lease_path = root / ".run/update-lease.json"
            server = SimpleNamespace(shutdown=Mock())
            coordinator = RecoveryCoordinator(root, state_path, lease_path, previous_id, 30, server)
            lifecycle_state.complete_operation_with_credential(
                root, operation_id=previous_id, generation=previous.generation,
                expected_phase=previous.active_operation.phase,
            )
            current = self.claim_operation(root)
            current_id = current.active_operation.operation_id
            self.assertGreater(current.generation, previous.generation)
            state_path.write_text(json.dumps({"phase": "starting", "transaction_id": current_id}), encoding="utf-8")
            status = safe_public_status(state_path, lease_path, root, current_id, 30)

            with patch("scratch.maintenance_server.subprocess.Popen") as popen:
                self.assertFalse(coordinator._queue_recovery(status))

            popen.assert_not_called()
            server.shutdown.assert_not_called()
            self.assertEqual(lifecycle_state.inspect_state(root), current)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["phase"], "starting")

    def test_qualified_idle_legacy_root_needs_no_canonical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.qualified_root(temporary)
            state_path = root / ".run/update-state.json"
            lease_path = root / ".run/update-lease.json"
            self.assertIsNone(lifecycle_state.inspect_state(root).active_operation)
            server = SimpleNamespace(shutdown=Mock())
            coordinator = RecoveryCoordinator(root, state_path, lease_path, "", 30, server)
            status = safe_public_status(state_path, lease_path, root, "", 30)

            with patch("scratch.maintenance_server.subprocess.Popen") as popen:
                self.assertTrue(coordinator._queue_recovery(status))

            popen.assert_called_once()
            server.shutdown.assert_called_once()
            self.assertTrue((root / ".run/update-recovery.legacy.requested").is_file())

    def test_unqualified_root_cannot_handoff_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = SimpleNamespace(shutdown=Mock())
            coordinator = RecoveryCoordinator(
                root, root / ".run/update-state.json", root / ".run/update-lease.json", "", 30, server,
            )
            with patch("scratch.maintenance_server.subprocess.Popen") as popen:
                self.assertFalse(coordinator._queue_recovery({"phase": "recovering"}))
            popen.assert_not_called()
            server.shutdown.assert_not_called()
            self.assertFalse((root / ".run").exists())

    def test_stale_recovery_marker_does_not_prevent_a_new_start_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.qualified_root(temporary)
            run_dir = root / ".run"
            (run_dir / "update-recovery.tx-stale.requested").write_text("old\n", encoding="utf-8")
            state_path = run_dir / "update-state.json"
            lease_path = run_dir / "update-lease.json"
            state_path.write_text(
                json.dumps({"phase": "failed", "transaction_id": "tx-stale"}),
                encoding="utf-8",
            )
            server = SimpleNamespace(shutdown=Mock())
            coordinator = RecoveryCoordinator(root, state_path, lease_path, "tx-stale", 30, server)

            with patch("scratch.maintenance_server.subprocess.Popen") as popen:
                self.assertTrue(
                    coordinator._queue_recovery(
                        {"phase": "failed", "controllerActive": False, "transaction": "tx-stale"}
                    )
                )

            popen.assert_called_once()
            server.shutdown.assert_called_once()

    def test_terminal_rollback_failure_never_enters_an_automatic_retry_loop(self) -> None:
        self.assertFalse(
            recovery_needed(
                {
                    "phase": "rollback_failed",
                    "controllerActive": False,
                    "terminal": True,
                }
            )
        )

    def test_http_responder_exposes_safe_live_status_and_disables_proxy_caching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.qualified_root(temporary)
            run_dir = root / ".run"
            state_path = run_dir / "update-state.json"
            lease_path = run_dir / "update-lease.json"
            state_path.write_text(
                json.dumps(
                    {
                        "phase": "rollback_failed",
                        "message": "Automatic recovery needs attention.",
                        "error": "rollback_failed",
                        "transaction_id": "tx-http-status",
                        "diagnostic_id": "tx-http-stat-123",
                    }
                ),
                encoding="utf-8",
            )
            server = MaintenanceServer(("127.0.0.1", 0), root, state_path, lease_path, "tx-http-status", 30)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = int(server.server_address[1])
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(f"http://127.0.0.1:{port}/__streamhome/update-status", timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(payload["phase"], "rollback_failed")
                    self.assertEqual(response.headers["CDN-Cache-Control"], "no-store")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(f"http://127.0.0.1:{port}/", timeout=2)
                self.assertEqual(raised.exception.code, 503)
                self.assertEqual(raised.exception.headers["Cache-Control"], "no-store, no-cache, must-revalidate, max-age=0")
                raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    @unittest.skipIf(os.name == "nt", "Controller process death and executable restart handoff require native Linux")
    def test_killed_controller_at_each_cutover_phase_launches_recovery_and_releases_maintenance(self) -> None:
        cases = (
            (mode, phase)
            for mode in ("legacy", "canonical")
            for phase in ("stopping", "installing", "starting", "rolling_back")
        )
        for mode, phase in cases:
            with self.subTest(mode=mode, phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = self.qualified_root(temporary)
                run_dir = root / ".run"
                transaction = (
                    self.claim_operation(root).active_operation.operation_id
                    if mode == "canonical"
                    else f"tx-{phase}"
                )
                start_marker = root / "start-reached.marker"
                start_script = root / "start.sh"
                start_script.write_text(
                    f"#!/usr/bin/env bash\nprintf '%s\\n' recovered > '{start_marker}'\n",
                    encoding="utf-8",
                )
                start_script.chmod(0o755)
                controller = subprocess.Popen(["sleep", "60"])
                maintenance: subprocess.Popen[bytes] | None = None
                try:
                    start_ticks = Path(f"/proc/{controller.pid}/stat").read_text(encoding="utf-8")
                    start_ticks = start_ticks[start_ticks.rfind(")") + 2 :].split()[19]
                    state_path = run_dir / "update-state.json"
                    lease_path = run_dir / "update-lease.json"
                    state_path.write_text(
                        json.dumps(
                            {
                                "phase": phase,
                                "message": f"Fault injection at {phase}.",
                                "transaction_id": transaction,
                                "started_at": time.time(),
                            }
                        ),
                        encoding="utf-8",
                    )
                    lease_path.write_text(
                        json.dumps(
                            {
                                "transaction_id": transaction,
                                "controller_pid": controller.pid,
                                "controller_start_ticks": start_ticks,
                                "heartbeat_at": time.time(),
                            }
                        ),
                        encoding="utf-8",
                    )
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", 0))
                        port = int(probe.getsockname()[1])
                    maintenance = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).with_name("maintenance_server.py")),
                            "--port",
                            str(port),
                            "--root",
                            str(root),
                            "--state-file",
                            str(state_path),
                            "--lease-file",
                            str(lease_path),
                            "--transaction-id",
                            transaction,
                            "--controller-silence-seconds",
                            "2",
                        ],
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                    )
                    controller.terminate()
                    controller.wait(timeout=3)
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline and not start_marker.is_file():
                        time.sleep(0.1)
                    self.assertTrue(start_marker.is_file(), f"recovery was not launched for {phase}")
                    maintenance.wait(timeout=5)
                    self.assertIsNotNone(maintenance.returncode, f"maintenance did not release its port for {phase}")
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", port))
                    persisted = json.loads(state_path.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["phase"], "recovering")
                    self.assertEqual(persisted["error"], "controller_lost")
                finally:
                    if controller.poll() is None:
                        controller.kill()
                        controller.wait(timeout=3)
                    if maintenance is not None and maintenance.poll() is None:
                        maintenance.kill()
                        maintenance.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
