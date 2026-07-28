import configparser
import hashlib
import json
import tempfile
import time
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.responses import Response
from starlette.requests import Request

from routes.setup import (
    TMDBValidationRequest,
    UnlockRequest,
    _drive_callback_landing_url,
    _drive_callback_url,
    _normalize_public_url,
    _restore_file_snapshot,
    _safe_drive_path,
    _setup_token,
    _setup_status_urls,
    _stage_env_update,
    _tmdb_validation_is_current,
    _tmdb_validation_token,
    drive_oauth_callback,
    unlock_setup,
    validate_tmdb,
)
from services.rclone import RcloneConfigEncryptionError, RcloneResult, RcloneService


class DriveSetupContractTests(unittest.TestCase):
    def test_public_url_and_callback_contract(self):
        self.assertEqual(_normalize_public_url("https://watch.example.com/"), "https://watch.example.com")
        self.assertEqual(_normalize_public_url("http://localhost:3000"), "http://localhost:3000")
        self.assertEqual(_normalize_public_url("http://192.168.1.25:3000"), "http://192.168.1.25:3000")
        self.assertEqual(_normalize_public_url("http://10.20.30.40:3000"), "http://10.20.30.40:3000")
        self.assertEqual(_normalize_public_url("http://172.20.0.5:3000"), "http://172.20.0.5:3000")
        self.assertEqual(_normalize_public_url("http://[fd12:3456::20]:3000"), "http://[fd12:3456::20]:3000")
        self.assertEqual(
            _drive_callback_url("https://watch.example.com"),
            "https://watch.example.com/api/setup/rclone/drive/callback",
        )
        for insecure_public_url in ("http://watch.example.com", "http://8.8.8.8:3000"):
            with self.assertRaises(HTTPException):
                _normalize_public_url(insecure_public_url)
        with self.assertRaises(HTTPException):
            _normalize_public_url("https://watch.example.com/setup")

    def test_status_urls_do_not_apply_final_public_https_policy(self):
        self.assertEqual(
            _setup_status_urls("http://8.8.8.8:3000"),
            ("http://8.8.8.8:3000", "http://8.8.8.8:3000/api/setup/rclone/drive/callback"),
        )
        self.assertEqual(_setup_status_urls("not a URL"), ("", ""))

    def test_manual_callback_returns_to_the_browser_facing_setup(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("watch.example.com", 443),
            "path": "/api/setup/rclone/drive/callback",
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.10", 43000),
        })
        self.assertEqual(
            _drive_callback_landing_url(request),
            "https://watch.example.com/setup?drive=callback",
        )

    def test_drive_paths_reject_remote_and_parent_syntax(self):
        self.assertEqual(_safe_drive_path("/StreamHome/Media/"), "StreamHome/Media")
        for invalid in ("../Media", "remote:Media", "Media//Movies"):
            with self.assertRaises(HTTPException):
                _safe_drive_path(invalid, allow_empty=False)

    def test_every_command_uses_the_application_owned_config(self):
        service = RcloneService()
        with tempfile.TemporaryDirectory() as directory:
            service.config_path = Path(directory) / "rclone.conf"
            with patch.object(service, "executable", return_value="rclone"):
                command = service.command("about", "streamhome-drive:")
        self.assertEqual(command[:3], ["rclone", "--config", str(service.config_path.resolve())])

    def test_linux_password_command_reads_the_environment_without_shell_quoting(self):
        service = RcloneService()
        linux_reader = service.password_reader("posix")
        with patch.object(service, "executable", return_value="rclone"), patch.object(
            service,
            "password_reader",
            return_value=linux_reader,
        ) as password_reader:
            command = service.command("config", "encryption", "set", password_command=True)
        password_reader.assert_called_once_with()
        self.assertEqual(
            command,
            [
                "rclone",
                "--config",
                str(service.config_path.resolve()),
                "--password-command",
                "printenv RCLONE_CONFIG_PASS",
                "config",
                "encryption",
                "set",
            ],
        )
        self.assertNotIn("sh -c", linux_reader)
        self.assertNotIn("'", linux_reader)

    def test_drive_config_contains_only_the_selected_remote(self):
        service = RcloneService()
        with tempfile.TemporaryDirectory() as directory:
            service.setup_root = Path(directory)
            job_id = "00000000-0000-0000-0000-000000000000"
            config_path = service.write_drive_config(job_id, "streamhome-drive", {
                "client_id": "client.apps.googleusercontent.com",
                "client_secret": "secret",
                "token": {"access_token": "access", "refresh_token": "refresh", "token_type": "Bearer", "expiry": "2099-01-01T00:00:00Z"},
            })
            parser = configparser.RawConfigParser()
            parser.read(config_path, encoding="utf-8")
            self.assertEqual(parser.sections(), ["streamhome-drive"])
            self.assertEqual(parser.get("streamhome-drive", "type"), "drive")
            self.assertEqual(json.loads(parser.get("streamhome-drive", "token"))["refresh_token"], "refresh")

    def test_rclone_failures_are_typed(self):
        self.assertEqual(RcloneService.classify(1, "oauth2: invalid_grant"), "drive_unauthorized")
        self.assertEqual(RcloneService.classify(1, "storageQuotaExceeded"), "drive_quota_exceeded")
        self.assertEqual(RcloneService.classify(1, "user rate limit exceeded"), "drive_rate_limited")

    def test_rclone_encryption_requires_version_1_68(self):
        service = RcloneService()
        with patch.object(service, "executable", return_value="rclone"), patch(
            "services.rclone.subprocess.run",
            return_value=SimpleNamespace(stdout="rclone v1.67.0\n"),
        ):
            self.assertFalse(service.encryption_supported())
        with patch.object(service, "executable", return_value="rclone"), patch(
            "services.rclone.subprocess.run",
            return_value=SimpleNamespace(stdout="rclone v1.68.0\n"),
        ):
            self.assertTrue(service.encryption_supported())

    def test_tmdb_validation_receipt_is_bound_to_setup_session_and_token(self):
        session_id = "setup-session"
        setup_cookie = _setup_token(session_id)
        request = Request({
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("watch.example.com", 443),
            "path": "/api/setup/complete",
            "query_string": b"",
            "headers": [(b"cookie", f"streamhome_setup={setup_cookie}".encode("ascii"))],
            "client": ("203.0.113.10", 43000),
        })
        receipt = _tmdb_validation_token(session_id, "tmdb-token")

        self.assertTrue(_tmdb_validation_is_current(request, "tmdb-token", receipt))
        self.assertFalse(_tmdb_validation_is_current(request, "different-token", receipt))

    def test_staged_environment_update_can_be_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".env"
            target.write_text('SETUP="false"\nKEEP="value"\n', encoding="utf-8")
            snapshot = target.read_bytes()
            staged_target, temporary = _stage_env_update(str(target), {"SETUP": "true", "WEB_PORT": "3001"})

            self.assertEqual(staged_target, target)
            self.assertEqual(target.read_bytes(), snapshot)
            os.replace(temporary, target)
            self.assertIn('SETUP="true"', target.read_text(encoding="utf-8"))
            _restore_file_snapshot(target, snapshot)
            self.assertEqual(target.read_bytes(), snapshot)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class _DriveCallbackDatabase:
    def __init__(self, job):
        self.job = job
        self.commits = 0

    async def execute(self, _statement):
        return _ScalarResult(self.job)

    def add(self, _value):
        return None

    async def commit(self):
        self.commits += 1


