import asyncio
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

import bcrypt
import httpx
import jwt
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, func, text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import AuthChallenge, RecoveryCode, User

router = APIRouter(prefix="/api/setup", tags=["Setup"])
SETUP_COOKIE = "streamhome_setup"
SETUP_SESSION_SECONDS = 30 * 60
_failed_unlocks: dict[str, tuple[int, float]] = {}
_completion_lock = asyncio.Lock()
_rclone_flows: dict[str, dict[str, Any]] = {}
RCLONE_PROVIDERS = {
    "drive": "Google Drive",
    "onedrive": "Microsoft OneDrive",
    "dropbox": "Dropbox",
    "s3": "Amazon S3 or compatible storage",
    "webdav": "WebDAV",
}


class UnlockRequest(BaseModel):
    code: str


class TMDBValidationRequest(BaseModel):
    token: str


class TOTPBeginRequest(BaseModel):
    email: str


class TOTPVerifyRequest(BaseModel):
    secret: str
    code: str


class RcloneTestRequest(BaseModel):
    remote_path: str


class RcloneConfigStartRequest(BaseModel):
    name: str
    provider: str


class RcloneConfigContinueRequest(BaseModel):
    flow_token: str
    result: str


class RcloneConfigCancelRequest(BaseModel):
    flow_token: str


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


def _now() -> int:
    return int(time.time())


def setup_required() -> bool:
    return not settings.SETUP_COMPLETE


def _bootstrap_code() -> str:
    return os.getenv("STREAMHOME_SETUP_CODE", "")


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _setup_token() -> str:
    return jwt.encode({"purpose": "setup", "iat": _now(), "exp": _now() + SETUP_SESSION_SECONDS}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _is_unlocked(request: Request) -> bool:
    token = request.cookies.get(SETUP_COOKIE)
    if not token or not setup_required():
        return False
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload.get("purpose") == "setup"
    except Exception:
        return False


def require_setup_session(request: Request) -> None:
    if not setup_required():
        raise HTTPException(status_code=409, detail={"code": "setup_complete", "message": "StreamHome is already configured."})
    if not _is_unlocked(request):
        raise HTTPException(status_code=401, detail={"code": "setup_locked", "message": "Enter the bootstrap code printed by start.sh."})


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


def _rclone_config_path() -> Path:
    return Path(settings.BASE_DIR) / "server" / "rclone" / "rclone.conf"


def _rclone_command() -> Optional[str]:
    return shutil.which("rclone")


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


async def _run_rclone(*arguments: str) -> tuple[int, str]:
    executable = _rclone_command()
    if not executable:
        return 127, "rclone is not installed"
    config = _rclone_config_path()
    command = [executable]
    if config.exists():
        command += ["--config", str(config)]
    command += list(arguments)
    try:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        return process.returncode or 0, output.decode("utf-8", errors="replace")[-2000:]
    except asyncio.TimeoutError:
        process.kill()
        return 124, "rclone timed out"


def _safe_remote_name(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", normalized):
        raise HTTPException(status_code=422, detail={"code": "invalid_rclone_name", "message": "Use 2-32 lowercase letters, numbers, dashes, or underscores."})
    return normalized


async def _run_rclone_rc(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    executable = _rclone_command()
    if not executable:
        raise HTTPException(status_code=503, detail={"code": "rclone_unavailable", "message": "Rclone is not installed."})
    config = _rclone_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    command = [executable, "--config", str(config), "rc", "--loopback", path, "--json", json.dumps(payload, separators=(",", ":"))]
    try:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        raise HTTPException(status_code=504, detail={"code": "rclone_timeout", "message": "Rclone configuration timed out."})
    if process.returncode:
        raise HTTPException(status_code=422, detail={"code": "rclone_configuration_failed", "message": "Rclone could not continue this configuration."})
    try:
        result = json.loads(stdout.decode("utf-8"))
        return result if isinstance(result, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail={"code": "invalid_rclone_response", "message": "Rclone returned an invalid configuration response."})


def _present_rclone_question(flow_token: str, response: dict[str, Any]) -> dict[str, Any]:
    option = response.get("Option") if isinstance(response.get("Option"), dict) else None
    state = response.get("State") if isinstance(response.get("State"), str) else ""
    error = response.get("Error") if isinstance(response.get("Error"), str) else ""
    if error:
        raise HTTPException(status_code=422, detail={"code": "rclone_configuration_failed", "message": "Rclone rejected this configuration value."})
    if not option or not state:
        _rclone_flows.pop(flow_token, None)
        return {"complete": True, "flowToken": flow_token}
    _rclone_flows[flow_token]["state"] = state
    _rclone_flows[flow_token]["expires"] = time.time() + 15 * 60
    examples = option.get("Examples") if isinstance(option.get("Examples"), list) else []
    return {
        "complete": False,
        "flowToken": flow_token,
        "question": {
            "name": str(option.get("Name", "value"))[:80],
            "help": str(option.get("Help", "Enter the requested value."))[:4000],
            "type": str(option.get("Type", "string"))[:40],
            "required": bool(option.get("Required", False)),
            "sensitive": bool(option.get("Sensitive", False) or option.get("IsPassword", False)),
            "defaultValue": str(option.get("DefaultStr", ""))[:1000],
            "examples": [
                {"value": str(item.get("Value", ""))[:1000], "help": str(item.get("Help", ""))[:500]}
                for item in examples[:25] if isinstance(item, dict)
            ],
        },
    }


@router.get("/status")
async def get_setup_status(request: Request, db: AsyncSession = Depends(get_session)):
    user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    required = setup_required() or user_count == 0
    if required and settings.SETUP_COMPLETE:
        settings.SETUP_COMPLETE = False
    unlocked = _is_unlocked(request)
    return {
        "required": required,
        "unlocked": unlocked,
        "webPort": settings.WEB_PORT,
        "serverVersion": settings.APP_VERSION,
        "mediaPath": str((Path(settings.BASE_DIR) / "server" / "media").resolve()) if unlocked else "",
        "databasePath": settings.db_path if unlocked else "",
    }


@router.post("/unlock", status_code=204)
async def unlock_setup(payload: UnlockRequest, request: Request, response: Response):
    if not setup_required():
        raise HTTPException(status_code=409, detail={"code": "setup_complete", "message": "StreamHome is already configured."})
    ip = _client_ip(request)
    attempts, blocked_until = _failed_unlocks.get(ip, (0, 0))
    if blocked_until > time.time():
        retry = max(1, int(blocked_until - time.time()))
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry)}, detail={"code": "setup_locked_out", "message": "Too many invalid setup attempts.", "retryAfterSeconds": retry})
    expected = _bootstrap_code()
    if not expected or not hmac.compare_digest(payload.code.strip(), expected):
        attempts += 1
        _failed_unlocks[ip] = (attempts, time.time() + 300 if attempts >= 5 else 0)
        raise HTTPException(status_code=401, detail={"code": "invalid_setup_code", "message": "The bootstrap code was not accepted."})
    _failed_unlocks.pop(ip, None)
    response.set_cookie(SETUP_COOKIE, _setup_token(), max_age=SETUP_SESSION_SECONDS, httponly=True, samesite="strict", secure=request.headers.get("x-forwarded-proto") == "https", path="/api/setup")


