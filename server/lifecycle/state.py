from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import threading
from typing import Any, Callable, Iterator, Mapping
import uuid


# Missing/legacy authority remains version 1. Version 2 is never selected by an
# ordinary claim: provisioning/adoption must qualify and migrate it separately.
SCHEMA_VERSION = 1
MANAGEMENT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, MANAGEMENT_SCHEMA_VERSION})
MAX_STATE_BYTES = 16 * 1024
MAX_LEASE_BYTES = 8 * 1024
CAPABILITY_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
CAPABILITY_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class OperationType(str, Enum):
    UPDATE = "update"
    RESTORE = "restore"
    MANAGEMENT = "management"


class LifecyclePhase(str, Enum):
    RESERVED = "reserved"
    EXCLUSIVE = "exclusive"
    VERIFYING = "verifying"
    RECOVERY_REQUIRED = "recovery_required"


class OwnerKind(str, Enum):
    UPDATER = "updater"
    RESTORE_CONTROLLER = "restore_controller"
    MANAGEMENT_CONTROLLER = "management_controller"


OPERATION_OWNERS: Mapping[OperationType, OwnerKind] = {
    OperationType.UPDATE: OwnerKind.UPDATER,
    OperationType.RESTORE: OwnerKind.RESTORE_CONTROLLER,
    OperationType.MANAGEMENT: OwnerKind.MANAGEMENT_CONTROLLER,
}


ALLOWED_TRANSITIONS: Mapping[LifecyclePhase, frozenset[LifecyclePhase]] = {
    LifecyclePhase.RESERVED: frozenset(
        {LifecyclePhase.EXCLUSIVE, LifecyclePhase.RECOVERY_REQUIRED}
    ),
    LifecyclePhase.EXCLUSIVE: frozenset(
        {LifecyclePhase.VERIFYING, LifecyclePhase.RECOVERY_REQUIRED}
    ),
    LifecyclePhase.VERIFYING: frozenset({LifecyclePhase.RECOVERY_REQUIRED}),
    # The transition remains a low-level primitive for future explicit operator
    # recovery and rollback compatibility. Automatic restore/update launchers
    # deliberately refuse recovery_required.
    LifecyclePhase.RECOVERY_REQUIRED: frozenset({LifecyclePhase.EXCLUSIVE}),
}


class LifecycleError(RuntimeError):
    code = "lifecycle_error"


class InvalidRootError(LifecycleError):
    code = "invalid_root"


class CorruptStateError(LifecycleError):
    code = "corrupt_state"


class UnsupportedSchemaError(CorruptStateError):
    code = "unsupported_schema"


class LifecycleConflictError(LifecycleError):
    code = "conflict"


class OwnershipError(LifecycleError):
    code = "ownership_mismatch"


class PersistenceError(LifecycleError):
    code = "persistence_error"


class ControllerLockError(LifecycleConflictError):
    code = "controller_conflict"


class UnsupportedFilesystemError(PersistenceError):
    code = "unsupported_filesystem"


@dataclass(frozen=True)
class ActiveOperation:
    operation_id: str
    operation_type: OperationType
    phase: LifecyclePhase
    owner_kind: OwnerKind
    owner_nonce_hash: str


@dataclass(frozen=True)
class LifecycleState:
    schema_version: int
    generation: int
    active_operation: ActiveOperation | None


@dataclass(frozen=True)
class ClaimResult:
    state: LifecycleState
    owner_capability: str = field(repr=False)


@dataclass(frozen=True)
class CompletionResult:
    state: LifecycleState
    credential_cleanup_error: str | None = None


@dataclass(frozen=True)
class OwnershipTransferResult:
    state: LifecycleState
    owner_capability: str = field(repr=False)
    credential_cleanup_error: str | None = None


@dataclass(frozen=True)
class LifecyclePaths:
    root: Path
    run_dir: Path
    lifecycle_dir: Path
    state_path: Path
    state_lock_path: Path
    controller_lock_path: Path
    lease_path: Path
    credentials_dir: Path

    def credential_path(self, operation_id: str) -> Path:
        return self.credentials_dir / f"{_validate_operation_id(operation_id)}.cap"

    def generation_credential_path(self, operation_id: str, generation: int) -> Path:
        canonical_id = _validate_operation_id(operation_id)
        canonical_generation = _validate_generation(generation, positive=True)
        return self.credentials_dir / f"{canonical_id}.{canonical_generation}.cap"


@dataclass(frozen=True)
class ControllerLease:
    schema_version: int
    operation_id: str
    generation: int
    owner_kind: OwnerKind
    pid: int
    process_start_identity: str


_UNSUPPORTED_FILESYSTEM_TYPES = frozenset(
    {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "coda",
        "gfs",
        "gfs2",
        "glusterfs",
        "gpfs",
        "lustre",
        "ncpfs",
        "nfs",
        "nfs4",
        "ocfs2",
        "orangefs",
        "pvfs2",
        "smb2",
        "smb3",
        "virtiofs",
    }
)
_QUALIFIED_FILESYSTEMS: set[tuple[Any, ...]] = set()
_QUALIFICATION_LOCK = threading.Lock()
def _enum_value(enum_type: type[Enum], value: Enum | str, field: str) -> Any:
    raw = value.value if isinstance(value, Enum) else value
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise ValueError(f"unsupported {field}") from exc


