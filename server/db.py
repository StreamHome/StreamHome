from typing import AsyncGenerator  # <-- TİP DOĞRULAMA İÇİN EKLENDİ
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
                    
        await conn.run_sync(migrate)

# STRICT TYPE CORRECTION FIX: Updated return signature to completely satisfy Pylance type diagnostics
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session:
        yield session
