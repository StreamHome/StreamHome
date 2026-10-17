"""Durable compensation for media files, bound to catalog acceptance receipts."""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import uuid
import asyncio
import re
from contextvars import ContextVar
from pathlib import Path

from config import settings
from lifecycle.media_work import settle, MediaRecoveryRequired
from lifecycle.storage_retention import is_protected_path, retain_existing_tree

current_mutation: ContextVar["MediaMutation | None"] = ContextVar("media_mutation", default=None)


def copy_bounded(source: Path | str, destination: Path | str, maximum_bytes: int):
    """An admitted local copy cannot grow beyond its measured source ceiling."""
    written = 0
    with open(source, "rb") as incoming, open(destination, "wb") as outgoing:
        while block := incoming.read(1024 * 1024):
            written += len(block)
            if written > maximum_bytes:
                from services.storage_capacity import StorageCapacityError
                raise StorageCapacityError("storage_reservation_exceeded")
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    shutil.copystat(source, destination)
    return str(destination)


async def _read_acceptance_receipt(engine, operation_id: str):
    """Read acceptance independently of a failed committing session."""
    from models import MediaOperationReceipt
    from sqlmodel.ext.asyncio.session import AsyncSession

    async with AsyncSession(engine) as fresh:
        return await fresh.get(MediaOperationReceipt, operation_id)


def _sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_tree(root: Path):
    for directory, _, files in os.walk(root, topdown=False):
        for name in files:
            path = Path(directory) / name
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _sync_directory(Path(directory))


def _retained_files(root: Path):
    """Reject ambiguous links before making a replacement tree."""
    if not root.exists():
        return []
    result = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise MediaRecoveryRequired("Media replacement contains an ambiguous symbolic link")
        if path.is_file() and is_protected_path(path.relative_to(root)):
            result.append(path)
    return result


def _same_bytes(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as first, right.open("rb") as second:
        return hashlib.file_digest(first, "sha256").digest() == hashlib.file_digest(second, "sha256").digest()


def _local_path(value: str) -> Path:
    path = Path(value).absolute()
    root = Path(settings.MEDIA_DIR).resolve()
    path.relative_to(root)
    if path.is_symlink() or not path.parent.resolve().is_relative_to(root):
        raise ValueError("Media publication paths must be contained regular files")
    return path


def _cloud_path(value: str) -> str:
    root = settings.RCLONE_REMOTE_PATH.rstrip("/") + "/"
    if not value.startswith(root) or ".." in value[len(root):].split("/"):
        raise ValueError("Cloud publication path is outside the configured media root")
    return value


def _contains_asset(directory: dict, kind: str, value: str) -> bool:
    if directory["kind"] == "directory" and kind == "local":
        path, parent = Path(value), Path(directory["path"])
        return path != parent and path.is_relative_to(parent)
    if directory["kind"] == "cloud_directory" and kind == "cloud":
        return value.startswith(directory["path"].rstrip("/") + "/")
    return False


def _validate_directory_ownership(entries: list[dict]) -> None:
    """Reject ambiguous replay before touching any original or backup."""
    directories = [(index, entry) for index, entry in enumerate(entries)
                   if entry["kind"] in {"directory", "cloud_directory"}]
    for offset, (index, directory) in enumerate(directories):
        asset_kind = "local" if directory["kind"] == "directory" else "cloud"
        for _, other in directories[offset + 1:]:
            if (other["kind"] == directory["kind"]
                    and (_contains_asset(directory, asset_kind, other["path"])
                         or _contains_asset(other, asset_kind, directory["path"]))):
                raise ValueError("Overlapping media directory publications are ambiguous")
    for index, entry in enumerate(entries):
        if entry["kind"] not in {"local", "cloud"}:
            continue
        if "directory_owner" not in entry:
            # Version-1 evidence predates capture-time ownership. List order
            # cannot tell whether this child was captured during construction
            # or after publication; never guess using the parent's final phase.
            if any(parent_index < index and _contains_asset(parent, entry["kind"], entry["path"])
                   for parent_index, parent in directories):
                raise ValueError("Media asset capture timing needs recovery evidence")
            continue
        owner = entry["directory_owner"]
        if owner is None:
            continue
        if (type(owner) is not int or not 0 <= owner < index
                or not _contains_asset(entries[owner], entry["kind"], entry["path"])
                or entries[owner].get("phase") != "published"):
            raise ValueError("Invalid media asset directory ownership")


def validate_operation_manifest(path: Path) -> dict:
    """Validate the whole replay plan before any asset is changed or deleted."""
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Unsafe media recovery manifest")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or payload.get("operation_id") != path.parent.name
            or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name)
            or not isinstance(payload.get("media_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", payload["media_id"])
            or payload.get("phase", "prepared") not in {"prepared", "compensated"}
            or not isinstance(payload.get("entries"), list) or len(payload["entries"]) > 1000):
        raise ValueError("Unrecognized media recovery manifest")
    seen = set()
    for entry in payload["entries"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("existed"), bool) or not isinstance(entry.get("backup"), str)):
            raise ValueError("Invalid media recovery entry")
        kind = entry.get("kind")
        key = (kind, entry["path"])
        if key in seen:
            raise ValueError("Duplicate media recovery entry")
        seen.add(key)
        if kind in {"local", "directory"}:
            destination = _local_path(entry["path"])
            if destination == Path(settings.MEDIA_DIR).resolve():
                raise ValueError("A media operation cannot own the library root")
        elif kind in {"cloud", "cloud_directory"}:
            _cloud_path(entry["path"])
        else:
            raise ValueError("Unknown media recovery entry")
        if kind in {"local", "cloud"}:
            if not re.fullmatch(r"asset-[0-9]+", entry["backup"]):
                raise ValueError("Invalid media asset backup")
            backup = path.parent / entry["backup"]
            if backup.is_symlink() or (backup.exists() and not backup.is_file()):
                raise ValueError("Unsafe media asset backup")
            if "retained_cloud" in entry:
                retained = entry["retained_cloud"]
                expected = (f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/.streamhome-retained/"
                    f"{payload['operation_id']}/{entry['backup']}/{entry['path'].rsplit('/', 1)[-1]}")
                if (kind != "cloud" or entry["existed"] or not is_protected_path(entry["path"])
                        or not isinstance(retained, dict) or retained.get("path") != expected
                        or not re.fullmatch(r"[0-9a-f]{64}", str(retained.get("sha256", "")))
                        or retained.get("phase") not in {"prepared", "verified"}
                        or type(retained.get("size")) is not int or retained["size"] < 0):
                    raise ValueError("Invalid protected cloud retention evidence")
                _cloud_path(retained["path"])
        else:
            if entry.get("phase") not in {"building", "prepared", "preserved", "published"} or not isinstance(entry.get("candidate"), str):
                raise ValueError("Invalid media directory recovery phase")
            if kind == "directory":
                for field, suffix in (("backup", "previous"), ("candidate", "candidate")):
                    expected = destination.with_name(f".{destination.name}.{payload['operation_id']}.{suffix}")
                    if _local_path(entry[field]) != expected:
                        raise ValueError("Media directory recovery identity mismatch")
                identity = entry.get("original_identity")
                if entry["existed"] and (not isinstance(identity, list) or len(identity) != 2 or any(not isinstance(value, int) for value in identity)):
                    raise ValueError("Media directory continuity evidence is missing")
            else:
                for field, namespace in (("backup", ".streamhome-replacement-backups"), ("candidate", ".streamhome-ingestion")):
                    if _cloud_path(entry[field]) != f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/{namespace}/{payload['operation_id']}":
                        raise ValueError("Cloud recovery identity mismatch")
    _validate_directory_ownership(payload["entries"])
    return payload


