"""Strict manifest contracts and explicit publication for SQLite backups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Mapping


MANIFEST_SCHEMA_VERSION = 1
DATABASE_CONTRACT_VERSION = 1
BACKUP_FORMAT = "streamhome-sqlite-standalone"
MAX_MANIFEST_BYTES = 16 * 1024
MAX_BACKUP_BYTES = (1 << 63) - 1
LEGACY_BACKUP_RE = re.compile(r"^backup_\d{8}_\d{6}\.db$")
MANIFEST_BACKUP_RE = re.compile(r"^backup_v1_\d{8}_\d{6}\.db$")
BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class BackupManifestError(RuntimeError):
    code = "backup_manifest_invalid"


class BackupManifestMissingError(BackupManifestError):
    code = "backup_manifest_missing"


class BackupManifestPersistenceError(BackupManifestError):
    code = "backup_manifest_persistence_error"


@dataclass(frozen=True, slots=True)
class BackupDatabaseIdentity:
    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    schema_version: int
    backup_format: str
    created_at: str
    application_build_id: str
    database_contract_version: int
    database: BackupDatabaseIdentity


@dataclass(frozen=True, slots=True)
class BackupEvidence:
    format: str
    database: BackupDatabaseIdentity
    manifest: BackupManifest | None


def is_legacy_backup_name(filename: str) -> bool:
    return isinstance(filename, str) and LEGACY_BACKUP_RE.fullmatch(filename) is not None


def is_manifest_backup_name(filename: str) -> bool:
    return isinstance(filename, str) and MANIFEST_BACKUP_RE.fullmatch(filename) is not None


def is_supported_backup_name(filename: str) -> bool:
    return is_legacy_backup_name(filename) or is_manifest_backup_name(filename)


def manifest_path_for(database_path: Path) -> Path:
    if not is_supported_backup_name(database_path.name):
        raise BackupManifestError("backup filename is invalid")
    return database_path.with_name(f"{database_path.name}.manifest.json")


def _strict_keys(payload: Mapping[str, Any], expected: set[str], description: str) -> None:
    if set(payload) != expected:
        raise BackupManifestError(f"{description} contains missing or unknown fields")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupManifestError("backup manifest contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    del value
    raise BackupManifestError("backup manifest contains a non-finite number")


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
        raise BackupManifestError("backup manifest timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BackupManifestError("backup manifest timestamp is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise BackupManifestError("backup manifest timestamp must use UTC")
    return value


def _validate_size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_BACKUP_BYTES:
        raise BackupManifestError("backup database size is invalid")
    return value


def parse_manifest_payload(payload: Any) -> BackupManifest:
    if not isinstance(payload, Mapping):
        raise BackupManifestError("backup manifest must be an object")
    _strict_keys(
        payload,
        {
            "schema_version",
            "backup_format",
            "created_at",
            "application_build_id",
            "database_contract_version",
            "database",
        },
        "backup manifest",
    )
    if (
        isinstance(payload["schema_version"], bool)
        or not isinstance(payload["schema_version"], int)
        or payload["schema_version"] != MANIFEST_SCHEMA_VERSION
    ):
        raise BackupManifestError("backup manifest schema is unsupported")
    if payload["backup_format"] != BACKUP_FORMAT:
        raise BackupManifestError("backup manifest format is unsupported")
    created_at = _validate_timestamp(payload["created_at"])
    build_id = payload["application_build_id"]
    if not isinstance(build_id, str) or not BUILD_ID_RE.fullmatch(build_id):
        raise BackupManifestError("backup application build identity is invalid")
    contract = payload["database_contract_version"]
    if (
        isinstance(contract, bool)
        or not isinstance(contract, int)
        or contract != DATABASE_CONTRACT_VERSION
    ):
        raise BackupManifestError("backup database contract is unsupported")
    database = payload["database"]
    if not isinstance(database, Mapping):
        raise BackupManifestError("backup database identity must be an object")
    _strict_keys(database, {"filename", "size_bytes", "sha256"}, "backup database identity")
    filename = database["filename"]
    if not isinstance(filename, str) or not is_supported_backup_name(filename):
        raise BackupManifestError("backup database filename is invalid")
    digest = database["sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise BackupManifestError("backup database digest is invalid")
    return BackupManifest(
        MANIFEST_SCHEMA_VERSION,
        BACKUP_FORMAT,
        created_at,
        build_id,
        DATABASE_CONTRACT_VERSION,
        BackupDatabaseIdentity(filename, _validate_size(database["size_bytes"]), digest),
    )


def manifest_payload(manifest: BackupManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "backup_format": manifest.backup_format,
        "created_at": manifest.created_at,
        "application_build_id": manifest.application_build_id,
        "database_contract_version": manifest.database_contract_version,
        "database": {
            "filename": manifest.database.filename,
            "size_bytes": manifest.database.size_bytes,
            "sha256": manifest.database.sha256,
        },
    }


def _assert_regular_owned(path: Path, description: str, *, private: bool) -> os.stat_result:
    try:
        file_state = path.lstat()
    except OSError as exc:
        raise BackupManifestError(f"{description} is unavailable") from exc
    if not stat.S_ISREG(file_state.st_mode) or path.is_symlink():
        raise BackupManifestError(f"{description} is not a trusted regular file")
    if hasattr(os, "geteuid") and file_state.st_uid != os.geteuid():
        raise BackupManifestError(f"{description} has unexpected ownership")
    if private and stat.S_IMODE(file_state.st_mode) & 0o077:
        raise BackupManifestError(f"{description} permissions are not private")
    return file_state


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise BackupManifestError("backup database cannot be hashed") from exc
    return digest.hexdigest()


def database_identity(path: Path, *, private: bool) -> BackupDatabaseIdentity:
    file_state = _assert_regular_owned(path, "backup database", private=private)
    if file_state.st_size <= 0 or file_state.st_size > MAX_BACKUP_BYTES:
        raise BackupManifestError("backup database size is invalid")
    return BackupDatabaseIdentity(path.name, file_state.st_size, _sha256(path))


def _read_manifest(path: Path) -> BackupManifest:
    file_state = _assert_regular_owned(path, "backup manifest", private=True)
    if file_state.st_size > MAX_MANIFEST_BYTES:
        raise BackupManifestError("backup manifest exceeds its size limit")
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(
                handle,
                object_pairs_hook=_object_pairs,
                parse_constant=_reject_constant,
            )
    except BackupManifestError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupManifestError("backup manifest cannot be decoded") from exc
    return parse_manifest_payload(payload)


def validate_backup_pair(database_path: Path) -> BackupEvidence:
    if not is_supported_backup_name(database_path.name):
        raise BackupManifestError("backup filename is invalid")
    manifest_path = manifest_path_for(database_path)
    new_format = is_manifest_backup_name(database_path.name)
    if not manifest_path.exists() and not manifest_path.is_symlink():
        if new_format:
            raise BackupManifestMissingError("new-format backup manifest is missing")
        return BackupEvidence("legacy", database_identity(database_path, private=False), None)
    manifest = _read_manifest(manifest_path)
    identity = database_identity(database_path, private=new_format)
    if manifest.database.filename != database_path.name or manifest.database != identity:
        raise BackupManifestError("backup manifest does not match its database")
    return BackupEvidence("manifest_v1", identity, manifest)


def create_manifest(
    database_path: Path,
    *,
    application_build_id: str,
    created_at: datetime | None = None,
) -> BackupManifest:
    if not is_manifest_backup_name(database_path.name):
        raise BackupManifestError("manifest-backed backup filename is invalid")
    if created_at is not None and created_at.tzinfo is None:
        raise BackupManifestError("backup manifest timestamp must be timezone-aware")
    timestamp = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp_text = timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest = BackupManifest(
        MANIFEST_SCHEMA_VERSION,
        BACKUP_FORMAT,
        timestamp_text,
        application_build_id,
        DATABASE_CONTRACT_VERSION,
        database_identity(database_path, private=True),
    )
    # Validate caller-provided provenance before touching the manifest path.
    parse_manifest_payload(manifest_payload(manifest))
    path = manifest_path_for(database_path)
    if path.exists() or path.is_symlink():
        raise BackupManifestPersistenceError("backup manifest already exists")
    encoded = (json.dumps(manifest_payload(manifest), sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    published = False
    complete = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("manifest write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        # Atomic no-clobber publication prevents a concurrent path substitution.
        os.link(temporary, path, follow_symlinks=False)
        published = True
        temporary.unlink()
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        complete = True
        return manifest
    except (OSError, BackupManifestError) as exc:
        if isinstance(exc, BackupManifestError):
            raise
        raise BackupManifestPersistenceError("backup manifest could not be published") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not complete:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            if published:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    _fsync_directory(path.parent)
                except OSError:
                    pass


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def delete_backup_pair(database_path: Path) -> None:
    """Delete one logical backup without exposing a manifest-less v1 database."""

    if not is_supported_backup_name(database_path.name):
        raise BackupManifestError("backup filename is invalid")
    manifest_path = manifest_path_for(database_path)
    try:
        # Removing the DB first leaves, at worst, an inert manifest.  The inverse
        # could turn a v1 backup into a manifest-less apparent legacy candidate.
        database_path.unlink()
        _fsync_directory(database_path.parent)
        try:
            manifest_path.unlink()
        except FileNotFoundError:
            if is_manifest_backup_name(database_path.name):
                raise BackupManifestPersistenceError(
                    "manifest-backed backup pair was incomplete"
                )
        _fsync_directory(database_path.parent)
    except BackupManifestError:
        raise
    except OSError as exc:
        raise BackupManifestPersistenceError("backup pair could not be deleted") from exc
