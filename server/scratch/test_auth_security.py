"""End-to-end regression checks for session-backed local authentication."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bcrypt
import jwt
import pyotp
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import engine, init_db
from models import AuthChallenge, AuthSession, RecoveryCode, SecurityEvent, User
from routes.auth import AUTH_COOKIE, get_current_user, health_router, router

EMAIL = "streamhome_auth_regression@example.test"
PASSWORD = "StreamHome-Regression-Password"
SECRET = "JBSWY3DPEHPK3PXP"
UPDATED_EMAIL = "streamhome_auth_regression_updated@example.test"
UPDATED_PASSWORD = "StreamHome-Regression-Password-Updated"


async def seed_user() -> int:
    await init_db()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        existing = (await db.execute(select(User).where(User.email == EMAIL))).scalars().first()
        if existing:
            await cleanup_user(existing.id, db)
        updated = (await db.execute(select(User).where(User.email == UPDATED_EMAIL))).scalars().first()
        if updated:
            await cleanup_user(updated.id, db)
        user = User(email=EMAIL, password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode(), totp_secret=SECRET, two_factor_enabled=True)
        db.add(user)
        await db.commit()
        return int(user.id)


async def reset_lockout() -> None:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        user = (await db.execute(select(User).where(User.email == EMAIL))).scalars().first()
        user.failed_login_attempts = 0
        user.lockout_until = None
        db.add(user)
        await db.commit()


async def cleanup_user(user_id: int | None = None, db: AsyncSession | None = None) -> None:
    owns_session = db is None
    session = db or AsyncSession(engine, expire_on_commit=False)
    try:
        if user_id is None:
            user = (await session.execute(select(User).where(User.email == EMAIL))).scalars().first()
            if not user:
                return
            user_id = user.id
        await session.execute(delete(SecurityEvent).where(SecurityEvent.user_id == user_id))
        await session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
        await session.execute(delete(AuthChallenge).where(AuthChallenge.user_id == user_id))
        await session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
    finally:
        if owns_session:
            await session.close()


app = FastAPI()
app.include_router(router)
app.include_router(health_router)


@app.get("/protected")
async def protected(user: User = Depends(get_current_user)):
    return {"email": user.email}


def run() -> None:
    user_id = asyncio.run(seed_user())
    client = TestClient(app)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0", "X-Forwarded-For": "10.0.0.25"}
    try:
        health = client.get("/api/health")
        assert health.status_code == 200 and health.json()["status"] == "ready"

        for attempt in range(LOCKOUT_TEST_ATTEMPTS := 5):
            response = client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong"}, headers=headers)
            assert response.status_code == (429 if attempt == LOCKOUT_TEST_ATTEMPTS - 1 else 401)
        assert int(response.headers["Retry-After"]) > 0
        asyncio.run(reset_lockout())

        password_step = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, headers=headers)
        assert password_step.status_code == 200
        challenge = password_step.json()["challengeToken"]
        assert challenge and "accessToken" not in password_step.json()

        factor_step = client.post("/api/auth/verify", json={"challenge_token": challenge, "method": "totp", "code": pyotp.TOTP(SECRET).now()}, headers=headers)
        assert factor_step.status_code == 200
        token = client.cookies.get(AUTH_COOKIE)
        assert token
        bearer = {**headers, "Authorization": f"Bearer {token}"}
        assert client.get("/protected", headers=bearer).status_code == 200
        query_only_client = TestClient(app)
        assert query_only_client.get(f"/protected?token={token}", headers=headers).status_code == 401
        assert client.get("/protected", headers=headers).status_code == 200
        assert client.post("/api/auth/verify", json={"challenge_token": challenge, "method": "totp", "code": pyotp.TOTP(SECRET).now()}, headers=headers).status_code == 401

        legacy = jwt.encode({"sub": EMAIL, "exp": time.time() + 60}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
        assert client.get("/protected", headers={"Authorization": f"Bearer {legacy}"}).status_code == 401

        reauth = client.post("/api/auth/reauthenticate", json={"password": PASSWORD}, headers=bearer)
        assert reauth.status_code == 200 and reauth.json()["requires2fa"]
        verified = client.post("/api/auth/verify", json={"challenge_token": reauth.json()["challengeToken"], "method": "totp", "code": pyotp.TOTP(SECRET).now()}, headers=bearer)
        assert verified.status_code == 200 and verified.json()["reauthenticated"]

        codes_response = client.post("/api/auth/recovery-codes/regenerate", headers=bearer)
        codes = codes_response.json()["recoveryCodes"]
        assert len(codes) == 10
        recovery_login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, headers=headers).json()
        recovery_verify = client.post("/api/auth/verify", json={"challenge_token": recovery_login["challengeToken"], "method": "recovery", "code": codes[0]}, headers=headers)
        assert recovery_verify.status_code == 200
        reused = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, headers=headers).json()
        assert client.post("/api/auth/verify", json={"challenge_token": reused["challengeToken"], "method": "recovery", "code": codes[0]}, headers=headers).status_code == 401

        sessions = client.get("/api/auth/sessions", headers=bearer)
        assert sessions.status_code == 200 and len(sessions.json()) == 2
        summary = client.get("/api/auth/security/summary", headers=bearer)
        assert summary.status_code == 200 and summary.json()["recoveryCodesRemaining"] == 9
        events = client.get("/api/auth/security/events", headers=bearer)
        serialized = events.text.lower()
        assert events.status_code == 200 and PASSWORD.lower() not in serialized and challenge.lower() not in serialized

        previous_days = settings.SESSION_LIFETIME_DAYS
        previous_minutes = settings.JWT_EXPIRATION_MINUTES
        previous_save = settings.save_to_json
        settings.save_to_json = lambda: None
        try:
            policy = client.put("/api/auth/security/session-policy", json={"session_lifetime_days": 30}, headers=bearer)
            assert policy.status_code == 200 and policy.json()["sessionLifetimeDays"] == 30
            assert policy.json()["existingSessionsChanged"] is False
        finally:
            settings.SESSION_LIFETIME_DAYS = previous_days
            settings.JWT_EXPIRATION_MINUTES = previous_minutes
            settings.save_to_json = previous_save

        email_change = client.put("/api/auth/security/email", json={"email": UPDATED_EMAIL, "current_password": PASSWORD}, headers=bearer)
        assert email_change.status_code == 200 and email_change.json()["email"] == UPDATED_EMAIL
        replacement_token = client.cookies.get(AUTH_COOKIE)
        assert replacement_token
        replacement_bearer = {**headers, "Authorization": f"Bearer {replacement_token}"}
        assert client.get("/protected", headers=bearer).status_code == 401
        assert client.get("/protected", headers=replacement_bearer).json()["email"] == UPDATED_EMAIL

        password_change = client.put("/api/auth/security/password", json={"current_password": PASSWORD, "new_password": UPDATED_PASSWORD}, headers=replacement_bearer)
        assert password_change.status_code == 200
        assert client.post("/api/auth/login", json={"email": UPDATED_EMAIL, "password": PASSWORD}, headers=headers).status_code == 401
        assert client.post("/api/auth/login", json={"email": UPDATED_EMAIL, "password": UPDATED_PASSWORD}, headers=headers).status_code == 200
        print("Authentication security regression checks passed.")
    finally:
        asyncio.run(cleanup_user(user_id))


if __name__ == "__main__":
    run()
