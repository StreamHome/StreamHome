"""Bound management journal persistence, subordinate to canonical authority.

It never migrates authority, starts processes, changes configuration, verifies a
runtime, accepts or releases a claim. Explicit generation takeover has a separate
retained-intent protocol in management_recovery_store.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from . import management_journal as contract, state


class ManagementJournalMissingError(contract.ManagementJournalError):
    code = "management_journal_missing"


def _journal_path(root: str | os.PathLike[str], operation_id: str) -> tuple[state.LifecyclePaths, Path]:
    canonical_id = contract._uuid(operation_id, "operation ID")
    paths = state.lifecycle_paths(root)
    return paths, paths.lifecycle_dir / "management" / canonical_id / "journal.json"


def _validate_parents(paths: state.LifecyclePaths, path: Path) -> None:
    state._validate_read_path(paths)
    for directory in (paths.lifecycle_dir / "management", path.parent):
        state._validate_directory(directory, "management journal directory")


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
        metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
    )


def inspect_management_journal(
    root: str | os.PathLike[str], operation_id: str,
) -> contract.ManagementJournal:
    """Read bounded private evidence without creating paths or acquiring locks."""
    paths, path = _journal_path(root, operation_id)
    descriptor: int | None = None
    try:
        # Missing parents must not make inspection create an empty operation.
        for directory in (paths.run_dir, paths.lifecycle_dir, paths.lifecycle_dir / "management", path.parent):
            try:
                directory.lstat()
            except FileNotFoundError as exc:
                raise ManagementJournalMissingError("management journal is absent") from exc
            state._validate_directory(directory, "management journal directory")
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_mode & 0o7177 or before.st_nlink != 1
            or before.st_size > contract.MAX_JOURNAL_BYTES
        ):
            raise contract.ManagementJournalError("management journal is not trusted private evidence")
        raw = os.read(descriptor, contract.MAX_JOURNAL_BYTES + 1)
        if len(raw) > contract.MAX_JOURNAL_BYTES or os.read(descriptor, 1):
            raise contract.ManagementJournalError("management journal exceeds its input bound")
        if _identity(before) != _identity(os.fstat(descriptor)) or _identity(before) != _identity(path.lstat()):
            raise contract.ManagementJournalError("management journal changed during inspection")
        journal = contract.parse_management_journal_json(raw)
        if journal.operation_id != operation_id:
            raise contract.ManagementJournalError("management journal path and operation do not match")
        return journal
    except FileNotFoundError as exc:
        raise ManagementJournalMissingError("management journal is absent") from exc
    except OSError as exc:
        raise contract.ManagementJournalError("management journal cannot be inspected") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_lease(
    paths: state.LifecyclePaths, lease: state.ControllerLeaseHandle,
    journal: contract.ManagementJournal, authority: state.LifecycleState,
) -> None:
    lease.require_current()
    if (
        lease.paths.root != paths.root
        or lease.lease.operation_id != journal.operation_id
        or lease.lease.generation != journal.generation
        or lease.lease.owner_kind is not state.OwnerKind.MANAGEMENT_CONTROLLER
        or lease.lease.schema_version != authority.schema_version
    ):
        raise state.OwnershipError("management journal requires its exact controller lease")
    contract.validate_active_journal(journal, authority)


def _publish(path: Path, journal: contract.ManagementJournal) -> None:
    payload = contract.management_journal_payload(journal)
    contract.parse_management_journal(payload)
    if len((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")) > contract.MAX_JOURNAL_BYTES:
        raise contract.ManagementJournalError("management journal exceeds its persistence bound")
    state._atomic_write_json(path, payload)


def _acknowledge_existing(path: Path) -> None:
    # A prior replace may have succeeded before its directory-fsync reply was
    # lost. An exact retry must reaffirm durability before acknowledging it.
    try:
        state._fsync_directory(path.parent)
    except OSError as exc:
        raise state.PersistenceError("management journal durability could not be reaffirmed") from exc


def create_management_journal(
    root: str | os.PathLike[str], journal: contract.ManagementJournal, *,
    lease: state.ControllerLeaseHandle,
) -> contract.ManagementJournal:
    """Publish only the exact initial journal under controller -> state locking."""
    contract.parse_management_journal(contract.management_journal_payload(journal))
    if journal.stage is not contract.ManagementStage.RESERVED:
        raise contract.ManagementJournalError("initial management journal must be reserved")
    paths, path = _journal_path(root, journal.operation_id)
    with state.hold_operation_with_credential(
        paths.root, operation_id=journal.operation_id, generation=journal.generation,
        expected_phase=state.LifecyclePhase.RESERVED,
        expected_operation_type=state.OperationType.MANAGEMENT,
    ) as authority:
        _require_lease(paths, lease, journal, authority)
        for directory in (paths.lifecycle_dir / "management", path.parent):
            state._ensure_private_directory(directory, "management journal directory")
        if path.exists() or path.is_symlink():
            if inspect_management_journal(paths.root, journal.operation_id) != journal:
                raise state.LifecycleConflictError("management journal already binds different inputs")
            _acknowledge_existing(path)
        else:
            _publish(path, journal)
    return journal


def advance_management_journal(
    root: str | os.PathLike[str], expected: contract.ManagementJournal,
    updated: contract.ManagementJournal, *, lease: state.ControllerLeaseHandle,
) -> contract.ManagementJournal:
    """CAS exact journal progress; no takeover, recovery resume or authority release."""
    contract.validate_journal_transition(expected, updated)
    paths, path = _journal_path(root, updated.operation_id)
    with state.hold_operation_with_credential(
        paths.root, operation_id=updated.operation_id, generation=updated.generation,
        expected_phase=contract.journal_phase(updated), expected_operation_type=state.OperationType.MANAGEMENT,
    ) as authority:
        _require_lease(paths, lease, updated, authority)
        _validate_parents(paths, path)
        current = inspect_management_journal(paths.root, updated.operation_id)
        if current == updated:
            _acknowledge_existing(path)
        elif current != expected:
            raise state.LifecycleConflictError("management journal changed after review")
        else:
            _publish(path, updated)
    return updated
