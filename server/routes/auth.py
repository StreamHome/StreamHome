import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime
from typing import Optional

import bcrypt
import jwt
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import delete, func, text, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import AuthChallenge, AuthSession, IntegrationCredential, RecoveryCode, SecurityEvent, User
from services.logger import logger
from services.request_security import client_ip, request_is_secure
from services.secret_crypto import is_protected_secret, protect_secret, reveal_secret
from services.rate_limit import clear as clear_rate_limit
from services.rate_limit import enforce as enforce_rate_limit
from services.rate_limit import fail as fail_rate_limit
from services.integration_auth import integration_token_hash

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
health_router = APIRouter(tags=["System"])
security = HTTPBearer(auto_error=False)

LOCKOUT_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
SESSION_ACTIVITY_WRITE_INTERVAL = 5 * 60
AUDIT_RETENTION_SECONDS = 180 * 24 * 60 * 60
RECOVERY_CODE_COUNT = 10
AUTH_COOKIE = "streamhome_session"


class LoginRequest(BaseModel):
    email: str
    password: str


class VerifyRequest(BaseModel):
    challenge_token: str
    method: str = "totp"
    code: str


class ReauthenticateRequest(BaseModel):
    password: str


class TOTPVerifySetupRequest(BaseModel):
    code: str


class TOTPDisableRequest(BaseModel):
    code: str


class UpdateEmailRequest(BaseModel):
    email: str
    current_password: str


class UpdatePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class SessionPolicyRequest(BaseModel):
    session_lifetime_days: int


class IntegrationCredentialRequest(BaseModel):
    name: str = "MediaSender"