@router.get("/readiness", dependencies=[Depends(require_setup_session)])
async def readiness(db: AsyncSession = Depends(get_session)):
    checks = []
    for name, command in (("python", shutil.which("python") or shutil.which("python3")), ("node", shutil.which("node")), ("ffmpeg", shutil.which("ffmpeg")), ("ffprobe", shutil.which("ffprobe")), ("rclone", _rclone_command())):
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


@router.get("/rclone/remotes", dependencies=[Depends(require_setup_session)])
async def rclone_remotes():
    code, output = await _run_rclone("listremotes")
    if code != 0:
        return {"available": code != 127, "remotes": [], "error": "Rclone could not list application remotes."}
    return {"available": True, "remotes": [line.strip() for line in output.splitlines() if line.strip()]}


@router.get("/rclone/providers", dependencies=[Depends(require_setup_session)])
async def rclone_providers():
    return {"providers": [{"id": provider, "name": label} for provider, label in RCLONE_PROVIDERS.items()]}


@router.post("/rclone/config/start", dependencies=[Depends(require_setup_session)])
async def start_rclone_config(payload: RcloneConfigStartRequest):
    name = _safe_remote_name(payload.name)
    provider = payload.provider.strip().lower()
    if provider not in RCLONE_PROVIDERS:
        raise HTTPException(status_code=422, detail={"code": "unsupported_rclone_provider", "message": "Choose a supported storage provider."})
    flow_token = secrets.token_urlsafe(24)
    _rclone_flows[flow_token] = {"name": name, "provider": provider, "state": "", "expires": time.time() + 15 * 60}
    response = await _run_rclone_rc("config/create", {"name": name, "type": provider, "parameters": {}, "opt": {"nonInteractive": True}})
    return _present_rclone_question(flow_token, response)


@router.post("/rclone/config/continue", dependencies=[Depends(require_setup_session)])
async def continue_rclone_config(payload: RcloneConfigContinueRequest):
    flow = _rclone_flows.get(payload.flow_token)
    if not flow or flow["expires"] < time.time() or not flow["state"]:
        _rclone_flows.pop(payload.flow_token, None)
        raise HTTPException(status_code=410, detail={"code": "rclone_flow_expired", "message": "This rclone configuration expired. Start again."})
    if len(payload.result) > 20000:
        raise HTTPException(status_code=422, detail={"code": "rclone_value_too_large", "message": "The configuration value is too large."})
    response = await _run_rclone_rc("config/update", {
        "name": flow["name"], "parameters": {},
        "opt": {"nonInteractive": True, "continue": True, "state": flow["state"], "result": payload.result},
    })
    result = _present_rclone_question(payload.flow_token, response)
    if result["complete"]:
        result["remote"] = f"{flow['name']}:"
    return result


