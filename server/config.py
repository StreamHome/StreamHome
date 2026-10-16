import os
import threading
import json
import base64
import secrets
import stat
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
from lifecycle.configuration_paths import resolve_temp_directory
from dotenv import load_dotenv

# Load .env file relative to the config.py location
config_dir = os.path.dirname(os.path.abspath(__file__))
base_dir = os.path.abspath(os.path.join(config_dir, ".."))
root_env_path = os.path.join(base_dir, ".env")
server_env_path = os.path.join(config_dir, ".env")
SETTINGS_WRITE_LOCK = threading.RLock()

def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default

# Inject local bin/ path into system PATH for discovery of ffmpeg/ffprobe/rclone
bin_path = os.path.abspath(os.path.join(base_dir, "bin"))
if bin_path not in os.environ["PATH"]:
    os.environ["PATH"] = bin_path + os.pathsep + os.environ["PATH"]
load_dotenv(dotenv_path=root_env_path, override=False)
load_dotenv(dotenv_path=server_env_path, override=False)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("private configuration write made no progress")
        view = view[written:]


def _private_atomic_replace(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        target_mode = os.lstat(path).st_mode
    except FileNotFoundError:
        target_mode = None
    if target_mode is not None and stat.S_ISLNK(target_mode):
        raise OSError("refusing to replace a symlinked private configuration file")

    temporary = f"{path}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    replaced = False
    try:
        fd = os.open(temporary, flags, stat.S_IRUSR | stat.S_IWUSR)
        if os.name != "nt":
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        _write_all(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        replaced = True
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(directory, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if not replaced:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise


def _append_private_env_values(path: str, values: dict[str, str]) -> None:
    existing = ""
    if os.path.exists(path):
        if os.path.islink(path):
            raise OSError("refusing to read a symlinked private configuration file")
        with open(path, "r", encoding="utf-8") as handle:
            existing = handle.read()
    content = existing.rstrip()
    for key, value in values.items():
        content += f'\n{key}="{value}"'
    _private_atomic_replace(path, content + "\n")


def _append_private_env(path: str, key: str, value: str) -> None:
    _append_private_env_values(path, {key: value})


def remove_env_key(path: str, key: str) -> None:
    if not os.path.exists(path):
        return
    if os.path.islink(path):
        raise OSError("refusing to read a symlinked private configuration file")
    prefix = f"{key}="
    with open(path, "r", encoding="utf-8") as handle:
        lines = [line for line in handle if not line.strip().startswith(prefix)]
    _private_atomic_replace(path, "".join(lines))

class Settings:
    BASE_DIR: str = base_dir
    ROOT_ENV_PATH: str = root_env_path
    SERVER_ENV_PATH: str = server_env_path
    # Missing configuration must fail closed into the setup gate.
    SETUP_COMPLETE: bool = os.getenv("SETUP", "false").lower() in ("true", "1", "yes")
    WEB_PORT: int = env_int("WEB_PORT", 3000, 1, 65535)
    PUBLIC_URL: str = os.getenv("PUBLIC_URL", f"http://localhost:{WEB_PORT}").rstrip("/")
    API_BEARER_TOKEN: str = os.getenv("API_BEARER_TOKEN", "")
    ALLOWED_ORIGINS: list[str] = [
        origin.strip().rstrip("/")
        for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    TRUSTED_PROXY_CIDRS: list[str] = [
        cidr.strip()
        for cidr in os.getenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32,::1/128").split(",")
        if cidr.strip()
    ]
    INGEST_TRUSTED_HOSTS: list[str] = [
        host.strip().lower()
        for host in os.getenv("INGEST_TRUSTED_HOSTS", "").split(",")
        if host.strip()
    ]
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
    TMDB_READ_ACCESS_TOKEN: str = os.getenv("TMDB_READ_ACCESS_TOKEN", "")
    db_path = os.path.abspath(os.path.join(config_dir, "database.db")).replace("\\", "/")
    DATABASE_URL: str = f"sqlite+aiosqlite:///{db_path}"
    MEDIA_DIR: str = os.path.abspath(os.path.join(config_dir, "media"))
    TEMP_DIR: str = str(resolve_temp_directory(Path(base_dir), environment=os.environ))
    PLAYBACK_CACHE_GB: float = max(0.25, min(500.0, float(os.getenv("PLAYBACK_CACHE_GB", "20"))))
    PLAYBACK_TRANSCODE_CONCURRENCY: int = env_int("PLAYBACK_TRANSCODE_CONCURRENCY", 2, 1, 8)
    PLAYBACK_JOB_STALL_SECONDS: int = env_int("PLAYBACK_JOB_STALL_SECONDS", 120, 30, 900)
    PLAYBACK_LOOPBACK_URL: str = os.getenv("PLAYBACK_LOOPBACK_URL", "http://127.0.0.1:8000").rstrip("/")
    
    # 2FA Authentication JWT settings
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    SECRET_ENCRYPTION_KEY: str = os.getenv("SECRET_ENCRYPTION_KEY", "")
    JWT_ALGORITHM: str = "HS256"
    SESSION_LIFETIME_DAYS: int = max(1, min(365, int(os.getenv("SESSION_LIFETIME_DAYS", "60"))))
    JWT_EXPIRATION_MINUTES: int = 60 * 24 * SESSION_LIFETIME_DAYS
    AUTH_CHALLENGE_MINUTES: int = 5
    REAUTHENTICATION_MINUTES: int = 10
    APP_VERSION: str = os.getenv("STREAMHOME_VERSION", "0.1.0-alpha.1")
    BUILD_ID: str = os.getenv("STREAMHOME_BUILD_ID", "dev")
    RECOMMENDATION_V2_ENABLED: bool = os.getenv("RECOMMENDATION_V2_ENABLED", "true").lower() in ("true", "1", "yes")
    RECOMMENDATION_V2_SHADOW: bool = os.getenv("RECOMMENDATION_V2_SHADOW", "false").lower() in ("true", "1", "yes")

    # Storage engine configuration: "LOCAL" or "CLOUD"
    STORAGE_ENGINE: str = os.getenv("STORAGE_ENGINE", "LOCAL")
    
    # Cloud storage configuration for rclone
    RCLONE_REMOTE_PATH: str = os.getenv("RCLONE_REMOTE_PATH", "gdrive:media")
    RCLONE_CONFIG_PASS: str = os.getenv("RCLONE_CONFIG_PASS", "")
    GOOGLE_DRIVE_AUDIENCE: str = os.getenv("GOOGLE_DRIVE_AUDIENCE", "external")
    GOOGLE_DRIVE_PUBLISHING_STATUS: str = os.getenv("GOOGLE_DRIVE_PUBLISHING_STATUS", "production")

    # Automated Database Backup System
    BACKUP_ENABLED: bool = os.getenv("BACKUP_ENABLED", "False").lower() in ("true", "1", "yes")

    # Automated Update System
    AUTO_UPDATE_ENABLED: bool = os.getenv("AUTO_UPDATE_ENABLED", "false").lower() in ("true", "1", "yes")
    UPDATE_IDLE_MINUTES: int = env_int("UPDATE_IDLE_MINUTES", 10, 5, 120)
    UPDATE_CHECK_INTERVAL_HOURS: int = env_int("UPDATE_CHECK_INTERVAL_HOURS", 6, 1, 24)
    UPDATE_MAINTENANCE_START: str = os.getenv("UPDATE_MAINTENANCE_START", "").strip()
    UPDATE_MAINTENANCE_END: str = os.getenv("UPDATE_MAINTENANCE_END", "").strip()
    UPDATE_BRANCH: str = os.getenv("UPDATE_BRANCH", "main").strip() or "main"
    UPDATE_REQUIRE_SIGNED_COMMITS: bool = os.getenv("UPDATE_REQUIRE_SIGNED_COMMITS", "false").lower() in ("true", "1", "yes")
    UPDATE_TRUSTED_SIGNERS: str = os.getenv("UPDATE_TRUSTED_SIGNERS", "").strip()

    # Library Optimization System
    HEVC_COMPRESSION_MODE: str = os.getenv("HEVC_COMPRESSION_MODE", "auto")

    # Ingestion Notification Settings
    VIDEO_SENDER_API_URL: Optional[str] = os.getenv("VIDEO_SENDER_API_URL", None)

    def load_from_json(self):
        json_path = os.path.join(config_dir, "settings.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.STORAGE_ENGINE = data.get("storage_engine", self.STORAGE_ENGINE)
                    self.RCLONE_REMOTE_PATH = data.get("rclone_remote_path", self.RCLONE_REMOTE_PATH)
                    self.PUBLIC_URL = data.get("public_url", self.PUBLIC_URL)
                    self.GOOGLE_DRIVE_AUDIENCE = data.get("google_drive_audience", getattr(self, "GOOGLE_DRIVE_AUDIENCE", "external"))
                    self.GOOGLE_DRIVE_PUBLISHING_STATUS = data.get("google_drive_publishing_status", getattr(self, "GOOGLE_DRIVE_PUBLISHING_STATUS", "production"))
                    self.BACKUP_ENABLED = data.get("backup_enabled", self.BACKUP_ENABLED)
                    self.AUTO_UPDATE_ENABLED = bool(data.get("auto_update_enabled", self.AUTO_UPDATE_ENABLED))
                    self.UPDATE_IDLE_MINUTES = max(5, min(120, int(data.get("update_idle_minutes", self.UPDATE_IDLE_MINUTES))))
                    self.UPDATE_CHECK_INTERVAL_HOURS = max(1, min(24, int(data.get("update_check_interval_hours", self.UPDATE_CHECK_INTERVAL_HOURS))))
                    self.UPDATE_MAINTENANCE_START = str(data.get("update_maintenance_start", self.UPDATE_MAINTENANCE_START)).strip()
                    self.UPDATE_MAINTENANCE_END = str(data.get("update_maintenance_end", self.UPDATE_MAINTENANCE_END)).strip()
                    self.HEVC_COMPRESSION_MODE = data.get("hevc_compression_mode", self.HEVC_COMPRESSION_MODE)
                    self.SESSION_LIFETIME_DAYS = max(1, min(365, int(data.get("session_lifetime_days", self.SESSION_LIFETIME_DAYS))))
                    self.JWT_EXPIRATION_MINUTES = 60 * 24 * self.SESSION_LIFETIME_DAYS
            except Exception as e:
                print(f"Error loading settings.json: {e}")

    def save_to_json(self):
        with SETTINGS_WRITE_LOCK:
            self._save_to_json_locked()

    def _save_to_json_locked(self):
        json_path = os.path.join(config_dir, "settings.json")
        temporary = f"{json_path}.{secrets.token_hex(8)}.tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as f:
            json.dump({
                "storage_engine": self.STORAGE_ENGINE,
                "rclone_remote_path": self.RCLONE_REMOTE_PATH,
                "public_url": self.PUBLIC_URL,
                "google_drive_audience": getattr(self, "GOOGLE_DRIVE_AUDIENCE", "external"),
                "google_drive_publishing_status": getattr(self, "GOOGLE_DRIVE_PUBLISHING_STATUS", "production"),
                "backup_enabled": self.BACKUP_ENABLED,
                "auto_update_enabled": self.AUTO_UPDATE_ENABLED,
                "update_idle_minutes": self.UPDATE_IDLE_MINUTES,
                "update_check_interval_hours": self.UPDATE_CHECK_INTERVAL_HOURS,
                "update_maintenance_start": self.UPDATE_MAINTENANCE_START,
                "update_maintenance_end": self.UPDATE_MAINTENANCE_END,
                "hevc_compression_mode": self.HEVC_COMPRESSION_MODE,
                "session_lifetime_days": self.SESSION_LIFETIME_DAYS
            }, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, json_path)

    def get_system_profile(self) -> dict:
        profile_path = os.path.join(config_dir, "system_profile.json")
        cores = os.cpu_count() or 2
        profile = {"cpu_cores": cores}
        try:
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2)
        except Exception:
            pass
        return profile

settings = Settings()
settings.load_from_json()


class ConfigurationInitializationError(RuntimeError):
    code = "configuration_initialization_failed"


@dataclass(frozen=True, slots=True)
class ConfigurationInitializationResult:
    generated_keys: tuple[str, ...]


def initialize_runtime_configuration(*, allow_mutation: bool) -> ConfigurationInitializationResult:
    """Validate required secrets and generate them only for authorized normal startup."""

    missing = [
        key
        for key in ("JWT_SECRET", "SECRET_ENCRYPTION_KEY", "RCLONE_CONFIG_PASS")
        if not str(getattr(settings, key, "") or "")
    ]
    if not missing:
        return ConfigurationInitializationResult(())
    if not allow_mutation:
        raise ConfigurationInitializationError("required startup configuration is missing")

    generated = {
        "JWT_SECRET": secrets.token_hex(32),
        "SECRET_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "RCLONE_CONFIG_PASS": secrets.token_urlsafe(36),
    }
    selected = {key: generated[key] for key in missing}
    try:
        _append_private_env_values(settings.SERVER_ENV_PATH, selected)
    except OSError as exc:
        raise ConfigurationInitializationError("required startup configuration could not be persisted") from exc
    for key, value in selected.items():
        setattr(settings, key, value)
        os.environ[key] = value
    return ConfigurationInitializationResult(tuple(missing))
