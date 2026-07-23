from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def git(command: list[str], *, cwd: Path) -> None:
    result = run(["git", *command], cwd=cwd)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(command)} failed:\n{result.stdout}\n{result.stderr}")


def create_fixture_repository(directory: Path) -> None:
    directory.mkdir(parents=True)
    git(["init", "-b", "main"], cwd=directory)
    git(["config", "user.email", "setup-test@streamhome.invalid"], cwd=directory)
    git(["config", "user.name", "StreamHome Setup Test"], cwd=directory)
    files = {
        "setup.sh": "#!/usr/bin/env bash\nset -e\nprintf 'ready' > .setup-ran\n",
        "setup.bat": "@echo off\r\n> .setup-ran-windows echo ready\r\n",
        "install.sh": "#!/usr/bin/env bash\nexit 0\n",
        "start.sh": "#!/usr/bin/env bash\nexit 0\n",
        "stop.sh": "#!/usr/bin/env bash\nexit 0\n",
        "test.sh": "#!/usr/bin/env bash\nexit 0\n",
    }
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8", newline="")
    git(["add", "."], cwd=directory)
    git(["update-index", "--chmod=+x", "setup.sh", "install.sh", "start.sh", "stop.sh", "test.sh"], cwd=directory)
    git(["commit", "-m", "fixture"], cwd=directory)


def bash_path(path: Path) -> str:
    resolved = str(path.resolve())
    if os.name != "nt":
        return path.resolve().as_posix()
    drive, tail = os.path.splitdrive(resolved)
    return f"/{drive[0].lower()}{tail.replace(os.sep, '/')}"


