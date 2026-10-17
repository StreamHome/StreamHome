"""Disposable domain proofs. Run only through tests/run_isolated.py."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import config
from config import settings
import db as database
from lifecycle.storage_placement import StoragePlacementRecoveryRequired, validate_placement_manifests
from lifecycle.storage_worker import StorageWorker
from models import Movie, PlaybackRun, StorageAsset, StorageKeepLocal, StorageMeasurement, StorageOperation, StorageReservation
from services.storage_capacity import StorageCapacity
from services.storage_common import DISK_RESERVE_BYTES, StorageError, digest
from services.storage_inventory import (accepted_identity, accepted_placement, adopt_asset,
    file_identity, invalidate_placement, persist_placement, scan_managed, storage_inventory, target_generation)
from services.storage_policy_service import StoragePolicyService
from services.media_source import local_media_identity, local_video_fingerprint


class HybridStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = self.root / 'server'
        self.media = self.server / 'media'
        self.temporary = self.server / 'temp'
        self.media.mkdir(parents=True)
        self.temporary.mkdir()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + str(self.server / 'database.db'))
        self.patches = [patch.object(database, 'engine', self.engine), patch.object(config, 'config_dir', str(self.server)),
            patch.object(settings, 'BASE_DIR', str(self.root)), patch.object(settings, 'MEDIA_DIR', str(self.media)),
            patch.object(settings, 'TEMP_DIR', str(self.temporary)), patch.object(settings, 'db_path', str(self.server / 'database.db')),
            patch.object(settings, 'STORAGE_POLICY', None), patch.object(settings, 'HEVC_COMPRESSION_MODE', 'auto'),
            patch.object(settings, 'STORAGE_ENGINE', 'LOCAL'), patch.object(settings, 'RCLONE_REMOTE_PATH', 'unit:media')]
        for item in self.patches:
            item.start()
        async with self.engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        self.service = StoragePolicyService()
        self.capacity = StorageCapacity()
        self.worker = StorageWorker()
        self.wake = patch.object(StoragePolicyService, '_wake', lambda _: None)
        self.wake.start()
        self.protection = patch('services.playback_prep.playback_prep_service.is_media_protected', AsyncMock(return_value=False))
        self.protection.start()

    async def asyncTearDown(self):
        self.protection.stop()
        self.wake.stop()
        await self.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def draft(self, **changes):
        return dict(offload_enabled=False, local_budget_bytes=100_000_000,
            cloud_budget_bytes=None, prefetch_next_episode=False, hevc_compression_mode='auto') | changes

    async def movie(self, *, name='film', content=b'verified-content'):
        path = self.media / 'Movies' / name / 'video.mp4'
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        row = Movie(id=name, title='Actual film', description='', thumbnail_url='', video_url='/media/Movies/' + name + '/video.mp4',
            duration='1:00', release_year=2026, source_fingerprint='accepted-playback-identity')
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(row)
            await session.commit()
        asset = await adopt_asset(name, None, row.video_url, source_fingerprint=row.source_fingerprint, cold_recovery_ready=True)
        return row, asset, path

    async def accept(self, row, asset, path):
        asset.remote_path = 'unit:media/.storage-assets/' + asset.id + '/' + asset.generation + '/' + path.name
        asset.remote_identity = 'exact-provider-content-identity'
        asset.target_generation = target_generation()
        asset.verified_at = time.time()
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            await session.merge(asset)
            await session.commit()
        persist_placement(asset, local_media_identity(path), row.audio_metadata)

    async def test_default_policy_is_unconfigured_and_read_only(self):
        status = await self.service.status()
        self.assertIsNone(settings.STORAGE_POLICY)
        self.assertFalse(status['policy']['offload_enabled'])
        self.assertIsNone(status['policy']['local_budget_bytes'])
        self.assertIsNone(status['local']['written_bytes'])
        self.assertFalse((self.server / 'settings.json').exists())

    async def test_review_is_bound_to_exact_session_draft_revision_target(self):
        preview = await self.service.preview('session-one', self.draft(), 0)
        for session, draft, revision in [('session-two', self.draft(), 0), ('session-one', self.draft(local_budget_bytes=3), 0), ('session-one', self.draft(), 1)]:
            with self.assertRaises(StorageError):
                await self.service.save(session, preview['review_id'], draft, revision, str(uuid.uuid4()))
        with patch('services.storage_policy_service.target_generation', return_value='different-target'):
            with self.assertRaisesRegex(StorageError, 'target'):
                await self.service.save('session-one', preview['review_id'], self.draft(), 0, str(uuid.uuid4()))
        self.assertIsNone(settings.STORAGE_POLICY)

    async def test_save_durable_receipt_reconciles_lost_response_and_preserves_settings(self):
        (self.server / 'settings.json').write_text(json.dumps({'unrelated_owner': {'keep': True}}))
        preview = await self.service.preview('human', self.draft(), 0)
        request = str(uuid.uuid4())
        saved = await self.service.save('human', preview['review_id'], self.draft(), 0, request)
        self.assertEqual(saved['policy']['revision'], 1)
        self.assertEqual((await self.service.operation('human', request))['operation']['phase'], 'saved')
        self.assertEqual(json.loads((self.server / 'settings.json').read_text())['unrelated_owner'], {'keep': True})
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            operation = await session.get(StorageOperation, request)
            await session.delete(operation)
            await session.commit()
        again = await self.service.save('human', preview['review_id'], self.draft(), 0, request)
        self.assertEqual(again['operation']['id'], request)
        self.assertEqual(again['policy']['revision'], 1)
        with self.assertRaises(StorageError):
            await self.service.operation('another-human', request)
        settings.save_to_json()
        self.assertIn(request, json.loads((self.server / 'settings.json').read_text())['storage_policy']['receipts'])
        self.assertEqual((self.server / 'settings.json').stat().st_mode & 0o777, 0o600)

    async def test_policy_requires_real_budgets_before_offload(self):
        with self.assertRaisesRegex(StorageError, 'budgets'):
            await self.service.preview('human', self.draft(offload_enabled=True), 0)
        with self.assertRaises(StorageError):
            await self.service.preview('human', self.draft(local_budget_bytes=True), 0)

    async def test_inventory_overlapping_roots_hardlinks_and_copies(self):
        path = self.media / 'sample'; path.write_bytes(b'a' * 17)
        os.link(path, self.media / 'hardlink')
        (self.media / 'copy').write_bytes(b'a' * 17)
        with patch('services.storage_inventory.managed_roots', return_value=[('media', self.media), ('temporary', self.media)]):
            measured = scan_managed()
            self.assertEqual(measured['written_bytes'], 34)
            incomplete = scan_managed(max_files=1)
            self.assertIsNone(incomplete['written_bytes'])
            self.assertEqual(incomplete['error_code'], 'storage_inventory_limit')

    async def test_parallel_admission_and_written_reservations_do_not_overlap(self):
        baseline = scan_managed()['written_bytes']
        settings.STORAGE_POLICY = self.draft(local_budget_bytes=baseline + 100) | {'revision': 1}
        results = await asyncio.gather(self.capacity.reserve('first', 70, self.temporary), self.capacity.reserve('second', 70, self.temporary), return_exceptions=True)
        self.assertEqual(sum(isinstance(value, str) for value in results), 1)
        reservation = next(value for value in results if isinstance(value, str))
        (self.temporary / 'output').write_bytes(b'x' * 30)
        await self.capacity.progress(reservation, 30)
        self.assertEqual(await self.capacity.remaining(), 40)
        # Dynamic DB/WAL growth is independently accounted, so freeze only that
        # irrelevant part while asserting measured output + remainder arithmetic.
        from services.storage_inventory import managed_roots
        with patch('services.storage_inventory.managed_roots', return_value=[('temporary', self.temporary)]):
            settings.STORAGE_POLICY['local_budget_bytes'] = 100
            await self.capacity.reserve('third', 30, self.temporary)
            with self.assertRaises(StorageError):
                await self.capacity.reserve('fourth', 1, self.temporary)
        await self.capacity.release(reservation)
        self.assertEqual(await self.capacity.remaining(), 30)

    async def test_filesystem_free_guard_applies_without_policy(self):
        with patch('services.storage_capacity.shutil.disk_usage', return_value=SimpleNamespace(free=DISK_RESERVE_BYTES + 5)):
            with self.assertRaises(StorageError) as found:
                await self.capacity.reserve('writer', 6, self.temporary)
        self.assertEqual(found.exception.code, 'storage_disk_space_insufficient')

    async def test_unknown_cloud_account_capacity_cannot_be_reserved(self):
        await self.worker._save_measurement('cloud', {'target_generation': target_generation(), 'used_bytes': 1, 'account_free_bytes': None})
        with self.assertRaises(StorageError) as found:
            await self.capacity.reserve('cloud-writer', 1, 'unit:media', scope='cloud')
        self.assertEqual(found.exception.code, 'storage_account_capacity_unknown')

    async def test_accepted_placement_survives_old_database_and_job_cleanup(self):
        row, asset, path = await self.movie()
        await self.accept(row, asset, path)
        expected = accepted_identity(row.video_url, path, audio_metadata=row.audio_metadata)
        self.assertEqual(expected[0], row.source_fingerprint)
        path.unlink()
        # The old DB backup predates every asset/receipt. Restoring that state
        # cannot remove the durable accepted remote placement.
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            stored = await session.get(StorageAsset, asset.id)
            await session.delete(stored)
            await session.commit()
        self.assertEqual(await storage_inventory.reconcile_accepted_placements(), 1)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            restored = await session.get(StorageAsset, asset.id)
            self.assertEqual(restored.remote_identity, asset.remote_identity)
        self.assertEqual(accepted_identity(row.video_url, None, asset.remote_identity, audio_metadata=row.audio_metadata), expected)
        self.assertEqual(len(validate_placement_manifests(self.root)), 1)
        self.assertTrue((self.media / '.storage-placements').is_dir())

    async def test_manifest_corruption_blocks_offline_restore(self):
        row, asset, path = await self.movie()
        await self.accept(row, asset, path)
        manifest = next((self.media / '.storage-placements').iterdir())
        manifest.write_text('{invalid')
        with self.assertRaises(StoragePlacementRecoveryRequired):
            validate_placement_manifests(self.root)

    async def test_accepted_identity_rejects_changed_local_video_and_metadata(self):
        row, asset, path = await self.movie()
        await self.accept(row, asset, path)
        identity = accepted_identity(row.video_url, path, audio_metadata=[])
        self.assertIsNotNone(identity[0])
        self.assertIsNone(accepted_identity(row.video_url, path, audio_metadata=[{'source': 'external'}])[0])
        path.write_bytes(b'other-generation')
        self.assertEqual(accepted_identity(row.video_url, path), (None, None))
        self.assertEqual(accepted_identity(row.video_url, None, 'wrong-remote'), (None, None))

    async def test_retirement_preserves_evidence_but_never_reactivates_deleted_asset(self):
        row, asset, path = await self.movie()
        await self.accept(row, asset, path)
        invalidate_placement(row.video_url)
        self.assertIsNone(accepted_placement(row.video_url))
        self.assertTrue(validate_placement_manifests(self.root)[0]['retired'])

    async def test_long_pause_is_protected_without_heartbeat_expiry(self):
        row, asset, path = await self.movie()
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(PlaybackRun(id='paused-run', profile_id='1', movie_id=row.id, lifecycle_state='active', last_seen_at=1))
            await session.commit()
        from lifecycle.storage_worker import media_protection_reason
        self.assertEqual(await media_protection_reason(row.id), 'active_playback')

    async def test_keep_local_removal_never_deletes_existing_file(self):
        row, asset, path = await self.movie()
        with patch('lifecycle.storage_worker.direct_recovery_proven', return_value=True):
            first = await self.service.set_keep_local('human', 'movie:' + row.id, True, str(uuid.uuid4()))
            self.assertTrue(first['item']['kept'])
            second = await self.service.set_keep_local('human', 'movie:' + row.id, False, str(uuid.uuid4()))
        self.assertFalse(second['item']['kept'])
        self.assertTrue(path.is_file())
        self.assertEqual(second['operation']['released_bytes'], 0)

    async def test_failed_remote_verification_never_removes_original(self):
        row, asset, path = await self.movie()
        await self.accept(row, asset, path)
        operation = StorageOperation(id=str(uuid.uuid4()), kind='cleanup')
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(operation); await session.commit()
        with patch.object(self.worker, '_verify_remote', AsyncMock(side_effect=StorageError('storage_verification_failed'))):
            with self.assertRaises(StorageError):
                await self.worker._verified_release(operation, asset, 0)
        self.assertTrue(path.exists())
        self.assertEqual(operation.released_bytes, 0)

    async def test_pin_during_upload_blocks_final_release(self):
        row, asset, path = await self.movie()
        await self.accept(row, asset, path)
        operation = StorageOperation(id=str(uuid.uuid4()), kind='cleanup')
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(operation)
            session.add(StorageKeepLocal(id='movie:' + row.id, media_id=row.id, enabled=True))
            await session.commit()
        with patch.object(self.worker, '_verify_remote', AsyncMock()):
            with self.assertRaises(StorageError) as found:
                await self.worker._verified_release(operation, asset, 0)
        self.assertEqual(found.exception.code, 'storage_media_protected')
        self.assertTrue(path.exists())

    async def test_successful_verified_unlink_reports_actual_freed_bytes(self):
        row, asset, path = await self.movie()
        await self.accept(row, asset, path)
        operation = StorageOperation(id=str(uuid.uuid4()), kind='cleanup')
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(operation); await session.commit()
        with patch.object(self.worker, '_verify_remote', AsyncMock()), patch('lifecycle.storage_worker.direct_recovery_proven', return_value=True):
            await self.worker._verified_release(operation, asset, 0)
        self.assertFalse(path.exists())
        self.assertEqual(operation.released_bytes, len(b'verified-content'))
        self.assertIsNotNone(accepted_placement(row.video_url))

    async def test_catalog_inventory_advances_bounded_batches(self):
        await self.movie(name='one')
        await self.movie(name='two')
        await self.movie(name='three')
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            for asset in (await session.exec(select(StorageAsset))).all():
                await session.delete(asset)
            await session.commit()
        for _ in range(3):
            await storage_inventory.reconcile_catalog(limit=1)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            assets = (await session.exec(select(StorageAsset))).all()
        self.assertEqual(len(assets), 3)

    async def test_native_copy_verify_accept_release_and_old_database_restore(self):
        from services.rclone import rclone_service
        remote_root = self.root / 'remote'
        remote_root.mkdir()
        configuration = self.server / 'rclone' / 'rclone.conf'
        configuration.parent.mkdir()
        configuration.write_text('[unit]\ntype = local\n')
        selected = 'unit:' + str(remote_root)
        settings.RCLONE_REMOTE_PATH = selected
        settings.STORAGE_POLICY = self.draft(offload_enabled=True, cloud_budget_bytes=10**9) | {'revision': 1}
        row, asset, path = await self.movie(content=b'native-rclone-generation' * 1024)
        operation = StorageOperation(id=str(uuid.uuid4()), kind='offload', payload=json.dumps({
            'asset_id': asset.id, 'generation': asset.generation, 'revision': 1, 'target_generation': target_generation()}))
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(operation); await session.commit()
        with patch.object(rclone_service, 'config_path', configuration), patch.object(rclone_service, 'config_dir', configuration.parent), \
                patch('lifecycle.storage_worker.direct_recovery_proven', return_value=True):
            await self.worker.offload(operation)
            placement = accepted_placement(row.video_url)
            self.assertIsNotNone(placement)
            self.assertFalse(path.exists())
            final = Path(placement['remote_path'].split(':', 1)[1])
            self.assertEqual(final.read_bytes(), b'native-rclone-generation' * 1024)
            self.assertEqual(operation.released_bytes, len(b'native-rclone-generation' * 1024))
            # Journal/DB receipts can disappear on old-backup restore. The
            # independent accepted placement still reconstructs cloud ownership.
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                for model in [StorageOperation, StorageAsset]:
                    for stored in (await session.exec(select(model))).all():
                        await session.delete(stored)
                await session.commit()
            self.assertEqual(await storage_inventory.reconcile_accepted_placements(), 1)
            from services.media_source import resolve_media_source, playback_source_fingerprint
            resolved = await resolve_media_source(row.video_url)
            self.assertTrue(resolved.cloud_exists)
            self.assertEqual(playback_source_fingerprint(resolved, row.audio_metadata), row.source_fingerprint)
            # Keep-local hydrates and verifies the exact accepted generation.
            restore_op = StorageOperation(id=str(uuid.uuid4()), kind='keep_local')
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                session.add(restore_op); await session.commit()
            await self.worker._restore_asset(restore_op, placement)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), final.read_bytes())
            self.assertEqual(accepted_identity(row.video_url, path, audio_metadata=row.audio_metadata)[0], row.source_fingerprint)

    async def native_interrupted_transfer(self, name):
        """Use real local rclone objects; only recovery eligibility is synthetic."""
        from services.rclone import rclone_service
        remote_root = self.root / ('remote-' + name)
        remote_root.mkdir()
        configuration = self.server / 'rclone' / 'rclone.conf'
        configuration.parent.mkdir(exist_ok=True)
        configuration.write_text('[unit]\ntype = local\n')
        settings.RCLONE_REMOTE_PATH = 'unit:' + str(remote_root)
        settings.STORAGE_POLICY = self.draft(offload_enabled=True, cloud_budget_bytes=10**9) | {'revision': 1}
        for item in [patch.object(rclone_service, 'config_path', configuration),
                     patch.object(rclone_service, 'config_dir', configuration.parent),
                     patch('lifecycle.storage_worker.direct_recovery_proven', return_value=True)]:
            item.start()
            self.patches.append(item)
        row, asset, path = await self.movie(name=name, content=b'native-interrupted-generation' * 128)
        operation = StorageOperation(id=str(uuid.uuid4()), kind='offload', status='running',
            payload=json.dumps({'asset_id': asset.id, 'generation': asset.generation,
                'revision': 1, 'target_generation': target_generation()}))
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(operation)
            await session.commit()
        return row, asset, path, operation

    async def restarted_transfer_worker(self):
        restarted = StorageWorker()
        # Run the real startup normalization without its periodic automatic work.
        with patch.object(restarted, '_run', AsyncMock()):
            await restarted.start()
            await restarted.stop()
        return restarted

    async def transfer_evidence(self, operation):
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            stored = await session.get(StorageOperation, operation.id)
            payload = json.loads(stored.payload)
            reservation = await session.get(StorageReservation, payload['reservation_id'])
        return stored, payload, reservation

    async def test_native_restart_before_publication_removes_only_owned_candidate(self):
        from services.rclone import rclone_service
        row, asset, path, operation = await self.native_interrupted_transfer('before-publication')
        original = path.read_bytes()
        native_run = rclone_service.run

        async def stop_before_publication(*args, **kwargs):
            if args[0] == 'copyto' and '/.storage-candidates/' in str(args[1]):
                raise OSError('synthetic interruption before final publication')
            return await native_run(*args, **kwargs)

        with patch.object(rclone_service, 'run', side_effect=stop_before_publication):
            with self.assertRaisesRegex(OSError, 'before final publication'):
                await self.worker.offload(operation)
        stored, payload, reservation = await self.transfer_evidence(operation)
        candidate = Path(payload['candidate'].split(':', 1)[1])
        final = Path(payload['final'].split(':', 1)[1])
        foreign = candidate.parent.parent / 'unrelated-operation' / path.name
        foreign.parent.mkdir()
        foreign.write_bytes(b'unrelated-candidate')
        self.assertEqual(candidate.read_bytes(), original)
        self.assertFalse(final.exists())
        self.assertEqual(reservation.state, 'active')
        self.assertIsNone(accepted_placement(row.video_url))

        restarted = await self.restarted_transfer_worker()
        with patch.object(rclone_service, 'run', wraps=native_run) as calls:
            await restarted.reconcile_transfers()
        stored, after_payload, reservation = await self.transfer_evidence(operation)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(candidate.exists())
        self.assertFalse(final.exists())
        self.assertEqual(foreign.read_bytes(), b'unrelated-candidate')
        self.assertEqual((stored.status, stored.phase), ('failed', 'reconciled'))
        self.assertEqual(stored.error_code, 'storage_transfer_not_accepted')
        self.assertEqual(stored.released_bytes, 0)
        self.assertEqual(reservation.state, 'released')
        self.assertEqual(after_payload, payload)
        self.assertEqual([call.args[1] for call in calls.await_args_list if call.args[0] == 'deletefile'], [payload['candidate']])

    async def test_native_restart_reconstructs_accepted_manifest_and_preserves_new_protection(self):
        from services.rclone import rclone_service
        for protection in ['policy', 'pin']:
            with self.subTest(protection=protection):
                row, asset, path, operation = await self.native_interrupted_transfer('accepted-' + protection)
                original = path.read_bytes()
                with patch('lifecycle.storage_worker.persist_placement', side_effect=OSError('synthetic manifest interruption')):
                    with self.assertRaisesRegex(OSError, 'manifest interruption'):
                        await self.worker.offload(operation)
                stored, payload, reservation = await self.transfer_evidence(operation)
                async with AsyncSession(self.engine, expire_on_commit=False) as session:
                    accepted = await session.get(StorageAsset, asset.id)
                self.assertEqual(stored.phase, 'accepted')
                self.assertEqual(accepted.remote_path, payload['final'])
                self.assertTrue(accepted.remote_identity)
                self.assertEqual(reservation.state, 'active')
                self.assertIsNone(accepted_placement(row.video_url))
                self.assertEqual(payload['media_identity'], local_media_identity(path))
                self.assertEqual(payload['audio_metadata'], row.audio_metadata)
                if protection == 'policy':
                    settings.STORAGE_POLICY = dict(settings.STORAGE_POLICY, offload_enabled=False, revision=2)
                else:
                    async with AsyncSession(self.engine, expire_on_commit=False) as session:
                        session.add(StorageKeepLocal(id='movie:' + row.id, media_id=row.id, enabled=True))
                        await session.commit()

                restarted = await self.restarted_transfer_worker()
                with patch.object(restarted, '_verify_remote', wraps=restarted._verify_remote) as verified, \
                        patch.object(rclone_service, 'run', wraps=rclone_service.run) as calls:
                    await restarted.reconcile_transfers()
                self.assertIn(payload['final'], [call.args[1] for call in verified.await_args_list])
                stored, _, reservation = await self.transfer_evidence(operation)
                placement = accepted_placement(row.video_url)
                self.assertIsNotNone(placement)
                self.assertEqual(placement['generation'], asset.generation)
                self.assertEqual(placement['remote_identity'], accepted.remote_identity)
                self.assertEqual(accepted_identity(row.video_url, path, audio_metadata=row.audio_metadata)[0], row.source_fingerprint)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(Path(payload['final'].split(':', 1)[1]).read_bytes(), original)
                self.assertFalse(Path(payload['candidate'].split(':', 1)[1]).exists())
                self.assertEqual((stored.status, stored.phase), ('completed', 'retained_local_copy'))
                self.assertEqual(stored.released_bytes, 0)
                self.assertEqual(reservation.state, 'released')
                self.assertEqual([call.args[1] for call in calls.await_args_list if call.args[0] == 'deletefile'], [payload['candidate']])

    async def test_native_restart_after_unlink_preserves_manifest_and_accepted_final(self):
        from services.rclone import rclone_service
        row, asset, path, operation = await self.native_interrupted_transfer('after-unlink')
        original = path.read_bytes()
        native_run = rclone_service.run

        async def interrupt_candidate_cleanup(*args, **kwargs):
            if args[0] == 'deletefile':
                raise OSError('synthetic interruption after local unlink')
            return await native_run(*args, **kwargs)

        with patch.object(rclone_service, 'run', side_effect=interrupt_candidate_cleanup):
            with self.assertRaisesRegex(OSError, 'after local unlink'):
                await self.worker.offload(operation)
        stored, payload, reservation = await self.transfer_evidence(operation)
        placement = accepted_placement(row.video_url)
        self.assertIsNotNone(placement)
        self.assertFalse(path.exists())
        self.assertEqual(stored.released_bytes, len(original))
        self.assertEqual(reservation.state, 'active')
        self.assertEqual(Path(payload['candidate'].split(':', 1)[1]).read_bytes(), original)
        restarted = await self.restarted_transfer_worker()
        with patch.object(rclone_service, 'run', wraps=native_run) as calls:
            await restarted.reconcile_transfers()
        stored, _, reservation = await self.transfer_evidence(operation)
        self.assertEqual(accepted_placement(row.video_url), placement)
        self.assertEqual(Path(payload['final'].split(':', 1)[1]).read_bytes(), original)
        self.assertFalse(path.exists())
        self.assertFalse(Path(payload['candidate'].split(':', 1)[1]).exists())
        self.assertEqual((stored.status, stored.phase), ('completed', 'completed'))
        self.assertEqual(stored.released_bytes, len(original))
        self.assertEqual(reservation.state, 'released')
        self.assertEqual([call.args[1] for call in calls.await_args_list if call.args[0] == 'deletefile'], [payload['candidate']])

    async def test_native_unknown_remote_observation_retains_aged_reservation_and_original(self):
        from services.rclone import RcloneResult, rclone_service
        row, asset, path, operation = await self.native_interrupted_transfer('unknown-remote')
        original = path.read_bytes()
        native_run = rclone_service.run

        async def stop_before_publication(*args, **kwargs):
            if args[0] == 'copyto' and '/.storage-candidates/' in str(args[1]):
                raise OSError('synthetic interruption before final publication')
            return await native_run(*args, **kwargs)

        with patch.object(rclone_service, 'run', side_effect=stop_before_publication):
            with self.assertRaises(OSError):
                await self.worker.offload(operation)
        _, payload, reservation = await self.transfer_evidence(operation)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            for model, identifier in [(StorageOperation, operation.id), (StorageReservation, reservation.id)]:
                stored = await session.get(model, identifier)
                stored.created_at = stored.updated_at = 1
                session.add(stored)
            await session.commit()
        restarted = await self.restarted_transfer_worker()

        async def unknown_final(*args, **kwargs):
            if args[0] == 'lsjson' and args[1] == payload['final']:
                return RcloneResult(124, error_code='rclone_timeout')
            return await native_run(*args, **kwargs)

        with patch.object(rclone_service, 'run', side_effect=unknown_final) as calls:
            await restarted.reconcile_transfers()
            await restarted.reconcile_transfers()
        stored, after_payload, after_reservation = await self.transfer_evidence(operation)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(Path(payload['candidate'].split(':', 1)[1]).read_bytes(), original)
        self.assertIsNone(accepted_placement(row.video_url))
        self.assertEqual((stored.status, stored.phase), ('blocked', 'recovery_required'))
        self.assertEqual(stored.error_code, 'storage_operation_interrupted')
        self.assertEqual(stored.released_bytes, 0)
        self.assertEqual(after_payload, payload)
        self.assertEqual(after_reservation.state, 'active')
        self.assertEqual(after_reservation.created_at, 1)
        self.assertEqual(after_reservation.updated_at, 1)
        self.assertEqual(after_reservation.peak_bytes, reservation.peak_bytes)
        self.assertEqual(after_reservation.written_bytes, reservation.written_bytes)
        self.assertFalse(any(call.args[0] == 'deletefile' for call in calls.await_args_list))

    async def test_refresh_receipts_remain_bound_to_each_human_request(self):
        first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
        first, second = await asyncio.gather(self.service.refresh('one', first_id), self.service.refresh('two', second_id))
        self.assertEqual(first['operation']['id'], first_id)
        self.assertEqual(second['operation']['id'], second_id)
        self.assertEqual((await self.service.operation('one', first_id))['operation']['id'], first_id)
        self.assertEqual((await self.service.operation('two', second_id))['operation']['id'], second_id)
        with self.assertRaises(StorageError):
            await self.service.operation('one', second_id)
        with patch.object(self.worker, 'measure', AsyncMock()):
            await self.worker.tick()
        self.assertEqual((await self.service.operation('two', second_id))['operation']['status'], 'completed')

    async def test_save_rechecks_new_keep_local_protection_after_preview(self):
        row, asset, path = await self.movie(content=b'x' * 1000)
        preview = await self.service.preview('human', self.draft(local_budget_bytes=500), 0)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(StorageKeepLocal(id='movie:' + row.id, media_id=row.id, enabled=True))
            await session.commit()
        with patch('lifecycle.storage_worker.storage_inventory.local_measurement', AsyncMock(return_value={'written_bytes':1000})):
            with self.assertRaises(StorageError) as found:
                await self.service.save('human', preview['review_id'], self.draft(local_budget_bytes=500), 0, str(uuid.uuid4()))
        self.assertEqual(found.exception.code, 'storage_review_protected_data_changed')
        self.assertIsNone(settings.STORAGE_POLICY)

    async def test_capacity_denial_records_pressure_and_admission_resolves_it(self):
        settings.STORAGE_POLICY = self.draft(local_budget_bytes=100, offload_enabled=True, cloud_budget_bytes=1000) | {'revision':1}
        with patch('services.storage_inventory.managed_roots', return_value=[('temporary', self.temporary)]):
            with self.assertRaises(StorageError):
                await self.capacity.reserve('real-writer', 101, self.temporary)
            pressure = await self.worker._measurement('capacity_pressure')
            self.assertTrue(pressure['active'])
            self.assertEqual(pressure['additional_bytes'], 101)
            settings.STORAGE_POLICY['local_budget_bytes'] = 200
            await self.capacity.reserve('real-writer', 101, self.temporary)
        self.assertFalse((await self.worker._measurement('capacity_pressure'))['active'])

    async def test_failed_unlink_never_reports_released_bytes(self):
        row, asset, path = await self.movie()
        await self.accept(row, asset, path)
        operation = StorageOperation(id=str(uuid.uuid4()), kind='cleanup')
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            session.add(operation); await session.commit()
        original = Path.unlink
        def fail_target(selected, *args, **kwargs):
            if selected == path:
                raise OSError('synthetic unlink failure')
            return original(selected, *args, **kwargs)
        with patch.object(self.worker, '_verify_remote', AsyncMock()), patch('lifecycle.storage_worker.direct_recovery_proven', return_value=True), patch.object(Path, 'unlink', fail_target):
            with self.assertRaises(OSError):
                await self.worker._verified_release(operation, asset, 0)
        self.assertTrue(path.exists())
        self.assertEqual(operation.released_bytes, 0)
        self.assertIsNotNone(accepted_placement(row.video_url))

    async def test_cleanup_plan_and_request_receipt_commit_atomically(self):
        preview = await self.service.cleanup_preview('human')
        operation_id = str(uuid.uuid4())
        original = AsyncSession.commit
        once = True
        async def lose_ack(selected):
            nonlocal once
            await original(selected)
            if once:
                once = False
                raise OSError('synthetic post-commit lost acknowledgement')
        with patch.object(AsyncSession, 'commit', lose_ack):
            with self.assertRaises(OSError):
                await self.service.cleanup_execute('human', preview['plan_id'], operation_id)
        self.assertEqual((await self.service.operation('human', operation_id))['operation']['id'], operation_id)
        retry = await self.service.cleanup_execute('human', preview['plan_id'], operation_id)
        self.assertEqual(retry['operation']['id'], operation_id)
        with self.assertRaises(StorageError):
            await self.service.cleanup_execute('different-human', preview['plan_id'], operation_id)

    async def test_direct_recovery_proof_is_bound_to_current_video_bytes(self):
        from lifecycle.storage_worker import direct_recovery_proven
        row, asset, path = await self.movie()
        row.codec, row.container = 'h264', 'mp4'
        row.analysis_metadata_str = json.dumps({'measurement_version':2, 'direct_timeline_compatible':True,
            'duration_provenance':'selected_stream_packets', 'source_fingerprint':row.source_fingerprint,
            'video_fingerprint':asset.video_fingerprint})
        self.assertTrue(direct_recovery_proven(row))
        path.write_bytes(b'changed-video-generation')
        self.assertFalse(direct_recovery_proven(row))

    async def test_offline_readiness_requires_accepted_selected_audio_identity(self):
        from lifecycle.storage_worker import offline_ready
        row, asset, path = await self.movie()
        audio = path.parent / 'audio' / 'en.m4a'
        audio.parent.mkdir(); audio.write_bytes(b'accepted-audio')
        row.audio_metadata = [{'source':'external', 'fileName':'en.m4a', 'default':True}]
        catalog = row.video_url.rsplit('/', 1)[0] + '/audio/en.m4a'
        with patch('lifecycle.storage_worker.direct_recovery_proven', return_value=True):
            self.assertFalse(await offline_ready(row, 'movie'))
            await adopt_asset(row.id, None, catalog, 'audio')
            self.assertTrue(await offline_ready(row, 'movie'))
            audio.write_bytes(b'a-different-audio-generation')
            self.assertFalse(await offline_ready(row, 'movie'))


if __name__ == '__main__':
    unittest.main()
