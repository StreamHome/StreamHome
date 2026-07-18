import os
import json
from typing import Optional
from dotenv import load_dotenv

# Load .env file relative to the config.py location
config_dir = os.path.dirname(os.path.abspath(__file__))
base_dir = os.path.abspath(os.path.join(config_dir, ".."))

# Inject local bin/ path into system PATH for discovery of ffmpeg/ffprobe/rclone
bin_path = os.path.abspath(os.path.join(base_dir, "bin"))
if bin_path not in os.environ["PATH"]:
    os.environ["PATH"] = bin_path + os.pathsep + os.environ["PATH"]
load_dotenv(dotenv_path=os.path.join(config_dir, ".env"))

class Settings:
    API_BEARER_TOKEN: str = os.getenv("API_BEARER_TOKEN", "secure-token-123")
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
    TMDB_READ_ACCESS_TOKEN: str = os.getenv("TMDB_READ_ACCESS_TOKEN", "")
    db_path = os.path.abspath(os.path.join(config_dir, "database.db")).replace("\\", "/")
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    MEDIA_DIR: str = os.getenv("MEDIA_DIR", "media")
    TEMP_DIR: str = os.getenv("TEMP_DIR", "temp")
    
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
    JWT_EXPIRATION_MINUTES: int = 60 * 24 * 60  # 60 day absolute session
    AUTH_CHALLENGE_MINUTES: int = 5
    REAUTHENTICATION_MINUTES: int = 10
    APP_VERSION: str = os.getenv("STREAMHOME_VERSION", "1.0.0")

    # Storage engine configuration: "LOCAL" or "CLOUD"
    STORAGE_ENGINE: str = os.getenv("STORAGE_ENGINE", "LOCAL")
    
    # Cloud storage configuration for rclone
    RCLONE_REMOTE_PATH: str = os.getenv("RCLONE_REMOTE_PATH", "gdrive:media")

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
                    self.BACKUP_ENABLED = data.get("backup_enabled", self.BACKUP_ENABLED)
                    self.AUTO_UPDATE_ENABLED = data.get("auto_update_enabled", self.AUTO_UPDATE_ENABLED)
                    self.HEVC_COMPRESSION_MODE = data.get("hevc_compression_mode", self.HEVC_COMPRESSION_MODE)
            except Exception as e:
                print(f"Error loading settings.json: {e}")

    def save_to_json(self):
        json_path = os.path.join(config_dir, "settings.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({
                    "storage_engine": self.STORAGE_ENGINE,
                    "rclone_remote_path": self.RCLONE_REMOTE_PATH,
                    "backup_enabled": self.BACKUP_ENABLED,
                    "auto_update_enabled": self.AUTO_UPDATE_ENABLED,
                    "hevc_compression_mode": self.HEVC_COMPRESSION_MODE
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
