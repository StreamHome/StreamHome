"""Bounded inventory and durable accepted placement evidence.

Placement manifests are durable media metadata, deliberately outside SQLite and
ordinary operation cleanup. An old database restore cannot erase accepted cloud
ownership. A missing row is never authority to delete an object or catalog entry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings, _private_atomic_replace
from lifecycle.storage_retention import is_protected_path
import db as database
from lifecycle.coordination import EffectClass, run_cancellation_safe_thread, runtime_work
from models import Episode, Movie, PlaybackRun, StorageAsset, StorageMeasurement
from services.storage_common import StorageError, digest

MAX_INVENTORY_FILES = 100_000
MAX_MANIFEST_BYTES = 128 * 1024


def target_generation() -> str:
    """Credential token rotation does not change the selected Drive target."""
    receipt = Path(settings.BASE_DIR) / 'server/rclone/.admin-drive-publication.json'
    identity = None
    try:
        if not receipt.is_symlink() and receipt.stat().st_size <= MAX_MANIFEST_BYTES:
            from lifecycle.drive_connection import _record
            payload = _record(receipt.parent)
            if payload and payload.get('phase') == 'committed':
                identity = payload.get('identity')
    except (OSError, ValueError):
        pass
    return digest({'remote': settings.RCLONE_REMOTE_PATH, 'connection': identity})


def cloud_read_enabled() -> bool:
    # Connection, read health and write policy have separate owners. Existing
    # legacy CLOUD data remains readable, and pausing offload does not disconnect.
    from services.rclone import rclone_service
    path = Path(rclone_service.config_path)
    return settings.STORAGE_ENGINE == 'CLOUD' or bool(settings.RCLONE_REMOTE_PATH and path.is_file() and not path.is_symlink())


def hybrid_policy_active() -> bool:
    return isinstance(getattr(settings, 'STORAGE_POLICY', None), dict)


def _manifest_path(catalog_path: str) -> Path:
    from services.media_source import canonicalize_catalog_path
    canonical = canonicalize_catalog_path(catalog_path)
    return Path(settings.MEDIA_DIR) / '.storage-placements' / (digest(canonical) + '.json')


def file_identity(path: Path) -> str:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise StorageError('storage_unsafe_asset')
    return json.dumps({'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
                       'mtime_ns': info.st_mtime_ns}, sort_keys=True, separators=(',', ':'))


def accepted_placement(catalog_path: str) -> dict | None:
    try:
        path = _manifest_path(catalog_path)
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        from lifecycle.storage_placement import validate_placement, StoragePlacementRecoveryRequired
        try:
            validate_placement(value, name=path.name)
        except StoragePlacementRecoveryRequired:
            return None
        if value.get('version') != 1 or value.get('catalog_path') != catalog_path or value.get('retired'):
            return None
        if value.get('target_generation') != target_generation():
            return None
        if not value.get('generation') or not value.get('remote_identity') or not value.get('remote_path'):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def accepted_identity(catalog_path: str, local_path: Path | None = None,
                      cloud_identity: str | None = None,
                      audio_metadata=None) -> tuple[str | None, str | None]:
    value = accepted_placement(catalog_path)
    if value is None:
        return None, None
    if local_path is not None and local_path.is_file():
        try:
            if file_identity(local_path) != value['local_identity']:
                return None, None
            from services.media_source import local_media_identity
            source = value.get('source_fingerprint') if _media_identity_matches(catalog_path, local_path, value) else None
            if audio_metadata is not None and value.get('audio_metadata_digest') != digest(audio_metadata):
                source = None
            return source, value.get('video_fingerprint')
        except (OSError, ValueError):
            return None, None
    if cloud_identity is not None and cloud_identity == value['remote_identity']:
        source = value.get('source_fingerprint')
        if audio_metadata is not None and value.get('audio_metadata_digest') != digest(audio_metadata):
            source = None
        return source, value.get('video_fingerprint')
    return None, None


def _media_identity_matches(catalog_path: str, local_path: Path, value: dict) -> bool:
    from services.media_source import local_media_identity
    try:
        current = json.loads(local_media_identity(local_path).removeprefix('local:'))
        expected = json.loads(value['media_identity'].removeprefix('local:'))
        observed = {item['name']: item for item in current}
        for item in expected:
            name = item['name']
            if observed.get(name) == item:
                observed.pop(name)
                continue
            if name in observed or not name.startswith('audio/'):
                return False
            # Missing sidecars count as unchanged only with their own exact,
            # accepted generation. An unrelated remote filename is insufficient.
            sidecar_path = catalog_path.rsplit('/', 1)[0] + '/' + name
            placement = accepted_placement(sidecar_path)
            if not placement:
                return False
            identity = json.loads(placement['local_identity'])
            if identity['size'] != item['size'] or identity['mtime_ns'] != item['modified']:
                return False
        return not observed
    except (ValueError, KeyError, TypeError):
        return False


def invalidate_placement(catalog_path: str) -> None:
    """Retire accepted identity after an explicitly accepted content mutation.

    Historical ownership evidence remains durable for restore/operator review;
    it can no longer resurrect a deliberately removed/replaced asset.
    """
    value = accepted_placement(catalog_path)
    if value is not None:
        value.update(retired=True, retired_at=time.time())
        _private_atomic_replace(str(_manifest_path(catalog_path)), json.dumps(value, sort_keys=True) + '\n')


def accepted_fingerprint(catalog_path: str, kind: str = 'source') -> str | None:
    value = accepted_placement(catalog_path)
    return value.get('video_fingerprint' if kind == 'video' else 'source_fingerprint') if value else None


def persist_placement(asset: StorageAsset, media_identity: str, audio_metadata=None) -> None:
    """Called only after the DB receipt and exact remote verification are durable."""
    if not asset.remote_identity or not asset.verified_at or not asset.remote_path:
        raise StorageError('storage_unverified_placement')
    path = _manifest_path(asset.catalog_path)
    if path.parent.is_symlink():
        raise StorageError('storage_unsafe_placement')
    from lifecycle.state import qualify_local_filesystem
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    qualify_local_filesystem(path.parent, require_private=False, description='storage placement metadata')
    value = {field: getattr(asset, field) for field in (
        'id', 'media_id', 'episode_id', 'catalog_path', 'kind', 'generation', 'size_bytes',
        'local_identity', 'source_fingerprint', 'video_fingerprint', 'remote_path',
        'remote_identity', 'target_generation', 'verified_at', 'cold_recovery_ready')}
    value.update(version=1, media_identity=media_identity,
                 audio_metadata_digest=digest(audio_metadata or []))
    _private_atomic_replace(str(path), json.dumps(value, sort_keys=True) + '\n')


def _hash_file(path: Path) -> tuple[str, str, int]:
    before = file_identity(path)
    checksum = hashlib.sha256()
    # NOFOLLOW also protects the open between identity checks.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise StorageError('storage_unsafe_asset')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            while block := stream.read(1024 * 1024):
                checksum.update(block)
    finally:
        os.close(fd)
    if file_identity(path) != before:
        raise StorageError('storage_source_changed')
    return checksum.hexdigest(), before, path.stat().st_size


async def adopt_asset(media_id: str, episode_id: str | None, catalog_path: str,
                      kind: str = 'video', source_fingerprint: str | None = None,
                      video_fingerprint: str | None = None,
                      cold_recovery_ready: bool = False) -> StorageAsset | None:
    from services.media_source import local_path_for, local_media_identity, local_video_fingerprint
    path = local_path_for(catalog_path)
    identity = file_identity(path) if path.is_file() else None
    async with AsyncSession(database.engine, expire_on_commit=False) as db:
        previous = (await db.exec(select(StorageAsset).where(StorageAsset.catalog_path == catalog_path))).first()
        if previous and identity == previous.local_identity:
            if previous.cold_recovery_ready != cold_recovery_ready or (source_fingerprint and source_fingerprint != previous.source_fingerprint):
                previous.cold_recovery_ready = cold_recovery_ready
                if source_fingerprint:
                    previous.source_fingerprint = source_fingerprint
                db.add(previous)
                await db.commit()
            return previous
    placement = accepted_placement(catalog_path)
    if identity is None:
        if not placement:
            return previous
        # Rehydrate exact ownership after an old DB restore. This does not
        # claim current provider health; serving still verifies remote identity.
        allowed = set(StorageAsset.model_fields)
        restored = StorageAsset(**{key: value for key, value in placement.items() if key in allowed})
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            await db.merge(restored)
            await db.commit()
        return restored
    generation, identity, size = await run_cancellation_safe_thread(_hash_file, path)
    if source_fingerprint is None:
        source_fingerprint = hashlib.sha256(local_media_identity(path).encode()).hexdigest()[:32]
    asset = StorageAsset(
        id=digest(catalog_path), media_id=media_id, episode_id=episode_id,
        catalog_path=catalog_path, kind=kind, generation=generation,
        size_bytes=size, local_identity=identity, source_fingerprint=source_fingerprint,
        video_fingerprint=video_fingerprint or local_video_fingerprint(path),
        cold_recovery_ready=cold_recovery_ready,
    )
    async with AsyncSession(database.engine, expire_on_commit=False) as db:
        await db.merge(asset)
        await db.commit()
    return asset


async def record_usage(media_id: str, episode_id: str | None) -> None:
    """Only real viewing history, never file creation/mtime, orders cold media."""
    from sqlalchemy import func
    async with AsyncSession(database.engine, expire_on_commit=False) as db:
        last = (await db.exec(select(func.max(PlaybackRun.last_progress_at)).where(
            PlaybackRun.movie_id == media_id, PlaybackRun.episode_id == episode_id))).first()
        assets = (await db.exec(select(StorageAsset).where(StorageAsset.media_id == media_id,
            StorageAsset.episode_id == episode_id))).all()
        for asset in assets:
            asset.last_used_at = float(last or 0)
            db.add(asset)
        await db.commit()


def managed_roots() -> list[tuple[str, Path]]:
    server = Path(settings.BASE_DIR) / 'server'
    return [('media', Path(settings.MEDIA_DIR)), ('temporary', Path(settings.TEMP_DIR)),
            ('backup', server / 'backup'), ('recovery', Path(settings.BASE_DIR) / '.run'),
            ('configuration', server / 'rclone'), ('database', Path(settings.db_path)),
            ('database', Path(settings.db_path + '-wal')), ('database', Path(settings.db_path + '-shm')),
            ('database', Path(settings.db_path + '-journal')),
            ('configuration', server / 'settings.json'), ('configuration', server / '.env'),
            ('configuration', Path(settings.BASE_DIR) / '.env')]


def scan_managed(*, max_files: int = MAX_INVENTORY_FILES, include_entries: bool = False) -> dict:
    """Bounded scan counts hardlinks/overlapping roots once; separate copies twice."""
    seen: set[tuple[int, int]] = set()
    categories: dict[str, int] = {}
    retained_sizes: dict[tuple[int, int], int] = {}
    filesystems: dict[str, int] = {}
    count = 0
    complete = True
    error = None
    entries = []
    inode_sizes = {}
    def scan_error(failure: OSError) -> None:
        # os.walk otherwise suppresses permission and I/O failures, turning an
        # unreadable managed subtree into a falsely complete zero-byte scan.
        raise failure

    for category, root in managed_roots():
        try:
            root_state = root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            complete, error = False, 'storage_inventory_unreadable'
            continue
        if stat.S_ISLNK(root_state.st_mode):
            complete, error = False, 'storage_unsafe_inventory_path'
            continue
        paths = (item for directory, dirs, names in os.walk(root, followlinks=False, onerror=scan_error)
                 for item in [Path(directory) / name for name in names]) if stat.S_ISDIR(root_state.st_mode) else iter([root])
        try:
            for path in paths:
                count += 1
                if count > max_files:
                    complete, error = False, 'storage_inventory_limit'
                    break
                if path.is_symlink():
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                identity = (info.st_dev, info.st_ino)
                inode_sizes.setdefault(identity, info.st_size)
                if include_entries:
                    # Aliases carry the same first-observed size used by the
                    # global sum even if a native writer grows between stats.
                    inode_sizes.setdefault(identity, info.st_size)
                    entries.append((os.path.abspath(path), info.st_dev, info.st_ino, inode_sizes[identity]))
                if is_protected_path(path):
                    retained_sizes.setdefault(identity, inode_sizes[identity])
                if identity in seen:
                    continue
                seen.add(identity)
                key = 'placement_metadata' if '.storage-placements' in path.parts else category
                categories[key] = categories.get(key, 0) + info.st_size
                device = str(info.st_dev)
                filesystems[device] = filesystems.get(device, 0) + info.st_size
        except OSError:
            complete, error = False, 'storage_inventory_unreadable'
        if count > max_files:
            break
    result = {'written_bytes': sum(categories.values()) if complete else None,
            'observed_bytes': sum(categories.values()), 'categories': categories,
            'retained_bytes': sum(retained_sizes.values()) if complete else None,
            'filesystems': filesystems, 'complete': complete, 'error_code': error,
            'measured_at': time.time()}
    if include_entries:
        result['_entries'] = entries
    return result


class StorageInventory:
    def __init__(self):
        self.lock = asyncio.Lock()

    async def local_measurement(self, *, include_entries: bool = False) -> dict:
        return await run_cancellation_safe_thread(scan_managed, include_entries=include_entries)

    async def reconcile_accepted_placements(self) -> int:
        from lifecycle.storage_placement import validate_placement_manifests
        values = await run_cancellation_safe_thread(validate_placement_manifests, Path(settings.BASE_DIR), media_dir=Path(settings.MEDIA_DIR))
        count = 0
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            for value in values:
                if value.get('retired'):
                    continue
                previous = await db.get(StorageAsset, value['id'])
                if previous and previous.generation != value['generation']:
                    # An older DB may reference a different content generation;
                    # retain both evidence and its explicit unresolved outcome.
                    continue
                if previous is None or not previous.remote_identity:
                    allowed = set(StorageAsset.model_fields)
                    await db.merge(StorageAsset(**{key: val for key, val in value.items() if key in allowed}))
                    count += 1
            await db.commit()
        return count

    async def reconcile_catalog(self, *, limit: int = 200) -> int:
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            cursor = await db.get(StorageMeasurement, 'inventory_cursor')
            after = json.loads(cursor.payload).get('after', '') if cursor else ''
            movies_query = select(Movie).where(Movie.catalog_source == 'server', Movie.type != 'series').order_by(Movie.id)
            episodes_query = select(Episode).order_by(Episode.id)
            movies = (await db.exec(movies_query.limit(limit + 1))).all() if after < 'movie:' else (await db.exec(movies_query.where(Movie.id > after.removeprefix('movie:')).limit(limit + 1))).all()
            episodes = (await db.exec(episodes_query.limit(limit + 1))).all() if after < 'episode:' else ((await db.exec(episodes_query.where(Episode.id > after.removeprefix('episode:')).limit(limit + 1))).all() if after.startswith('episode:') else [])
            entries = [('movie:' + movie.id, movie.id, None, movie.video_url, movie.source_fingerprint, movie) for movie in movies]
            entries += [('episode:' + episode.id, episode.movie_id, episode.id, episode.video_url, episode.source_fingerprint, episode) for episode in episodes]
            entries.sort(key=lambda value: value[0])
            batch, has_more = entries[:limit], len(entries) > limit
        count = 0
        for key, media_id, episode_id, path, source_fingerprint, media in batch:
            if not str(path).startswith('/media/'):
                continue
            try:
                from lifecycle.storage_worker import direct_recovery_proven
                asset = await adopt_asset(media_id, episode_id, path, source_fingerprint=source_fingerprint,
                    cold_recovery_ready=direct_recovery_proven(media))
                if asset:
                    count += 1
                from services.media_source import local_path_for, EXTERNAL_AUDIO_EXTENSIONS
                audio_dir = local_path_for(path).parent / 'audio'
                if audio_dir.is_dir():
                    for audio in sorted(audio_dir.iterdir())[:64]:
                        if audio.is_file() and audio.suffix.lower() in EXTERNAL_AUDIO_EXTENSIONS:
                            audio_catalog = '/media/' + audio.relative_to(Path(settings.MEDIA_DIR)).as_posix()
                            await adopt_asset(media_id, episode_id, audio_catalog, 'audio', cold_recovery_ready=direct_recovery_proven(media))
                await record_usage(media_id, episode_id)
            except (StorageError, OSError, ValueError):
                continue
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            await db.merge(StorageMeasurement(id='inventory_cursor', payload=json.dumps({'after': batch[-1][0] if batch and has_more else ''}), measured_at=time.time()))
            await db.commit()
        return count


storage_inventory = StorageInventory()
