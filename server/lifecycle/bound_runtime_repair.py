"""Exact filesystem reconstruction under an existing parent's fenced ownership."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Callable
import uuid

from . import runtime_recovery_cache as cache, state
from .runtime_build import RuntimeBuildError, canonical_bytes, generation, require_runtime_root, verify_runtime


def targets(root: Path, execution) -> dict[str, Path]:
    return {name: root / 'runtime' / name / generation(identity) for name, identity in (
        ('python', execution.python_environment_id), ('node', execution.node_environment_id),
        ('web', execution.web_bundle_id), ('bundles', execution.runtime_bundle_id))}


def fingerprint(path: Path) -> str:
    """Hash bounded no-follow metadata, including missing/corrupt trees.

    File contents or sensitive diagnostic data are never copied into a review.
    Inode/ctime evidence catches replacement or writes before quarantine.
    """
    rows = [];pending = [(path, '.')]
    while pending:
        current, relative = pending.pop()
        try:
            item = current.lstat()
        except FileNotFoundError:
            rows.append([relative, 'missing']);continue
        if len(rows) >= 200000 or len(relative) > 4096:
            raise RuntimeBuildError('The bound artifact inventory exceeds its review limit.')
        if not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)):
            raise RuntimeBuildError('An artifact contains an unsupported special file.')
        if item.st_uid != os.geteuid():
            raise RuntimeBuildError('A bound artifact has a foreign owner.')
        link = hashlib.sha256(os.fsencode(os.readlink(current))).hexdigest() if stat.S_ISLNK(item.st_mode) else None
        rows.append([relative, item.st_dev, item.st_ino, item.st_mode, item.st_nlink, item.st_size,
                     item.st_mtime_ns, item.st_ctime_ns, link])
        if stat.S_ISDIR(item.st_mode):
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.name, reverse=True)
            pending.extend((current / entry.name, entry.name if relative == '.' else relative + '/' + entry.name)
                           for entry in children)
    return hashlib.sha256(canonical_bytes(rows)).hexdigest()


def inspect(root: Path, cache_root: Path, execution, *, checkpoint: Callable[[], None]) -> dict:
    require_runtime_root(root)
    manifest = cache.inspect(cache_root, execution, checkpoint=checkpoint)
    identities = {}
    for name, target in targets(root, execution).items():
        checkpoint();state._validate_directory(target.parent, 'bound runtime generation parent')
        identities[name] = fingerprint(target)
    return {'manifest_sha256': execution.runtime_manifest_sha256, 'targets': identities,
            'filesystem_sha256': hashlib.sha256(canonical_bytes(identities)).hexdigest()}


def reconstruct(root: Path, cache_root: Path, execution, *, expected: dict, receipt: Path,
                checkpoint: Callable[[], None], quiescent: Callable[[], None]) -> dict:
    """Preserve corrupt trees, restore at original paths, leave parent unresolved."""
    if os.geteuid() == 0:
        raise RuntimeBuildError('Bound environment repair must use the service account.')
    quiescent()
    fresh = inspect(root, cache_root, execution, checkpoint=checkpoint)
    if fresh != expected:
        raise RuntimeBuildError('The reviewed bound artifacts changed before reconstruction.')
    manifest = cache.inspect(cache_root, execution, checkpoint=checkpoint)
    attempt = str(uuid.uuid4())
    record = {'schema_version': 1, 'attempt_id': attempt, 'manifest_sha256': execution.runtime_manifest_sha256,
              'filesystem_sha256': expected['filesystem_sha256'], 'status': 'planned', 'trees': {}}
    if receipt.exists() or receipt.is_symlink():
        raise RuntimeBuildError('This exact repair attempt already has a receipt; obtain a fresh review.')
    state._atomic_write_json(receipt, record)
    for name, target in targets(root, execution).items():
        quiescent();checkpoint()
        if fingerprint(target) != expected['targets'][name]:
            raise RuntimeBuildError('A bound artifact changed before its reviewed rename.')
        quarantine = target.with_name('.repair-' + target.name + '-' + attempt)
        present = target.exists() or target.is_symlink()
        record['trees'][name] = {'quarantine': quarantine.name if present else None, 'stage': 'rename_planned'}
        state._atomic_write_json(receipt, record)
        checkpoint()
        if present:
            if quarantine.exists() or quarantine.is_symlink():
                raise RuntimeBuildError('The contained repair evidence destination already exists.')
            os.rename(target, quarantine);state._fsync_directory(target.parent)
        record['trees'][name]['stage'] = 'reconstructing';state._atomic_write_json(receipt, record)
        if name == 'bundles':
            target.mkdir(mode=0o700)
            cache._copy_checked(cache_root / 'manifests' / (execution.runtime_manifest_sha256 + '.json'),
                target / 'manifest.json', execution.runtime_manifest_sha256, checkpoint)
            cache.restore_empty_tree(cache_root, target / 'tools', manifest['tools'], checkpoint=checkpoint)
        else:
            cache.restore_empty_tree(cache_root, target, manifest[name], checkpoint=checkpoint)
        record['trees'][name]['stage'] = 'verified';state._atomic_write_json(receipt, record)
    quiescent();checkpoint();verify_runtime(root, execution);quiescent()
    record['status'] = 'complete';state._atomic_write_json(receipt, record)
    return record
