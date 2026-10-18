"""Auth/playback regressions; run only in a disposable installation."""
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bcrypt
from fastapi import HTTPException, Response
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.requests import Request

from models import AuthChallenge, AuthSession, Movie, PlaybackRun, Profile, User
from routes import auth, playback
from services.logger import _SafeDiagnosticFilter, _SafeDiagnosticFormatter
from services.request_security import client_ip, request_is_secure, same_origin_request
from services.media_source import ResolvedMediaSource, resolve_media_source
from services.playback_source import PlaybackSourceStat
from lifecycle.media_work import media_ownership, media_lock
from lifecycle.coordination import get_runtime_coordinator
from lifecycle.media_publication import MediaMutation
from config import settings


def request(session: AuthSession | None = None, **headers: str) -> Request:
    value = Request({"type": "http", "method": "POST", "path": "/api/setup/tmdb/validate", "headers": [(key.encode(), text.encode()) for key, text in headers.items()], "client": ("127.0.0.1", 5000), "scheme": "http", "server": ("localhost", 8000), "query_string": b""})
    if session is not None:
        value.state.auth_session = session
    return value


class AuthenticationRemediationRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Each unittest case owns a fresh loop and a disposable installation.
        self.lock_patch = patch("lifecycle.media_work._locks", {})
        self.lock_patch.start()
        self.directory = tempfile.TemporaryDirectory(prefix="streamhome-auth-remediation-")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.directory.name) / 'auth.db'}", connect_args={"timeout": 5})

        @event.listens_for(self.engine.sync_engine, "connect")
        def pragmas(connection, _record):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")

        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.user = User(id=81, email="isolated@example.test", password_hash=bcrypt.hashpw(b"correct-password", bcrypt.gensalt(rounds=4)).decode(), two_factor_enabled=True, totp_secret="JBSWY3DPEHPK3PXP")
        self.session = AuthSession(id="session-a", user_id=81, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600, reauthenticated_at=time.time(), selected_profile_id="protected", selected_profile_pin_version=1)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(self.user)
            db.add(self.session)
            db.add(Profile(id="protected", name="Protected", pin_enabled=True, pin_hash="unused", pin_version=1))
            db.add(AuthSession(id="session-b", user_id=81, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600))
            db.add(Movie(id="m_test", title="Test", description="", thumbnail_url="", banner_url="", video_url="/media/test.mp4", duration="2m", release_year=2026, source_fingerprint="fingerprint", probed_duration=120))
            db.add(PlaybackRun(id="run-test", auth_session_id="session-a", profile_id="protected", movie_id="m_test", source_fingerprint="fingerprint", lifecycle_state="active", sequence_number=1))
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.lock_patch.stop()
        self.directory.cleanup()

    async def test_parallel_failures_increment_database_counter(self) -> None:
        ready = asyncio.Event()
        loaded = 0

        async def failure():
            nonlocal loaded
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                user = await db.get(User, 81)
                loaded += 1
                if loaded == 6:
                    ready.set()
                await ready.wait()
                await auth.register_failure(user, db, request(), "login_failure")

        await asyncio.gather(*(failure() for _ in range(6)))
        async with AsyncSession(self.engine) as db:
            user = await db.get(User, 81)
            self.assertEqual(user.failed_login_attempts, 6)
            self.assertGreater(user.lockout_until, time.time())

    async def test_parallel_factor_failures_increment_challenge_counter(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            token = await auth.create_challenge(await db.get(User, 81), "login", db)

        async def attempt():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                with self.assertRaises(HTTPException):
                    await auth.verify(auth.VerifyRequest(challenge_token=token, code="bad-code"), request(), None, db)

        await asyncio.gather(*(attempt() for _ in range(5)))
        async with AsyncSession(self.engine) as db:
            challenge = (await db.exec(select(AuthChallenge))).first()
            self.assertEqual(challenge.attempts, 5)
            self.assertEqual((await db.get(User, 81)).failed_login_attempts, 5)

    async def test_reauthentication_challenge_rejects_another_session_before_consuming(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            user = await db.get(User, 81)
            token = await auth.create_challenge(user, "reauthentication", db, auth_session_id="session-a")
            other = await db.get(AuthSession, "session-b")
            cookie = auth.encode_session_token(user, other)
            with patch("routes.auth.pyotp.TOTP.verify", return_value=True) as factor:
                with self.assertRaises(HTTPException) as caught:
                    await auth.verify(auth.VerifyRequest(challenge_token=token, code="123456"), request(cookie=f"streamhome_session={cookie}"), None, db)
            self.assertEqual(caught.exception.status_code, 403)
            factor.assert_not_called()
            challenge = (await db.exec(select(AuthChallenge))).first()
            self.assertIsNone(challenge.used_at)
            self.assertIsNone(other.reauthenticated_at)

    async def test_reauthentication_challenge_accepts_only_bound_session_once(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            user = await db.get(User, 81)
            token = await auth.create_challenge(user, "reauthentication", db, auth_session_id=self.session.id)
            cookie = auth.encode_session_token(user, self.session)
            browser = request(cookie=f"streamhome_session={cookie}")
            with patch("routes.auth.pyotp.TOTP.verify", return_value=True):
                result = await auth.verify(auth.VerifyRequest(challenge_token=token, code="123456"), browser, None, db)
                self.assertTrue(result["reauthenticated"])
                with self.assertRaises(HTTPException) as caught:
                    await auth.verify(auth.VerifyRequest(challenge_token=token, code="123456"), browser, None, db)
            self.assertEqual(caught.exception.status_code, 401)

    async def test_locked_account_does_not_hash_sensitive_passwords_or_totp(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            user = await db.get(User, 81)
            user.lockout_until = time.time() + 300
            await db.commit()
            with patch("routes.auth.bcrypt.checkpw") as bcrypt_check, patch("routes.auth.pyotp.TOTP.verify") as totp_check:
                for operation in (
                    auth.update_email(auth.UpdateEmailRequest(email="next@example.test", current_password="wrong"), Response(), request(), user, self.session, db),
                    auth.update_password(auth.UpdatePasswordRequest(current_password="wrong", new_password="new-password"), request(), user, self.session, db),
                    auth.disable_totp(auth.TOTPDisableRequest(code="123456"), request(), user, self.session, db),
                ):
                    with self.assertRaises(HTTPException) as caught:
                        await operation
                    self.assertEqual(caught.exception.status_code, 429)
                bcrypt_check.assert_not_called()
                totp_check.assert_not_called()

    async def test_oversized_utf8_password_is_failed_authentication(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            with patch("routes.auth.bcrypt.checkpw") as bcrypt_check:
                with self.assertRaises(HTTPException) as caught:
                    await auth.login(auth.LoginRequest(email=self.user.email, password="é" * 37), request(), db)
            self.assertEqual(caught.exception.status_code, 401)
            self.assertEqual((await db.get(User, 81)).failed_login_attempts, 1)
            bcrypt_check.assert_not_called()

    async def test_disable_totp_failures_are_bounded(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            user = await db.get(User, 81)
            with patch("routes.auth.pyotp.TOTP.verify", return_value=False) as verification:
                for _ in range(5):
                    with self.assertRaises(HTTPException):
                        await auth.disable_totp(auth.TOTPDisableRequest(code="bad-code"), request(), user, self.session, db)
                with self.assertRaises(HTTPException) as caught:
                    await auth.disable_totp(auth.TOTPDisableRequest(code="bad-code"), request(), user, self.session, db)
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(verification.call_count, 5)
            self.assertTrue(user.two_factor_enabled)

    async def test_pin_version_change_invalidates_runs_and_existing_tickets(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            ticket, _ = playback.issue_playback_ticket(self.user, self.session, "protected", "run-test", "m_test", "fingerprint")
            profile = await db.get(Profile, "protected")
            profile.pin_version = 2
            await db.commit()
            for operation in (playback.authorized_run(db, request(self.session), "run-test"), playback.validate_playback_ticket(ticket, "m_test", db)):
                with self.assertRaises(HTTPException) as caught:
                    await operation
                self.assertEqual(caught.exception.status_code, 403)
                self.assertEqual(caught.exception.detail["code"], "profile_pin_required")

    async def test_run_authorization_refreshes_grant_changed_after_request_authentication(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as viewer:
            stale_session = await viewer.get(AuthSession, self.session.id)
            browser = request(stale_session)
            async with AsyncSession(self.engine, expire_on_commit=False) as writer:
                current = await writer.get(AuthSession, self.session.id)
                current.selected_profile_id = None
                current.selected_profile_pin_version = None
                await writer.commit()
            with self.assertRaises(HTTPException) as caught:
                await playback.authorized_run(viewer, browser, "run-test")
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(caught.exception.detail["code"], "profile_pin_required")

    async def test_close_is_idempotent_and_keeps_session_authorization(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            progress = playback.PlaybackCloseRequest(sequence_number=1, timestamp=20, duration_watched=1, event="exit")
            with patch("routes.playback.record_playback_progress", new=AsyncMock(return_value="view")) as record:
                first = await playback.close_playback_run("run-test", progress, request(self.session), db, self.user)
                second = await playback.close_playback_run("run-test", progress, request(self.session), db, self.user)
            self.assertEqual(first["status"], "abandoned")
            self.assertEqual(second["status"], "abandoned")
            self.assertEqual(first["nextSequenceNumber"], second["nextSequenceNumber"])
            self.assertEqual(record.await_count, 1)
            other = await db.get(AuthSession, "session-b")
            with self.assertRaises(HTTPException) as caught:
                await playback.close_playback_run("run-test", progress, request(other), db, self.user)
            self.assertEqual(caught.exception.status_code, 403)

    async def test_local_response_keeps_authorized_inode_across_publication(self) -> None:
        previous_media_dir = settings.MEDIA_DIR
        settings.MEDIA_DIR = self.directory.name
        try:
            video = Path(self.directory.name) / "Movies" / "Test" / "test.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"old-authorized-video")
            catalog_path = "/media/Movies/Test/test.mp4"
            source = await resolve_media_source(catalog_path, check_cloud=False)
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie = await db.get(Movie, "m_test")
                movie.video_url = catalog_path
                movie.source_fingerprint = source.fingerprint
                run = await db.get(PlaybackRun, "run-test")
                run.source_fingerprint = source.fingerprint
                await db.commit()
                ticket, _ = playback.issue_playback_ticket(self.user, self.session, "protected", "run-test", "m_test", source.fingerprint)
                browser = request(self.session)
                browser.scope["method"] = "GET"
                browser.scope["asgi"] = {"spec_version": "2.4"}
                response = await playback.playback_source_bridge("m_test", browser, ticket, "main", db)
                self.assertFalse(media_lock("m_test").locked())
                async with media_ownership("m_test"):
                    replacement = video.with_suffix(".replacement")
                    replacement.write_bytes(b"new-unrelated-video")
                    replacement.replace(video)
                    movie.source_fingerprint = (await resolve_media_source(catalog_path, check_cloud=False)).fingerprint
                    await db.commit()
                messages = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message):
                    messages.append(message)

                await response(browser.scope, receive, send)
                self.assertEqual(b"".join(message.get("body", b"") for message in messages), b"old-authorized-video")
                with self.assertRaises(HTTPException) as caught:
                    await playback.playback_source_bridge("m_test", browser, ticket, "main", db)
                self.assertEqual(caught.exception.detail["code"], "PLAYBACK_SOURCE_CHANGED")
        finally:
            settings.MEDIA_DIR = previous_media_dir

    async def test_cloud_response_holds_ownership_and_cleans_up_on_disconnect(self) -> None:
        source = ResolvedMediaSource(catalog_path="/media/Movies/Test/test.mp4", relative_path="Movies/Test/test.mp4", local_path=Path(self.directory.name) / "missing.mp4", local_exists=False, cloud_exists=True, cloud_path="isolated:/test.mp4", cloud_identity="old-cloud-object", cloud_size=4)

        class RemoteReader:
            closed = False

            async def stat(self):
                return PlaybackSourceStat(4, "video/mp4", True)

            async def open_range(self, _start, _length):
                try:
                    yield b"old-"
                finally:
                    self.closed = True

        reader = RemoteReader()
        baseline_work = len(get_runtime_coordinator().active_work())
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            movie = await db.get(Movie, "m_test")
            movie.source_fingerprint = source.fingerprint
            run = await db.get(PlaybackRun, "run-test")
            run.source_fingerprint = source.fingerprint
            await db.commit()
            ticket, _ = playback.issue_playback_ticket(self.user, self.session, "protected", "run-test", "m_test", source.fingerprint)
            browser = request(self.session)
            browser.scope["method"] = "GET"
            browser.scope["asgi"] = {"spec_version": "2.4"}
            with patch("routes.playback.require_available_source", new=AsyncMock(return_value=source)), patch("routes.playback.source_reader", return_value=reader):
                response = await playback.playback_source_bridge("m_test", browser, ticket, "main", db)
            self.assertTrue(media_lock("m_test").locked())
            published = asyncio.Event()

            async def publisher():
                async with media_ownership("m_test"):
                    published.set()

            task = asyncio.create_task(publisher())
            await asyncio.sleep(0)
            self.assertFalse(published.is_set())

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def disconnect(message):
                if message["type"] == "http.response.body":
                    raise asyncio.CancelledError

            with self.assertRaises(asyncio.CancelledError):
                await response(browser.scope, receive, disconnect)
            await asyncio.wait_for(task, timeout=1)
            self.assertTrue(reader.closed)
            self.assertFalse(media_lock("m_test").locked())
            self.assertEqual(len(get_runtime_coordinator().active_work()), baseline_work)

    async def test_run_polling_waits_until_unaccepted_publication_is_compensated(self) -> None:
        previous_media_dir = settings.MEDIA_DIR
        settings.MEDIA_DIR = self.directory.name
        try:
            video = Path(self.directory.name) / "Movies" / "Test" / "test.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"original-for-run-polling")
            catalog_path = "/media/Movies/Test/test.mp4"
            source = await resolve_media_source(catalog_path, check_cloud=False)
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                movie = await db.get(Movie, "m_test")
                movie.video_url = catalog_path
                movie.source_fingerprint = source.fingerprint
                run = await db.get(PlaybackRun, "run-test")
                run.source_fingerprint = source.fingerprint
                await db.commit()
            async with AsyncSession(self.engine, expire_on_commit=False) as viewer:
                with patch("routes.playback.ensure_source_metadata", new=AsyncMock()) as repair, patch("routes.playback.synchronize_source_fingerprint", new=AsyncMock()), patch("routes.playback.build_run_response", new=AsyncMock(return_value={"observed": "accepted-source"})):
                    async with media_ownership("m_test"):
                        async with MediaMutation("m_test") as mutation:
                            mutation.capture_local(str(video))
                            video.write_bytes(b"unaccepted-candidate")
                            poll = asyncio.create_task(playback.get_playback_run("run-test", request(self.session), False, viewer, self.user))
                            await asyncio.sleep(0.05)
                            self.assertFalse(poll.done())
                            repair.assert_not_awaited()
                    result = await asyncio.wait_for(poll, timeout=1)
                    self.assertEqual(result, {"observed": "accepted-source"})
                    # Legacy measurement refresh is allowed after compensation,
                    # but it must observe the restored original under ownership.
                    repair.assert_awaited_once()
                    observed_source = repair.await_args.args[2]
                    self.assertEqual(observed_source.fingerprint, source.fingerprint)
                    self.assertEqual(observed_source.local_path.read_bytes(), b"original-for-run-polling")
            self.assertEqual(video.read_bytes(), b"original-for-run-polling")
            async with AsyncSession(self.engine) as db:
                self.assertEqual((await db.get(Movie, "m_test")).source_fingerprint, source.fingerprint)
        finally:
            settings.MEDIA_DIR = previous_media_dir


class DiagnosticAndProxyRegression(unittest.TestCase):
    def test_application_log_redacts_ffmpeg_urls_headers_and_tracebacks(self) -> None:
        record = logging.LogRecord("streamhome", logging.ERROR, __file__, 1, "FFmpeg failed for %s\nAuthorization: Bearer private-value", ("https://source.example/private-video?signature=signed-secret",), None)
        _SafeDiagnosticFilter().filter(record)
        rendered = _SafeDiagnosticFormatter("%(message)s").format(record)
        for forbidden in ("source.example", "private-video", "signed-secret", "private-value"):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("FFmpeg failed", rendered)
        try:
            raise ValueError("https://source.example/secret?token=secret")
        except ValueError:
            import sys
            record.exc_info = sys.exc_info()
        self.assertNotIn("source.example", _SafeDiagnosticFormatter().format(record))

    def test_trusted_chain_keeps_https_and_lan_setup_origin(self) -> None:
        previous = (settings.PUBLIC_URL, settings.SETUP_COMPLETE, settings.ALLOWED_ORIGINS)
        try:
            settings.PUBLIC_URL = "http://localhost:3000"
            settings.SETUP_COMPLETE = False
            settings.ALLOWED_ORIGINS = []
            browser = request(origin="https://192.168.1.25:3000", cookie="streamhome_setup=isolated", **{"x-forwarded-for": "198.51.100.20, 127.0.0.1", "x-forwarded-proto": "https, http", "x-forwarded-host": "192.168.1.25:3000"})
            self.assertEqual(client_ip(browser), "198.51.100.20")
            self.assertTrue(request_is_secure(browser))
            self.assertTrue(same_origin_request(browser))
        finally:
            settings.PUBLIC_URL, settings.SETUP_COMPLETE, settings.ALLOWED_ORIGINS = previous

    def test_forged_prefix_is_not_the_client_identity_or_secure_scheme(self) -> None:
        browser = request(**{"x-forwarded-for": "203.0.113.50, 198.51.100.20", "x-forwarded-proto": "https, http"})
        self.assertEqual(client_ip(browser), "198.51.100.20")
        self.assertFalse(request_is_secure(browser))


if __name__ == "__main__":
    unittest.main()
