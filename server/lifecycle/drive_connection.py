"""Recoverable first-connection publication, without application imports or secrets in receipts."""
from __future__ import annotations

from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any
import uuid

from lifecycle.state import qualify_local_filesystem

MAX_FILE_BYTES = 4 * 1024 * 1024
JOURNAL_NAME = ".admin-drive-publication.json"
CONFIG_HEADER = b"RCLONE_ENCRYPT_V0:"
REMOTE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class DriveConnectionPublicationError(RuntimeError):
    def __init__(self, code: str = "drive_publication_recovery_required"):
        self.code = code
        super().__init__(code)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_encrypted_config(data: bytes | None) -> bool:
    """Recognize Rclone's encryption marker after leading comments and blanks."""
    if data is None:
        return False
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith((b"#", b";")):
            continue
        return line == CONFIG_HEADER
    return False


def _read(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DriveConnectionPublicationError() from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
            raise DriveConnectionPublicationError()
        chunks = []
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_FILE_BYTES:
            raise DriveConnectionPublicationError()
        return data
    finally:
        os.close(fd)


def _sync(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    # Refuse special/hardlinked targets before replacing any installation file.
    _read(path)
    temporary = path.with_name(f".drive-write-{secrets.token_hex(12)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("configuration write made no progress")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    try:
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _json(data: bytes) -> dict[str, Any]:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError) as exc:
        raise DriveConnectionPublicationError() from exc


def _bytes(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _paths(root: Path, *, create: bool = False) -> tuple[Path, Path, Path]:
    root = Path(root).resolve(strict=True)
    server = root / "server"
    directory = server / "rclone"
    for path in (server, directory):
        if create and not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700)
            _sync(path.parent)
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise DriveConnectionPublicationError()
    return directory, directory / "rclone.conf", server / "settings.json"


@contextmanager
def _lock(directory: Path):
    qualify_local_filesystem(directory, require_private=True, description="Drive configuration directory")
    fd = os.open(directory / ".admin-drive.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DriveConnectionPublicationError()
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _identity(job_id: str, generation: int, remote_name: str, remote_path: str,
              audience: str, publishing_status: str) -> dict:
    try:
        valid_id = str(uuid.UUID(job_id)) == job_id
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    relative = (remote_path.removeprefix(remote_name + ":")
                if isinstance(remote_path, str) and isinstance(remote_name, str) else "")
    if (not valid_id or type(generation) is not int or generation < 1
            or not isinstance(remote_name, str) or not isinstance(remote_path, str)
            or not REMOTE_RE.fullmatch(remote_name) or not remote_path.startswith(remote_name + ":")
            or not relative or len(relative) > 512 or ":" in relative or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or any(ord(char) < 32 for char in relative)
            or not isinstance(audience, str) or not isinstance(publishing_status, str)
            or audience not in {"external", "internal"} or publishing_status not in {"testing", "production"}):
        raise DriveConnectionPublicationError("drive_publication_invalid")
    return {"jobId": job_id, "generation": generation, "remoteName": remote_name,
            "remotePath": remote_path, "audience": audience, "publishingStatus": publishing_status}


def _record(directory: Path) -> dict | None:
    data = _read(directory / JOURNAL_NAME)
    if data is None:
        return None
    record = _json(data)
    if set(record) != {"version", "phase", "identity", "previousSettings", "nextSettings", "configHash"}:
        raise DriveConnectionPublicationError()
    if record["version"] != 1 or record["phase"] not in {"prepared", "committed"}:
        raise DriveConnectionPublicationError()
    identity = record["identity"]
    if not isinstance(identity, dict) or set(identity) != {"jobId", "generation", "remoteName", "remotePath", "audience", "publishingStatus"}:
        raise DriveConnectionPublicationError()
    _identity(identity["jobId"], identity["generation"], identity["remoteName"], identity["remotePath"], identity["audience"], identity["publishingStatus"])
    for key in ("nextSettings", "configHash"):
        if not isinstance(record[key], str) or not re.fullmatch(r"[a-f0-9]{64}", record[key]):
            raise DriveConnectionPublicationError()
    if record["previousSettings"] is not None:
        try:
            original = base64.b64decode(record["previousSettings"], validate=True)
        except (ValueError, TypeError) as exc:
            raise DriveConnectionPublicationError() from exc
        _json(original)
    return record


def _receipt(record: dict) -> dict:
    identity = record["identity"]
    return {"jobId": identity["jobId"], "generation": identity["generation"],
            "remotePath": identity["remotePath"], "status": "saved"}


def _verify_committed(record: dict, config: Path, settings: Path) -> dict:
    current = _read(config)
    saved = _read(settings)
    if not _is_encrypted_config(current) or saved is None:
        raise DriveConnectionPublicationError()
    payload = _json(saved)
    identity = record["identity"]
    expected = {"rclone_remote_path": identity["remotePath"], "google_drive_audience": identity["audience"],
                "google_drive_publishing_status": identity["publishingStatus"]}
    if any(payload.get(key) != value for key, value in expected.items()):
        raise DriveConnectionPublicationError()
    # Rclone may legitimately rotate encrypted token bytes, and other settings may
    # be edited after acceptance. The receipt binds the published connection, not
    # the future byte identity of every mutable configuration field.
    return _receipt(record)


def _durable_receipt(record: dict, config: Path, settings: Path) -> dict:
    # A namespace-visible acceptance marker can precede a failed directory fsync.
    # Every acknowledgement path, including GET reconciliation, must establish
    # durability before using that marker as proof of a successful save.
    try:
        _sync(config.parent)
        _sync(settings.parent)
    except OSError as exc:
        raise DriveConnectionPublicationError() from exc
    return _verify_committed(record, config, settings)


def _recover(directory: Path, config: Path, settings: Path) -> dict | None:
    record = _record(directory)
    if record is None:
        return None
    if record["phase"] == "committed":
        # A caller may be recovering from an error after replace but before the
        # directory synchronization completed. Re-establish durability before
        # returning an accepted result; persistent sync errors remain fail closed.
        return _durable_receipt(record, config, settings)
    current_config = _read(config)
    current_settings = _read(settings)
    previous = (base64.b64decode(record["previousSettings"], validate=True)
                if record["previousSettings"] is not None else None)
    # Inspect BOTH targets before the first repair; unrelated or ambiguous writes
    # retain all evidence instead of being overwritten during automatic startup.
    if current_config is not None and _digest(current_config) != record["configHash"]:
        raise DriveConnectionPublicationError()
    allowed = {record["nextSettings"], _digest(previous) if previous is not None else None}
    if (_digest(current_settings) if current_settings is not None else None) not in allowed:
        raise DriveConnectionPublicationError()
    if previous is None:
        settings.unlink(missing_ok=True)
        _sync(settings.parent)
    elif current_settings != previous:
        _atomic_write(settings, previous)
    if current_config is not None:
        config.unlink()
        _sync(directory)
    (directory / JOURNAL_NAME).unlink()
    _sync(directory)
    return None


def recover_drive_connection(root: Path) -> dict | None:
    """Startup-only recovery under runtime admission, never a forced overwrite."""
    directory, config, settings = _paths(root)
    if not directory.exists() or (not (directory / JOURNAL_NAME).exists()
                                  and not (directory / JOURNAL_NAME).is_symlink()):
        return None
    with _lock(directory):
        return _recover(directory, config, settings)


def get_drive_connection_receipt(root: Path, job_id: str, generation: int) -> dict | None:
    """Inspect acceptance without creating directories, locks or configuration."""
    directory, config, settings = _paths(root)
    record = _record(directory)
    if record is None:
        return None
    identity = record["identity"]
    if identity["jobId"] != job_id or identity["generation"] != generation:
        return None
    if record["phase"] != "committed":
        raise DriveConnectionPublicationError("drive_publication_pending")
    return _durable_receipt(record, config, settings)


def publish_drive_connection(*, root: Path, job_id: str, generation: int,
                             encrypted_config: Path, remote_name: str, remote_path: str,
                             audience: str, publishing_status: str) -> dict:
    """Durably publish a first connection, preserving storage/compression policy.

    Caller owns the async configuration lock and lifecycle work lease. Encryption
    and provider verification happen before entry; no external I/O occurs here.
    """
    identity = _identity(job_id, generation, remote_name, remote_path, audience, publishing_status)
    directory, config, settings = _paths(root, create=True)
    os.chmod(directory, 0o700)
    qualify_local_filesystem(settings.parent, require_private=False, description="server configuration directory")
    with _lock(directory):
        accepted = _recover(directory, config, settings)
        if accepted:
            if accepted["jobId"] == job_id and accepted["generation"] == generation:
                return accepted
            raise DriveConnectionPublicationError("drive_already_configured")
        if _read(config) is not None:
            raise DriveConnectionPublicationError("drive_already_configured")
        if encrypted_config.parent.resolve() != directory.resolve() or encrypted_config.name in {config.name, JOURNAL_NAME}:
            raise DriveConnectionPublicationError("drive_publication_invalid")
        encrypted = _read(encrypted_config)
        if not _is_encrypted_config(encrypted):
            raise DriveConnectionPublicationError("drive_encryption_required")
        original = _read(settings)
        payload = _json(original) if original is not None else {}
        payload.update(rclone_remote_path=remote_path, google_drive_audience=audience,
                       google_drive_publishing_status=publishing_status)
        updated = _bytes(payload)
        if len(updated) > MAX_FILE_BYTES:
            raise DriveConnectionPublicationError("drive_publication_invalid")
        record = {"version": 1, "phase": "prepared", "identity": identity,
                  "previousSettings": base64.b64encode(original).decode("ascii") if original is not None else None,
                  "nextSettings": _digest(updated), "configHash": _digest(encrypted)}
        if len(_bytes(record)) > MAX_FILE_BYTES:
            raise DriveConnectionPublicationError("drive_publication_invalid")
        _atomic_write(directory / JOURNAL_NAME, _bytes(record))
        try:
            _atomic_write(config, encrypted)
            _atomic_write(settings, updated)
            record["phase"] = "committed"
            _atomic_write(directory / JOURNAL_NAME, _bytes(record))
            return _verify_committed(record, config, settings)
        except BaseException as exc:
            # An exception after the durable acceptance write is a lost response,
            # not permission to roll back an already accepted connection.
            try:
                accepted = _recover(directory, config, settings)
            except Exception as recovery_error:
                raise DriveConnectionPublicationError() from recovery_error
            if accepted:
                return accepted
            raise DriveConnectionPublicationError("drive_publication_failed") from exc
