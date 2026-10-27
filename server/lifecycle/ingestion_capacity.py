"""Supervise separately owned ingestion outputs against durable storage claims."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil

from config import settings
from lifecycle.coordination import run_cancellation_safe_thread
from lifecycle.media_work import settle
from services.ingestion_estimate import MIB
from services.storage_capacity import StorageCapacityError, storage_capacity
from services.storage_common import policy_value


@dataclass
class Workspace:
    reservation: str
    path: Path
    peak: int
    growing: bool = True


def workspace_size(path: Path) -> int:
    size = 0
    for child in path.rglob('*'):
        try:
            if child.is_file() and not child.is_symlink():
                size += child.stat().st_size
        except FileNotFoundError:
            pass  # Native output and publication rename their task-owned parts.
    return size


class IngestionCapacity:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.workspaces: dict[str, Workspace] = {}
        self.lock = asyncio.Lock()

    async def admit(self, name: str, path: Path, peak: int) -> None:
        async with self.lock:
            if name in self.workspaces:
                raise StorageCapacityError('storage_reservation_exists')
            reservation, cancelled = await settle(storage_capacity.reserve(
                f'ingestion-{name}:{self.task_id}', peak, path))
            self.workspaces[name] = Workspace(reservation, path, peak)
            if cancelled:
                raise asyncio.CancelledError()

    async def check(self) -> None:
        async with self.lock:
            for workspace in self.workspaces.values():
                size = await run_cancellation_safe_thread(workspace_size, workspace.path)
                if size > workspace.peak:
                    raise StorageCapacityError('storage_reservation_exceeded')
                if workspace.growing and size >= workspace.peak - max(64 * MIB, workspace.peak // 8):
                    peak = workspace.peak + max(128 * MIB, workspace.peak // 2)
                    # The original reservation stays active throughout the check;
                    # failed admission cannot create an unreserved interval.
                    _, cancelled = await settle(storage_capacity.resize(workspace.reservation, peak))
                    workspace.peak = peak
                    if cancelled:
                        raise asyncio.CancelledError()
                await storage_capacity.progress(workspace.reservation, size)
                # Other host processes can consume physical space independently
                # of managed reservations. Preserve the configured emergency floor.
                existing = workspace.path
                while not existing.exists() and existing != existing.parent:
                    existing = existing.parent
                free = (await run_cancellation_safe_thread(shutil.disk_usage, existing)).free
                if free < policy_value(settings)['disk_reserve_bytes']:
                    raise StorageCapacityError('storage_disk_space_insufficient')

    async def seal(self, name: str, *, headroom: int = 0) -> None:
        """The caller has drained the native writer before freezing its workspace."""
        async with self.lock:
            workspace = self.workspaces[name]
            size = await run_cancellation_safe_thread(workspace_size, workspace.path)
            if size > workspace.peak:
                raise StorageCapacityError('storage_reservation_exceeded')
            await storage_capacity.progress(workspace.reservation, size)
            peak = size + headroom
            _, cancelled = await settle(storage_capacity.resize(workspace.reservation, peak))
            workspace.peak, workspace.growing = peak, False
            if cancelled:
                raise asyncio.CancelledError()

    async def close(self) -> None:
        # Queue cleanup calls this only after every native writer and monitor has
        # settled. Retained preview/sidecar bytes remain in the managed inventory.
        for workspace in self.workspaces.values():
            await settle(storage_capacity.release(workspace.reservation))