class _UnlockDatabase:
    def __init__(self, job):
        self.job = job
        self.commits = 0

    async def get(self, _model, _identifier):
        return self.job

    def add(self, _value):
        return None

    async def commit(self):
        self.commits += 1


class DriveCallbackFlowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def callback_request(state: str) -> Request:
        return Request({
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("watch.example.com", 443),
            "path": "/api/setup/rclone/drive/callback",
            "query_string": f"state={state}".encode("ascii"),
            "headers": [],
            "client": ("203.0.113.10", 43000),
        })

    async def test_valid_single_use_state_does_not_require_the_setup_cookie(self):
        state = "oauth-state-with-256-bits-of-randomness"
        job = SimpleNamespace(
            id="drive-job-id",
            state_hash=hashlib.sha256(state.encode("utf-8")).hexdigest(),
            status="authorizing",
            expires_at=time.time() + 300,
            public_url="https://watch.example.com",
            error_code=None,
            progress="Waiting for Google",
            updated_at=time.time(),
        )
        database = _DriveCallbackDatabase(job)

        with patch("routes.setup.rclone_service.cleanup_job"):
            response = await drive_oauth_callback(self.callback_request(state), database)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(job.error_code, "drive_authorization_failed")
        self.assertEqual(database.commits, 1)

    async def test_completed_callback_replay_is_idempotent(self):
        state = "completed-oauth-state"
        job = SimpleNamespace(
            id="drive-job-id",
            state_hash=hashlib.sha256(state.encode("utf-8")).hexdigest(),
            status="selecting_folder",
            expires_at=time.time() + 300,
            public_url="https://watch.example.com",
            error_code=None,
            progress="Google Drive connected",
            updated_at=time.time(),
        )
        database = _DriveCallbackDatabase(job)

        response = await drive_oauth_callback(self.callback_request(state), database)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "https://watch.example.com/setup?driveJob=drive-job-id&drive=connected",
        )
        self.assertEqual(job.status, "selecting_folder")
        self.assertEqual(database.commits, 0)


class TMDBValidationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_validation_returns_a_current_session_bound_receipt(self):
        session_id = "setup-session"
        setup_cookie = _setup_token(session_id)
        request = Request({
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("watch.example.com", 443),
            "path": "/api/setup/tmdb/validate",
            "query_string": b"",
            "headers": [(b"cookie", f"streamhome_setup={setup_cookie}".encode("ascii"))],
            "client": ("203.0.113.10", 43000),
        })
        response = SimpleNamespace(status_code=200)

        with patch("routes.setup.httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
            result = await validate_tmdb(TMDBValidationRequest(token="tmdb-token"), request)

        self.assertTrue(result["valid"])
        self.assertTrue(_tmdb_validation_is_current(request, "tmdb-token", result["validationToken"]))


class SetupUnlockResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_correct_bootstrap_code_rebinds_an_active_drive_job(self):
        old_hash = hashlib.sha256(b"old-session").hexdigest()
        job = SimpleNamespace(
            id="drive-job-id",
            session_hash=old_hash,
            status="ready",
            expires_at=time.time() + 600,
            updated_at=time.time(),
        )
        database = _UnlockDatabase(job)
        response = Response()
        request = Request({
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("watch.example.com", 443),
            "path": "/api/setup/unlock",
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.10", 43000),
        })

        with patch("routes.setup.setup_required", return_value=True), patch(
            "routes.setup._bootstrap_code",
            return_value="bootstrap",
        ), patch(
            "routes.setup.enforce_rate_limit",
            new=AsyncMock(),
        ), patch("routes.setup.clear_rate_limit", new=AsyncMock()):
            await unlock_setup(
                UnlockRequest(code="bootstrap", drive_job_id=job.id),
                request,
                response,
                database,
            )

        self.assertNotEqual(job.session_hash, old_hash)
        self.assertEqual(database.commits, 1)
        self.assertIn("streamhome_setup=", response.headers["set-cookie"])


class RcloneActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_encryption_failure_never_installs_plaintext_config(self):
        service = RcloneService()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service.config_dir = root / "active"
            service.config_path = service.config_dir / "rclone.conf"
            service.config_dir.mkdir()
            source = root / "source.conf"
            source.write_text("[drive]\ntype = drive\ntoken = secret\n", encoding="utf-8")
            with patch.object(service, "encryption_supported", return_value=True), patch.object(
                service,
                "run",
                new=AsyncMock(return_value=RcloneResult(1, stderr="unsupported command")),
            ):
                with self.assertRaises(RcloneConfigEncryptionError):
                    await service.activate_remote(source, "drive")
            self.assertFalse(service.config_path.exists())
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
