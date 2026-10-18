"""Admin Drive regressions. Execute with tests/run_isolated.py only."""
from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.requests import Request

import config
from config import settings
from db import get_session
from models import AdminDriveJob, AuthSession, DriveSetupJob, User
from routes import admin_drive as drive, auth, setup
from services.rclone import RcloneResult, RcloneService


class AdminDriveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="admin-drive-test-")
        self.root = Path(self.directory.name)
        (self.root / "server/rclone").mkdir(parents=True)
        (self.root / "server/settings.json").write_text(json.dumps({"storage_engine": "LOCAL", "unrelated": {"retained": True}}))
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'test.db'}")
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.session = AuthSession(id="human-a", user_id=1, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600, reauthenticated_at=time.time())
        self.other = AuthSession(id="human-b", user_id=1, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600, reauthenticated_at=time.time())
        self.user = User(id=1, email="test@example.test", password_hash="unused")
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(self.user); db.add(self.session); db.add(self.other); await db.commit()
        self.service = RcloneService()
        self.service.root = self.root
        self.service.config_dir = self.root / "server/rclone"
        self.service.config_path = self.service.config_dir / "rclone.conf"
        self.service.setup_root = self.root / "server/temp/rclone-setup"
        self.service.encryption_supported = lambda: True
        self.service._encrypted_candidate = AsyncMock(side_effect=self.encrypt)
        self.objects = {}
        self.commands = []
        self.delete_failure = False
        self.content_failure = False
        self.listing = []
        self.service.run = AsyncMock(side_effect=self.provider_run)
        self.patches = [patch.object(drive, "rclone_service", self.service), patch.object(setup, "rclone_service", self.service),
                        patch.object(settings, "BASE_DIR", str(self.root)), patch.object(settings, "PUBLIC_URL", "https://watch.example.test"),
                        patch.object(settings, "RCLONE_REMOTE_PATH", ""), patch.object(settings, "STORAGE_ENGINE", "LOCAL"),
                        patch.object(settings, "GOOGLE_DRIVE_AUDIENCE", "external"), patch.object(settings, "GOOGLE_DRIVE_PUBLISHING_STATUS", "production")]
        for p in self.patches: p.start()
        app = self.app = FastAPI()
        app.include_router(drive.router)
        app.include_router(setup.router)
        async def session_dependency():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                yield db
        app.dependency_overrides[get_session] = session_dependency
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://watch.example.test", cookies={auth.AUTH_COOKIE: auth.encode_session_token(self.user, self.session)})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        for p in reversed(self.patches): p.stop()
        self.directory.cleanup()

    async def encrypt(self, source):
        path = self.service.config_dir / "candidate"
        path.write_bytes(b"RCLONE_ENCRYPT_V0:\nsynthetic-ciphertext\n")
        return path

    async def provider_run(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        if args[0] == "rcat":
            self.objects[args[1]] = kwargs["input_data"]
            return RcloneResult(0)
        if args[0] == "cat":
            value = b"wrong" if self.content_failure else self.objects.get(args[1], b"")
            return RcloneResult(0, value.decode()) if value else RcloneResult(1)
        if args[0] == "deletefile":
            if self.delete_failure: return RcloneResult(1)
            self.objects.pop(args[1], None)
            return RcloneResult(0)
        if args[0] == "lsjson":
            if "--include" in args:
                name = args[-1].lstrip("/")
                exists = f"{args[1]}/{name}" in self.objects
                return RcloneResult(0, json.dumps([{"Name": name, "IsDir": False}] if exists else []))
            return RcloneResult(0, json.dumps(self.listing))
        if args[0] == "about": return RcloneResult(0, '{"total":1000,"used":100,"free":900,"private":"removed"}')
        return RcloneResult(0)

    async def add_job(self, *, status="selecting_folder", session=None, **values):
        timestamp = time.time()
        owner = session or self.session
        defaults = dict(id=str(__import__('uuid').uuid4()), auth_session_id=owner.id, active_session_key=owner.id,
            state_hash=hashlib.sha256(b"oauth-state").hexdigest(), status=status, public_url="https://watch.example.test",
            selected_path="StreamHome", created_at=timestamp, updated_at=timestamp, expires_at=timestamp + 600)
        defaults.update(values)
        job = AdminDriveJob(**defaults)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(job); await db.commit()
        self.service.write_job_secret(job.id, {"client_id":"test.apps.googleusercontent.com", "client_secret":"synthetic-secret", "pkce_verifier":"synthetic-verifier"})
        return job

    async def read(self, job_id):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return await db.get(AdminDriveJob, job_id)

    async def test_finish_refreshes_expiring_and_nonexpiring_sqlite_sessions(self):
        for expiring in (True, False):
            with self.subTest(expire_on_commit=expiring):
                job = await self.add_job(status="saving", operation="save", generation=3,
                                         operation_owner=drive._PROCESS_OWNER,
                                         state_hash=__import__('uuid').uuid4().hex)
                key = (job.id, 3)
                drive._ACTIVE_OPERATIONS[key] = asyncio.current_task()
                async with AsyncSession(self.engine, expire_on_commit=expiring) as db:
                    row = await db.get(AdminDriveJob, job.id)
                    returned = await drive._finish(db, row, status="saved", active_session_key=None)
                    self.assertIs(returned, row)
                    self.assertEqual((row.id, row.status, row.generation), (job.id, "saved", 3))
                    self.assertIsNone(row.operation)
                    self.assertIsNone(row.operation_owner)
                    self.assertNotIn(key, drive._ACTIVE_OPERATIONS)
                self.assertEqual((await self.read(job.id)).status, "saved")

    async def test_finish_preserves_stale_generation_and_saved_idempotency(self):
        from sqlalchemy import update
        for expiring in (True, False):
            for saved_winner in (False, True):
                with self.subTest(expire_on_commit=expiring, saved_winner=saved_winner):
                    job = await self.add_job(status="saving", operation="save", generation=3,
                                             operation_owner=drive._PROCESS_OWNER, active_session_key=None,
                                             state_hash=__import__('uuid').uuid4().hex)
                    key = (job.id, 3)
                    drive._ACTIVE_OPERATIONS[key] = asyncio.current_task()
                    async with AsyncSession(self.engine, expire_on_commit=expiring) as db:
                        row = await db.get(AdminDriveJob, job.id)
                        async with AsyncSession(self.engine, expire_on_commit=False) as competitor:
                            await competitor.execute(update(AdminDriveJob).where(AdminDriveJob.id == job.id).values(
                                generation=4, status="saved" if saved_winner else "ready", operation=None,
                                operation_owner=None, progress="Winning generation retained"))
                            await competitor.commit()
                        if saved_winner:
                            self.assertIs(await drive._finish(db, row, status="saved"), row)
                        else:
                            with self.assertRaises(HTTPException) as raised:
                                await drive._finish(db, row, status="saved")
                            self.assertEqual(raised.exception.detail["code"], "drive_job_changed")
                        self.assertEqual(row.generation, 4)
                        self.assertEqual(row.progress, "Winning generation retained")
                        self.assertNotIn(key, drive._ACTIVE_OPERATIONS)

    async def test_finish_commit_errors_preserve_exception_and_cleanup_after_expiry(self):
        for expiring in (True, False):
            for committed in (False, True):
                for cancelled in (False, True):
                    with self.subTest(expire_on_commit=expiring, committed=committed, cancelled=cancelled):
                        job = await self.add_job(status="saving", operation="save", generation=3,
                                                 operation_owner=drive._PROCESS_OWNER, active_session_key=None,
                                                 state_hash=__import__('uuid').uuid4().hex)
                        key = (job.id, 3)
                        drive._ACTIVE_OPERATIONS[key] = asyncio.current_task()
                        failure = asyncio.CancelledError("synthetic cancellation") if cancelled else RuntimeError("synthetic commit failure")
                        async with AsyncSession(self.engine, expire_on_commit=expiring) as db:
                            row = await db.get(AdminDriveJob, job.id)
                            original_commit = db.commit
                            async def fail_commit():
                                if committed:
                                    await original_commit()
                                else:
                                    await db.rollback()
                                raise failure
                            with patch.object(db, "commit", side_effect=fail_commit):
                                with self.assertRaises(type(failure)) as raised:
                                    await drive._finish(db, row, status="saved", active_session_key=None)
                            self.assertIs(raised.exception, failure)
                            self.assertNotIn(key, drive._ACTIVE_OPERATIONS)
                        current = await self.read(job.id)
                        self.assertEqual(current.status, "saved" if committed else "saving")
                        self.assertEqual(current.operation, None if committed else "save")

    async def test_startup_recovers_multiple_jobs_with_default_expiration(self):
        # Execute the actual startup helper in a narrow namespace, avoiding the
        # unrelated application bootstrap/import side effects in this unit test.
        source = Path(__file__).resolve().parents[1] / "application.py"
        tree = ast.parse(source.read_text())
        helper = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "_recover_drive_setup")
        namespace = dict(AsyncSession=AsyncSession, engine=self.engine, select=select,
                         DriveSetupJob=DriveSetupJob, time=time, rclone_service=self.service)
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source), "exec"), namespace)
        jobs = [await self.add_job(status="exchanging_code", operation="oauth", operation_owner="old-process",
                                   generation=number, active_session_key=None,
                                   state_hash=__import__('uuid').uuid4().hex) for number in (2, 3)]
        # The startup helper uses AsyncSession's default expire_on_commit=True.
        await namespace["_recover_drive_setup"]()
        await namespace["_recover_drive_setup"]()
        for job in jobs:
            current = await self.read(job.id)
            self.assertEqual(current.status, "failed")
            self.assertEqual(current.error_code, "drive_operation_interrupted")
            self.assertIsNone(current.operation)

    async def test_receipt_recovery_after_lost_commit_ack_in_expiring_session(self):
        job = await self.add_job(status="saving", operation="save", operation_owner="old-process", generation=5)
        candidate = await self.encrypt(None)
        await asyncio.to_thread(drive._publish, root=self.root, job_id=job.id, generation=5,
                                encrypted_config=candidate, remote_name="streamhome-drive",
                                remote_path="streamhome-drive:StreamHome", audience="external",
                                publishing_status="production")
        before = self.service.config_path.read_bytes()
        key = (job.id, 5)
        drive._ACTIVE_OPERATIONS[key] = asyncio.current_task()
        async with AsyncSession(self.engine) as db:
            row = await db.get(AdminDriveJob, job.id)
            original_commit = db.commit
            async def lose_ack():
                await original_commit()
                raise RuntimeError("synthetic lost commit acknowledgement")
            with patch.object(db, "commit", side_effect=lose_ack):
                with self.assertRaisesRegex(RuntimeError, "lost commit acknowledgement"):
                    await drive._reconcile(db, row)
            self.assertNotIn(key, drive._ACTIVE_OPERATIONS)
        async with AsyncSession(self.engine) as db:
            row = await db.get(AdminDriveJob, job.id)
            await drive._reconcile(db, row)
            self.assertEqual(row.status, "saved")
            self.assertIsNone(row.active_session_key)
            self.assertIsNone(row.operation)
        self.assertEqual(self.service.config_path.read_bytes(), before)
        self.assertEqual(self.service._encrypted_candidate.await_count, 0)
        self.assertFalse(self.service.job_dir(job.id).exists())

    async def test_authentication_reauth_and_machine_isolation(self):
        self.client.cookies.clear()
        self.assertEqual((await self.client.get("/api/system/drive/setup")).status_code, 401)
        self.client.cookies.set(setup.SETUP_COOKIE, setup._setup_token("bootstrap"))
        self.assertEqual((await self.client.get("/api/system/drive/setup")).status_code, 401)
        self.client.cookies.set(auth.AUTH_COOKIE, auth.encode_session_token(self.user, self.other))
        async with AsyncSession(self.engine) as db:
            other = await db.get(AuthSession, self.other.id); other.reauthenticated_at = None; db.add(other); await db.commit()
        response = await self.client.get("/api/system/drive/setup")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "reauthentication_required")

    async def test_owned_job_is_hidden_from_other_human_session(self):
        job = await self.add_job(session=self.other)
        self.assertEqual((await self.client.get(f"/api/system/drive/jobs/{job.id}")).status_code, 404)
        self.assertIsNone((await self.client.get("/api/system/drive/setup")).json()["job"])

    async def test_lost_start_can_reconcile_and_duplicate_start_is_rejected(self):
        payload = {"client_id":"test.apps.googleusercontent.com", "client_secret":"synthetic-secret"}
        response = await self.client.post("/api/system/drive/jobs", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        current = (await self.client.get("/api/system/drive/setup")).json()
        self.assertEqual(current["job"]["id"], data["jobId"])
        self.assertEqual(current["job"]["selectedPath"], "")
        self.assertIsNone(current["job"]["pendingOperation"])
        self.assertEqual(current["job"]["generation"], 0)
        self.assertEqual(current["callbackUrl"], "https://watch.example.test/api/setup/rclone/drive/callback")
        self.assertNotIn("synthetic-secret", json.dumps(current))
        self.assertNotIn("pkce_verifier", json.dumps(current))
        query = parse_qs(urlsplit(data["authorizationUrl"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual((await self.client.post("/api/system/drive/jobs", json=payload)).status_code, 409)
        self.assertEqual((await self.client.post("/api/system/drive/jobs", json={**payload, "public_url":"https://attacker.test"})).status_code, 422)

    async def test_existing_configuration_blocks_first_connection_even_if_invalid(self):
        self.service.config_path.write_text("invalid existing configuration")
        response = await self.client.post("/api/system/drive/jobs", json={"client_id":"test.apps.googleusercontent.com","client_secret":"synthetic"})
        self.assertEqual(response.status_code, 409)
        self.assertTrue((await self.client.get("/api/system/drive/setup")).json()["configured"])
        self.assertEqual(self.service.config_path.read_text(), "invalid existing configuration")

    async def test_expiry_and_cancel_are_owned_and_idempotent(self):
        job = await self.add_job(expires_at=time.time() - 1)
        self.assertEqual((await self.client.get(f"/api/system/drive/jobs/{job.id}")).json()["status"], "expired")
        self.assertEqual((await self.client.post(f"/api/system/drive/jobs/{job.id}/select-folder", json={"path":"Other"})).status_code, 409)
        for _ in range(2):
            response = await self.client.delete(f"/api/system/drive/jobs/{job.id}")
            self.assertEqual(response.json()["status"], "cancelled")
        self.assertFalse(self.service.job_dir(job.id).exists())

    async def test_callback_exchanges_once_and_never_enters_setup(self):
        job = await self.add_job(status="authorizing")
        token = type("TokenResponse", (), {"status_code":200, "json": lambda _: {"access_token":"synthetic-access","refresh_token":"synthetic-refresh"}})()
        with patch.object(drive.httpx.AsyncClient, "post", AsyncMock(return_value=token)) as exchange:
            request = Request({"type":"http","method":"GET","scheme":"https","server":("watch.example.test",443),"path":"/api/setup/rclone/drive/callback","headers":[],"query_string":b"state=oauth-state&code=synthetic-code"})
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                response = await setup.drive_oauth_callback(request, db)
                self.assertEqual(response.headers["location"], "https://watch.example.test/admin/drive/callback?drive=connected")
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                await setup.drive_oauth_callback(request, db)
            self.assertEqual(exchange.await_count, 1)
        self.assertEqual((await self.read(job.id)).status, "selecting_folder")

    async def test_revoked_owner_callback_cannot_exchange(self):
        job = await self.add_job(status="authorizing")
        async with AsyncSession(self.engine) as db:
            session = await db.get(AuthSession,self.session.id); session.revoked_at=time.time(); db.add(session); await db.commit()
        with patch.object(drive.httpx.AsyncClient, "post", AsyncMock()) as exchange:
            request = Request({"type":"http","method":"GET","scheme":"https","server":("watch.example.test",443),"path":"/api/setup/rclone/drive/callback","headers":[],"query_string":b"state=oauth-state&code=synthetic-code"})
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                response = await setup.drive_oauth_callback(request, db)
            self.assertTrue(response.headers["location"].endswith("/admin/drive/callback?drive=error"))
            exchange.assert_not_awaited()
        self.assertEqual((await self.read(job.id)).status,"authorizing")

    async def test_folder_phase_path_and_inspection_bounds(self):
        job = await self.add_job(status="authorizing")
        response = await self.client.get(f"/api/system/drive/jobs/{job.id}/folders")
        self.assertEqual(response.status_code,409)
        async with AsyncSession(self.engine) as db:
            row = await db.get(AdminDriveJob,job.id);row.status="selecting_folder";db.add(row);await db.commit()
        self.assertEqual((await self.client.post(f"/api/system/drive/jobs/{job.id}/select-folder",json={"path":"../escape"})).status_code,422)
        self.listing = [{"Name":"child","IsDir":True},{"Name":"file","IsDir":False}]
        response = await self.client.get(f"/api/system/drive/jobs/{job.id}/folders",params={"path":"StreamHome"})
        self.assertEqual(response.json()["folders"],[{"name":"child","path":"StreamHome/child","id":None}])
        self.assertTrue(all(kwargs["hard_output_limit"] <= 65536 for _,kwargs in self.commands))
        self.listing = [{"Name":str(i),"IsDir":True} for i in range(201)]
        self.assertEqual((await self.client.get(f"/api/system/drive/jobs/{job.id}/folders")).status_code,422)

    async def test_test_verifies_content_and_deletion_before_ready(self):
        job = await self.add_job()
        response = await self.client.post(f"/api/system/drive/jobs/{job.id}/test")
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["job"]["status"],"ready")
        self.assertEqual(response.json()["quota"],{"total":1000,"used":100,"free":900})
        self.assertEqual(self.objects,{})
        self.assertTrue(any(args[0]=="cat" for args,_ in self.commands))
        self.assertTrue(any(args[0]=="deletefile" for args,_ in self.commands))

    async def test_content_mismatch_cannot_pass(self):
        job = await self.add_job();self.content_failure=True
        response = await self.client.post(f"/api/system/drive/jobs/{job.id}/test")
        self.assertEqual(response.status_code,422)
        self.assertEqual(response.json()["detail"]["code"],"drive_test_content_failed")
        self.assertEqual((await self.read(job.id)).status,"selecting_folder")
        self.assertEqual(self.objects,{})

    async def test_failed_deletion_blocks_save_and_folder_change_until_cleaned(self):
        job = await self.add_job();self.delete_failure=True
        response = await self.client.post(f"/api/system/drive/jobs/{job.id}/test")
        self.assertEqual(response.json()["detail"]["code"],"drive_test_cleanup_failed")
        self.assertIsNotNone((await self.read(job.id)).health_name)
        self.assertEqual((await self.client.post(f"/api/system/drive/jobs/{job.id}/save")).status_code,409)
        self.assertEqual((await self.client.post(f"/api/system/drive/jobs/{job.id}/select-folder",json={"path":"Other"})).status_code,409)
        self.delete_failure=False
        self.assertEqual((await self.client.delete(f"/api/system/drive/jobs/{job.id}")).json()["status"],"cancelled")
        self.assertEqual(self.objects,{})

    async def test_concurrent_phase_claims_serialize_external_work(self):
        job=await self.add_job();entered=asyncio.Event();release=asyncio.Event()
        original=self.provider_run
        async def blocked(*args,**kwargs):
            if args[0]=="rcat":entered.set();await release.wait()
            return await original(*args,**kwargs)
        self.service.run.side_effect=blocked
        first=asyncio.create_task(self.client.post(f"/api/system/drive/jobs/{job.id}/test"))
        await entered.wait()
        try:
            for action in (self.client.post(f"/api/system/drive/jobs/{job.id}/save"),self.client.delete(f"/api/system/drive/jobs/{job.id}"),self.client.post(f"/api/system/drive/jobs/{job.id}/select-folder",json={"path":"Other"})):
                self.assertEqual((await action).status_code,409)
        finally:release.set()
        self.assertEqual((await first).status_code,200)
        self.assertEqual((await self.read(job.id)).status,"ready")

    async def test_save_receipt_is_idempotent_and_preserves_storage_and_settings(self):
        job=await self.add_job(status="ready")
        for _ in range(2):
            response=await self.client.post(f"/api/system/drive/jobs/{job.id}/save")
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()["job"]["status"],"saved")
        data=json.loads((self.root/"server/settings.json").read_text())
        self.assertEqual(data["storage_engine"],"LOCAL")
        self.assertEqual(data["unrelated"],{"retained":True})
        self.assertEqual(settings.STORAGE_ENGINE,"LOCAL")
        self.assertEqual(settings.RCLONE_REMOTE_PATH,"streamhome-drive:StreamHome")
        self.assertEqual(self.service._encrypted_candidate.await_count,1)

    async def test_lost_save_database_ack_is_reconciled_from_durable_receipt(self):
        job=await self.add_job(status="ready")
        original=drive._finish
        async def lose(db,row,**kwargs):
            if kwargs.get("status")=="saved":raise RuntimeError("synthetic lost acknowledgement")
            return await original(db,row,**kwargs)
        with patch.object(drive,"_finish",side_effect=lose):
            with self.assertRaises(RuntimeError):
                await self.client.post(f"/api/system/drive/jobs/{job.id}/save")
        self.assertEqual((await self.read(job.id)).status,"saving")
        response=await self.client.get("/api/system/drive/setup")
        self.assertEqual(response.json()["job"]["status"],"saved")
        self.assertTrue(response.json()["configured"])
        self.assertEqual((await self.client.post(f"/api/system/drive/jobs/{job.id}/save")).status_code,200)

    async def test_active_config_race_cannot_replace_connection(self):
        job=await self.add_job(status="ready")
        original=self.encrypt
        async def competitor(source):
            candidate=await original(source)
            self.service.config_path.write_bytes(b"existing-owner")
            return candidate
        self.service._encrypted_candidate.side_effect=competitor
        response=await self.client.post(f"/api/system/drive/jobs/{job.id}/save")
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.service.config_path.read_bytes(),b"existing-owner")
        self.assertEqual(json.loads((self.root/"server/settings.json").read_text())["storage_engine"],"LOCAL")

    async def test_old_process_phase_is_never_inferred_successful(self):
        job=await self.add_job(status="exchanging_code",operation="oauth",operation_owner="old-process",generation=4)
        response=await self.client.get(f"/api/system/drive/jobs/{job.id}")
        self.assertEqual(response.json()["status"],"failed")
        self.assertEqual(response.json()["errorCode"],"drive_operation_interrupted")

    async def test_saved_job_cannot_be_cancelled_or_reused_for_cloud_mutation(self):
        job=await self.add_job(status="ready")
        await self.client.post(f"/api/system/drive/jobs/{job.id}/save")
        self.assertEqual((await self.client.delete(f"/api/system/drive/jobs/{job.id}")).status_code,409)
        self.assertEqual((await self.client.post(f"/api/system/drive/jobs/{job.id}/folders",json={"path":"Other"})).status_code,409)

    async def test_uncertain_mkdir_survives_restart_until_positive_parent_observation(self):
        job = await self.add_job()
        original = self.provider_run
        async def timeout_mkdir(*args, **kwargs):
            if args[0] == "mkdir": return RcloneResult(124, error_code="rclone_timeout")
            return await original(*args, **kwargs)
        self.service.run.side_effect = timeout_mkdir
        response = await self.client.post(f"/api/system/drive/jobs/{job.id}/folders", json={"path":"StreamHome/New folder"})
        self.assertEqual(response.status_code, 503)
        current = (await self.client.get("/api/system/drive/setup")).json()["job"]
        self.assertEqual(current["pendingOperation"], "mkdir_uncertain")
        self.assertEqual(current["pendingFolderPath"], "StreamHome/New folder")
        with patch.object(drive, "_PROCESS_OWNER", "99999999:unknown:restarted"):
            current = (await self.client.get("/api/system/drive/setup")).json()["job"]
        self.assertEqual(current["pendingOperation"], "mkdir_uncertain")
        self.assertEqual((await self.client.post(f"/api/system/drive/jobs/{job.id}/folders",json={"path":"StreamHome/New folder"})).status_code, 409)
        self.listing = [{"Name":"New folder", "IsDir":True}]
        current = (await self.client.get("/api/system/drive/setup")).json()["job"]
        self.assertIsNone(current["pendingOperation"])
        self.assertIsNone(current["pendingFolderPath"])
        self.assertEqual(len([call for call in self.service.run.call_args_list if call.args[0] == "mkdir"]), 1)

    async def test_uncertain_mkdir_can_cancel_without_deleting_unknown_folder(self):
        job = await self.add_job(operation="mkdir_uncertain", operation_owner=None, pending_folder_path="StreamHome/Unknown", generation=1)
        response = await self.client.delete(f"/api/system/drive/jobs/{job.id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertFalse(any(args[0] in {"delete", "deletefile", "purge", "rmdir"} for args, _ in self.commands))

    async def test_test_cancellation_joins_verified_cleanup(self):
        job = await self.add_job()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.provider_run
        async def blocked(*args, **kwargs):
            if args[0] == "cat":
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)
        self.service.run.side_effect = blocked
        task = asyncio.create_task(self.client.post(f"/api/system/drive/jobs/{job.id}/test"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        current = await self.read(job.id)
        self.assertEqual(current.status, "selecting_folder")
        self.assertEqual(current.error_code, "drive_test_interrupted")
        self.assertIsNone(current.health_name)
        self.assertEqual(self.objects, {})

    async def test_create_ack_loss_stays_pending_until_positive_observation(self):
        job = await self.add_job()
        original = drive._finish
        async def lose_ack(db, row, **kwargs):
            if kwargs.get("pending_folder_path", "missing") is None:
                raise RuntimeError("synthetic lost database acknowledgement")
            return await original(db, row, **kwargs)
        with patch.object(drive, "_finish", side_effect=lose_ack):
            task = asyncio.create_task(self.client.post(f"/api/system/drive/jobs/{job.id}/folders", json={"path":"StreamHome/New"}))
            with self.assertRaises(RuntimeError): await task
        current = (await self.client.get("/api/system/drive/setup")).json()["job"]
        self.assertEqual(current["pendingOperation"], "mkdir_uncertain")
        self.listing = [{"Name":"New", "IsDir":True}]
        current = (await self.client.get("/api/system/drive/setup")).json()["job"]
        self.assertIsNone(current["pendingOperation"])

    async def test_settings_save_waits_for_publication_and_keeps_drive_fields(self):
        entered, release = threading.Event(), threading.Event()
        original = drive.publish_drive_connection
        candidate = await self.encrypt(None)
        job = await self.add_job(status="ready", generation=1)
        def blocked(**kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(**kwargs)
        with patch.object(drive, "publish_drive_connection", side_effect=blocked), patch.object(config, "config_dir", str(self.root / "server")), patch.object(settings,"HEVC_COMPRESSION_MODE","auto"):
            publication = asyncio.create_task(asyncio.to_thread(drive._publish, root=self.root, job_id=job.id, generation=1, encrypted_config=candidate, remote_name="streamhome-drive", remote_path="streamhome-drive:StreamHome", audience="external", publishing_status="production"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            def policy_writer():
                settings.HEVC_COMPRESSION_MODE = "on"
                settings.save_to_json()
            policy = asyncio.create_task(asyncio.to_thread(policy_writer))
            await asyncio.sleep(0.02)
            self.assertFalse(policy.done())
            release.set()
            await publication
            await policy
            data = json.loads((self.root / "server/settings.json").read_text())
        self.assertEqual(data["rclone_remote_path"], "streamhome-drive:StreamHome")
        self.assertEqual(data["hevc_compression_mode"], "on")
        self.assertEqual(data["storage_engine"], "LOCAL")

    async def test_simultaneous_cas_loser_does_not_retire_winning_live_operation(self):
        job = await self.add_job()
        loaded, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        count = 0
        original_claim, original_run = drive._claim, self.provider_run
        async def simultaneous(db, row, operation, *args, **kwargs):
            nonlocal count
            if operation == "test":
                count += 1
                if count == 2: loaded.set()
                await loaded.wait()
            return await original_claim(db, row, operation, *args, **kwargs)
        async def blocked(*args, **kwargs):
            if args[0] == "rcat": entered.set(); await release.wait()
            return await original_run(*args, **kwargs)
        self.service.run.side_effect = blocked
        with patch.object(drive, "_claim", side_effect=simultaneous):
            tasks = [asyncio.create_task(self.client.post(f"/api/system/drive/jobs/{job.id}/test")) for _ in range(2)]
            try:
                await asyncio.wait_for(entered.wait(), 5)
                done, pending = await asyncio.wait(tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                self.assertEqual(len(done), 1)
                self.assertEqual(next(iter(done)).result().status_code, 409)
                current = (await self.client.get(f"/api/system/drive/jobs/{job.id}")).json()
                self.assertEqual(current["status"], "testing")
                self.assertEqual(current["pendingOperation"], "test")
                self.assertEqual((await self.client.delete(f"/api/system/drive/jobs/{job.id}")).status_code, 409)
            finally:
                release.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(sorted(result.status_code for result in results), [200,409])

    async def test_stale_storage_policy_waiting_behind_save_cannot_erase_connection(self):
        from lifecycle.bootstrap import evaluate_startup, require_startup_context
        try:
            require_startup_context()
        except RuntimeError:
            evaluate_startup(install=True)
        import application
        self.app.post("/api/system/settings", response_model=application.SystemSettingsResponse)(application.save_system_settings)
        job = await self.add_job(status="ready")
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.encrypt
        async def blocked(source):
            entered.set()
            await release.wait()
            return await original(source)
        self.service._encrypted_candidate.side_effect = blocked
        with patch.object(application, "rclone_service", self.service):
            saving = asyncio.create_task(self.client.post(f"/api/system/drive/jobs/{job.id}/save"))
            await entered.wait()
            policy = asyncio.create_task(self.client.post("/api/system/settings", json={"storageEngine":"LOCAL", "rcloneRemotePath":"", "hevcCompressionMode":"auto"}))
            await asyncio.sleep(0.02)
            self.assertFalse(policy.done())
            release.set()
            saved, stale = await asyncio.gather(saving, policy)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "drive_target_changed")
        self.assertEqual(settings.RCLONE_REMOTE_PATH, "streamhome-drive:StreamHome")
        self.assertEqual((await self.client.get("/api/system/drive/setup")).json()["job"]["status"], "saved")

    async def test_bounded_library_recognition_reads_metadata_without_catalog_import(self):
        from models import Movie
        job = await self.add_job()
        original = self.provider_run
        async def library(*args, **kwargs):
            if args[0] == "lsjson":
                children = [{"Name":"Movies", "IsDir":True}] if args[1] == "streamhome-drive:StreamHome" else [{"Name":"Film", "IsDir":True}]
                return RcloneResult(0, json.dumps(children))
            if args[0] == "cat":
                return RcloneResult(0, json.dumps({"media_type":"movie", "tmdb_id":23, "title":"Synthetic Film", "catalog_source":"server"}))
            return await original(*args, **kwargs)
        self.service.run.side_effect = library
        response = await self.client.post(f"/api/system/drive/jobs/{job.id}/select-folder", json={"path":"StreamHome"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["libraryState"], "streamhome")
        self.assertEqual(len(self.service.run.call_args_list), 3)
        self.assertTrue(all(call.kwargs["hard_output_limit"] <= 65536 for call in self.service.run.call_args_list))
        async with AsyncSession(self.engine) as db:
            self.assertIsNone((await db.exec(select(Movie))).first())

    async def test_admin_gets_obey_configuration_write_admission(self):
        from lifecycle.bootstrap import evaluate_startup, require_startup_context
        from lifecycle.coordination import EffectClass, RuntimeCoordinator, use_runtime_coordinator_for_testing
        try: require_startup_context()
        except RuntimeError: evaluate_startup(install=True)
        import application
        self.app.add_middleware(application.RuntimeWriteCoordinationMiddleware)
        coordinator = RuntimeCoordinator()
        coordinator.close_admission([EffectClass.CONFIG_WRITE], operation_id=str(__import__('uuid').uuid4()), generation=1)
        with use_runtime_coordinator_for_testing(coordinator):
            response = await self.client.get("/api/system/drive/setup")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "lifecycle_work_blocked")


class BoundedRcloneTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_pipe_read_rejects_oversized_output(self):
        process=await asyncio.create_subprocess_exec(sys.executable,"-c","import sys;sys.stdout.write('x'*100000)",stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        with self.assertRaises(RuntimeError):
            await RcloneService._bounded_communicate(process,None,1024)
        if process.returncode is None:process.kill()
        await process.wait()

    async def test_native_large_producer_is_reaped_after_hard_limit(self):
        service = RcloneService()
        with tempfile.TemporaryDirectory() as directory:
            service.config_path = Path(directory) / "rclone.conf"
            with patch.object(service, "command", return_value=[sys.executable,"-c","import sys,time;sys.stdout.write('x'*1000000);sys.stdout.flush();time.sleep(30)"]):
                result = await asyncio.wait_for(service.run("version", hard_output_limit=1024), timeout=10)
        self.assertEqual(result.error_code, "rclone_output_limit")

    async def test_cancelled_encryption_removes_plaintext_candidate(self):
        service = RcloneService()
        with tempfile.TemporaryDirectory() as directory:
            service.config_dir = Path(directory)
            source = Path(directory) / "source"
            source.write_text("synthetic plaintext OAuth credentials")
            with patch.object(service,"encryption_supported",return_value=True), patch.object(service,"run",AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError): await service._encrypted_candidate(source)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["source"])


if __name__ == "__main__":
    unittest.main()
