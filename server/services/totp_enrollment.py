from __future__ import annotations

import io
import time

import pyotp
import segno

from models import TOTPEnrollment
from services.secret_crypto import protect_secret, reveal_secret

TOTP_ENROLLMENT_SECONDS = 15 * 60
TOTP_ISSUER = "StreamHome"


def normalize_totp_email(email: str) -> str:
    normalized = email.strip().lower()
    if "@" not in normalized or any(character.isspace() for character in normalized):
        raise ValueError("Enter a valid administrator email.")
    return normalized


def create_totp_enrollment(
    *,
    enrollment_id: str,
    owner_type: str,
    email: str,
    setup_session_hash: str | None = None,
    user_id: int | None = None,
    auth_session_id: str | None = None,
    current_time: float | None = None,
) -> tuple[TOTPEnrollment, str]:
    timestamp = current_time if current_time is not None else time.time()
    secret = pyotp.random_base32()
    enrollment = TOTPEnrollment(
        id=enrollment_id,
        owner_type=owner_type,
        setup_session_hash=setup_session_hash,
        user_id=user_id,
        auth_session_id=auth_session_id,
        email=normalize_totp_email(email),
        secret_encrypted=protect_secret(secret),
        created_at=timestamp,
        expires_at=timestamp + TOTP_ENROLLMENT_SECONDS,
    )
    return enrollment, secret


def enrollment_secret(enrollment: TOTPEnrollment) -> str:
    return reveal_secret(enrollment.secret_encrypted)


def provisioning_uri(enrollment: TOTPEnrollment) -> str:
    return pyotp.TOTP(enrollment_secret(enrollment)).provisioning_uri(
        name=enrollment.email,
        issuer_name=TOTP_ISSUER,
    )


def verify_enrollment_code(enrollment: TOTPEnrollment, code: str) -> bool:
    normalized = "".join(character for character in code if character.isdigit())
    return len(normalized) == 6 and pyotp.TOTP(enrollment_secret(enrollment)).verify(normalized, valid_window=1)


def render_qr_svg(enrollment: TOTPEnrollment) -> bytes:
    output = io.BytesIO()
    qr_code = segno.make_qr(provisioning_uri(enrollment), error="M")
    qr_code.save(
        output,
        kind="svg",
        scale=6,
        border=3,
        dark="#120b08",
        light="#fffaf7",
        xmldecl=False,
        svgns=True,
    )
    return output.getvalue()


def qr_response_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, private, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
