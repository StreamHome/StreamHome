import re

import bcrypt


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
