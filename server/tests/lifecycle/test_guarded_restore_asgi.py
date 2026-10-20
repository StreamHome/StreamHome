"""Actual guarded ASGI/controller/subprocess restore, restart, migration and WAL."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lifecycle import restore_controller, state
from scratch import runtime_control
from tests.fixtures.guarded_restore_runtime import EphemeralRestoreRuntime


def available_port():
    with closing(socket.socket()) as selected:
        selected.bind(("127.0.0.1", 0))
        return selected.getsockname()[1]


class DisposableRestoreInstallation:
    def __init__(self, *, authority_schema_version=1):
        if authority_schema_version not in {1, 2}:
            raise ValueError("unsupported disposable authority fixture")
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-guarded-restore-")
        self.root = Path(self.temporary.name)
        source = Path(__file__).resolve().parents[2]
        for original in source.rglob("*.py"):
            relative = original.relative_to(source)
            if relative.parts[0] in {"temp", "media", "backup", "rclone", "venv", ".run"} or "__pycache__" in relative.parts:
                continue
            target = self.root / "server" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
        for original in (source / "data").glob("*.json"):
            target = self.root / "server/data" / original.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
        for name in ("start.sh", "update.sh"):
            shutil.copyfile(source.parent / name, self.root / name)
        self.backend_port = available_port()
        self.web_port = available_port()
        while self.web_port in {8000, self.backend_port}:
            self.web_port = available_port()
        self.home = self.root / "test-home"
        self.home.mkdir()
        (self.root / ".env").write_text(f"SETUP=true\nSTORAGE_ENGINE=LOCAL\nWEB_PORT={self.web_port}\nPUBLIC_URL=http://127.0.0.1:{self.web_port}\nJWT_SECRET=isolated-asgi-controller-secret\nSECRET_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\nRCLONE_CONFIG_PASS=isolated-config-password\n", encoding="utf-8")
        (self.root / ".env").chmod(0o600)
        self.database = self.root / "server/database.db"
        backup_root = self.root / "server/backup"
        backup_root.mkdir()
        self.backup = backup_root / "backup_20261013_120000.db"
        self.create_database(self.database, "original")
        self.create_database(self.backup, "candidate", migration=True)
        if authority_schema_version == 2:
            # Fixture initialization only; production migration needs separately
            # qualified target/rollback readers and explicit idle-only adoption.
            paths = state.lifecycle_paths(self.root)
            state._ensure_lifecycle_directories(paths)
            state._write_state_unlocked(paths, state.LifecycleState(2, 0, None))
        self.claim = state.claim_operation_with_credential(self.root, operation_type="restore", owner_kind="restore_controller")
        self.operation_id = self.claim.state.active_operation.operation_id
        identity = restore_controller.prevalidate_candidate(self.root, self.backup.name)
        restore_controller.create_restore_journal(self.root, operation_id=self.operation_id, generation=self.claim.state.generation, candidate=identity)
        self.paths = restore_controller.restore_paths(self.root, self.operation_id)
        self.controllers = []

    def create_database(self, path, marker, *, migration=False):
        from sqlalchemy import create_engine
        from sqlmodel import SQLModel, Session
        from models import DownloadTask, User

        engine = create_engine(f"sqlite:///{path}")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(User(id=1, email="guarded@example.test", password_hash="unused"))
            db.add(DownloadTask(id="interrupted-fixture", tmdb_id=1, media_type="movie", video_url="", status="DOWNLOADING", created_at="2026-10-13T00:00:00Z"))
            db.commit()
        engine.dispose()
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE restore_fixture(marker TEXT)")
            db.execute("INSERT INTO restore_fixture VALUES (?)", (marker,))
            if migration:
                db.execute("DROP INDEX IF EXISTS ix_authchallenge_auth_session_id")
                db.execute("ALTER TABLE authchallenge DROP COLUMN auth_session_id")
            db.commit()
        path.chmod(0o600)

    def launch(self, *, crash=False, operator_action=None):
        current = state.inspect_state(self.root)
        capability = state.load_controller_capability(self.root, operation_id=self.operation_id, generation=current.generation)
        ack_read, ack_write = os.pipe()
        cap_read, cap_write = os.pipe()
        gate_read, gate_write = os.pipe()
        environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
        environment.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(self.root / "server"), STREAMHOME_INSTANCE_ROOT=str(self.root))
        command = [sys.executable, "-m", "tests.fixtures.guarded_restore_runtime", "--root", str(self.root), "--operation-id", self.operation_id, "--generation", str(current.generation), "--backend-port", str(self.backend_port), "--ack-fd", str(ack_write), "--capability-fd", str(cap_read), "--gate-fd", str(gate_read)]
        if crash:
            command.append("--crash-after-accept")
        if operator_action:
            command.extend(["--operator-action", operator_action])
        process = subprocess.Popen(command, cwd=self.root / "server", env=environment, pass_fds=(ack_write, cap_read, gate_read), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.controllers.append(process)
        os.close(ack_write)
        os.close(cap_read)
        os.close(gate_read)
        os.write(cap_write, capability.encode("ascii"))
        os.close(cap_write)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(ack_read, selectors.EVENT_READ)
                if not selector.select(timeout=10):
                    raise RuntimeError("Controller did not acknowledge real FD handoff")
            acknowledgement = json.loads(os.read(ack_read, 4096))
            if not acknowledgement.get("owned"):
                raise RuntimeError("Controller did not acquire exact lifecycle authority")
            os.write(gate_write, b"1")
        finally:
            os.close(ack_read)
            os.close(gate_write)
        return process, acknowledgement

    def finish(self, process, expected=0):
        stdout, stderr = process.communicate(timeout=40)
        if process.returncode != expected:
            runtime_log = self.paths.operation_dir / "test-asgi-runtime.log"
            diagnostics = runtime_log.read_text()[-12000:] if runtime_log.exists() else "No runtime log"
            raise AssertionError(f"Controller exited {process.returncode}, expected {expected}.\n{stdout}\n{stderr}\n{diagnostics}")

    def close(self):
        for process in self.controllers:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
        for service in ("backend", "maintenance", "web"):
            runtime_control.terminate_owned_service(self.root, service)
        self.temporary.cleanup()


class GuardedRestoreASGIRegression(unittest.TestCase):
    def setUp(self):
        self.installation = DisposableRestoreInstallation()

    def tearDown(self):
        self.installation.close()

    def prepare_accepted_journal(self):
        fixture = self.installation
        rc = restore_controller
        size, digest, previous_size, previous_digest = rc.create_rollback_checkpoint(fixture.paths)
        journal = rc.update_restore_journal(fixture.root, fixture.operation_id, progress="checkpoint_ready", rollback_size=size, rollback_sha256=digest, previous_size=previous_size, previous_sha256=previous_digest)
        size, digest = rc.stage_candidate(fixture.paths, journal)
        journal = rc.update_restore_journal(fixture.root, fixture.operation_id, progress="candidate_staged", staged_size=size, staged_sha256=digest)
        rc.activate_staged_candidate(fixture.paths, journal)
        rc.update_restore_journal(fixture.root, fixture.operation_id, progress="verifying_candidate")
        for before, after in (("reserved", "exclusive"), ("exclusive", "verifying")):
            state.transition_operation_with_credential(fixture.root, operation_id=fixture.operation_id, generation=fixture.claim.state.generation, expected_phase=before, next_phase=after)
        rc.accept_guarded_runtime(fixture.root, fixture.operation_id, generation=fixture.claim.state.generation, decision="accept")
        rc.update_restore_journal(fixture.root, fixture.operation_id, progress="normal_recovery")
        return fixture.paths.journal_path.read_bytes()

    def test_repeated_acceptance_preserves_journal_and_rejects_another_decision(self):
        original = self.prepare_accepted_journal()
        fixture = self.installation
        journal = restore_controller.accept_guarded_runtime(fixture.root, fixture.operation_id, generation=fixture.claim.state.generation, decision="accept")
        self.assertEqual(journal["progress"], "normal_recovery")
        self.assertEqual(fixture.paths.journal_path.read_bytes(), original)
        with self.assertRaises(restore_controller.RestoreConflictError):
            restore_controller.accept_guarded_runtime(fixture.root, fixture.operation_id, generation=fixture.claim.state.generation, decision="rollback")
        self.assertEqual(fixture.paths.journal_path.read_bytes(), original)

    def test_repeated_acceptance_fails_closed_if_durability_cannot_be_reaffirmed(self):
        original = self.prepare_accepted_journal()
        fixture = self.installation
        with patch.object(restore_controller, "_fsync_directory", side_effect=OSError("injected durability failure")):
            with self.assertRaises(restore_controller.RestorePersistenceError):
                restore_controller.accept_guarded_runtime(fixture.root, fixture.operation_id, generation=fixture.claim.state.generation, decision="accept")
        self.assertEqual(fixture.paths.journal_path.read_bytes(), original)
        self.assertTrue(restore_controller.read_restore_journal(fixture.root, fixture.operation_id)["accepted"])

    def test_exact_accepted_http_retry_crosses_completion_fence_with_authority_checks(self):
        self.prepare_accepted_journal()
        fixture = self.installation
        generation = fixture.claim.state.generation
        capability = state.load_controller_capability(
            fixture.root, operation_id=fixture.operation_id, generation=generation)
        runtime = EphemeralRestoreRuntime(fixture.root, fixture.backend_port)
        try:
            with runtime.patches():
                runtime.start_backend(fixture.root, fixture.operation_id, generation)
                runtime.wait_guarded(fixture.root, fixture.operation_id, generation, "dev")
                restore_controller._commit_guarded_runtime(
                    fixture.operation_id, generation, "dev", capability, "accept")
                runtime.wait_general("dev")
                before = fixture.paths.journal_path.read_bytes()
                body = {"operation_id": fixture.operation_id, "generation": generation,
                        "build_id": "dev", "decision": "accept"}
                url = "http://127.0.0.1:8000/api/lifecycle/restore/commit"
                code, response = runtime.request_json(url, method="POST", body=body,
                    headers={"X-StreamHome-Restore-Commit": capability})
                self.assertEqual(code, 200, response)
                self.assertTrue(response["committed"])
                self.assertEqual(fixture.paths.journal_path.read_bytes(), before)
                for changes, credential, expected in (
                    ({"generation": generation + 1}, capability, 403),
                    ({"build_id": "foreign-build"}, capability, 403),
                    ({"operation_id": "foreign-operation"}, capability, 403),
                    ({"decision": "rollback"}, capability, 409),
                    ({}, "invalid-controller-capability", 503),
                ):
                    with self.subTest(changes=changes, expected=expected):
                        code, response = runtime.request_json(url, method="POST",
                            body={**body, **changes},
                            headers={"X-StreamHome-Restore-Commit": credential})
                        self.assertEqual(code, expected, response)
                        self.assertEqual(fixture.paths.journal_path.read_bytes(), before)
                code, response = runtime.request_json(
                    "http://127.0.0.1:8000/api/auth/login", method="POST",
                    body={"email": "synthetic@example.test", "password": "synthetic"})
                self.assertEqual(code, 503, response)
                self.assertIn(response["detail"]["code"], {"startup_not_ready", "lifecycle_work_blocked"})
        finally:
            runtime.stop_runtime(fixture.root, fixture.web_port)

    def test_actual_controller_guarded_acceptance_runs_migrations_and_normal_recovery(self):
        fixture = self.installation
        process, handoff = fixture.launch()
        self.assertEqual(handoff["generation"], fixture.claim.state.generation + 1)
        fixture.finish(process)
        self.assertIsNone(state.inspect_state(fixture.root).active_operation)
        self.assertEqual(restore_controller.read_restore_journal(fixture.root, fixture.operation_id)["progress"], "succeeded")
        with closing(sqlite3.connect(fixture.database)) as db:
            self.assertIn("auth_session_id", {row[1] for row in db.execute("PRAGMA table_info(authchallenge)")})
            self.assertEqual(db.execute("SELECT marker FROM restore_fixture").fetchone()[0], "candidate")
            self.assertEqual(db.execute("SELECT status FROM downloadtask WHERE id='interrupted-fixture'").fetchone()[0], "FAILED")
        code, payload = restore_controller._request_json(f"http://127.0.0.1:{fixture.backend_port}/api/health/ready")
        self.assertEqual(code, 200)
        self.assertEqual(payload["readyKind"], "general")

    def test_actual_accepted_controller_crash_wal_and_restart_continue_without_rollback(self):
        fixture = self.installation
        process, _handoff = fixture.launch(crash=True)
        fixture.finish(process, expected=73)
        before = restore_controller.read_restore_journal(fixture.root, fixture.operation_id)
        self.assertTrue(before["accepted"])
        self.assertEqual(before["progress"], "normal_recovery")
        self.assertTrue(Path(f"{fixture.database}-wal").exists())
        inode = fixture.database.stat().st_ino
        restarted, _handoff = fixture.launch()
        fixture.finish(restarted)
        self.assertEqual(fixture.database.stat().st_ino, inode)
        self.assertIsNone(state.inspect_state(fixture.root).active_operation)
        with closing(sqlite3.connect(fixture.database)) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT marker FROM restore_fixture ORDER BY rowid")], ["candidate", "accepted-wal-write"])
        self.assertEqual(restore_controller.read_restore_journal(fixture.root, fixture.operation_id)["progress"], "succeeded")

    def test_actual_accepted_rollback_operator_resume_normalizes_exclusive_to_verifying(self):
        fixture = self.installation
        rc = restore_controller
        size, digest, previous_size, previous_digest = rc.create_rollback_checkpoint(fixture.paths)
        journal = rc.update_restore_journal(fixture.root, fixture.operation_id, progress="checkpoint_ready", rollback_size=size, rollback_sha256=digest, previous_size=previous_size, previous_sha256=previous_digest)
        size, digest = rc.stage_candidate(fixture.paths, journal)
        journal = rc.update_restore_journal(fixture.root, fixture.operation_id, progress="candidate_staged", staged_size=size, staged_sha256=digest)
        rc.activate_staged_candidate(fixture.paths, journal)
        rc.restore_rollback_checkpoint(fixture.paths, rc.read_restore_journal(fixture.root, fixture.operation_id))
        rc.update_restore_journal(fixture.root, fixture.operation_id, progress="verifying_rollback")
        rc.update_restore_journal(fixture.root, fixture.operation_id, progress="normal_recovery", accepted=True, outcome="rolled_back")
        rc.update_restore_journal(fixture.root, fixture.operation_id, progress="recovery_required", error_code="injected_controller_interruption")
        for before, after in (("reserved", "exclusive"), ("exclusive", "verifying"), ("verifying", "recovery_required")):
            state.transition_operation_with_credential(fixture.root, operation_id=fixture.operation_id, generation=fixture.claim.state.generation, expected_phase=before, next_phase=after)
        inode = fixture.database.stat().st_ino
        process, _handoff = fixture.launch(operator_action="continue_recovery")
        fixture.finish(process)
        self.assertEqual(fixture.database.stat().st_ino, inode)
        self.assertIsNone(state.inspect_state(fixture.root).active_operation)
        self.assertEqual(rc.read_restore_journal(fixture.root, fixture.operation_id)["progress"], "rolled_back")
        with closing(sqlite3.connect(fixture.database)) as db:
            self.assertEqual(db.execute("SELECT marker FROM restore_fixture").fetchone()[0], "original")


if __name__ == "__main__":
    unittest.main()
