"""Recover only the original UPDATE's finite paired-link publication."""
from __future__ import annotations

import os
from pathlib import Path
import secrets

from . import state, update_controller
from .layout import layout_for_root
from .management_journal import execution_tuple_sha256
from .runtime_build import RuntimeBuildError
from .control.provision_worker import _private_record


def recover(root: Path, *, authority, journal, bound, proof, target: str,
            source: dict, source_path: Path, assert_children_stopped) -> None:
    """Called under the existing controller's credential/state publication lock."""
    from .managed_update import bound_journal
    from .update_recovery import digest, require_controller_permission
    from .runtime_links import verify_links
    old, new = bound.parent.execution, bound.candidate
    previous, candidate = execution_tuple_sha256(old), execution_tuple_sha256(new)
    chosen = old if target == 'previous' else new
    operation = bound.parent.operation_id;generation = authority.generation
    if (set(source) != {'schema_version', 'operation_id', 'generation', 'previous', 'candidate', 'stage'}
            or type(source['schema_version']) is not int or source['schema_version'] != 1
            or source['operation_id'] != operation or type(source['generation']) is not int
            or not bound.parent.original_generation < source['generation'] <= generation
            or source['stage'] not in {'applying', 'complete'}
            or (source['previous'], source['candidate']) not in {(previous, candidate), (candidate, previous)}
            or bound.accepted is not None and chosen != bound.accepted):
        raise RuntimeBuildError('The original update web binding receipt does not explain this recovery.')
    grant = require_controller_permission(root, journal, bound, generation)
    if source['generation'] != generation and (grant is None
            or grant['runtime_link_source_sha256'] != digest(source)):
        raise RuntimeBuildError('The update web binding receipt was not part of this exact recovery review.')
    layout = layout_for_root(root)
    receipt = layout.state/f'runtime-link-update-recovery-{operation}-{generation}.json'
    intent = {'schema_version': 1, 'operation_id': operation, 'generation': generation,
        'source_generation': source['generation'], 'source_receipt_sha256': digest(source),
        'target_execution_sha256': execution_tuple_sha256(chosen), 'stage': 'applying'}
    if receipt.exists() or receipt.is_symlink():
        if _private_record(receipt) not in (intent, {**intent, 'stage': 'complete'}):
            raise RuntimeBuildError('Another update web recovery intent must be preserved.')
    web = root/'web';before = web.lstat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_uid, value.st_mode)
    def checkpoint():
        if (state.read_controller_lease(root) != proof.lease or not state.controller_lease_is_current(proof.lease)
                or bound_journal(root, operation) != (authority, journal, bound)
                or _private_record(source_path) != source or web.resolve(strict=True) != web
                or identity(web.lstat()) != identity(before)):
            raise RuntimeBuildError('The update link recovery owner or source changed.')
        update_controller._require_managed_progress_permission(root, journal)
        assert_children_stopped()
    for name, old_target, new_target in (('node_modules', old.web_dependency_target, new.web_dependency_target),
            ('dist', old.web_dist_target, new.web_dist_target)):
        path = web/name
        if not path.is_symlink() or os.readlink(path) not in {old_target, new_target}:
            raise RuntimeBuildError('An unrelated update web binding cannot be replaced.')
    checkpoint()
    state._atomic_write_json(receipt, intent)
    for name, destination in (('node_modules', chosen.web_dependency_target), ('dist', chosen.web_dist_target)):
        checkpoint()
        temporary = web/('.binding-'+secrets.token_hex(16))
        try:
            temporary.symlink_to(destination, target_is_directory=True)
            os.replace(temporary, web/name);state._fsync_directory(web)
        finally:
            temporary.unlink(missing_ok=True)
    checkpoint();verify_links(root, chosen)
    state._atomic_write_json(receipt, {**intent, 'stage': 'complete'})
