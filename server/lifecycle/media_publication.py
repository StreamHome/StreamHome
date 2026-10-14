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

current_mutation: ContextVar["MediaMutation | None"] = ContextVar("media_mutation", default=None)


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
        if any(e["kind"] == "local" and e["path"] == str(path) for e in self.entries):
            return
        if not self.root.exists():
            self.persist()
        backup = self.root / f"asset-{len(self.entries)}"
        existed = path.exists()
        if existed:
            if not path.is_file():
                raise ValueError("Only regular media assets can be captured")
            shutil.copy2(path, backup)
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
        source = Path(staging).resolve()
        source.relative_to(Path(settings.TEMP_DIR).resolve())
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
        shutil.copytree(source, candidate)
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
        for arguments in (("sync", staging, remote_staging), ("check", staging, remote_staging, "--one-way")):
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
        entry["phase"] = "preserved"
        self.persist()
        result = await rclone_service.run("sync", remote_staging, target, timeout=6 * 3600)
        checked = await rclone_service.run("check", remote_staging, target, timeout=3600)
        if not result.ok or not checked.ok:
            raise RuntimeError("Cloud publication failed verification")
        entry["phase"] = "published"
        self.persist()

    async def cleanup(self):
        from services.rclone import rclone_service
        for entry in self.entries:
            if entry["kind"] == "directory":
                for name in ("backup", "candidate"):
                    path = _local_path(entry[name])
                    if path.exists():
                        shutil.rmtree(path)
                        _sync_directory(path.parent)
            elif entry["kind"] == "cloud_directory":
                for name in ("backup", "candidate"):
                    result = await rclone_service.run("purge", _cloud_path(entry[name]), timeout=3600)
                    if not result.ok and result.error_code != "drive_not_found":
                        raise RuntimeError("Cloud publication cleanup remains pending")
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
                                shutil.rmtree(path)
                            os.replace(backup, path)
                            _sync_directory(path.parent)
                        elif entry["phase"] not in {"building", "prepared"}:
                            identity = [path.stat().st_dev, path.stat().st_ino] if path.exists() else None
                            if identity != entry.get("original_identity"):
                                raise RuntimeError("Previous directory evidence is missing")
                    elif path.exists():
                        shutil.rmtree(path)
                        _sync_directory(path.parent)
                    continue
                if entry["kind"] == "cloud_directory":
                    path = _cloud_path(entry["path"])
                    if entry.get("phase") == "building":
                        continue
                    if entry["existed"]:
                        result = await rclone_service.run("sync", _cloud_path(entry["backup"]), path, timeout=6 * 3600)
                        checked = await rclone_service.run("check", _cloud_path(entry["backup"]), path, timeout=3600)
                        if not result.ok or not checked.ok:
                            raise RuntimeError("Cloud directory compensation failed")
                    else:
                        result = await rclone_service.run("purge", path, timeout=3600)
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
                        shutil.copy2(backup, temporary)
                        with temporary.open("rb") as handle:
                            os.fsync(handle.fileno())
                        os.replace(temporary, path)
                        _sync_directory(path.parent)
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
        if receipt is None and not mutation.compensated:
            await mutation.compensate()
        elif receipt is not None and receipt.media_id != mutation.media_id:
            raise RuntimeError("Media acceptance receipt mismatch")
        await mutation.cleanup()
