from __future__ import annotations

import io
import os
import pty
import unittest
from unittest.mock import patch

from lifecycle.terminal import Console, InputRequiredError, Progress, UserCancelled, open_console


class TerminalTests(unittest.TestCase):
    def test_yes_is_default_and_invalid_input_reprompts(self) -> None:
        for answer in ("\n", "Y\n", "yEs\n", " NO \n", "invalid\ny\n"):
            output = io.StringIO()
            result = Console(io.StringIO(answer), output, interactive=True).confirm("Continue?")
            self.assertEqual(result, "no" not in answer.lower())
            self.assertIn("[Y/n]", output.getvalue())
            if "invalid" in answer:
                self.assertIn("Enter Yes or No", output.getvalue())

    def test_eof_interruption_and_nonterminal_never_become_consent(self) -> None:
        for raw in ("", "y", "\x1b[2J\n", "x" * 5000 + "\n"):
            with self.subTest(raw=raw[:20]), self.assertRaises(UserCancelled):
                Console(io.StringIO(raw), io.StringIO(), interactive=True).confirm("Continue?")

    def test_interrupt_cancels(self) -> None:
        class Interrupted(io.StringIO):
            def readline(self, size: int = -1) -> str:
                raise KeyboardInterrupt()
        with self.assertRaises(UserCancelled):
            Console(Interrupted(), io.StringIO(), interactive=True).confirm("Continue?")
        with self.assertRaises(InputRequiredError):
            Console(io.StringIO("\n"), io.StringIO(), interactive=False)

    def test_ordinary_empty_field_keeps_current_value(self) -> None:
        console = Console(io.StringIO("\n"), io.StringIO(), interactive=True)
        self.assertEqual(console.value("Port", "3000", int), 3000)

    def test_controlling_terminal_uses_separate_nonseekable_streams(self) -> None:
        master, slave = pty.openpty()
        path = os.ttyname(slave)
        real_open = open
        opened = []

        def open_tty(name: str, mode: str, **kwargs):
            self.assertEqual(name, "/dev/tty")
            handle = real_open(path, mode, **kwargs)
            opened.append(handle)
            return handle

        try:
            os.write(master, b"\n")
            with patch("builtins.open", side_effect=open_tty), open_console() as console:
                self.assertTrue(console.confirm("Continue?"))
            self.assertTrue(all(handle.closed for handle in opened))
        finally:
            os.close(slave)
            os.close(master)

    def test_no_tty_is_explicit_error(self) -> None:
        with patch("builtins.open", side_effect=OSError("no terminal")), self.assertRaises(InputRequiredError):
            with open_console():
                self.fail("No console is available")

    def test_nonterminal_progress_is_plain_and_preserves_failure(self) -> None:
        output = io.StringIO()
        progress = Progress(output)
        with progress.stage("Installing Python libraries"):
            pass
        with self.assertRaises(ValueError), progress.stage("Building web assets"):
            raise ValueError("failure")
        self.assertEqual(output.getvalue().splitlines(), [
            "[StreamHome] Installing Python libraries ...", "[StreamHome] Installing Python libraries: done",
            "[StreamHome] Building web assets ...", "[StreamHome] Building web assets: failed",
        ])
        self.assertIsNone(progress.active)

    def test_configured_labels_never_inject_terminal_control(self) -> None:
        output = io.StringIO()
        console = Console(io.StringIO("\n"), output, interactive=True)
        console.value("Port\x1b[2J", "3000", int)
        self.assertNotIn("\x1b", output.getvalue())


if __name__ == "__main__":
    unittest.main()
