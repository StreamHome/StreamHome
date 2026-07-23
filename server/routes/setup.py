import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

import bcrypt
import httpx
import jwt
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import AuthChallenge, DriveSetupJob, IntegrationCredential, RecoveryCode, User
from services.integration_auth import integration_token_hash
from services.rclone import REMOTE_NAME_RE, rclone_service
from services.request_security import client_ip, normalize_origin, request_is_secure, trusted_proxy_origin
from services.secret_crypto import protect_secret
from services.rate_limit import clear as clear_rate_limit
from services.rate_limit import enforce as enforce_rate_limit
from services.rate_limit import fail as fail_rate_limit

router = APIRouter(prefix="/api/setup", tags=["Setup"])
SETUP_COOKIE = "streamhome_setup"
SETUP_SESSION_SECONDS = 30 * 60
_failed_unlocks: dict[str, tuple[int, float]] = {}
_completion_lock = asyncio.Lock()
GOOGLE_DRIVE_GUIDE_URL = "https://github.com/WaqSea/StreamHome/blob/main/docs/google-drive.md"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class UnlockRequest(BaseModel):
    code: str


class TMDBValidationRequest(BaseModel):
    token: str


class TOTPBeginRequest(BaseModel):
    email: str


class TOTPVerifyRequest(BaseModel):
    secret: str
    code: str


class DriveOAuthStartRequest(BaseModel):
    client_id: str
    client_secret: str
    remote_name: str = "streamhome-drive"
    audience: str = "external"
    publishing_status: str = "production"
    public_url: str


class DriveFolderRequest(BaseModel):
    path: str = ""


class DriveSelectFolderRequest(BaseModel):
    path: str


class CompleteRequest(BaseModel):
    email: str
    password: str
    tmdb_token: str
    web_port: int = 3000
    totp_secret: Optional[str] = None
    totp_code: Optional[str] = None
    backup_enabled: bool = False
    auto_update_enabled: bool = False
    hevc_compression_mode: str = "auto"
    storage_engine: str = "LOCAL"
    rclone_remote_path: Optional[str] = None
    drive_job_id: Optional[str] = None
    public_url: str = ""


def _now() -> int:
    return int(time.time())


def setup_required() -> bool:
    return not settings.SETUP_COMPLETE


def _bootstrap_code() -> str:
    return os.getenv("STREAMHOME_SETUP_CODE", "")


def _setup_token(session_id: str) -> str:
    return jwt.encode({"purpose": "setup", "sid": session_id, "iat": _now(), "exp": _now() + SETUP_SESSION_SECONDS}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _setup_session_id(request: Request) -> Optional[str]:
    token = request.cookies.get(SETUP_COOKIE)
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        session_id = payload.get("sid")
        return session_id if payload.get("purpose") == "setup" and isinstance(session_id, str) else None
    except Exception:
        return None


def _is_unlocked(request: Request) -> bool:
    return bool(setup_required() and _setup_session_id(request))


def require_setup_session(request: Request) -> None:
    if not setup_required():
        raise HTTPException(status_code=409, detail={"code": "setup_complete", "message": "StreamHome is already configured."})
    if not _is_unlocked(request):
        raise HTTPException(status_code=401, detail={"code": "setup_locked", "message": "Enter the bootstrap code printed by start.sh."})


def _hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_public_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_public_url", "message": "Enter a valid public StreamHome URL."}) from exc
    localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in ({"http", "https"} if localhost else {"https"}):
        raise HTTPException(status_code=422, detail={"code": "invalid_public_url", "message": "The public URL must use HTTPS outside localhost."})
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_public_url", "message": "Use only the public origin without a path, query, or credentials."})
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _drive_callback_url(public_url: str) -> str:
    return f"{_normalize_public_url(public_url)}/api/setup/rclone/drive/callback"


def _safe_drive_path(value: str, *, allow_empty: bool = True) -> str:
    normalized = value.strip().replace("\\", "/").strip("/")
    if not normalized and allow_empty:
        return ""
    parts = normalized.split("/")
    if not normalized or len(normalized) > 512 or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail={"code": "invalid_drive_path", "message": "Choose a valid Google Drive folder."})
    if ":" in normalized or any(ord(character) < 32 for character in normalized):
        raise HTTPException(status_code=422, detail={"code": "invalid_drive_path", "message": "The Drive folder contains unsupported characters."})
    return normalized