def now() -> float:
    return time.time()


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def recovery_hash(value: str) -> str:
    normalized = value.replace("-", "").replace(" ", "").upper()
    return hmac.new(settings.JWT_SECRET.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) > 254 or normalized.count("@") != 1 or any(character.isspace() for character in normalized):
        raise HTTPException(status_code=422, detail={"code": "invalid_email", "message": "Enter a valid email address."})
    local, domain = normalized.split("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        raise HTTPException(status_code=422, detail={"code": "invalid_email", "message": "Enter a valid email address."})
    return normalized


def encode_session_token(user: User, session: AuthSession) -> str:
    payload = {
        "sub": user.email,
        "jti": session.id,
        "iat": datetime.utcnow(),
        "exp": datetime.utcfromtimestamp(session.expires_at),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


async def revoke_other_user_sessions(user: User, current: AuthSession, db: AsyncSession) -> int:
    result = await db.execute(select(AuthSession).where(
        AuthSession.user_id == user.id,
        AuthSession.id != current.id,
        AuthSession.revoked_at == None,
    ))
    revoked = 0
    timestamp = now()
    for item in result.scalars().all():
        item.revoked_at = timestamp
        db.add(item)
        revoked += 1
    return revoked


def device_label(request: Request) -> str:
    ua = request.headers.get("user-agent", "").lower()
    if not ua:
        return "Unknown device"
    browser = "Firefox" if "firefox" in ua else "Edge" if "edg/" in ua else "Chrome" if "chrome" in ua else "Safari" if "safari" in ua else "Browser"
    os_name = "Windows" if "windows" in ua else "iPhone" if "iphone" in ua else "iPad" if "ipad" in ua else "Android" if "android" in ua else "macOS" if "mac os" in ua else "Linux" if "linux" in ua else "Unknown OS"
    return f"{browser} on {os_name}"[:120]


async def add_event(
    db: AsyncSession,
    request: Request,
    event_type: str,
    outcome: str,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    timestamp = now()
    await db.execute(delete(SecurityEvent).where(SecurityEvent.created_at < timestamp - AUDIT_RETENTION_SECONDS))
    db.add(SecurityEvent(
        id=str(uuid.uuid4()),
        user_id=user_id,
        event_type=event_type,
        outcome=outcome,
        created_at=timestamp,
        ip_address=client_ip(request),
        device_label=device_label(request),
        session_id=session_id,
        details=json.dumps(details, separators=(",", ":")) if details else None,
    ))


def lockout_exception(user: User) -> HTTPException:
    remaining = max(1, int((user.lockout_until or now()) - now()))
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"code": "account_locked", "message": "Too many failed attempts. Try again when the lock expires.", "retryAfterSeconds": remaining},
        headers={"Retry-After": str(remaining)},
    )


def check_lockout(user: User) -> None:
    if user.lockout_until and now() < user.lockout_until:
        raise lockout_exception(user)


async def register_failure(user: User, db: AsyncSession, request: Request, event_type: str) -> None:
    user.failed_login_attempts += 1
    if user.failed_login_attempts >= LOCKOUT_ATTEMPTS:
        user.lockout_until = now() + LOCKOUT_SECONDS
        logger.warning(f"[Security] Account {user.email} locked for {LOCKOUT_SECONDS} seconds")
    db.add(user)
    await add_event(db, request, event_type, "failure", user.id, details={"locked": bool(user.lockout_until)})
    await db.commit()


async def create_challenge(user: User, purpose: str, db: AsyncSession) -> str:
    raw = secrets.token_urlsafe(32)
    timestamp = now()
    await db.execute(delete(AuthChallenge).where(AuthChallenge.expires_at < timestamp))
    db.add(AuthChallenge(
        id=str(uuid.uuid4()),
        token_hash=token_hash(raw),
        user_id=user.id,
        purpose=purpose,
        created_at=timestamp,
        expires_at=timestamp + settings.AUTH_CHALLENGE_MINUTES * 60,
    ))
    await db.commit()
    return raw


def set_session_cookie(response: Response, token: str, request: Request, max_age: int) -> None:
    response.set_cookie(
        AUTH_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=request_is_secure(request),
        samesite="strict",
        path="/",
    )


async def issue_session(user: User, db: AsyncSession, request: Request) -> JSONResponse:
    timestamp = now()
    session_id = str(uuid.uuid4())
    expires_at = timestamp + settings.JWT_EXPIRATION_MINUTES * 60
    session = AuthSession(
        id=session_id,
        user_id=user.id,
        created_at=timestamp,
        last_seen_at=timestamp,
        expires_at=expires_at,
        ip_address=client_ip(request),
        device_label=device_label(request),
    )
    previous = None
    if user.last_login_at:
        previous = {"at": user.last_login_at, "ipAddress": user.last_login_ip, "deviceLabel": user.last_login_device}
    user.last_login_at = timestamp
    user.last_login_ip = session.ip_address
    user.last_login_device = session.device_label
    user.failed_login_attempts = 0
    user.lockout_until = None
    db.add(user)
    db.add(session)
    await add_event(db, request, "login_success", "success", user.id, session_id)
    await db.commit()
    await clear_rate_limit(db, "login_ip", client_ip(request))
    access_token = encode_session_token(user, session)
    payload = {
        "email": user.email,
        "session": {"id": session_id, "expiresAt": expires_at},
        "previousLogin": previous,
    }
    response = JSONResponse(payload)
    set_session_cookie(response, access_token, request, max(1, int(expires_at - now())))
    return response


async def resolve_auth(request: Request, credentials: Optional[HTTPAuthorizationCredentials], db: AsyncSession) -> tuple[User, AuthSession]:
    token = credentials.credentials if credentials else request.cookies.get(AUTH_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        email = payload.get("sub")
        session_id = payload.get("jti")
        if not email or not session_id:
            raise ValueError("Legacy or incomplete token")
        user_result = await db.execute(select(User).where(User.email == email))
        user = user_result.scalars().first()
        session_result = await db.execute(select(AuthSession).where(AuthSession.id == session_id))
        auth_session = session_result.scalars().first()
        if not user or not auth_session or auth_session.user_id != user.id or auth_session.revoked_at or auth_session.expires_at <= now():
            raise ValueError("Inactive session")
        if now() - auth_session.last_seen_at >= SESSION_ACTIVITY_WRITE_INTERVAL:
            auth_session.last_seen_at = now()
            db.add(auth_session)
            await db.commit()
        request.state.auth_session = auth_session
        return user, auth_session
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_session),
) -> User:
    user, _ = await resolve_auth(request, credentials, db)
    return user