@router.post("/rclone/config/cancel", status_code=204, dependencies=[Depends(require_setup_session)])
async def cancel_rclone_config(payload: RcloneConfigCancelRequest):
    flow = _rclone_flows.pop(payload.flow_token, None)
    if flow:
        try:
            await _run_rclone_rc("config/delete", {"name": flow["name"]})
        except HTTPException:
            pass


@router.post("/rclone/test", dependencies=[Depends(require_setup_session)])
async def rclone_test(payload: RcloneTestRequest):
    remote = payload.remote_path.strip()
    if not remote or ":" not in remote:
        raise HTTPException(status_code=422, detail={"code": "invalid_rclone_remote", "message": "Select a configured rclone remote path."})
    code, output = await _run_rclone("lsd", remote, "--max-depth", "1")
    if code != 0:
        raise HTTPException(status_code=422, detail={"code": "rclone_test_failed", "message": "The rclone remote could not be reached."})
    return {"valid": True, "remotePath": remote}


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
        if not payload.tmdb_token.strip():
            raise HTTPException(status_code=422, detail={"code": "tmdb_required", "message": "A validated TMDB token is required."})
        await validate_tmdb(TMDBValidationRequest(token=payload.tmdb_token))
        hevc_mode = payload.hevc_compression_mode.lower()
        if hevc_mode not in {"auto", "always", "never"}:
            raise HTTPException(status_code=422, detail={"code": "invalid_hevc_mode", "message": "Choose auto, always, or never."})
        storage = payload.storage_engine.upper()
        if storage not in {"LOCAL", "CLOUD"}:
            raise HTTPException(status_code=422, detail={"code": "invalid_storage_engine", "message": "Choose local or cloud storage."})
        if storage == "CLOUD" and not payload.rclone_remote_path:
            raise HTTPException(status_code=422, detail={"code": "rclone_required", "message": "Cloud storage requires a tested rclone remote."})
        if storage == "CLOUD":
            remote_code, remote_output = await _run_rclone("lsd", payload.rclone_remote_path or "", "--max-depth", "1")
            if remote_code != 0:
                raise HTTPException(status_code=422, detail={"code": "rclone_test_failed", "message": "The rclone remote could not be reached."})
        if payload.totp_secret and (not payload.totp_code or not pyotp.TOTP(payload.totp_secret).verify(payload.totp_code, valid_window=1)):
            raise HTTPException(status_code=422, detail={"code": "invalid_totp", "message": "Verify TOTP again before completing setup."})

        existing = (await db.execute(select(User).where(func.lower(User.email) == email))).scalars().first()
        if not existing:
            existing = (await db.execute(select(User).order_by(User.id).limit(1))).scalars().first()
        user = existing or User(email=email, password_hash="")
        user.email = email
        user.password_hash = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode()
        user.totp_secret = payload.totp_secret or None
        user.two_factor_enabled = bool(payload.totp_secret)
        user.failed_login_attempts = 0
        user.lockout_until = None
        db.add(user)
        await db.flush()
        await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
        recovery_codes = _make_recovery_codes(user.id, db) if user.two_factor_enabled else []
        await db.execute(delete(AuthChallenge).where(AuthChallenge.user_id == user.id))

        ingestion_token = secrets.token_urlsafe(36)
        _atomic_update_env(settings.SERVER_ENV_PATH, {
            "TMDB_READ_ACCESS_TOKEN": payload.tmdb_token.strip(),
            "API_BEARER_TOKEN": ingestion_token,
            "STORAGE_ENGINE": storage,
            "RCLONE_REMOTE_PATH": payload.rclone_remote_path or settings.RCLONE_REMOTE_PATH,
            "BACKUP_ENABLED": str(payload.backup_enabled).lower(),
            "AUTO_UPDATE_ENABLED": str(payload.auto_update_enabled).lower(),
            "HEVC_COMPRESSION_MODE": hevc_mode,
        })
        try:
            await db.commit()
            _atomic_update_env(settings.ROOT_ENV_PATH, {"WEB_PORT": str(payload.web_port), "SETUP": "true"})
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=500, detail={"code": "setup_save_failed", "message": "Setup could not be saved. No installation flag was activated."})

        settings.SETUP_COMPLETE = True
        settings.WEB_PORT = payload.web_port
        settings.TMDB_READ_ACCESS_TOKEN = payload.tmdb_token.strip()
        settings.API_BEARER_TOKEN = ingestion_token
        settings.STORAGE_ENGINE = storage
        settings.RCLONE_REMOTE_PATH = payload.rclone_remote_path or settings.RCLONE_REMOTE_PATH
        settings.BACKUP_ENABLED = payload.backup_enabled
        settings.AUTO_UPDATE_ENABLED = payload.auto_update_enabled
        settings.HEVC_COMPRESSION_MODE = hevc_mode
        settings.save_to_json()
        response.delete_cookie(SETUP_COOKIE, path="/api/setup")
        asyncio.create_task(_restart_after_response())
        return {
            "complete": True,
            "restartScheduled": os.name != "nt",
            "webPort": payload.web_port,
            "recoveryCodes": recovery_codes,
            "ingestionToken": ingestion_token,
        }