async def _drive_job(db: AsyncSession, request: Request, job_id: str, *, allow_terminal: bool = True) -> DriveSetupJob:
    job = await db.get(DriveSetupJob, job_id)
    session_id = _setup_session_id(request)
    if not job or not session_id or not hmac.compare_digest(job.session_hash, _hash_value(session_id)):
        raise HTTPException(status_code=404, detail={"code": "drive_job_not_found", "message": "This Google Drive setup job was not found."})
    if job.expires_at < time.time() and job.status not in {"cancelled", "failed", "expired"}:
        job.status = "expired"
        job.error_code = "drive_job_expired"
        job.progress = "Google Drive setup expired. Start again."
        job.updated_at = time.time()
        db.add(job)
        await db.commit()
        rclone_service.cleanup_job(job.id)
    if not allow_terminal and job.status in {"cancelled", "failed", "expired"}:
        raise HTTPException(status_code=409, detail={"code": job.error_code or "drive_job_unavailable", "message": job.progress})
    return job


def _drive_job_response(job: DriveSetupJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "remoteName": job.remote_name,
        "selectedPath": job.selected_path,
        "progress": job.progress,
        "errorCode": job.error_code,
        "audience": job.audience,
        "publishingStatus": job.publishing_status,
        "expiresAt": job.expires_at,
    }


def _atomic_update_env(path: str, updates: dict[str, str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                value = remaining.pop(key).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
                output.append(f'{key}="{value}"')
                continue
        output.append(line)
    for key, raw_value in remaining.items():
        value = raw_value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
        output.append(f'{key}="{value}"')
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _recovery_hash(value: str) -> str:
    normalized = value.replace("-", "").replace(" ", "").upper()
    return hmac.new(settings.JWT_SECRET.encode(), normalized.encode(), hashlib.sha256).hexdigest()


def _make_recovery_codes(user_id: int, db: AsyncSession) -> list[str]:
    codes: list[str] = []
    timestamp = time.time()
    for _ in range(10):
        compact = secrets.token_hex(8).upper()
        display = f"{compact[:4]}-{compact[4:8]}-{compact[8:12]}-{compact[12:]}"
        codes.append(display)
        db.add(RecoveryCode(id=str(uuid.uuid4()), user_id=user_id, code_hash=_recovery_hash(display), created_at=timestamp))
    return codes


def _port_available(port: int) -> bool:
    if port == settings.WEB_PORT:
        return True
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


@router.get("/status")
async def get_setup_status(request: Request, db: AsyncSession = Depends(get_session)):
    user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    required = setup_required() or user_count == 0
    if required and settings.SETUP_COMPLETE:
        settings.SETUP_COMPLETE = False
    unlocked = _is_unlocked(request)
    public_url = settings.PUBLIC_URL
    configured_origin = normalize_origin(public_url)
    configured_host = urlsplit(configured_origin).hostname if configured_origin else None
    configured_loopback = configured_host in {"localhost", "127.0.0.1", "::1"}
    public_url_explicit = os.getenv("STREAMHOME_PUBLIC_URL_EXPLICIT", "false").lower() in {"true", "1", "yes"}
    browser_origin = trusted_proxy_origin(request)
    if required and browser_origin and (configured_loopback or not public_url_explicit):
        public_url = browser_origin
    return {
        "required": required,
        "unlocked": unlocked,
        "webPort": settings.WEB_PORT,
        "serverVersion": settings.APP_VERSION,
        "mediaPath": str((Path(settings.BASE_DIR) / "server" / "media").resolve()) if unlocked else "",
        "databasePath": settings.db_path if unlocked else "",
        "publicUrl": public_url,
        "driveCallbackUrl": _drive_callback_url(public_url),
        "driveGuideUrl": GOOGLE_DRIVE_GUIDE_URL,
    }


@router.post("/unlock", status_code=204)
async def unlock_setup(payload: UnlockRequest, request: Request, response: Response, db: AsyncSession = Depends(get_session)):
    if not setup_required():
        raise HTTPException(status_code=409, detail={"code": "setup_complete", "message": "StreamHome is already configured."})
    ip = client_ip(request)
    await enforce_rate_limit(db, "setup_unlock", ip)
    attempts, blocked_until = _failed_unlocks.get(ip, (0, 0))
    if blocked_until > time.time():
        retry = max(1, int(blocked_until - time.time()))
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry)}, detail={"code": "setup_locked_out", "message": "Too many invalid setup attempts.", "retryAfterSeconds": retry})
    expected = _bootstrap_code()
    if not expected or not hmac.compare_digest(payload.code.strip(), expected):
        attempts += 1
        _failed_unlocks[ip] = (attempts, time.time() + 300 if attempts >= 5 else 0)
        await fail_rate_limit(db, "setup_unlock", ip, limit=5, window_seconds=300)
        raise HTTPException(status_code=401, detail={"code": "invalid_setup_code", "message": "The bootstrap code was not accepted."})
    _failed_unlocks.pop(ip, None)
    await clear_rate_limit(db, "setup_unlock", ip)
    session_id = secrets.token_urlsafe(24)
    response.set_cookie(SETUP_COOKIE, _setup_token(session_id), max_age=SETUP_SESSION_SECONDS, httponly=True, samesite="strict", secure=request_is_secure(request), path="/")


