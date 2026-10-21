"""Process-local lifecycle admission and active-work coordination.

This module deliberately has no dependency on application configuration, database
state, logging, routes, models, or the durable lifecycle authority.  Durable
operation ownership remains in :mod:`lifecycle.state`; this module only describes
work that is alive in the current Python process.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token, copy_context
from dataclasses import dataclass, field
from enum import Enum
import os
import re
import threading
import time
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, TypeVar
import uuid


class EffectClass(str, Enum):
    DB_WRITE = "db_write"
    DATABASE_SNAPSHOT = "database_snapshot"
    DURABLE_MEDIA_WRITE = "durable_media_write"
    CLOUD_WRITE = "cloud_write"
    DISPOSABLE_CACHE_WRITE = "disposable_cache_write"
    PLAYBACK_STREAM = "playback_stream"
    CONFIG_WRITE = "config_write"


class CoordinationError(RuntimeError):
    """Base class for deterministic process-local coordination failures."""


class AdmissionClosedError(CoordinationError):
    """Requested effects are closed by a lifecycle fence."""


class LeaseStateError(CoordinationError):
    """A lease is unknown, released, or used by the wrong coordinator."""


class FenceConflictError(CoordinationError):
    """Admission is already closed by a different lifecycle fence."""


_OWNER_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")
_START_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _effect_set(effects: Iterable[EffectClass | str]) -> frozenset[EffectClass]:
    result: set[EffectClass] = set()
    for value in effects:
        try:
            result.add(value if isinstance(value, EffectClass) else EffectClass(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported runtime effect class") from exc
    if not result:
        raise ValueError("at least one runtime effect class is required")
    return frozenset(result)


def _owner_label(value: str) -> str:
    if not isinstance(value, str) or not _OWNER_RE.fullmatch(value):
        raise ValueError("owner label must be a stable, non-sensitive identifier")
    return value


def _operation_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("operation_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("operation_id must be a canonical UUID") from exc
    canonical = str(parsed)
    if canonical != value:
        raise ValueError("operation_id must be a canonical UUID")
    return canonical


def _generation(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("generation must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class FenceIdentity:
    operation_id: str
    generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _operation_id(self.operation_id))
        object.__setattr__(self, "generation", _generation(self.generation))


@dataclass(frozen=True, slots=True)
class ChildProcessIdentity:
    pid: int
    process_start_identity: str | None

    def __post_init__(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("child pid must be a positive integer")
        if self.process_start_identity is not None and not _START_IDENTITY_RE.fullmatch(
            self.process_start_identity
        ):
            raise ValueError("child process start identity has an invalid format")


@dataclass(frozen=True, slots=True)
class DrainSnapshot:
    snapshot_id: str
    fence: FenceIdentity
    effects: frozenset[EffectClass]
    lease_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class DrainResult:
    complete: bool
    timed_out: bool
    remaining: tuple[Mapping[str, object], ...]


@dataclass(slots=True)
class _LeaseRecord:
    lease_id: str
    owner: str
    effects: set[EffectClass]
    admitted_monotonic: float
    active_scopes: int = 0
    release_requested: bool = False
    cancellation_requested: threading.Event = field(default_factory=threading.Event)
    children: set[ChildProcessIdentity] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    event: asyncio.Event


class WorkLease:
    """Narrow handle for one exact registered root work item."""

    __slots__ = ("_coordinator", "lease_id")

    def __init__(self, coordinator: "RuntimeCoordinator", lease_id: str):
        self._coordinator = coordinator
        self.lease_id = lease_id

    @property
    def cancellation_requested(self) -> bool:
        return self._coordinator._cancellation_requested(self.lease_id)

    @property
    def effects(self) -> frozenset[EffectClass]:
        return self._coordinator._lease_effects(self.lease_id)

    def attach_child(
        self,
        pid: int,
        process_start_identity: str | None = None,
    ) -> ChildProcessIdentity:
        identity = ChildProcessIdentity(
            pid=pid,
            process_start_identity=(
                process_start_identity
                if process_start_identity is not None
                else read_process_start_identity(pid)
            ),
        )
        self._coordinator._attach_child(self.lease_id, identity)
        return identity

    def detach_child(self, identity: ChildProcessIdentity) -> None:
        self._coordinator._detach_child(self.lease_id, identity)

    def release(self) -> None:
        """Request release of this exact lease; a second request is an error."""

        self._coordinator._request_release(self.lease_id)


class RuntimeCoordinator:
    """Race-free process-local admission, registration, drain, and cancellation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._leases: dict[str, _LeaseRecord] = {}
        self._closed_effects: dict[EffectClass, FenceIdentity] = {}
        self._snapshots: dict[str, DrainSnapshot] = {}
        self._waiters: set[_Waiter] = set()
        self._current_lease: ContextVar[WorkLease | None] = ContextVar(
            f"streamhome_runtime_lease_{id(self)}", default=None
        )

    @asynccontextmanager
    async def work(
        self,
        owner: str,
        effects: Iterable[EffectClass | str],
    ) -> AsyncIterator[WorkLease]:
        """Admit and register work, reusing/widening a propagated root lease."""

        normalized_owner = _owner_label(owner)
        requested = _effect_set(effects)
        lease, token, root_scope = self._enter(normalized_owner, requested)
        try:
            yield lease
        finally:
            try:
                self._exit_scope(lease.lease_id, root_scope=root_scope)
            finally:
                if root_scope:
                    assert token is not None
                    # A pending release remains visible for nested scopes or child
                    # processes, but must not leak into later work in this task.
                    self._current_lease.reset(token)

    def current_lease(self) -> WorkLease | None:
        lease = self._current_lease.get()
        if lease is None:
            return None
        with self._lock:
            return lease if lease.lease_id in self._leases else None

    def admit(self, owner: str, effects: Iterable[EffectClass | str]) -> WorkLease:
        """Register an explicit root lease for ownership spanning task boundaries."""

        normalized_owner = _owner_label(owner)
        requested = _effect_set(effects)
        with self._lock:
            return self._admit_locked(normalized_owner, requested, active_scopes=0)

    @contextmanager
    def bind(self, lease: WorkLease):
        """Propagate an already admitted lease within the current execution context."""

        if not isinstance(lease, WorkLease) or lease._coordinator is not self:
            raise LeaseStateError("runtime lease belongs to another coordinator")
        with self._lock:
            if lease.lease_id not in self._leases:
                raise LeaseStateError("runtime lease is no longer active")
        token = self._current_lease.set(lease)
        try:
            yield lease
        finally:
            self._current_lease.reset(token)

    def close_admission(
        self,
        effects: Iterable[EffectClass | str],
        *,
        operation_id: str,
        generation: int,
    ) -> DrainSnapshot:
        """Atomically close effects and snapshot work admitted before the fence."""

        selected = _effect_set(effects)
        fence = FenceIdentity(operation_id, generation)
        with self._lock:
            conflicts = {
                existing
                for effect in selected
                if (existing := self._closed_effects.get(effect)) is not None
                and existing != fence
            }
            if conflicts:
                raise FenceConflictError("runtime admission is owned by another lifecycle fence")
            for effect in selected:
                self._closed_effects[effect] = fence
            lease_ids = frozenset(
                lease_id
                for lease_id, record in self._leases.items()
                if record.effects.intersection(selected)
            )
            snapshot = DrainSnapshot(
                snapshot_id=str(uuid.uuid4()),
                fence=fence,
                effects=selected,
                lease_ids=lease_ids,
            )
            self._snapshots[snapshot.snapshot_id] = snapshot
            return snapshot

    def release_fence(self, snapshot: DrainSnapshot) -> None:
        """Release only the process-local admission fence owning ``snapshot``.

        One lifecycle operation may close additional effects in more than one
        call, so its exact operation/generation pair owns the complete fence.
        Releasing any authoritative snapshot for that fence reopens all and only
        effects still owned by the same pair. All snapshots for the released
        fence become stale immediately.
        """

        if not isinstance(snapshot, DrainSnapshot):
            raise LeaseStateError("admission fence snapshot is invalid")
        with self._lock:
            authoritative = self._snapshots.get(snapshot.snapshot_id)
            if authoritative != snapshot:
                raise LeaseStateError("admission fence snapshot is unknown or does not match")
            self._assert_fence_closed(snapshot)
            owned_effects = {
                effect
                for effect, fence in self._closed_effects.items()
                if fence == snapshot.fence
            }
            if not owned_effects:
                raise FenceConflictError("runtime admission fence is no longer active")
            for effect in owned_effects:
                del self._closed_effects[effect]
            stale_snapshot_ids = [
                snapshot_id
                for snapshot_id, candidate in self._snapshots.items()
                if candidate.fence == snapshot.fence
            ]
            for snapshot_id in stale_snapshot_ids:
                del self._snapshots[snapshot_id]
        self._notify_waiters()

    async def drain(self, snapshot: DrainSnapshot, timeout: float) -> DrainResult:
        """Wait only for the exact pre-fence leases captured by ``snapshot``."""

        if timeout < 0:
            raise ValueError("drain timeout cannot be negative")
        deadline = time.monotonic() + timeout
        loop = asyncio.get_running_loop()
        while True:
            event = asyncio.Event()
            waiter = _Waiter(loop, event)
            with self._lock:
                authoritative = self._snapshots.get(snapshot.snapshot_id)
                if authoritative != snapshot:
                    raise LeaseStateError("drain snapshot is unknown or does not match")
                self._assert_fence_closed(snapshot)
                remaining = snapshot.lease_ids.intersection(self._leases)
                if not remaining:
                    return DrainResult(complete=True, timed_out=False, remaining=())
                self._waiters.add(waiter)
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                with self._lock:
                    self._waiters.discard(waiter)
                    remaining = snapshot.lease_ids.intersection(self._leases)
                    diagnostics = self._diagnostics_for(remaining)
                if remaining:
                    return DrainResult(
                        complete=False,
                        timed_out=True,
                        remaining=diagnostics,
                    )
                return DrainResult(complete=True, timed_out=False, remaining=())
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining_time)
            except asyncio.TimeoutError:
                with self._lock:
                    remaining = snapshot.lease_ids.intersection(self._leases)
                    diagnostics = self._diagnostics_for(remaining)
                if remaining:
                    return DrainResult(
                        complete=False,
                        timed_out=True,
                        remaining=diagnostics,
                    )
                return DrainResult(complete=True, timed_out=False, remaining=())
            finally:
                with self._lock:
                    self._waiters.discard(waiter)

    def request_cancellation(self, snapshot: DrainSnapshot) -> int:
        """Signal service-owned cancellation for remaining pre-fence work."""

        with self._lock:
            authoritative = self._snapshots.get(snapshot.snapshot_id)
            if authoritative != snapshot:
                raise LeaseStateError("drain snapshot is unknown or does not match")
            self._assert_fence_closed(snapshot)
            records = [
                self._leases[lease_id]
                for lease_id in snapshot.lease_ids
                if lease_id in self._leases
            ]
            for record in records:
                record.cancellation_requested.set()
            return len(records)

    def active_work(self) -> tuple[Mapping[str, object], ...]:
        """Return a sanitized diagnostic projection with no resource strings."""

        with self._lock:
            return self._diagnostics_for(self._leases)

    def closed_effects(self) -> Mapping[str, Mapping[str, object]]:
        with self._lock:
            return {
                effect.value: {
                    "operation_id": fence.operation_id,
                    "generation": fence.generation,
                }
                for effect, fence in sorted(
                    self._closed_effects.items(), key=lambda item: item[0].value
                )
            }

    def _enter(
        self,
        owner: str,
        requested: frozenset[EffectClass],
    ) -> tuple[WorkLease, Token[WorkLease | None] | None, bool]:
        current = self._current_lease.get()
        with self._lock:
            if current is not None:
                record = self._leases.get(current.lease_id)
                if record is not None:
                    additional = requested.difference(record.effects)
                    blocked = additional.intersection(self._closed_effects)
                    if blocked:
                        raise AdmissionClosedError("requested runtime effects are closed")
                    record.effects.update(additional)
                    record.active_scopes += 1
                    return current, None, False
                # A task may intentionally outlive the request that scheduled it.
                # Its copied context is stale, so it must pass normal admission as
                # a new root rather than silently reuse an already released lease.
                current = None

            if requested.intersection(self._closed_effects):
                raise AdmissionClosedError("requested runtime effects are closed")
            lease = self._admit_locked(owner, requested, active_scopes=1)
            token = self._current_lease.set(lease)
            return lease, token, True

    def _admit_locked(
        self,
        owner: str,
        requested: frozenset[EffectClass],
        *,
        active_scopes: int,
    ) -> WorkLease:
        if requested.intersection(self._closed_effects):
            raise AdmissionClosedError("requested runtime effects are closed")
        from .runtime_admission import registered_runtime_allows_work
        if not registered_runtime_allows_work():
            raise AdmissionClosedError("the managed runtime has no current settled admission")
        lease_id = str(uuid.uuid4())
        self._leases[lease_id] = _LeaseRecord(
            lease_id=lease_id,
            owner=owner,
            effects=set(requested),
            admitted_monotonic=time.monotonic(),
            active_scopes=active_scopes,
        )
        return WorkLease(self, lease_id)

    def _request_release(self, lease_id: str) -> None:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None:
                raise LeaseStateError("runtime lease is unknown or already released")
            if record.release_requested:
                raise LeaseStateError("runtime lease release was already requested")
            record.release_requested = True
            finalized = self._finalize_if_ready_locked(record)
        if finalized:
            self._notify_waiters()

    def _exit_scope(self, lease_id: str, *, root_scope: bool) -> None:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None:
                raise LeaseStateError("runtime lease is no longer active")
            if record.active_scopes <= 0:
                raise LeaseStateError("runtime lease scope accounting is invalid")
            record.active_scopes -= 1
            if root_scope:
                record.release_requested = True
            finalized = self._finalize_if_ready_locked(record)
        if finalized:
            self._notify_waiters()

    def _finalize_if_ready_locked(self, record: _LeaseRecord) -> bool:
        if (
            not record.release_requested
            or record.active_scopes
            or record.children
        ):
            return False
        if self._leases.get(record.lease_id) is not record:
            return False
        del self._leases[record.lease_id]
        return True

    def _notify_waiters(self) -> None:
        with self._lock:
            waiters = tuple(self._waiters)
        for waiter in waiters:
            waiter.loop.call_soon_threadsafe(waiter.event.set)

    def _lease_effects(self, lease_id: str) -> frozenset[EffectClass]:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None:
                raise LeaseStateError("runtime lease is no longer active")
            return frozenset(record.effects)

    def _cancellation_requested(self, lease_id: str) -> bool:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None:
                raise LeaseStateError("runtime lease is no longer active")
            return record.cancellation_requested.is_set()

    def _attach_child(self, lease_id: str, identity: ChildProcessIdentity) -> None:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None:
                raise LeaseStateError("runtime lease is no longer active")
            record.children.add(identity)

    def _detach_child(self, lease_id: str, identity: ChildProcessIdentity) -> None:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None:
                raise LeaseStateError("runtime lease is no longer active")
            if identity not in record.children:
                raise LeaseStateError("child process is not attached to this lease")
            record.children.remove(identity)
            finalized = self._finalize_if_ready_locked(record)
        if finalized:
            self._notify_waiters()

    def _assert_fence_closed(self, snapshot: DrainSnapshot) -> None:
        if any(self._closed_effects.get(effect) != snapshot.fence for effect in snapshot.effects):
            raise FenceConflictError("runtime admission fence no longer matches snapshot")

    def _diagnostics_for(self, lease_ids: Iterable[str]) -> tuple[Mapping[str, object], ...]:
        now = time.monotonic()
        return tuple(
            {
                "lease_id": record.lease_id,
                "owner": record.owner,
                "effects": sorted(effect.value for effect in record.effects),
                "age_seconds": max(0.0, now - record.admitted_monotonic),
                "cancellation_requested": record.cancellation_requested.is_set(),
                "child_processes": [
                    {
                        "pid": child.pid,
                        "process_start_identity": child.process_start_identity,
                    }
                    for child in sorted(
                        record.children,
                        key=lambda item: (item.pid, item.process_start_identity or ""),
                    )
                ],
            }
            for record in sorted(
                (self._leases[lease_id] for lease_id in lease_ids if lease_id in self._leases),
                key=lambda item: item.lease_id,
            )
        )


