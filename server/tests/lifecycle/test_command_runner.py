from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

from lifecycle.command_runner import CommandUnavailableError, run_bounded


class CommandRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def _run(self, script, **kwargs):
        return run_bounded([sys.executable, "-c", script], cwd=self.directory, environment={"PATH": "/usr/bin:/bin"}, **kwargs)

    def test_command_separates_streams_and_exit_status(self) -> None:
        result = self._run("import sys; print('out'); print('err',file=sys.stderr); sys.exit(3)")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, b"out\n", b"err\n"))

    def test_timeout_and_stdout_or_stderr_overflow_are_bounded(self) -> None:
        for script, kwargs in (
            ("import time; time.sleep(10)", {"timeout": 0.1}),
            ("print('x'*100000)", {"maximum_output": 1024}),
            ("import sys; print('x'*100000,file=sys.stderr)", {"maximum_output": 1024}),
            ("import os,time; pid=os.fork(); time.sleep(10) if pid==0 else None", {"timeout": 0.1}),
        ):
            with self.subTest(script=script), self.assertRaises(CommandUnavailableError):
                self._run(script, **kwargs)

    def test_relative_or_unavailable_executable_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_bounded(["python3"], cwd=self.directory, environment={})
        with self.assertRaises(CommandUnavailableError):
            run_bounded([str(self.directory / "missing")], cwd=self.directory, environment={})


if __name__ == "__main__":
    unittest.main()
