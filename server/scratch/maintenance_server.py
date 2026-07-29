from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ACTIVE_PHASES = {
    "preflight",
    "waiting_for_idle",
    "stopping",
    "installing",
    "starting",
    "rolling_back",
    "recovering",
}
SUCCESS_PHASES = {"succeeded", "rolled_back"}
FAILURE_PHASES = {"failed", "rollback_failed"}
PUBLIC_PHASE_LABELS = {
    "preflight": "Validating update",
    "waiting_for_idle": "Waiting for a safe cutover",
    "stopping": "Stopping StreamHome safely",
    "installing": "Activating the validated release",
    "starting": "Starting and health-checking StreamHome",
    "rolling_back": "Restoring the previous release",
    "recovering": "Recovering an interrupted update",
    "succeeded": "Update completed",
    "rolled_back": "Previous release restored",
    "failed": "Update stopped safely",
    "rollback_failed": "Automatic recovery needs attention",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def atomic_update_json(path: Path, **changes: Any) -> dict[str, Any]:
    payload = read_json(path)
    payload.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return payload


def linux_process_start_ticks(pid: int) -> str:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = stat_line[stat_line.rfind(")") + 2 :].split()
        return remainder[19]
    except (IndexError, OSError, ValueError):
        return ""


def process_is_alive(pid: int, expected_start_ticks: str = "") -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if expected_start_ticks and os.name == "posix":
        actual = linux_process_start_ticks(pid)
        return bool(actual and actual == expected_start_ticks)
    return True


def legacy_lease(root: Path) -> dict[str, Any]:
    lock = root / ".run" / "update.lock"
    try:
        pid = int((lock / "owner.pid").read_text(encoding="utf-8").strip())
        heartbeat = (lock / "heartbeat").stat().st_mtime
    except (OSError, ValueError):
        return {}
    return {"controller_pid": pid, "heartbeat_at": heartbeat, "legacy": True}


def controller_is_current(
    lease: dict[str, Any],
    transaction_id: str,
    silence_seconds: float,
    now: float | None = None,
) -> bool:
    current_time = time.time() if now is None else now
    lease_transaction = str(lease.get("transaction_id") or "")
    if transaction_id and lease_transaction and lease_transaction != transaction_id:
        return False
    try:
        heartbeat_at = float(lease.get("heartbeat_at") or 0)
        pid = int(lease.get("controller_pid") or 0)
    except (TypeError, ValueError):
        return False
    if heartbeat_at <= 0 or current_time - heartbeat_at > silence_seconds:
        return False
    return process_is_alive(pid, str(lease.get("controller_start_ticks") or ""))


def safe_public_status(
    state_path: Path,
    lease_path: Path,
    root: Path,
    transaction_id: str,
    silence_seconds: float,
) -> dict[str, Any]:
    state = read_json(state_path)
    state_transaction = str(state.get("transaction_id") or transaction_id or "")
    lease = read_json(lease_path) or legacy_lease(root)
    active = controller_is_current(lease, state_transaction, silence_seconds)
    phase = str(state.get("phase") or "recovering")
    if phase in ACTIVE_PHASES and not active:
        phase = "recovering"
    started_at = state.get("started_at") or state.get("updated_at") or time.time()
    try:
        elapsed_seconds = max(0, int(time.time() - float(started_at)))
    except (TypeError, ValueError):
        elapsed_seconds = 0
    error = str(state.get("error") or "")
    return {
        "phase": phase,
        "label": PUBLIC_PHASE_LABELS.get(phase, "StreamHome maintenance"),
        "message": str(state.get("message") or "StreamHome is completing a protected update."),
        "error": error if phase in FAILURE_PHASES else "",
        "diagnostic": str(state.get("diagnostic_id") or "") if phase in FAILURE_PHASES else "",
        "transaction": state_transaction[:12],
        "target": str(state.get("target_commit") or "")[:12],
        "elapsedSeconds": elapsed_seconds,
        "controllerActive": active,
        "terminal": phase in SUCCESS_PHASES | FAILURE_PHASES,
    }


def recovery_needed(status: dict[str, Any]) -> bool:
    phase = str(status.get("phase") or "")
    if status.get("controllerActive"):
        return False
    return phase in ACTIVE_PHASES or phase in SUCCESS_PHASES or phase == "failed"


class RecoveryCoordinator:
    def __init__(
        self,
        root: Path,
        state_path: Path,
        lease_path: Path,
        transaction_id: str,
        silence_seconds: float,
        server: ThreadingHTTPServer,
    ) -> None:
        self.root = root
        self.state_path = state_path
        self.lease_path = lease_path
        self.transaction_id = transaction_id
        self.silence_seconds = silence_seconds
        self.server = server
        self.stop_event = threading.Event()

    def _request_path(self, transaction_id: str) -> Path:
        safe_transaction = transaction_id if transaction_id and transaction_id.replace("-", "").isalnum() else "legacy"
        return self.root / ".run" / f"update-recovery.{safe_transaction}.requested"

    def _queue_recovery(self, status: dict[str, Any]) -> bool:
        transaction_id = str(status.get("transaction") or self.transaction_id or "legacy")
        request_path = self._request_path(transaction_id)
        try:
            descriptor = os.open(request_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{time.time():.6f}\n")

        atomic_update_json(
            self.state_path,
            phase="recovering",
            message="The update controller stopped unexpectedly. Automatic recovery is starting.",
            error="controller_lost",
            recovery_requested_at=time.time(),
            updated_at=time.time(),
        )
        restart_script = self.root / "restart.sh"
        log_path = self.root / "update.log"
        try:
            with log_path.open("ab", buffering=0) as log_handle:
                log_handle.write(
                    f"[StreamHome Maintenance] Controller lease expired; queued recovery for {transaction_id}.\n".encode()
                )
                subprocess.Popen(
                    [str(restart_script)],
                    cwd=str(self.root),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            atomic_update_json(
                self.state_path,
                phase="rollback_failed",
                message="The update controller stopped and the recovery handoff could not be started. Run ./start.sh from the StreamHome directory.",
                error="recovery_handoff_failed",
                recovery_error=type(exc).__name__,
                finished_at=time.time(),
                updated_at=time.time(),
            )
            return False
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        return True

    def run(self) -> None:
        while not self.stop_event.wait(2):
            status = safe_public_status(
                self.state_path,
                self.lease_path,
                self.root,
                self.transaction_id,
                self.silence_seconds,
            )
            if recovery_needed(status):
                self._queue_recovery(status)
                return


def maintenance_page(status: dict[str, Any]) -> bytes:
    phase = html.escape(str(status["phase"]))
    label = html.escape(str(status["label"]))
    message = html.escape(str(status["message"]))
    transaction = html.escape(str(status.get("transaction") or "not recorded"))
    target = html.escape(str(status.get("target") or "not recorded"))
    error = html.escape(str(status.get("error") or ""))
    diagnostic = html.escape(str(status.get("diagnostic") or ""))
    elapsed = int(status.get("elapsedSeconds") or 0)
    detail = (
        f'<p class="error">Recovery code: <strong>{error}</strong>{f" · diagnostic {diagnostic}" if diagnostic else ""}. Run <code>./start.sh</code> from the StreamHome directory if automatic recovery does not complete.</p>'
        if error and status.get("terminal")
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>StreamHome maintenance</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ min-height: 100vh; margin: 0; display: grid; place-items: center; background: #0b0807; color: #f4ebe7; }}
    main {{ width: min(38rem, calc(100% - 3rem)); padding: 2rem; border: 1px solid #563226; border-radius: 1rem; background: #160f0d; box-shadow: 0 1.5rem 5rem #0008; }}
    p {{ color: #c8b6ae; line-height: 1.6; }}
    small {{ color: #ff6b3d; letter-spacing: .08em; text-transform: uppercase; }}
    dl {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .75rem; margin-top: 1.5rem; }}
    dl div {{ border-top: 1px solid #3a2822; padding-top: .7rem; }}
    dt {{ color: #8e7d75; font-size: .75rem; text-transform: uppercase; }}
    dd {{ margin: .25rem 0 0; font-family: ui-monospace, monospace; }}
    .error {{ color: #ffb29b; }}
    code {{ color: #fff; }}
  </style>
</head>
<body data-update-phase="{phase}">
  <main>
    <small>StreamHome / Maintenance</small>
    <h1>{label}</h1>
    <p>{message}</p>
    {detail}
    <dl>
      <div><dt>Phase</dt><dd>{phase}</dd></div>
      <div><dt>Elapsed</dt><dd>{elapsed}s</dd></div>
      <div><dt>Transaction</dt><dd>{transaction}</dd></div>
      <div><dt>Target</dt><dd>{target}</dd></div>
    </dl>
  </main>
  <script>
    setTimeout(() => {{ const next = new URL(location.href); next.searchParams.set('_sh', Date.now().toString()); location.replace(next); }}, 4000);
  </script>
</body>
</html>
""".encode("utf-8")


class MaintenanceHandler(BaseHTTPRequestHandler):
    server: "MaintenanceServer"

    def _status(self) -> dict[str, Any]:
        return safe_public_status(
            self.server.state_path,
            self.server.lease_path,
            self.server.root,
            self.server.transaction_id,
            self.server.silence_seconds,
        )

    def _send_headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("CDN-Cache-Control", "no-store")
        self.send_header("Surrogate-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Retry-After", "4")
        self.end_headers()

    def _respond(self, include_body: bool) -> None:
        path = urlsplit(self.path).path
        status = self._status()
        if path == "/__streamhome/update-status":
            body = (json.dumps(status, separators=(",", ":")) + "\n").encode("utf-8")
            self._send_headers(HTTPStatus.OK, "application/json; charset=utf-8", len(body))
        else:
            body = maintenance_page(status)
            self._send_headers(HTTPStatus.SERVICE_UNAVAILABLE, "text/html; charset=utf-8", len(body))
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._respond(True)

    def do_HEAD(self) -> None:
        self._respond(False)

    def log_message(self, format: str, *args: object) -> None:
        return


class MaintenanceServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        root: Path,
        state_path: Path,
        lease_path: Path,
        transaction_id: str,
        silence_seconds: float,
    ) -> None:
        self.root = root
        self.state_path = state_path
        self.lease_path = lease_path
        self.transaction_id = transaction_id
        self.silence_seconds = silence_seconds
        super().__init__(address, MaintenanceHandler)


def main() -> None:
    default_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Temporary StreamHome maintenance responder")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--lease-file", type=Path)
    parser.add_argument("--transaction-id", default="")
    parser.add_argument("--controller-silence-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if not 2 <= args.controller_silence_seconds <= 300:
        raise SystemExit("controller silence must be between 2 and 300 seconds")
    root = args.root.resolve()
    state_path = (args.state_file or root / ".run" / "update-state.json").resolve()
    lease_path = (args.lease_file or root / ".run" / "update-lease.json").resolve()
    state = read_json(state_path)
    transaction_id = args.transaction_id or str(state.get("transaction_id") or "")
    server = MaintenanceServer(
        ("0.0.0.0", args.port),
        root,
        state_path,
        lease_path,
        transaction_id,
        args.controller_silence_seconds,
    )
    coordinator = RecoveryCoordinator(root, state_path, lease_path, transaction_id, args.controller_silence_seconds, server)
    monitor = threading.Thread(target=coordinator.run, name="streamhome-update-recovery", daemon=True)
    monitor.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        coordinator.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
