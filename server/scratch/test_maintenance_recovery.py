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

from scratch.maintenance_server import (
    RecoveryCoordinator,
    MaintenanceServer,
    controller_is_current,
    maintenance_page,
    recovery_needed,
    safe_public_status,
)


class MaintenanceRecoveryTests(unittest.TestCase):
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
            root = Path(temporary)
            run_dir = root / ".run"
            run_dir.mkdir()
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

            status = safe_public_status(state_path, lease_path, root, "tx-abandoned", 30)

            self.assertEqual(status["phase"], "recovering")
            self.assertFalse(status["controllerActive"])
            self.assertTrue(recovery_needed(status))
            rendered = maintenance_page(status).decode("utf-8")
            self.assertIn("Recovering an interrupted update", rendered)
            self.assertIn("tx-abandone", rendered)

    def test_recovery_handoff_is_single_use_and_persists_visible_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / ".run"
            run_dir.mkdir()
            (root / "restart.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
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
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["phase"], "recovering")
            self.assertEqual(persisted["error"], "controller_lost")
            self.assertTrue((run_dir / "update-recovery.tx-recover-once.requested").is_file())

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
            root = Path(temporary)
            run_dir = root / ".run"
            run_dir.mkdir()
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
    def test_killed_controller_at_each_cutover_phase_queues_recovery_and_releases_maintenance(self) -> None:
        for phase in ("stopping", "installing", "starting", "rolling_back"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_dir = root / ".run"
                run_dir.mkdir()
                transaction = f"tx-{phase}"
                restart_marker = root / "restart-reached.marker"
                restart_script = root / "restart.sh"
                restart_script.write_text(
                    f"#!/usr/bin/env bash\nprintf '%s\\n' recovered > '{restart_marker}'\n",
                    encoding="utf-8",
                )
                restart_script.chmod(0o755)
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
                        ]
                    )
                    controller.terminate()
                    controller.wait(timeout=3)
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline and not restart_marker.is_file():
                        time.sleep(0.1)
                    self.assertTrue(restart_marker.is_file(), f"recovery was not queued for {phase}")
                    maintenance.wait(timeout=3)
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
