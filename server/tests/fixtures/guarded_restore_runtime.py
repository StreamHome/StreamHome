"""Test-only runtime IO adapter: real guarded ASGI on owned ephemeral ports."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

from lifecycle import restore_controller, state
from scratch import runtime_control


class EphemeralRestoreRuntime:
    def __init__(self, root: Path, backend_port: int, *, crash_after_accept=False):
        self.root = Path(root).resolve()
        self.backend_port = backend_port
        self.crash_after_accept = crash_after_accept
        self.processes = []
        self.request = restore_controller._request_json
        self.guarded_ready = restore_controller._wait_guarded_ready
        self.general_ready = restore_controller._wait_general_ready
        self.commit_runtime = restore_controller._commit_guarded_runtime

    def request_json(self, url, **kwargs):
        parsed = urlsplit(url)
        if parsed.hostname == "127.0.0.1" and parsed.port == 8000:
            url = urlunsplit((parsed.scheme, f"127.0.0.1:{self.backend_port}", parsed.path, parsed.query, parsed.fragment))
        return self.request(url, **kwargs)

    def start_backend(self, root, operation_id, generation):
        if Path(root).resolve() != self.root:
            raise AssertionError("Runtime adapter cannot start another installation")
        token = secrets.token_urlsafe(32)
        environment = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL"}}
        environment.update(PYTHONPATH=str(self.root / "server"), PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", STREAMHOME_INSTANCE_ROOT=str(self.root), STREAMHOME_INSTANCE_TOKEN=token, STREAMHOME_SERVICE="backend", STREAMHOME_BUILD_ID="dev", STREAMHOME_LIFECYCLE_STARTUP_MODE="restore_verify", STREAMHOME_LIFECYCLE_OPERATION_ID=operation_id, STREAMHOME_LIFECYCLE_GENERATION=str(generation))
        log_path = restore_controller.restore_paths(root, operation_id).operation_dir / "test-asgi-runtime.log"
        with log_path.open("ab") as log:
            os.chmod(log_path, 0o600)
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(self.backend_port), "--no-proxy-headers"], cwd=self.root / "server", env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        try:
            runtime_control.record_service(self.root, "backend", process.pid, token)
        except BaseException:
            process.terminate()
            process.wait(timeout=5)
            raise
        return process, "dev"

    def stop_runtime(self, root, _web_port):
        if Path(root).resolve() != self.root:
            raise AssertionError("Runtime adapter cannot stop another installation")
        if not runtime_control.terminate_owned_service(self.root, "backend"):
            raise RuntimeError("Disposable backend could not be stopped")
        for process in self.processes:
            if process.poll() is None:
                process.wait(timeout=5)

    def wait_guarded(self, root, operation_id, generation, build, **_kwargs):
        return self.guarded_ready(root, operation_id, generation, build, timeout=15)

    def wait_general(self, build, **_kwargs):
        self.general_ready(build, timeout=20)
        if self.crash_after_accept:
            # Preserve a committed WAL frame across abrupt controller death.
            # Normal guarded migrations/recovery have already exercised app writes.
            connection = sqlite3.connect(self.root / "server/database.db")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO restore_fixture VALUES ('accepted-wal-write')")
            connection.commit()
            os._exit(73)

    def commit_guarded(self, operation_id, generation, build, capability, decision):
        # Real HTTP authorization must reject foreign generation and capability.
        body = {"operation_id": operation_id, "generation": generation + 1, "build_id": build, "decision": decision}
        code, _ = self.request_json("http://127.0.0.1:8000/api/lifecycle/restore/commit", method="POST", body=body, headers={"X-StreamHome-Restore-Commit": capability})
        if code != 403:
            raise AssertionError("Foreign generation was not rejected by guarded ASGI")
        body["generation"] = generation
        code, _ = self.request_json("http://127.0.0.1:8000/api/lifecycle/restore/commit", method="POST", body=body, headers={"X-StreamHome-Restore-Commit": "invalid-controller-capability"})
        if code not in {403, 503}:
            raise AssertionError("Foreign controller capability was not rejected")
        journal = restore_controller.read_restore_journal(self.root, operation_id)
        if journal["accepted"]:
            opposite = {**body, "decision": "rollback" if decision == "accept" else "accept"}
            code, _ = self.request_json("http://127.0.0.1:8000/api/lifecycle/restore/commit", method="POST", body=opposite, headers={"X-StreamHome-Restore-Commit": capability})
            if code != 409:
                raise AssertionError("An accepted restore decision was allowed to change")
        return self.commit_runtime(operation_id, generation, build, capability, decision)

    @contextmanager
    def patches(self):
        with ExitStack() as stack:
            for name, value in {"_start_backend": self.start_backend, "_stop_supported_runtime": self.stop_runtime, "_request_json": self.request_json, "_wait_guarded_ready": self.wait_guarded, "_wait_general_ready": self.wait_general, "_commit_guarded_runtime": self.commit_guarded, "_start_web": lambda *_args: None, "_build_id": lambda _root: "dev"}.items():
                stack.enter_context(patch.object(restore_controller, name, value))
            yield self


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--backend-port", required=True, type=int)
    parser.add_argument("--capability-fd", required=True, type=int)
    parser.add_argument("--ack-fd", required=True, type=int)
    parser.add_argument("--gate-fd", required=True, type=int)
    parser.add_argument("--operator-action")
    parser.add_argument("--crash-after-accept", action="store_true")
    arguments = parser.parse_args()
    root = state.resolve_installation_root(arguments.root)
    capability = restore_controller._read_pipe_secret(arguments.capability_fd)
    runtime = EphemeralRestoreRuntime(root, arguments.backend_port, crash_after_accept=arguments.crash_after_accept)
    with runtime.patches():
        result = restore_controller._run_controller(root, arguments.operation_id, arguments.generation, capability, acknowledgement_fd=arguments.ack_fd, gate_fd=arguments.gate_fd, operator_action=arguments.operator_action)
    print(json.dumps({"result": result, "generation": state.inspect_state(root).generation}), flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