async def get_current_session(request: Request, user: User = Depends(get_current_user)) -> AuthSession:
    del user
    return request.state.auth_session


@router.get("/session")
async def browser_session(user: User = Depends(get_current_user), session: AuthSession = Depends(get_current_session)):
    return {"authenticated": True, "email": user.email, "expiresAt": session.expires_at}


async def require_recent_reauth(session: AuthSession = Depends(get_current_session)) -> AuthSession:
    if not session.reauthenticated_at or now() - session.reauthenticated_at > settings.REAUTHENTICATION_MINUTES * 60:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "reauthentication_required", "message": "Confirm your password and authenticator code to continue."})
    return session


@health_router.get("/api/health")
async def health(db: AsyncSession = Depends(get_session)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ready", "version": settings.APP_VERSION, "serverTime": now()}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "server_unavailable", "message": "The server database is not ready."})


@router.post("/login")
async def login(req: LoginRequest, request: Request, db: AsyncSession = Depends(get_session)):
    requested_email = req.email.strip().lower()
    request_ip = client_ip(request)
    await enforce_rate_limit(db, "login_ip", request_ip)
    result = await db.execute(select(User).where(func.lower(User.email) == requested_email))
    user = result.scalars().first()
    if not user:
        await add_event(db, request, "login_failure", "failure")
        await fail_rate_limit(db, "login_ip", request_ip, limit=30, window_seconds=LOCKOUT_SECONDS)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_credentials", "message": "Invalid email or password."})
    check_lockout(user)
    if not bcrypt.checkpw(req.password.encode("utf-8"), user.password_hash.encode("utf-8")):
        await register_failure(user, db, request, "login_failure")
        await fail_rate_limit(db, "login_ip", request_ip, limit=30, window_seconds=LOCKOUT_SECONDS)
        if user.lockout_until:
            raise lockout_exception(user)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_credentials", "message": "Invalid email or password."})
    if user.two_factor_enabled:
        challenge = await create_challenge(user, "login", db)
        return {"requires2fa": True, "challengeToken": challenge, "email": user.email, "expiresInSeconds": settings.AUTH_CHALLENGE_MINUTES * 60, "message": "Authenticator or recovery code required."}
    return await issue_session(user, db, request)


@router.post("/verify")
async def verify(req: VerifyRequest, request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security), db: AsyncSession = Depends(get_session)):
    challenge_result = await db.execute(select(AuthChallenge).where(AuthChallenge.token_hash == token_hash(req.challenge_token)))
    challenge = challenge_result.scalars().first()
    if not challenge or challenge.used_at or challenge.expires_at <= now() or challenge.attempts >= LOCKOUT_ATTEMPTS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "challenge_expired", "message": "This verification request expired. Sign in again."})
    user_result = await db.execute(select(User).where(User.id == challenge.user_id))
    user = user_result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "challenge_expired", "message": "This verification request expired. Sign in again."})
    check_lockout(user)
    method = req.method.lower()
    valid = False
    used_recovery: Optional[RecoveryCode] = None
    if method == "totp" and user.two_factor_enabled and user.totp_secret:
        valid = pyotp.TOTP(reveal_secret(user.totp_secret)).verify(req.code, valid_window=1)
        if valid and not is_protected_secret(user.totp_secret):
            user.totp_secret = protect_secret(user.totp_secret)
            db.add(user)
    elif method == "recovery" and user.two_factor_enabled:
        code_result = await db.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id, RecoveryCode.code_hash == recovery_hash(req.code), RecoveryCode.used_at == None))
        used_recovery = code_result.scalars().first()
        valid = used_recovery is not None
    if not valid:
        challenge.attempts += 1
        db.add(challenge)
        await register_failure(user, db, request, "factor_failure")
        if user.lockout_until:
            raise lockout_exception(user)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_factor", "message": "The verification code was not accepted."})

    consumed_at = now()
    challenge_claim = await db.execute(
        update(AuthChallenge)
        .where(AuthChallenge.id == challenge.id, AuthChallenge.used_at == None, AuthChallenge.attempts < LOCKOUT_ATTEMPTS)
        .values(used_at=consumed_at)
    )
    if challenge_claim.rowcount != 1:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "challenge_expired", "message": "This verification request expired. Sign in again."})
    if used_recovery:
        recovery_claim = await db.execute(
            update(RecoveryCode)
            .where(RecoveryCode.id == used_recovery.id, RecoveryCode.used_at == None)
            .values(used_at=consumed_at)
        )
        if recovery_claim.rowcount != 1:
            await db.rollback()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_factor", "message": "The verification code was not accepted."})
        await add_event(db, request, "recovery_code_used", "success", user.id)
    user.failed_login_attempts = 0
    user.lockout_until = None
    db.add(user)
    if challenge.purpose == "login":
        return await issue_session(user, db, request)
    if challenge.purpose == "reauthentication":
        _, auth_session = await resolve_auth(request, credentials, db)
        if auth_session.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Challenge does not belong to this session")
        auth_session.reauthenticated_at = now()
        db.add(auth_session)
        await add_event(db, request, "reauthentication", "success", user.id, auth_session.id)
        await db.commit()
        return {"reauthenticated": True, "validForSeconds": settings.REAUTHENTICATION_MINUTES * 60}
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported challenge purpose")


