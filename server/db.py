from typing import AsyncGenerator
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
            existing_tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            movie_columns = {row[1] for row in check.execute("PRAGMA table_info(movie)").fetchall()}
            playback_run_columns = {row[1] for row in check.execute("PRAGMA table_info(playbackrun)").fetchall()} if "playbackrun" in existing_tables else set()
            playback_upgrade_needed = (
                "playbackrun" not in existing_tables
                or bool(playback_run_columns and "last_progress_at" not in playback_run_columns)
                or bool(movie_columns and "source_fingerprint" not in movie_columns)
            )
            if playback_upgrade_needed:
                backup_dir = os.path.join(os.path.dirname(settings.db_path), "backup")
                os.makedirs(backup_dir, exist_ok=True)
                backup_path = os.path.join(backup_dir, f"pre-playback-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
                target = sqlite3.connect(backup_path)
                try:
                    check.backup(target)
                    logger.info(f"[Database] Pre-playback migration backup created: {backup_path}")
                finally:
                    target.close()
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

            if "playbacksession" in inspector.get_table_names():
                logger.info("[Database] Deduplicating playbacksession rows...")
                sync_conn.exec_driver_sql(
                    "DELETE FROM playbacksession WHERE id NOT IN ("
                    "SELECT id FROM ("
                    "SELECT id, ROW_NUMBER() OVER (PARTITION BY profile_id, movie_id, COALESCE(episode_id, '') "
                    "ORDER BY updated_at DESC, timestamp DESC, is_finished DESC) as rn "
                    "FROM playbacksession"
                    ") WHERE rn = 1)"
                )
                logger.info("[Database] Adding unique constraint index to playbacksession...")
                sync_conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_playback_session_profile_movie_episode "
                    "ON playbacksession (profile_id, movie_id, COALESCE(episode_id, ''))"
                )

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
                    "keywords_str": "TEXT DEFAULT '[]'",
                    "collection_name": "TEXT",
                    "crew_str": "TEXT DEFAULT '[]'",
                    "trope_vectors_str": "TEXT DEFAULT '[]'",
                    "dialogue_wpm": "FLOAT",
                    "dialogue_word_count": "INTEGER DEFAULT 0",
                    "dialogue_language": "TEXT",
                    "dialogue_confidence": "FLOAT DEFAULT 0",
                    "vibe_analysis_status": "TEXT",
                    "vibe_analysis_version": "INTEGER DEFAULT 0",
                    "vibe_analyzed_at": "FLOAT",
                }
                for column, sql_type in recommendation_columns.items():
                    if column not in movie_cols:
                        logger.info(f"[Database] Migrating: Adding '{column}' column to 'movie' table...")
                        sync_conn.exec_driver_sql(f"ALTER TABLE movie ADD COLUMN {column} {sql_type}")

                # Probe fields migration
                probe_columns = {
                    "probed_duration": "FLOAT",
                    "container": "TEXT",
                    "codec": "TEXT",
                    "width": "INTEGER",
                    "height": "INTEGER",
                    "frame_rate": "FLOAT",
                    "source_fingerprint": "TEXT",
                    "audio_metadata_str": "TEXT DEFAULT '[]'",
                }
                for column, sql_type in probe_columns.items():
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
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_movie_collection_name ON movie (collection_name)")
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_movie_source_fingerprint ON movie (source_fingerprint)")
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_movie_vibe_analysis_status ON movie (vibe_analysis_status)")
                sync_conn.exec_driver_sql(
                    "UPDATE movie SET cache_state = CASE "
                    "WHEN catalog_source = 'tmdb_cache' THEN 'ready' ELSE NULL END "
                    "WHERE cache_state IS NULL"
                )

            if "episode" in inspector.get_table_names():
                ep_cols = [col["name"] for col in inspector.get_columns("episode")]
                probe_columns = {
                    "probed_duration": "FLOAT",
                    "container": "TEXT",
                    "codec": "TEXT",
                    "width": "INTEGER",
                    "height": "INTEGER",
                    "frame_rate": "FLOAT",
                    "source_fingerprint": "TEXT",
                    "audio_metadata_str": "TEXT DEFAULT '[]'",
                    "dialogue_wpm": "FLOAT",
                    "dialogue_word_count": "INTEGER DEFAULT 0",
                    "dialogue_language": "TEXT",
                    "dialogue_confidence": "FLOAT DEFAULT 0",
                    "vibe_analysis_status": "TEXT",
                    "vibe_analysis_version": "INTEGER DEFAULT 0",
                    "vibe_analyzed_at": "FLOAT",
                }
                for column, sql_type in probe_columns.items():
                    if column not in ep_cols:
                        logger.info(f"[Database] Migrating: Adding '{column}' column to 'episode' table...")
                        sync_conn.exec_driver_sql(f"ALTER TABLE episode ADD COLUMN {column} {sql_type}")
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_episode_source_fingerprint ON episode (source_fingerprint)")
                sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_episode_vibe_analysis_status ON episode (vibe_analysis_status)")

            if "playbackrun" in inspector.get_table_names():
                playback_run_cols = [col["name"] for col in inspector.get_columns("playbackrun")]
                if "last_progress_at" not in playback_run_cols:
                    logger.info("[Database] Migrating: Adding 'last_progress_at' column to 'playbackrun' table...")
                    sync_conn.exec_driver_sql("ALTER TABLE playbackrun ADD COLUMN last_progress_at FLOAT NOT NULL DEFAULT 0")
                    sync_conn.exec_driver_sql("UPDATE playbackrun SET last_progress_at = COALESCE(last_seen_at, created_at, 0)")

            if "profilerecommendation" in inspector.get_table_names():
                recommendation_cols = [col["name"] for col in inspector.get_columns("profilerecommendation")]
                if "candidate_source" not in recommendation_cols:
                    sync_conn.exec_driver_sql("ALTER TABLE profilerecommendation ADD COLUMN candidate_source TEXT DEFAULT 'ranked'")
                if "source_confidence" not in recommendation_cols:
                    sync_conn.exec_driver_sql("ALTER TABLE profilerecommendation ADD COLUMN source_confidence FLOAT DEFAULT 0.5")
                if "reason_details_str" not in recommendation_cols:
                    sync_conn.exec_driver_sql("ALTER TABLE profilerecommendation ADD COLUMN reason_details_str TEXT DEFAULT '[]'")

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

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session:
        yield session