class SetupScriptContracts(unittest.TestCase):
    def test_documented_bootstrap_contracts_and_safe_runtime_rules(self) -> None:
        install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
        install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
        setup_sh = (ROOT / "setup.sh").read_text(encoding="utf-8")
        start_sh = (ROOT / "start.sh").read_text(encoding="utf-8")
        stop_sh = (ROOT / "stop.sh").read_text(encoding="utf-8")
        windows_setup = (ROOT / "scripts" / "setup-windows.ps1").read_text(encoding="utf-8")
        windows_stop = (ROOT / "scripts" / "stop-windows.ps1").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("https://github.com/WaqSea/StreamHome.git", install_sh)
        self.assertIn("status --porcelain --untracked-files=normal", install_sh)
        self.assertIn("merge --ff-only", install_sh)
        self.assertIn("exec ./setup.sh", install_sh)
        self.assertIn("cmd.exe /d /c setup.bat", install_ps1)
        self.assertIn("npm ci", setup_sh)
        self.assertIn("npm run build", setup_sh)
        self.assertIn("listener-inspector", setup_sh)
        self.assertIn("lsof", setup_sh)
        self.assertIn("exec \"$ROOT_DIR/start.sh\"", setup_sh)
        self.assertIn("do not repeat setup", setup_sh)
        self.assertIn('"$ROOT_DIR/stop.sh" --startup', start_sh)
        self.assertIn("setup and dependencies do not need to be repeated", start_sh)
        self.assertIn("is_streamhome_process", stop_sh)
        self.assertIn("stop_process_tree", stop_sh)
        self.assertIn("It was not stopped.", stop_sh)
        self.assertIn('Invoke-Checked $npm @("ci")', windows_setup)
        self.assertNotIn("server\\cli.py --setup", windows_setup)
        self.assertIn("taskkill.exe /PID", windows_stop)
        self.assertNotIn("/im python.exe", windows_stop.lower())
        self.assertNotIn("/im node.exe", windows_stop.lower())
        self.assertIn("releases/tag/v0.1.0-alpha.1", readme)

    def test_unix_bootstrap_clones_runs_setup_and_refuses_dirty_update(self) -> None:
        bash = shutil.which("bash")
        if os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
            bash = str(candidate) if candidate.is_file() else bash
        if not bash:
            self.skipTest("Bash is not installed")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as temporary:
            root = Path(temporary)
            remote = root / "remote"
            install_directory = root / "unix-install"
            create_fixture_repository(remote)
            fixture_url = remote.resolve().as_uri()
            source = (ROOT / "install.sh").read_text(encoding="utf-8")
            source = source.replace("https://github.com/WaqSea/StreamHome.git", fixture_url)
            installer = root / "install-fixture.sh"
            installer.write_text(source, encoding="utf-8", newline="\n")

            environment = os.environ.copy()
            environment["STREAMHOME_INSTALL_DIR"] = bash_path(install_directory)
            environment["STREAMHOME_REF"] = "main"
            first = run([bash, "-lc", f"'{bash_path(installer)}'"], cwd=root, env=environment)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual((install_directory / ".setup-ran").read_text(encoding="utf-8"), "ready")

            (install_directory / ".setup-ran").unlink()
            local_change = install_directory / "local-change.txt"
            local_change.write_text("preserve me", encoding="utf-8")
            second = run([bash, "-lc", f"'{bash_path(installer)}'"], cwd=root, env=environment)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("local changes", (second.stdout + second.stderr).lower())
            self.assertEqual(local_change.read_text(encoding="utf-8"), "preserve me")

    @unittest.skipIf(os.name == "nt", "Unix listener ownership uses /proc or lsof")
    def test_unix_stop_recovers_owned_orphan_and_preserves_unrelated_listener(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("Bash is not installed")
        if not any(shutil.which(command) for command in ("lsof", "ss", "fuser")):
            self.skipTest("No supported listener-inspection command is installed")

        def unused_port() -> int:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                return int(probe.getsockname()[1])

        def wait_for_listener(port: int) -> None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        return
                time.sleep(0.05)
            self.fail(f"Listener on port {port} did not start")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as temporary:
            fixture = Path(temporary)
            server_directory = fixture / "server"
            unrelated_directory = fixture / "unrelated"
            server_directory.mkdir()
            unrelated_directory.mkdir()
            shutil.copy2(ROOT / "stop.sh", fixture / "stop.sh")
            (fixture / ".env").write_text("WEB_PORT=3000\n", encoding="utf-8")
            (fixture / ".run").mkdir()
            listener_source = (
                "import socket, sys, time\n"
                "listener = socket.socket()\n"
                "listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                "listener.bind(('127.0.0.1', int(sys.argv[1])))\n"
                "listener.listen()\n"
                "time.sleep(60)\n"
            )
            (server_directory / "main.py").write_text(listener_source, encoding="utf-8")
            (unrelated_directory / "other.py").write_text(listener_source, encoding="utf-8")

            owned_port = unused_port()
            owned = subprocess.Popen(
                [sys.executable, "main.py", str(owned_port)],
                cwd=server_directory,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                wait_for_listener(owned_port)
                recovered = run(
                    [bash, str(fixture / "stop.sh"), "--startup", "--recover-port", str(owned_port)],
                    cwd=fixture,
                )
                self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                self.assertIn("earlier StreamHome process", recovered.stdout)
                owned.wait(timeout=5)
            finally:
                if owned.poll() is None:
                    owned.kill()
                    owned.wait(timeout=5)

            unrelated_port = unused_port()
            unrelated = subprocess.Popen(
                [sys.executable, "other.py", str(unrelated_port)],
                cwd=unrelated_directory,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                wait_for_listener(unrelated_port)
                stale_record = fixture / ".run" / "backend.pid"
                stale_record.write_text(str(unrelated.pid), encoding="utf-8")
                preserved = run(
                    [bash, str(fixture / "stop.sh"), "--startup", "--recover-port", str(unrelated_port)],
                    cwd=fixture,
                )
                self.assertEqual(preserved.returncode, 0, preserved.stdout + preserved.stderr)
                self.assertIsNone(unrelated.poll())
                self.assertIn(f"PID {unrelated.pid}", preserved.stderr)
                self.assertIn("It was not stopped.", preserved.stderr)
                self.assertFalse(stale_record.exists())
            finally:
                unrelated.kill()
                unrelated.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "PowerShell bootstrap test is Windows-specific")
    def test_windows_bootstrap_clones_runs_setup_and_refuses_dirty_update(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as temporary:
            root = Path(temporary)
            remote = root / "remote"
            install_directory = root / "windows-install"
            create_fixture_repository(remote)
            fixture_url = remote.resolve().as_uri()
            source = (ROOT / "install.ps1").read_text(encoding="utf-8")
            source = source.replace("https://github.com/WaqSea/StreamHome.git", fixture_url)
            installer = root / "install-fixture.ps1"
            installer.write_text(source, encoding="utf-8")

            environment = os.environ.copy()
            environment["STREAMHOME_INSTALL_DIR"] = str(install_directory)
            environment["STREAMHOME_REF"] = "main"
            first = run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installer)],
                cwd=root,
                env=environment,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual((install_directory / ".setup-ran-windows").read_text(encoding="utf-8").strip(), "ready")

            (install_directory / ".setup-ran-windows").unlink()
            local_change = install_directory / "local-change.txt"
            local_change.write_text("preserve me", encoding="utf-8")
            second = run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installer)],
                cwd=root,
                env=environment,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("local changes", (second.stdout + second.stderr).lower())
            self.assertEqual(local_change.read_text(encoding="utf-8"), "preserve me")


if __name__ == "__main__":
    unittest.main(verbosity=2)
