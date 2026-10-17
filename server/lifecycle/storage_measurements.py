"""Independent, bounded storage observations owned by the normal runtime.

Only successful samples are durable. Refresh progress is process-local and never
creates Activity entries. Reading this state cannot start work.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import random
import sys
import time

from lifecycle.coordination import AdmissionClosedError, EffectClass, runtime_work
from services.storage_common import StorageError, utc


def cloud_read_enabled():
    from services.storage_inventory import cloud_read_enabled as configured
    return configured()


def target_generation():
    from services.storage_inventory import target_generation as generation
    return generation()


@dataclass
class RefreshState:
    state: str = 'idle'
    phase: str | None = None
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    next_attempt_at: float | None = None
    error_code: str | None = None
    generation: str | None = None
    failures: int = 0
    completed: float = 0.0
    due: float = 0.0
    dirty: bool = False
    dirty_since: float = 0.0


class StorageMeasurements:
    LOCAL_INTERVAL = 60.0
    CLOUD_INTERVAL = 300.0
    MAX_BACKOFF = 900.0

    def __init__(self, worker):
        self.worker = worker
        self.states = {name: RefreshState() for name in ('local', 'cloud')}
        self.locks = {name: asyncio.Lock() for name in self.states}
        self.events = {name: asyncio.Event() for name in self.states}
        self.tasks: list[asyncio.Task] = []
        self.loop = None

    def snapshot(self, name: str, *, configured: bool = True, last_success: float | None = None) -> dict:
        state = self.states[name]
        current = name != 'cloud' or state.generation == target_generation()
        return {'state': 'unconfigured' if not configured else (state.state if current else 'idle'),
                'phase': state.phase if configured and current else None,
                'last_attempt_at': utc(state.last_attempt_at) if current else None,
                'last_success_at': utc(last_success if last_success is not None else (state.last_success_at if current else None)),
                'next_attempt_at': utc(state.next_attempt_at) if configured and current else None,
                'error_code': state.error_code if configured and current else None}

    def invalidate(self, *, local: bool = True, cloud: bool = False):
        """Coalesce owned changes; never launch a scan from the writer's scope."""
        if self.loop is None or self.loop.is_closed():
            return
        def wake():
            for name, changed in (('local', local), ('cloud', cloud)):
                if changed:
                    if not self.states[name].dirty:
                        self.states[name].dirty_since = time.monotonic()
                    self.states[name].dirty = True
                    self.events[name].set()
        self.loop.call_soon_threadsafe(wake)

    async def start(self):
        if self.tasks:
            return
        self.loop = asyncio.get_running_loop()
        self.states = {name: RefreshState() for name in self.states}
        self.events = {name: asyncio.Event() for name in self.states}
        self.tasks = [asyncio.create_task(self._run(name), name='storage-measure-' + name) for name in self.states]

    async def stop(self):
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.loop = None

    def phase(self, name: str, phase: str):
        self.states[name].phase = phase

    def _schedule(self, name: str, *, failed: bool):
        state = self.states[name]
        interval = self.LOCAL_INTERVAL if name == 'local' else self.CLOUD_INTERVAL
        if failed:
            interval = min(self.MAX_BACKOFF, interval * 2 ** min(max(0, state.failures - 1), 4))
        jitter = random.uniform(0, min(30, interval * .1)) if name == 'cloud' else 0
        state.completed = time.monotonic()
        state.due = state.completed + interval + jitter
        state.next_attempt_at = time.time() + interval + jitter

    async def measure(self, name: str, *, fresh: bool = False):
        """Single-flight observations. Admission callers require a post-request sample.

        A transfer completing during a background sample cannot use that earlier
        observation to release its conservative reservation.
        """
        requested = time.monotonic()
        async with self.locks[name]:
            state = self.states[name]
            if not fresh and state.completed >= requested:
                if state.state == 'error':
                    raise StorageError(state.error_code or 'storage_measurement_failed')
                return
            if name == 'cloud' and not cloud_read_enabled():
                state.state, state.phase = 'unconfigured', None
                state.next_attempt_at = None
                if fresh:
                    raise StorageError('drive_not_connected')
                return
            state.state, state.phase, state.error_code = 'running', None, None
            state.last_attempt_at = time.time()
            state.generation = target_generation() if name == 'cloud' else None
            state.dirty = False
            try:
                async with runtime_work('storage.measure.' + name, [EffectClass.DB_WRITE]):
                    if name == 'local':
                        self.phase(name, 'scanning_local')
                        await self.worker._measure_local()
                    else:
                        self.phase(name, 'waiting_capacity')
                        await self.worker._measure_cloud()
                state.last_success_at = time.time()
                state.state, state.phase, state.failures = 'idle', None, 0
                self._schedule(name, failed=False)
            except asyncio.CancelledError:
                state.state, state.phase = 'idle', None
                self._schedule(name, failed=True)
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, StorageError) else 'storage_' + name + '_measurement_failed'
                state.state, state.phase, state.error_code = 'error', None, code
                state.failures += 1
                self._schedule(name, failed=True)
                raise
            finally:
                self.events[name].set()

    async def _run(self, name: str):
        event = self.events[name]
        while True:
            event.clear()
            state = self.states[name]
            configured = name == 'local' or cloud_read_enabled()
            changed = name == 'cloud' and (state.generation != target_generation() or state.state == 'unconfigured')
            if not configured:
                state.state, state.phase, state.next_attempt_at = 'unconfigured', None, None
            else:
                # Local writes debounce for two seconds with a ten-second
                # completion floor; cloud writes keep a one-minute floor.
                # Neither may bypass failure backoff.
                if state.dirty and not state.failures and state.completed:
                    event_due = max(state.completed + 10, state.dirty_since + 2) if name == 'local' else state.completed + 60
                    state.due = min(state.due, event_due)
                    state.next_attempt_at = time.time() + max(0, state.due - time.monotonic())
                if changed or time.monotonic() >= state.due:
                    try:
                        await self.measure(name)
                    except asyncio.CancelledError:
                        raise
                    except (AdmissionClosedError, StorageError):
                        pass
                    except Exception:
                        # Provider details and paths never enter public progress.
                        pass
            timeout = min(15.0, max(.05, state.due - time.monotonic())) if configured else 15.0
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except TimeoutError:
                pass


def invalidate_storage_measurements(*, local: bool = True, cloud: bool = False):
    """A hook for already-loaded owned writers; safe before runtime startup."""
    module = sys.modules.get('lifecycle.storage_worker')
    worker = getattr(module, 'storage_worker', None)
    if worker is not None:
        worker.measurements.invalidate(local=local, cloud=cloud)
