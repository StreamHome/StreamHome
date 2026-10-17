"""Shared durable admission for every application-owned storage writer.

Reservations represent *additional peak bytes*. Written bytes are measured by the
inventory and removed from the reservation remainder; they are never added twice.
Unknown/orphaned owners remain reserved until an explicit observed resolution.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import time
import uuid
import weakref

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
import db as database
from lifecycle.coordination import run_cancellation_safe_thread
from models import StorageMeasurement, StorageReservation
from services.storage_common import StorageError, policy_value


class StorageCapacityError(StorageError):
    pass


def filesystem_for(path: Path | str) -> tuple[str, Path]:
    candidate = Path(path).absolute()
    while not candidate.exists():
        if candidate.parent == candidate:
            raise StorageCapacityError('storage_filesystem_unavailable')
        candidate = candidate.parent
    if candidate.is_symlink():
        raise StorageCapacityError('storage_unsafe_path')
    return str(candidate.stat().st_dev), candidate


def _owned_entries(path: str, measured: dict, *, sqlite_backup: bool = False) -> dict[tuple[int, int], int]:
    root = Path(os.path.abspath(path))
    companions = {Path(str(root) + suffix) for suffix in ('-journal', '-wal', '-shm', '.manifest.json')} if sqlite_backup else set()
    result = {}
    for name, device, inode, size in measured.get('_entries', []):
        candidate = Path(name)
        if candidate == root or root in candidate.parents or candidate in companions:
            result[(device, inode)] = size
    return result


def _remaining_from_sample(rows, measured: dict, *, scope: str = 'local', filesystem: str | None = None) -> int:
    """Usage and materialization must come from one deduplicated inventory.

    Progress is supervision, not authority to spend reusable workspace twice.
    Shrinking files automatically restores their remaining peak. Overlapping
    claims retain full peaks rather than crediting one physical byte twice.
    """
    selected = [row for row in rows if row.scope == scope and (filesystem is None or row.filesystem == filesystem)]
    if scope != 'local':
        return sum(max(0, row.peak_bytes - row.written_bytes) for row in selected)
    tracked = {row.id: _owned_entries(row.path, measured, sqlite_backup=row.owner == 'backup.snapshot') for row in rows
               if row.scope == 'local' and row.track_materialized and row.peak_bytes > 0}
    conflicts = set()
    for index, row in enumerate(rows):
        if row.id not in tracked:
            continue
        path = Path(os.path.abspath(row.path))
        for other in rows[index + 1:]:
            if other.id not in tracked:
                continue
            other_path = Path(os.path.abspath(other.path))
            if (path == other_path or path in other_path.parents or other_path in path.parents
                    or tracked[row.id].keys() & tracked[other.id].keys()):
                conflicts.update((row.id, other.id))
    return sum(max(0, row.peak_bytes - min(row.peak_bytes,
        max(0, sum(tracked[row.id].values()) - row.baseline_bytes)
        if row.id in tracked and row.id not in conflicts else 0)) for row in selected)


class StorageCapacity:
    def __init__(self):
        self._locks = weakref.WeakKeyDictionary()

    @property
    def lock(self):
        loop = asyncio.get_running_loop()
        return self._locks.setdefault(loop, asyncio.Lock())

    async def _pressure(self, db, policy, owner, additional, filesystem, code, path):
        if not policy['offload_enabled']:
            return
        previous = await db.get(StorageMeasurement, 'capacity_pressure')
        payload = json.loads(previous.payload) if previous else {}
        additional = max(additional, payload.get('additional_bytes', 0)) if payload.get('revision') == policy['revision'] else additional
        await db.merge(StorageMeasurement(id='capacity_pressure', measured_at=time.time(), payload=json.dumps({
            'additional_bytes': additional, 'filesystem': filesystem, 'owner': owner,
            'revision': policy['revision'], 'error_code': code, 'active': True, 'path': str(path)})))
        await db.commit()
        from lifecycle.storage_worker import storage_worker
        storage_worker.wake()

    async def reserve(self, owner: str, additional_bytes: int, path: Path | str, *,
                      scope: str = 'local', operation_id: str | None = None,
                      track_materialized: bool = True) -> str:
        # Admission can commit before its asynchronous session close returns.
        # Observe the accepted ID even if the caller is cancelled in that gap;
        # no physical writer has started, so only that exact claim is releasable.
        from lifecycle.media_work import settle
        identifier, cancelled = await settle(self._reserve(
            owner, additional_bytes, path, scope=scope, operation_id=operation_id,
            track_materialized=track_materialized))
        if cancelled:
            await settle(self.release(identifier))
            raise asyncio.CancelledError()
        return identifier

    async def _reserve(self, owner: str, additional_bytes: int, path: Path | str, *,
                       scope: str = 'local', operation_id: str | None = None,
                       track_materialized: bool = True) -> str:
        if type(additional_bytes) is not int or additional_bytes < 0 or additional_bytes > 2 ** 63 - 1:
            raise StorageCapacityError('storage_invalid_reservation', status_code=422)
        if scope not in {'local', 'cloud'} or not owner or len(owner) > 128:
            raise StorageCapacityError('storage_invalid_reservation', status_code=422)
        async with self.lock:
            policy = policy_value(settings)
            if scope == 'local':
                from services.storage_inventory import storage_inventory
                measured = await storage_inventory.local_measurement(include_entries=True)
                if measured['written_bytes'] is None:
                    raise StorageCapacityError('storage_measurement_unknown')
                filesystem, existing = filesystem_for(path)
                free = (await run_cancellation_safe_thread(shutil.disk_usage, existing)).free
            else:
                from services.storage_inventory import target_generation
                filesystem = target_generation()
                async with AsyncSession(database.engine, expire_on_commit=False) as db:
                    row = await db.get(StorageMeasurement, 'cloud')
                    measured = json.loads(row.payload) if row else {}
                if not row or time.time() - row.measured_at > 60 or measured.get('used_bytes') is None:
                    raise StorageCapacityError('storage_cloud_measurement_unknown')
                if measured.get('target_generation') != filesystem:
                    raise StorageCapacityError('storage_target_changed')
                free = measured.get('account_free_bytes')
                if free is None:
                    raise StorageCapacityError('storage_account_capacity_unknown')
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                # BEGIN IMMEDIATE prevents a second process from admitting the
                # same free bytes. No external I/O occurs in this transaction.
                await db.connection()
                await db.exec(__import__('sqlalchemy').text('BEGIN IMMEDIATE'))
                rows = (await db.exec(select(StorageReservation).where(StorageReservation.state == 'active'))).all()
                scope_remaining = _remaining_from_sample(rows, measured, scope=scope)
                # Physical free-space may be sampled after a native writer has
                # shrunk its files. Retain full peaks for the device guard; its
                # safety reserve is independent of coherent managed-budget math.
                device_remaining = sum(r.peak_bytes for r in rows if r.scope == scope and r.filesystem == filesystem) if scope == 'local' else _remaining_from_sample(rows, measured, scope=scope, filesystem=filesystem)
                budget = policy['local_budget_bytes' if scope == 'local' else 'cloud_budget_bytes']
                written = measured['written_bytes' if scope == 'local' else 'used_bytes']
                if budget is not None and written + scope_remaining + additional_bytes > budget:
                    if scope == 'local':
                        await self._pressure(db, policy, owner, additional_bytes, filesystem, 'storage_local_budget_exceeded', existing)
                    raise StorageCapacityError('storage_local_budget_exceeded' if scope == 'local' else 'storage_cloud_budget_exceeded')
                reserve = policy['disk_reserve_bytes'] if scope == 'local' else 0
                if free - device_remaining - reserve < additional_bytes:
                    if scope == 'local':
                        await self._pressure(db, policy, owner, additional_bytes, filesystem, 'storage_disk_space_insufficient', existing)
                    raise StorageCapacityError('storage_disk_space_insufficient' if scope == 'local' else 'storage_account_quota_exceeded')
                identifier = str(uuid.uuid4())
                db.add(StorageReservation(id=identifier, owner=owner, operation_id=operation_id,
                    scope=scope, filesystem=filesystem, path=os.path.abspath(path) if scope == 'local' else str(path),
                    peak_bytes=additional_bytes, track_materialized=track_materialized,
                    baseline_bytes=sum(_owned_entries(str(path), measured, sqlite_backup=owner == 'backup.snapshot').values()) if scope == 'local' and track_materialized else 0))
                if scope == 'local':
                    pressure = await db.get(StorageMeasurement, 'capacity_pressure')
                    if pressure:
                        value = json.loads(pressure.payload)
                        if value.get('owner') == owner and value.get('additional_bytes', 0) <= additional_bytes:
                            value['active'] = False
                            pressure.payload = json.dumps(value)
                            db.add(pressure)
                await db.commit()
                return identifier

    async def progress(self, reservation_id: str, written_bytes: int) -> None:
        async with self.lock:
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                row = await db.get(StorageReservation, reservation_id)
                if row is None or row.state != 'active':
                    raise StorageCapacityError('storage_reservation_missing')
                if (type(written_bytes) is not int or written_bytes < 0 or written_bytes > row.peak_bytes
                        or (row.scope == 'cloud' and written_bytes < row.written_bytes)):
                    raise StorageCapacityError('storage_reservation_exceeded')
                delta = written_bytes - row.written_bytes
                if row.scope == 'cloud' and delta:
                    measurement = await db.get(StorageMeasurement, 'cloud')
                    if measurement:
                        value = json.loads(measurement.payload)
                        if value.get('target_generation') == row.filesystem:
                            value['used_bytes'] = value.get('used_bytes', 0) + delta
                            if value.get('account_free_bytes') is not None:
                                value['account_free_bytes'] = max(0, value['account_free_bytes'] - delta)
                            measurement.payload = json.dumps(value)
                            db.add(measurement)
                row.written_bytes, row.updated_at = written_bytes, time.time()
                db.add(row)
                await db.commit()

    async def release(self, reservation_id: str) -> None:
        async with self.lock:
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                row = await db.get(StorageReservation, reservation_id)
                if row and row.state == 'active':
                    row.state, row.updated_at = 'released', time.time()
                    db.add(row)
                    await db.commit()

    async def remaining(self, scope: str = 'local', *, observe: bool = True) -> int:
        async with self.lock:
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                rows = (await db.exec(select(StorageReservation).where(StorageReservation.state == 'active'))).all()
            if not observe:
                return sum(max(0, row.peak_bytes - row.written_bytes) for row in rows if row.scope == scope)
            if scope == 'local':
                from services.storage_inventory import storage_inventory
                measured = await storage_inventory.local_measurement(include_entries=True)
            else:
                measured = {}
            return _remaining_from_sample(rows, measured, scope=scope)

    @asynccontextmanager
    async def allocation(self, owner: str, additional_bytes: int, path: Path | str, *, scope: str = 'local', track_materialized: bool = True):
        from lifecycle.media_work import settle
        reservation, cancelled = await settle(self.reserve(owner, additional_bytes, path, scope=scope, track_materialized=track_materialized))
        try:
            if cancelled:
                raise asyncio.CancelledError()
            yield reservation
        finally:
            # Preserve ownership until even cancellation observes the release.
            await settle(self.release(reservation))


storage_capacity = StorageCapacity()
