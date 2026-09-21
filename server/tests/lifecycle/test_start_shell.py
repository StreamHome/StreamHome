from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


class StartShellRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.repository_root = Path(__file__).resolve().parents[3]
        source = (self.repository_root / "start.sh").read_text(encoding="utf-8")
        prefix, marker, _ = source.rpartition('\nmain "$@"')
        self.assertTrue(marker)
        self.library = self.temp / "start-functions.sh"
        self.library.write_text(prefix + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _shell(self, body: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            ["bash", "-c", f'source "$1"\n{body}', "bash", str(self.library)],
            cwd=self.repository_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_healthy_long_recovery_is_not_treated_as_failed_launch(self) -> None:
        run_dir = self.temp / ".run"
        run_dir.mkdir()
        (run_dir / "backend.pid").write_text("101\n", encoding="utf-8")
        (run_dir / "web.pid").write_text("102\n", encoding="utf-8")
        result = self._shell(
            "\n".join(
                (
                    f"RUN_DIR={str(run_dir)!r}",
                    "STARTUP_TIMEOUT_SECONDS=1",
                    "STREAMHOME_BUILD_ID=build123",
                    "WEB_PORT=3000",
                    "service_record_matches() { return 0; }",
                    "endpoint_ready() { [[ \"$2\" == web ]]; }",
                    "endpoint_live_without_error() { return 0; }",
                    "sleep() { :; }",
                    "wait_for_services",
                    'printf "%s\\n" "$STARTUP_RECOVERY_PENDING"',
                )
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true")

    def test_failed_recovery_liveness_does_not_mask_startup_failure(self) -> None:
        run_dir = self.temp / ".run"
        run_dir.mkdir()
        (run_dir / "backend.pid").write_text("101\n", encoding="utf-8")
        (run_dir / "web.pid").write_text("102\n", encoding="utf-8")
        result = self._shell(
            "\n".join(
                (
                    f"RUN_DIR={str(run_dir)!r}",
                    "STARTUP_TIMEOUT_SECONDS=1",
                    "STREAMHOME_BUILD_ID=build123",
                    "WEB_PORT=3000",
                    "service_record_matches() { return 0; }",
                    "endpoint_ready() { [[ \"$2\" == web ]]; }",
                    "endpoint_live_without_error() { return 1; }",
                    "sleep() { :; }",
                    "if wait_for_services; then exit 9; fi",
                    'printf "%s\\n" "$STARTUP_RECOVERY_PENDING"',
                )
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "false")

    def test_recovery_required_never_launches_automatic_restore_controller(self) -> None:
        result = self._shell(
            "\n".join(
                (
                    "restore_recovery_needed() { return 1; }",
                    "launch_restore_recovery() { printf 'unexpected-launch\\n'; return 0; }",
                    "lifecycle_startup_allowed() { return 1; }",
                    "fail() { printf 'blocked\\n'; return 1; }",
                    "if authorize_lifecycle_startup; then exit 9; fi",
                )
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "blocked")


if __name__ == "__main__":
    unittest.main()