class MediaMutation:
    def __init__(self, media_id: str):
        self.operation_id = uuid.uuid4().hex
        self.media_id = media_id
        self.root = Path(settings.TEMP_DIR).resolve() / "media-operations" / self.operation_id
        self.entries: list[dict] = []
        self.accepted = False
        self.acceptance_pending = False
        self.entered = False
        self.compensated = False
        self._admitted_local: set[str] = set()
        self._capacity_reservations: list[str] = []
        self._admitted_directories: set[str] = set()
        self._copy_reservations: dict[tuple[str, str], str] = {}
        self._capture_limits: dict[str, int] = {}
        self._directory_limits: dict[str, int] = {}

    async def _reserve_copy(self, size: int, path: Path | str, *, scope: str = "local") -> str:
        from services.storage_capacity import storage_capacity
        key = (scope, str(path))
        if key in self._copy_reservations:
            return self._copy_reservations[key]
        reservation, cancelled = await settle(storage_capacity.reserve(
            f"media-mutation:{self.operation_id}", size, path, operation_id=self.operation_id, scope=scope))
        self._capacity_reservations.append(reservation)
        self._copy_reservations[key] = reservation
        if cancelled:
            raise asyncio.CancelledError
        return reservation

    async def _download_cloud_copy(self, remote: str, destination: Path, size: int, *, workspace: Path | None = None):
        from services.rclone import rclone_service
        from services.storage_capacity import StorageCapacityError, storage_capacity
        workspace = workspace or self.root / f".copy-{uuid.uuid4().hex}"
        reservation = await self._reserve_copy(size, workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        copying = asyncio.create_task(rclone_service.copyto_atomic(remote, str(workspace / "asset"), timeout=900))
        try:
            while not copying.done():
                await asyncio.wait({copying}, timeout=0.1)
                written = sum(path.stat().st_size for path in workspace.iterdir() if path.is_file())
                if written > size:
                    raise StorageCapacityError("storage_reservation_exceeded")
                await storage_capacity.progress(reservation, written)
            result = await copying
            completed = workspace / "asset"
            if result.ok and completed.is_file() and completed.stat().st_size == size:
                os.replace(completed, destination)
                _sync_directory(destination.parent)
                return result
            raise RuntimeError("Could not preserve complete cloud compensation bytes")
        finally:
            if not copying.done():
                copying.cancel()
            await settle(asyncio.gather(copying, return_exceptions=True))
            await settle(asyncio.to_thread(shutil.rmtree, workspace))
            await settle(storage_capacity.release(reservation))
            self._capacity_reservations.remove(reservation)
            self._copy_reservations.pop(("local", str(workspace)), None)

    async def capture_local_asset(self, value: str) -> None:
        """Admit preserved originals and rollback overlap before copying bytes."""
        from services.storage_capacity import storage_capacity
        path = _local_path(value)
        if any(e["kind"] == "local" and e["path"] == str(path) for e in self.entries):
            return
        size = path.stat().st_size if path.is_file() else 0
        backup = self.root / f"asset-{len(self.entries)}"
        reservation = await self._reserve_copy(size, backup)
        if size:
            await self._reserve_copy(size, path.with_name(f".{path.name}.{self.operation_id}.rollback"))
        self._admitted_local.add(str(path))
        self._capture_limits[str(path)] = size
        _, cancelled = await settle(asyncio.to_thread(self.capture_local, value))
        await storage_capacity.progress(reservation, backup.stat().st_size if backup.is_file() else 0)
        if cancelled:
            raise asyncio.CancelledError

    def persist(self):
        self.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root.parent, 0o700)
        _sync_directory(self.root.parent.parent)
        _sync_directory(self.root.parent.parent.parent)
        initial = not self.root.exists()
        directory = self.root.with_name(f".{self.operation_id}.creating") if initial else self.root
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / "operation.json"
        temporary = directory / "operation.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "operation_id": self.operation_id,
                       "media_id": self.media_id, "phase": "compensated" if self.compensated else "prepared",
                       "entries": self.entries}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(directory)
        if initial:
            os.replace(directory, self.root)
            _sync_directory(self.root.parent)

    def capture_local(self, value: str):
        path = _local_path(value)
        from services.storage_inventory import hybrid_policy_active
        if hybrid_policy_active() and str(path) not in self._admitted_local:
            from services.storage_capacity import StorageCapacityError
            raise StorageCapacityError("storage_copy_admission_required")
        if any(e["kind"] == "local" and e["path"] == str(path) for e in self.entries):
            return
        if not self.root.exists():
            self.persist()
        backup = self.root / f"asset-{len(self.entries)}"
        existed = path.exists()
        if existed:
            if not path.is_file():
                raise ValueError("Only regular media assets can be captured")
            copy_bounded(path, backup, self._capture_limits.get(str(path), path.stat().st_size))
            with backup.open("rb") as handle:
                os.fsync(handle.fileno())
        self.entries.append({"kind": "local", "path": str(path), "existed": existed,
                             "backup": backup.name,
                             "directory_owner": self._directory_owner("local", str(path))})
        self.persist()

    def _directory_owner(self, kind: str, value: str) -> int | None:
        for index, entry in enumerate(self.entries):
            if entry.get("phase") == "published" and _contains_asset(entry, kind, value):
                return index
        return None

    async def capture_cloud(self, value: str):
        from services.rclone import rclone_service
        path = _cloud_path(value)
        if any(e["kind"] == "cloud" and e["path"] == path for e in self.entries):
            return
        if not self.root.exists():
            self.persist()
        backup = self.root / f"asset-{len(self.entries)}"
        state = await rclone_service.run("lsjson", path, "--stat", timeout=60)
        if not state.ok and state.error_code != "drive_not_found":
            raise RuntimeError("Could not establish previous cloud asset identity")
        existed = state.ok
        if existed:
            from services.storage_inventory import hybrid_policy_active
            if hybrid_policy_active():
                payload = json.loads(state.stdout)
                if isinstance(payload, list):
                    payload = payload[0]
                size = int(payload["Size"])
                if size < 0:
                    raise ValueError("Cloud compensation size is invalid")
                await self._reserve_copy(size, self.root / f".verification-{backup.name}")
                await self._reserve_copy(size, path, scope="cloud")
                copied = await self._download_cloud_copy(path, backup, size)
            else:
                copied = await rclone_service.copyto_atomic(path, str(backup), timeout=900)
            if not copied.ok or not backup.is_file():
                raise RuntimeError("Could not preserve previous cloud asset")
            with backup.open("rb") as handle:
                os.fsync(handle.fileno())
            _sync_directory(backup.parent)
        self.entries.append({"kind": "cloud", "path": path, "existed": existed,
                             "backup": backup.name,
                             "directory_owner": self._directory_owner("cloud", path)})
        self.persist()

    async def commit(self, db):
        from models import MediaOperationReceipt
        from sqlalchemy.ext.asyncio import AsyncEngine

        # Runtime callers own their transactions on a single AsyncEngine. An
        # externally managed connection cannot prove durable session acceptance.
        engine = db.bind
        if not isinstance(engine, AsyncEngine):
            raise ValueError("Media acceptance requires an engine-bound session")
        if db.get_bind(mapper=MediaOperationReceipt) is not engine.sync_engine:
            raise ValueError("Media acceptance receipt is bound to a different engine")
        db.add(MediaOperationReceipt(operation_id=self.operation_id, media_id=self.media_id))

        async def commit_and_resolve():
            self.acceptance_pending = True
            try:
                await db.commit()
            except BaseException:
                try:
                    # A failing after_commit hook may leave an already committed
                    # ORM transaction. Release it before deciding with a fresh
                    # session on the exact engine that attempted acceptance.
                    await db.close()
                    receipt = await _read_acceptance_receipt(engine, self.operation_id)
                    if receipt is not None and receipt.media_id != self.media_id:
                        raise ValueError("Media acceptance receipt mismatch")
                except BaseException as verification_error:
                    raise MediaRecoveryRequired("Media acceptance outcome needs recovery") from verification_error
                if receipt is not None:
                    self.accepted = True
                self.acceptance_pending = False
                # Preserve the acknowledgment error even when acceptance is
                # proven. Asset handling follows the receipt, not this exception.
                raise
            self.accepted = True
            self.acceptance_pending = False

        _, cancelled = await settle(commit_and_resolve())
        if cancelled:
            raise asyncio.CancelledError

    def publish_directory(self, staging: str, destination: str):
        """Use same-filesystem renames; intent precedes the first destructive move."""
        destination = _local_path(destination)
        from services.storage_inventory import hybrid_policy_active
        if hybrid_policy_active() and str(destination) not in self._admitted_directories:
            from services.storage_capacity import StorageCapacityError
            raise StorageCapacityError("storage_copy_admission_required")
        source = Path(staging).resolve()
        source.relative_to(Path(settings.TEMP_DIR).resolve())
        retained = _retained_files(destination)
        for original in retained:
            incoming = source / original.relative_to(destination)
            if incoming.exists() and (not incoming.is_file() or incoming.is_symlink() or not _same_bytes(original, incoming)):
                raise MediaRecoveryRequired("A protected media file conflicts with its replacement")
        destination.parent.mkdir(parents=True, exist_ok=True)
        candidate = destination.with_name(f".{destination.name}.{self.operation_id}.candidate")
        backup = destination.with_name(f".{destination.name}.{self.operation_id}.previous")
        if candidate.exists() or backup.exists():
            raise RuntimeError("Unresolved media directory publication evidence")
        entry = {"kind": "directory", "path": str(destination),
                 "existed": destination.exists(), "backup": str(backup),
                 "candidate": str(candidate), "phase": "building",
                 "original_identity": [destination.stat().st_dev, destination.stat().st_ino] if destination.exists() else None}
        _validate_directory_ownership([*self.entries, entry])
        self.entries.append(entry)
        self.persist()

        remaining = self._directory_limits.get(str(destination))
        def admitted_copy(source_file, destination_file):
            nonlocal remaining
            size = Path(source_file).stat().st_size
            if remaining is not None:
                if size > remaining:
                    from services.storage_capacity import StorageCapacityError
                    raise StorageCapacityError("storage_reservation_exceeded")
                remaining -= size
            return copy_bounded(source_file, destination_file, size)
        shutil.copytree(source, candidate, copy_function=admitted_copy)
        for original in retained:
            preserved = candidate / original.relative_to(destination)
            if not preserved.exists():
                preserved.parent.mkdir(parents=True, exist_ok=True)
                admitted_copy(original, preserved)
        _sync_tree(candidate)
        _sync_directory(candidate.parent)
        entry["phase"] = "prepared"
        self.persist()
        if entry["existed"]:
            os.replace(destination, backup)
            _sync_directory(destination.parent)
        entry["phase"] = "preserved"
        self.persist()
        os.replace(candidate, destination)
        _sync_directory(destination.parent)
        entry["phase"] = "published"
        self.persist()

    async def publish_directory_admitted(self, staging: str, destination: str):
        """Publication copies have their own device-bound peak admission."""
        from services.storage_capacity import storage_capacity
        source = Path(staging).resolve()
        target = _local_path(destination)
        size = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
        size += sum(path.stat().st_size for path in _retained_files(target)
                    if not (source / path.relative_to(target)).exists())
        candidate = target.with_name(f".{target.name}.{self.operation_id}.candidate")
        reservation = await self._reserve_copy(size, candidate)
        self._admitted_directories.add(str(target))
        self._directory_limits[str(target)] = size
        _, cancelled = await settle(asyncio.to_thread(self.publish_directory, staging, destination))
        await storage_capacity.progress(reservation, size)
        if cancelled:
            raise asyncio.CancelledError

    async def publish_cloud_directory(self, staging: str, subpath: str):
        from services.rclone import rclone_service
        target = _cloud_path(f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/{subpath.replace(chr(92), '/')}")
        remote_staging = f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/.streamhome-ingestion/{self.operation_id}"
        backup = f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/.streamhome-replacement-backups/{self.operation_id}"
        entry = {"kind": "cloud_directory", "path": target,
                 "backup": backup, "candidate": remote_staging,
                 "existed": False, "phase": "building"}
        _validate_directory_ownership([*self.entries, entry])
        self.entries.append(entry)
        self.persist()
        for arguments in (("copy", staging, remote_staging), ("check", staging, remote_staging, "--one-way")):
            result = await rclone_service.run(*arguments, timeout=6 * 3600)
            if not result.ok:
                raise RuntimeError("Cloud candidate verification failed")
        previous = await rclone_service.run("lsjson", target, "--stat", timeout=60)
        if not previous.ok and previous.error_code != "drive_not_found":
            raise RuntimeError("Cloud publication target identity is unknown")
        entry["existed"] = previous.ok
        if previous.ok:
            copied = await rclone_service.run("copy", target, backup, timeout=6 * 3600)
            checked = await rclone_service.run("check", target, backup, timeout=3600)
            if not copied.ok or not checked.ok:
                raise RuntimeError("Could not preserve the previous cloud tree")
        # Check protected collisions before any target write. A preserved subtitle
        # cannot silently disagree with the candidate catalog's sidecar claim.
        await self._check_protected_cloud_collisions(remote_staging, target)
        entry["phase"] = "preserved"
        self.persist()
        await self._copy_cloud_tree_preserving(remote_staging, target)
        entry["phase"] = "published"
        self.persist()

    async def _cloud_files(self, remote: str) -> list[str]:
        from services.rclone import rclone_service
        result = await rclone_service.run("lsjson", remote, "--recursive", "--files-only",
            timeout=300, output_limit=None, hard_output_limit=8 * 1024 * 1024)
        if not result.ok:
            if result.error_code == "drive_not_found":
                return []
            raise MediaRecoveryRequired("Cloud retention inventory is unavailable")
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) > 20000:
            raise MediaRecoveryRequired("Cloud retention inventory exceeds its bounded manifest")
        files = []
        for item in payload:
            name = item.get("Path") if isinstance(item, dict) else None
            if (not isinstance(name, str) or not name or name.startswith("/")
                    or ".." in name.split("/") or any(char in name for char in "\r\n\\\x00")):
                raise MediaRecoveryRequired("Cloud retention inventory contains an unsafe path")
            files.append(name)
        return files

    def _cloud_file_list(self, names: list[str]) -> Path:
        path = self.root / f"retention-files-{uuid.uuid4().hex}"
        with path.open("x", encoding="utf-8") as handle:
            handle.write("".join(name + "\n" for name in names))
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(path.parent)
        return path

    async def _check_protected_cloud_collisions(self, source: str, target: str,
            *, independent_children: frozenset[str] = frozenset()):
        from services.rclone import rclone_service
        incoming = set(await self._cloud_files(source))
        existing = set(await self._cloud_files(target))
        collisions = sorted(name for name in incoming & existing
                            if is_protected_path(name) and name not in independent_children)
        if collisions:
            manifest = self._cloud_file_list(collisions)
            result = await rclone_service.run("check", source, target, "--one-way",
                "--download", "--files-from-raw", str(manifest), timeout=3600)
            if not result.ok:
                raise MediaRecoveryRequired("A protected cloud file conflicts with its replacement")

    async def _copy_cloud_tree_preserving(self, source: str, target: str,
            *, independent_children: frozenset[str] = frozenset()):
        """Add sidecars without replacement; overwrite only catalog-owned sources.

        Copy never removes destination-only data. The exact manifest also keeps
        videos nested in a cache directory outside the overwrite set.
        """
        from services.rclone import rclone_service
        files = [name for name in await self._cloud_files(source) if name not in independent_children]
        if not files:
            return
        # During replay, independently captured children restore their original
        # bytes after the containing tree. Replaying an intermediate tree over
        # an already restored child would violate that temporal ownership.
        selected = ("--files-from-raw", str(self._cloud_file_list(files))) if independent_children else ()
        added = await rclone_service.run("copy", source, target, "--ignore-existing", *selected, timeout=6 * 3600)
        if not added.ok:
            raise RuntimeError("Cloud publication additive copy failed")
        mutable = sorted(name for name in files if not is_protected_path(name))
        if mutable:
            manifest = self._cloud_file_list(mutable)
            result = await rclone_service.run("copy", source, target, "--ignore-times",
                "--files-from-raw", str(manifest), timeout=6 * 3600)
            if not result.ok:
                raise RuntimeError("Cloud publication replacement failed")
        checked = await rclone_service.run("check", source, target, "--one-way", *selected, timeout=3600)
        if not checked.ok:
            raise RuntimeError("Cloud publication failed verification")

    async def _cloud_digest(self, path: str) -> str | None:
        """Stream a strong hash even for providers without a native checksum."""
        from services.rclone import rclone_service
        result = await rclone_service.run("hashsum", "sha256", path, "--download",
            timeout=3600, output_limit=None, hard_output_limit=8192)
        if not result.ok:
            if result.error_code == "drive_not_found":
                return None
            raise MediaRecoveryRequired("Protected cloud bytes could not be verified")
        lines = result.stdout.splitlines()
        if not lines:
            return None
        if len(lines) != 1 or not re.match(r"^[0-9a-f]{64}  .+$", lines[0]):
            raise MediaRecoveryRequired("Protected cloud identity is ambiguous")
        return lines[0][:64]

    async def _retain_created_cloud_asset(self, entry: dict):
        """Journal, verify and retire one unaccepted protected generation.

        A durable verified archive permits retry after delete acknowledgment loss;
        a changed visible generation or archive never authorizes another delete.
        """
        from services.rclone import rclone_service
        path = _cloud_path(entry["path"])
        retained = entry.get("retained_cloud")
        if retained is None:
            digest = await self._cloud_digest(path)
            if digest is None:
                return
            stat = await rclone_service.run("lsjson", path, "--stat", timeout=60)
            if not stat.ok:
                raise MediaRecoveryRequired("Protected cloud identity is unavailable")
            payload = json.loads(stat.stdout)
            if isinstance(payload, list) and len(payload) == 1:
                payload = payload[0]
            size = payload.get("Size") if isinstance(payload, dict) else None
            if type(size) is not int or size < 0 or payload.get("IsDir") is True:
                raise MediaRecoveryRequired("Protected cloud identity is ambiguous")
            basename = path.rsplit("/", 1)[-1]
            if any(char in basename for char in "\r\n\\\x00"):
                raise MediaRecoveryRequired("Protected cloud path is unsafe")
            retained = {"path": f"{settings.RCLONE_REMOTE_PATH.rstrip('/')}/.streamhome-retained/"
                f"{self.operation_id}/{entry['backup']}/{basename}",
                "sha256": digest, "size": size, "phase": "prepared"}
            entry["retained_cloud"] = retained
            self.persist()
        archived = _cloud_path(retained["path"])
        expected = retained["sha256"]
        visible = await self._cloud_digest(path)
        if visible is not None and visible != expected:
            raise MediaRecoveryRequired("Protected cloud generation changed during retention")
        archive_digest = await self._cloud_digest(archived)
        from services.storage_inventory import hybrid_policy_active
        reservation = (await self._reserve_copy(retained["size"], archived, scope="cloud")
                       if hybrid_policy_active() else None)
        if archive_digest != expected:
            if retained["phase"] == "verified" or visible is None:
                raise MediaRecoveryRequired("Verified protected cloud archive changed")
            copied = await rclone_service.run("copyto", path, archived, "--ignore-times", timeout=3600)
            if not copied.ok or await self._cloud_digest(archived) != expected:
                raise MediaRecoveryRequired("Protected cloud archive could not be verified")
        if reservation is not None:
            from services.storage_capacity import storage_capacity
            _, cancelled = await settle(storage_capacity.progress(reservation, retained["size"]))
            if cancelled:
                raise asyncio.CancelledError
        retained["phase"] = "verified"
        self.persist()
        if visible is not None:
            if await self._cloud_digest(path) != expected:
                raise MediaRecoveryRequired("Protected cloud generation changed before retirement")
            deleted = await rclone_service.run("deletefile", path, timeout=60)
            if not deleted.ok and deleted.error_code != "drive_not_found":
                raise MediaRecoveryRequired("Protected cloud retirement acknowledgment is pending")

    async def cleanup(self):
        from services.rclone import rclone_service
        for entry in self.entries:
            if entry["kind"] == "directory":
                for name in ("backup", "candidate"):
                    path = _local_path(entry[name])
                    if path.exists():
                        # Includes legacy/ambiguous generations and any sidecars
                        # created by a rejected candidate. Never recurse-delete.
                        retain_existing_tree(path)
            elif entry["kind"] == "cloud_directory":
                # These uniquely named trees are durable retained generations.
                # A recursive provider purge cannot prove that its descendants
                # contain no accepted or ambiguous cache/sidecar bytes.
                continue
        # Retain the manifest until every backup has been removed. Its durable
        # compensated phase permits a retry after partial cleanup.
        for child in self.root.iterdir():
            if child.name == "operation.json":
                continue
            if child.is_dir():
                raise RuntimeError("Unexpected media cleanup artifact")
            child.unlink()
        _sync_directory(self.root)
        (self.root / "operation.json").unlink()
        _sync_directory(self.root)
        self.root.rmdir()
        _sync_directory(self.root.parent)
        # Reservations remain durable while compensation/evidence remain. A
        # successful cleanup also resolves admission from an interrupted owner.
        if self._capacity_reservations:
            from services.storage_capacity import storage_capacity
            for reservation in self._capacity_reservations:
                await storage_capacity.release(reservation)
            self._capacity_reservations.clear()

    async def compensate(self):
        from services.rclone import rclone_service
        _validate_directory_ownership(self.entries)
        failures = []
        # Restore containing original trees before independent child captures.
        # A capture owned by a published tree contains rejected candidate bytes,
        # never pre-operation evidence, and must not be replayed into the original.
        ordered = [entry for entry in reversed(self.entries)
                   if entry["kind"] in {"directory", "cloud_directory"}]
        ordered.extend(entry for entry in reversed(self.entries)
                       if entry["kind"] not in {"directory", "cloud_directory"}
                       and entry.get("directory_owner") is None)
        for entry in ordered:
            try:
                if entry["kind"] == "directory":
                    path = _local_path(entry["path"])
                    backup = _local_path(entry["backup"])
                    if entry["existed"]:
                        if backup.exists():
                            if path.exists():
                                retain_existing_tree(path)
                            os.replace(backup, path)
                            _sync_directory(path.parent)
                        elif entry["phase"] not in {"building", "prepared"}:
                            identity = [path.stat().st_dev, path.stat().st_ino] if path.exists() else None
                            if identity != entry.get("original_identity"):
                                raise RuntimeError("Previous directory evidence is missing")
                    elif path.exists():
                        retain_existing_tree(path)
                    continue
                if entry["kind"] == "cloud_directory":
                    path = _cloud_path(entry["path"])
                    if entry.get("phase") == "building":
                        continue
                    if entry["existed"]:
                        backup_remote = _cloud_path(entry["backup"])
                        independent = frozenset(child["path"][len(path.rstrip("/")) + 1:]
                            for child in self.entries
                            if child["kind"] == "cloud" and child.get("directory_owner") is None
                            and _contains_asset(entry, "cloud", child["path"]))
                        await self._check_protected_cloud_collisions(backup_remote, path,
                            independent_children=independent)
                        await self._copy_cloud_tree_preserving(backup_remote, path,
                            independent_children=independent)
                    else:
                        # Remove only proven current-job source/catalog objects;
                        # sidecars and caches survive even rejected publication.
                        candidate = _cloud_path(entry["candidate"])
                        for name in await self._cloud_files(candidate):
                            if is_protected_path(name):
                                continue
                            manifest = self._cloud_file_list([name])
                            checked = await rclone_service.run("check", candidate, path,
                                "--one-way", "--download", "--files-from-raw", str(manifest), timeout=3600)
                            if not checked.ok:
                                raise MediaRecoveryRequired("Cloud rollback object ownership is ambiguous")
                            result = await rclone_service.run("deletefile", f"{path}/{name}", timeout=60)
                            if not result.ok and result.error_code != "drive_not_found":
                                raise RuntimeError("Cloud directory compensation failed")
                    continue
                backup = self.root / entry["backup"]
                if backup.parent != self.root or not entry["backup"].startswith("asset-"):
                    raise ValueError("Invalid media compensation evidence")
                if entry["kind"] == "local":
                    path = _local_path(entry["path"])
                    if entry["existed"]:
                        if not backup.is_file():
                            raise RuntimeError("Original media asset evidence is missing")
                        path.parent.mkdir(parents=True, exist_ok=True)
                        # Keep backup evidence until the whole compensation succeeds.
                        temporary = path.with_name(f".{path.name}.{self.operation_id}.rollback")
                        copy_bounded(backup, temporary, backup.stat().st_size)
                        with temporary.open("rb") as handle:
                            os.fsync(handle.fileno())
                        os.replace(temporary, path)
                        _sync_directory(path.parent)
                    else:
                        if path.exists() and is_protected_path(path):
                            retain_existing_tree(path)
                        else:
                            path.unlink(missing_ok=True)
                        if path.parent.exists():
                            _sync_directory(path.parent)
                elif entry["kind"] == "cloud":
                    path = _cloud_path(entry["path"])
                    if entry["existed"]:
                        if backup.is_symlink() or not backup.is_file():
                            raise RuntimeError("Original cloud asset evidence is missing")
                        result = await rclone_service.run("copyto", str(backup), path, timeout=900)
                        if result.ok:
                            verification = self.root / f"verify-{backup.name}"
                            from services.storage_inventory import hybrid_policy_active
                            if hybrid_policy_active():
                                verified = await self._download_cloud_copy(path, verification, backup.stat().st_size,
                                    workspace=self.root / f".verification-{backup.name}")
                            else:
                                verified = await rclone_service.copyto_atomic(path, str(verification), timeout=900)
                            def digest(file):
                                with file.open("rb") as handle:
                                    return hashlib.file_digest(handle, "sha256").hexdigest()
                            matches = False
                            if verified.ok and verification.is_file():
                                matches, _ = await settle(asyncio.to_thread(lambda: digest(verification) == digest(backup)))
                            if not matches:
                                raise RuntimeError("Cloud compensation could not be verified")
                    else:
                        if is_protected_path(path):
                            await self._retain_created_cloud_asset(entry)
                            continue
                        result = await rclone_service.run("deletefile", path, timeout=60)
                    if not result.ok and result.error_code != "drive_not_found":
                        raise RuntimeError("Cloud compensation failed")
                else:
                    raise ValueError("Unknown media compensation operation")
            except Exception as error:
                failures.append(error)
        if failures:
            raise RuntimeError("Media compensation requires recovery") from failures[0]
        if any(entry["kind"] in {"cloud", "cloud_directory"} for entry in self.entries):
            # Publication may have cached the rejected object's stat. Restored
            # bytes must be visible to ticket/cache identity checks immediately.
            from services.media_source import clear_cloud_object_cache
            clear_cloud_object_cache()
        self.compensated = True
        self.persist()

    async def __aenter__(self):
        parent = self.root.parent
        if parent.exists():
            if parent.is_symlink() or not parent.is_dir():
                raise MediaRecoveryRequired("Unsafe media recovery evidence")
            for directory in parent.iterdir():
                try:
                    payload = validate_operation_manifest(directory / "operation.json")
                except (OSError, ValueError, UnicodeError) as error:
                    raise MediaRecoveryRequired("Unrecognized media recovery evidence") from error
                if payload["media_id"] == self.media_id:
                    raise MediaRecoveryRequired("Previous media operation requires recovery")
        self.persist()
        self.token = current_mutation.set(self)
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if not self.entered:
            return
        self.entered = False
        current_mutation.reset(self.token)
        if self.acceptance_pending:
            # No commit result or failed-session snapshot authorizes rollback.
            # Startup can resolve this manifest against the durable receipt.
            raise MediaRecoveryRequired("Media acceptance outcome needs recovery")
        if not self.accepted:
            await settle(self.compensate())
        await settle(self.cleanup())


