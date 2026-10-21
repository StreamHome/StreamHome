"""Managed first-run authority: durable finalization and ephemeral one-use codes.

No code is issued at import/startup. A protected local command explicitly asks
the running authority to issue/retrieve/renew. Only a verifier remains in memory;
retrievable code material is private and bound to this supervisor incarnation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Callable
import uuid

from . import state


CODE_TTL_SECONDS = 15 * 60
_RAW_NAME = "bootstrap-code.json"
_STATE_NAME = "setup-finalization.json"


class SetupAuthorityError(RuntimeError):
    code = "setup_authority_unavailable"


class SetupAlreadyFinalized(SetupAuthorityError):
    code = "setup_complete"


def _uuid(value: Any) -> str:
    try:
        identity = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError as exc:
        raise SetupAuthorityError("The setup authority identity is invalid.") from exc
    if identity is None or not identity.int or str(identity) != value:
        raise SetupAuthorityError("The setup authority identity is invalid.")
    return value


def _read(path: Path) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SetupAuthorityError("Duplicate setup authority evidence.")
            result[key] = value
        return result

    def reject(value):
        raise SetupAuthorityError("Invalid setup authority number.")

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_mode & 0o7177 or before.st_nlink != 1 or before.st_size > 4096:
                raise SetupAuthorityError("Setup authority evidence is not a private regular file.")
            raw = handle.read(4097)
            after, linked = os.fstat(handle.fileno()), path.lstat()
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mode, item.st_uid, item.st_nlink, item.st_mtime_ns, item.st_ctime_ns)
            if len(raw) > 4096 or identity(before) != identity(after) or identity(before) != identity(linked):
                raise SetupAuthorityError("Setup authority evidence changed during inspection.")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
        if not isinstance(payload, dict):
            raise SetupAuthorityError("Setup authority evidence is invalid.")
        return payload
    except FileNotFoundError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise SetupAuthorityError("Setup authority evidence is unavailable.") from exc


def read_finalization(directory: Path, installation_id: str) -> dict[str, Any]:
    state._validate_directory(directory, "setup state directory")
    try:
        payload = _read(directory / _STATE_NAME)
    except FileNotFoundError as exc:
        raise SetupAuthorityError("First-run eligibility is missing; explicit recovery is required.") from exc
    if (
        set(payload) != {"schema_version", "installation_id", "state", "finalization_id"}
        or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
        or payload["installation_id"] != _uuid(installation_id)
        or payload["state"] not in ("eligible", "finalized")
    ):
        raise SetupAuthorityError("First-run eligibility does not match this installation.")
    if payload["state"] == "finalized":
        _uuid(payload["finalization_id"])
    elif payload["finalization_id"] is not None:
        raise SetupAuthorityError("First-run eligibility has conflicting finalization evidence.")
    return payload


def provision_eligibility(directory: Path, installation_id: str) -> None:
    """First-install provisioner only; never resets an existing receipt."""
    state._validate_directory(directory, "setup state directory")
    path = directory / _STATE_NAME
    if path.exists() or path.is_symlink():
        read_finalization(directory, installation_id)
        state._fsync_directory(directory)
        return
    temporary = directory / (".setup-eligibility-" + secrets.token_hex(16))
    try:
        state._atomic_write_json(temporary, {
            "schema_version": 1, "installation_id": _uuid(installation_id),
            "state": "eligible", "finalization_id": None,
        })
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            read_finalization(directory, installation_id)
        finally:
            temporary.unlink()
        state._fsync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def recover_committed_finalization(
    directory: Path, installation_id: str, *, has_account: bool,
    database_finalization_id: str | None,
) -> None:
    """Repair a lost receipt only toward finalized using independently read DB proof.

    Missing eligibility with an empty database remains an explicit repair error.
    Corrupt or foreign evidence is never replaced by this startup reconciliation.
    """
    state._validate_directory(directory, "setup state directory")
    identity = _uuid(installation_id)
    if type(has_account) is not bool:
        raise SetupAuthorityError("The account proof must be explicit.")
    finalization = _uuid(database_finalization_id) if database_finalization_id is not None else None
    try:
        _read(directory / _STATE_NAME)
    except FileNotFoundError:
        if not has_account and finalization is None:
            raise SetupAuthorityError("First-run eligibility is missing; explicit recovery is required.")
        temporary = directory / (".setup-finalized-" + secrets.token_hex(16))
        try:
            state._atomic_write_json(temporary, {
                "schema_version": 1, "installation_id": identity,
                "state": "finalized", "finalization_id": finalization or str(uuid.uuid4()),
            })
            try:
                os.link(temporary, directory / _STATE_NAME, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        state._fsync_directory(directory)
    read_finalization(directory, identity)


@dataclass(frozen=True)
class BootstrapCode:
    code: str = field(repr=False)
    expires_at: int
    remaining_seconds: int


class SetupAuthority:
    def __init__(
        self, installation_id: str, supervisor_invocation_id: str, state_directory: Path,
        runtime_directory: Path, *, wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.installation_id = _uuid(installation_id)
        if not re.fullmatch(r"[0-9a-f]{32}", supervisor_invocation_id) or int(supervisor_invocation_id, 16) == 0:
            raise SetupAuthorityError("The supervisor incarnation is invalid.")
        self.invocation = supervisor_invocation_id
        self.state_directory, self.runtime_directory = state_directory, runtime_directory
        state._validate_directory(runtime_directory, "setup runtime directory")
        self.lock = asyncio.Lock()
        self.wall_clock, self.monotonic_clock = wall_clock, monotonic_clock
        self._verifier: bytes | None = None
        self._generation: str | None = None
        self._deadline = 0.0
        self._expires_at = 0
        self.finalized = read_finalization(state_directory, installation_id)["state"] == "finalized"
        # No old supervisor code can be accepted after restart. Delete only the
        # exact private owned file, never a caller-selected path.
        self._remove_raw()

    def _remove_raw(self) -> None:
        path = self.runtime_directory / _RAW_NAME
        try:
            _read(path)
        except FileNotFoundError:
            return
        path.unlink()
        state._fsync_directory(self.runtime_directory)

    def _require_eligible(self) -> None:
        receipt = read_finalization(self.state_directory, self.installation_id)
        if self.finalized or receipt["state"] == "finalized":
            self.finalized = True
            raise SetupAlreadyFinalized("StreamHome setup has already been finalized.")

    def _remaining(self) -> int:
        return max(0, math.ceil(min(self._deadline - self.monotonic_clock(), self._expires_at - self.wall_clock())))

    async def issue(self, *, renew: bool = False) -> BootstrapCode:
        if type(renew) is not bool:
            raise SetupAuthorityError("The renewal choice must be explicit.")
        async with self.lock:
            self._require_eligible()
            if not renew and self._verifier is not None and self._remaining() > 0:
                try:
                    raw = _read(self.runtime_directory / _RAW_NAME)
                except FileNotFoundError:
                    self._verifier = None
                else:
                    if (
                        set(raw) != {"schema_version", "installation_id", "invocation", "generation", "expires_at", "code"}
                        or type(raw["schema_version"]) is not int or raw["schema_version"] != 1
                        or raw["installation_id"] != self.installation_id or raw["invocation"] != self.invocation
                        or raw["generation"] != self._generation or raw["expires_at"] != self._expires_at
                        or not isinstance(raw["code"], str)
                        or not hmac.compare_digest(hashlib.sha256(raw["code"].encode()).digest(), self._verifier)
                    ):
                        raise SetupAuthorityError("The ephemeral setup code does not match the running authority.")
                    return BootstrapCode(raw["code"], self._expires_at, self._remaining())
            self._verifier = None
            self._remove_raw()
            code = secrets.token_urlsafe(32)
            generation = str(uuid.uuid4())
            expires = int(self.wall_clock()) + CODE_TTL_SECONDS
            state._atomic_write_json(self.runtime_directory / _RAW_NAME, {
                "schema_version": 1, "installation_id": self.installation_id,
                "invocation": self.invocation, "generation": generation, "expires_at": expires, "code": code,
            })
            self._verifier, self._generation = hashlib.sha256(code.encode()).digest(), generation
            self._expires_at, self._deadline = expires, self.monotonic_clock() + CODE_TTL_SECONDS
            return BootstrapCode(code, expires, self._remaining())

    async def consume(self, code: str) -> bool:
        async with self.lock:
            self._require_eligible()
            if not isinstance(code, str) or len(code) > 256 or self._verifier is None or self._remaining() <= 0:
                return False
            if not hmac.compare_digest(hashlib.sha256(code.encode()).digest(), self._verifier):
                return False
            self._verifier = None
            self._remove_raw()
            return True

    def finalize_locked(self, finalization_id: str) -> None:
        """After the matching DB receipt commits, while holding completion lock."""
        if not self.lock.locked():
            raise SetupAuthorityError("Setup finalization requires serialized completion.")
        identity = _uuid(finalization_id)
        self.finalized = True
        self._verifier = None
        try:
            self._remove_raw()
            previous = read_finalization(self.state_directory, self.installation_id)
            if previous["state"] == "finalized":
                return
            state._atomic_write_json(self.state_directory / _STATE_NAME, {
                "schema_version": 1, "installation_id": self.installation_id,
                "state": "finalized", "finalization_id": identity,
            })
        except (OSError, state.PersistenceError) as exc:
            raise SetupAuthorityError("Setup committed; durable finalization requires reconciliation.") from exc

    async def reconcile(self, *, has_account: bool, database_finalization_id: str | None) -> None:
        async with self.lock:
            if self.finalized:
                return
            if database_finalization_id is not None:
                _uuid(database_finalization_id)
            if has_account or database_finalization_id is not None:
                self.finalize_locked(database_finalization_id or str(uuid.uuid4()))


_authority: SetupAuthority | None = None


def install_setup_authority(authority: SetupAuthority) -> None:
    global _authority
    if _authority is not None:
        raise SetupAuthorityError("Setup authority was already installed in this process.")
    _authority = authority


def require_setup_authority() -> SetupAuthority:
    if _authority is None:
        raise SetupAuthorityError("The managed runtime setup authority is unavailable.")
    return _authority
