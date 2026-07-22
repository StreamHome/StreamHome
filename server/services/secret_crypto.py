from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from config import settings


PREFIX = "enc:v1:"


def protect_secret(value: str | None) -> str | None:
    if not value or value.startswith(PREFIX):
        return value
    token = Fernet(settings.SECRET_ENCRYPTION_KEY.encode("ascii")).encrypt(value.encode("utf-8"))
    return PREFIX + token.decode("ascii")


def reveal_secret(value: str | None) -> str | None:
    if not value or not value.startswith(PREFIX):
        return value
    try:
        return Fernet(settings.SECRET_ENCRYPTION_KEY.encode("ascii")).decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Stored authentication secret could not be decrypted.") from exc


def is_protected_secret(value: str | None) -> bool:
    return bool(value and value.startswith(PREFIX))
