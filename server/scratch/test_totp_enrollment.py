import time
import unittest

import pyotp
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.requests import Request

from models import AuthSession, TOTPEnrollment, User
from routes.auth import (
    TOTPVerifySetupRequest,
    setup_totp as begin_admin_totp,
    totp_enrollment_qr as admin_totp_qr,
    verify_totp_setup as verify_admin_totp,
)
from routes.setup import (
    TOTPBeginRequest,
    TOTPVerifyRequest,
    _setup_token,
    _setup_totp_enrollment,
    begin_totp as begin_setup_totp,
    setup_totp_qr,
    verify_totp as verify_setup_totp,
)
from services.secret_crypto import is_protected_secret


def request(path: str, *, setup_session: str | None = None) -> Request:
    headers = []
    if setup_session:
        cookie = f"streamhome_setup={_setup_token(setup_session)}"
        headers.append((b"cookie", cookie.encode("ascii")))
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("watch.example.test", 443),
        "path": path,
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.50", 45000),
    })


class TOTPEnrollmentRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.db = AsyncSession(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_setup_enrollment_is_server_owned_and_qr_is_not_cacheable(self):
        setup_session = "setup-session"
        setup_request = request("/api/setup/totp/begin", setup_session=setup_session)
        result = await begin_setup_totp(
            TOTPBeginRequest(email="Admin@Example.Test"),
            setup_request,
            self.db,
        )
        enrollment = await self.db.get(TOTPEnrollment, result["enrollmentId"])

        self.assertIsNotNone(enrollment)
        self.assertEqual(enrollment.email, "admin@example.test")
        self.assertTrue(is_protected_secret(enrollment.secret_encrypted))
        self.assertNotEqual(enrollment.secret_encrypted, result["manualKey"])
        self.assertNotIn("secret", result)
        self.assertTrue(result["qrImageUrl"].endswith(f"/{enrollment.id}/qr"))

        qr_response = await setup_totp_qr(enrollment.id, setup_request, self.db)
        self.assertEqual(qr_response.media_type, "image/svg+xml")
        self.assertIn(b"<svg", qr_response.body)
        self.assertEqual(qr_response.headers["cache-control"], "no-store, private, max-age=0")

        code = pyotp.TOTP(result["manualKey"]).now()
        verified = await verify_setup_totp(
            TOTPVerifyRequest(enrollment_id=enrollment.id, code=code),
            setup_request,
            self.db,
        )
        self.assertTrue(verified["valid"])
        await self.db.refresh(enrollment)
        self.assertIsNotNone(enrollment.verified_at)

        wrong_session = request("/api/setup/totp/verify", setup_session="different-session")
        with self.assertRaises(HTTPException) as context:
            await _setup_totp_enrollment(self.db, wrong_session, enrollment.id)
        self.assertEqual(context.exception.status_code, 404)

    async def test_expired_setup_enrollment_is_rejected_and_removed(self):
        setup_session = "setup-session"
        setup_request = request("/api/setup/totp/begin", setup_session=setup_session)
        result = await begin_setup_totp(
            TOTPBeginRequest(email="admin@example.test"),
            setup_request,
            self.db,
        )
        enrollment = await self.db.get(TOTPEnrollment, result["enrollmentId"])
        enrollment.expires_at = time.time() - 1
        self.db.add(enrollment)
        await self.db.commit()

        with self.assertRaises(HTTPException) as context:
            await _setup_totp_enrollment(self.db, setup_request, enrollment.id)
        self.assertEqual(context.exception.status_code, 410)
        self.assertIsNone(await self.db.get(TOTPEnrollment, enrollment.id))

    async def test_admin_secret_is_promoted_only_after_bound_verification(self):
        user = User(email="admin@example.test", password_hash="hash")
        self.db.add(user)
        await self.db.flush()
        auth_session = AuthSession(
            id="auth-session",
            user_id=user.id,
            created_at=time.time(),
            last_seen_at=time.time(),
            expires_at=time.time() + 3600,
            reauthenticated_at=time.time(),
        )
        self.db.add(auth_session)
        await self.db.commit()

        result = await begin_admin_totp(user, auth_session, self.db)
        enrollment = await self.db.get(TOTPEnrollment, result["enrollmentId"])
        self.assertIsNone(user.totp_secret)
        self.assertFalse(user.two_factor_enabled)
        self.assertTrue(is_protected_secret(enrollment.secret_encrypted))

        qr_response = await admin_totp_qr(enrollment.id, user, auth_session, self.db)
        self.assertIn(b"<svg", qr_response.body)
        self.assertEqual(qr_response.headers["pragma"], "no-cache")

        code = pyotp.TOTP(result["manualKey"]).now()
        verified = await verify_admin_totp(
            TOTPVerifySetupRequest(enrollment_id=enrollment.id, code=code),
            request("/api/auth/2fa/verify-setup"),
            user,
            auth_session,
            self.db,
        )
        self.assertEqual(verified["message"], "TOTP successfully enabled.")
        self.assertEqual(len(verified["recoveryCodes"]), 10)
        self.assertTrue(user.two_factor_enabled)
        self.assertEqual(user.totp_secret, enrollment.secret_encrypted)
        self.assertIsNone(await self.db.get(TOTPEnrollment, enrollment.id))


if __name__ == "__main__":
    unittest.main()