def read_process_start_identity(pid: int) -> str | None:
    """Read Linux /proc start ticks without invoking an external process."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if os.name != "posix":
        return None
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="ascii") as handle:
            raw = handle.read()
        # The parenthesized comm field may itself contain spaces or parentheses.
        fields_after_comm = raw[raw.rfind(")") + 2 :].split()
        value = fields_after_comm[19]  # starttime is Linux proc stat field 22.
    except (OSError, IndexError, UnicodeError):
        return None
    return value if _START_IDENTITY_RE.fullmatch(value) else None


_ThreadResult = TypeVar("_ThreadResult")


async def run_cancellation_safe_thread(
    function: Callable[..., _ThreadResult],
    /,
    *args: Any,
    cancellation_callback: Callable[[], None] | None = None,
    **kwargs: Any,
) -> _ThreadResult:
    """Run thread work to its real completion before propagating cancellation.

    ``asyncio`` cannot stop a function that is already executing in a worker
    thread.  Polling and joining it here keeps the surrounding work lease and any
    service-owned domain lock alive until the unsafe-to-abandon operation reaches
    its actual completion boundary.  The copied context preserves the root lease
    for process association performed inside the thread.
    """

    context = copy_context()
    result: list[_ThreadResult] = []
    failure: list[BaseException] = []

    def invoke() -> None:
        try:
            result.append(context.run(function, *args, **kwargs))
        except BaseException as exc:
            failure.append(exc)

    worker = threading.Thread(
        target=invoke,
        name="streamhome-lifecycle-work",
    )
    worker.start()
    cancellation_received = False
    cancellation_callback_failure: Exception | None = None
    while worker.is_alive():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancellation_received = True
        if cancellation_received and cancellation_callback is not None:
            try:
                cancellation_callback()
            except Exception as exc:
                cancellation_callback_failure = exc
    worker.join()
    if cancellation_received:
        if failure:
            raise asyncio.CancelledError() from failure[0]
        if cancellation_callback_failure is not None:
            raise asyncio.CancelledError() from cancellation_callback_failure
        raise asyncio.CancelledError()
    if failure:
        raise failure[0]
    return result[0]


_runtime_coordinator: RuntimeCoordinator | None = None
_singleton_lock = threading.Lock()


def get_runtime_coordinator() -> RuntimeCoordinator:
    global _runtime_coordinator
    if _runtime_coordinator is None:
        with _singleton_lock:
            if _runtime_coordinator is None:
                _runtime_coordinator = RuntimeCoordinator()
    return _runtime_coordinator


@contextmanager
def use_runtime_coordinator_for_testing(coordinator: RuntimeCoordinator):
    """Temporarily replace the process singleton in isolated tests only."""

    if not isinstance(coordinator, RuntimeCoordinator):
        raise TypeError("test coordinator must be a RuntimeCoordinator")
    global _runtime_coordinator
    with _singleton_lock:
        previous = _runtime_coordinator
        _runtime_coordinator = coordinator
    try:
        yield coordinator
    finally:
        with _singleton_lock:
            if _runtime_coordinator is not coordinator:
                raise LeaseStateError("runtime coordinator changed during isolated test")
            _runtime_coordinator = previous


@asynccontextmanager
async def runtime_work(
    owner: str,
    effects: Iterable[EffectClass | str],
) -> AsyncIterator[WorkLease]:
    async with get_runtime_coordinator().work(owner, effects) as lease:
        yield lease