@router.get("/readiness", dependencies=[Depends(require_setup_session)])
async def readiness(db: AsyncSession = Depends(get_session)):
    checks = []
    for name, command in (("python", shutil.which("python") or shutil.which("python3")), ("node", shutil.which("node")), ("ffmpeg", shutil.which("ffmpeg")), ("ffprobe", shutil.which("ffprobe")), ("rclone", rclone_service.executable())):
        checks.append({"id": name, "ready": bool(command), "detail": command or "Not found"})
    for name, path in (("database", Path(settings.db_path).parent), ("media", Path(settings.BASE_DIR) / "server" / "media")):
        path.mkdir(parents=True, exist_ok=True)
        checks.append({"id": name, "ready": os.access(path, os.W_OK), "detail": str(path.resolve())})
    try:
        await db.execute(text("SELECT 1"))
        database_ready = True
    except Exception:
        database_ready = False
    checks.append({"id": "database_connection", "ready": database_ready, "detail": "SQLite readiness"})
    return {"checks": checks, "ready": all(check["ready"] for check in checks if check["id"] != "rclone")}


@router.post("/tmdb/validate", dependencies=[Depends(require_setup_session)])
async def validate_tmdb(payload: TMDBValidationRequest):
    token = payload.token.strip()
    if not token:
        raise HTTPException(status_code=422, detail={"code": "tmdb_required", "message": "A TMDB read-access token is required."})
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://api.themoviedb.org/3/configuration", headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            raise HTTPException(status_code=422, detail={"code": "invalid_tmdb_token", "message": "TMDB did not accept this read-access token."})
        return {"valid": True}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail={"code": "tmdb_timeout", "message": "TMDB validation timed out."})
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail={"code": "tmdb_unreachable", "message": "TMDB could not be reached."})


@router.post("/totp/begin", dependencies=[Depends(require_setup_session)])
async def begin_totp(payload: TOTPBeginRequest):
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail={"code": "invalid_email", "message": "Enter a valid administrator email."})
    secret = pyotp.random_base32()
    return {"secret": secret, "provisioningUri": pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="StreamHome")}


@router.post("/totp/verify", dependencies=[Depends(require_setup_session)])
async def verify_totp(payload: TOTPVerifyRequest):
    valid = pyotp.TOTP(payload.secret).verify(payload.code, valid_window=1)
    if not valid:
        raise HTTPException(status_code=422, detail={"code": "invalid_totp", "message": "The authenticator code was not accepted."})
    return {"valid": True}


