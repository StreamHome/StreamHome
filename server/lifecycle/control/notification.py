"""Bounded systemd startup extensions from the supervisor's own process."""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from .rpc import ControlError


STARTUP_RECOVERY_SECONDS = 900
STARTUP_EXTENSION_SECONDS = 30
STARTUP_EXTENSION_INTERVAL = 5


@contextmanager
def extending_startup(notify, launches_open):
    """Keep a healthy slow startup activating, without extending indefinitely.

    Hashing a large immutable runtime can block the asyncio thread. A thread in
    the same supervisor process keeps notifications timely during that bounded
    work. Neither it nor the timer can authorize READY or an application launch.
    """
    deadline = time.monotonic() + STARTUP_RECOVERY_SECONDS
    finished = threading.Event()
    failure = []
    def extend():
        try:
            notify(f"EXTEND_TIMEOUT_USEC={STARTUP_EXTENSION_SECONDS * 1_000_000}")
        except OSError:
            failure.append(True)
    extend()
    def heartbeat():
        while not finished.wait(STARTUP_EXTENSION_INTERVAL):
            if time.monotonic() >= deadline or not launches_open() or failure:
                return
            extend()
    thread = threading.Thread(target=heartbeat, name="streamhome-startup-notify", daemon=True)
    thread.start()
    try:
        yield deadline
        if failure:
            raise ControlError("control_notify_unavailable")
    finally:
        finished.set()
        thread.join(timeout=1)
