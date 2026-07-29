from __future__ import annotations

import os
import json
import sqlite3
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
        "restart.sh": "#!/usr/bin/env bash\nexit 0\n",
        "update.sh": (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            "if [[ \"${1:-}\" == \"--manual-execute\" ]]; then\n"
            "  target=\"$2\"\n"
            "  root=\"$6\"\n"
            "  git -C \"$root\" merge --ff-only \"$target\"\n"
            "  (cd \"$root\" && ./setup.sh --no-start --skip-system-packages)\n"
            "fi\n"
        ),
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
            "restart.sh",
            "update.sh",
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
        restart_sh = (ROOT / "restart.sh").read_text(encoding="utf-8")
        update_sh = (ROOT / "update.sh").read_text(encoding="utf-8")
        start_sh = (ROOT / "start.sh").read_text(encoding="utf-8")
        stop_sh = (ROOT / "stop.sh").read_text(encoding="utf-8")
        test_sh = (ROOT / "test.sh").read_text(encoding="utf-8")
        setup_py = (ROOT / "server" / "routes" / "setup.py").read_text(encoding="utf-8")
        cli_py = (ROOT / "server" / "cli.py").read_text(encoding="utf-8")

        self.assertIn(REPOSITORY_URL, install_sh)
        self.assertIn("remote set-url origin", install_sh)
        self.assertIn("https://github.com/WaqSea/StreamHome.git", install_sh)
        self.assertIn("status --porcelain --untracked-files=normal", install_sh)
        self.assertIn("merge-base --is-ancestor", install_sh)
        self.assertIn(".streamhome-install.", install_sh)
        self.assertIn("install.lock", install_sh)
        self.assertIn("--manual-execute", install_sh)
        self.assertIn("health-gated update", install_sh)
        self.assertNotIn('git -C "$INSTALL_DIR" checkout "$INSTALL_REF"', install_sh)
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

        self.assertIn('nohup bash "$SCRIPT_PATH" --execute', restart_sh)
        self.assertIn('exec bash "$ROOT_DIR/start.sh"', restart_sh)
        self.assertIn("restart.log", restart_sh)
        self.assertIn('"restart.sh"', setup_py)
        self.assertIn("handoff.wait", setup_py)

        self.assertIn('nohup bash "$controller" --execute', update_sh)
        self.assertIn("--manual-execute", update_sh)
        self.assertIn("preflight_target", update_sh)
        self.assertIn("./setup.sh --no-start --skip-system-packages", update_sh)
        self.assertIn("/api/update/handoff", update_sh)
        self.assertIn("X-StreamHome-Update-Handoff", update_sh)
        self.assertIn("create_database_checkpoint", update_sh)
        self.assertIn("start_maintenance", update_sh)
        self.assertIn("maintenance.pid", update_sh)
        self.assertIn("update-lease.json", update_sh)
        self.assertIn("transaction_id", update_sh)
        self.assertIn("--controller-silence-seconds", update_sh)
        self.assertIn("--finalize-recovery", update_sh)
        self.assertIn("update-diagnostics.json", update_sh)
        self.assertIn("prepare_python_wheelhouse", update_sh)
        self.assertIn("--no-index", update_sh)
        self.assertIn("activate_prepared_web", update_sh)
        self.assertIn("restore_previous_web", update_sh)
        self.assertIn("emergency rollback", update_sh)
        self.assertIn("rollback_release", update_sh)
        self.assertIn("recover_interrupted_release", update_sh)
        self.assertIn('git -C "$ROOT_DIR" reset --hard "$OLD_COMMIT"', update_sh)
        self.assertIn('"$ROOT_DIR/start.sh"', update_sh)
        self.assertIn("rollback_failed", update_sh)

        self.assertIn('"$ROOT_DIR/stop.sh" --startup --lock-held', start_sh)
        self.assertIn("detect_server_ip", start_sh)
        self.assertIn("STREAMHOME_PUBLIC_URL_EXPLICIT", start_sh)
        self.assertIn("-m uvicorn main:app --host 127.0.0.1 --port 8000", start_sh)
        self.assertNotIn('"$BACKEND_PYTHON" main.py', start_sh)
        self.assertIn("wait_for_services", start_sh)
        self.assertIn("wait_for_port_release", start_sh)
        self.assertIn("wait_for_active_update", start_sh)
        self.assertIn("STREAMHOME_UPDATE_WAIT_SECONDS", start_sh)
        self.assertIn("STREAMHOME_STARTUP_TIMEOUT_SECONDS", start_sh)
        self.assertIn("Startup attempt", start_sh)
        self.assertIn("STREAMHOME_FINALIZE_UPDATE_RECOVERY", start_sh)
        self.assertIn("lifecycle.lock", start_sh)
        self.assertIn("--recover-interrupted", start_sh)
        self.assertIn("Setup URL: %s/setup", start_sh)

        self.assertIn("is_streamhome_process", stop_sh)
        self.assertIn("stop_process_tree", stop_sh)
        self.assertIn("maintenance_server.py", stop_sh)
        self.assertIn("stop_recorded_process maintenance", stop_sh)
        self.assertIn("It was not stopped.", stop_sh)
        self.assertIn("its PID record was preserved", stop_sh)
        self.assertIn("Shutdown did not complete cleanly", stop_sh)

        self.assertIn("--server-only", test_sh)
        self.assertIn("--web-only", test_sh)
        self.assertIn("--syntax-only", test_sh)
        self.assertIn("Port 8000 is active", test_sh)
        self.assertIn("shellcheck -x", test_sh)
        self.assertIn("Generated runtime or build metadata must not be tracked", test_sh)
        self.assertIn("server/system_profile.json", test_sh)
        self.assertIn('"$ROOT_DIR/restart.sh"', test_sh)
        self.assertIn('"$ROOT_DIR/update.sh"', test_sh)

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

    @unittest.skipIf(os.name == "nt", "Detached restart parentage requires native Linux process semantics")
    def test_linux_restart_handoff_is_reparented_before_start(self) -> None:
        bash = bash_command()
        if not bash:
            self.skipTest("Bash is not installed")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as temporary:
            fixture = Path(temporary)
            shutil.copy2(ROOT / "restart.sh", fixture / "restart.sh")
            start_script = fixture / "start.sh"
            start_script.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$PPID\" > start.ppid\n"
                "printf 'restart reached start.sh\\n'\n",
                encoding="utf-8",
                newline="\n",
            )
            environment = os.environ.copy()
            environment["STREAMHOME_RESTART_DELAY_SECONDS"] = "0.5"
            handoff = subprocess.Popen(
                [bash, str(fixture / "restart.sh")],
                cwd=fixture,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            handoff_pid = handoff.pid
            self.assertEqual(handoff.wait(timeout=5), 0)

            marker = fixture / "start.ppid"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.05)
            self.assertTrue(marker.exists(), (fixture / "restart.log").read_text(encoding="utf-8"))
            self.assertNotEqual(int(marker.read_text(encoding="utf-8").strip()), handoff_pid)
            self.assertIn(
                "restart reached start.sh",
                (fixture / "restart.log").read_text(encoding="utf-8"),
            )

    @unittest.skipIf(os.name == "nt", "Update cutover and rollback require native Linux lifecycle semantics")
    def test_linux_update_rolls_back_database_and_release_when_new_web_start_fails(self) -> None:
        bash = bash_command()
        if not bash:
            self.skipTest("Bash is not installed")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as temporary:
            fixture = Path(temporary)
            remote = fixture / "remote"
            installed = fixture / "installed"
            fake_bin = fixture / "fake-bin"
            fake_bin.mkdir()
            remote.mkdir()
            git(["init", "-b", "main"], cwd=remote)
            git(["config", "user.email", "update-test@streamhome.invalid"], cwd=remote)
            git(["config", "user.name", "StreamHome Update Test"], cwd=remote)

            helper = (ROOT / "server" / "scratch" / "maintenance_server.py").read_text(encoding="utf-8")
            old_files = {
                ".gitignore": ".run/\n*.log\nvenv/\nserver/database.db\n",
                "setup.sh": (
                    "#!/usr/bin/env bash\n"
                    "mkdir -p venv/bin\n"
                    "printf '#!/usr/bin/env bash\\nexec python3 \"$@\"\\n' > venv/bin/python\n"
                    "chmod +x venv/bin/python\n"
                ),
                "test.sh": "#!/usr/bin/env bash\nexit 0\n",
                "stop.sh": "#!/usr/bin/env bash\nexit 0\n",
                "start.sh": "#!/usr/bin/env bash\ncd \"$(dirname \"$0\")\"\nprintf 'healthy-old\\n' > healthy.marker\nexit 0\n",
                ".env": f"WEB_PORT={unused_port()}\n",
                "server/requirements.txt": "",
                "server/requirements.lock": "",
                "server/scratch/maintenance_server.py": helper,
                "web/node_modules/runtime.txt": "known-working dependencies\n",
                "web/dist/index.html": "known-working assets\n",
            }
            for relative, content in old_files.items():
                target = remote / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
            git(["add", "."], cwd=remote)
            git(["update-index", "--chmod=+x", "setup.sh", "test.sh", "stop.sh", "start.sh"], cwd=remote)
            git(["commit", "-m", "known working release"], cwd=remote)
            old_commit = run(["git", "rev-parse", "HEAD"], cwd=remote).stdout.strip()

            target_start = (
                "#!/usr/bin/env bash\n"
                "cd \"$(dirname \"$0\")\"\n"
                "python3 - <<'PY'\n"
                "import sqlite3\n"
                "connection = sqlite3.connect('server/database.db')\n"
                "connection.execute(\"UPDATE update_probe SET value='mutated'\")\n"
                "connection.commit()\n"
                "connection.close()\n"
                "PY\n"
                "exit 1\n"
            )
            (remote / "start.sh").write_text(target_start, encoding="utf-8", newline="\n")
            git(["add", "start.sh"], cwd=remote)
            git(["commit", "-m", "broken candidate startup"], cwd=remote)
            target_commit = run(["git", "rev-parse", "HEAD"], cwd=remote).stdout.strip()

            clone = run(["git", "clone", remote.as_uri(), str(installed)], cwd=fixture)
            self.assertEqual(clone.returncode, 0, clone.stdout + clone.stderr)
            git(["reset", "--hard", old_commit], cwd=installed)
            prepared = run([bash, str(installed / "setup.sh"), "--no-start"], cwd=installed)
            self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
            (installed / ".run").mkdir()
            token_file = installed / ".run" / "update-handoff.test.token"
            token_file.write_text("test-token", encoding="utf-8")
            database = installed / "server" / "database.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE update_probe (value TEXT NOT NULL)")
            connection.execute("INSERT INTO update_probe (value) VALUES ('original')")
            connection.commit()
            connection.close()

            fake_curl = fake_bin / "curl"
            fake_curl.write_text("#!/usr/bin/env bash\nprintf '200'\n", encoding="utf-8", newline="\n")
            fake_curl.chmod(0o755)
            controller = fixture / "controller.sh"
            controller_source = (ROOT / "update.sh").read_text(encoding="utf-8").replace(
                "https://github.com/StreamHome/StreamHome.git",
                remote.as_uri(),
            )
            controller_source = controller_source.replace(
                "        ./venv/bin/python -m compileall -q server\n"
                "        PYTHONPATH=server ./venv/bin/python server/scratch/test_update_system.py\n",
                "        true\n",
            )
            controller.write_text(controller_source, encoding="utf-8", newline="\n")
            controller.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            result = run(
                [
                    bash,
                    str(controller),
                    "--execute",
                    target_commit,
                    old_commit,
                    "false",
                    str(token_file),
                    str(installed),
                ],
                cwd=fixture,
                env=environment,
                timeout=90,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(run(["git", "rev-parse", "HEAD"], cwd=installed).stdout.strip(), old_commit)
            self.assertEqual((installed / "healthy.marker").read_text(encoding="utf-8"), "healthy-old\n")
            self.assertEqual(
                (installed / "web" / "node_modules" / "runtime.txt").read_text(encoding="utf-8"),
                "known-working dependencies\n",
            )
            self.assertEqual(
                (installed / "web" / "dist" / "index.html").read_text(encoding="utf-8"),
                "known-working assets\n",
            )
            self.assertFalse((installed / ".run" / "maintenance.pid").exists())
            connection = sqlite3.connect(database)
            try:
                value = connection.execute("SELECT value FROM update_probe").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(value, "original")
            status_payload = json.loads((installed / ".run" / "update-state.json").read_text(encoding="utf-8"))
            self.assertEqual(status_payload["phase"], "rolled_back")
            self.assertEqual(status_payload["failed_target"], target_commit)

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