def _validate_operation_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("operation_id must be a string")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("operation_id must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("operation_id must be a canonical UUID")
    return canonical


def _validate_generation(value: int, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("generation must be an integer")
    if value < (1 if positive else 0):
        raise ValueError("generation is outside the supported range")
    return value


def _validate_capability(value: str) -> str:
    if not isinstance(value, str) or not CAPABILITY_RE.fullmatch(value):
        raise ValueError("owner capability has an invalid format")
    return value


def hash_owner_capability(capability: str) -> str:
    validated = _validate_capability(capability)
    return f"sha256:{hashlib.sha256(validated.encode('ascii')).hexdigest()}"


def new_owner_capability() -> str:
    return secrets.token_urlsafe(32)


def _current_uid() -> int | None:
    return os.geteuid() if hasattr(os, "geteuid") else None


def _assert_owned(st: os.stat_result, description: str) -> None:
    current_uid = _current_uid()
    if current_uid is not None and st.st_uid != current_uid:
        raise InvalidRootError(f"{description} is not owned by the current installation user")


def _assert_private_mode(st: os.stat_result, description: str) -> None:
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise CorruptStateError(f"{description} permissions are not private")


def _decode_mount_field(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _filesystem_mount_identity(path: Path) -> tuple[str, ...]:
    """Return the exact current Linux mount identity owning *path*."""

    if sys.platform != "linux":
        raise UnsupportedFilesystemError("lifecycle storage requires Linux")
    try:
        resolved = path.resolve(strict=True)
        file_state = resolved.stat()
        device = f"{os.major(file_state.st_dev)}:{os.minor(file_state.st_dev)}"
        candidates: list[tuple[int, int, tuple[str, ...]]] = []
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            separator = fields.index("-")
            mount_point = Path(_decode_mount_field(fields[4]))
            try:
                resolved.relative_to(mount_point)
            except ValueError:
                continue
            if fields[2] != device:
                continue
            identity = (
                fields[0],
                fields[1],
                fields[2],
                _decode_mount_field(fields[3]),
                str(mount_point),
                fields[5],
                ",".join(fields[6:separator]),
                fields[separator + 1].lower(),
                _decode_mount_field(fields[separator + 2]),
                fields[separator + 3],
            )
            candidates.append((len(mount_point.parts), int(fields[0]), identity))
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        raise UnsupportedFilesystemError("lifecycle filesystem cannot be classified") from exc
    if not candidates:
        raise UnsupportedFilesystemError("lifecycle filesystem mount is unavailable")
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _filesystem_type(path: Path) -> str:
    """Return the Linux mount type owning *path* without mutating it."""

    identity = _filesystem_mount_identity(path)
    return identity[7]


def _probe_filesystem_primitives(directory: Path) -> None:
    token = f"{os.getpid()}.{secrets.token_hex(8)}"
    source = directory / f".streamhome-fs-probe.{token}.tmp"
    target = directory / f".streamhome-fs-probe.{token}.ready"
    lock_path = directory / f".streamhome-fs-probe.{token}.lock"
    descriptor: int | None = None
    lock_descriptor: int | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(source, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        if os.write(descriptor, b"streamhome-filesystem-probe\n") <= 0:
            raise OSError("filesystem probe write made no progress")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(source, target)
        _fsync_directory(directory)
        if target.read_bytes() != b"streamhome-filesystem-probe\n":
            raise OSError("filesystem atomic replacement was not observable")

        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,os,sys; "
                    "fd=os.open(sys.argv[1], os.O_RDWR); "
                    "\ntry:\n fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                    "except BlockingIOError:\n sys.exit(0)\n"
                    "else:\n sys.exit(1)\n"
                ),
                str(lock_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            timeout=5,
            check=False,
        )
        if probe.returncode != 0:
            raise OSError("cross-process flock exclusion is unavailable")
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnsupportedFilesystemError(
            "lifecycle filesystem does not provide required durability primitives"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
        cleanup_failed = False
        for path in (source, target, lock_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                cleanup_failed = True
        try:
            _fsync_directory(directory)
        except OSError:
            cleanup_failed = True
        if cleanup_failed and sys.exc_info()[0] is None:
            raise UnsupportedFilesystemError("filesystem probe cleanup could not be synchronized")


def qualify_local_filesystem(
    directory: str | os.PathLike[str],
    *,
    require_private: bool,
    description: str = "lifecycle directory",
) -> str:
    """Fail closed unless a directory supplies the supported local-Linux contract."""

    path = Path(directory)
    try:
        st = path.lstat()
    except OSError as exc:
        raise UnsupportedFilesystemError(f"{description} cannot be inspected") from exc
    if not stat.S_ISDIR(st.st_mode) or path.is_symlink():
        raise UnsupportedFilesystemError(f"{description} is not a trusted directory")
    _assert_owned(st, description)
    if require_private:
        _assert_private_mode(st, description)
    mount_identity = _filesystem_mount_identity(path)
    filesystem_type = mount_identity[7]
    if (
        filesystem_type in _UNSUPPORTED_FILESYSTEM_TYPES
        or filesystem_type == "fuse"
        or filesystem_type.startswith("fuse.")
    ):
        raise UnsupportedFilesystemError("network or FUSE lifecycle storage is unsupported")
    # Trust checks above always run. The active semantic probe is reusable only
    # for this exact directory inode on this exact mount/options identity.
    identity = (
        str(path.resolve(strict=True)),
        st.st_dev,
        st.st_ino,
        stat.S_IMODE(st.st_mode),
        st.st_uid,
        st.st_gid,
        filesystem_type,
        mount_identity,
    )
    with _QUALIFICATION_LOCK:
        if identity not in _QUALIFIED_FILESYSTEMS:
            _probe_filesystem_primitives(path)
            _QUALIFIED_FILESYSTEMS.add(identity)
    return filesystem_type


def qualify_lifecycle_filesystem(
    root: str | os.PathLike[str] | None = None,
) -> str:
    paths = lifecycle_paths(root)
    _ensure_lifecycle_directories(paths)
    return qualify_local_filesystem(
        paths.lifecycle_dir,
        require_private=True,
        description="lifecycle authority directory",
    )


def resolve_installation_root(root: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    if root is not None and (".." in candidate.parts or candidate.is_symlink()):
        raise InvalidRootError("installation root must be a canonical non-symlink path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise InvalidRootError("installation root does not exist") from exc
    if not resolved.is_dir():
        raise InvalidRootError("installation root is not a directory")
    _assert_owned(resolved.stat(), "installation root")
    server_dir = resolved / "server"
    start_script = resolved / "start.sh"
    update_script = resolved / "update.sh"
    if not server_dir.is_dir() or server_dir.is_symlink():
        raise InvalidRootError("installation root is missing its server directory")
    _assert_owned(server_dir.lstat(), "installation server directory")
    for marker in (start_script, update_script):
        if not marker.is_file() or marker.is_symlink():
            raise InvalidRootError("installation root is missing required lifecycle scripts")
        _assert_owned(marker.lstat(), "installation lifecycle script")
    return resolved


def lifecycle_paths(root: str | os.PathLike[str] | None = None) -> LifecyclePaths:
    resolved = resolve_installation_root(root)
    run_dir = resolved / ".run"
    lifecycle_dir = run_dir / "lifecycle"
    return LifecyclePaths(
        root=resolved,
        run_dir=run_dir,
        lifecycle_dir=lifecycle_dir,
        state_path=lifecycle_dir / "state.json",
        state_lock_path=lifecycle_dir / "state.lock",
        controller_lock_path=lifecycle_dir / "controller.lock",
        lease_path=lifecycle_dir / "lease.json",
        credentials_dir=lifecycle_dir / "credentials",
    )


def _validate_directory(path: Path, description: str) -> None:
    try:
        st = path.lstat()
    except OSError as exc:
        raise CorruptStateError(f"{description} cannot be inspected") from exc
    if not stat.S_ISDIR(st.st_mode) or path.is_symlink():
        raise CorruptStateError(f"{description} is not a trusted directory")
    _assert_owned(st, description)
    _assert_private_mode(st, description)


def _ensure_private_directory(path: Path, description: str) -> None:
    if path.exists() or path.is_symlink():
        _validate_directory(path, description)
        return
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        _fsync_directory(path.parent)
    except FileExistsError:
        _validate_directory(path, description)
    except OSError as exc:
        raise PersistenceError(f"{description} could not be created") from exc


def _ensure_lifecycle_directories(paths: LifecyclePaths, *, credentials: bool = False) -> None:
    _ensure_private_directory(paths.run_dir, "runtime directory")
    _ensure_private_directory(paths.lifecycle_dir, "lifecycle directory")
    if credentials:
        _ensure_private_directory(paths.credentials_dir, "lifecycle credential directory")


def _validate_read_path(paths: LifecyclePaths) -> None:
    if paths.run_dir.exists() or paths.run_dir.is_symlink():
        _validate_directory(paths.run_dir, "runtime directory")
    else:
        return
    if paths.lifecycle_dir.exists() or paths.lifecycle_dir.is_symlink():
        _validate_directory(paths.lifecycle_dir, "lifecycle directory")


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_locked_file(path: Path, *, nonblocking: bool = False) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode):
            raise CorruptStateError("lifecycle lock is not a regular file")
        _assert_owned(st, "lifecycle lock")
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(descriptor, operation)
        return descriptor
    except BlockingIOError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise ControllerLockError("controller ownership is already held") from exc
    except LifecycleError:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise PersistenceError("lifecycle lock could not be opened") from exc


@contextmanager
def _state_lock(paths: LifecyclePaths) -> Iterator[None]:
    _ensure_lifecycle_directories(paths)
    qualify_local_filesystem(
        paths.lifecycle_dir,
        require_private=True,
        description="lifecycle authority directory",
    )
    descriptor = _open_locked_file(paths.state_lock_path)
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _strict_keys(payload: Mapping[str, Any], expected: set[str], description: str) -> None:
    if set(payload) != expected:
        raise CorruptStateError(f"{description} contains missing or unknown fields")


def _parse_state_payload(payload: Any) -> LifecycleState:
    if not isinstance(payload, dict):
        raise CorruptStateError("lifecycle state must be an object")
    _strict_keys(payload, {"schema_version", "generation", "active_operation"}, "lifecycle state")
    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise CorruptStateError("schema_version must be an integer")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaError("lifecycle state schema is unsupported")
    try:
        generation = _validate_generation(payload["generation"])
    except ValueError as exc:
        raise CorruptStateError(str(exc)) from exc
    operation_payload = payload["active_operation"]
    if operation_payload is None:
        return LifecycleState(schema_version, generation, None)
    if not isinstance(operation_payload, dict):
        raise CorruptStateError("active_operation must be an object or null")
    _strict_keys(
        operation_payload,
        {"operation_id", "operation_type", "phase", "owner_kind", "owner_nonce_hash"},
        "active operation",
    )
    try:
        operation_id = _validate_operation_id(operation_payload["operation_id"])
        operation_type = _enum_value(OperationType, operation_payload["operation_type"], "operation_type")
        phase = _enum_value(LifecyclePhase, operation_payload["phase"], "phase")
        owner_kind = _enum_value(OwnerKind, operation_payload["owner_kind"], "owner_kind")
        _validate_schema_operation(schema_version, operation_type, owner_kind)
    except ValueError as exc:
        raise CorruptStateError(str(exc)) from exc
    nonce_hash = operation_payload["owner_nonce_hash"]
    if not isinstance(nonce_hash, str) or not CAPABILITY_HASH_RE.fullmatch(nonce_hash):
        raise CorruptStateError("owner_nonce_hash has an invalid format")
    if generation < 1:
        raise CorruptStateError("active operation requires a positive generation")
    return LifecycleState(
        schema_version,
        generation,
        ActiveOperation(operation_id, operation_type, phase, owner_kind, nonce_hash),
    )


def _validate_schema_operation(
    schema_version: int, operation_type: OperationType, owner_kind: OwnerKind
) -> None:
    if schema_version == SCHEMA_VERSION:
        if (
            operation_type is OperationType.MANAGEMENT
            or owner_kind is OwnerKind.MANAGEMENT_CONTROLLER
        ):
            raise UnsupportedSchemaError("management requires lifecycle authority version 2")
    elif OPERATION_OWNERS[operation_type] is not owner_kind:
        raise ValueError("operation type and owner kind do not match")


def _state_payload(state: LifecycleState) -> dict[str, Any]:
    operation = state.active_operation
    return {
        "schema_version": state.schema_version,
        "generation": state.generation,
        "active_operation": (
            None
            if operation is None
            else {
                "operation_id": operation.operation_id,
                "operation_type": operation.operation_type.value,
                "phase": operation.phase.value,
                "owner_kind": operation.owner_kind.value,
                "owner_nonce_hash": operation.owner_nonce_hash,
            }
        ),
    }


def _read_json_file(path: Path, maximum_bytes: int, description: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        del value
        raise ValueError("non-finite JSON number")

    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode):
            raise CorruptStateError(f"{description} is not a trusted regular file")
        _assert_owned(st, description)
        _assert_private_mode(st, description)
        if st.st_size > maximum_bytes:
            raise CorruptStateError(f"{description} exceeds its size limit")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
            descriptor = None
            return json.load(
                handle,
                object_pairs_hook=reject_duplicates,
                parse_constant=reject_constant,
            )
    except CorruptStateError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CorruptStateError(f"{description} cannot be decoded") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_state_unlocked(paths: LifecyclePaths) -> LifecycleState:
    if not paths.state_path.exists() and not paths.state_path.is_symlink():
        return LifecycleState(SCHEMA_VERSION, 0, None)
    return _parse_state_payload(_read_json_file(paths.state_path, MAX_STATE_BYTES, "lifecycle state"))


def inspect_state(root: str | os.PathLike[str] | None = None) -> LifecycleState:
    paths = lifecycle_paths(root)
    _validate_read_path(paths)
    if not paths.lifecycle_dir.exists():
        return LifecycleState(SCHEMA_VERSION, 0, None)
    if not paths.state_path.exists() and not paths.state_path.is_symlink():
        return LifecycleState(SCHEMA_VERSION, 0, None)
    return _read_state_unlocked(paths)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    except LifecycleError:
        raise
    except OSError as exc:
        raise PersistenceError("lifecycle state could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _write_state_unlocked(paths: LifecyclePaths, state: LifecycleState) -> None:
    payload = _state_payload(state)
    _parse_state_payload(payload)
    _atomic_write_json(paths.state_path, payload)


def _require_expected_operation(
    state: LifecycleState,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    owner_capability: str,
    expected_operation_type: OperationType | str | None = None,
) -> ActiveOperation:
    operation = _require_operation_identity_owner(
        state,
        operation_id=operation_id,
        generation=generation,
        owner_capability=owner_capability,
    )
    phase = _enum_value(LifecyclePhase, expected_phase, "expected_phase")
    if operation.phase is not phase:
        raise LifecycleConflictError("operation phase does not match")
    if expected_operation_type is not None:
        operation_type = _enum_value(
            OperationType,
            expected_operation_type,
            "expected_operation_type",
        )
        if operation.operation_type is not operation_type:
            raise LifecycleConflictError("operation type does not match")
    return operation


def _require_operation_identity_owner(
    state: LifecycleState,
    *,
    operation_id: str,
    generation: int,
    owner_capability: str,
) -> ActiveOperation:
    validated_id = _validate_operation_id(operation_id)
    validated_generation = _validate_generation(generation, positive=True)
    operation = state.active_operation
    if operation is None:
        raise LifecycleConflictError("no lifecycle operation is active")
    if operation.operation_id != validated_id:
        raise LifecycleConflictError("operation identity does not match")
    if state.generation != validated_generation:
        raise LifecycleConflictError("operation generation does not match")
    supplied_hash = hash_owner_capability(owner_capability)
    if not hmac.compare_digest(operation.owner_nonce_hash, supplied_hash):
        raise OwnershipError("lifecycle owner authorization failed")
    return operation


def claim_operation(
    root: str | os.PathLike[str] | None,
    *,
    operation_type: OperationType | str,
    owner_kind: OwnerKind | str,
    operation_id: str | None = None,
    owner_capability: str | None = None,
) -> ClaimResult:
    paths = lifecycle_paths(root)
    selected_id = _validate_operation_id(operation_id or str(uuid.uuid4()))
    selected_type = _enum_value(OperationType, operation_type, "operation_type")
    selected_owner = _enum_value(OwnerKind, owner_kind, "owner_kind")
    capability = _validate_capability(owner_capability or new_owner_capability())
    with _state_lock(paths):
        current = _read_state_unlocked(paths)
        if current.active_operation is not None:
            raise LifecycleConflictError("another lifecycle operation is active")
        _validate_schema_operation(current.schema_version, selected_type, selected_owner)
        next_generation = current.generation + 1
        claimed = LifecycleState(
            current.schema_version,
            next_generation,
            ActiveOperation(
                selected_id,
                selected_type,
                LifecyclePhase.RESERVED,
                selected_owner,
                hash_owner_capability(capability),
            ),
        )
        _write_state_unlocked(paths, claimed)
    return ClaimResult(claimed, capability)


def validate_operation_owner(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    owner_capability: str,
) -> LifecycleState:
    paths = lifecycle_paths(root)
    with _state_lock(paths):
        current = _read_state_unlocked(paths)
        _require_expected_operation(
            current,
            operation_id=operation_id,
            generation=generation,
            expected_phase=expected_phase,
            owner_capability=owner_capability,
        )
        return current


def validate_operation_owner_current_phase(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    owner_capability: str,
) -> LifecycleState:
    paths = lifecycle_paths(root)
    with _state_lock(paths):
        current = _read_state_unlocked(paths)
        _require_operation_identity_owner(
            current,
            operation_id=operation_id,
            generation=generation,
            owner_capability=owner_capability,
        )
        return current


@contextmanager
def hold_idle_authority(
    root: str | os.PathLike[str] | None,
) -> Iterator[LifecycleState]:
    """Hold the short-lived authority lock while a subordinate idle write commits."""

    paths = lifecycle_paths(root)
    with _state_lock(paths):
        current = _read_state_unlocked(paths)
        if current.active_operation is not None:
            raise LifecycleConflictError("another lifecycle operation is active")
        yield current


@contextmanager
def hold_operation_with_credential(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    expected_operation_type: OperationType | str | None = None,
) -> Iterator[LifecycleState]:
    """Hold exact authority while one bounded subordinate atomic write commits."""

    paths = lifecycle_paths(root)
    capability = _read_credential(paths, operation_id, generation)
    with _state_lock(paths):
        current = _read_state_unlocked(paths)
        _require_expected_operation(
            current,
            operation_id=operation_id,
            generation=generation,
            expected_phase=expected_phase,
            owner_capability=capability,
            expected_operation_type=expected_operation_type,
        )
        yield current


def transition_operation(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    next_phase: LifecyclePhase | str,
    owner_capability: str,
) -> LifecycleState:
    paths = lifecycle_paths(root)
    next_value = _enum_value(LifecyclePhase, next_phase, "next_phase")
    with _state_lock(paths):
        current = _read_state_unlocked(paths)
        operation = _require_expected_operation(
            current,
            operation_id=operation_id,
            generation=generation,
            expected_phase=expected_phase,
            owner_capability=owner_capability,
        )
        if next_value not in ALLOWED_TRANSITIONS[operation.phase]:
            raise LifecycleConflictError("lifecycle phase transition is not allowed")
        updated = LifecycleState(
            current.schema_version,
            current.generation,
            ActiveOperation(
                operation.operation_id,
                operation.operation_type,
                next_value,
                operation.owner_kind,
                operation.owner_nonce_hash,
            ),
        )
        _write_state_unlocked(paths, updated)
        return updated


def transfer_operation_owner(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    owner_capability: str,
    next_owner_kind: OwnerKind | str,
    next_owner_capability: str,
) -> LifecycleState:
    paths = lifecycle_paths(root)
    next_owner = _enum_value(OwnerKind, next_owner_kind, "next_owner_kind")
    next_capability = _validate_capability(next_owner_capability)
    with _state_lock(paths):
        current = _read_state_unlocked(paths)
        operation = _require_expected_operation(
            current,
            operation_id=operation_id,
            generation=generation,
            expected_phase=expected_phase,
            owner_capability=owner_capability,
        )
        _validate_schema_operation(current.schema_version, operation.operation_type, next_owner)
        updated = LifecycleState(
            current.schema_version,
            current.generation + 1,
            ActiveOperation(
                operation.operation_id,
                operation.operation_type,
                operation.phase,
                next_owner,
                hash_owner_capability(next_capability),
            ),
        )
        _write_state_unlocked(paths, updated)
        return updated


def _transfer_operation_owner_with_credential(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    owner_capability: str,
    next_owner_kind: OwnerKind | str,
) -> OwnershipTransferResult:
    """Fence an owner transfer with a fresh generation-scoped capability.

    The new credential is durable before the authority references it. If state
    persistence reports an indeterminate post-replace failure, the authoritative
    state is re-read and accepted only when it exactly names the new capability.
    The caller must hold controller.lock for the complete call.
    """

    paths = lifecycle_paths(root)
    next_owner = _enum_value(OwnerKind, next_owner_kind, "next_owner_kind")
    validated_generation = _validate_generation(generation, positive=True)
    next_generation = validated_generation + 1
    next_capability = new_owner_capability()
    next_path = paths.generation_credential_path(operation_id, next_generation)
    previous_path = _credential_path_for_generation(paths, operation_id, validated_generation)
    validate_operation_owner(
        paths.root,
        operation_id=operation_id,
        generation=validated_generation,
        expected_phase=expected_phase,
        owner_capability=owner_capability,
    )
    if next_path.exists() or next_path.is_symlink():
        # controller.lock proves no competing transfer can own this path. With
        # authority still at the old generation, it is an orphan from a crash
        # before the prior transfer's state CAS.
        _delete_credential_path(paths, next_path)
    _write_credential_path(paths, next_path, next_capability)
    try:
        try:
            updated = transfer_operation_owner(
                paths.root,
                operation_id=operation_id,
                generation=validated_generation,
                expected_phase=expected_phase,
                owner_capability=owner_capability,
                next_owner_kind=next_owner,
                next_owner_capability=next_capability,
            )
        except LifecycleError:
            current = inspect_state(paths.root)
            operation = current.active_operation
            if not (
                operation is not None
                and operation.operation_id == operation_id
                and current.generation == next_generation
                and operation.owner_kind is next_owner
                and hmac.compare_digest(
                    operation.owner_nonce_hash,
                    hash_owner_capability(next_capability),
                )
            ):
                raise
            updated = current
    except Exception:
        try:
            _delete_credential_path(paths, next_path)
        except LifecycleError:
            pass
        raise

    cleanup_error: str | None = None
    if previous_path != next_path:
        try:
            _delete_credential_path(paths, previous_path)
        except LifecycleError as exc:
            cleanup_error = exc.code
    return OwnershipTransferResult(updated, next_capability, cleanup_error)


def complete_operation(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    owner_capability: str,
    expected_operation_type: OperationType | str | None = None,
) -> LifecycleState:
    paths = lifecycle_paths(root)
    with _state_lock(paths):
        current = _read_state_unlocked(paths)
        _require_expected_operation(
            current,
            operation_id=operation_id,
            generation=generation,
            expected_phase=expected_phase,
            owner_capability=owner_capability,
            expected_operation_type=expected_operation_type,
        )
        completed = LifecycleState(current.schema_version, current.generation, None)
        _write_state_unlocked(paths, completed)
        return completed


def _write_credential_path(paths: LifecyclePaths, path: Path, capability: str) -> Path:
    _ensure_lifecycle_directories(paths, credentials=True)
    qualify_local_filesystem(
        paths.lifecycle_dir,
        require_private=True,
        description="lifecycle authority directory",
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        try:
            os.fchmod(descriptor, 0o600)
            remaining = memoryview((capability + "\n").encode("ascii"))
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("credential write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(paths.credentials_dir)
        return path
    except FileExistsError as exc:
        raise LifecycleConflictError("operation credential already exists") from exc
    except OSError as exc:
        if created:
            try:
                path.unlink()
                _fsync_directory(paths.credentials_dir)
            except OSError:
                pass
        raise PersistenceError("operation credential could not be persisted") from exc


def _write_credential(paths: LifecyclePaths, operation_id: str, capability: str) -> Path:
    return _write_credential_path(paths, paths.credential_path(operation_id), capability)


def _credential_path_for_generation(
    paths: LifecyclePaths,
    operation_id: str,
    generation: int | None,
) -> Path:
    if generation is not None:
        generation_path = paths.generation_credential_path(operation_id, generation)
        if generation_path.exists() or generation_path.is_symlink():
            return generation_path
    return paths.credential_path(operation_id)


def _read_credential(
    paths: LifecyclePaths,
    operation_id: str,
    generation: int | None = None,
) -> str:
    _validate_read_path(paths)
    _validate_directory(paths.credentials_dir, "lifecycle credential directory")
    path = _credential_path_for_generation(paths, operation_id, generation)
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode):
            raise OwnershipError("operation credential is not a trusted file")
        _assert_owned(st, "operation credential")
        _assert_private_mode(st, "operation credential")
        if st.st_size > 256:
            raise OwnershipError("operation credential has an invalid format")
        capability = os.read(descriptor, 257).decode("ascii").strip()
        return _validate_capability(capability)
    except OwnershipError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise OwnershipError("operation credential is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _delete_credential_path(paths: LifecyclePaths, path: Path) -> None:
    _validate_read_path(paths)
    _validate_directory(paths.credentials_dir, "lifecycle credential directory")
    try:
        path.unlink()
        _fsync_directory(paths.credentials_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PersistenceError("operation credential could not be removed") from exc


def _delete_credential(
    paths: LifecyclePaths,
    operation_id: str,
    generation: int | None = None,
) -> None:
    _delete_credential_path(
        paths,
        _credential_path_for_generation(paths, operation_id, generation),
    )


def claim_operation_with_credential(
    root: str | os.PathLike[str] | None,
    *,
    operation_type: OperationType | str,
    owner_kind: OwnerKind | str,
    operation_id: str | None = None,
) -> ClaimResult:
    paths = lifecycle_paths(root)
    selected_id = _validate_operation_id(operation_id or str(uuid.uuid4()))
    capability = new_owner_capability()
    _write_credential(paths, selected_id, capability)
    try:
        return claim_operation(
            paths.root,
            operation_type=operation_type,
            owner_kind=owner_kind,
            operation_id=selected_id,
            owner_capability=capability,
        )
    except Exception:
        keep_for_recovery = False
        try:
            current = inspect_state(paths.root)
            operation = current.active_operation
            keep_for_recovery = bool(
                operation is not None
                and operation.operation_id == selected_id
                and hmac.compare_digest(
                    operation.owner_nonce_hash,
                    hash_owner_capability(capability),
                )
            )
        except LifecycleError:
            keep_for_recovery = True
        if not keep_for_recovery:
            try:
                _delete_credential(paths, selected_id)
            except LifecycleError:
                pass
        raise


def validate_operation_with_credential(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str | None = None,
) -> LifecycleState:
    paths = lifecycle_paths(root)
    capability = _read_credential(paths, operation_id, generation)
    if expected_phase is None:
        return validate_operation_owner_current_phase(
            paths.root,
            operation_id=operation_id,
            generation=generation,
            owner_capability=capability,
        )
    return validate_operation_owner(
        paths.root,
        operation_id=operation_id,
        generation=generation,
        expected_phase=expected_phase,
        owner_capability=capability,
    )


def load_controller_capability(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
) -> str:
    """Load the fixed-path owner credential for an offline lifecycle controller.

    The capability is deliberately unavailable through the lifecycle CLI or public
    projections.  This narrow API exists so a replacement controller can resume an
    orphaned operation without placing the secret in argv or the environment.
    """

    paths = lifecycle_paths(root)
    capability = _read_credential(paths, operation_id, generation)
    validate_operation_owner_current_phase(
        paths.root,
        operation_id=operation_id,
        generation=generation,
        owner_capability=capability,
    )
    return capability


def transition_operation_with_credential(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    next_phase: LifecyclePhase | str,
) -> LifecycleState:
    paths = lifecycle_paths(root)
    capability = _read_credential(paths, operation_id, generation)
    return transition_operation(
        paths.root,
        operation_id=operation_id,
        generation=generation,
        expected_phase=expected_phase,
        next_phase=next_phase,
        owner_capability=capability,
    )


def complete_operation_with_credential(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    expected_operation_type: OperationType | str | None = None,
) -> CompletionResult:
    paths = lifecycle_paths(root)
    capability = _read_credential(paths, operation_id, generation)
    completed = complete_operation(
        paths.root,
        operation_id=operation_id,
        generation=generation,
        expected_phase=expected_phase,
        owner_capability=capability,
        expected_operation_type=expected_operation_type,
    )
    try:
        _delete_credential(paths, operation_id, generation)
    except LifecycleError as exc:
        return CompletionResult(completed, exc.code)
    return CompletionResult(completed)


def public_projection(state: LifecycleState) -> dict[str, Any]:
    operation = state.active_operation
    return {
        "schema_version": state.schema_version,
        "generation": state.generation,
        "active_operation": (
            None
            if operation is None
            else {
                "operation_id": operation.operation_id,
                "operation_type": operation.operation_type.value,
                "phase": operation.phase.value,
                "owner_kind": operation.owner_kind.value,
            }
        ),
    }


def startup_decision(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    state = inspect_state(root)
    if state.active_operation is None:
        return {"decision": "normal", "generation": state.generation}
    projection = public_projection(state)
    return {
        "decision": "blocked",
        "generation": state.generation,
        "active_operation": projection["active_operation"],
    }


def _process_start_identity(pid: int) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        identity = line[line.rfind(")") + 2 :].split()[19]
    except (IndexError, OSError, ValueError) as exc:
        raise LifecycleConflictError("process start identity is unavailable") from exc
    if not identity.isdigit():
        raise LifecycleConflictError("process start identity is invalid")
    return identity


def _lease_payload(lease: ControllerLease) -> dict[str, Any]:
    return {
        "schema_version": lease.schema_version,
        "operation_id": lease.operation_id,
        "generation": lease.generation,
        "owner_kind": lease.owner_kind.value,
        "pid": lease.pid,
        "process_start_identity": lease.process_start_identity,
    }


def _parse_lease_payload(payload: Any) -> ControllerLease:
    if not isinstance(payload, dict):
        raise CorruptStateError("controller lease must be an object")
    _strict_keys(
        payload,
        {"schema_version", "operation_id", "generation", "owner_kind", "pid", "process_start_identity"},
        "controller lease",
    )
    if (
        isinstance(payload["schema_version"], bool)
        or not isinstance(payload["schema_version"], int)
        or payload["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise UnsupportedSchemaError("controller lease schema is unsupported")
    try:
        operation_id = _validate_operation_id(payload["operation_id"])
        generation = _validate_generation(payload["generation"], positive=True)
        owner_kind = _enum_value(OwnerKind, payload["owner_kind"], "owner_kind")
    except ValueError as exc:
        raise CorruptStateError(str(exc)) from exc
    if payload["schema_version"] == SCHEMA_VERSION and owner_kind is OwnerKind.MANAGEMENT_CONTROLLER:
        raise UnsupportedSchemaError("management requires controller lease version 2")
    pid = payload["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise CorruptStateError("controller lease pid is invalid")
    identity = payload["process_start_identity"]
    if not isinstance(identity, str) or not identity.isdigit():
        raise CorruptStateError("controller lease process identity is invalid")
    return ControllerLease(payload["schema_version"], operation_id, generation, owner_kind, pid, identity)


def read_controller_lease(root: str | os.PathLike[str] | None = None) -> ControllerLease | None:
    paths = lifecycle_paths(root)
    _validate_read_path(paths)
    if not paths.lease_path.exists() and not paths.lease_path.is_symlink():
        return None
    return _parse_lease_payload(_read_json_file(paths.lease_path, MAX_LEASE_BYTES, "controller lease"))


def controller_lease_is_current(lease: ControllerLease) -> bool:
    try:
        os.kill(lease.pid, 0)
        return hmac.compare_digest(_process_start_identity(lease.pid), lease.process_start_identity)
    except (OSError, LifecycleError):
        return False


class ControllerLeaseHandle:
    def __init__(self, paths: LifecyclePaths, descriptor: int, lease: ControllerLease) -> None:
        self.paths = paths
        self.descriptor = descriptor
        self.lease = lease
        self._released = False

    def require_current(self) -> None:
        """Require this live process's exact persisted controller ownership."""
        if (
            self._released
            or self.lease.pid != os.getpid()
            or self.lease.process_start_identity != _process_start_identity(os.getpid())
            or read_controller_lease(self.paths.root) != self.lease
        ):
            raise OwnershipError("controller lease is no longer owned by this process")

    def release(self) -> None:
        if self._released:
            return
        try:
            current = read_controller_lease(self.paths.root)
            if current == self.lease:
                try:
                    self.paths.lease_path.unlink()
                    _fsync_directory(self.paths.lifecycle_dir)
                except FileNotFoundError:
                    pass
        finally:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self.descriptor)
                self._released = True

    def __enter__(self) -> ControllerLeaseHandle:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def acquire_controller_lease(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    owner_capability: str,
) -> ControllerLeaseHandle:
    paths = lifecycle_paths(root)
    _ensure_lifecycle_directories(paths)
    qualify_local_filesystem(
        paths.lifecycle_dir,
        require_private=True,
        description="lifecycle authority directory",
    )
    # Installation lock order is controller.lock -> state.lock. Never reverse it.
    descriptor = _open_locked_file(paths.controller_lock_path, nonblocking=True)
    try:
        state = validate_operation_owner(
            paths.root,
            operation_id=operation_id,
            generation=generation,
            expected_phase=expected_phase,
            owner_capability=owner_capability,
        )
        operation = state.active_operation
        if operation is None:  # pragma: no cover - protected by validate_operation_owner
            raise LifecycleConflictError("no lifecycle operation is active")
        lease = ControllerLease(
            state.schema_version,
            operation.operation_id,
            state.generation,
            operation.owner_kind,
            os.getpid(),
            _process_start_identity(os.getpid()),
        )
        _atomic_write_json(paths.lease_path, _lease_payload(lease))
        return ControllerLeaseHandle(paths, descriptor, lease)
    except Exception:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        raise


def acquire_transferred_controller_lease(
    root: str | os.PathLike[str] | None,
    *,
    operation_id: str,
    generation: int,
    expected_phase: LifecyclePhase | str,
    owner_capability: str,
    next_owner_kind: OwnerKind | str,
    pre_transfer_check: Callable[[], None] | None = None,
) -> tuple[ControllerLeaseHandle, OwnershipTransferResult]:
    """Acquire process ownership and durably fence the prior lifecycle owner."""

    paths = lifecycle_paths(root)
    _ensure_lifecycle_directories(paths)
    qualify_local_filesystem(
        paths.lifecycle_dir,
        require_private=True,
        description="lifecycle authority directory",
    )
    descriptor = _open_locked_file(paths.controller_lock_path, nonblocking=True)
    try:
        if pre_transfer_check is not None:
            pre_transfer_check()
        transferred = _transfer_operation_owner_with_credential(
            paths.root,
            operation_id=operation_id,
            generation=generation,
            expected_phase=expected_phase,
            owner_capability=owner_capability,
            next_owner_kind=next_owner_kind,
        )
        operation = transferred.state.active_operation
        if operation is None:  # pragma: no cover - transfer guarantees an operation
            raise LifecycleConflictError("no lifecycle operation is active")
        lease = ControllerLease(
            transferred.state.schema_version,
            operation.operation_id,
            transferred.state.generation,
            operation.owner_kind,
            os.getpid(),
            _process_start_identity(os.getpid()),
        )
        _atomic_write_json(paths.lease_path, _lease_payload(lease))
        return ControllerLeaseHandle(paths, descriptor, lease), transferred
    except Exception:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        raise
