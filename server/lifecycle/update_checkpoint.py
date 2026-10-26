"""Original UPDATE-owned SQLite checkpoint evidence, never a separate DB owner."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import stat
import time
import uuid

from . import state, update_controller, update_execution
from .layout import layout_for_root
from .execution_state import read_active_execution
from .management_journal import execution_tuple_sha256
from .control.rpc import ControlError


def path_for(root: Path, operation_id: str, *, create=False) -> Path:
    if str(uuid.UUID(operation_id)) != operation_id:
        raise ControlError('control_update_checkpoint_invalid')
    layout = layout_for_root(root)
    base = layout.durable_runtime/'update-checkpoints'
    operation = base/operation_id
    if create:
        state._validate_directory(layout.durable_runtime, 'update checkpoint parent')
        state._ensure_private_directory(base, 'update checkpoint directory')
        state._ensure_private_directory(operation, 'update checkpoint operation directory')
    return operation/'database.db'


def _private_file(path: Path):
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o7177 or metadata.st_nlink != 1 or path.is_symlink()):
        raise ControlError('control_update_checkpoint_untrusted')
    return metadata


def _identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
        metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def identity(path: Path, *, checkpoint=None) -> tuple[int, str, tuple]:
    before = _private_file(path)
    digest = hashlib.sha256();last = 0.0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise ControlError('control_update_checkpoint_changed')
        while chunk := handle.read(1024*1024):
            digest.update(chunk)
            if checkpoint is not None and time.monotonic() >= last:
                checkpoint();last = time.monotonic()+.25
        if (_identity(os.fstat(handle.fileno())) != _identity(before)
                or _identity(_private_file(path)) != _identity(before)):
            raise ControlError('control_update_checkpoint_changed')
    return before.st_size, digest.hexdigest(), _identity(before)


def require_checkpoint(root: Path, bound: update_execution.UpdateExecution, *, checkpoint=None) -> Path:
    from .restore_controller import validate_sqlite_database
    if bound.checkpoint is None:
        raise ControlError('control_update_checkpoint_unbound')
    path = path_for(root, bound.parent.operation_id)
    state._validate_directory(path.parent.parent, 'update checkpoint directory')
    state._validate_directory(path.parent, 'update checkpoint operation directory')
    size, digest, observed = identity(path, checkpoint=checkpoint)
    if size != bound.checkpoint.size_bytes or digest != bound.checkpoint.sha256:
        raise ControlError('control_update_checkpoint_changed')
    validate_sqlite_database(path, standalone=True)
    if _identity(_private_file(path)) != observed:
        raise ControlError('control_update_checkpoint_changed')
    return path


def create(root: Path, proof) -> dict:
    """Reuse SQLite backup under the original stopped updater, then bind once."""
    from .managed_update import bound_journal, require_delegate_peer, supervisor_command
    from .restore_controller import _copy_sqlite
    proof.require_current()
    authority, original, bound = bound_journal(root, proof.lease.operation_id)
    if (authority.active_operation.phase is not state.LifecyclePhase.EXCLUSIVE
            or authority.generation != bound.parent.original_generation+1
            or original['phase'] != 'stopping' or bound.checkpoint is not None
            or bound.accepted is not None or bound.verification is not None):
        raise ControlError('control_update_checkpoint_unavailable')
    active = read_active_execution(root)
    if (active.installation_id != bound.parent.installation_id or active.execution != bound.parent.execution
            or active.generation >= bound.parent.original_generation):
        raise ControlError('control_parent_binding_changed')
    update_controller.validate_pre_cutover_baseline(root, original['previous_commit'])
    if supervisor_command(root, proof, 'update_assert_stopped').get('stopped') is not True:
        raise ControlError('control_children_busy')
    layout = layout_for_root(root)
    state._validate_directory(layout.database.parent, 'canonical database directory')
    _private_file(layout.database)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(layout.database)+suffix)
        if sidecar.exists() or sidecar.is_symlink():_private_file(sidecar)
    path = path_for(root, bound.parent.operation_id, create=True)
    journal_path, lock_path, _ = update_controller._update_paths(root, proof.lease.operation_id, authority.generation)
    descriptor = state._open_locked_file(lock_path)
    try:
        if update_controller.read_update_journal(journal_path) != original:
            raise ControlError('control_parent_binding_changed')
        # An existing operation artifact is evidence, even if its journal write
        # was interrupted. Never overwrite it or attribute it by SQLite validity.
        if path.exists() or path.is_symlink():
            raise ControlError('control_update_checkpoint_exists')
        proof.require_current()
        _copy_sqlite(layout.database, path, source_standalone=False, checkpoint=proof.require_current)
        size, digest, observed = identity(path, checkpoint=proof.require_current)
        prepared = update_execution.DatabaseCheckpoint(bound.parent.operation_id, authority.generation,
            execution_tuple_sha256(bound.parent.execution), digest, size)
        updated = {**original, 'managed_database_checkpoint': prepared.payload()}
        update_execution.require_transition(original, updated)
        proof.require_current()
        with state.hold_operation_with_credential(root, operation_id=bound.parent.operation_id,
                generation=authority.generation, expected_phase=state.LifecyclePhase.EXCLUSIVE,
                expected_operation_type=state.OperationType.UPDATE) as current:
            if (current != authority or update_controller.read_update_journal(journal_path) != original
                    or _identity(_private_file(path)) != observed):
                raise ControlError('control_parent_binding_changed')
            require_delegate_peer(root, proof.lease, os.getpid(), expected_invocation=os.environ.get('INVOCATION_ID', ''))
            if supervisor_command(root, proof, 'update_assert_stopped', authority_locked=True).get('stopped') is not True:
                raise ControlError('control_children_busy')
            from .runtime_build import canonical_bytes
            update_controller._private_atomic_write(journal_path, canonical_bytes(updated))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)
    return {'created': True}


def require_database(root: Path) -> Path:
    """A managed continuation must not initialize a missing or aliased database."""
    from .restore_controller import validate_sqlite_database
    path = layout_for_root(root).database
    state._validate_directory(path.parent, 'canonical database directory')
    _private_file(path)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path)+suffix)
        if sidecar.exists() or sidecar.is_symlink():_private_file(sidecar)
    validate_sqlite_database(path, standalone=False)
    return path


def require_launch_database(root: Path, bound: update_execution.UpdateExecution) -> Path:
    """Qualify guarded launch without requiring a retired accepted snapshot."""
    if bound.accepted is None:
        require_checkpoint(root, bound)
    return require_database(root)


def restore(root: Path, proof) -> dict:
    """Use the existing rollback file primitives, only with original UPDATE proof."""
    from .managed_update import bound_journal, runtime_action, supervisor_command
    from .restore_controller import (_close_sqlite_journal, _copy_sqlite,
        require_no_media_publication_evidence, validate_sqlite_database)
    proof.require_current()
    if runtime_action(root, proof, 'assert_rollback') != {'rollback_allowed': True}:
        raise ControlError('control_update_checkpoint_unavailable')
    authority, journal, bound = bound_journal(root, proof.lease.operation_id)
    checkpoint = require_checkpoint(root, bound, checkpoint=proof.require_current)
    database = require_database(root)
    update_controller.validate_pre_cutover_baseline(root, journal['previous_commit'])
    require_no_media_publication_evidence(root)
    proof.require_current()
    if supervisor_command(root, proof, 'update_assert_stopped').get('stopped') is not True:
        raise ControlError('control_children_busy')
    # Close committed candidate WAL through SQLite before replacing the main
    # file. A crash cannot replay stale candidate WAL against the old snapshot.
    _close_sqlite_journal(database)
    proof.require_current()
    def before_publish():
        proof.require_current()
        require_checkpoint(root, bound, checkpoint=proof.require_current)
        require_database(root)
        if (bound_journal(root, proof.lease.operation_id) != (authority, journal, bound)
                or supervisor_command(root, proof, 'update_assert_stopped').get('stopped') is not True):
            raise ControlError('control_parent_binding_changed')
    _copy_sqlite(checkpoint, database, source_standalone=True,
        checkpoint=proof.require_current, before_publish=before_publish)
    validate_sqlite_database(database, standalone=True)
    proof.require_current()
    if bound_journal(root, proof.lease.operation_id) != (authority, journal, bound):
        raise ControlError('control_parent_binding_changed')
    return {'restored': True}


def cleanup(root: Path, proof) -> dict:
    """Retire only a verified, now unnecessary snapshot; preserve unknown bytes."""
    from .managed_update import bound_journal, require_delegate_peer, runtime_action
    from .restore_controller import RestoreError
    proof.require_current()
    authority, journal, bound = bound_journal(root, proof.lease.operation_id)
    path = path_for(root, bound.parent.operation_id)
    if not path.exists() and not path.is_symlink():return {'discarded': True}
    if bound.accepted is None or bound.checkpoint is None:return {'discarded': False}
    active = read_active_execution(root)
    if (active.installation_id != bound.parent.installation_id or active.execution != bound.accepted
            or active.operation_id != bound.parent.operation_id or active.generation != authority.generation):
        return {'discarded': False}
    if runtime_action(root, proof, 'readiness') != {'ready': True}:return {'discarded': False}
    try:
        require_checkpoint(root, bound, checkpoint=proof.require_current)
        observed = _identity(_private_file(path))
    except (ControlError, RestoreError, OSError, ValueError):
        return {'discarded': False}
    proof.require_current()
    with state.hold_operation_with_credential(root, operation_id=bound.parent.operation_id,
            generation=authority.generation, expected_phase=state.LifecyclePhase.VERIFYING,
            expected_operation_type=state.OperationType.UPDATE) as current:
        if (current != authority or bound_journal(root, bound.parent.operation_id) != (authority, journal, bound)
                or _identity(_private_file(path)) != observed):
            raise ControlError('control_parent_binding_changed')
        require_delegate_peer(root, proof.lease, os.getpid(), expected_invocation=os.environ.get('INVOCATION_ID', ''))
        path.unlink();state._fsync_directory(path.parent)
    return {'discarded': True}
