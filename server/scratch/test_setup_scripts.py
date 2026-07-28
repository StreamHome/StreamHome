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
REPOSITORY_URL = "https://github.com/StreamHome/StreamHome.git"


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
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
        ".gitignore": ".setup-ran\n.setup-args\n",
        "setup.sh": (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            "printf 'ready' > .setup-ran\n"
            "printf '%s\\n' \"$@\" > .setup-args\n"
        ),
        "install.sh": "#!/usr/bin/env bash\nexit 0\n",
        "start.sh": "#!/usr/bin/env bash\nexit 0\n",
        "stop.sh": "#!/usr/bin/env bash\nexit 0\n",
        "test.sh": "#!/usr/bin/env bash\nexit 0\n",
    }
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8", newline="")
    git(["add", "."], cwd=directory)
    git(
        [
            "update-index",
            "--chmod=+x",
            "setup.sh",
            "install.sh",
            "start.sh",
            "stop.sh",
            "test.sh",
        ],
        cwd=directory,
    )
    git(["commit", "-m", "fixture"], cwd=directory)


def bash_path(path: Path) -> str:
    resolved = str(path.resolve())
    if os.name != "nt":
        return path.resolve().as_posix()
    drive, tail = os.path.splitdrive(resolved)
    return f"/{drive[0].lower()}{tail.replace(os.sep, '/')}"


