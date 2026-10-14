"""Production detached restore launcher and FD gate with an ephemeral ASGI adapter."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest
from unittest import mock

from lifecycle import restore_controller, state
from scratch import runtime_control


def detached_adapter_main():
    """Preserve production argparse, fork/setsid, FD reads and controller body."""
    from tests.fixtures.guarded_restore_runtime import EphemeralRestoreRuntime
    root = Path(os.environ["STREAMHOME_TEST_RESTORE_ROOT"])
    runtime = EphemeralRestoreRuntime(root, int(os.environ["STREAMHOME_TEST_BACKEND_PORT"]))
    with runtime.patches():
        return restore_controller.main()


@unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "Linux process ownership required")
class DetachedControllerHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Production deliberately detaches twice. Adopt only this test process's
        # descendants so killed test controllers are reaped before re-entry.
        cls.libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        if cls.libc.prctl(37, ctypes.byref(old), 0, 0, 0) != 0:
            raise unittest.SkipTest("Linux subreaper inspection unavailable")
        cls.previous_subreaper = old.value
        if cls.libc.prctl(36, 1, 0, 0, 0) != 0:
            raise unittest.SkipTest("Linux test subreaper unavailable")

    @classmethod
    def tearDownClass(cls):
        cls.libc.prctl(36, cls.previous_subreaper, 0, 0, 0)

    def setUp(self):
        from tests.lifecycle.test_guarded_restore_asgi import DisposableRestoreInstallation
        self.fixture = DisposableRestoreInstallation()
        self.detached_pids = []
        self.detached_leases = {}
        self.reaped_pids = set()
        self.handoffs = []
        self.original_popen = subprocess.Popen

    def tearDown(self):
        try:
            for handoff in self.handoffs:
                if not handoff._released:
                    os.close(handoff._gate_fd)
                    handoff._released = True
            for pid in self.detached_pids:
                self.kill_owned(pid)
                self.reap(pid)
        finally:
            self.fixture.close()
            while True:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if not pid:
                    break

    def reap(self, pid, timeout=5):
        if pid in self.reaped_pids:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                if runtime_control.inspect_process(pid) is None:
                    self.reaped_pids.add(pid)
                    return
                time.sleep(0.02)
                continue
            if waited:
                self.reaped_pids.add(pid)
                return
            time.sleep(0.02)
        raise AssertionError("Owned detached test controller was not reaped")

    def kill_owned(self, pid):
        if pid in self.reaped_pids:
            return
        lease = self.detached_leases.get(pid)
        if lease is not None and state.controller_lease_is_current(lease):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def popen(self, command, *args, **kwargs):
        selected = list(command)
        if selected[1:3] == ["-m", "lifecycle.restore_controller"]:
            selected[2] = "tests.lifecycle.test_controller_handoff"
            environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
            environment.update(HOME=str(self.fixture.home), PYTHONPATH=str(self.fixture.root / "server"),
                PYTHONDONTWRITEBYTECODE="1", STREAMHOME_TEST_RESTORE_ROOT=str(self.fixture.root),
                STREAMHOME_TEST_BACKEND_PORT=str(self.fixture.backend_port))
            kwargs["env"] = environment
        process = self.original_popen(selected, *args, **kwargs)
        self.fixture.controllers.append(process)
        return process

    def spawn(self):
        current = state.inspect_state(self.fixture.root)
        capability = state.load_controller_capability(self.fixture.root,
            operation_id=self.fixture.operation_id, generation=current.generation)
        with mock.patch.object(restore_controller.subprocess, "Popen", new=self.popen):
            handoff = restore_controller.spawn_restore_controller(self.fixture.root,
                operation_id=self.fixture.operation_id, generation=current.generation,
                owner_capability=capability)
        self.handoffs.append(handoff)
        lease = state.read_controller_lease(self.fixture.root)
        self.assertIsNotNone(lease)
        self.detached_pids.append(lease.pid)
        self.detached_leases[lease.pid] = lease
        return handoff, lease

    def wait_terminal(self, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = state.inspect_state(self.fixture.root)
            journal = restore_controller.read_restore_journal(self.fixture.root, self.fixture.operation_id)
            if current.active_operation is None and journal["progress"] in {"succeeded", "rolled_back"}:
                for pid in self.detached_pids:
                    self.reap(pid)
                return journal
            if current.active_operation and current.active_operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
                log = self.fixture.paths.operation_dir / "test-asgi-runtime.log"
                raise AssertionError(f"Controller failed closed unexpectedly: {journal}. {log.read_text()[-5000:] if log.exists() else ''}")
            time.sleep(0.05)
        raise AssertionError("Detached controller did not reach verified terminal state")

    def test_detached_owner_outlives_launcher_and_waits_for_response_gate(self):
        original = restore_controller.artifact_identity(self.fixture.database)
        handoff, lease = self.spawn()
        self.assertEqual(handoff.process.wait(timeout=5), 0)
        self.assertNotEqual(lease.pid, handoff.process.pid)
        self.assertTrue(state.controller_lease_is_current(lease))
        self.assertEqual(handoff.generation, self.fixture.claim.state.generation + 1)
        self.assertEqual(restore_controller.read_restore_journal(self.fixture.root, self.fixture.operation_id)["progress"], "controller_owned")
        self.assertFalse(self.fixture.paths.rollback_path.exists())
        self.assertEqual(restore_controller.artifact_identity(self.fixture.database), original)
        handoff.release_after_response()
        self.assertEqual(self.wait_terminal()["progress"], "succeeded")

    def test_closed_response_gate_fails_closed_before_any_database_replace(self):
        original = restore_controller.artifact_identity(self.fixture.database)
        handoff, lease = self.spawn()
        os.close(handoff._gate_fd)
        handoff._released = True
        self.reap(lease.pid)
        journal = restore_controller.read_restore_journal(self.fixture.root, self.fixture.operation_id)
        self.assertEqual(journal["error_code"], "restore_response_not_confirmed")
        self.assertEqual(journal["progress"], "recovery_required")
        self.assertFalse(journal["accepted"])
        self.assertFalse(self.fixture.paths.rollback_path.exists())
        self.assertFalse(self.fixture.paths.candidate_path.exists())
        self.assertEqual(restore_controller.artifact_identity(self.fixture.database), original)
        self.assertEqual(restore_controller.recover_restore(self.fixture.root), 3)

    def test_killed_detached_owner_reentry_rotates_generation_and_completes(self):
        original = restore_controller.artifact_identity(self.fixture.database)
        handoff, lease = self.spawn()
        old_generation = handoff.generation
        old_capability = state.load_controller_capability(self.fixture.root,
            operation_id=self.fixture.operation_id, generation=old_generation)
        self.kill_owned(lease.pid)
        self.reap(lease.pid)
        handoff.release_after_response()
        self.assertEqual(restore_controller.artifact_identity(self.fixture.database), original)
        with mock.patch.object(restore_controller.subprocess, "Popen", new=self.popen):
            self.assertEqual(restore_controller.recover_restore(self.fixture.root), 0)
        replacement = state.read_controller_lease(self.fixture.root)
        self.assertIsNotNone(replacement)
        self.assertNotEqual(replacement.pid, lease.pid)
        self.detached_pids.append(replacement.pid)
        self.detached_leases[replacement.pid] = replacement
        self.assertEqual(replacement.generation, old_generation + 1)
        with self.assertRaises(state.LifecycleError):
            state.validate_operation_owner(self.fixture.root, operation_id=self.fixture.operation_id,
                generation=old_generation, expected_phase="reserved", owner_capability=old_capability)
        self.assertEqual(self.wait_terminal()["progress"], "succeeded")


if __name__ == "__main__":
    if os.environ.get("STREAMHOME_TEST_RESTORE_ROOT"):
        raise SystemExit(detached_adapter_main())
    unittest.main()
