import re
import time

import bcrypt
from fastapi import HTTPException, status
from sqlalchemy import exists, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from models import AuthSession, Profile


PROFILE_PIN_RE = re.compile(r"^[0-9]{4,8}$")


def validate_profile_pin(pin: str) -> str:
    normalized = pin.strip()
    if not PROFILE_PIN_RE.fullmatch(normalized):
        raise ValueError("Profile PINs must contain 4 to 8 digits.")
    return normalized


def hash_profile_pin(pin: str) -> str:
    normalized = validate_profile_pin(pin)
    return bcrypt.hashpw(normalized.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_profile_pin(pin: str, pin_hash: str) -> bool:
    try:
        normalized = validate_profile_pin(pin)
        return bcrypt.checkpw(normalized.encode("utf-8"), pin_hash.encode("utf-8"))
    except (TypeError, ValueError):
        return False


async def require_profile_access(
    db: AsyncSession,
    auth_session: AuthSession,
    profile_id: str,
) -> Profile:
    profile = await db.get(Profile, profile_id, populate_existing=True)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "profile_not_found", "message": "That profile does not exist."},
        )
    if not profile.pin_enabled or not profile.pin_hash:
        return profile
    # An authentication dependency can have loaded its session before profile
    # deletion cleared that grant. Do not reuse that stale dependency snapshot
    # when selecting a newly recreated profile with an equal PIN version.
    stored_session = await db.get(AuthSession, auth_session.id, populate_existing=True)
    if not stored_session or stored_session.revoked_at or stored_session.expires_at <= time.time():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_session", "message": "The signed-in session is no longer active."},
        )
    if (
        stored_session.selected_profile_id != profile.id
        or stored_session.selected_profile_pin_version != profile.pin_version
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "profile_pin_required", "message": "Enter this profile's PIN before continuing."},
        )
    return profile


async def grant_profile_access(
    db: AsyncSession,
    auth_session: AuthSession,
    profile: Profile,
) -> None:
    session_id = auth_session.id
    profile_id = profile.id
    pin_version = profile.pin_version
    pin_enabled = profile.pin_enabled
    pin_hash = profile.pin_hash
    verified_protection = exists(
        select(Profile.id).where(
            Profile.id == profile_id,
            Profile.pin_enabled.is_(pin_enabled),
            Profile.pin_hash == pin_hash if pin_hash is not None else Profile.pin_hash.is_(None),
            Profile.pin_version == pin_version if pin_version is not None else Profile.pin_version.is_(None),
        )
    )
    now = time.time()
    result = await db.execute(
        update(AuthSession)
        .where(
            AuthSession.id == session_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
            verified_protection,
        )
        .values(
            selected_profile_id=profile_id,
            selected_profile_pin_version=pin_version if pin_enabled and pin_hash else None,
        )
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount:
        # The exact salted hash distinguishes delete/recreate of the same ID and
        # PIN version. The check and grant are one writer operation; deletion
        # clears grants while holding that same SQLite writer transaction.
        await db.commit()
        return
    stored_session = await db.get(AuthSession, session_id, populate_existing=True)
    if not stored_session or stored_session.revoked_at or stored_session.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_session", "message": "The signed-in session is no longer active."},
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "profile_pin_required", "message": "Profile protection changed. Select or unlock this profile again."},
    )