@router.post("/reauthenticate")
async def reauthenticate(req: ReauthenticateRequest, request: Request, user: User = Depends(get_current_user), session: AuthSession = Depends(get_current_session), db: AsyncSession = Depends(get_session)):
    check_lockout(user)
    if not bcrypt.checkpw(req.password.encode("utf-8"), user.password_hash.encode("utf-8")):
        await register_failure(user, db, request, "reauthentication_failure")
        if user.lockout_until:
            raise lockout_exception(user)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_credentials", "message": "The password was not accepted."})
    if user.two_factor_enabled:
        challenge = await create_challenge(user, "reauthentication", db)
        return {"requires2fa": True, "challengeToken": challenge, "email": user.email, "expiresInSeconds": settings.AUTH_CHALLENGE_MINUTES * 60}
    session.reauthenticated_at = now()
    db.add(session)
    await add_event(db, request, "reauthentication", "success", user.id, session.id)
    await db.commit()
    return {"reauthenticated": True, "validForSeconds": settings.REAUTHENTICATION_MINUTES * 60}


@router.get("/reauthenticate/status")
async def reauthentication_status(session: AuthSession = Depends(get_current_session)):
    remaining = max(0, int(settings.REAUTHENTICATION_MINUTES * 60 - (now() - (session.reauthenticated_at or 0))))
    return {"reauthenticated": remaining > 0, "remainingSeconds": remaining}


@router.post("/logout", status_code=204)
async def logout(response: Response, request: Request, user: User = Depends(get_current_user), session: AuthSession = Depends(get_current_session), db: AsyncSession = Depends(get_session)):
    session.revoked_at = now()
    db.add(session)
    await add_event(db, request, "logout", "success", user.id, session.id)
    await db.commit()
    response.delete_cookie(AUTH_COOKIE, path="/", samesite="strict")


@router.get("/security/summary")
async def security_summary(user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    del session
    recovery_result = await db.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id, RecoveryCode.used_at == None))
    events_result = await db.execute(select(SecurityEvent).where(SecurityEvent.user_id == user.id, SecurityEvent.event_type == "login_success").order_by(SecurityEvent.created_at.desc()).limit(2))
    logins = events_result.scalars().all()
    previous = logins[1] if len(logins) > 1 else None
    return {
        "email": user.email,
        "twoFactorEnabled": user.two_factor_enabled,
        "recoveryCodesRemaining": len(recovery_result.scalars().all()),
        "sessionLifetimeDays": settings.SESSION_LIFETIME_DAYS,
        "previousLogin": None if not previous else {"at": previous.created_at, "ipAddress": previous.ip_address, "deviceLabel": previous.device_label},
    }


