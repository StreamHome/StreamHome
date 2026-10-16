"""Session-owned, first Google Drive connection from the Admin Center."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from weakref import WeakValueDictionary
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import SETTINGS_WRITE_LOCK, settings
from db import get_session
from lifecycle.coordination import read_process_start_identity, run_cancellation_safe_thread
from lifecycle.drive_connection import (
    DriveConnectionPublicationError,
    get_drive_connection_receipt,
    publish_drive_connection,
)
from models import AdminDriveJob, AuthSession
from routes.auth import require_recent_reauth
from routes.setup import (
    DRIVE_SCOPE, GOOGLE_AUTHORIZE_URL, GOOGLE_DRIVE_GUIDE_URL, GOOGLE_TOKEN_URL,
    _drive_callback_url, _hash_value, _normalize_public_url, _safe_drive_path, _setup_status_urls,
)
from services.rclone import RcloneConfigEncryptionError, rclone_service

router = APIRouter(prefix="/api/system/drive", tags=["Admin Drive"])
_PROCESS_OWNER = f"{os.getpid()}:{read_process_start_identity(os.getpid()) or 'unknown'}:{uuid.uuid4().hex}"
_REMOTE_NAME = "streamhome-drive"
_TERMINAL = {"saved", "cancelled", "failed", "expired"}
_FOLDER_STATES = {"selecting_folder", "ready"}
_LIST_BYTES = 65536
_LIST_ITEMS = 200
_ACTIVE_OPERATIONS: WeakValueDictionary = WeakValueDictionary()


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str
    client_secret: str
    audience: str = "external"
    publishing_status: str = "production"


class FolderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


def _error(code: str, message: str, status: int = 409) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _configured() -> bool:
    # A present configuration is protected even if its contents are invalid.
    return rclone_service.config_path.exists() or rclone_service.config_path.is_symlink()


def _first_connection() -> None:
    if _configured():
        raise _error("drive_already_configured", "Google Drive is already configured. Replacing a connection is not supported.")


def _response(job: AdminDriveJob) -> dict[str, Any]:
    return {
        "id": job.id, "status": job.status, "remoteName": job.remote_name,
        "selectedPath": job.selected_path or "", "progress": job.progress,
        "errorCode": job.error_code, "audience": job.audience,
        "publishingStatus": job.publishing_status, "expiresAt": job.expires_at,
        "libraryState": job.library_state,
        "pendingOperation": job.operation, "generation": job.generation,
        "pendingFolderPath": job.pending_folder_path,
    }


def _owner_alive(owner: str | None) -> bool:
    if owner == _PROCESS_OWNER:
        return True
    try:
        pid, started, _nonce = (owner or "").split(":", 2)
        current = read_process_start_identity(int(pid))
        return bool(current and current == started)
    except (ValueError, OSError):
        return False


def _operation_running(job: AdminDriveJob) -> bool:
    if job.operation_owner != _PROCESS_OWNER:
        return _owner_alive(job.operation_owner)
    task = _ACTIVE_OPERATIONS.get((job.id, job.generation))
    return bool(task is not None and not task.done())


async def _finish(db: AsyncSession, job: AdminDriveJob, **values: Any) -> AdminDriveJob:
    operation, generation = job.operation, job.generation
    values.setdefault("operation", None)
    values.setdefault("operation_owner", None)
    try:
        result = await db.execute(update(AdminDriveJob).where(
            AdminDriveJob.id == job.id,
            AdminDriveJob.generation == generation,
            AdminDriveJob.operation == operation,
            AdminDriveJob.operation_owner == job.operation_owner,
        ).values(updated_at=time.time(), **values))
        await db.commit()
    finally:
        _ACTIVE_OPERATIONS.pop((job.id, generation), None)
    if result.rowcount != 1:
        await db.refresh(job)
        if values.get("status") == "saved" and job.status == "saved":
            return job
        raise _error("drive_job_changed", "The Drive connection changed. Refresh its status.")
    await db.refresh(job)
    return job


async def _claim(db: AsyncSession, job: AdminDriveJob, operation: str, allowed: set[str], *, allow_uncertain: bool = False, **values: Any) -> AdminDriveJob:
    allowed_operation = job.operation is None or (allow_uncertain and job.operation == "mkdir_uncertain")
    if not allowed_operation or job.status not in allowed:
        raise _error("drive_job_busy" if job.operation else "drive_job_phase", "Refresh the Drive connection before continuing.")
    key = (job.id, job.generation + 1)
    task = asyncio.current_task()
    try:
        result = await db.execute(update(AdminDriveJob).where(
            AdminDriveJob.id == job.id, AdminDriveJob.generation == job.generation,
            AdminDriveJob.operation == job.operation, AdminDriveJob.status.in_(allowed),
        ).values(operation=operation, operation_owner=_PROCESS_OWNER,
                 generation=AdminDriveJob.generation + 1, updated_at=time.time(), **values))
        if result.rowcount != 1:
            await db.rollback()
            raise _error("drive_job_busy", "Another Drive operation is in progress. Refresh its status.")
        # The winning uncommitted CAS is still invisible to other readers. Publish
        # its live task before committing the phase, without touching a winner's
        # ownership when a competing CAS loses.
        _ACTIVE_OPERATIONS[key] = task
        await db.commit()
    except BaseException:
        if _ACTIVE_OPERATIONS.get(key) is task:
            _ACTIVE_OPERATIONS.pop(key, None)
        raise
    await db.refresh(job)
    return job


async def _receipt(job: AdminDriveJob) -> dict | None:
    try:
        return await run_cancellation_safe_thread(
            get_drive_connection_receipt, Path(settings.BASE_DIR), job.id, job.generation,
        )
    except DriveConnectionPublicationError as exc:
        raise _error(exc.code, "Drive publication requires server recovery before continuing.", 503) from exc


async def _reconcile(db: AsyncSession, job: AdminDriveJob) -> AdminDriveJob:
    if job.status in {"saving", "saved"}:
        receipt = await _receipt(job)
        if receipt:
            if job.status != "saved":
                await _finish(db, job, status="saved", active_session_key=None,
                              progress="Google Drive connection saved. Storage mode is unchanged.", error_code=None)
            with SETTINGS_WRITE_LOCK:
                settings.RCLONE_REMOTE_PATH = receipt["remotePath"]
                settings.GOOGLE_DRIVE_AUDIENCE = job.audience
                settings.GOOGLE_DRIVE_PUBLISHING_STATUS = job.publishing_status
            rclone_service.cleanup_job(job.id)
            return job
        if job.status == "saved":
            raise _error("drive_publication_unverified", "The saved Drive connection could not be verified.", 503)
    if job.operation in {"mkdir", "mkdir_uncertain"} and job.pending_folder_path and not _operation_running(job):
        if job.operation != "mkdir_uncertain":
            await _finish(db, job, operation="mkdir_uncertain", error_code="drive_folder_create_uncertain",
                          progress="Drive folder creation is unconfirmed. Check status or cancel; the folder will be preserved.")
        parent, _, name = job.pending_folder_path.rpartition("/")
        try:
            items = await _listing(job, parent)
        except HTTPException:
            return job
        if any(item.get("Name") == name and item.get("IsDir") is True for item in items):
            await _finish(db, job, pending_folder_path=None, error_code=None,
                          progress="The requested Drive folder is present. Select a media folder to continue.")
        return job
    if job.operation and not _operation_running(job):
        # A crash never turns a partially executed operation into success.
        await _finish(db, job, status="selecting_folder" if job.health_name else "failed",
                      active_session_key=job.auth_session_id if job.health_name else None,
                      expires_at=time.time() + 1800 if job.health_name else job.expires_at,
                      error_code="drive_operation_interrupted",
                      progress="The server restarted during this operation. Retry the test or cancel to remove its temporary file." if job.health_name else "The server restarted during this operation. Start the connection again.")
    if job.status not in _TERMINAL and job.expires_at <= time.time() and not job.operation:
        await _claim(db, job, "expire", {job.status})
        await _finish(db, job, status="expired", active_session_key=job.auth_session_id if job.health_name else None,
                      error_code="drive_job_expired", progress="The Drive connection expired. Start again.")
        if not job.health_name:
            rclone_service.cleanup_job(job.id)
    return job


async def _job(db: AsyncSession, session: AuthSession, job_id: str) -> AdminDriveJob:
    job = await db.get(AdminDriveJob, job_id)
    if not job or not hmac.compare_digest(job.auth_session_id, session.id):
        raise _error("drive_job_not_found", "This Drive connection was not found.", 404)
    return await _reconcile(db, job)


@router.get("/setup")
async def get_setup(session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    job = (await db.exec(select(AdminDriveJob).where(
        AdminDriveJob.auth_session_id == session.id,
    ).order_by(AdminDriveJob.active_session_key.is_not(None).desc(), AdminDriveJob.created_at.desc()).limit(1))).first()
    if job:
        job = await _reconcile(db, job)
    public_url, callback_url = _setup_status_urls(settings.PUBLIC_URL)
    return {"configured": _configured(), "publicUrl": public_url, "callbackUrl": callback_url,
            "guideUrl": GOOGLE_DRIVE_GUIDE_URL, "job": _response(job) if job else None}


@router.post("/jobs")
async def start_job(payload: StartRequest, session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    _first_connection()
    if not await asyncio.to_thread(rclone_service.encryption_supported):
        raise _error("rclone_upgrade_required", "Google Drive requires Rclone 1.68 or newer.", 422)
    client_id, client_secret = payload.client_id.strip(), payload.client_secret.strip()
    audience, publishing_status = payload.audience.strip().lower(), payload.publishing_status.strip().lower()
    public_url = _normalize_public_url(settings.PUBLIC_URL)
    if not client_id or len(client_id) > 300 or not client_id.endswith(".apps.googleusercontent.com") or any(ord(c) < 32 for c in client_id):
        raise _error("invalid_google_client_id", "Enter the Web application client ID from Google Cloud.", 422)
    if not client_secret or len(client_secret) > 500 or any(ord(c) < 32 for c in client_secret):
        raise _error("invalid_google_client_secret", "Enter the Google OAuth client secret.", 422)
    if audience not in {"external", "internal"} or publishing_status not in {"testing", "production"}:
        raise _error("invalid_google_app_status", "Choose the Google OAuth audience and publishing status.", 422)
    existing = (await db.exec(select(AdminDriveJob).where(AdminDriveJob.active_session_key == session.id))).first()
    if existing:
        await _reconcile(db, existing)
        if existing.active_session_key:
            raise _error("drive_job_active", "A Drive connection is already in progress. Refresh its status.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    timestamp = time.time()
    job = AdminDriveJob(id=str(uuid.uuid4()), auth_session_id=session.id, active_session_key=session.id,
        state_hash=_hash_value(state), status="authorizing", public_url=public_url,
        audience=audience, publishing_status=publishing_status, created_at=timestamp,
        updated_at=timestamp, expires_at=timestamp + 600)
    # Stage protected credentials before publishing the discoverable job row.
    rclone_service.write_job_secret(job.id, {"client_id": client_id, "client_secret": client_secret, "pkce_verifier": verifier})
    try:
        db.add(job)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        rclone_service.cleanup_job(job.id)
        raise _error("drive_job_active", "A Drive connection is already in progress. Refresh its status.") from exc
    except BaseException:
        # A cancelled commit is ambiguous: retain credentials for reconciliation.
        raise
    authorization_url = f"{GOOGLE_AUTHORIZE_URL}?{urlencode({'client_id': client_id, 'redirect_uri': _drive_callback_url(public_url), 'response_type': 'code', 'scope': DRIVE_SCOPE, 'access_type': 'offline', 'prompt': 'consent', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}"
    return {"jobId": job.id, "authorizationUrl": authorization_url, "expiresAt": job.expires_at}


async def oauth_callback(request: Request, db: AsyncSession, state_hash: str) -> RedirectResponse | None:
    job = (await db.execute(select(AdminDriveJob).where(AdminDriveJob.state_hash == state_hash))).scalars().first()
    if not isinstance(job, AdminDriveJob):
        return None
    landing = f"{job.public_url}/admin/drive/callback"
    session = await db.get(AuthSession, job.auth_session_id)
    now = time.time()
    valid_session = bool(session and not session.revoked_at and session.expires_at > now
                         and session.reauthenticated_at and now - session.reauthenticated_at <= settings.REAUTHENTICATION_MINUTES * 60)
    if not valid_session:
        return RedirectResponse(f"{landing}?drive=error", status_code=303)
    job = await _reconcile(db, job)
    if job.status in _FOLDER_STATES | {"saving", "saved"}:
        return RedirectResponse(f"{landing}?drive=connected", status_code=303)
    if job.status != "authorizing" or job.operation:
        return RedirectResponse(f"{landing}?drive=error", status_code=303)
    if _configured():
        return RedirectResponse(f"{landing}?drive=error", status_code=303)
    try:
        await _claim(db, job, "oauth", {"authorizing"}, status="exchanging_code", progress="Securing Google Drive authorization")
    except HTTPException:
        return RedirectResponse(f"{landing}?drive=error", status_code=303)
    try:
        oauth_error, code = request.query_params.get("error"), request.query_params.get("code")
        if oauth_error or not code:
            raise _error("drive_authorization_denied" if oauth_error == "access_denied" else "drive_authorization_failed", "Google Drive authorization was not completed.", 422)
        secret = rclone_service.read_job_secret(job.id)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id": secret["client_id"], "client_secret": secret["client_secret"], "code": code,
                "code_verifier": secret["pkce_verifier"], "grant_type": "authorization_code",
                "redirect_uri": _drive_callback_url(job.public_url),
            })
        data = response.json()
        if response.status_code != 200 or not isinstance(data, dict):
            raise _error("drive_token_exchange_failed", "Google did not accept the OAuth callback.", 422)
        if not isinstance(data.get("refresh_token"), str) or not isinstance(data.get("access_token"), str) or not data["refresh_token"] or not data["access_token"]:
            raise _error("drive_refresh_token_missing", "Google did not return offline access. Reconnect and approve access again.", 422)
        from datetime import datetime, timezone
        secret["token"] = {"access_token": data["access_token"], "refresh_token": data["refresh_token"], "token_type": "Bearer", "expiry": datetime.fromtimestamp(time.time() + max(60, min(86400, int(data.get("expires_in", 3600)))), timezone.utc).isoformat().replace("+00:00", "Z")}
        rclone_service.write_job_secret(job.id, secret)
        config = rclone_service.write_drive_config(job.id, job.remote_name, secret)
        check = await _run(config, "about", f"{job.remote_name}:", "--json")
        if not check.ok:
            raise _error("drive_configuration_invalid", "Rclone could not use the Google authorization.", 422)
        await _finish(db, job, status="selecting_folder", progress="Google Drive connected. Select a media folder.", error_code=None, expires_at=time.time() + 1800)
        return RedirectResponse(f"{landing}?drive=connected", status_code=303)
    except asyncio.CancelledError:
        await _finish(db, job, status="failed", active_session_key=None, error_code="drive_authorization_interrupted", progress="Drive authorization was interrupted. Start again.")
        rclone_service.cleanup_job(job.id)
        raise
    except (HTTPException, httpx.HTTPError, ValueError, KeyError, OSError) as exc:
        code = exc.detail.get("code", "drive_authorization_failed") if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else "drive_authorization_failed"
        await _finish(db, job, status="failed", active_session_key=None, error_code=code, progress="Google Drive authorization could not be completed.")
        rclone_service.cleanup_job(job.id)
        return RedirectResponse(f"{landing}?drive=error", status_code=303)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    return _response(await _job(db, session, job_id))


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    job = await _job(db, session, job_id)
    if job.status == "saved":
        raise _error("drive_already_saved", "This Drive connection has already been saved.")
    if job.status == "cancelled":
        return _response(job)
    await _claim(db, job, "cancel", {job.status}, allow_uncertain=True)
    if job.health_name:
        if not await _delete_health(job):
            await _finish(db, job, error_code="drive_test_cleanup_failed", progress="The temporary test file could not be removed. Retry cancellation.")
            raise _error("drive_test_cleanup_failed", "The temporary Drive test file could not be removed.", 422)
    await _finish(db, job, status="cancelled", active_session_key=None, health_name=None, pending_folder_path=None,
                  error_code="drive_job_cancelled", progress="Google Drive connection was cancelled.")
    rclone_service.cleanup_job(job.id)
    return _response(job)


async def _run(config: Path, *arguments: str, input_data: bytes | None = None, limit: int = _LIST_BYTES):
    return await rclone_service.run(*arguments, config_path=config, timeout=30, input_data=input_data,
                                    output_limit=limit, hard_output_limit=limit)


def _config(job: AdminDriveJob) -> Path:
    return rclone_service.job_dir(job.id) / "rclone.conf"


def _items(output: str) -> list[dict]:
    try:
        items = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise _error("invalid_rclone_response", "Drive returned an invalid folder listing.", 502) from exc
    if not isinstance(items, list) or len(items) > _LIST_ITEMS or any(not isinstance(item, dict) for item in items):
        raise _error("drive_folder_limit", "This folder is too large to inspect. Choose a smaller media folder.", 422)
    return items


async def _listing(job: AdminDriveJob, path: str) -> list[dict]:
    result = await _run(_config(job), "lsjson", f"{job.remote_name}:{path}", "--max-depth", "1")
    if not result.ok:
        raise _error("drive_folder_limit" if result.error_code == "rclone_output_limit" else "drive_folder_list_failed", "Google Drive folders could not be listed safely.", 422)
    return _items(result.stdout)


async def _library_state(job: AdminDriveJob, path: str, items: list[dict]) -> str:
    if not items:
        return "empty"
    # Recognition is evidence from a bounded sample, never a catalog import or count.
    categories = [i for i in items if i.get("IsDir") is True and i.get("Name") in {"Movies", "Series"}]
    for category in categories[:2]:
        category_path = f"{path}/{category['Name']}"
        children = await _listing(job, category_path)
        for child in [i for i in children if i.get("IsDir") is True][:2]:
            name = child.get("Name")
            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                continue
            target = _safe_drive_path(f"{category_path}/{name}/.metadata/metadata.json", allow_empty=False)
            result = await _run(_config(job), "cat", f"{job.remote_name}:{target}", "--head", "16385", limit=16384)
            if not result.ok:
                continue
            try:
                data = json.loads(result.stdout)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("media_type") in {"movie", "tv", "series"} and isinstance(data.get("tmdb_id"), int) and not isinstance(data.get("tmdb_id"), bool) and data["tmdb_id"] > 0 and isinstance(data.get("title"), str) and data.get("catalog_source") == "server":
                return "streamhome"
    return "nonempty"


@router.get("/jobs/{job_id}/folders")
async def list_folders(job_id: str, path: str = "", session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    job = await _job(db, session, job_id)
    _first_connection()
    normalized = _safe_drive_path(path)
    await _claim(db, job, "list", _FOLDER_STATES)
    try:
        items = await _listing(job, normalized)
        folders = []
        for item in items:
            name = item.get("Name")
            if item.get("IsDir") is not True or not isinstance(name, str) or not name or "/" in name or "\\" in name:
                continue
            child = _safe_drive_path("/".join(p for p in (normalized, name) if p), allow_empty=False)
            folders.append({"name": name, "path": child, "id": item.get("ID") if isinstance(item.get("ID"), str) else None})
        return {"path": normalized, "folders": folders}
    finally:
        await _finish(db, job)


@router.post("/jobs/{job_id}/folders")
async def create_folder(job_id: str, payload: FolderRequest, session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    job = await _job(db, session, job_id)
    _first_connection()
    path = _safe_drive_path(payload.path, allow_empty=False)
    await _claim(db, job, "mkdir", _FOLDER_STATES, status="selecting_folder", library_state="unknown", pending_folder_path=path)
    try:
        result = await _run(_config(job), "mkdir", f"{job.remote_name}:{path}")
        if not result.ok:
            raise _error("drive_folder_create_uncertain", "Drive folder creation is unconfirmed. Check status before continuing.", 503)
    except BaseException:
        await _finish(db, job, operation="mkdir_uncertain", error_code="drive_folder_create_uncertain",
                      progress="Drive folder creation is unconfirmed. Check status or cancel; the folder will be preserved.")
        raise
    await _finish(db, job, pending_folder_path=None)
    return {"path": path}


@router.post("/jobs/{job_id}/select-folder")
async def select_folder(job_id: str, payload: FolderRequest, session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    job = await _job(db, session, job_id)
    _first_connection()
    if job.health_name:
        raise _error("drive_test_cleanup_required", "Remove the previous temporary test file by retrying the test before selecting another folder.")
    path = _safe_drive_path(payload.path, allow_empty=False)
    await _claim(db, job, "select", _FOLDER_STATES)
    try:
        items = await _listing(job, path)
        library_state = await _library_state(job, path, items)
    except BaseException:
        await _finish(db, job)
        raise
    await _finish(db, job, selected_path=path, library_state=library_state, status="selecting_folder",
                  progress="Folder selected. Run the temporary read and write test.", error_code=None)
    return _response(job)


async def _delete_health(job: AdminDriveJob) -> bool:
    if not job.health_name or not job.selected_path:
        return True
    target = f"{job.remote_name}:{job.selected_path}/{job.health_name}"
    try:
        return await _delete_health_at(job, target)
    except (OSError, ValueError, HTTPException):
        return False


async def _delete_health_at(job: AdminDriveJob, target: str) -> bool:
    await _run(_config(job), "deletefile", target)
    # A lost deletion response can be reconciled only by observing absence.
    listing = await _run(_config(job), "lsjson", f"{job.remote_name}:{job.selected_path}", "--max-depth", "1", "--include", f"/{job.health_name}")
    if not listing.ok:
        return False
    try:
        return not _items(listing.stdout)
    except HTTPException:
        return False


@router.post("/jobs/{job_id}/test")
async def test_folder(job_id: str, session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    job = await _job(db, session, job_id)
    _first_connection()
    if not job.selected_path:
        raise _error("drive_folder_required", "Select a Google Drive folder first.", 422)
    await _claim(db, job, "test", _FOLDER_STATES, status="testing", error_code=None,
                 progress="Testing temporary file creation, content reading and deletion")
    root = f"{job.remote_name}:{job.selected_path}"
    failure: HTTPException | None = None
    cancelled: BaseException | None = None
    quota = None
    try:
        if job.health_name:
            if not await _delete_health(job):
                raise _error("drive_test_cleanup_failed", "The previous temporary test file could not be removed.", 422)
        job.health_name = f".streamhome-healthcheck-{secrets.token_hex(16)}"
        db.add(job)
        await db.commit()
        content = f"StreamHome Google Drive health check {secrets.token_hex(16)}\n".encode("ascii")
        upload = await _run(_config(job), "rcat", f"{root}/{job.health_name}", input_data=content)
        if not upload.ok:
            raise _error("drive_test_write_failed", "Drive could not create the temporary test file.", 422)
        verify = await _run(_config(job), "cat", f"{root}/{job.health_name}", "--head", "512", limit=512)
        if not verify.ok or verify.stdout.encode("utf-8") != content:
            raise _error("drive_test_content_failed", "Drive did not return the temporary file's exact content.", 422)
        about = await _run(_config(job), "about", f"{job.remote_name}:", "--json")
        if about.ok:
            try:
                parsed = json.loads(about.stdout)
                if isinstance(parsed, dict):
                    quota = {key: value for key, value in parsed.items() if key in {"total", "used", "free", "trashed", "other"} and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
            except ValueError:
                pass
    except HTTPException as exc:
        failure = exc
    except asyncio.CancelledError as exc:
        cancelled = exc
    except (OSError, ValueError):
        failure = _error("drive_test_failed", "The Drive access test could not be completed.", 422)
    # Join cleanup through cancellation so success can never precede verified deletion.
    cleanup_task = asyncio.create_task(_delete_health(job))
    while True:
        try:
            cleanup_ok = await asyncio.shield(cleanup_task)
            break
        except asyncio.CancelledError as exc:
            cancelled = exc
    if not cleanup_ok:
        failure = _error("drive_test_cleanup_failed", "The temporary Drive test file could not be removed. Retry the test or cancel.", 422)
    await _finish(db, job, status="selecting_folder" if failure or cancelled else "ready",
                  health_name=None if cleanup_ok else job.health_name,
                  error_code=failure.detail["code"] if failure else ("drive_test_interrupted" if cancelled else None),
                  progress="Drive access test failed. Retry after resolving the error." if failure or cancelled else "Google Drive access verified. Save the connection to finish.",
                  expires_at=time.time() + 1800)
    if cancelled:
        raise cancelled
    if failure:
        raise failure
    return {"valid": True, "remotePath": root, "quota": quota, "job": _response(job)}


@router.post("/jobs/{job_id}/save")
async def save_job(job_id: str, session: AuthSession = Depends(require_recent_reauth), db: AsyncSession = Depends(get_session)):
    job = await _job(db, session, job_id)
    if job.status == "saved":
        return {"valid": True, "remotePath": f"{job.remote_name}:{job.selected_path}", "job": _response(job)}
    _first_connection()
    if job.health_name or not job.selected_path:
        raise _error("drive_not_ready", "Complete the temporary Drive access test before saving.")
    await _claim(db, job, "save", {"ready"}, status="saving", progress="Saving the protected Drive connection")
    remote_path = f"{job.remote_name}:{job.selected_path}"
    candidate = None
    try:
        async with rclone_service._config_lock:
            _first_connection()
            rclone_service.config_dir.mkdir(parents=True, exist_ok=True)
            candidate = await rclone_service._encrypted_candidate(_config(job))
            await run_cancellation_safe_thread(_publish,
                root=Path(settings.BASE_DIR), job_id=job.id, generation=job.generation,
                encrypted_config=candidate, remote_name=job.remote_name, remote_path=remote_path,
                audience=job.audience, publishing_status=job.publishing_status)
    except asyncio.CancelledError:
        # Publication and DB acknowledgement have different commit boundaries.
        # Keep saving/generation intact: GET reconciles the durable receipt.
        if await _receipt(job):
            await _reconcile(db, job)
        else:
            await _finish(db, job, status="ready", error_code="drive_save_interrupted", progress="Drive saving was interrupted. Check its status before retrying.")
        raise
    except (DriveConnectionPublicationError, RcloneConfigEncryptionError, OSError, HTTPException) as exc:
        if await _receipt(job):
            await _reconcile(db, job)
        else:
            code = exc.code if isinstance(exc, DriveConnectionPublicationError) else "drive_save_failed"
            await _finish(db, job, status="ready", error_code=code, progress="Drive connection could not be saved. Check its status before retrying.")
            raise _error(code, "The Drive connection could not be saved safely.", 422) from exc
    finally:
        if candidate:
            candidate.unlink(missing_ok=True)
    if job.status != "saved":
        await db.refresh(job)
    if job.status != "saved":
        await _finish(db, job, status="saved", active_session_key=None, error_code=None,
                      progress="Google Drive connection saved. Storage mode is unchanged.")
    rclone_service.cleanup_job(job.id)
    return {"valid": True, "remotePath": remote_path, "job": _response(job)}


def _publish(**kwargs) -> dict:
    with SETTINGS_WRITE_LOCK:
        receipt = publish_drive_connection(**kwargs)
        settings.RCLONE_REMOTE_PATH = receipt["remotePath"]
        settings.GOOGLE_DRIVE_AUDIENCE = kwargs["audience"]
        settings.GOOGLE_DRIVE_PUBLISHING_STATUS = kwargs["publishing_status"]
        return receipt


async def recover_jobs(db: AsyncSession) -> None:
    for job in (await db.exec(select(AdminDriveJob).where(AdminDriveJob.status.notin_(list(_TERMINAL))))).all():
        await _reconcile(db, job)
