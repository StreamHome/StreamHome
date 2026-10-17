"""Server-owned hybrid policy, reviews, receipts and sanitized admin views."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import time
import uuid
import weakref

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import SETTINGS_WRITE_LOCK, settings
import db as database
from lifecycle.coordination import run_cancellation_safe_thread
from lifecycle.media_work import media_ownership
from models import (Episode, Movie, StorageAsset, StorageKeepLocal, StorageMeasurement,
                    StorageOperation, StorageReservation, StorageReview)
from services.storage_common import (DRAFT_FIELDS, StorageError, canonical_draft, digest,
                                     operation_value, policy_value, request_id, utc)
from services.storage_inventory import cloud_read_enabled, target_generation

StoragePolicyError = StorageError


def retention_id(media_id: str, episode_id: str | None = None) -> str:
    return ('episode:' + episode_id) if episode_id is not None else ('movie:' + media_id)


def _cursor(value: str | None) -> str:
    if not value:
        return ''
    try:
        decoded = base64.urlsafe_b64decode(value.encode()).decode()
        if len(decoded) > 512:
            raise ValueError()
        return decoded
    except (ValueError, UnicodeError):
        raise StorageError('storage_invalid_cursor', status_code=422)


def _next_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode()


def _session(auth_session_id: str) -> str:
    if not auth_session_id:
        raise StorageError('storage_human_session_required', status_code=403)
    return digest(auth_session_id)


class StoragePolicyService:
    def __init__(self):
        self._save_locks = weakref.WeakKeyDictionary()

    @property
    def save_lock(self):
        loop = asyncio.get_running_loop()
        return self._save_locks.setdefault(loop, asyncio.Lock())

    async def _measurement(self, name: str) -> tuple[dict, float | None]:
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            row = await db.get(StorageMeasurement, name)
            return (json.loads(row.payload), row.measured_at) if row else ({}, None)

    async def status(self) -> dict:
        from services.rclone import rclone_service
        from services.storage_capacity import storage_capacity, filesystem_for
        local, measured_at = await self._measurement('local')
        cloud, cloud_at = await self._measurement('cloud')
        configured = cloud_read_enabled()
        if cloud.get('target_generation') != target_generation():
            cloud, cloud_at = {}, None
        health = rclone_service.cloud_health()
        reachable = cloud.get('reachable')
        if health['state'] in {'unauthorized', 'unreachable', 'unavailable', 'permission_denied'}:
            reachable = False
        free = None
        try:
            _, existing = filesystem_for(settings.MEDIA_DIR)
            free = (await run_cancellation_safe_thread(shutil.disk_usage, existing)).free
        except (OSError, StorageError):
            pass
        policy = policy_value(settings)
        reason = None
        if policy['offload_enabled']:
            if not configured:
                reason = 'drive_not_connected'
            elif reachable is False:
                reason = cloud.get('error_code') or 'drive_unavailable'
            elif policy['local_budget_bytes'] is None or policy['cloud_budget_bytes'] is None:
                reason = 'storage_budgets_required'
            elif local.get('written_bytes') is None:
                reason = 'storage_measurement_unknown'
            if reason is None:
                cycle, _ = await self._measurement('offload_cycle')
                reason = cycle.get('blocker')
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            active = (await db.exec(select(StorageOperation).where(StorageOperation.status.in_(['queued', 'running'])).order_by(StorageOperation.created_at.desc()).limit(1))).first()
        return {
            'policy': policy,
            'local': {'written_bytes': local.get('written_bytes'), 'reserved_bytes': await storage_capacity.remaining(observe=False),
                'protected_bytes': local.get('protected_bytes'), 'free_bytes': free,
                'disk_reserve_bytes': policy['disk_reserve_bytes'],
                'categories': [{'key': key, 'label': key.replace('_', ' ').title(), 'bytes': amount}
                               for key, amount in local.get('categories', {}).items()],
                'measured_at': utc(measured_at), 'stale': measured_at is None or time.time() - measured_at > 300,
                'error_code': local.get('error_code')},
            'cloud': {'configured': configured, 'reachable': reachable,
                'health_state': str(health['state']), 'folder_name': settings.RCLONE_REMOTE_PATH.split(':', 1)[-1] if configured else None,
                'used_bytes': cloud.get('used_bytes'), 'account_total_bytes': cloud.get('account_total_bytes'),
                'account_free_bytes': cloud.get('account_free_bytes'), 'measured_at': utc(cloud_at),
                'stale': cloud_at is None or time.time() - cloud_at > 300,
                'error_code': cloud.get('error_code')},
            'offload': {'state': 'paused' if not policy['offload_enabled'] else ('blocked' if reason else ('working' if active and active.kind == 'offload' else 'monitoring')),
                        'reason': reason},
            'capabilities': {'offload': True, 'keep_local': True, 'cleanup': True},
            'operation': operation_value(active) if active else None,
        }

    async def preview(self, auth_session_id: str, draft: dict, expected_revision: int) -> dict:
        canonical = canonical_draft(draft)
        policy = policy_value(settings)
        if type(expected_revision) is not int or expected_revision != policy['revision']:
            raise StorageError('storage_revision_changed', 'Storage settings changed. Review the latest values.')
        if canonical['offload_enabled'] and not cloud_read_enabled():
            raise StorageError('drive_not_connected', 'Connect Google Drive before enabling automatic offloading.', 422)
        local, _ = await self._measurement('local')
        if canonical['local_budget_bytes'] is not None and local.get('protected_bytes') is not None and canonical['local_budget_bytes'] < local['protected_bytes']:
            raise StorageError('storage_budget_below_protected_data', 'The local budget cannot fit currently protected data.', 422)
        changes = [{'field': key, 'before': policy[key], 'after': canonical[key]}
                   for key in sorted(DRAFT_FIELDS) if policy[key] != canonical[key]]
        if not changes:
            raise StorageError('storage_no_changes', 'There are no changes to save.', 422)
        review = StorageReview(id=str(uuid.uuid4()), kind='policy', session_hash=_session(auth_session_id),
            policy_revision=policy['revision'], target_generation=target_generation(),
            draft_hash=digest(canonical), payload=json.dumps(canonical), expires_at=time.time() + 300)
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            db.add(review)
            await db.commit()
        effects = ['Saving settings records the policy; media movement is reported separately in Activity.']
        if canonical['offload_enabled']:
            effects += ['Unused caches are considered first, then verified duplicates, then recoverable cold video and audio.',
                        'Capacity and playback protections are checked again before each operation.']
        else:
            effects += ['New automatic transfers are paused. Existing Google Drive media remains readable.']
        return {'review_id': review.id, 'expires_at': utc(review.expires_at), 'policy_revision': policy['revision'],
                'draft': canonical, 'changes': changes, 'effects': effects}

    def _accepted_save(self, session_hash: str, operation_id: str, canonical: dict | None = None) -> dict | None:
        stored = getattr(settings, 'STORAGE_POLICY', None) or {}
        receipt = stored.get('receipts', {}).get(operation_id)
        if receipt is None:
            return None
        if receipt.get('session_hash') != session_hash or (canonical is not None and receipt.get('draft_hash') != digest(canonical)):
            raise StorageError('storage_request_conflict')
        # Resolve an acknowledgement lost after atomic replace/directory fsync.
        # Presence in memory alone is never proof of durable settings acceptance.
        path = Path(settings.BASE_DIR) / 'server/settings.json'
        try:
            with SETTINGS_WRITE_LOCK:
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                try:
                    with os.fdopen(fd, 'r', closefd=False) as handle:
                        current = json.load(handle)
                    observed = (current.get('storage_policy') or {}).get('receipts', {}).get(operation_id)
                    if observed != receipt:
                        raise StorageError('storage_save_outcome_uncertain')
                    os.fsync(fd)
                finally:
                    os.close(fd)
                fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            raise StorageError('storage_save_outcome_uncertain') from exc
        return receipt

    async def save(self, auth_session_id: str, review_id: str, draft: dict,
                   expected_revision: int, operation_id: str) -> dict:
        operation_id = request_id(operation_id)
        canonical = canonical_draft(draft)
        session_hash = _session(auth_session_id)
        async with self.save_lock:
            receipt = self._accepted_save(session_hash, operation_id, canonical)
            if receipt:
                return {'policy': policy_value(settings), 'operation': receipt['operation']}
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                review = await db.get(StorageReview, review_id)
                old_operation = await db.get(StorageOperation, operation_id)
                if old_operation:
                    if (old_operation.kind == 'policy_save' and old_operation.session_hash == session_hash
                        and json.loads(old_operation.payload).get('draft_hash') == digest(canonical)):
                        return {'policy': policy_value(settings), 'operation': operation_value(old_operation)}
                    raise StorageError('storage_request_conflict')
                if (not review or review.kind != 'policy' or review.session_hash != session_hash
                    or review.draft_hash != digest(canonical) or review.policy_revision != expected_revision
                    or review.used_by or review.expires_at <= time.time()):
                    raise StorageError('storage_review_expired', 'The review changed or expired. Review these settings again.')
                bound_target = review.target_generation
            from services.storage_capacity import storage_capacity, _remaining_from_sample
            # Serialize final admission against every new storage writer. The
            # database read closes before filesystem/protection/settings I/O.
            async with storage_capacity.lock:
                if canonical['local_budget_bytes'] is not None:
                    from lifecycle.storage_worker import storage_worker
                    from services.storage_inventory import storage_inventory
                    measured = await storage_inventory.local_measurement(include_entries=True)
                    # One fresh inventory sample drives both terms. Only unwritten
                    # reservation bytes are added to the protected written floor.
                    async with AsyncSession(database.engine, expire_on_commit=False) as db:
                        claims = (await db.exec(select(StorageReservation).where(
                            StorageReservation.state == 'active', StorageReservation.scope == 'local'))).all()
                    floor = await storage_worker.protected_floor(measured=measured)
                    outstanding = _remaining_from_sample(claims, measured, scope='local')
                    if canonical['local_budget_bytes'] < floor + outstanding:
                        raise StorageError('storage_review_protected_data_changed',
                            'Protected local data or admitted work changed or exceeds this budget. Refresh storage and review again.')
                now = time.time()
                operation = StorageOperation(id=operation_id, session_hash=session_hash, kind='policy_save',
                    status='completed', phase='saved', bytes_done=0, released_bytes=0,
                    created_at=now, updated_at=now, message='Settings saved. File placement is managed separately.',
                    payload=json.dumps({'draft_hash': digest(canonical)}))
                encoded = operation_value(operation)

                def persist():
                    with SETTINGS_WRITE_LOCK:
                        current = policy_value(settings)
                        if time.time() >= review.expires_at:
                            raise StorageError('storage_review_expired')
                        if current['revision'] != expected_revision:
                            raise StorageError('storage_revision_changed')
                        if target_generation() != bound_target:
                            raise StorageError('storage_target_changed')
                        if canonical['offload_enabled'] and not cloud_read_enabled():
                            raise StorageError('drive_not_connected')
                        prior = getattr(settings, 'STORAGE_POLICY', None)
                        previous_hevc = settings.HEVC_COMPRESSION_MODE
                        receipts = dict((prior or {}).get('receipts', {}))
                        receipts[operation_id] = {'session_hash': session_hash, 'draft_hash': digest(canonical), 'operation': encoded}
                        # Receipts remain available for loss-of-ack reconciliation;
                        # history is bounded while the latest accepted receipt stays.
                        receipts = dict(list(receipts.items())[-128:])
                        settings.STORAGE_POLICY = dict(canonical, revision=expected_revision + 1,
                            disk_reserve_bytes=current['disk_reserve_bytes'], last_operation_id=operation_id,
                            receipts=receipts)
                        settings.HEVC_COMPRESSION_MODE = canonical['hevc_compression_mode']
                        try:
                            settings._save_to_json_locked()
                        except BaseException:
                            # A directory fsync can fail after replace. Read the
                            # actual file before deciding whether acceptance happened.
                            try:
                                document = json.loads((Path(settings.BASE_DIR) / 'server/settings.json').read_text())
                                observed = document.get('storage_policy') or {}
                                if observed.get('last_operation_id') == operation_id:
                                    settings.STORAGE_POLICY = observed
                                    settings.HEVC_COMPRESSION_MODE = document['hevc_compression_mode']
                                    raise StorageError('storage_save_outcome_uncertain', 'Check the operation status before retrying.')
                            except StorageError:
                                raise
                            except (OSError, ValueError, KeyError):
                                pass
                            settings.STORAGE_POLICY = prior
                            settings.HEVC_COMPRESSION_MODE = previous_hevc
                            raise
                await run_cancellation_safe_thread(persist)
            # Settings receipt already survives a lost DB acknowledgement.
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                review = await db.get(StorageReview, review_id)
                if review:
                    review.used_by = operation_id
                    db.add(review)
                db.add(operation)
                await db.commit()
            self._wake()
            return {'policy': policy_value(settings), 'operation': encoded}

    @staticmethod
    def _wake():
        from lifecycle.storage_worker import storage_worker
        storage_worker.wake()

    async def operation(self, auth_session_id: str, operation_id: str) -> dict:
        session_hash = _session(auth_session_id)
        request_id(operation_id)
        receipt = self._accepted_save(session_hash, operation_id)
        if receipt:
            return {'operation': receipt['operation']}
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            row = await db.get(StorageOperation, operation_id)
            if row is None or (row.session_hash and row.session_hash != session_hash):
                raise StorageError('storage_operation_not_found', status_code=404)
            return {'operation': operation_value(row)}

    async def _queue(self, auth_session_id: str, operation_id: str, kind: str, payload: dict) -> StorageOperation:
        async with self.save_lock:
            return await self._queue_locked(auth_session_id, operation_id, kind, payload)

    async def _queue_locked(self, auth_session_id: str, operation_id: str, kind: str, payload: dict) -> StorageOperation:
        operation_id = request_id(operation_id)
        session_hash = _session(auth_session_id)
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            row = await db.get(StorageOperation, operation_id)
            if row:
                if row.session_hash != session_hash or row.kind != kind or json.loads(row.payload) != payload:
                    raise StorageError('storage_request_conflict')
                return row
            row = StorageOperation(id=operation_id, session_hash=session_hash, kind=kind, payload=json.dumps(payload))
            db.add(row)
            await db.commit()
        self._wake()
        return row

    async def refresh(self, auth_session_id: str, operation_id: str) -> dict:
        # Each request keeps its exact session-bound receipt. The worker may
        # coalesce measurement work while preserving every requested lookup ID.
        return {'operation': operation_value(await self._queue(auth_session_id, operation_id, 'refresh', {}))}

    async def _catalog_item(self, identifier: str):
        kind, separator, key = identifier.partition(':')
        if not separator or kind not in {'movie', 'episode'} or not key:
            raise StorageError('storage_media_not_found', status_code=404)
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            media = await db.get(Movie if kind == 'movie' else Episode, key)
            if not media or (kind == 'movie' and (media.catalog_source != 'server' or media.type == 'series')):
                raise StorageError('storage_media_not_found', status_code=404)
            return kind, media

    async def _retention_item(self, identifier: str) -> dict:
        from services.media_source import local_path_for
        from lifecycle.storage_worker import media_protection_reason, offline_ready
        kind, media = await self._catalog_item(identifier)
        media_id = media.id if kind == 'movie' else media.movie_id
        episode_id = media.id if kind == 'episode' else None
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            pin = await db.get(StorageKeepLocal, identifier)
            assets = (await db.exec(select(StorageAsset).where(StorageAsset.media_id == media_id, StorageAsset.episode_id == episode_id))).all()
            operation = (await db.exec(select(StorageOperation).where(StorageOperation.media_id == identifier, StorageOperation.status.in_(['queued', 'running'])).limit(1))).first()
        try:
            local = local_path_for(media.video_url).is_file()
        except ValueError:
            local = False
        cloud = any(asset.kind == 'video' and asset.remote_identity and asset.verified_at and asset.target_generation == target_generation() for asset in assets)
        reason = await media_protection_reason(media_id, episode_id, include_pin=False)
        return {'id': identifier, 'media_id': media_id, 'title': media.title, 'kind': kind,
            'local_state': 'preparing' if operation else ('both' if local and cloud else 'local' if local else 'cloud' if cloud else 'unavailable'),
            'bytes': sum(asset.size_bytes for asset in assets) if assets else None,
            'kept': bool(pin and pin.enabled), 'offline_ready': await offline_ready(media, kind),
            'protected_reason': 'keep_local' if pin and pin.enabled else reason,
            'operation_id': operation.id if operation else None}

    async def retention(self, cursor: str | None = None, limit: int = 25) -> dict:
        after = _cursor(cursor)
        limit = max(1, min(int(limit), 50))
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            movies_query = select(Movie.id).where(Movie.catalog_source == 'server', Movie.type != 'series').order_by(Movie.id)
            if after.startswith('movie:'):
                movies_query = movies_query.where(Movie.id > after.removeprefix('movie:'))
            movies = (await db.exec(movies_query.limit(limit + 1))).all()
            episodes_query = select(Episode.id).order_by(Episode.id)
            if after.startswith('episode:'):
                episodes_query = episodes_query.where(Episode.id > after.removeprefix('episode:'))
            episodes = (await db.exec(episodes_query.limit(limit + 1))).all() if after < 'movie:' else []
        keys = sorted(['movie:' + value for value in movies] + ['episode:' + value for value in episodes])
        eligible = [value for value in keys if value > after]
        selected = eligible[:limit]
        items = [await self._retention_item(identifier) for identifier in selected]
        return {'items': items, 'next_cursor': _next_cursor(selected[-1]) if len(eligible) > limit else None}

    async def set_keep_local(self, auth_session_id: str, media_id: str, enabled: bool, operation_id: str) -> dict:
        if type(enabled) is not bool:
            raise StorageError('storage_invalid_keep_local', status_code=422)
        kind, media = await self._catalog_item(media_id)
        parent = media.id if kind == 'movie' else media.movie_id
        episode = media.id if kind == 'episode' else None
        operation_id = request_id(operation_id)
        session_hash = _session(auth_session_id)
        async with media_ownership(parent, *([episode] if episode else [])):
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                existing = await db.get(StorageOperation, operation_id)
                payload = {'retention_id': media_id, 'enabled': enabled}
                if existing:
                    if existing.session_hash != session_hash or existing.kind != 'keep_local' or json.loads(existing.payload) != payload:
                        raise StorageError('storage_request_conflict')
                    row = existing
                else:
                    pin = await db.get(StorageKeepLocal, media_id)
                    if pin is None:
                        pin = StorageKeepLocal(id=media_id, media_id=parent, episode_id=episode, enabled=enabled)
                    pin.enabled, pin.updated_at = enabled, time.time()
                    row = StorageOperation(id=operation_id, session_hash=session_hash, kind='keep_local', media_id=media_id,
                        title=media.title, payload=json.dumps(payload), status='queued' if enabled else 'completed',
                        phase='queued' if enabled else 'protection_removed',
                        message=None if enabled else 'Keep local removed. Existing files remain in place.')
                    db.add(pin)
                    db.add(row)
                    await db.commit()
        self._wake()
        return {'item': await self._retention_item(media_id), 'operation': operation_value(row)}

    async def activity(self, cursor: str | None = None, limit: int = 25) -> dict:
        after = _cursor(cursor)
        limit = max(1, min(int(limit), 50))
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            query = select(StorageOperation).order_by(StorageOperation.created_at.desc(), StorageOperation.id.desc())
            if after:
                try:
                    stamp, identifier = after.split('|', 1)
                    from sqlalchemy import or_, and_
                    query = query.where(or_(StorageOperation.created_at < float(stamp), and_(StorageOperation.created_at == float(stamp), StorageOperation.id < identifier)))
                except ValueError:
                    raise StorageError('storage_invalid_cursor', status_code=422)
            rows = (await db.exec(query.limit(limit + 1))).all()
        selected = rows[:limit]
        return {'items': [operation_value(row) for row in selected],
            'next_cursor': _next_cursor(str(selected[-1].created_at) + '|' + selected[-1].id) if len(rows) > limit else None}

    async def cleanup_preview(self, auth_session_id: str) -> dict:
        from lifecycle.storage_worker import storage_worker
        plan = await storage_worker.cleanup_plan()
        row = StorageReview(id=str(uuid.uuid4()), kind='cleanup', session_hash=_session(auth_session_id),
            policy_revision=policy_value(settings)['revision'], target_generation=target_generation(),
            draft_hash=digest(plan), payload=json.dumps(plan), expires_at=time.time() + 300)
        async with AsyncSession(database.engine, expire_on_commit=False) as db:
            db.add(row)
            await db.commit()
        return {'plan_id': row.id, 'expires_at': utc(row.expires_at),
            'estimated_release_bytes': sum(item['bytes'] for item in plan['candidates']),
            'steps': [{'kind': item['kind'], 'bytes': item['bytes']} for item in plan['candidates']],
            'blocker': plan.get('blocker')}

    async def cleanup_execute(self, auth_session_id: str, plan_id: str, operation_id: str) -> dict:
        session_hash = _session(auth_session_id)
        operation_id = request_id(operation_id)
        async with self.save_lock:
            async with AsyncSession(database.engine, expire_on_commit=False) as db:
                existing = await db.get(StorageOperation, operation_id)
                if existing:
                    if (existing.session_hash != session_hash or existing.kind != 'cleanup'
                        or json.loads(existing.payload).get('plan_id') != plan_id):
                        raise StorageError('storage_request_conflict')
                    return {'operation': operation_value(existing)}
                plan = await db.get(StorageReview, plan_id)
                if (not plan or plan.kind != 'cleanup' or plan.session_hash != session_hash
                    or plan.expires_at <= time.time() or plan.policy_revision != policy_value(settings)['revision']
                    or plan.target_generation != target_generation() or plan.used_by):
                    raise StorageError('storage_cleanup_plan_expired')
                payload = {'plan_id': plan_id, 'plan': json.loads(plan.payload), 'revision': plan.policy_revision,
                           'target_generation': plan.target_generation}
                plan.used_by = operation_id
                operation = StorageOperation(id=operation_id, session_hash=session_hash, kind='cleanup',
                    payload=json.dumps(payload))
                db.add(plan)
                db.add(operation)
                # Plan consumption and its exact request receipt are one atomic
                # transaction. A lost acknowledgement remains lookupable.
                await db.commit()
        self._wake()
        return {'operation': operation_value(operation)}


storage_policy_service = StoragePolicyService()