def bash_command() -> str | None:
    bash = shutil.which("bash")
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        bash = str(candidate) if candidate.is_file() else bash
    return bash


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class SetupScriptContracts(unittest.TestCase):
    def test_linux_bootstrap_setup_and_lifecycle_contracts(self) -> None:
        install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
        setup_sh = (ROOT / "setup.sh").read_text(encoding="utf-8")
        start_sh = (ROOT / "start.sh").read_text(encoding="utf-8")
        stop_sh = (ROOT / "stop.sh").read_text(encoding="utf-8")
        test_sh = (ROOT / "test.sh").read_text(encoding="utf-8")
        cli_py = (ROOT / "server" / "cli.py").read_text(encoding="utf-8")

        self.assertIn(REPOSITORY_URL, install_sh)
        self.assertIn("remote set-url origin", install_sh)
        self.assertIn("https://github.com/WaqSea/StreamHome.git", install_sh)
        self.assertIn("status --porcelain --untracked-files=normal", install_sh)
        self.assertIn("merge --ff-only", install_sh)
        self.assertIn(".streamhome-install.", install_sh)
        self.assertIn("install.lock", install_sh)
        self.assertIn("setup_args+=(--no-start)", install_sh)
        self.assertIn("./setup.sh", install_sh)
        self.assertIn('INSTALL_REF="${STREAMHOME_REF:-main}"', install_sh)
        self.assertIn("Git branch or tag (default: main)", install_sh)
        self.assertIn('[[ "$(uname -s)" == "Linux" ]]', install_sh)
        self.assertNotIn("brew install", install_sh)

        self.assertIn("npm ci", setup_sh)
        self.assertIn("npm run build", setup_sh)
        self.assertIn("-m pip check", setup_sh)
        self.assertIn("listener-inspector", setup_sh)
        self.assertIn('MIN_RCLONE_VERSION="1.68"', setup_sh)
        self.assertIn('RCLONE_INSTALL_VERSION="1.74.4"', setup_sh)
        self.assertIn("downloads.rclone.org/v${RCLONE_INSTALL_VERSION}", setup_sh)
        self.assertNotIn("rclone-current-${os_name}-${arch}.zip", setup_sh)
        self.assertNotIn("${archive_url}.sha256", setup_sh)
        self.assertIn("hashlib.sha256", setup_sh)
        self.assertIn("do not repeat setup", setup_sh)
        self.assertIn('"$ROOT_DIR/stop.sh" --quiet', setup_sh)

        self.assertIn('"$ROOT_DIR/stop.sh" --startup --lock-held', start_sh)
        self.assertIn("detect_server_ip", start_sh)
        self.assertIn("STREAMHOME_PUBLIC_URL_EXPLICIT", start_sh)
        self.assertIn("-m uvicorn main:app --host 127.0.0.1 --port 8000", start_sh)
        self.assertNotIn('"$BACKEND_PYTHON" main.py', start_sh)
        self.assertIn("wait_for_services", start_sh)
        self.assertIn("lifecycle.lock", start_sh)
        self.assertIn("Setup URL: %s/setup", start_sh)

        self.assertIn("is_streamhome_process", stop_sh)
        self.assertIn("stop_process_tree", stop_sh)
        self.assertIn("It was not stopped.", stop_sh)
        self.assertIn("its PID record was preserved", stop_sh)
        self.assertIn("Shutdown did not complete cleanly", stop_sh)

        self.assertIn("--server-only", test_sh)
        self.assertIn("--web-only", test_sh)
        self.assertIn("--syntax-only", test_sh)
        self.assertIn("Port 8000 is active", test_sh)
        self.assertIn("shellcheck -x", test_sh)
        self.assertIn("Generated TypeScript build metadata must not be tracked", test_sh)

        self.assertIn("./venv/bin/python server/cli.py", cli_py)
        self.assertNotIn("start.bat", cli_py)
        self.assertNotIn("start_background.sh", cli_py)

    def test_linux_bootstrap_is_atomic_forwards_options_and_refuses_dirty_update(self) -> None:
        bash = bash_command()
        if not bash:
            self.skipTest("Bash is not installed")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as temporary:
            root = Path(temporary)
            remote = root / "remote"
            install_directory = root / "linux-install"
            create_fixture_repository(remote)
            fixture_url = remote.resolve().as_uri()
            source = (ROOT / "install.sh").read_text(encoding="utf-8")
            source = source.replace(REPOSITORY_URL, fixture_url)
            if os.name == "nt":
                source = source.replace(
                    '[[ "$(uname -s)" == "Linux" ]] || fail "The alpha server installer supports Linux only."',
                    "true",
                )
            installer = root / "install-fixture.sh"
            installer.write_text(source, encoding="utf-8", newline="\n")
            installer.chmod(0o755)

            environment = os.environ.copy()
            environment["STREAMHOME_INSTALL_DIR"] = bash_path(install_directory)
            environment["STREAMHOME_REF"] = "main"
            first = run([bash, "-lc", f"'{bash_path(installer)}'"], cwd=root, env=environment)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual((install_directory / ".setup-ran").read_text(encoding="utf-8"), "ready")

            (remote / "release.txt").write_text("next release\n", encoding="utf-8")
            git(["add", "release.txt"], cwd=remote)
            git(["commit", "-m", "next fixture release"], cwd=remote)
            expected_commit = run(["git", "rev-parse", "HEAD"], cwd=remote).stdout.strip()

            (install_directory / ".setup-ran").unlink()
            forwarded = run(
                [bash, "-lc", f"'{bash_path(installer)}' --no-start --skip-system-packages"],
                cwd=root,
                env=environment,
            )
            self.assertEqual(forwarded.returncode, 0, forwarded.stdout + forwarded.stderr)
            self.assertEqual(
                (install_directory / ".setup-args").read_text(encoding="utf-8").splitlines(),
                ["--no-start", "--skip-system-packages"],
            )
            installed_commit = run(["git", "rev-parse", "HEAD"], cwd=install_directory).stdout.strip()
            self.assertEqual(installed_commit, expected_commit)
            self.assertEqual((install_directory / "release.txt").read_text(encoding="utf-8"), "next release\n")

            (install_directory / ".setup-ran").unlink()
            local_change = install_directory / "local-change.txt"
            local_change.write_text("preserve me", encoding="utf-8")
            dirty = run([bash, "-lc", f"'{bash_path(installer)}'"], cwd=root, env=environment)
            self.assertNotEqual(dirty.returncode, 0)
            self.assertIn("local changes", (dirty.stdout + dirty.stderr).lower())
            self.assertEqual(local_change.read_text(encoding="utf-8"), "preserve me")

            failed_install = root / "failed-install"
            failed_environment = environment.copy()
            failed_environment["STREAMHOME_INSTALL_DIR"] = bash_path(failed_install)
            failed_environment["STREAMHOME_REF"] = "missing-ref"
            failed = run([bash, "-lc", f"'{bash_path(installer)}'"], cwd=root, env=failed_environment)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(failed_install.exists())
            self.assertFalse(Path(f"{failed_install}.install.lock").exists())
            self.assertEqual(list(root.glob(".streamhome-install.*")), [])

    def test_linux_stop_executes_without_pid_records_under_nounset(self) -> None:
        bash = bash_command()
        if not bash:
            self.skipTest("Bash is not installed")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as temporary:
            fixture = Path(temporary)
            script = fixture / "stop.sh"
            shutil.copy2(ROOT / "stop.sh", script)
            (fixture / ".env").write_text("WEB_PORT=3000\n", encoding="utf-8")
            (fixture / ".run").mkdir()
            result = run(
                [bash, "-lc", f"'{bash_path(script)}' --quiet --recover-port {unused_port()}"],
                cwd=fixture,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("unbound variable", (result.stdout + result.stderr).lower())
            self.assertFalse((fixture / ".run" / "lifecycle.lock").exists())

    @unittest.skipIf(os.name == "nt", "Linux listener ownership uses /proc or lsof")
    def test_linux_stop_recovers_owned_orphan_and_preserves_unrelated_listener(self) -> None:
        bash = bash_command()
        if not bash:
            self.skipTest("Bash is not installed")
        if not any(shutil.which(command) for command in ("lsof", "ss", "fuser")):
            self.skipTest("No supported listener-inspection command is installed")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
