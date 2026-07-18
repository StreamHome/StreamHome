from typing import AsyncGenerator  # <-- TİP DOĞRULAMA İÇİN EKLENDİ
import os
import sqlite3
from datetime import datetime

from sqlmodel import SQLModel
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import event
from config import settings

from services.logger import logger

engine = create_async_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False}
)

@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

async def init_db():
    # Ensure models are imported before creating tables
    import models
    if os.path.exists(settings.db_path):
        check = sqlite3.connect(settings.db_path)
        try:
            movie_columns = {row[1] for row in check.execute("PRAGMA table_info(movie)").fetchall()}
            if movie_columns and "catalog_source" not in movie_columns:
                backup_dir = os.path.join(os.path.dirname(settings.db_path), "backup")
                os.makedirs(backup_dir, exist_ok=True)
                backup_path = os.path.join(backup_dir, f"pre-recommendation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
                target = sqlite3.connect(backup_path)
                try:
                    check.backup(target)
                    logger.info(f"[Database] Pre-recommendation migration backup created: {backup_path}")
                finally:
                    target.close()
        finally:
            check.close()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        
        def migrate(sync_conn):
            from sqlalchemy import inspect
            inspector = inspect(sync_conn)
            if "downloadtask" in inspector.get_table_names():
                columns = [col["name"] for col in inspector.get_columns("downloadtask")]
                if "language" not in columns:
                    logger.info("[Database] Migrating: Adding 'language' column to 'downloadtask' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE downloadtask ADD COLUMN language TEXT")
                if "error_message" not in columns:
                    logger.info("[Database] Migrating: Adding 'error_message' column to 'downloadtask' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE downloadtask ADD COLUMN error_message TEXT")
                if "has_video" not in columns:
                    logger.info("[Database] Migrating: Adding 'has_video' column to 'downloadtask' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE downloadtask ADD COLUMN has_video BOOLEAN")
                if "has_audio" not in columns:
                    logger.info("[Database] Migrating: Adding 'has_audio' column to 'downloadtask' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE downloadtask ADD COLUMN has_audio BOOLEAN")
                if "scan_quality" not in columns:
                    logger.info("[Database] Migrating: Adding 'scan_quality' column to 'downloadtask' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE downloadtask ADD COLUMN scan_quality TEXT")
                if "skip_markers_str" not in columns:
                    logger.info("[Database] Migrating: Adding 'skip_markers_str' column to 'downloadtask' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE downloadtask ADD COLUMN skip_markers_str TEXT DEFAULT '{}'")
            
            if "user" in inspector.get_table_names():
                user_cols = [col["name"] for col in inspector.get_columns("user")]
                if "totp_secret" not in user_cols:
                    logger.info("[Database] Migrating: Adding 'totp_secret' column to 'user' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE user ADD COLUMN totp_secret TEXT")
                if "two_factor_enabled" not in user_cols:
                    logger.info("[Database] Migrating: Adding 'two_factor_enabled' column to 'user' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE user ADD COLUMN two_factor_enabled BOOLEAN DEFAULT 0")
                if "failed_login_attempts" not in user_cols:
                    logger.info("[Database] Migrating: Adding 'failed_login_attempts' column to 'user' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE user ADD COLUMN failed_login_attempts INTEGER DEFAULT 0")
                if "lockout_until" not in user_cols:
                    logger.info("[Database] Migrating: Adding 'lockout_until' column to 'user' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE user ADD COLUMN lockout_until FLOAT")
                if "last_login_at" not in user_cols:
                    sync_conn.exec_driver_sql("ALTER TABLE user ADD COLUMN last_login_at FLOAT")
                if "last_login_ip" not in user_cols:
                    sync_conn.exec_driver_sql("ALTER TABLE user ADD COLUMN last_login_ip TEXT")
                if "last_login_device" not in user_cols:
                    sync_conn.exec_driver_sql("ALTER TABLE user ADD COLUMN last_login_device TEXT")

            if "movie" in inspector.get_table_names():
                movie_cols = [col["name"] for col in inspector.get_columns("movie")]
                if "vote_average" not in movie_cols:
                    logger.info("[Database] Migrating: Adding 'vote_average' column to 'movie' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE movie ADD COLUMN vote_average FLOAT DEFAULT 7.5")
                if "vote_count" not in movie_cols:
                    logger.info("[Database] Migrating: Adding 'vote_count' column to 'movie' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE movie ADD COLUMN vote_count INTEGER DEFAULT 100")
                recommendation_columns = {
                    "tmdb_id": "INTEGER",
                    "catalog_source": "TEXT DEFAULT 'server'",
                    "availability": "TEXT DEFAULT 'available'",
                    "popularity": "FLOAT DEFAULT 0",
                    "cached_at": "FLOAT",
                    "metadata_refreshed_at": "FLOAT",
                    "remote_thumbnail_url": "TEXT",
                    "remote_banner_url": "TEXT",
                    "local_thumbnail_url": "TEXT",
                    "local_banner_url": "TEXT",
                    "cache_state": "TEXT",
                }
                for column, sql_type in recommendation_columns.items():
                    if column not in movie_cols:
                        logger.info(f"[Database] Migrating: Adding '{column}' column to 'movie' table...")
                        sync_conn.exec_driver_sql(f"ALTER TABLE movie ADD COLUMN {column} {sql_type}")

                # Existing IDs are already stable TMDB-derived identifiers. Backfill them once,
                # then classify rows by actual playable media instead of their folder location.
                sync_conn.exec_driver_sql(
                    "UPDATE movie SET tmdb_id = CAST(SUBSTR(id, 3) AS INTEGER) "
                    "WHERE tmdb_id IS NULL AND (id LIKE 'm_%' OR id LIKE 'tv_%')"
                )
                sync_conn.exec_driver_sql(
                    "UPDATE movie SET catalog_source = 'tmdb_cache', availability = 'cached' "
                    "WHERE COALESCE(TRIM(video_url), '') = '' "
                    "AND NOT EXISTS (SELECT 1 FROM episode e WHERE e.movie_id = movie.id "
                    "AND COALESCE(TRIM(e.video_url), '') <> '')"
                )
                sync_conn.exec_driver_sql(
                    "UPDATE movie SET catalog_source = 'server', availability = 'available' "
                    "WHERE COALESCE(TRIM(video_url), '') <> '' "
                    "OR EXISTS (SELECT 1 FROM episode e WHERE e.movie_id = movie.id "
                    "AND COALESCE(TRIM(e.video_url), '') <> '')"
                )
                if "downloadtask" in inspector.get_table_names():
                    sync_conn.exec_driver_sql(
                        "UPDATE movie SET catalog_source = 'server', availability = 'processing' "
                        "WHERE tmdb_id IN (SELECT tmdb_id FROM downloadtask "
                        "WHERE status IN ('PENDING', 'DOWNLOADING', 'MERGING', 'MOVING_CLOUD'))"
                    )
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_movie_tmdb_id ON movie (tmdb_id)")
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_movie_catalog_source ON movie (catalog_source)")
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_movie_availability ON movie (availability)")
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_movie_cache_state ON movie (cache_state)")
                sync_conn.exec_driver_sql(
                    "UPDATE movie SET cache_state = CASE "
                    "WHEN catalog_source = 'tmdb_cache' THEN 'ready' ELSE NULL END "
                    "WHERE cache_state IS NULL"
                )

            if "telemetryevent" in inspector.get_table_names():
                telemetry_cols = [col["name"] for col in inspector.get_columns("telemetryevent")]
                if "dedupe_key" not in telemetry_cols:
                    sync_conn.exec_driver_sql("ALTER TABLE telemetryevent ADD COLUMN dedupe_key TEXT")
                sync_conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_telemetry_dedupe_key "
                    "ON telemetryevent (dedupe_key) WHERE dedupe_key IS NOT NULL"
                )

            if "profiletaste" in inspector.get_table_names():
                # Older builds allowed duplicate taste rows. Merge them before enforcing the
                # invariant used by atomic recommendation updates.
                sync_conn.exec_driver_sql(
                    "DELETE FROM profiletaste WHERE id NOT IN ("
                    "SELECT MAX(id) FROM profiletaste GROUP BY profile_id, tag_type, LOWER(TRIM(tag_value)))"
                )
                sync_conn.exec_driver_sql(
                    "UPDATE profiletaste SET tag_value = LOWER(TRIM(tag_value))"
                )
                sync_conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_profile_taste_tag "
                    "ON profiletaste (profile_id, tag_type, tag_value)"
                )
                    
        await conn.run_sync(migrate)

# STRICT TYPE CORRECTION FIX: Updated return signature to completely satisfy Pylance type diagnostics
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session:
        yield session