async def recover_media_operations():
    """Run before catalog scanning; never discard unresolved original assets."""
    from db import engine
    from models import MediaOperationReceipt
    from sqlmodel.ext.asyncio.session import AsyncSession
    root = Path(settings.TEMP_DIR).resolve() / "media-operations"
    if root.is_symlink():
        raise RuntimeError("Unsafe media recovery root")
    if not root.exists():
        return
    for directory in sorted(root.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Unsafe media recovery evidence")
        path = directory / "operation.json"
        if re.fullmatch(r"\.[0-9a-f]{32}\.creating", directory.name):
            # An initial manifest is published atomically before asset work.
            if any(child.name not in {"operation.tmp", "operation.json"} or child.is_symlink() or not child.is_file() for child in directory.iterdir()):
                raise RuntimeError("Ambiguous initial media operation evidence")
            shutil.rmtree(directory)
            _sync_directory(root)
            continue
        if not any(directory.iterdir()):
            directory.rmdir()
            _sync_directory(root)
            continue
        if not path.is_file():
            raise RuntimeError("Media operation manifest is missing")
        if path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError("Unsafe media recovery evidence")
        payload = validate_operation_manifest(path)
        mutation = MediaMutation(payload["media_id"])
        mutation.operation_id = payload["operation_id"]
        mutation.root = path.parent
        mutation.entries = payload["entries"]
        mutation.compensated = payload.get("phase") == "compensated"
        async with AsyncSession(engine) as db:
            receipt = await db.get(MediaOperationReceipt, mutation.operation_id)
            from models import StorageReservation
            from sqlmodel import select
            reservations = (await db.exec(select(StorageReservation).where(
                StorageReservation.operation_id == mutation.operation_id, StorageReservation.state == "active"))).all()
            mutation._capacity_reservations = [row.id for row in reservations]
            mutation._copy_reservations = {(row.scope, row.path): row.id for row in reservations}
        if receipt is None and not mutation.compensated:
            await mutation.compensate()
        elif receipt is not None and receipt.media_id != mutation.media_id:
            raise RuntimeError("Media acceptance receipt mismatch")
        await mutation.cleanup()
