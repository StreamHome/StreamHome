"""Media ownership and bounded, cancellation-aware subprocess work.

These locks coordinate media writers within the single supported server process;
installation-wide authority remains in lifecycle.state.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")
_locks: dict[str, asyncio.Lock] = {}


class MediaRecoveryRequired(RuntimeError):
    """An unresolved publication prevents serving an ambiguous media source."""


def require_resolved_media(media_id: str) -> None:
    """Fail closed on pending or malformed private publication evidence."""
    import json
    from config import settings

    root = Path(settings.TEMP_DIR).resolve() / "media-operations"
    if root.is_symlink():
        raise MediaRecoveryRequired("Unsafe media recovery evidence")
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise MediaRecoveryRequired("Unsafe media recovery evidence")
    for directory in root.iterdir():
        path = directory / "operation.json"
        if directory.is_symlink() or not directory.is_dir() or path.is_symlink() or not path.is_file():
            raise MediaRecoveryRequired("Unrecognized media recovery evidence")
        try:
            from lifecycle.media_publication import validate_operation_manifest
            payload = validate_operation_manifest(path)
        except (OSError, ValueError, UnicodeError) as exc:
            raise MediaRecoveryRequired("Unreadable media recovery evidence") from exc
        if payload["media_id"] == media_id:
            raise MediaRecoveryRequired("This media publication requires recovery")


def media_lock(media_id: str) -> asyncio.Lock:
    return _locks.setdefault(media_id, asyncio.Lock())


@asynccontextmanager
async def media_ownership(*media_ids: str):
    locks = [media_lock(key) for key in sorted(set(media_ids))]
    acquired = []
    try:
        for lock in locks:
            await lock.acquire()
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


async def settle(awaitable: Awaitable[T]) -> tuple[T, bool]:
    """Observe a critical operation's actual outcome despite caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        if task.done():
            return task.result(), cancelled
        # Await a notification rather than the child itself. Cancelling this
        # waiter cannot cancel the critical operation or orphan its exception.
        notification = asyncio.get_running_loop().create_future()

        def completed(_):
            if not notification.done():
                notification.set_result(None)

        task.add_done_callback(completed)
        try:
            await notification
        except asyncio.CancelledError:
            cancelled = True
        finally:
            task.remove_done_callback(completed)


async def accepted_commit(db, record: Callable[[], None] | None = None) -> None:
    _, cancelled = await settle(db.commit())
    if record is not None:
        record()
    if cancelled:
        raise asyncio.CancelledError


async def stop_process(process, grace: float = 2.0) -> None:
    """Reap a child before its registered work lease may be released."""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    else:
        await process.wait()


async def supervised_output(process, *, timeout: float = 60.0,
                            output_path: Path | None = None,
                            maximum_bytes: int | None = None,
                            stdout_limit: int = 16 * 1024 * 1024) -> tuple[bytes, bytes]:
    """Drain pipes with bounded memory and enforce limits while a child runs."""
    async def read_pipe(pipe, limit, tail=False):
        data = bytearray()
        if pipe is None:
            return b""
        while chunk := await pipe.read(65536):
            data.extend(chunk)
            if len(data) > limit:
                if not tail:
                    raise ValueError("Media subprocess output exceeded its limit")
                del data[:-limit]
        return bytes(data)

    async def watch_size():
        while process.returncode is None:
            if output_path is not None and maximum_bytes is not None:
                try:
                    if output_path.stat().st_size > maximum_bytes:
                        raise ValueError("Media output exceeded its byte limit")
                except FileNotFoundError:
                    pass
            await asyncio.sleep(0.1)
        if output_path is not None and maximum_bytes is not None and output_path.exists():
            if output_path.stat().st_size > maximum_bytes:
                raise ValueError("Media output exceeded its byte limit")

    tasks = [asyncio.create_task(read_pipe(process.stdout, stdout_limit)),
             asyncio.create_task(read_pipe(process.stderr, 65536, True)),
             asyncio.create_task(process.wait()), asyncio.create_task(watch_size())]
    try:
        async with asyncio.timeout(timeout):
            results = await asyncio.gather(*tasks)
        return results[0], results[1]
    except BaseException:
        await settle(stop_process(process))
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await settle(asyncio.gather(*tasks, return_exceptions=True))
