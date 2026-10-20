"""Bounded subprocesses for stdlib-only control/discovery helpers.

Commands are argument arrays, never shell input. Output is returned as private
data and must be interpreted/redacted before display. Only the child process
group created by this runner is terminated on timeout or output overflow.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Mapping, Sequence


class CommandUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def run_bounded(
    arguments: Sequence[str], *, cwd: Path, environment: Mapping[str, str],
    timeout: float = 15.0, maximum_output: int = 256 * 1024,
) -> CommandResult:
    if (
        not arguments or not Path(arguments[0]).is_absolute()
        or type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60
        or type(maximum_output) is not int or not 0 < maximum_output <= 16 * 1024 * 1024
    ):
        raise ValueError("A fixed absolute executable and positive command bounds are required.")
    process = None
    finished = False
    try:
        process = subprocess.Popen(
            list(arguments), cwd=cwd, env=dict(environment), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        output = {"stdout": bytearray(), "stderr": bytearray()}
        with selectors.DefaultSelector() as selector:
            for name in output:
                selector.register(getattr(process, name), selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandUnavailableError("The command exceeded its time bound.")
                for key, _ in selector.select(min(remaining, 0.25)):
                    block = os.read(key.fileobj.fileno(), 16384)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(block)
                    if sum(len(value) for value in output.values()) > maximum_output:
                        raise CommandUnavailableError("The command exceeded its output bound.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandUnavailableError("The command exceeded its time bound.")
            code = process.wait(timeout=remaining)
            finished = True
        return CommandResult(code, bytes(output["stdout"]), bytes(output["stderr"]))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandUnavailableError("The command could not be completed.") from exc
    finally:
        if process is not None:
            if not finished:
                # The leader has not been reaped, so this group ID cannot have
                # been recycled. Include descendants retaining output pipes.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            for handle in (process.stdout, process.stderr):
                if handle is not None:
                    handle.close()