@router.put("/security/email")
async def update_email(req: UpdateEmailRequest, response: Response, request: Request, user: User = Depends(get_current_user), current: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    if not bcrypt.checkpw(req.current_password.encode("utf-8"), user.password_hash.encode("utf-8")):
        await register_failure(user, db, request, "email_change_failure")
        if user.lockout_until:
            raise lockout_exception(user)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_credentials", "message": "The current password was not accepted."})
    next_email = normalize_email(req.email)
    if next_email == user.email.lower():
        raise HTTPException(status_code=400, detail={"code": "no_changes", "message": "Enter a different email address."})
    duplicate = await db.execute(select(User).where(func.lower(User.email) == next_email, User.id != user.id))
    if duplicate.scalars().first():
        raise HTTPException(status_code=409, detail={"code": "email_unavailable", "message": "That email address is already in use."})
    previous_email = user.email
    user.email = next_email
    db.add(user)
    await db.execute(delete(AuthChallenge).where(AuthChallenge.user_id == user.id, AuthChallenge.used_at == None))
    revoked = await revoke_other_user_sessions(user, current, db)
    await add_event(db, request, "email_changed", "success", user.id, current.id, {
        "previousEmail": previous_email,
        "otherSessionsRevoked": revoked,
    })
    await db.commit()
    access_token = encode_session_token(user, current)
    set_session_cookie(response, access_token, request, max(1, int(current.expires_at - now())))
    return {
        "message": "Account email updated.",
        "email": user.email,
        "otherSessionsRevoked": revoked,
    }


@router.put("/security/password")
async def update_password(req: UpdatePasswordRequest, request: Request, user: User = Depends(get_current_user), current: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    current_password = req.current_password.encode("utf-8")
    new_password = req.new_password.encode("utf-8")
    if not bcrypt.checkpw(current_password, user.password_hash.encode("utf-8")):
        await register_failure(user, db, request, "password_change_failure")
        if user.lockout_until:
            raise lockout_exception(user)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_credentials", "message": "The current password was not accepted."})
    if len(req.new_password) < 6:
        raise HTTPException(status_code=422, detail={"code": "weak_password", "message": "The new password must contain at least six characters."})
    if len(new_password) > 72:
        raise HTTPException(status_code=422, detail={"code": "password_too_long", "message": "The new password must not exceed 72 UTF-8 bytes."})
    if bcrypt.checkpw(new_password, user.password_hash.encode("utf-8")):
        raise HTTPException(status_code=400, detail={"code": "no_changes", "message": "Choose a password different from the current password."})
    user.password_hash = bcrypt.hashpw(new_password, bcrypt.gensalt()).decode("utf-8")
    db.add(user)
    await db.execute(delete(AuthChallenge).where(AuthChallenge.user_id == user.id, AuthChallenge.used_at == None))
    revoked = await revoke_other_user_sessions(user, current, db)
    await add_event(db, request, "password_changed", "success", user.id, current.id, {"otherSessionsRevoked": revoked})
    await db.commit()
    return {"message": "Password updated.", "otherSessionsRevoked": revoked}


@router.put("/security/session-policy")
async def update_session_policy(req: SessionPolicyRequest, request: Request, user: User = Depends(get_current_user), current: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    if req.session_lifetime_days < 1 or req.session_lifetime_days > 365:
        raise HTTPException(status_code=422, detail={"code": "invalid_session_lifetime", "message": "Session lifetime must be between 1 and 365 days."})
    previous_days = settings.SESSION_LIFETIME_DAYS
    if req.session_lifetime_days == previous_days:
        raise HTTPException(status_code=400, detail={"code": "no_changes", "message": "Enter a different session lifetime."})
    settings.SESSION_LIFETIME_DAYS = req.session_lifetime_days
    settings.JWT_EXPIRATION_MINUTES = 60 * 24 * req.session_lifetime_days
    settings.save_to_json()
    await add_event(db, request, "session_policy_changed", "success", user.id, current.id, {
        "previousDays": previous_days,
        "sessionLifetimeDays": req.session_lifetime_days,
        "existingSessionsChanged": False,
    })
    await db.commit()
    return {
        "message": "Session lifetime updated for new sign-ins.",
        "sessionLifetimeDays": settings.SESSION_LIFETIME_DAYS,
        "existingSessionsChanged": False,
    }


@router.get("/sessions")
async def list_sessions(user: User = Depends(get_current_user), current: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at == None, AuthSession.expires_at > now()).order_by(AuthSession.last_seen_at.desc()))
    return [{"id": item.id, "createdAt": item.created_at, "lastSeenAt": item.last_seen_at, "expiresAt": item.expires_at, "ipAddress": item.ip_address, "deviceLabel": item.device_label, "current": item.id == current.id} for item in result.scalars().all()]


@router.get("/integrations")
async def list_integrations(user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    del user, session
    result = await db.execute(select(IntegrationCredential).order_by(IntegrationCredential.created_at.desc()))
    return [{
        "id": item.id,
        "name": item.name,
        "scopes": item.scopes,
        "createdAt": item.created_at,
        "expiresAt": item.expires_at,
        "revokedAt": item.revoked_at,
        "lastUsedAt": item.last_used_at,
    } for item in result.scalars().all()]


@router.post("/integrations/rotate")
async def rotate_ingestion_credential(req: IntegrationCredentialRequest, request: Request, user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    name = req.name.strip()[:80]
    if not name:
        raise HTTPException(status_code=422, detail={"code": "invalid_integration_name", "message": "Enter a name for the integration credential."})
    timestamp = now()
    existing = (await db.execute(select(IntegrationCredential).where(IntegrationCredential.revoked_at == None))).scalars().all()
    for item in existing:
        if "ingest" in item.scopes:
            item.revoked_at = timestamp
            db.add(item)
    raw_token = secrets.token_urlsafe(36)
    credential = IntegrationCredential(
        id=str(uuid.uuid4()),
        name=name,
        token_hash=integration_token_hash(raw_token),
        created_at=timestamp,
    )
    credential.scopes = ["ingest"]
    db.add(credential)
    await add_event(db, request, "integration_credential_rotated", "success", user.id, session.id, {"credentialId": credential.id, "name": name})
    await db.commit()
    return {"credential": {"id": credential.id, "name": credential.name, "scopes": credential.scopes, "createdAt": credential.created_at}, "token": raw_token}


@router.delete("/integrations/{credential_id}")
async def revoke_integration_credential(credential_id: str, request: Request, user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    credential = await db.get(IntegrationCredential, credential_id)
    if not credential:
        raise HTTPException(status_code=404, detail="Integration credential not found")
    if not credential.revoked_at:
        credential.revoked_at = now()
        db.add(credential)
        await add_event(db, request, "integration_credential_revoked", "success", user.id, session.id, {"credentialId": credential.id, "name": credential.name})
        await db.commit()
    return {"revoked": True}


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, request: Request, user: User = Depends(get_current_user), current: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user.id))
    target = result.scalars().first()
    if not target:
        raise HTTPException(status_code=404, detail="Session not found")
    target.revoked_at = now()
    db.add(target)
    await add_event(db, request, "session_revoked", "success", user.id, current.id, {"currentSession": target.id == current.id})
    await db.commit()
    return {"revoked": True, "currentSession": target.id == current.id}


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request, user: User = Depends(get_current_user), current: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    revoked = await revoke_other_user_sessions(user, current, db)
    await add_event(db, request, "sessions_revoked", "success", user.id, current.id, {"count": revoked})
    await db.commit()
    return {"revokedCount": revoked}


@router.get("/security/events")
async def security_events(before: Optional[float] = None, limit: int = 25, user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    del session
    safe_limit = max(1, min(limit, 50))
    statement = select(SecurityEvent).where(SecurityEvent.user_id == user.id)
    if before:
        statement = statement.where(SecurityEvent.created_at < before)
    result = await db.execute(statement.order_by(SecurityEvent.created_at.desc()).limit(safe_limit + 1))
    rows = result.scalars().all()
    has_more = len(rows) > safe_limit
    rows = rows[:safe_limit]
    return {"events": [{"id": item.id, "type": item.event_type, "outcome": item.outcome, "createdAt": item.created_at, "ipAddress": item.ip_address, "deviceLabel": item.device_label, "details": json.loads(item.details) if item.details else None} for item in rows], "nextCursor": rows[-1].created_at if has_more and rows else None}


async def replace_recovery_codes(user: User, db: AsyncSession) -> list[str]:
    existing = await db.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id))
    for item in existing.scalars().all():
        await db.delete(item)
    codes: list[str] = []
    timestamp = now()
    for _ in range(RECOVERY_CODE_COUNT):
        compact = secrets.token_hex(8).upper()
        display = f"{compact[:4]}-{compact[4:8]}-{compact[8:12]}-{compact[12:]}"
        codes.append(display)
        db.add(RecoveryCode(id=str(uuid.uuid4()), user_id=user.id, code_hash=recovery_hash(display), created_at=timestamp))
    return codes


@router.post("/recovery-codes/regenerate")
async def regenerate_recovery_codes(request: Request, user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    if not user.two_factor_enabled:
        raise HTTPException(status_code=400, detail="Enable TOTP before generating recovery codes")
    codes = await replace_recovery_codes(user, db)
    await add_event(db, request, "recovery_codes_regenerated", "success", user.id, session.id)
    await db.commit()
    return {"recoveryCodes": codes, "remaining": len(codes)}


@router.post("/2fa/setup")
async def setup_totp(user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    del session
    if user.two_factor_enabled:
        raise HTTPException(status_code=400, detail="TOTP is already enabled")
    secret = pyotp.random_base32()
    user.totp_secret = protect_secret(secret)
    user.two_factor_enabled = False
    db.add(user)
    await db.commit()
    return {"secret": secret, "provisioning_uri": pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="StreamHome")}


@router.post("/2fa/verify-setup")
async def verify_totp_setup(req: TOTPVerifySetupRequest, request: Request, user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    if not user.totp_secret or not pyotp.TOTP(reveal_secret(user.totp_secret)).verify(req.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid verification code")
    user.two_factor_enabled = True
    user.failed_login_attempts = 0
    user.lockout_until = None
    codes = await replace_recovery_codes(user, db)
    db.add(user)
    await add_event(db, request, "totp_enabled", "success", user.id, session.id)
    await db.commit()
    return {"message": "TOTP successfully enabled.", "recoveryCodes": codes}


@router.post("/2fa/disable")
async def disable_totp(req: TOTPDisableRequest, request: Request, user: User = Depends(get_current_user), session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    if not user.two_factor_enabled or not user.totp_secret:
        return {"message": "TOTP is already disabled."}
    if not pyotp.TOTP(reveal_secret(user.totp_secret)).verify(req.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid verification code")
    user.two_factor_enabled = False
    user.totp_secret = None
    db.add(user)
    codes = await db.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id))
    for item in codes.scalars().all():
        await db.delete(item)
    others = await db.execute(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.id != session.id, AuthSession.revoked_at == None))
    for item in others.scalars().all():
        item.revoked_at = now()
        db.add(item)
    await add_event(db, request, "totp_disabled", "success", user.id, session.id)
    await db.commit()
    return {"message": "TOTP successfully disabled."}


@router.get("/2fa/status")
async def get_totp_status(user: User = Depends(get_current_user)):
    return {"two_factor_enabled": user.two_factor_enabled, "email": user.email}
