"""Unprivileged content-addressed inputs for exact bound-runtime reconstruction.

The caller supplies the canonical bound manifest hash and owns all lifecycle and
quiescence checks. Cached bytes alone never authorize replacement or a start.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Callable

from . import state
from .runtime_build import RuntimeBuildError, generation, tree_inventory


_HASH = re.compile(r'[0-9a-f]{64}')
_LIMIT = 512 * 1024 * 1024


def _entries(value: dict) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 100000:
        raise RuntimeBuildError('The bound recovery inventory is invalid.')
    for name, expected in value.items():
        if (not isinstance(name, str) or not name or len(name) > 4096
                or PurePosixPath(name).is_absolute() or PurePosixPath(name).as_posix() != name
                or any(part in {'', '.', '..'} for part in name.split('/'))
                or not isinstance(expected, str) or not (_HASH.fullmatch(expected) or expected.startswith('link:') and 0 < len(expected[5:]) <= 4096)
                or any(parent.as_posix() in value for parent in PurePosixPath(name).parents if parent.as_posix() != '.')):
            raise RuntimeBuildError('The bound recovery inventory is invalid.')
    return value


def _private_file(path: Path, *, source_links: bool = False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    metadata = os.fstat(fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_nlink < 1 or not source_links and metadata.st_nlink != 1
            or metadata.st_mode & 0o7022 or metadata.st_size > _LIMIT):
        os.close(fd)
        raise RuntimeBuildError('A recovery input is untrusted or exceeds its size bound.')
    return os.fdopen(fd, 'rb')


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def _object(cache: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise RuntimeBuildError('The recovery object digest is invalid.')
    return cache / 'objects' / digest


def _copy_checked(source: Path, target: Path, expected: str, checkpoint: Callable[[], None], *, source_links: bool = False) -> None:
    checkpoint()
    temporary = target.with_name('.' + target.name + '.' + secrets.token_hex(16))
    try:
        digest = hashlib.sha256()
        with _private_file(source, source_links=source_links) as reader, temporary.open('xb') as writer:
            os.fchmod(writer.fileno(), 0o700)
            before = os.fstat(reader.fileno())
            total = 0
            while block := reader.read(1024 * 1024):
                checkpoint()
                total += len(block)
                if total > _LIMIT:
                    raise RuntimeBuildError('A recovery object exceeds its size bound.')
                digest.update(block);writer.write(block)
            writer.flush();os.fsync(writer.fileno())
            if digest.hexdigest() != expected or _identity(before) != _identity(os.fstat(reader.fileno())):
                raise RuntimeBuildError('A recovery input changed or does not match the bound manifest.')
        checkpoint()
        if target.exists() or target.is_symlink():
            raise RuntimeBuildError('A recovery destination already exists.')
        os.link(temporary, target, follow_symlinks=False)
        temporary.unlink()
        state._fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_manifest(cache: Path, expected: str) -> tuple[dict, bytes]:
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise RuntimeBuildError('The bound recovery manifest digest is invalid.')
    for directory in (cache, cache / 'objects', cache / 'manifests'):
        state._validate_directory(directory, 'bound recovery cache directory')
    with _private_file(cache / 'manifests' / (expected + '.json')) as reader:
        raw = reader.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeBuildError('The exact bound recovery manifest is unavailable.')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise RuntimeBuildError('The bound recovery manifest is incompatible.')
    for name in ('bundle_id', 'python_id', 'node_id', 'web_id'):
        generation(value.get(name))
    for name in ('python', 'node', 'web', 'tools'):
        _entries(value.get(name))
    return value, raw


def capture(root: Path, cache: Path, manifest_sha256: str, *, checkpoint: Callable[[], None]) -> None:
    """Capture only manifest-named immutable package bytes, never application data."""
    if os.geteuid() == 0 or not _HASH.fullmatch(manifest_sha256):
        raise RuntimeBuildError('Recovery cache construction requires the service account and exact manifest.')
    for directory in (cache, cache / 'objects', cache / 'manifests'):
        state._ensure_private_directory(directory, 'bound runtime recovery cache')
    # Locate the already completed immutable manifest by hash, not by a mutable
    # active link. The caller supplies its exact bundle identity through root.
    manifest_path = root / 'manifest.json'
    with _private_file(manifest_path) as handle:
        raw = handle.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise RuntimeBuildError('The completed runtime manifest changed before caching.')
    manifest = json.loads(raw)
    bundle = generation(manifest.get('bundle_id'))
    runtime = root.parent.parent
    if root.name != bundle or root.parent.name != 'bundles':
        raise RuntimeBuildError('The recovery source is not its permanent bundle.')
    for category, identity in (('python', 'python_id'), ('node', 'node_id'), ('web', 'web_id'), ('tools', None)):
        source = root / 'tools' if identity is None else runtime / category / generation(manifest.get(identity))
        inventory = _entries(manifest.get(category))
        if tree_inventory(source) != inventory:
            raise RuntimeBuildError('The runtime changed before recovery inputs were captured.')
        for name, expected in inventory.items():
            checkpoint()
            if expected.startswith('link:'):
                continue
            target = _object(cache, expected)
            if target.exists() or target.is_symlink():
                with _private_file(target) as handle:
                    if hashlib.file_digest(handle, 'sha256').hexdigest() != expected:
                        raise RuntimeBuildError('An existing recovery object is corrupt; evidence was retained.')
            else:
                # npm/esbuild legitimately hardlink identical installed files.
                # Capture verified bytes into an independent single-link object;
                # cached objects and reconstruction inputs remain single-link.
                _copy_checked(source / name, target, expected, checkpoint, source_links=True)
    path = cache / 'manifests' / (manifest_sha256 + '.json')
    if not path.exists() and not path.is_symlink():
        _copy_checked(manifest_path, path, manifest_sha256, checkpoint)
    _read_manifest(cache, manifest_sha256)


def inspect(cache: Path, execution, *, checkpoint: Callable[[], None]) -> dict:
    """Read/verify required objects without changing runtime or canonical state."""
    if os.geteuid() == 0:
        raise RuntimeBuildError('Bound recovery inputs must be inspected as the service account.')
    manifest, _ = _read_manifest(cache, execution.runtime_manifest_sha256)
    if any(manifest.get(name) != expected for name, expected in (
            ('bundle_id', execution.runtime_bundle_id), ('python_id', execution.python_environment_id),
            ('node_id', execution.node_environment_id), ('web_id', execution.web_bundle_id),
            ('application_commit', execution.application_commit), ('application_build_id', execution.application_build_id))):
        raise RuntimeBuildError('The recovery manifest names another execution.')
    checked = set()
    for category in ('python', 'node', 'web', 'tools'):
        for expected in manifest[category].values():
            checkpoint()
            if expected.startswith('link:') or expected in checked:
                continue
            with _private_file(_object(cache, expected)) as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != expected:
                    raise RuntimeBuildError('A required exact recovery object is corrupt.')
            checked.add(expected)
    return manifest


def restore_empty_tree(cache: Path, target: Path, inventory: dict, *, checkpoint: Callable[[], None]) -> None:
    """Reconstruct at the original permanent path after separately proven quiescence.

    This primitive refuses any existing destination. The canonical controller must
    preserve corrupt evidence and journal each rename before calling it.
    """
    if os.geteuid() == 0:
        raise RuntimeBuildError('Environment reconstruction must use the service account.')
    _entries(inventory)
    state._validate_directory(cache, 'bound recovery cache directory')
    state._validate_directory(cache / 'objects', 'bound recovery objects')
    state._validate_directory(target.parent, 'permanent reconstruction parent')
    checkpoint()
    target.mkdir(mode=0o700)
    for name, expected in inventory.items():
        checkpoint()
        destination = target / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not expected.startswith('link:'):
            _copy_checked(_object(cache, expected), destination, expected, checkpoint)
    for name, expected in inventory.items():
        if expected.startswith('link:'):
            checkpoint();(target / name).symlink_to(expected[5:])
    if tree_inventory(target) != inventory:
        raise RuntimeBuildError('The reconstructed environment does not match its original bound inventory.')
    checkpoint();state._fsync_directory(target.parent)
