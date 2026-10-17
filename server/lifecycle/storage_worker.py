"""Bounded, owned hybrid storage work with copy/verify/accept/release ordering."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
import db as database
from lifecycle.coordination import AdmissionClosedError, EffectClass, run_cancellation_safe_thread, runtime_work
from lifecycle.media_work import media_ownership, require_resolved_media, settle
from lifecycle.storage_placement import validate_placement_manifests
from models import (Episode, Movie, PlaybackRun, StorageAsset, StorageKeepLocal,
                    StorageMeasurement, StorageOperation, StorageReview)
from services.storage_common import StorageError, digest, policy_value, CLEANUP_RETENTION_VERSION, validate_cleanup_retention
from lifecycle.storage_retention import is_protected_path
from services.storage_capacity import storage_capacity
from services.storage_inventory import (accepted_placement, adopt_asset, cloud_read_enabled, file_identity,
    persist_placement, storage_inventory, target_generation, _hash_file)


async def media_protection_reason(media_id: str, episode_id: str | None = None, *, include_pin: bool = True) -> str | None:
    from services.playback_prep import playback_prep_service
    key = episode_id or media_id
    if await playback_prep_service.is_media_protected(key):
        return 'active_playback'
    try:
        require_resolved_media(media_id)
        if episode_id:
            require_resolved_media(episode_id)
    except Exception:
        return 'recovery_required'
    async with AsyncSession(database.engine, expire_on_commit=False) as db:
        # A long Pause is still an open playback lifetime, independent of TTLs.
        run_query = select(PlaybackRun).where(PlaybackRun.movie_id == media_id,
            PlaybackRun.lifecycle_state.in_(['active', 'paused']))
        if episode_id is not None:
            run_query = run_query.where(PlaybackRun.episode_id == episode_id)
        active = (await db.exec(run_query)).first()
        if active:
            return 'active_playback'
        if include_pin:
            pin = (await db.exec(select(StorageKeepLocal).where(StorageKeepLocal.media_id == media_id,
                StorageKeepLocal.enabled == True))).all()
            if any(item.episode_id is None or item.episode_id == episode_id for item in pin):
                return 'keep_local'
    return None


def direct_recovery_proven(media) -> bool:
    from services.media_analysis import current_duration_analysis, stored_analysis
    from services.media_source import local_path_for, local_video_fingerprint
    analysis = stored_analysis(media.analysis_metadata_str)
    try:
        path = local_path_for(media.video_url)
        if path.is_file():
            observed_video = local_video_fingerprint(path)
        else:
            placement = accepted_placement(media.video_url)
            observed_video = placement.get('video_fingerprint') if placement else None
    except (OSError, ValueError):
        return False
    embedded = [item for item in (media.audio_metadata or []) if item.get('source', 'embedded') == 'embedded']
    audio = next((item for item in embedded if item.get('default')), embedded[0] if embedded else {})
    return bool((Path(media.video_url).suffix.lower() == '.mp4' or 'mp4' in (media.container or '').lower())
        and (media.codec or '').lower() in {'h264', 'avc', 'avc1'}
        and str(audio.get('codec') or '').lower() in {'', 'aac', 'mp4a', 'mp3'}
        and current_duration_analysis(analysis) and analysis.get('direct_timeline_compatible') is True
        and analysis.get('source_fingerprint') == media.source_fingerprint
        and observed_video is not None and analysis.get('video_fingerprint') == observed_video)


def selected_external_audio(media) -> list[dict]:
    tracks = media.audio_metadata or []
    selected = next((item for item in tracks if item.get('default')), tracks[0] if tracks else None)
    return [selected] if selected and selected.get('source') == 'external' else []


async def offline_ready(media, kind: str) -> bool:
    from services.media_source import local_path_for
    from services.playback_prep import playback_prep_service
    try:
        path = local_path_for(media.video_url)
        if not path.is_file():
            return False
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            asset = (await db.exec(select(StorageAsset).where(StorageAsset.catalog_path == media.video_url))).first()
        if not asset or asset.local_identity != file_identity(path):
            return False
        for audio in selected_external_audio(media):
            name = audio.get('fileName') or audio.get('file_name')
            if not name or Path(name).name != name or not (path.parent / 'audio' / name).is_file():
                return False
            catalog = media.video_url.rsplit('/', 1)[0] + '/audio/' + name
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                audio_asset = (await db.exec(select(StorageAsset).where(StorageAsset.catalog_path == catalog))).first()
            if not audio_asset or audio_asset.kind != 'audio' or audio_asset.local_identity != file_identity(path.parent / 'audio' / name):
                return False
        return direct_recovery_proven(media) or bool(media.source_fingerprint and
            playback_prep_service.playback_ready(str(media.id), media.source_fingerprint, media))
    except (OSError, ValueError):
        return False


class StorageWorker:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.event = asyncio.Event()
        self.stopping = False
        self.last_automatic = 0.0
        from lifecycle.storage_measurements import StorageMeasurements
        self.measurements = StorageMeasurements(self)

    def wake(self):
        self.event.set()

    async def start(self, *, measurements: bool = True):
        if self.task and not self.task.done():
            return
        validate_placement_manifests(Path(settings.BASE_DIR), media_dir=Path(settings.MEDIA_DIR))
        self.stopping = False
        self.event = asyncio.Event()
        # Interrupted transfers keep their evidence/reservations. Only operations
        # whose effects can be positively reconciled are retried below.
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            rows = (await db.exec(select(StorageOperation).where(StorageOperation.status == 'running'))).all()
            for row in rows:
                if row.kind in {'refresh', 'keep_local'}:
                    row.status, row.phase = 'queued', 'recovering'
                else:
                    row.status, row.phase, row.error_code = 'blocked', 'recovery_required', 'storage_operation_interrupted'
                row.updated_at = time.time()
                db.add(row)
            await db.commit()
        if measurements:
            await self.measurements.start()
        self.task = asyncio.create_task(self._run(), name='storage-maintenance-worker')
        self.wake()

    async def stop(self):
        self.stopping = True
        await self.measurements.stop()
        self.wake()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self):
        while not self.stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except AdmissionClosedError:
                pass
            except Exception:
                # Never put provider output/paths into API-visible errors.
                from services.logger import logger
                logger.warning('[Storage] A maintenance iteration stopped; durable evidence was retained.')
            self.event.clear()
            try:
                await asyncio.wait_for(self.event.wait(), timeout=15)
            except TimeoutError:
                pass

    async def _set(self, operation: StorageOperation, **fields):
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            row = await db.get(StorageOperation, operation.id)
            if row is None:
                raise StorageError('storage_operation_missing')
            for key, value in fields.items():
                setattr(row, key, value)
                setattr(operation, key, value)
            row.updated_at = operation.updated_at = time.time()
            db.add(row)
            await db.commit()

    async def tick(self):
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            operation = (await db.exec(select(StorageOperation).where(StorageOperation.status == 'queued')
                .order_by(StorageOperation.created_at).limit(1))).first()
        if operation:
            try:
                async with runtime_work('storage.' + operation.kind, [EffectClass.DB_WRITE,
                    EffectClass.DURABLE_MEDIA_WRITE, EffectClass.DISPOSABLE_CACHE_WRITE, EffectClass.CLOUD_WRITE]):
                    await self._set(operation, status='running', phase='starting')
                    if operation.kind == 'refresh':
                        await self._set(operation, phase='measuring_local')
                        await self.measure_local()
                        if cloud_read_enabled():
                            await self._set(operation, phase='measuring_cloud')
                            await self.measure_cloud()
                        await self._set(operation, phase='reconciling_assets')
                        await storage_inventory.reconcile_catalog()
                        await self._set(operation, status='completed', phase='measured')
                        async with AsyncSession(database.engine, expire_on_commit=False) as db:
                            aliases = (await db.exec(select(StorageOperation).where(StorageOperation.kind == 'refresh',
                                StorageOperation.status == 'queued').limit(50))).all()
                            for alias in aliases:
                                alias.status, alias.phase, alias.updated_at = 'completed', 'measured', time.time()
                                db.add(alias)
                            await db.commit()
                    elif operation.kind in {'keep_local', 'prefetch'}:
                        await self.keep_local(operation)
                    elif operation.kind == 'cleanup':
                        await self.execute_cleanup(operation)
                    elif operation.kind == 'offload':
                        await self.offload(operation)
                    else:
                        raise StorageError('storage_unknown_operation')
            except asyncio.CancelledError:
                await settle(self._set(operation, status='blocked', phase='interrupted', error_code='storage_operation_interrupted'))
                raise
            except StorageError as exc:
                await self._set(operation, status='blocked', phase='blocked', error_code=exc.code, message=exc.message)
            except Exception:
                await self._set(operation, status='failed', phase='failed', error_code='storage_operation_failed',
                    message='The operation stopped safely. Local originals and recovery evidence were retained.')
            return
        if time.monotonic() - self.last_automatic < 60:
            return
        self.last_automatic = time.monotonic()
        async with runtime_work('storage.reconciliation', [EffectClass.DB_WRITE, EffectClass.CLOUD_WRITE, EffectClass.DURABLE_MEDIA_WRITE]):
            await self.reconcile_transfers()
            if policy_value(settings)['prefetch_next_episode']:
                await self.schedule_prefetch()
        if not policy_value(settings)['offload_enabled']:
            return
        async with runtime_work('storage.plan', [EffectClass.DB_WRITE, EffectClass.DISPOSABLE_CACHE_WRITE]):
            await self.measure()
            policy = policy_value(settings)
            local = await self._measurement('local')
            written = local.get('written_bytes')
            reserved = await storage_capacity.remaining()
            cycle = await self._measurement('offload_cycle')
            pressure = await self._measurement('capacity_pressure')
            if written is None or policy['local_budget_bytes'] is None:
                return
            effective = written + reserved
            pressing = pressure.get('active') is True and pressure.get('revision') == policy['revision']
            target = policy['local_budget_bytes'] * .8
            if pressing:
                target = min(target, max(0, policy['local_budget_bytes'] - pressure['additional_bytes']))
                if pressure.get('error_code') == 'storage_disk_space_insufficient':
                    free = (await run_cancellation_safe_thread(shutil.disk_usage, pressure['path'])).free
                    deficit = max(0, pressure['additional_bytes'] + policy['disk_reserve_bytes'] + reserved - free)
                    target = min(target, max(0, effective - deficit))
            cycling = cycle.get('active') is True
            if cycling and effective <= target:
                await self._save_measurement('offload_cycle', {'active': False})
                if pressing:
                    await self._save_measurement('capacity_pressure', dict(pressure, active=False))
                return
            if not cycling and not pressing and effective < policy['local_budget_bytes'] * .9:
                return
            await self._save_measurement('offload_cycle', {'active': True})
            plan = await self.cleanup_plan()
            if pressing and pressure.get('error_code') == 'storage_disk_space_insufficient':
                plan['candidates'] = [item for item in plan['candidates'] if item['filesystem'] == pressure['filesystem']]
            if plan['candidates']:
                row = StorageOperation(id=str(uuid.uuid4()), kind='cleanup', payload=json.dumps({
                    'plan': plan, 'revision': policy['revision'], 'target_generation': target_generation()}))
            else:
                candidate = await self.cold_candidate(filesystem=pressure['filesystem'] if pressing and pressure.get('error_code') == 'storage_disk_space_insufficient' else None)
                if candidate is None:
                    # Durable blocked state reports why the target cannot be
                    # met; a changed inventory/protection can reopen planning.
                    await self._save_measurement('offload_cycle', {'active': True, 'blocker': 'storage_retained_data_blocks_capacity', 'measured_at': local.get('measured_at')})
                    return
                row = StorageOperation(id=str(uuid.uuid4()), kind='offload', media_id=candidate.media_id,
                    bytes_total=candidate.size_bytes, payload=json.dumps({'asset_id': candidate.id,
                        'generation': candidate.generation, 'revision': policy['revision'], 'target_generation': target_generation()}))
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                db.add(row)
                await db.commit()
            self.wake()

    async def _measurement(self, name):
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            row = await db.get(StorageMeasurement, name)
            return json.loads(row.payload) if row else {}

    async def measure(self):
        # Dashboard metrics are available before expensive asset adoption hashes.
        await self.measure_local()
        if cloud_read_enabled():
            await self.measure_cloud()
        await storage_inventory.reconcile_catalog()

    async def measure_local(self):
        await self.measurements.measure('local', fresh=True)

    async def _measure_local(self):
        local = await storage_inventory.local_measurement()
        if local.get('written_bytes') is None:
            raise StorageError(local.get('error_code') or 'storage_measurement_unknown')
        local['protected_bytes'] = await self.protected_floor(measured=local)
        from services.storage_capacity import filesystem_for
        _, existing = filesystem_for(settings.MEDIA_DIR)
        local['free_bytes'] = (await run_cancellation_safe_thread(shutil.disk_usage, existing)).free
        await self._save_measurement('local', local)

    async def protected_floor(self, measured=None):
        """Fresh conservative protected floor, without provider or write I/O."""
        from services.media_source import local_path_for
        if measured is None:
            measured = await storage_inventory.local_measurement()
        if measured['written_bytes'] is None:
            raise StorageError('storage_measurement_unknown', 'Local usage could not be measured. Refresh storage and review again.')
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            assets = (await db.exec(select(StorageAsset))).all()
        eligible, identities = 0, set()
        for asset in assets:
            path = local_path_for(asset.catalog_path)
            if asset.kind not in {'video', 'audio'} or is_protected_path(path):
                continue
            if not path.is_file() or await media_protection_reason(asset.media_id, asset.episode_id):
                continue
            media = await self._media(asset)
            # The floor must not promise HLS recovery under a new, smaller
            # budget before its full-spool admission is established.
            if not media or not direct_recovery_proven(media) or file_identity(path) != asset.local_identity:
                continue
            info = path.stat()
            # Removing one alias cannot free bytes retained by another path.
            if info.st_nlink > 1:
                continue
            identity = (info.st_dev, info.st_ino)
            if identity not in identities:
                identities.add(identity)
                eligible += info.st_size
        return max(0, measured['written_bytes'] - eligible)

    async def _save_measurement(self, name, payload):
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            await db.merge(StorageMeasurement(id=name, payload=json.dumps(payload), measured_at=time.time()))
            await db.commit()

    async def measure_cloud(self):
        await self.measurements.measure('cloud', fresh=True)

    async def _measure_cloud(self):
        # Provider I/O serializes cloud accounting only. Local admission and
        # cached dashboard reads never wait for a cloud network timeout.
        async with storage_capacity.cloud_lock:
            try:
                await self._measure_cloud_locked()
            except BaseException:
                # A failed new observation cannot leave an earlier dashboard
                # value eligible for admission, even within its 60-second TTL.
                await settle(self._invalidate_cloud_capacity())
                raise

    async def _invalidate_cloud_capacity(self):
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            row = await db.get(StorageMeasurement, 'cloud')
            if row:
                value = json.loads(row.payload)
                value['capacity_valid'] = False
                row.payload = json.dumps(value)
                db.add(row)
                await db.commit()

    async def _measure_cloud_locked(self):
        from services.rclone import rclone_service
        # size is aggregated by Rclone without returning an unbounded listing.
        remote = settings.RCLONE_REMOTE_PATH
        generation = target_generation()
        self.measurements.phase('cloud', 'measuring_folder')
        size = await rclone_service.run('size', remote, '--json', timeout=60, hard_output_limit=65536)
        if not size.ok:
            raise StorageError(size.error_code or 'storage_cloud_measurement_failed')
        self.measurements.phase('cloud', 'measuring_account')
        about = await rclone_service.run('about', remote.split(':', 1)[0] + ':', '--json', timeout=30, hard_output_limit=65536)
        value = {'target_generation': generation, 'used_bytes': None, 'account_total_bytes': None,
                 'account_free_bytes': None, 'reachable': bool(size.ok), 'error_code': size.error_code, 'capacity_valid': True}
        try:
            if size.ok:
                result = json.loads(size.stdout)
                amount = result.get('bytes')
                if type(amount) is int and amount >= 0:
                    value['used_bytes'] = amount
            if about.ok:
                result = json.loads(about.stdout)
                for key, field in [('total', 'account_total_bytes'), ('free', 'account_free_bytes')]:
                    amount = result.get(key)
                    if type(amount) is int and amount >= 0:
                        value[field] = amount
            elif not value['error_code']:
                value['error_code'] = about.error_code
        except (ValueError, TypeError):
            value['error_code'] = 'storage_cloud_measurement_invalid'
        if target_generation() != generation:
            raise StorageError('storage_target_changed')
        if value['used_bytes'] is None or value['error_code']:
            raise StorageError(value['error_code'] or 'storage_cloud_measurement_invalid')
        await self._save_measurement('cloud', value)

    async def schedule_prefetch(self):
        """Prepare one actual next episode for the latest open series run."""
        from sqlalchemy import or_, and_
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            runs = (await db.exec(select(PlaybackRun).where(PlaybackRun.lifecycle_state == 'active',
                PlaybackRun.episode_id.is_not(None)).order_by(PlaybackRun.updated_at.desc()).limit(10))).all()
            for run in runs:
                current = await db.get(Episode, run.episode_id)
                if not current:
                    continue
                following = (await db.exec(select(Episode).where(Episode.movie_id == run.movie_id,
                    or_(Episode.season_number > current.season_number,
                        and_(Episode.season_number == current.season_number, Episode.episode_number > current.episode_number)))
                    .order_by(Episode.season_number, Episode.episode_number).limit(1))).first()
                if not following or not following.video_url or not following.source_fingerprint:
                    continue
                identifier = digest({'run': run.id, 'next': following.id, 'generation': following.source_fingerprint})
                if await db.get(StorageOperation, identifier):
                    continue
                row = StorageOperation(id=identifier, kind='prefetch', media_id='episode:' + following.id,
                    title=following.title, payload=json.dumps({'retention_id': 'episode:' + following.id,
                        'revision': policy_value(settings)['revision'], 'source_fingerprint': following.source_fingerprint}))
                db.add(row)
                await db.commit()
                self.wake()
                return

    async def _media(self, asset):
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            return await db.get(Episode if asset.episode_id else Movie, asset.episode_id or asset.media_id)

    async def recovery_proven(self, media, asset=None):
        if direct_recovery_proven(media):
            return True
        from services.media_source import resolve_media_source
        from services.playback_prep import playback_prep_service
        source = await resolve_media_source(media.video_url)
        return await playback_prep_service.cold_recovery_ready(media, source)

    async def cold_candidate(self, *, filesystem=None):
        from services.media_source import local_path_for
        from sqlalchemy import and_, or_
        cursor = await self._measurement('cold_candidate_cursor')
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            query = select(StorageAsset).order_by(StorageAsset.last_used_at, StorageAsset.id)
            if cursor.get('id'):
                query = query.where(or_(StorageAsset.last_used_at > cursor['last_used_at'],
                    and_(StorageAsset.last_used_at == cursor['last_used_at'], StorageAsset.id > cursor['id'])))
            assets = (await db.exec(query.limit(200))).all()
        for asset in assets:
            if asset.kind not in {'video', 'audio'} or is_protected_path(local_path_for(asset.catalog_path)):
                continue
            if not local_path_for(asset.catalog_path).is_file() or local_path_for(asset.catalog_path).stat().st_nlink > 1:
                continue
            if filesystem is not None and str(local_path_for(asset.catalog_path).stat().st_dev) != filesystem:
                continue
            if await media_protection_reason(asset.media_id, asset.episode_id):
                continue
            media = await self._media(asset)
            if not media:
                continue
            # Source-bound direct proof is sufficient for protected Range
            # recovery. HLS remains local until an explicit recovery proof exists.
            video_ready = await self.recovery_proven(media, asset)
            if video_ready:
                asset.cold_recovery_ready = True
                async with AsyncSession(database.engine, expire_on_commit=False) as db:
                    await db.merge(asset)
                    await db.commit()
                await self._save_measurement('cold_candidate_cursor', {'id': asset.id, 'last_used_at': asset.last_used_at})
                return asset
        await self._save_measurement('cold_candidate_cursor', {'id': assets[-1].id, 'last_used_at': assets[-1].last_used_at} if len(assets) == 200 else {})
        return None

    async def cleanup_plan(self):
        from services.media_source import local_path_for
        candidates = []
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            assets = (await db.exec(select(StorageAsset).order_by(StorageAsset.last_used_at).limit(200))).all()
        for asset in assets:
            path = local_path_for(asset.catalog_path)
            if asset.kind not in {'video', 'audio'} or is_protected_path(path):
                continue
            if await media_protection_reason(asset.media_id, asset.episode_id):
                continue
            media = await self._media(asset)
            if not media or not await self.recovery_proven(media, asset):
                continue
            placement = accepted_placement(asset.catalog_path)
            if placement and path.is_file() and path.stat().st_nlink == 1:
                candidates.append({'kind': 'verified_duplicate', 'asset_id': asset.id,
                    'generation': asset.generation, 'bytes': asset.size_bytes, 'identity': asset.local_identity,
                    'filesystem': str(path.stat().st_dev)})
        return {'retention_version': CLEANUP_RETENTION_VERSION, 'candidates': candidates[:25],
                'blocker': None if candidates else 'storage_no_releasable_media'}

    @staticmethod
    def _tree_identity(root):
        entries = []
        total = 0
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise StorageError('storage_unsafe_cache')
            if path.is_file():
                info = path.stat()
                total += info.st_size
                entries.append((str(path.relative_to(root)), info.st_size, info.st_mtime_ns, info.st_ino))
            if len(entries) > 100_000:
                raise StorageError('storage_inventory_limit')
        return total, digest(entries)

    async def execute_cleanup(self, operation):
        from services.media_source import local_path_for
        payload = json.loads(operation.payload)
        plan = payload.get('plan')
        validate_cleanup_retention(plan)
        if payload['revision'] != policy_value(settings)['revision'] or payload['target_generation'] != target_generation():
            raise StorageError('storage_cleanup_plan_expired')
        # Preflight every asset before executing even the first release. Legacy
        # jobs and a mixed protected plan must never partially consume data.
        assets = []
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            for item in plan['candidates']:
                asset = await db.get(StorageAsset, item['asset_id'])
                if not asset or asset.generation != item['generation']:
                    raise StorageError('storage_source_changed')
                if asset.kind not in {'video', 'audio'} or is_protected_path(local_path_for(asset.catalog_path)):
                    raise StorageError('storage_retained_data_protected', 'Caches and subtitles cannot be released by cleanup.')
                assets.append(asset)
        for asset in assets:
            await self._verified_release(operation, asset, payload['revision'])
        await self._set(operation, status='completed', phase='completed')
        await self.measure_local()

    async def _verify_remote(self, asset, remote):
        from services.media_source import local_path_for, cloud_object_info
        from services.rclone import rclone_service
        path = local_path_for(asset.catalog_path)
        # --download compares bytes even where the provider lacks a common hash.
        # Both temporary and final objects retain the exact local basename.
        if path.is_file():
            checked = await rclone_service.run('check', str(path.parent), remote.rsplit('/', 1)[0],
                '--one-way', '--download', '--include', '/' + path.name, '--checkers', '1',
                timeout=3600, hard_output_limit=65536)
            if not checked.ok:
                raise StorageError(checked.error_code or 'storage_verification_failed', 'Remote content verification failed; the local original was retained.')
        # A filter or backend capability can make a directory check compare no
        # file at all. Independently read the exact object and compare its full
        # accepted SHA-256 generation; name/size alone never authorize release.
        process, chunks = await rclone_service.open_stream('cat', remote)
        checksum, count = hashlib.sha256(), 0
        try:
            async with asyncio.timeout(3600):
                async for block in chunks:
                    count += len(block)
                    if count > asset.size_bytes:
                        raise StorageError('storage_verification_failed')
                    checksum.update(block)
        finally:
            await settle(chunks.aclose())
        if process.returncode != 0 or count != asset.size_bytes or checksum.hexdigest() != asset.generation:
            raise StorageError('storage_verification_failed', 'Remote content verification failed; the local original was retained.')
        info = await cloud_object_info(remote, refresh=True)
        if not info or info.size != asset.size_bytes:
            raise StorageError('storage_remote_identity_unverified')
        return info

    async def offload(self, operation):
        from services.media_source import local_path_for, local_media_identity
        from services.rclone import rclone_service
        payload = json.loads(operation.payload)
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            asset = await db.get(StorageAsset, payload['asset_id'])
        policy = policy_value(settings)
        if (not asset or asset.generation != payload['generation'] or policy['revision'] != payload['revision']
            or target_generation() != payload['target_generation'] or not policy['offload_enabled']):
            raise StorageError('storage_policy_changed')
        if asset.kind not in {'video', 'audio'} or is_protected_path(local_path_for(asset.catalog_path)):
            raise StorageError('storage_retained_data_protected', 'Caches and subtitles cannot be offloaded or released.')
        if await media_protection_reason(asset.media_id, asset.episode_id):
            raise StorageError('storage_media_protected')
        media = await self._media(asset)
        if not media or not await self.recovery_proven(media, asset):
            raise StorageError('storage_cold_recovery_unproven')
        path = local_path_for(asset.catalog_path)
        if file_identity(path) != asset.local_identity:
            raise StorageError('storage_source_changed')
        original_media_identity = local_media_identity(path)
        await self.measure_cloud()
        reservation = await storage_capacity.reserve('storage.offload', asset.size_bytes * 2,
            settings.RCLONE_REMOTE_PATH, scope='cloud', operation_id=operation.id)
        candidate = settings.RCLONE_REMOTE_PATH.rstrip('/') + '/.storage-candidates/' + operation.id + '/' + path.name
        final = settings.RCLONE_REMOTE_PATH.rstrip('/') + '/.storage-assets/' + asset.id + '/' + asset.generation + '/' + path.name
        payload.update(candidate=candidate, final=final, reservation_id=reservation,
            media_identity=original_media_identity, audio_metadata=media.audio_metadata)
        await self._set(operation, phase='uploading', payload=json.dumps(payload))
        # On failure we retain the reservation: an uncertain provider copy still
        # consumes capacity. Explicit observed cleanup/reconciliation releases it.
        result = await rclone_service.run('copyto', str(path), candidate, timeout=3600, hard_output_limit=65536)
        if not result.ok:
            raise StorageError(result.error_code or 'storage_upload_failed')
        await storage_capacity.progress(reservation, asset.size_bytes)
        await self._set(operation, phase='verifying', bytes_done=asset.size_bytes)
        await self._verify_remote(asset, candidate)
        if file_identity(path) != asset.local_identity or local_media_identity(path) != original_media_identity:
            raise StorageError('storage_source_changed')
        result = await rclone_service.run('copyto', candidate, final, timeout=3600, hard_output_limit=65536)
        if not result.ok:
            raise StorageError(result.error_code or 'storage_publication_uncertain')
        await storage_capacity.progress(reservation, asset.size_bytes * 2)
        info = await self._verify_remote(asset, final)
        asset.remote_path, asset.remote_identity = final, info.identity
        asset.target_generation, asset.verified_at = payload['target_generation'], time.time()
        asset.cold_recovery_ready = True
        asset.updated_at = time.time()
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            await db.merge(asset)
            row = await db.get(StorageOperation, operation.id)
            row.phase = 'accepted'
            row.payload = json.dumps(payload)
            db.add(row)
            await db.commit()
        # Durable metadata survives old SQLite backup restore and ordinary job
        # cleanup. No local unlink may precede this synchronization.
        await run_cancellation_safe_thread(persist_placement, asset, original_media_identity, media.audio_metadata)
        await self._verified_release(operation, asset, payload['revision'])
        cleanup = await rclone_service.run('deletefile', candidate, timeout=60, hard_output_limit=65536)
        if cleanup.ok:
            observed = await rclone_service.run('lsjson', candidate, '--stat', timeout=30, hard_output_limit=65536)
            if observed.error_code == 'drive_not_found':
                await storage_capacity.release(reservation)
                await self._set(operation, status='completed', phase='completed')
                await self.measure_cloud()
                return
        # Final media is never deleted to compensate a cleanup failure.
        await self._set(operation, status='completed', phase='cleanup_debt', error_code='storage_candidate_cleanup_pending',
            message='The verified local release completed; temporary cloud cleanup still needs confirmation.')

    async def _verified_release(self, operation, asset, revision):
        from services.media_source import local_path_for
        path = local_path_for(asset.catalog_path)
        if asset.kind not in {'video', 'audio'} or is_protected_path(path):
            raise StorageError('storage_retained_data_protected', 'Caches and subtitles cannot be released.')
        if not path.exists():
            return
        if path.stat().st_nlink > 1:
            raise StorageError('storage_retained_data_protected', 'A retained alias shares this source; no space can be released.')
        placement = accepted_placement(asset.catalog_path)
        if not placement or placement['generation'] != asset.generation:
            raise StorageError('storage_placement_unaccepted')
        await self._set(operation, phase='verifying')
        await self._verify_remote(asset, placement['remote_path'])
        async with media_ownership(asset.media_id, *([asset.episode_id] if asset.episode_id else [])):
            policy = policy_value(settings)
            if policy['revision'] != revision or (operation.kind == 'offload' and not policy['offload_enabled']):
                raise StorageError('storage_policy_changed')
            if target_generation() != asset.target_generation:
                raise StorageError('storage_target_changed')
            if await media_protection_reason(asset.media_id, asset.episode_id):
                raise StorageError('storage_media_protected')
            media = await self._media(asset)
            if not media or not await self.recovery_proven(media, asset):
                raise StorageError('storage_cold_recovery_unproven')
            if file_identity(path) != asset.local_identity:
                raise StorageError('storage_source_changed')
            # Verify exact content generation again. Stat alone is not content
            # verification and cannot authorize deleting a modified original.
            generation, identity, size = await run_cancellation_safe_thread(_hash_file, path)
            if generation != asset.generation or identity != asset.local_identity:
                raise StorageError('storage_source_changed')
            payload = json.loads(operation.payload)
            releases = payload.setdefault('release_intents', {})
            releases[asset.id] = {'generation': asset.generation, 'bytes': size,
                'local_identity': identity, 'previous_released_bytes': operation.released_bytes}
            await self._set(operation, phase='releasing_local_copy', payload=json.dumps(payload))
            def unlink():
                if path.stat().st_nlink > 1:
                    raise StorageError('storage_retained_data_protected', 'A retained alias shares this source.')
                from config import SETTINGS_WRITE_LOCK
                with SETTINGS_WRITE_LOCK:
                    current = policy_value(settings)
                    if current['revision'] != revision or (operation.kind == 'offload' and not current['offload_enabled']):
                        raise StorageError('storage_policy_changed')
                    if target_generation() != asset.target_generation or file_identity(path) != identity:
                        raise StorageError('storage_source_changed')
                    path.unlink()
                    fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            await run_cancellation_safe_thread(unlink)
            if path.exists():
                raise StorageError('storage_local_release_failed')
            await self._set(operation, released_bytes=operation.released_bytes + size)

    async def _cleanup_candidate(self, candidate: str) -> bool:
        from services.rclone import rclone_service
        observed = await rclone_service.run('lsjson', candidate, '--stat', timeout=30, hard_output_limit=65536)
        if observed.error_code == 'drive_not_found':
            return True
        if not observed.ok:
            return False
        removed = await rclone_service.run('deletefile', candidate, timeout=60, hard_output_limit=65536)
        if not removed.ok:
            return False
        observed = await rclone_service.run('lsjson', candidate, '--stat', timeout=30, hard_output_limit=65536)
        return observed.error_code == 'drive_not_found'

    async def reconcile_transfers(self):
        """Resolve bounded interrupted work from observed copies, never from age.

        The accepted final is never a rollback/cleanup target. Unknown provider
        results retain capacity and evidence until a later positive observation.
        """
        from services.media_source import local_path_for
        from services.rclone import rclone_service
        from sqlalchemy import or_
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            backups = (await db.exec(select(StorageOperation).where(StorageOperation.kind == 'backup_sync',
                StorageOperation.status.in_(['blocked', 'failed'])).order_by(StorageOperation.updated_at).limit(10))).all()
        for operation in backups:
            payload = json.loads(operation.payload)
            if payload.get('target_generation') != target_generation() or not payload.get('reservation_id'):
                continue
            from models import StorageReservation
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                reservation = await db.get(StorageReservation, payload['reservation_id'])
            if not reservation or reservation.state not in {'active', 'snapshot'}:
                continue
            await self.measure_cloud()
            measured = await self._measurement('cloud')
            if measured.get('target_generation') == target_generation() and measured.get('used_bytes') is not None:
                await storage_capacity.release(reservation.id)
                await self._set(operation, status='failed', phase='reconciled', error_code='storage_backup_sync_interrupted',
                    message='Cloud capacity was reconciled. The interrupted backup was not reported as verified.')
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            rows = (await db.exec(select(StorageOperation).where(StorageOperation.kind == 'offload',
                or_(StorageOperation.status.in_(['blocked', 'failed']), StorageOperation.phase == 'cleanup_debt'))
                .order_by(StorageOperation.updated_at).limit(10))).all()
        for operation in rows:
            payload = json.loads(operation.payload)
            if not payload.get('reservation_id'):
                continue
            if payload.get('target_generation') != target_generation():
                continue
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                asset = await db.get(StorageAsset, payload.get('asset_id'))
            if asset is None or asset.generation != payload.get('generation'):
                continue
            path = local_path_for(asset.catalog_path)
            prefix = settings.RCLONE_REMOTE_PATH.rstrip('/')
            candidate = prefix + '/.storage-candidates/' + operation.id + '/' + path.name
            final = prefix + '/.storage-assets/' + asset.id + '/' + asset.generation + '/' + path.name
            if payload.get('candidate') != candidate or payload.get('final') != final:
                continue
            observed = await rclone_service.run('lsjson', final, '--stat', timeout=30, hard_output_limit=65536)
            if observed.ok:
                try:
                    info = await self._verify_remote(asset, final)
                except StorageError:
                    continue
                placement = accepted_placement(asset.catalog_path)
                if placement is None:
                    # Acceptance can be reconstructed only from exact original
                    # source identity retained before upload and unchanged local
                    # bytes. An ambiguous source is preserved for operator review.
                    if not path.is_file() or file_identity(path) != asset.local_identity or not payload.get('media_identity'):
                        continue
                    asset.remote_path, asset.remote_identity = final, info.identity
                    asset.target_generation, asset.verified_at = payload['target_generation'], time.time()
                    async with AsyncSession(database.engine, expire_on_commit=False) as db:
                        await db.merge(asset)
                        await db.commit()
                    await run_cancellation_safe_thread(persist_placement, asset, payload['media_identity'], payload.get('audio_metadata', []))
                # Safe recovery preserves a duplicate if policy/protection changed.
                # It never retries a local deletion based merely on old intent.
                if path.is_file() and policy_value(settings)['offload_enabled'] and policy_value(settings)['revision'] == payload['revision']:
                    try:
                        await self._verified_release(operation, asset, payload['revision'])
                    except StorageError:
                        pass
                cleaned = await self._cleanup_candidate(candidate)
                if cleaned:
                    await storage_capacity.release(payload['reservation_id'])
                    await self._set(operation, status='completed', phase='completed' if not path.exists() else 'retained_local_copy', error_code=None,
                        message=None if not path.exists() else 'The accepted cloud copy is safe. The local copy remains protected.')
                    await self.measure_cloud()
            elif observed.error_code == 'drive_not_found' and path.is_file() and file_identity(path) == asset.local_identity:
                if await self._cleanup_candidate(candidate):
                    await storage_capacity.release(payload['reservation_id'])
                    await self._set(operation, status='failed', phase='reconciled', error_code='storage_transfer_not_accepted',
                        message='The incomplete temporary copy was removed. The local original remains available.')

    async def keep_local(self, operation):
        from services.storage_policy_service import storage_policy_service
        from services.media_source import local_path_for, resolve_media_source
        from services.playback_prep import playback_prep_service
        payload = json.loads(operation.payload)
        kind, media = await storage_policy_service._catalog_item(payload['retention_id'])
        if operation.kind == 'prefetch':
            if (not policy_value(settings)['prefetch_next_episode']
                or payload['revision'] != policy_value(settings)['revision']
                or payload['source_fingerprint'] != media.source_fingerprint):
                raise StorageError('storage_prefetch_policy_changed')
        else:
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                pin = await db.get(StorageKeepLocal, payload['retention_id'])
            if not pin or not pin.enabled:
                await self._set(operation, status='completed', phase='protection_removed')
                return
        catalog_paths = [media.video_url]
        for audio in selected_external_audio(media):
            name = audio.get('fileName') or audio.get('file_name')
            if not name or Path(name).name != name:
                raise StorageError('storage_audio_identity_unknown')
            catalog_paths.append(media.video_url.rsplit('/', 1)[0] + '/audio/' + name)
        for catalog_path in catalog_paths:
            path = local_path_for(catalog_path)
            if path.is_file():
                await adopt_asset(media.id if kind == 'movie' else media.movie_id,
                    media.id if kind == 'episode' else None, catalog_path,
                    kind='video' if catalog_path == media.video_url else 'audio',
                    source_fingerprint=media.source_fingerprint if catalog_path == media.video_url else None)
                continue
            placement = accepted_placement(catalog_path)
            if not placement:
                raise StorageError('storage_verified_cloud_source_required')
            await self._restore_asset(operation, placement)
        if not direct_recovery_proven(media):
            source = await resolve_media_source(media.video_url)
            await self._set(operation, phase='preparing_playback')
            if not await playback_prep_service.ensure_offline_baseline(media, source, timeout=300):
                raise StorageError('storage_offline_baseline_unavailable')
        ready = await offline_ready(media, kind)
        await self._set(operation, status='completed' if ready else 'blocked',
            phase='ready_offline' if ready else 'blocked', error_code=None if ready else 'storage_offline_verification_failed')

    async def _restore_asset(self, operation, placement):
        from services.media_source import local_path_for, cloud_object_info
        from services.rclone import rclone_service
        path = local_path_for(placement['catalog_path'])
        info = await cloud_object_info(placement['remote_path'], refresh=True)
        if not info or info.identity != placement['remote_identity']:
            raise StorageError('storage_remote_identity_changed')
        temporary = path.with_name('.' + path.name + '.' + operation.id + '.storage-part')
        reservation = await storage_capacity.reserve('storage.keep-local', placement['size_bytes'], temporary, operation_id=operation.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._set(operation, phase='downloading', bytes_total=placement['size_bytes'])
            copied = await rclone_service.run('copyto', placement['remote_path'], str(temporary), timeout=3600, hard_output_limit=65536)
            if not copied.ok:
                raise StorageError(copied.error_code or 'storage_download_failed')
            await storage_capacity.progress(reservation, temporary.stat().st_size)
            generation, _, _ = await run_cancellation_safe_thread(_hash_file, temporary)
            if generation != placement['generation']:
                raise StorageError('storage_verification_failed')
            async with media_ownership(placement['media_id'], *([placement['episode_id']] if placement.get('episode_id') else [])):
                if path.exists():
                    raise StorageError('storage_source_changed')
                identity = json.loads(placement['local_identity'])
                def publish():
                    os.utime(temporary, ns=(identity['mtime_ns'], identity['mtime_ns']))
                    fd = os.open(temporary, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.replace(temporary, path)
                    fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                await run_cancellation_safe_thread(publish)
                placement['local_identity'] = file_identity(path)
                asset = StorageAsset(**{key: value for key, value in placement.items() if key in StorageAsset.model_fields})
                async with AsyncSession(database.engine, expire_on_commit=False) as db:
                    await db.merge(asset)
                    await db.commit()
                from config import _private_atomic_replace
                from services.storage_inventory import _manifest_path
                await run_cancellation_safe_thread(_private_atomic_replace, str(_manifest_path(asset.catalog_path)), json.dumps(placement) + '\n')
        finally:
            if temporary.exists():
                temporary.unlink()
            await settle(storage_capacity.release(reservation))


storage_worker = StorageWorker()
