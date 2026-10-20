"""Explicit terminal input and concise progress for installer/control commands.

Importing this module never opens a terminal. Downloaded shell input is never
used as an answer stream; interactive entry points open /dev/tty explicitly.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import io
import re
from typing import Callable, Iterator, TextIO, TypeVar


class InputRequiredError(RuntimeError):
    pass


class UserCancelled(RuntimeError):
    pass


T = TypeVar("T")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def display_text(value: str) -> str:
    """Never interpret terminal control sequences from configuration/errors."""
    return _CONTROL.sub("?", str(value))


class Console:
    def __init__(self, input_stream: TextIO, output_stream: TextIO, *, interactive: bool) -> None:
        if not interactive:
            raise InputRequiredError("A terminal is required; provide explicit noninteractive settings instead.")
        self.input = input_stream
        self.output = output_stream

    def write(self, message: str = "") -> None:
        self.output.write(display_text(message) + "\n")
        self.output.flush()

    def _read(self, prompt: str) -> str:
        self.output.write(display_text(prompt))
        self.output.flush()
        try:
            answer = self.input.readline(4098)
        except (KeyboardInterrupt, EOFError) as exc:
            raise UserCancelled("Cancelled; nothing was applied.") from exc
        if not answer or not answer.endswith("\n"):
            # EOF and overlong unterminated input cannot approve anything.
            raise UserCancelled("Input ended; nothing was applied.")
        if len(answer) > 4096 or _CONTROL.search(answer.rstrip("\r\n")):
            raise UserCancelled("Invalid terminal input; nothing was applied.")
        return answer.rstrip("\r\n")

    def confirm(self, question: str) -> bool:
        while True:
            answer = self._read(f"{question} [Y/n]: ").strip().lower()
            if answer in {"", "y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.write("Enter Yes or No; Enter selects Yes.")

    def value(self, label: str, current: str, validate: Callable[[str], T]) -> T:
        while True:
            answer = self._read(f"{label} [{current}]: ")
            try:
                return validate(answer if answer else current)
            except ValueError as exc:
                self.write(str(exc))

    def choice(self, label: str, choices: tuple[str, ...], current: str) -> str:
        if current not in choices:
            raise ValueError("The default choice is unsupported.")

        def validate(value: str) -> str:
            if value not in choices:
                raise ValueError("Choose one of: " + ", ".join(choices) + ".")
            return value

        self.write("Available choices: " + ", ".join(choices))
        return self.value(label, current, validate)


@contextmanager
def open_console() -> Iterator[Console]:
    with ExitStack() as handles:
        try:
            reader = handles.enter_context(open("/dev/tty", "r", encoding="utf-8"))
            writer = handles.enter_context(open("/dev/tty", "w", encoding="utf-8", buffering=1))
        except (OSError, io.UnsupportedOperation) as exc:
            raise InputRequiredError("No controlling terminal; provide explicit noninteractive settings.") from exc
        if not reader.isatty() or not writer.isatty():
            raise InputRequiredError("The controlling terminal is unavailable.")
        yield Console(reader, writer, interactive=True)


class Progress:
    """TTY lines become completed stages; redirected output stays readable."""

    def __init__(self, stream: TextIO, *, no_color: bool = False) -> None:
        self.stream = stream
        self.terminal = bool(stream.isatty())
        self.no_color = no_color
        self.active: str | None = None

    def _line(self, text: str, *, replace: bool = False) -> None:
        prefix = "\r\x1b[2K" if self.terminal and replace else ""
        self.stream.write(prefix + display_text(text) + ("" if self.terminal and self.active else "\n"))
        self.stream.flush()

    @contextmanager
    def stage(self, description: str) -> Iterator[None]:
        if self.active is not None:
            raise RuntimeError("A progress stage is already active.")
        self.active = display_text(description)
        self._line(f"[StreamHome] {self.active} ...")
        try:
            yield
        except BaseException:
            description, self.active = self.active, None
            self._line(f"[StreamHome] {description}: failed", replace=True)
            raise
        else:
            description, self.active = self.active, None
            self._line(f"[StreamHome] {description}: done", replace=True)