@router.post("/rclone/drive/oauth/start", dependencies=[Depends(require_setup_session)])
async def start_drive_oauth(payload: DriveOAuthStartRequest, request: Request, db: AsyncSession = Depends(get_session)):
    client_id = payload.client_id.strip()
    client_secret = payload.client_secret.strip()
    remote_name = payload.remote_name.strip().lower()
    audience = payload.audience.strip().lower()
    publishing_status = payload.publishing_status.strip().lower()
    public_url = _normalize_public_url(payload.public_url)
    if not client_id or len(client_id) > 300 or not client_id.endswith(".apps.googleusercontent.com"):
        raise HTTPException(status_code=422, detail={"code": "invalid_google_client_id", "message": "Enter the Web application client ID from Google Cloud."})
    if not client_secret or len(client_secret) > 500:
        raise HTTPException(status_code=422, detail={"code": "invalid_google_client_secret", "message": "Enter the Google OAuth client secret."})
    if not REMOTE_NAME_RE.fullmatch(remote_name):
        raise HTTPException(status_code=422, detail={"code": "invalid_rclone_name", "message": "Use 2-32 lowercase letters, numbers, dashes, or underscores."})
    if audience not in {"external", "internal"} or publishing_status not in {"testing", "production"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_google_app_status", "message": "Choose the Google OAuth audience and publishing status."})
    session_id = _setup_session_id(request)
    if not session_id:
        raise HTTPException(status_code=401, detail={"code": "setup_locked", "message": "Unlock setup again."})
    job_id = str(uuid.uuid4())
    state_value = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    timestamp = time.time()
    job = DriveSetupJob(
        id=job_id,
        session_hash=_hash_value(session_id),
        state_hash=_hash_value(state_value),
        status="authorizing",
        remote_name=remote_name,
        audience=audience,
        publishing_status=publishing_status,
        public_url=public_url,
        progress="Waiting for Google authorization",
        created_at=timestamp,
        updated_at=timestamp,
        expires_at=timestamp + 10 * 60,
    )
    db.add(job)
    await db.commit()
    rclone_service.write_job_secret(job_id, {"client_id": client_id, "client_secret": client_secret, "pkce_verifier": verifier})
    authorization_url = f"{GOOGLE_AUTHORIZE_URL}?{urlencode({
        'client_id': client_id,
        'redirect_uri': _drive_callback_url(public_url),
        'response_type': 'code',
        'scope': DRIVE_SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'state': state_value,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })}"
    return {"jobId": job_id, "authorizationUrl": authorization_url, "expiresAt": job.expires_at}


