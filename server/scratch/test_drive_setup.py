import configparser
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from routes.setup import (
    _drive_callback_landing_url,
    _drive_callback_url,
    _normalize_public_url,
    _safe_drive_path,
    _setup_status_urls,
    drive_oauth_callback,
)
from services.rclone import RcloneService


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


if __name__ == "__main__":
    unittest.main()
