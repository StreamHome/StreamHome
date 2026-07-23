from __future__ import annotations

import asyncio
import configparser
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from config import settings
from services.logger import logger
from services.secret_crypto import protect_secret, reveal_secret


REMOTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MINIMUM_ENCRYPTION_VERSION = (1, 68)


class RcloneConfigEncryptionError(RuntimeError):
    pass


@dataclass(slots=True)
class RcloneResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    error_code: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class RcloneService:
    """Single, application-owned gateway for every rclone subprocess."""

    def __init__(self) -> None:
        self.root = Path(settings.BASE_DIR)
        self.config_dir = self.root / "server" / "rclone"
        self.config_path = self.config_dir / "rclone.conf"
        self.setup_root = self.root / "server" / "temp" / "rclone-setup"
        self._semaphore = asyncio.Semaphore(4)
        self._config_lock = asyncio.Lock()

    def executable(self) -> Optional[str]:
        candidate = self.root / "bin" / ("rclone.exe" if os.name == "nt" else "rclone")
        if candidate.exists():
            return str(candidate)
        return shutil.which("rclone")

    def version(self) -> Optional[tuple[int, int, int]]:
        executable = self.executable()
        if not executable:
            return None
        try:
            result = subprocess.run(
                [executable, "version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"rclone v(\d+)\.(\d+)(?:\.(\d+))?", result.stdout)
        if not match:
            return None
        return tuple(int(part or 0) for part in match.groups())

    def encryption_supported(self) -> bool:
        version = self.version()
        return bool(version and version[:2] >= MINIMUM_ENCRYPTION_VERSION)

    def version_label(self) -> str:
        version = self.version()
        return ".".join(map(str, version)) if version else "unknown"

    def command(self, *arguments: str, config_path: Optional[Path] = None, password_command: bool = False) -> list[str]:
        executable = self.executable()
        if not executable:
            raise FileNotFoundError("rclone is not installed")
        selected_config = Path(config_path or self.config_path).resolve()
        command = [executable, "--config", str(selected_config)]
        if password_command:
            reader = "cmd /d /c echo %RCLONE_CONFIG_PASS%" if os.name == "nt" else "sh -c 'printf %s \"$RCLONE_CONFIG_PASS\"'"
            command.extend(["--password-command", reader])
        return [*command, *map(str, arguments)]

    @staticmethod
    def classify(returncode: int, output: str) -> Optional[str]:
        if returncode == 0:
            return None
        lowered = output.lower()
        if "invalid_grant" in lowered or "token has been expired or revoked" in lowered:
            return "drive_unauthorized"
        if "rate limit" in lowered or "ratelimitexceeded" in lowered or "user rate limit" in lowered:
            return "drive_rate_limited"
        if "storagequotaexceeded" in lowered or "quota exceeded" in lowered:
            return "drive_quota_exceeded"
        if "not found" in lowered or "directory not found" in lowered:
            return "drive_not_found"
        if "permission" in lowered or "forbidden" in lowered:
            return "drive_permission_denied"
        if "timeout" in lowered or "connection" in lowered or "network" in lowered:
            return "drive_network_error"
        return "rclone_failed"

    async def run(
        self,
        *arguments: str,
        config_path: Optional[Path] = None,
        timeout: float = 60,
        input_data: Optional[bytes] = None,
        output_limit: int = 8000,
        password_command: bool = False,
    ) -> RcloneResult:
        try:
            command = self.command(*arguments, config_path=config_path, password_command=password_command)
        except FileNotFoundError:
            return RcloneResult(127, error_code="rclone_unavailable")
        selected_config = Path(config_path or self.config_path)
        selected_config.parent.mkdir(parents=True, exist_ok=True)
        async with self._semaphore:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(input_data), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return RcloneResult(124, error_code="rclone_timeout")
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
        stdout_text = stdout.decode("utf-8", errors="replace")[-output_limit:]
        stderr_text = stderr.decode("utf-8", errors="replace")[-output_limit:]
        return RcloneResult(
            process.returncode or 0,
            stdout=stdout_text,
            stderr=stderr_text,
            error_code=self.classify(process.returncode or 0, f"{stdout_text}\n{stderr_text}"),
        )

    async def open_stream(
        self,
        *arguments: str,
        config_path: Optional[Path] = None,
    ) -> tuple[asyncio.subprocess.Process, AsyncIterator[bytes]]:
        command = self.command(*arguments, config_path=config_path)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def chunks() -> AsyncIterator[bytes]:
            try:
                assert process.stdout is not None
                while chunk := await process.stdout.read(64 * 1024):
                    yield chunk
                await process.wait()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

        return process, chunks()

    async def copyto_atomic(self, remote: str, destination: str, *, timeout: float = 60 * 60) -> RcloneResult:
        final_path = Path(destination)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=final_path.parent,
            prefix=f".{final_path.name}.",
            suffix=".rclone-part",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        try:
            result = await self.run("copyto", remote, str(temporary), timeout=timeout)
            if result.ok:
                os.replace(temporary, final_path)
            return result
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9-]{36}", job_id):
            raise ValueError("invalid job identifier")
        return self.setup_root / job_id

    def write_job_secret(self, job_id: str, payload: dict) -> Path:
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        target = directory / "oauth.json"
        protected = dict(payload)
        for key in ("client_secret", "pkce_verifier", "token"):
            if key in protected:
                protected[key] = protect_secret(json.dumps(protected[key], separators=(",", ":")) if isinstance(protected[key], (dict, list)) else str(protected[key]))
        self._atomic_write(target, json.dumps(protected, separators=(",", ":")), 0o600)
        return target

    def read_job_secret(self, job_id: str) -> dict:
        target = self.job_dir(job_id) / "oauth.json"
        payload = json.loads(target.read_text(encoding="utf-8"))
        for key in ("client_secret", "pkce_verifier", "token"):
            if key in payload:
                revealed = reveal_secret(payload[key])
                if key == "token":
                    payload[key] = json.loads(revealed)
                else:
                    payload[key] = revealed
        return payload

    def write_drive_config(self, job_id: str, remote_name: str, payload: dict) -> Path:
        if not REMOTE_NAME_RE.fullmatch(remote_name):
            raise ValueError("invalid remote name")
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        config_path = directory / "rclone.conf"
        parser = configparser.RawConfigParser()
        parser.optionxform = str
        parser[remote_name] = {
            "type": "drive",
            "client_id": str(payload["client_id"]),
            "client_secret": str(payload["client_secret"]),
            "scope": "drive",
            "token": json.dumps(payload["token"], separators=(",", ":")),
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
            parser.write(handle)
            temporary = Path(handle.name)
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, config_path)
        return config_path

    async def _encrypted_candidate(self, source: Path) -> Path:
        if not self.encryption_supported():
            raise RcloneConfigEncryptionError(
                f"Rclone 1.68 or newer is required for configuration encryption; found {self.version_label()}."
            )
        with tempfile.NamedTemporaryFile("wb", dir=self.config_dir, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copyfile(source, temporary)
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            result = await self.run(
                "config",
                "encryption",
                "set",
                config_path=temporary,
                timeout=30,
                password_command=True,
            )
            if not result.ok:
                raise RcloneConfigEncryptionError(
                    "Rclone could not encrypt its application-owned configuration."
                )
            header = temporary.read_text(encoding="utf-8", errors="ignore")[:128]
            if "RCLONE_ENCRYPT_V" not in header:
                raise RcloneConfigEncryptionError("Rclone reported success without encrypting its configuration.")
            check = await self.run(
                "config",
                "encryption",
                "check",
                config_path=temporary,
                timeout=30,
                password_command=True,
            )
            if not check.ok:
                raise RcloneConfigEncryptionError("The encrypted Rclone configuration could not be verified.")
            return temporary
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    async def activate_remote(self, source: Path, remote_name: str) -> None:
        async with self._config_lock:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.config_dir, 0o700)
            source_parser = configparser.RawConfigParser()
            source_parser.read(source, encoding="utf-8")
            if not source_parser.has_section(remote_name):
                raise ValueError("temporary Drive remote is missing")
            temporary = await self._encrypted_candidate(source)
            os.replace(temporary, self.config_path)

    async def ensure_config_encrypted(self) -> bool:
        if not self.config_path.is_file():
            return True
        try:
            header = self.config_path.read_text(encoding="utf-8", errors="ignore")[:128]
        except OSError:
            return False
        if "RCLONE_ENCRYPT_V" in header:
            return True
        try:
            async with self._config_lock:
                temporary = await self._encrypted_candidate(self.config_path)
                os.replace(temporary, self.config_path)
            return True
        except (OSError, RcloneConfigEncryptionError):
            return False

    def cleanup_job(self, job_id: str) -> None:
        directory = self.job_dir(job_id)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)

    @staticmethod
    def _atomic_write(path: Path, value: str, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(value)
            temporary = Path(handle.name)
        if os.name != "nt":
            os.chmod(temporary, mode)
        os.replace(temporary, path)


rclone_service = RcloneService()