@router.get("/rclone/drive/callback")
async def drive_oauth_callback(request: Request, db: AsyncSession = Depends(get_session)):
    state_value = request.query_params.get("state", "")
    state_hash = _hash_value(state_value) if state_value else ""
    result = await db.execute(select(DriveSetupJob).where(DriveSetupJob.state_hash == state_hash))
    job = result.scalars().first()
    fallback_url = settings.PUBLIC_URL
    if not job:
        return RedirectResponse(f"{fallback_url}/setup?drive=error&code=drive_state_mismatch", status_code=303)
    session_id = _setup_session_id(request)
    if not session_id or not hmac.compare_digest(job.session_hash, _hash_value(session_id)) or job.expires_at < time.time() or job.status != "authorizing":
        job.status = "failed"
        job.error_code = "drive_state_mismatch"
        job.progress = "Google authorization could not be verified."
        job.updated_at = time.time()
        db.add(job)
        await db.commit()
        return RedirectResponse(f"{job.public_url}/setup?driveJob={job.id}&drive=error", status_code=303)
    oauth_error = request.query_params.get("error")
    code = request.query_params.get("code")
    if oauth_error or not code:
        job.status = "failed"
        job.error_code = "drive_authorization_denied" if oauth_error == "access_denied" else "drive_authorization_failed"
        job.progress = "Google Drive authorization was not completed."
        job.updated_at = time.time()
        db.add(job)
        await db.commit()
        rclone_service.cleanup_job(job.id)
        return RedirectResponse(f"{job.public_url}/setup?driveJob={job.id}&drive=error", status_code=303)
    job.status = "exchanging_code"
    job.progress = "Securing Google Drive authorization"
    job.updated_at = time.time()
    db.add(job)
    await db.commit()
    try:
        secret_payload = rclone_service.read_job_secret(job.id)
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id": secret_payload["client_id"],
                "client_secret": secret_payload["client_secret"],
                "code": code,
                "code_verifier": secret_payload["pkce_verifier"],
                "grant_type": "authorization_code",
                "redirect_uri": _drive_callback_url(job.public_url),
            })
        token_data = token_response.json()
        if token_response.status_code != 200:
            error_name = str(token_data.get("error", "")) if isinstance(token_data, dict) else ""
            error_code = "invalid_google_client" if error_name in {"invalid_client", "redirect_uri_mismatch"} else "drive_token_exchange_failed"
            raise HTTPException(status_code=422, detail={"code": error_code, "message": "Google did not accept the OAuth callback."})
        refresh_token = token_data.get("refresh_token")
        access_token = token_data.get("access_token")
        if not refresh_token or not access_token:
            raise HTTPException(status_code=422, detail={"code": "drive_refresh_token_missing", "message": "Google did not return offline access. Reconnect and approve access again."})
        expires_in = max(60, int(token_data.get("expires_in", 3600)))
        token = {
            "access_token": access_token,
            "token_type": token_data.get("token_type", "Bearer"),
            "refresh_token": refresh_token,
            "expiry": __import__("datetime").datetime.fromtimestamp(time.time() + expires_in, __import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        secret_payload["token"] = token
        rclone_service.write_job_secret(job.id, secret_payload)
        config_path = rclone_service.write_drive_config(job.id, job.remote_name, secret_payload)
        check = await rclone_service.run("about", f"{job.remote_name}:", "--json", config_path=config_path, timeout=30)
        if not check.ok:
            raise HTTPException(status_code=422, detail={"code": check.error_code or "drive_configuration_invalid", "message": "Rclone could not use the Google Drive authorization."})
        job.status = "selecting_folder"
        job.progress = "Google Drive connected. Select a media folder."
        job.error_code = None
        job.updated_at = time.time()
        job.expires_at = time.time() + 30 * 60
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        job.status = "failed"
        job.error_code = str(detail.get("code", "drive_authorization_failed"))
        job.progress = str(detail.get("message", "Google Drive authorization failed."))
        job.updated_at = time.time()
    except (httpx.HTTPError, ValueError, KeyError, OSError, json.JSONDecodeError):
        job.status = "failed"
        job.error_code = "drive_authorization_failed"
        job.progress = "Google Drive authorization could not be completed."
        job.updated_at = time.time()
    if job.status == "failed":
        rclone_service.cleanup_job(job.id)
    db.add(job)
    await db.commit()
    return RedirectResponse(f"{job.public_url}/setup?driveJob={job.id}&drive={'connected' if job.status == 'selecting_folder' else 'error'}", status_code=303)


@router.get("/rclone/drive/jobs/{job_id}", dependencies=[Depends(require_setup_session)])
async def get_drive_job(job_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    return _drive_job_response(await _drive_job(db, request, job_id))


@router.delete("/rclone/drive/jobs/{job_id}", status_code=204, dependencies=[Depends(require_setup_session)])
async def cancel_drive_job(job_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    job = await _drive_job(db, request, job_id)
    job.status = "cancelled"
    job.error_code = "drive_job_cancelled"
    job.progress = "Google Drive setup was cancelled."
    job.updated_at = time.time()
    db.add(job)
    await db.commit()
    rclone_service.cleanup_job(job.id)


@router.get("/rclone/drive/jobs/{job_id}/folders", dependencies=[Depends(require_setup_session)])
async def list_drive_folders(job_id: str, request: Request, path: str = "", db: AsyncSession = Depends(get_session)):
    job = await _drive_job(db, request, job_id, allow_terminal=False)
    normalized = _safe_drive_path(path)
    config_path = rclone_service.job_dir(job.id) / "rclone.conf"
    target = f"{job.remote_name}:{normalized}"
    result = await rclone_service.run("lsjson", target, "--dirs-only", "--max-depth", "1", config_path=config_path, timeout=30)
    if not result.ok:
        raise HTTPException(status_code=422, detail={"code": result.error_code or "drive_folder_list_failed", "message": "Google Drive folders could not be listed."})
    try:
        items = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail={"code": "invalid_rclone_response", "message": "Rclone returned an invalid folder list."}) from exc
    folders = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("IsDir"):
            continue
        name = str(item.get("Name", ""))
        child_path = "/".join(part for part in (normalized, name) if part)
        folders.append({"name": name, "path": child_path, "id": item.get("ID")})
    return {"path": normalized, "folders": folders}


@router.post("/rclone/drive/jobs/{job_id}/folders", dependencies=[Depends(require_setup_session)])
async def create_drive_folder(job_id: str, payload: DriveFolderRequest, request: Request, db: AsyncSession = Depends(get_session)):
    job = await _drive_job(db, request, job_id, allow_terminal=False)
    normalized = _safe_drive_path(payload.path, allow_empty=False)
    config_path = rclone_service.job_dir(job.id) / "rclone.conf"
    result = await rclone_service.run("mkdir", f"{job.remote_name}:{normalized}", config_path=config_path, timeout=30)
    if not result.ok:
        raise HTTPException(status_code=422, detail={"code": result.error_code or "drive_folder_create_failed", "message": "The Google Drive folder could not be created."})
    return {"path": normalized}


@router.post("/rclone/drive/jobs/{job_id}/select-folder", dependencies=[Depends(require_setup_session)])
async def select_drive_folder(job_id: str, payload: DriveSelectFolderRequest, request: Request, db: AsyncSession = Depends(get_session)):
    job = await _drive_job(db, request, job_id, allow_terminal=False)
    job.selected_path = _safe_drive_path(payload.path, allow_empty=False)
    job.status = "selecting_folder"
    job.progress = "Folder selected. Run the read and write test."
    job.error_code = None
    job.updated_at = time.time()
    db.add(job)
    await db.commit()
    return _drive_job_response(job)


@router.post("/rclone/drive/jobs/{job_id}/test", dependencies=[Depends(require_setup_session)])
async def test_drive_folder(job_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    job = await _drive_job(db, request, job_id, allow_terminal=False)
    if not job.selected_path:
        raise HTTPException(status_code=422, detail={"code": "drive_folder_required", "message": "Select a Google Drive folder first."})
    job.status = "testing"
    job.progress = "Testing Google Drive read and write access"
    job.updated_at = time.time()
    db.add(job)
    await db.commit()
    config_path = rclone_service.job_dir(job.id) / "rclone.conf"
    root = f"{job.remote_name}:{job.selected_path}"
    health_name = f".streamhome-healthcheck-{secrets.token_hex(8)}"
    health_path = f"{root}/{health_name}"
    quota: Optional[dict] = None
    try:
        listing = await rclone_service.run("lsjson", root, "--dirs-only", "--max-depth", "1", config_path=config_path, timeout=30)
        if not listing.ok:
            raise RuntimeError(listing.error_code or "drive_test_failed")
        upload = await rclone_service.run("rcat", health_path, config_path=config_path, timeout=30, input_data=b"StreamHome Google Drive health check\n")
        if not upload.ok:
            raise RuntimeError(upload.error_code or "drive_test_failed")
        verify = await rclone_service.run("lsjson", health_path, config_path=config_path, timeout=30)
        if not verify.ok:
            raise RuntimeError(verify.error_code or "drive_test_failed")
        about = await rclone_service.run("about", f"{job.remote_name}:", "--json", config_path=config_path, timeout=30)
        if about.ok:
            try:
                quota = json.loads(about.stdout)
            except json.JSONDecodeError:
                quota = None
        job.status = "ready"
        job.progress = "Google Drive is connected and ready."
        job.error_code = None
        job.updated_at = time.time()
        job.expires_at = time.time() + 60 * 60
        db.add(job)
        await db.commit()
        return {"valid": True, "remotePath": root, "quota": quota, "job": _drive_job_response(job)}
    except RuntimeError as exc:
        job.status = "selecting_folder"
        job.error_code = str(exc)
        job.progress = "Google Drive access test failed."
        job.updated_at = time.time()
        db.add(job)
        await db.commit()
        raise HTTPException(status_code=422, detail={"code": job.error_code, "message": job.progress})
    finally:
        await rclone_service.run("deletefile", health_path, config_path=config_path, timeout=20)


@router.post("/rclone/drive/jobs/{job_id}/activate", dependencies=[Depends(require_setup_session)])
async def activate_drive(job_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    job = await _drive_job(db, request, job_id)
    if job.status != "ready" or not job.selected_path:
        raise HTTPException(status_code=409, detail={"code": "drive_not_ready", "message": "Test Google Drive before activating it."})
    job.progress = "Google Drive is ready to activate when setup is finalized."
    job.updated_at = time.time()
    db.add(job)
    await db.commit()
    return {"valid": True, "remotePath": f"{job.remote_name}:{job.selected_path}", "job": _drive_job_response(job)}


async def _restart_after_response() -> None:
    await asyncio.sleep(1.25)
    script = Path(settings.BASE_DIR) / "start.sh"
    if os.name != "nt" and script.exists():
        subprocess.Popen(["bash", str(script)], cwd=settings.BASE_DIR, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@router.post("/complete", dependencies=[Depends(require_setup_session)])
async def complete_setup(payload: CompleteRequest, request: Request, response: Response, db: AsyncSession = Depends(get_session)):
    async with _completion_lock:
        if not setup_required():
            raise HTTPException(status_code=409, detail={"code": "setup_complete", "message": "StreamHome is already configured."})
        email = payload.email.strip().lower()
        if "@" not in email or any(character.isspace() for character in email):
            raise HTTPException(status_code=422, detail={"code": "invalid_email", "message": "Enter a valid administrator email."})
        password_bytes = payload.password.encode("utf-8")
        if len(payload.password) < 6 or len(password_bytes) > 72:
            raise HTTPException(status_code=422, detail={"code": "invalid_password", "message": "Use a password between 6 characters and 72 UTF-8 bytes."})
        if payload.web_port < 1 or payload.web_port > 65535:
            raise HTTPException(status_code=422, detail={"code": "invalid_web_port", "message": "Web port must be between 1 and 65535."})
        if not _port_available(payload.web_port):
            raise HTTPException(status_code=409, detail={"code": "web_port_in_use", "message": f"Port {payload.web_port} is already in use."})
        public_url = _normalize_public_url(payload.public_url or settings.PUBLIC_URL)
        if not payload.tmdb_token.strip():
            raise HTTPException(status_code=422, detail={"code": "tmdb_required", "message": "A validated TMDB token is required."})
        await validate_tmdb(TMDBValidationRequest(token=payload.tmdb_token))
        hevc_mode = payload.hevc_compression_mode.lower()
        if hevc_mode not in {"auto", "on", "off"}:
            raise HTTPException(status_code=422, detail={"code": "invalid_hevc_mode", "message": "Choose auto, on, or off."})
        storage = payload.storage_engine.upper()
        if storage not in {"LOCAL", "CLOUD"}:
            raise HTTPException(status_code=422, detail={"code": "invalid_storage_engine", "message": "Choose local or cloud storage."})
        drive_job: Optional[DriveSetupJob] = None
        remote_path = settings.RCLONE_REMOTE_PATH
        if storage == "CLOUD":
            if not payload.drive_job_id:
                raise HTTPException(status_code=422, detail={"code": "drive_job_required", "message": "Connect and test Google Drive before completing setup."})
            drive_job = await _drive_job(db, request, payload.drive_job_id)
            if drive_job.status != "ready" or not drive_job.selected_path:
                raise HTTPException(status_code=422, detail={"code": "drive_not_ready", "message": "Google Drive must pass its read and write test."})
            remote_path = f"{drive_job.remote_name}:{drive_job.selected_path}"
            await rclone_service.activate_remote(rclone_service.job_dir(drive_job.id) / "rclone.conf", drive_job.remote_name)
            remote_result = await rclone_service.run("lsjson", remote_path, "--dirs-only", "--max-depth", "1", timeout=30)
            if not remote_result.ok:
                raise HTTPException(status_code=422, detail={"code": remote_result.error_code or "drive_test_failed", "message": "The activated Google Drive remote could not be reached."})
        if payload.totp_secret and (not payload.totp_code or not pyotp.TOTP(payload.totp_secret).verify(payload.totp_code, valid_window=1)):
            raise HTTPException(status_code=422, detail={"code": "invalid_totp", "message": "Verify TOTP again before completing setup."})

        existing = (await db.execute(select(User).where(func.lower(User.email) == email))).scalars().first()
        if not existing:
            existing = (await db.execute(select(User).order_by(User.id).limit(1))).scalars().first()
        user = existing or User(email=email, password_hash="")
        user.email = email
        user.password_hash = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode()
        user.totp_secret = protect_secret(payload.totp_secret)
        user.two_factor_enabled = bool(payload.totp_secret)
        user.failed_login_attempts = 0
        user.lockout_until = None
        db.add(user)
        await db.flush()
        await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
        recovery_codes = _make_recovery_codes(user.id, db) if user.two_factor_enabled else []
        await db.execute(delete(AuthChallenge).where(AuthChallenge.user_id == user.id))

        ingestion_token = secrets.token_urlsafe(36)
        await db.execute(delete(IntegrationCredential))
        ingestion_credential = IntegrationCredential(
            id=str(uuid.uuid4()),
            name="MediaSender",
            token_hash=integration_token_hash(ingestion_token),
        )
        ingestion_credential.scopes = ["ingest"]
        db.add(ingestion_credential)
        _atomic_update_env(settings.SERVER_ENV_PATH, {
            "TMDB_READ_ACCESS_TOKEN": payload.tmdb_token.strip(),
            "STORAGE_ENGINE": storage,
            "RCLONE_REMOTE_PATH": remote_path,
            "GOOGLE_DRIVE_AUDIENCE": drive_job.audience if drive_job else settings.GOOGLE_DRIVE_AUDIENCE,
            "GOOGLE_DRIVE_PUBLISHING_STATUS": drive_job.publishing_status if drive_job else settings.GOOGLE_DRIVE_PUBLISHING_STATUS,
            "BACKUP_ENABLED": str(payload.backup_enabled).lower(),
            "AUTO_UPDATE_ENABLED": str(payload.auto_update_enabled).lower(),
            "HEVC_COMPRESSION_MODE": hevc_mode,
        })
        try:
            await db.commit()
            _atomic_update_env(settings.ROOT_ENV_PATH, {"WEB_PORT": str(payload.web_port), "PUBLIC_URL": public_url, "SETUP": "true"})
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=500, detail={"code": "setup_save_failed", "message": "Setup could not be saved. No installation flag was activated."})

        settings.SETUP_COMPLETE = True
        settings.WEB_PORT = payload.web_port
        settings.PUBLIC_URL = public_url
        settings.TMDB_READ_ACCESS_TOKEN = payload.tmdb_token.strip()
        settings.STORAGE_ENGINE = storage
        settings.RCLONE_REMOTE_PATH = remote_path
        if drive_job:
            settings.GOOGLE_DRIVE_AUDIENCE = drive_job.audience
            settings.GOOGLE_DRIVE_PUBLISHING_STATUS = drive_job.publishing_status
        settings.BACKUP_ENABLED = payload.backup_enabled
        settings.AUTO_UPDATE_ENABLED = payload.auto_update_enabled
        settings.HEVC_COMPRESSION_MODE = hevc_mode
        settings.save_to_json()
        if drive_job:
            rclone_service.cleanup_job(drive_job.id)
        response.delete_cookie(SETUP_COOKIE, path="/")
        asyncio.create_task(_restart_after_response())
        return {
            "complete": True,
            "restartScheduled": os.name != "nt",
            "webPort": payload.web_port,
            "recoveryCodes": recovery_codes,
            "ingestionToken": ingestion_token,
        }
