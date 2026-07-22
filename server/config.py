import os
import json
from typing import Optional
from dotenv import load_dotenv

# Load .env file relative to the config.py location
config_dir = os.path.dirname(os.path.abspath(__file__))
base_dir = os.path.abspath(os.path.join(config_dir, ".."))
root_env_path = os.path.join(base_dir, ".env")
server_env_path = os.path.join(config_dir, ".env")

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

class Settings:
    BASE_DIR: str = base_dir
    ROOT_ENV_PATH: str = root_env_path
    SERVER_ENV_PATH: str = server_env_path
    SETUP_COMPLETE: bool = os.getenv("SETUP", "true").lower() in ("true", "1", "yes")
    WEB_PORT: int = env_int("WEB_PORT", 3000, 1, 65535)
    PUBLIC_URL: str = os.getenv("PUBLIC_URL", f"http://localhost:{WEB_PORT}").rstrip("/")
    API_BEARER_TOKEN: str = os.getenv("API_BEARER_TOKEN", "secure-token-123")
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
    TMDB_READ_ACCESS_TOKEN: str = os.getenv("TMDB_READ_ACCESS_TOKEN", "")
    db_path = os.path.abspath(os.path.join(config_dir, "database.db")).replace("\\", "/")
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    MEDIA_DIR: str = os.path.abspath(os.getenv("MEDIA_DIR", os.path.join(config_dir, "media")))
    TEMP_DIR: str = os.path.abspath(os.getenv("TEMP_DIR", os.path.join(config_dir, "temp")))
    PLAYBACK_CACHE_GB: float = max(0.25, min(500.0, float(os.getenv("PLAYBACK_CACHE_GB", "20"))))
    PLAYBACK_TRANSCODE_CONCURRENCY: int = env_int("PLAYBACK_TRANSCODE_CONCURRENCY", 2, 1, 8)
    
    # 2FA Authentication JWT settings
    JWT_SECRET = os.getenv("JWT_SECRET")
    if not JWT_SECRET:
        import secrets
        generated_secret = secrets.token_hex(32)
        env_file = os.path.join(config_dir, ".env")
        try:
            with open(env_file, "a") as f:
                f.write(f'\nJWT_SECRET="{generated_secret}"\n')
        except Exception:
            pass
        os.environ["JWT_SECRET"] = generated_secret
        JWT_SECRET = generated_secret
    JWT_ALGORITHM: str = "HS256"
    SESSION_LIFETIME_DAYS: int = max(1, min(365, int(os.getenv("SESSION_LIFETIME_DAYS", "60"))))
    JWT_EXPIRATION_MINUTES: int = 60 * 24 * SESSION_LIFETIME_DAYS
    AUTH_CHALLENGE_MINUTES: int = 5
    REAUTHENTICATION_MINUTES: int = 10
    APP_VERSION: str = os.getenv("STREAMHOME_VERSION", "1.0.0")
    RECOMMENDATION_V2_ENABLED: bool = os.getenv("RECOMMENDATION_V2_ENABLED", "true").lower() in ("true", "1", "yes")
    RECOMMENDATION_V2_SHADOW: bool = os.getenv("RECOMMENDATION_V2_SHADOW", "false").lower() in ("true", "1", "yes")

    # Storage engine configuration: "LOCAL" or "CLOUD"
    STORAGE_ENGINE: str = os.getenv("STORAGE_ENGINE", "LOCAL")
    
    # Cloud storage configuration for rclone
    RCLONE_REMOTE_PATH: str = os.getenv("RCLONE_REMOTE_PATH", "gdrive:media")
    GOOGLE_DRIVE_AUDIENCE: str = os.getenv("GOOGLE_DRIVE_AUDIENCE", "external")
    GOOGLE_DRIVE_PUBLISHING_STATUS: str = os.getenv("GOOGLE_DRIVE_PUBLISHING_STATUS", "production")

    # Automated Database Backup System
    BACKUP_ENABLED: bool = os.getenv("BACKUP_ENABLED", "False").lower() in ("true", "1", "yes")

    # Automated Update System
    AUTO_UPDATE_ENABLED: bool = os.getenv("AUTO_UPDATE_ENABLED", "False").lower() in ("true", "1", "yes")

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
                    self.AUTO_UPDATE_ENABLED = data.get("auto_update_enabled", self.AUTO_UPDATE_ENABLED)
                    self.HEVC_COMPRESSION_MODE = data.get("hevc_compression_mode", self.HEVC_COMPRESSION_MODE)
                    self.SESSION_LIFETIME_DAYS = max(1, min(365, int(data.get("session_lifetime_days", self.SESSION_LIFETIME_DAYS))))
                    self.JWT_EXPIRATION_MINUTES = 60 * 24 * self.SESSION_LIFETIME_DAYS
            except Exception as e:
                print(f"Error loading settings.json: {e}")

    def save_to_json(self):
        json_path = os.path.join(config_dir, "settings.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({
                    "storage_engine": self.STORAGE_ENGINE,
                    "rclone_remote_path": self.RCLONE_REMOTE_PATH,
                    "public_url": self.PUBLIC_URL,
                    "google_drive_audience": getattr(self, "GOOGLE_DRIVE_AUDIENCE", "external"),
                    "google_drive_publishing_status": getattr(self, "GOOGLE_DRIVE_PUBLISHING_STATUS", "production"),
                    "backup_enabled": self.BACKUP_ENABLED,
                    "auto_update_enabled": self.AUTO_UPDATE_ENABLED,
                    "hevc_compression_mode": self.HEVC_COMPRESSION_MODE,
                    "session_lifetime_days": self.SESSION_LIFETIME_DAYS
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving settings.json: {e}")

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
