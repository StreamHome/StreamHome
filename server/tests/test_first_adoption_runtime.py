"""Legacy adoption must open normal traffic only after its exact durable commit."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from lifecycle import bootstrap, state as lifecycle_state


class FirstAdoptionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        import application
        from lifecycle.storage_worker import storage_worker
        self.application = application
        self.main = main
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'server').mkdir()
        (self.root / 'start.sh').write_text('#!/bin/sh\n')
        (self.root / 'update.sh').write_text('#!/bin/sh\n')
        (self.root / '.run').mkdir(mode=0o700)
        self.journal = self.root / '.run/update-state.json'
        self.payload = {
            'transaction_id': str(uuid.uuid4()), 'target_commit': 'a' * 40,
            'current_commit': 'a' * 40, 'phase': 'starting',
            'runtime_committed': False, 'lifecycle_generation': None, 'lifecycle_phase': None,
        }
        self.write()
        env = {
            bootstrap.INSTANCE_ROOT_ENV: str(self.root),
            bootstrap.STARTUP_MODE_ENV: 'update_verify',
            bootstrap.FIRST_ADOPTION_ENV: 'true',
            bootstrap.UPDATE_TRANSACTION_ENV: self.payload['transaction_id'],
            bootstrap.UPDATE_COMMIT_TOKEN_ENV: 'synthetic-private-token',
            'STREAMHOME_BUILD_ID': 'a' * 12,
        }
        self.enterContext(patch.dict(os.environ, env))
        context = bootstrap.evaluate_startup(self.root)
        self.assertEqual(context.mode, bootstrap.StartupMode.UPDATE_VERIFY)
        self.readiness = bootstrap.StartupReadiness(context)
        self.enterContext(patch.object(application, 'STARTUP_CONTEXT', context))
        self.enterContext(patch.object(application, 'STARTUP_READINESS', self.readiness))
        self.enterContext(patch.object(application.state, 'UPDATE_TRANSACTION_ID', ''))
        self.enterContext(patch.object(application.state, 'MAINTENANCE_MODE', True))
        self.enterContext(patch.object(application.state, 'MAINTENANCE_REASON', 'StreamHome is completing guarded startup verification.'))
        self.enterContext(patch.object(application, '_normal_recovery_steps', return_value=()))
        self.start_workers = self.enterContext(patch.object(application, '_start_runtime_workers', AsyncMock()))
        self.stop_workers = self.enterContext(patch.object(application, '_stop_runtime_workers', AsyncMock()))
        self.enterContext(patch.object(storage_worker.measurements, 'start', AsyncMock()))
        self.enterContext(patch.object(application, '_optional_playback_baselines', AsyncMock()))
        app = FastAPI()
        app.add_middleware(application.StartupModeGateMiddleware)

        @app.get('/protected-probe')
        async def protected_probe():
            return {'normalTraffic': True}

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver')
        self.addAsyncCleanup(self.client.aclose)

    def write(self):
        self.journal.write_text(json.dumps(self.payload))

    async def promote(self):
        runtime = self.application._WorkerRuntime()
        await self.application._promote_committed_update(runtime, None)
        for task in runtime.tasks:
            await asyncio.gather(task, return_exceptions=True)

    async def test_commit_opens_workers_and_http_without_shared_identity(self):
        self.assertEqual((await self.client.get('/protected-probe')).status_code, 503)
        self.payload.update(runtime_committed=True, runtime_committed_at=1.0)
        self.write()
        await self.promote()
        self.start_workers.assert_awaited_once()
        self.assertTrue(self.readiness.projection()['workersStarted'])
        self.assertTrue(self.readiness.projection()['generalReady'])
        self.assertEqual((await self.client.get('/protected-probe')).status_code, 200)
        self.assertFalse(self.application.state.MAINTENANCE_MODE)
        self.assertEqual(lifecycle_state.inspect_state(self.root).generation, 0)
        self.assertIsNone(lifecycle_state.inspect_state(self.root).active_operation)
        self.payload['target_commit'] = 'a' * 12 + 'b' * 28
        self.write()
        self.assertEqual((await self.client.get('/protected-probe')).status_code, 503)

    async def test_uncommitted_adoption_never_starts_workers(self):
        await self.promote()
        self.start_workers.assert_not_awaited()
        self.assertEqual(self.readiness.projection()['errorCode'], 'update_adoption_commit_unproven')
        self.assertEqual((await self.client.get('/protected-probe')).status_code, 503)

    async def test_new_shared_operation_during_worker_start_stays_closed(self):
        self.payload.update(runtime_committed=True, runtime_committed_at=1.0)
        self.write()

        async def claim(*unused):
            lifecycle_state.claim_operation_with_credential(self.root, operation_type='restore', owner_kind='restore_controller')

        self.start_workers.side_effect = claim
        await self.promote()
        self.stop_workers.assert_awaited_once()
        self.assertEqual(self.readiness.projection()['errorCode'], 'update_lifecycle_unresolved')
        self.assertEqual((await self.client.get('/protected-probe')).status_code, 503)
