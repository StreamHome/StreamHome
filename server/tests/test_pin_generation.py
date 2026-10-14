"""Actual profile save/unlock and playback revocation on concurrent SQLite writers."""
from __future__ import annotations

import asyncio
import importlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.requests import Request

from lifecycle.bootstrap import StartupMode
from models import AuthSession, Movie, PlaybackRun, PlaybackSession, Profile, User
from routes import playback
from services.profile_security import hash_profile_pin, require_profile_access, verify_profile_pin


def browser_request(session: AuthSession) -> Request:
    request = Request({
        "type": "http", "method": "POST", "path": "/api/profiles/protected/unlock",
        "headers": [], "client": ("127.0.0.1", 53000), "scheme": "http",
        "server": ("localhost", 53001), "query_string": b"",
    })
    request.state.auth_session = session
    return request


class ProfilePinGenerationRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="streamhome-pin-generation-")
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.directory.name) / 'profiles.db'}",
            connect_args={"timeout": 5},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def pragmas(connection, _record):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")

        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        context = SimpleNamespace(mode=StartupMode.NORMAL, guarded=False)
        with patch("lifecycle.bootstrap.require_startup_context", return_value=context), patch(
            "lifecycle.bootstrap.get_startup_readiness", return_value=SimpleNamespace(context=context)
        ):
            self.application = importlib.import_module("application")
        self.engine_patch = patch.object(self.application, "engine", self.engine)
        self.engine_patch.start()
        self.user = User(id=91, email="synthetic@example.test", password_hash="unused")
        self.session = AuthSession(
            id="synthetic-session", user_id=91, created_at=time.time(),
            last_seen_at=time.time(), expires_at=time.time() + 3600,
        )
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(self.user)
            db.add(self.session)
            db.add(Profile(id="protected", name="Protected", pin_enabled=True,
                           pin_hash=hash_profile_pin("1111"), pin_version=1))
            db.add(Movie(id="m_synthetic", title="Synthetic", description="",
                         thumbnail_url="", video_url="/media/synthetic.mp4", duration="1m",
                         release_year=2026, source_fingerprint="synthetic-fingerprint"))
            db.add(PlaybackRun(id="synthetic-run", profile_id="protected",
                               auth_session_id=self.session.id, movie_id="m_synthetic",
                               source_fingerprint="synthetic-fingerprint", lifecycle_state="active",
                               sequence_number=1))
            await db.commit()

    async def asyncTearDown(self):
        self.engine_patch.stop()
        await self.engine.dispose()
        self.directory.cleanup()

    def save_request(self, **values):
        return self.application.ProfileSaveRequest(id="protected", name="Protected", **values)

    async def saved_profile(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return await db.get(Profile, "protected")

    async def unlocked_ticket(self, pin):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
        result = await self.application.unlock_profile(
            "protected", self.application.ProfileUnlockRequest(pin=pin),
            browser_request(session), self.user, session,
        )
        self.assertTrue(result["verified"])
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
            ticket, _ = playback.issue_playback_ticket(
                self.user, session, "protected", "synthetic-run", "m_synthetic", "synthetic-fingerprint",
            )
            await playback.validate_playback_ticket(ticket, "m_synthetic", db)
            await playback.authorized_run(db, browser_request(session), "synthetic-run")
            return ticket, session.selected_profile_pin_version

    async def concurrent_saves(self, first_request, second_request, between=None):
        """Both actual routes are pending before either writer reaches SQLite."""
        both_arrived = asyncio.Event()
        second_may_write = asyncio.Event()
        arrived = 0

        class ScheduledSession(AsyncSession):
            async def execute(session, statement, *args, **kwargs):
                nonlocal arrived
                task_name = asyncio.current_task().get_name()
                if getattr(statement, "is_update", False) and task_name in {"first-pin-save", "second-pin-save"}:
                    arrived += 1
                    if arrived == 2:
                        both_arrived.set()
                    await both_arrived.wait()
                    if task_name == "second-pin-save":
                        await second_may_write.wait()
                return await super().execute(statement, *args, **kwargs)

        with patch.object(self.application, "AsyncSession", ScheduledSession):
            first = asyncio.create_task(self.application.save_profile(first_request, self.user), name="first-pin-save")
            second = asyncio.create_task(self.application.save_profile(second_request, self.user), name="second-pin-save")
            try:
                await asyncio.wait_for(first, timeout=10)
                result = await between() if between else None
                second_may_write.set()
                await asyncio.wait_for(second, timeout=10)
                return result
            finally:
                second_may_write.set()
                for task in (first, second):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(first, second, return_exceptions=True)

    async def test_concurrent_new_pins_revoke_actual_unlock_ticket_run_and_progress(self):
        ticket, granted_version = await self.concurrent_saves(
            self.save_request(pin_enabled=True, pin="2222"),
            self.save_request(pin_enabled=True, pin="3333"),
            lambda: self.unlocked_ticket("2222"),
        )
        profile = await self.saved_profile()
        self.assertTrue(verify_profile_pin("3333", profile.pin_hash))
        self.assertEqual(granted_version, 2)
        self.assertEqual(profile.pin_version, 3)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
            for operation in (
                playback.validate_playback_ticket(ticket, "m_synthetic", db),
                playback.authorized_run(db, browser_request(session), "synthetic-run"),
                playback.update_playback_progress(
                    "synthetic-run", playback.PlaybackProgressRequest(timestamp=20, duration_watched=1, sequence_number=1),
                    browser_request(session), db, self.user,
                ),
            ):
                with self.assertRaises(HTTPException) as caught:
                    await operation
                self.assertEqual(caught.exception.status_code, 403)
                self.assertEqual(caught.exception.detail["code"], "profile_pin_required")
            self.assertEqual((await db.get(PlaybackRun, "synthetic-run")).sequence_number, 1)
            self.assertEqual((await db.exec(select(PlaybackSession))).all(), [])
        # Only the newest PIN can restore the existing run's profile grant.
        with self.assertRaises(HTTPException):
            await self.unlocked_ticket("2222")
        _, latest_grant = await self.unlocked_ticket("3333")
        self.assertEqual(latest_grant, 3)

    async def test_concurrent_empty_pin_preserves_newer_pin_and_generation(self):
        ticket, granted_version = await self.concurrent_saves(
            self.save_request(pin_enabled=True, pin="2222"),
            self.save_request(pin_enabled=True, pin=""),
            lambda: self.unlocked_ticket("2222"),
        )
        profile = await self.saved_profile()
        self.assertTrue(verify_profile_pin("2222", profile.pin_hash))
        self.assertEqual(profile.pin_version, granted_version)
        async with AsyncSession(self.engine) as db:
            await playback.validate_playback_ticket(ticket, "m_synthetic", db)

    async def test_concurrent_name_only_edit_preserves_newer_pin_and_generation(self):
        await self.concurrent_saves(
            self.save_request(pin_enabled=True, pin="2222"),
            self.application.ProfileSaveRequest(id="protected", name="Renamed"),
        )
        profile = await self.saved_profile()
        self.assertEqual(profile.name, "Renamed")
        self.assertTrue(profile.pin_enabled)
        self.assertTrue(verify_profile_pin("2222", profile.pin_hash))
        self.assertEqual(profile.pin_version, 2)

    async def test_concurrent_disable_cannot_be_undone_with_an_old_empty_pin_form(self):
        with self.assertRaises(HTTPException) as caught:
            await self.concurrent_saves(
                self.save_request(pin_enabled=False), self.save_request(pin_enabled=True, pin=""),
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "profile_pin_required")
        profile = await self.saved_profile()
        self.assertFalse(profile.pin_enabled)
        self.assertIsNone(profile.pin_hash)
        self.assertEqual(profile.pin_version, 2)

    async def test_enable_disable_and_unchanged_form_increment_only_security_changes(self):
        await self.application.save_profile(self.save_request(pin_enabled=True, pin=None), self.user)
        self.assertEqual((await self.saved_profile()).pin_version, 1)
        await self.application.save_profile(self.save_request(pin_enabled=False), self.user)
        self.assertEqual((await self.saved_profile()).pin_version, 2)
        await self.application.save_profile(self.save_request(pin_enabled=False), self.user)
        self.assertEqual((await self.saved_profile()).pin_version, 2)
        with self.assertRaises(HTTPException):
            await self.application.save_profile(self.save_request(pin_enabled=True), self.user)
        await self.application.save_profile(self.save_request(pin_enabled=True, pin="4444"), self.user)
        profile = await self.saved_profile()
        self.assertTrue(verify_profile_pin("4444", profile.pin_hash))
        self.assertEqual(profile.pin_version, 3)

    async def test_concurrent_enable_requests_each_advance_unprotected_profile_generation(self):
        await self.application.save_profile(self.save_request(pin_enabled=False), self.user)
        ticket, granted_version = await self.concurrent_saves(
            self.save_request(pin_enabled=True, pin="2222"),
            self.save_request(pin_enabled=True, pin="3333"),
            lambda: self.unlocked_ticket("2222"),
        )
        profile = await self.saved_profile()
        self.assertEqual(granted_version, 3)
        self.assertEqual(profile.pin_version, 4)
        self.assertTrue(verify_profile_pin("3333", profile.pin_hash))
        async with AsyncSession(self.engine) as db:
            with self.assertRaises(HTTPException) as caught:
                await playback.validate_playback_ticket(ticket, "m_synthetic", db)
            self.assertEqual(caught.exception.detail["code"], "profile_pin_required")

    async def test_parallel_new_pins_each_advance_current_database_generation(self):
        await asyncio.gather(*(
            self.application.save_profile(self.save_request(pin_enabled=True, pin=f"{value:04d}"), self.user)
            for value in range(2000, 2006)
        ))
        profile = await self.saved_profile()
        self.assertEqual(profile.pin_version, 7)
        self.assertTrue(any(verify_profile_pin(f"{value:04d}", profile.pin_hash) for value in range(2000, 2006)))

    async def test_new_profile_generation_and_invalid_pin_do_not_mutate_existing_profile(self):
        for profile_id, enabled, pin, expected_version in (
            ("new-plain", False, None, 0), ("new-protected", True, "5555", 1),
        ):
            await self.application.save_profile(self.application.ProfileSaveRequest(
                id=profile_id, name="New", pin_enabled=enabled, pin=pin,
            ), self.user)
            async with AsyncSession(self.engine) as db:
                self.assertEqual((await db.get(Profile, profile_id)).pin_version, expected_version)
        for pin in ("12", "not-a-pin"):
            with self.assertRaises(HTTPException) as caught:
                await self.application.save_profile(self.save_request(pin_enabled=True, pin=pin), self.user)
            self.assertEqual(caught.exception.status_code, 422)
        with self.assertRaises(HTTPException):
            await self.application.save_profile(self.application.ProfileSaveRequest(
                id="missing-pin", name="Invalid", pin_enabled=True,
            ), self.user)
        self.assertEqual((await self.saved_profile()).pin_version, 1)
        async with AsyncSession(self.engine) as db:
            self.assertIsNone(await db.get(Profile, "missing-pin"))

    async def test_wrong_then_correct_pin_can_clear_failure_and_create_a_verified_grant(self):
        with self.assertRaises(HTTPException) as caught:
            await self.unlocked_ticket("9999")
        self.assertEqual(caught.exception.status_code, 401)
        _, granted_version = await self.unlocked_ticket("1111")
        self.assertEqual(granted_version, 1)

    async def unlock_during_security_change(self, old_pin, new_pin):
        verified = asyncio.Event()
        continue_unlock = asyncio.Event()
        clear_failure = self.application.clear_rate_limit

        async def scheduled_clear(*args, **kwargs):
            verified.set()
            await continue_unlock.wait()
            return await clear_failure(*args, **kwargs)

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            old_session = await db.get(AuthSession, self.session.id)
        with patch.object(self.application, "clear_rate_limit", scheduled_clear):
            unlock = asyncio.create_task(self.application.unlock_profile(
                "protected", self.application.ProfileUnlockRequest(pin=old_pin),
                browser_request(old_session), self.user, old_session,
            ))
            try:
                await asyncio.wait_for(verified.wait(), timeout=10)
                await self.application.save_profile(self.save_request(pin_enabled=True, pin=new_pin), self.user)
                continue_unlock.set()
                with self.assertRaises(HTTPException) as denied:
                    await asyncio.wait_for(unlock, timeout=10)
                self.assertEqual(denied.exception.status_code, 403)
                self.assertEqual(denied.exception.detail["code"], "profile_pin_required")
            finally:
                continue_unlock.set()
                if not unlock.done():
                    unlock.cancel()
                await asyncio.gather(unlock, return_exceptions=True)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
            ticket, _ = playback.issue_playback_ticket(
                self.user, session, "protected", "synthetic-run", "m_synthetic", "synthetic-fingerprint",
            )
            for operation in (
                playback.validate_playback_ticket(ticket, "m_synthetic", db),
                playback.authorized_run(db, browser_request(session), "synthetic-run"),
            ):
                with self.assertRaises(HTTPException) as caught:
                    await operation
                self.assertEqual(caught.exception.status_code, 403)
                self.assertEqual(caught.exception.detail["code"], "profile_pin_required")
            return session.selected_profile_pin_version

    async def test_clear_failure_commit_cannot_grant_a_newer_unverified_pin_generation(self):
        with self.assertRaises(HTTPException):
            await self.unlocked_ticket("9999")
        granted_version = await self.unlock_during_security_change("1111", "2222")
        self.assertIsNone(granted_version)
        profile = await self.saved_profile()
        self.assertEqual(profile.pin_version, 2)
        self.assertTrue(verify_profile_pin("2222", profile.pin_hash))

    async def test_unprotected_unlock_cannot_grant_concurrently_enabled_protection(self):
        await self.application.save_profile(self.save_request(pin_enabled=False), self.user)
        self.assertIsNone(await self.unlock_during_security_change("", "2222"))
        self.assertEqual((await self.saved_profile()).pin_version, 3)

    async def assert_replacement_protection_denies_stale_session(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
            self.assertIsNone(session.selected_profile_id)
            self.assertIsNone(session.selected_profile_pin_version)
            self.assertIsNone(await db.get(PlaybackRun, "synthetic-run"))
            db.add(PlaybackRun(
                id="replacement-run", profile_id="protected", auth_session_id=session.id,
                movie_id="m_synthetic", source_fingerprint="synthetic-fingerprint",
                lifecycle_state="active", sequence_number=1,
            ))
            await db.commit()
            ticket, _ = playback.issue_playback_ticket(
                self.user, session, "protected", "replacement-run", "m_synthetic", "synthetic-fingerprint",
            )
            for operation in (
                require_profile_access(db, session, "protected"),
                playback.validate_playback_ticket(ticket, "m_synthetic", db),
                playback.authorized_run(db, browser_request(session), "replacement-run"),
                playback.update_playback_progress(
                    "replacement-run", playback.PlaybackProgressRequest(timestamp=20, duration_watched=1, sequence_number=1),
                    browser_request(session), db, self.user,
                ),
            ):
                with self.assertRaises(HTTPException) as denied:
                    await operation
                self.assertEqual(denied.exception.status_code, 403)
                self.assertEqual(denied.exception.detail["code"], "profile_pin_required")
            self.assertEqual((await db.get(PlaybackRun, "replacement-run")).sequence_number, 1)
            self.assertEqual((await db.exec(select(PlaybackSession))).all(), [])

    async def recreate_during_unlock(self, new_pin, *, pause_at_writer=False):
        verified = asyncio.Event()
        continue_unlock = asyncio.Event()
        clear_failure = self.application.clear_rate_limit

        async def scheduled_clear(*args, **kwargs):
            verified.set()
            await continue_unlock.wait()
            return await clear_failure(*args, **kwargs)

        class ScheduledSession(AsyncSession):
            async def execute(session, statement, *args, **kwargs):
                if (asyncio.current_task().get_name() == "delayed-profile-unlock"
                        and getattr(statement, "is_update", False)
                        and statement.table.name == AuthSession.__tablename__):
                    verified.set()
                    await continue_unlock.wait()
                return await super().execute(statement, *args, **kwargs)

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
            old_profile = await db.get(Profile, "protected")
            old_hash = old_profile.pin_hash
            old_version = old_profile.pin_version
        patch_target = "AsyncSession" if pause_at_writer else "clear_rate_limit"
        patched_value = ScheduledSession if pause_at_writer else scheduled_clear
        with patch.object(self.application, patch_target, patched_value):
            unlock = asyncio.create_task(self.application.unlock_profile(
                "protected", self.application.ProfileUnlockRequest(pin="1111"),
                browser_request(session), self.user, session,
            ), name="delayed-profile-unlock")
            try:
                await asyncio.wait_for(verified.wait(), timeout=10)
                await self.application.delete_profile("protected", self.user)
                await self.application.save_profile(self.save_request(pin_enabled=new_pin is not None, pin=new_pin), self.user)
                replacement = await self.saved_profile()
                self.assertEqual(replacement.pin_version, old_version if new_pin is not None else 0)
                self.assertNotEqual(replacement.pin_hash, old_hash)
                if new_pin is not None:
                    self.assertTrue(verify_profile_pin(new_pin, replacement.pin_hash))
                continue_unlock.set()
                with self.assertRaises(HTTPException) as denied:
                    await asyncio.wait_for(unlock, timeout=10)
                self.assertEqual(denied.exception.status_code, 403)
                self.assertEqual(denied.exception.detail["code"], "profile_pin_required")
            finally:
                continue_unlock.set()
                if not unlock.done():
                    unlock.cancel()
                await asyncio.gather(unlock, return_exceptions=True)
        if new_pin is None:
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                current_session = await db.get(AuthSession, self.session.id)
                # Unprotected profiles remain available under the existing owner
                # policy. Re-enabling protection must still require a new PIN.
                self.assertFalse((await require_profile_access(db, current_session, "protected")).pin_enabled)
            await self.application.save_profile(self.save_request(pin_enabled=True, pin="2222"), self.user)
        await self.assert_replacement_protection_denies_stale_session()

    async def test_old_verified_unlock_cannot_authorize_deleted_recreated_profile_new_pin(self):
        await self.recreate_during_unlock("2222")

    async def test_old_verified_unlock_cannot_authorize_same_pin_recreation_with_fresh_salt(self):
        await self.recreate_during_unlock("1111")
        # A fresh verification of the recreated profile is permitted.
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
        result = await self.application.unlock_profile(
            "protected", self.application.ProfileUnlockRequest(pin="1111"), browser_request(session), self.user, session,
        )
        self.assertTrue(result["verified"])
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await playback.authorized_run(db, browser_request(await db.get(AuthSession, session.id)), "replacement-run")

    async def test_delete_recreate_between_prepared_grant_and_sql_write_is_rejected(self):
        await self.recreate_during_unlock("2222", pause_at_writer=True)

    async def test_protected_unlock_cannot_regrant_unprotected_recreation_then_new_protection(self):
        await self.recreate_during_unlock(None)

    async def test_delete_writer_serializes_late_grant_then_recreation_remains_locked(self):
        delete_holds_writer = asyncio.Event()
        grant_attempted = asyncio.Event()
        continue_delete = asyncio.Event()

        class ScheduledSession(AsyncSession):
            async def execute(session, statement, *args, **kwargs):
                task_name = asyncio.current_task().get_name()
                is_grant_update = getattr(statement, "is_update", False) and statement.table.name == AuthSession.__tablename__
                if task_name == "late-profile-unlock" and is_grant_update:
                    grant_attempted.set()
                result = await super().execute(statement, *args, **kwargs)
                if task_name == "profile-delete" and is_grant_update:
                    delete_holds_writer.set()
                    await continue_delete.wait()
                return result

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
        with patch.object(self.application, "AsyncSession", ScheduledSession):
            deletion = asyncio.create_task(self.application.delete_profile("protected", self.user), name="profile-delete")
            unlock = None
            try:
                await asyncio.wait_for(delete_holds_writer.wait(), timeout=10)
                unlock = asyncio.create_task(self.application.unlock_profile(
                    "protected", self.application.ProfileUnlockRequest(pin="1111"), browser_request(session), self.user, session,
                ), name="late-profile-unlock")
                await asyncio.wait_for(grant_attempted.wait(), timeout=10)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(unlock), timeout=0.1)
                continue_delete.set()
                self.assertEqual((await asyncio.wait_for(deletion, timeout=10))["status"], "deleted")
                with self.assertRaises(HTTPException) as denied:
                    await asyncio.wait_for(unlock, timeout=10)
                self.assertEqual(denied.exception.status_code, 403)
            finally:
                continue_delete.set()
                for task in (deletion, unlock):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (deletion, unlock) if task is not None), return_exceptions=True)
        await self.application.save_profile(self.save_request(pin_enabled=True, pin="2222"), self.user)
        await self.assert_replacement_protection_denies_stale_session()

    async def test_completed_grant_is_cleared_before_recreation(self):
        await self.unlocked_ticket("1111")
        await self.application.delete_profile("protected", self.user)
        await self.application.save_profile(self.save_request(pin_enabled=True, pin="1111"), self.user)
        await self.assert_replacement_protection_denies_stale_session()

    async def test_select_route_stale_dependency_cannot_regrant_recreated_protection(self):
        await self.unlocked_ticket("1111")
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            stale_session = await db.get(AuthSession, self.session.id)
        self.assertEqual(stale_session.selected_profile_pin_version, 1)
        await self.application.delete_profile("protected", self.user)
        await self.application.save_profile(self.save_request(pin_enabled=True, pin="2222"), self.user)
        with self.assertRaises(HTTPException) as denied:
            await self.application.select_profile("protected", stale_session, self.user)
        self.assertEqual(denied.exception.status_code, 403)
        await self.assert_replacement_protection_denies_stale_session()

    async def test_unprotected_snapshot_cannot_grant_recreated_protected_profile(self):
        await self.application.save_profile(self.save_request(pin_enabled=False), self.user)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            session = await db.get(AuthSession, self.session.id)
        verified, continue_unlock = asyncio.Event(), asyncio.Event()
        clear_failure = self.application.clear_rate_limit

        async def scheduled_clear(*args, **kwargs):
            verified.set()
            await continue_unlock.wait()
            return await clear_failure(*args, **kwargs)

        with patch.object(self.application, "clear_rate_limit", scheduled_clear):
            unlock = asyncio.create_task(self.application.unlock_profile(
                "protected", self.application.ProfileUnlockRequest(pin=""), browser_request(session), self.user, session,
            ))
            try:
                await asyncio.wait_for(verified.wait(), timeout=10)
                await self.application.delete_profile("protected", self.user)
                await self.application.save_profile(self.save_request(pin_enabled=True, pin="2222"), self.user)
                continue_unlock.set()
                with self.assertRaises(HTTPException) as denied:
                    await asyncio.wait_for(unlock, timeout=10)
                self.assertEqual(denied.exception.status_code, 403)
            finally:
                continue_unlock.set()
                if not unlock.done():
                    unlock.cancel()
                await asyncio.gather(unlock, return_exceptions=True)
        await self.assert_replacement_protection_denies_stale_session()


if __name__ == "__main__":
    unittest.main()
