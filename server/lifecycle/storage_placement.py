"""Offline-safe validation of restore-surviving accepted placement metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

MAX_MANIFEST_BYTES = 128 * 1024


class StoragePlacementRecoveryRequired(RuntimeError):
    pass


def validate_placement(value: dict, *, name: str | None = None) -> dict:
    try:
        catalog = value['catalog_path']
        parts = PurePosixPath(catalog).parts
        if (value.get('version') != 1 or not catalog.startswith('/media/')
            or len(parts) < 4 or parts[2] not in {'Movies', 'Series'}
            or any(part in {'.', '..'} for part in parts)
            or '\\' in catalog or '?' in catalog or '#' in catalog):
            raise ValueError()
        expected = hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if name is not None and name != expected + '.json':
            raise ValueError()
        if value['id'] != expected or not re.fullmatch('[a-f0-9]{64}', value['generation']):
            raise ValueError()
        if type(value['size_bytes']) is not int or value['size_bytes'] < 0:
            raise ValueError()
        if not value['remote_path'] or not value['remote_identity'] or not value['target_generation'] or not value['verified_at']:
            raise ValueError()
        identity = json.loads(value['local_identity'])
        if identity['size'] != value['size_bytes']:
            raise ValueError()
        if not value['media_id'] or value['kind'] not in {'video', 'audio'}:
            raise ValueError()
    except (KeyError, ValueError, TypeError) as exc:
        raise StoragePlacementRecoveryRequired('Accepted storage placement metadata requires recovery.') from exc
    return value


def validate_placement_manifests(root: Path, *, media_dir: Path | None = None) -> list[dict]:
    """No config/database imports, mutations, provider requests or rollback."""
    directory = (media_dir or Path(root) / 'server/media') / '.storage-placements'
    if directory.is_symlink():
        raise StoragePlacementRecoveryRequired('Unsafe accepted storage placement directory.')
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise StoragePlacementRecoveryRequired('Unsafe accepted storage placement directory.')
    values = []
    for path in directory.iterdir():
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_MANIFEST_BYTES or path.suffix != '.json':
                raise ValueError()
            values.append(validate_placement(json.loads(path.read_text()), name=path.name))
        except (OSError, ValueError, TypeError) as exc:
            raise StoragePlacementRecoveryRequired('Accepted storage placement metadata requires recovery.') from exc
    return values
