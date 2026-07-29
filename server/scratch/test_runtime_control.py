from __future__ import annotations

import tempfile
import os
import socket
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scratch import runtime_control


class RuntimeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "server").mkdir()
        (self.root / "web").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def process(
        self,
        *,
        pid: int,
        ppid: int = 1,
        pgid: int | None = None,
        sid: int | None = None,
        start_ticks: str = "100",
        cwd: Path | None = None,
        command: str = "",
        environment: dict[str, str] | None = None,
    ) -> runtime_control.ProcessInfo:
        return runtime_control.ProcessInfo(
            pid=pid,
            ppid=ppid,
            pgid=pgid if pgid is not None else pid,
            sid=sid if sid is not None else pid,
            start_ticks=start_ticks,
            cwd=str(cwd or self.root),
            command=command,
            environment=environment or {},
        )

    def marked_environment(self, service: str, token: str = "token") -> dict[str, str]:
        return {
            runtime_control.RUNTIME_ROOT_KEY: str(self.root),
            runtime_control.RUNTIME_SERVICE_KEY: service,
            runtime_control.RUNTIME_TOKEN_KEY: token,
        }

    def test_marked_identity_requires_the_exact_installation_and_service(self) -> None:
        backend = self.process(
            pid=10,
            cwd=Path("/"),
            command="python unrelated.py",
            environment=self.marked_environment("backend"),
        )
        self.assertEqual(runtime_control.owned_service(backend, self.root), "backend")

        wrong_root = self.process(
            pid=11,
            environment={
                runtime_control.RUNTIME_ROOT_KEY: str(self.root / "other"),
                runtime_control.RUNTIME_SERVICE_KEY: "backend",
                runtime_control.RUNTIME_TOKEN_KEY: "token",
            },
        )
        self.assertEqual(runtime_control.owned_service(wrong_root, self.root), "")

        unknown_service = self.process(
            pid=12,
            environment=self.marked_environment("updater"),
        )
        self.assertEqual(runtime_control.owned_service(unknown_service, self.root), "")

    def test_runtime_record_rejects_pid_reuse_start_or_token_mismatch(self) -> None:
        info = self.process(
            pid=20,
            start_ticks="500",
            environment=self.marked_environment("web", "expected-token"),
        )
        record = {
            "pid": 20,
            "start_ticks": "500",
            "root": str(self.root),
            "service": "web",
            "token": "expected-token",
        }
        self.assertTrue(runtime_control.record_matches(info, self.root, "web", record))
        self.assertFalse(
            runtime_control.record_matches(
                info,
                self.root,
                "web",
                {**record, "start_ticks": "499"},
            )
        )
        self.assertFalse(
            runtime_control.record_matches(
                info,
                self.root,
                "web",
                {**record, "token": "different-token"},
            )
        )

    def test_legacy_entry_points_are_scoped_by_cwd_and_command(self) -> None:
        backend = self.process(
            pid=30,
            cwd=self.root / "server",
            command="python -m uvicorn main:app --host 127.0.0.1 --port 8000",
        )
        web = self.process(
            pid=31,
            cwd=self.root / "web",
            command="node node_modules/tsx/dist/cli.mjs server.ts",
        )
        outside = self.process(
            pid=32,
            cwd=self.root.parent,
            command="python -m uvicorn main:app --host 127.0.0.1 --port 8000",
        )
        self.assertEqual(runtime_control.legacy_service(backend, self.root), "backend")
        self.assertEqual(runtime_control.legacy_service(web, self.root), "web")
        self.assertEqual(runtime_control.legacy_service(outside, self.root), "")

    def test_owned_discovery_expands_to_unmarked_service_descendants(self) -> None:
        backend = self.process(
            pid=40,
            command="python -m uvicorn main:app",
            environment=self.marked_environment("backend"),
        )
        ffmpeg = self.process(
            pid=41,
            ppid=40,
            pgid=40,
            sid=40,
            command="ffmpeg -i input output",
        )
        nested = self.process(
            pid=42,
            ppid=41,
            pgid=40,
            sid=40,
            command="rclone copy source destination",
        )
        unrelated = self.process(pid=43, command="sleep 60")
        snapshot = {
            process.pid: process
            for process in (backend, ffmpeg, nested, unrelated)
        }
        with patch.object(runtime_control, "ancestor_pids", return_value=set()):
            owned = runtime_control.discover_owned(snapshot, self.root)
        self.assertEqual(
            owned,
            {
                40: "backend",
                41: "backend",
                42: "backend",
            },
        )

    def test_process_groups_are_signaled_only_when_every_visible_member_is_owned(self) -> None:
        backend = self.process(
            pid=50,
            pgid=50,
            environment=self.marked_environment("backend"),
        )
        child = self.process(pid=51, ppid=50, pgid=50)
        unrelated_same_group = self.process(pid=52, pgid=50)
        owned = {50: "backend", 51: "backend"}
        with patch.object(runtime_control.os, "getpgrp", return_value=999, create=True):
            self.assertEqual(
                runtime_control.safe_owned_groups(
                    {50: backend, 51: child},
                    owned,
                ),
                {50},
            )
            self.assertEqual(
                runtime_control.safe_owned_groups(
                    {50: backend, 51: child, 52: unrelated_same_group},
                    owned,
                ),
                set(),
            )

    @unittest.skipIf(os.name == "nt", "Listener ownership readiness requires native Linux /proc semantics")
    def test_port_readiness_rejects_an_active_listener(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = int(listener.getsockname()[1])
            self.assertFalse(runtime_control.port_is_available(port))

    @unittest.skipIf(os.name == "nt", "TCP TIME_WAIT inspection requires native Linux /proc semantics")
    def test_port_readiness_accepts_time_wait_without_a_listener(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        accepted, _ = listener.accept()
        accepted.shutdown(socket.SHUT_WR)
        accepted.close()
        listener.close()
        client.recv(1)
        client.close()

        hexadecimal_port = f"{port:04X}"
        deadline = time.monotonic() + 2
        found_time_wait = False
        while time.monotonic() < deadline:
            for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
                try:
                    lines = table.read_text(encoding="utf-8").splitlines()[1:]
                except OSError:
                    continue
                if any(
                    len(fields := line.split()) >= 4
                    and fields[1].rsplit(":", 1)[-1].upper() == hexadecimal_port
                    and fields[3] == "06"
                    for line in lines
                ):
                    found_time_wait = True
                    break
            if found_time_wait:
                break
            time.sleep(0.05)
        self.assertTrue(found_time_wait, "The test connection did not enter TIME_WAIT")
        self.assertTrue(runtime_control.port_is_available(port))


if __name__ == "__main__":
    unittest.main(verbosity=2)
