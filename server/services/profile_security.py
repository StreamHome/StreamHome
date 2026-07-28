import re

import bcrypt
from fastapi import HTTPException, status
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
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "profile_not_found", "message": "That profile does not exist."},
        )
    if not profile.pin_enabled or not profile.pin_hash:
        return profile
    if (
        auth_session.selected_profile_id != profile.id
        or auth_session.selected_profile_pin_version != profile.pin_version
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
    stored_session = await db.get(AuthSession, auth_session.id)
    if not stored_session or stored_session.revoked_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_session", "message": "The signed-in session is no longer active."},
        )
    stored_session.selected_profile_id = profile.id
    stored_session.selected_profile_pin_version = profile.pin_version if profile.pin_enabled and profile.pin_hash else None
    db.add(stored_session)
    await db.commit()
