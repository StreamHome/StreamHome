from lifecycle.bootstrap import (
    MandatoryRecoveryStep,
    ReadinessCapability,
    StartupMode,
    get_startup_readiness,
    revalidate_startup_context,
    require_startup_context,
    run_mandatory_recovery,
)

STARTUP_CONTEXT = require_startup_context()

import os
import sys
import warnings
import asyncio
import json
import mimetypes
from contextlib import AsyncExitStack, asynccontextmanager
from typing import List, Optional
from datetime import datetime

import time
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db import init_db, engine, verify_database_integrity
from models import (
    Movie, Episode, PlaybackSession, WatchlistItem, MovieResponse,
    PlaybackSessionResponse, DiscoverMovieResponse, EpisodeResponse,
    Profile, ProfileResponse, APIModel, DownloadTask, TelemetryRequest,
    RecommendationFeedResponse, MediaPreferenceRequest, RecommendationExposureBatch,
    RecommendationOnboardingRequest, User, DriveSetupJob, PlaybackRun, AuthSession,
    TelemetryEvent, ProfileTaste, ProfileVibeVector, ProfileMediaPreference,
    ProfileOnboardingPreference, RecommendationExposure, ViewingAttempt,
    PlaybackMilestone, ProfileRecommendation, RecommendationRefreshState,
    RecommendationRuntimeMetric
)
from services.recommendation import (
    process_telemetry_event,
    record_authoritative_signal,
    rank_movies_for_profile,
    build_recommendation_payload,
    recommendation_worker,
    set_media_preference,
    get_media_preferences,
    record_recommendation_exposures,
    set_onboarding_preferences,
    get_onboarding_preferences,
    get_recommendation_diagnostics,
    reset_media_preferences,
    persist_profile_pool,
)
from services.request_security import allowed_origins, client_ip, same_origin_request, unsafe_cookie_request_requires_same_origin
from services.profile_security import grant_profile_access, hash_profile_pin, require_profile_access, verify_profile_pin
from services.rate_limit import clear as clear_rate_limit
from services.rate_limit import enforce as enforce_rate_limit
from services.rate_limit import fail as fail_rate_limit
from config import initialize_runtime_configuration, settings
from services.logger import initialize_file_logging, logger
from services.queue import queue_manager
from services.hevc_compressor import hevc_compressor
from services.playback_prep import playback_prep_service
from services.ingest_preview import ingest_preview_service
from services.vibe_analysis import vibe_analysis_manager
from services.update import automatic_update_worker
from lifecycle.coordination import (
    AdmissionClosedError,
    DrainSnapshot,
    EffectClass,
    get_runtime_coordinator,
    runtime_work,
)
from lifecycle.media_work import accepted_commit, settle
from lifecycle import state as lifecycle_state
import services.state as state
from routes.queue import router as queue_router
from routes.auth import router as auth_router, health_router, get_current_session, get_current_user, require_recent_reauth
from routes.backup import router as backup_router
from routes.update import router as update_router
from routes.lifecycle import router as lifecycle_router
from routes.setup import router as setup_router
from routes.playback import router as playback_router
from routes.admin_profiles import router as admin_profiles_router
from routes.media_updates import router as media_updates_router

STARTUP_READINESS = get_startup_readiness()

# 💥 WINDOWS ASYNC SUBPROCESS FIX
if sys.platform == 'win32':
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

async def playback_run_reaper():
    """Expire abandoned playback runs after 24 hours without activity."""
    while True:
        try:
            await asyncio.sleep(300)  # Check every 5 minutes
            async with runtime_work("playback.run-reaper", [EffectClass.DB_WRITE]):
                async with AsyncSession(engine) as db:
                    cutoff = time.time() - 24 * 60 * 60
                    stmt = select(PlaybackRun).where(PlaybackRun.lifecycle_state == "active", PlaybackRun.last_seen_at < cutoff)
                    result = await db.exec(stmt)
                    expired_runs = result.all()
                    if expired_runs:
                        logger.info(f"[Lifespan Reaper] Found {len(expired_runs)} inactive playback runs. Marking as expired.")
                        for run in expired_runs:
                            run.lifecycle_state = "expired"
                            db.add(run)
                        await db.commit()
        except AdmissionClosedError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Lifespan Reaper] Error reaping expired playback runs: {e}")

class _WorkerRuntime:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []
        self.update_stop = asyncio.Event()
        self.recommendation_stop = asyncio.Event()
        self.started = False


async def _initialize_configuration(*, allow_mutation: bool) -> None:
    initialize_runtime_configuration(allow_mutation=allow_mutation)


async def _initialize_runtime_filesystem() -> None:
    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    os.makedirs(settings.MEDIA_DIR, exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_DIR, "Movies"), exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_DIR, "Series"), exist_ok=True)
    ingest_preview_service.initialize_storage()
    playback_prep_service.initialize_storage()
    initialize_file_logging()
    settings.get_system_profile()
    ingest_preview_service.cleanup_expired()


async def _initialize_database(*, guarded: bool) -> None:
    if guarded and not os.path.isfile(settings.db_path):
        raise RuntimeError("guarded_database_missing")
    await init_db(
        allow_migration_backups=not guarded,
        allow_external_config_mutation=not guarded,
    )
    await verify_database_integrity()


async def _recover_setup_state() -> None:
    async with AsyncSession(engine) as db:
        existing_user = (await db.exec(select(User))).first()
        if not existing_user:
            settings.SETUP_COMPLETE = False


async def _harden_rclone_configuration() -> None:
    if not await rclone_service.ensure_config_encrypted():
        raise RuntimeError("rclone_configuration_hardening_failed")


async def _report_tmdb_configuration() -> None:
    try:
        from services.tmdb import tmdb_client

        if not tmdb_client.api_key and not tmdb_client.read_access_token:
            logger.warning(
                "TMDB_READ_ACCESS_TOKEN or TMDB_API_KEY is not configured; "
                "catalog recovery will use captured-media placeholders."
            )
    except Exception as exc:
        logger.warning(
            f"[Lifespan Startup] TMDB configuration check failed: {type(exc).__name__}"
        )


async def _recover_drive_setup() -> None:
    async with AsyncSession(engine) as db:
        drive_jobs = (await db.exec(select(DriveSetupJob))).all()
        changed = False
        for job in drive_jobs:
            job_changed = False
            if job.expires_at < time.time() and job.status not in {"cancelled", "failed", "expired"}:
                job.status = "expired"
                job.error_code = "drive_job_expired"
                job.progress = "Google Drive setup expired. Start again."
                rclone_service.cleanup_job(job.id)
                job_changed = True
            elif job.status == "exchanging_code":
                job.status = "failed"
                job.error_code = "drive_server_restarted"
                job.progress = "The server restarted during Google authorization. Start the Drive connection again."
                job_changed = True
            elif job.status == "testing":
                job.status = "selecting_folder"
                job.error_code = "drive_test_interrupted"
                job.progress = "The Drive test was interrupted. Run it again."
                job_changed = True
            if job_changed:
                changed = True
                job.updated_at = time.time()
                db.add(job)
        if changed:
            await db.commit()


async def _recover_interrupted_ingestion() -> None:
    async with AsyncSession(engine) as db:
        statement = select(DownloadTask).where(
            DownloadTask.status.in_(["DOWNLOADING", "MERGING", "MOVING_CLOUD"])
        )
        dangling_tasks = (await db.exec(statement)).all()
        for task in dangling_tasks:
            task.status = "FAILED"
            task.error_message = "Interrupted by server shutdown/restart."
            db.add(task)
            ingest_preview_service.mark_error(
                task.id,
                "INGESTION_INTERRUPTED",
                "The download and play-while-downloading preview were interrupted by a server restart.",
            )
            if task.media_type == "movie":
                movie = await db.get(Movie, f"m_{task.tmdb_id}")
                if movie and movie.preview_task_id == task.id:
                    movie.preview_task_id = None
                    if not movie.video_url.startswith("/media/"):
                        movie.video_url = ""
                        movie.availability = "cached"
                    db.add(movie)
            elif task.season is not None and task.episode is not None:
                episode = await db.get(Episode, f"ep_{task.tmdb_id}_s{task.season}_e{task.episode}")
                if episode and episode.preview_task_id == task.id:
                    episode.preview_task_id = None
                    if not episode.video_url.startswith("/media/"):
                        episode.video_url = ""
                    db.add(episode)
                show = await db.get(Movie, f"tv_{task.tmdb_id}")
                if show:
                    episodes = (await db.exec(select(Episode).where(Episode.movie_id == show.id))).all()
                    has_local = any(
                        item.video_url.startswith("/media/")
                        for item in episodes
                        if item.video_url
                    )
                    show.availability = "available" if has_local else "cached"
                    db.add(show)
        if dangling_tasks:
            await db.commit()


async def _seed_default_profile() -> None:
    async with AsyncSession(engine) as db:
        if (await db.exec(select(Profile))).first():
            return
        db.add(
            Profile(
                id="1",
                name="Admin",
                avatar_color="from-blue-600 to-indigo-600",
                theme="netflix",
                pin_enabled=False,
                pin_hash=None,
            )
        )
        await db.commit()


async def _recover_catalog_and_languages() -> None:
    async with runtime_work(
        "catalog.startup-recovery",
        [
            EffectClass.DB_WRITE,
            EffectClass.DURABLE_MEDIA_WRITE,
            EffectClass.DISPOSABLE_CACHE_WRITE,
        ],
    ):
        from lifecycle.media_publication import recover_media_operations

        await recover_media_operations()
        await queue_manager.sync_media_from_disk(raise_on_critical_failure=True)
        from services.audio_extractor import repair_completed_ingestion_languages

        await repair_completed_ingestion_languages()


async def _daily_backup_worker() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            if settings.BACKUP_ENABLED:
                from services.backup import (
                    create_backup,
                    get_local_backups_async,
                    prune_old_backups,
                    sync_backups_to_cloud,
                    backup_due,
                )

                backups = await get_local_backups_async()
                should_backup = backup_due(backups)
                if should_backup:
                    logger.info("[Backup Worker] Initiating online daily database backup...")
                    async with runtime_work("backup.daily", [EffectClass.DATABASE_SNAPSHOT]):
                        backup_path = await create_backup()
                        await prune_old_backups(keep_count=7)
                        cloud_synced = None
                        if settings.STORAGE_ENGINE == "CLOUD":
                            cloud_synced = await sync_backups_to_cloud()
                    if cloud_synced is False:
                        logger.error(
                            f"[Backup Worker] Local daily backup completed but cloud synchronization failed: {backup_path}"
                        )
                    else:
                        logger.info(
                            f"[Backup Worker] Daily database backup successfully completed: {backup_path}"
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[Backup Worker] Error in daily backup scheduler: {type(exc).__name__}")
        await asyncio.sleep(3600)


async def _optional_playback_baselines() -> None:
    try:
        await playback_prep_service.schedule_catalog_baselines()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(f"[Lifespan Startup] Optional playback baseline scheduling failed: {type(exc).__name__}")


async def _stop_runtime_workers(runtime: _WorkerRuntime) -> None:
    runtime.update_stop.set()
    runtime.recommendation_stop.set()
    for task in runtime.tasks:
        task.cancel()
    if runtime.tasks:
        await asyncio.gather(*runtime.tasks, return_exceptions=True)
    runtime.tasks.clear()
    await playback_prep_service.shutdown()
    try:
        await queue_manager.stop()
    except Exception:
        pass
    try:
        await vibe_analysis_manager.stop()
    except Exception:
        pass
    try:
        await hevc_compressor.stop()
    except Exception:
        pass
    try:
        from services.tmdb import tmdb_client

        await tmdb_client.stop_cache_workers()
        await tmdb_client.close()
    except Exception:
        pass
    runtime.started = False


async def _start_runtime_workers(runtime: _WorkerRuntime) -> None:
    if runtime.started:
        return
    try:
        queue_manager.start()
        from services.tmdb import tmdb_client

        await tmdb_client.start_cache_workers()
        await vibe_analysis_manager.start()
        hevc_compressor.start()
        runtime.tasks.extend(
            [
                asyncio.create_task(_daily_backup_worker(), name="daily-backup"),
                asyncio.create_task(automatic_update_worker(runtime.update_stop), name="automatic-update"),
                asyncio.create_task(
                    recommendation_worker(runtime.recommendation_stop),
                    name="recommendation-worker",
                ),
                asyncio.create_task(playback_run_reaper(), name="playback-run-reaper"),
            ]
        )
        runtime.started = True
    except BaseException:
        await _stop_runtime_workers(runtime)
        raise


def _normal_recovery_steps() -> tuple[MandatoryRecoveryStep, ...]:
    return (
        MandatoryRecoveryStep(
            "configuration",
            lambda: _initialize_configuration(allow_mutation=True),
            frozenset({ReadinessCapability.AUTHENTICATION}),
        ),
        MandatoryRecoveryStep("runtime_filesystem", _initialize_runtime_filesystem),
        MandatoryRecoveryStep(
            "database",
            lambda: _initialize_database(guarded=False),
            frozenset({ReadinessCapability.DATABASE}),
        ),
        MandatoryRecoveryStep("setup_state", _recover_setup_state),
        MandatoryRecoveryStep(
            "rclone_hardening",
            _harden_rclone_configuration,
            frozenset({ReadinessCapability.CLOUD}),
        ),
        MandatoryRecoveryStep("tmdb_configuration", _report_tmdb_configuration),
        MandatoryRecoveryStep("drive_setup", _recover_drive_setup),
        MandatoryRecoveryStep("interrupted_ingestion", _recover_interrupted_ingestion),
        MandatoryRecoveryStep("default_profile", _seed_default_profile),
        MandatoryRecoveryStep(
            "catalog_and_languages",
            _recover_catalog_and_languages,
            frozenset({ReadinessCapability.CATALOG, ReadinessCapability.PLAYBACK}),
        ),
    )


def _guarded_recovery_steps() -> tuple[MandatoryRecoveryStep, ...]:
    return (
        MandatoryRecoveryStep(
            "configuration",
            lambda: _initialize_configuration(allow_mutation=False),
            frozenset({ReadinessCapability.AUTHENTICATION}),
        ),
        MandatoryRecoveryStep(
            "candidate_database",
            lambda: _initialize_database(guarded=True),
            frozenset({ReadinessCapability.DATABASE}),
        ),
    )


def _close_guarded_admission() -> DrainSnapshot | None:
    if not STARTUP_CONTEXT.guarded:
        return None
    if STARTUP_CONTEXT.operation_id is None:
        raise RuntimeError("guarded startup identity is incomplete")
    if STARTUP_CONTEXT.generation is None:
        # The positively confirmed pre-Phase-1 adoption bridge has no shared
        # generation. Request/worker gating remains fail closed until commit.
        return None
    blocked = [
        effect
        for effect in EffectClass
        if effect.value not in STARTUP_CONTEXT.allowed_effects
    ]
    return get_runtime_coordinator().close_admission(
        blocked,
        operation_id=STARTUP_CONTEXT.operation_id,
        generation=STARTUP_CONTEXT.generation,
    )


async def _promote_committed_update(
    runtime: _WorkerRuntime,
    fence: DrainSnapshot | None,
) -> None:
    while state.UPDATE_TRANSACTION_ID:
        await asyncio.sleep(0.1)

    async def revalidate_and_open_admission() -> None:
        revalidate_startup_context(STARTUP_CONTEXT)
        if fence is not None:
            get_runtime_coordinator().release_fence(fence)

    async def revalidate_after_recovery() -> None:
        revalidate_startup_context(STARTUP_CONTEXT)

    result = await run_mandatory_recovery(
        STARTUP_CONTEXT,
        (MandatoryRecoveryStep("lifecycle_authority", revalidate_and_open_admission),)
        + _normal_recovery_steps()
        + (MandatoryRecoveryStep("lifecycle_authority_final", revalidate_after_recovery),),
    )
    STARTUP_READINESS.record_recovery(result)
    if not result.succeeded:
        logger.error(f"[Lifespan Startup] Post-update recovery failed: {result.error_code}")
        return
    if STARTUP_CONTEXT.operation_id is None or STARTUP_CONTEXT.generation is None:
        STARTUP_READINESS.mark_worker_start_failed("update_lifecycle_identity_missing")
        return
    try:
        completion_fence = get_runtime_coordinator().close_admission(
            list(EffectClass),
            operation_id=STARTUP_CONTEXT.operation_id,
            generation=STARTUP_CONTEXT.generation,
        )
    except Exception:
        STARTUP_READINESS.mark_worker_start_failed("update_completion_fence_failed")
        logger.error("[Lifespan Startup] Update completion fence could not be established.")
        return
    drained = await get_runtime_coordinator().drain(completion_fence, timeout=30.0)
    if not drained.complete:
        STARTUP_READINESS.mark_worker_start_failed("update_completion_drain_timeout")
        return
    try:
        await _start_runtime_workers(runtime)
    except Exception:
        STARTUP_READINESS.mark_worker_start_failed()
        logger.error("[Lifespan Startup] Post-update worker startup failed.")
        return
    STARTUP_READINESS.mark_workers_started()
    STARTUP_READINESS.mark_general_ready()
    while True:
        try:
            current = lifecycle_state.inspect_state(STARTUP_CONTEXT.installation_root)
        except lifecycle_state.LifecycleError:
            current = None
        if (
            current is not None
            and current.active_operation is None
            and current.generation == STARTUP_CONTEXT.generation
        ):
            get_runtime_coordinator().release_fence(completion_fence)
            if state.MAINTENANCE_REASON == "StreamHome is completing guarded startup verification.":
                state.MAINTENANCE_MODE = False
                state.MAINTENANCE_REASON = ""
            break
        operation = current.active_operation if current is not None else None
        if (
            operation is None
            or operation.operation_id != STARTUP_CONTEXT.operation_id
            or current.generation != STARTUP_CONTEXT.generation
            or operation.phase is lifecycle_state.LifecyclePhase.RECOVERY_REQUIRED
        ):
            STARTUP_READINESS.mark_worker_start_failed("update_lifecycle_unresolved")
            await _stop_runtime_workers(runtime)
            return
        await asyncio.sleep(0.1)
    runtime.tasks.append(
        asyncio.create_task(_optional_playback_baselines(), name="playback-baselines")
    )


async def _promote_committed_restore(
    runtime: _WorkerRuntime,
    fence: DrainSnapshot | None,
) -> None:
    """Continue accepted restore/rollback through mandatory normal recovery."""

    while not state.RESTORE_COMMIT_DECISION:
        await asyncio.sleep(0.1)

    async def revalidate_and_open_admission() -> None:
        revalidate_startup_context(STARTUP_CONTEXT)
        if fence is not None:
            get_runtime_coordinator().release_fence(fence)

    async def revalidate_after_recovery() -> None:
        revalidate_startup_context(STARTUP_CONTEXT)

    result = await run_mandatory_recovery(
        STARTUP_CONTEXT,
        (MandatoryRecoveryStep("lifecycle_authority", revalidate_and_open_admission),)
        + _normal_recovery_steps()
        + (MandatoryRecoveryStep("lifecycle_authority_final", revalidate_after_recovery),),
    )
    STARTUP_READINESS.record_recovery(result)
    if not result.succeeded:
        logger.error(f"[Lifespan Startup] Post-restore recovery failed: {result.error_code}")
        return
    if STARTUP_CONTEXT.operation_id is None or STARTUP_CONTEXT.generation is None:
        STARTUP_READINESS.mark_worker_start_failed("restore_lifecycle_identity_missing")
        return
    try:
        completion_fence = get_runtime_coordinator().close_admission(
            list(EffectClass),
            operation_id=STARTUP_CONTEXT.operation_id,
            generation=STARTUP_CONTEXT.generation,
        )
    except Exception:
        STARTUP_READINESS.mark_worker_start_failed("restore_completion_fence_failed")
        logger.error("[Lifespan Startup] Restore completion fence could not be established.")
        return
    drained = await get_runtime_coordinator().drain(completion_fence, timeout=30.0)
    if not drained.complete:
        STARTUP_READINESS.mark_worker_start_failed("restore_completion_drain_timeout")
        return
    try:
        await _start_runtime_workers(runtime)
    except Exception:
        STARTUP_READINESS.mark_worker_start_failed()
        logger.error("[Lifespan Startup] Post-restore worker startup failed.")
        return
    STARTUP_READINESS.mark_workers_started()
    STARTUP_READINESS.mark_general_ready()
    while True:
        try:
            current = lifecycle_state.inspect_state(STARTUP_CONTEXT.installation_root)
        except lifecycle_state.LifecycleError:
            current = None
        if (
            current is not None
            and current.active_operation is None
            and current.generation == STARTUP_CONTEXT.generation
        ):
            get_runtime_coordinator().release_fence(completion_fence)
            if state.MAINTENANCE_REASON == "StreamHome is completing guarded startup verification.":
                state.MAINTENANCE_MODE = False
                state.MAINTENANCE_REASON = ""
            break
        operation = current.active_operation if current is not None else None
        if (
            operation is None
            or operation.operation_id != STARTUP_CONTEXT.operation_id
            or current.generation != STARTUP_CONTEXT.generation
            or operation.phase is lifecycle_state.LifecyclePhase.RECOVERY_REQUIRED
        ):
            STARTUP_READINESS.mark_worker_start_failed("restore_lifecycle_unresolved")
            await _stop_runtime_workers(runtime)
            return
        await asyncio.sleep(0.1)
    runtime.tasks.append(
        asyncio.create_task(_optional_playback_baselines(), name="playback-baselines")
    )


async def _complete_normal_startup(runtime: _WorkerRuntime) -> None:
    async def revalidate_authority() -> None:
        revalidate_startup_context(STARTUP_CONTEXT)

    result = await run_mandatory_recovery(
        STARTUP_CONTEXT,
        (MandatoryRecoveryStep("lifecycle_authority", revalidate_authority),)
        + _normal_recovery_steps(),
    )
    STARTUP_READINESS.record_recovery(result)
    if not result.succeeded:
        state.MAINTENANCE_MODE = True
        state.MAINTENANCE_REASON = "StreamHome mandatory startup recovery failed."
        logger.error(f"[Lifespan Startup] Mandatory recovery failed: {result.error_code}")
        return
    try:
        await _start_runtime_workers(runtime)
    except Exception:
        STARTUP_READINESS.mark_worker_start_failed()
        state.MAINTENANCE_MODE = True
        state.MAINTENANCE_REASON = "StreamHome background worker startup failed."
        logger.error("[Lifespan Startup] Background worker startup failed.")
        return
    STARTUP_READINESS.mark_workers_started()
    STARTUP_READINESS.mark_general_ready()
    runtime.tasks.append(
        asyncio.create_task(_optional_playback_baselines(), name="playback-baselines")
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    runtime = _WorkerRuntime()
    startup_task: asyncio.Task | None = None
    fence: DrainSnapshot | None = None

    async def revalidate_authority() -> None:
        nonlocal fence
        revalidate_startup_context(STARTUP_CONTEXT)
        fence = _close_guarded_admission()

    if STARTUP_CONTEXT.mode is StartupMode.NORMAL:
        revalidate_startup_context(STARTUP_CONTEXT)
        startup_task = asyncio.create_task(
            _complete_normal_startup(runtime),
            name="normal-startup-recovery",
        )
    else:
        state.MAINTENANCE_MODE = True
        state.MAINTENANCE_REASON = "StreamHome is completing guarded startup verification."
        result = await run_mandatory_recovery(
            STARTUP_CONTEXT,
            (MandatoryRecoveryStep("lifecycle_authority", revalidate_authority),)
            + _guarded_recovery_steps(),
        )
        STARTUP_READINESS.record_recovery(result)
        if result.succeeded:
            STARTUP_READINESS.mark_guarded_ready()
            if STARTUP_CONTEXT.mode is StartupMode.UPDATE_VERIFY:
                startup_task = asyncio.create_task(
                    _promote_committed_update(runtime, fence),
                    name="post-update-startup",
                )
            elif STARTUP_CONTEXT.mode is StartupMode.RESTORE_VERIFY:
                startup_task = asyncio.create_task(
                    _promote_committed_restore(runtime, fence),
                    name="post-restore-startup",
                )
        else:
            logger.error(f"[Lifespan Startup] Guarded recovery failed: {result.error_code}")

    try:
        yield
    finally:
        if startup_task is not None:
            startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)
        await _stop_runtime_workers(runtime)


class StartupModeGateMiddleware(BaseHTTPMiddleware):
    """Expose only health/commit surfaces until startup is generally ready."""

    async def dispatch(self, request: Request, call_next):
        readiness = STARTUP_READINESS.projection()
        public_ready = readiness["generalReady"]
        if public_ready and STARTUP_CONTEXT.mode in {
            StartupMode.UPDATE_VERIFY,
            StartupMode.RESTORE_VERIFY,
        }:
            try:
                current = lifecycle_state.inspect_state(STARTUP_CONTEXT.installation_root)
                public_ready = (
                    current.active_operation is None
                    and current.generation == STARTUP_CONTEXT.generation
                )
            except lifecycle_state.LifecycleError:
                public_ready = False
        if public_ready:
            return await call_next(request)
        path = request.url.path
        allowed = path in {"/api/health", "/api/health/live", "/api/health/ready"}
        if STARTUP_CONTEXT.mode is StartupMode.UPDATE_VERIFY and path == "/api/update/commit":
            allowed = True
        if STARTUP_CONTEXT.mode is StartupMode.RESTORE_VERIFY and path == "/api/lifecycle/restore/commit":
            allowed = True
        if allowed:
            return await call_next(request)
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "startup_not_ready",
                    "message": "StreamHome is not accepting normal traffic during guarded startup.",
                }
            },
            headers={"Retry-After": "5"},
        )


class ActivityTrackingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            state.MAINTENANCE_MODE
            and not request.url.path.startswith("/api/health")
            and request.url.path != "/api/update/commit"
            and request.url.path != "/api/lifecycle/restore/commit"
            and (
                bool(state.UPDATE_TRANSACTION_ID)
                or not request.url.path.startswith("/api/backup/restore/")
            )
        ):
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "maintenance_mode",
                        "message": state.MAINTENANCE_REASON or "StreamHome is temporarily unavailable for maintenance.",
                    }
                },
                headers={"Retry-After": "30"},
            )
        if "/api/update" in request.url.path:
            return await call_next(request)

        state.ACTIVE_HTTP_REQUESTS += 1
        state.LAST_HTTP_ACTIVITY_TIMESTAMP = time.time()
        try:
            response = await call_next(request)
            return response
        finally:
            state.ACTIVE_HTTP_REQUESTS = max(0, state.ACTIVE_HTTP_REQUESTS - 1)
            state.LAST_HTTP_ACTIVITY_TIMESTAMP = time.time()

class SetupGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        allowed = (
            request.method == "OPTIONS"
            or path.startswith("/api/setup")
            or path.startswith("/api/health")
            or path == "/api/update/commit"
            or path == "/api/lifecycle/restore/commit"
        )
        if not settings.SETUP_COMPLETE and not allowed:
            return JSONResponse(
                status_code=503,
                content={"detail": {"code": "setup_required", "message": "Complete StreamHome setup before using this endpoint."}},
                headers={"Retry-After": "5"},
            )
        return await call_next(request)


class SecurityBoundaryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if unsafe_cookie_request_requires_same_origin(request) and not same_origin_request(request):
            return JSONResponse(
                status_code=403,
                content={"detail": {"code": "cross_site_request_blocked", "message": "Cross-site authenticated requests are not allowed."}},
            )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return response


class RuntimeWriteCoordinationMiddleware(BaseHTTPMiddleware):
    """Register normal HTTP mutation lifetimes without leasing read-only traffic."""

    async def dispatch(self, request: Request, call_next):
        hidden_setup_write = request.url.path in {
            "/api/setup/readiness",
            "/api/setup/rclone/drive/callback",
        }
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"} and not hidden_setup_write:
            return await call_next(request)
        path = request.url.path
        if path.startswith("/api/backup") or path in {
            "/api/lifecycle/restore/commit",
            "/api/update/commit",
            "/api/update/cutover",
            "/api/update/install",
            "/api/update/pending",
            "/api/update/presence",
        }:
            # Protected controller requests validate their exact operation and
            # capability instead of entering normal mutation admission. Backup
            # operations declare their narrower effects in the backup service.
            return await call_next(request)
        effects = {EffectClass.DB_WRITE}
        if path.startswith("/api/setup") or path in {
            "/api/system/settings",
            "/api/update/policy",
        }:
            effects.add(EffectClass.CONFIG_WRITE)
        try:
            async with runtime_work("http.mutation", effects):
                return await call_next(request)
        except AdmissionClosedError:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "lifecycle_work_blocked",
                        "message": "StreamHome is not accepting this type of work during maintenance.",
                    }
                },
                headers={"Retry-After": "5"},
            )

app = FastAPI(title="StreamHome Media Server", version=settings.APP_VERSION, lifespan=lifespan)


@app.exception_handler(OperationalError)
async def database_operational_error_handler(request: Request, exc: OperationalError):
    del request
    message = str(exc).lower()
    if "database is locked" in message or "database table is locked" in message:
        logger.warning("[Database] Request rejected because SQLite is temporarily busy.")
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "database_busy", "message": "The server database is temporarily busy. Try again."}},
            headers={"Retry-After": "1"},
        )
    logger.error(f"[Database] Operational request failure: {type(exc).__name__}")
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": "database_unavailable", "message": "The server database is unavailable."}},
        headers={"Retry-After": "5"},
    )


@app.exception_handler(AdmissionClosedError)
async def runtime_admission_error_handler(request: Request, exc: AdmissionClosedError):
    del request, exc
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": "lifecycle_work_blocked",
                "message": "StreamHome is not accepting this type of work during maintenance.",
            }
        },
        headers={"Retry-After": "5"},
    )

app.add_middleware(ActivityTrackingMiddleware)
app.add_middleware(RuntimeWriteCoordinationMiddleware)
app.add_middleware(SetupGateMiddleware)
app.add_middleware(SecurityBoundaryMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins()),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(StartupModeGateMiddleware)

app.include_router(queue_router)
app.include_router(auth_router)
app.include_router(health_router)
app.include_router(playback_router)
app.include_router(media_updates_router)
app.include_router(backup_router, prefix="/api/backup", tags=["backup"])
app.include_router(update_router, prefix="/api/update", tags=["update"])
app.include_router(lifecycle_router, prefix="/api/lifecycle", tags=["lifecycle"])
app.include_router(setup_router)
app.include_router(admin_profiles_router)

from fastapi.responses import FileResponse, StreamingResponse
import re
from routes.playback import cloud_file_size, open_cloud_chunks
from services.cloud_cache import ACTIVE_CLOUD_DOWNLOADS, download_file_from_cloud_task
from services.rclone import rclone_service
from services.media_source import is_safe_presentation_asset, local_path_for

@app.get("/media/{file_path:path}")
async def serve_media_file(file_path: str, request: Request):
    file_path = file_path.lstrip("/")
    catalog_path = f"/media/{file_path.replace(chr(92), '/')}"
    if not is_safe_presentation_asset(catalog_path):
        raise HTTPException(status_code=403, detail="Direct access is limited to safe presentation assets.")
    try:
        abs_path = str(local_path_for(catalog_path))
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if os.path.exists(abs_path):
        return FileResponse(abs_path)

    if settings.STORAGE_ENGINE == "CLOUD":
        target_remote = f"{settings.RCLONE_REMOTE_PATH}/{file_path.replace('\\', '/')}"

        if abs_path not in ACTIVE_CLOUD_DOWNLOADS:
            ACTIVE_CLOUD_DOWNLOADS.add(abs_path)
            asyncio.create_task(download_file_from_cloud_task(target_remote, abs_path))

        file_size = await cloud_file_size(target_remote)
        chunks = await open_cloud_chunks(target_remote, 0, file_size)
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        return StreamingResponse(
            chunks,
            status_code=200,
            media_type=content_type,
            headers={"Content-Length": str(file_size), "Cache-Control": "public, max-age=300"},
        )

    raise HTTPException(status_code=404, detail="File not found")


# ----------------- Movies Catalog API -----------------

@asynccontextmanager
async def _catalog_session(profile_id: Optional[str], auth_session: AuthSession):
    """Keep personalized ranking ownership until its transaction is settled."""
    async with AsyncExitStack() as work:
        db = AsyncSession(engine)
        try:
            if profile_id:
                await require_profile_access(db, auth_session, profile_id)
                await work.enter_async_context(
                    runtime_work("catalog.personalized", [EffectClass.DB_WRITE])
                )
            yield db
        finally:
            # Session close rolls back unfinished ranking writes and returns its
            # connection. A shield alone can leave that cleanup running after
            # repeated cancellation has already released the work lease.
            _, cancelled = await settle(db.close())
            if cancelled:
                raise asyncio.CancelledError


@app.get("/api/movies", response_model=List[MovieResponse])
async def get_movies(
    profile_id: Optional[str] = Query(None),
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    """Fetches all cataloged media assets with linked episode detail mappings, optionally personalized."""
    async with _catalog_session(profile_id, auth_session) as db:
        stmt = select(Movie)
        result = await db.exec(stmt)
        movies = result.all()

        ep_result = await db.exec(select(Episode).order_by(Episode.season_number, Episode.episode_number))
        episodes_by_movie = {}
        for episode in ep_result.all():
            episodes_by_movie.setdefault(episode.movie_id, []).append(episode)
        results = [(m, MovieResponse.from_db(m, episodes_by_movie.get(m.id))) for m in movies]

        if profile_id:
            ranked = await rank_movies_for_profile(db, profile_id, movies, episodes_by_movie)
            responses = {movie.id: response for movie, response in results}
            ordered_responses = [responses[movie.id] for _, movie, _, _, _ in ranked]
            await accepted_commit(db)
            return ordered_responses

        return [res for m, res in results]

@app.get("/api/movies/featured", response_model=Optional[MovieResponse])
async def get_featured_movie(user = Depends(get_current_user)):
    """Returns the featured or most recently cataloged movie asset."""
    async with AsyncSession(engine) as db:
        stmt = select(Movie).order_by(Movie.release_year.desc())
        result = await db.exec(stmt)
        movie = result.first()

        if not movie:
            return None

        episodes = None
        if movie.type == "series":
            ep_stmt = select(Episode).where(Episode.movie_id == movie.id).order_by(Episode.season_number, Episode.episode_number)
            ep_result = await db.exec(ep_stmt)
            episodes = ep_result.all()

        return MovieResponse.from_db(movie, episodes)


@app.get("/api/movies/{media_id}", response_model=MovieResponse)
async def get_movie(media_id: str, user = Depends(get_current_user)):
    """Resolve one canonical playable or metadata-only catalog record."""
    async with AsyncSession(engine) as db:
        movie = await db.get(Movie, media_id)
        if not movie:
            raise HTTPException(status_code=404, detail="Media not found")
        episodes = None
        if movie.type == "series":
            result = await db.exec(select(Episode).where(Episode.movie_id == movie.id).order_by(Episode.season_number, Episode.episode_number))
            episodes = list(result.all())
        return MovieResponse.from_db(movie, episodes)


# ----------------- Playback Tracking & Pulse -----------------

async def authorize_profile(profile_id: str, auth_session: AuthSession) -> None:
    async with AsyncSession(engine) as db:
        await require_profile_access(db, auth_session, profile_id)


@app.get("/api/track/{profile_id}", response_model=List[PlaybackSessionResponse])
async def get_playback_tracking(
    profile_id: str,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    """Retrieves continue-watching tracking playback states for a profile."""
    async with AsyncSession(engine) as db:
        await require_profile_access(db, auth_session, profile_id)
        stmt = select(PlaybackSession).where(PlaybackSession.profile_id == profile_id)
        result = await db.exec(stmt)
        sessions = result.all()
        return [
            PlaybackSessionResponse(
                movieId=s.movie_id,
                profileId=s.profile_id,
                timestamp=s.timestamp,
                duration_watched=s.duration_watched,
                completion_rate=s.completion_rate,
                updatedAt=s.updated_at,
                episodeId=s.episode_id,
                is_finished=s.is_finished
            )
            for s in sessions
        ]

@app.post("/api/telemetry")
async def handle_telemetry(
    request: TelemetryRequest,
    profile_id: str = Query(...),
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    """Receives generic tracking telemetry from the web UI."""
    await authorize_profile(profile_id, auth_session)
    accepted = await process_telemetry_event(profile_id, request)
    return {"status": "accepted" if accepted else "ignored", "accepted": accepted}

# ----------------- Watchlist Management API -----------------

from pydantic import BaseModel

class WatchlistToggleRequest(BaseModel):
    profile_id: str
    movie_id: str

@app.get("/api/recommendations/{profile_id}", response_model=RecommendationFeedResponse)
async def get_recommendations(
    profile_id: str,
    scope: str = Query("home"),
    category: str = Query("recommended"),
    limit: int = Query(48, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    """Return a personalized mixed cached/server catalog for the future web client."""
    if scope not in {"home", "movies", "series"}:
        raise HTTPException(status_code=400, detail="Invalid recommendation scope")
    async with _catalog_session(profile_id, auth_session) as db:
        payload = await build_recommendation_payload(db, profile_id, scope, category, limit, offset)
        return RecommendationFeedResponse(**payload)

@app.get("/api/recommendations/{profile_id}/preferences")
async def list_recommendation_preferences(
    profile_id: str,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    await authorize_profile(profile_id, auth_session)
    return {"preferences": await get_media_preferences(profile_id)}

@app.put("/api/recommendations/{profile_id}/preferences/{movie_id}")
async def update_recommendation_preference(
    profile_id: str,
    movie_id: str,
    request: MediaPreferenceRequest,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    await authorize_profile(profile_id, auth_session)
    try:
        return await set_media_preference(profile_id, movie_id, request.preference)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))

@app.post("/api/recommendations/{profile_id}/exposures")
async def add_recommendation_exposures(
    profile_id: str,
    request: RecommendationExposureBatch,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    await authorize_profile(profile_id, auth_session)
    return {"accepted": await record_recommendation_exposures(profile_id, request.exposures)}

@app.get("/api/recommendations/{profile_id}/onboarding")
async def read_recommendation_onboarding(
    profile_id: str,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    await authorize_profile(profile_id, auth_session)
    return await get_onboarding_preferences(profile_id)

@app.put("/api/recommendations/{profile_id}/onboarding")
async def update_recommendation_onboarding(
    profile_id: str,
    request: RecommendationOnboardingRequest,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    await authorize_profile(profile_id, auth_session)
    try:
        return await set_onboarding_preferences(profile_id, request.genres, request.title_ids)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))

@app.get("/api/recommendations/{profile_id}/diagnostics")
async def recommendation_diagnostics(
    profile_id: str,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    await authorize_profile(profile_id, auth_session)
    return await get_recommendation_diagnostics(profile_id)

@app.post("/api/recommendations/{profile_id}/rebuild")
async def rebuild_recommendations(
    profile_id: str,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    async with AsyncSession(engine) as db:
        await require_profile_access(db, auth_session, profile_id)
        await persist_profile_pool(db, profile_id)
        await db.commit()
    return {"status": "rebuilt"}

@app.delete("/api/recommendations/{profile_id}/preferences")
async def clear_recommendation_preferences(
    profile_id: str,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    await authorize_profile(profile_id, auth_session)
    return {"cleared": await reset_media_preferences(profile_id)}

@app.get("/api/watchlist/{profile_id}", response_model=List[str])
async def get_watchlist(
    profile_id: str,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    """Retrieves watchlist items for a profile."""
    async with AsyncSession(engine) as db:
        await require_profile_access(db, auth_session, profile_id)
        stmt = select(WatchlistItem).where(WatchlistItem.profile_id == profile_id)
        result = await db.exec(stmt)
        items = result.all()
        items.sort(key=lambda x: x.created_at, reverse=True)
        return [item.movie_id for item in items]

@app.post("/api/watchlist/toggle")
async def toggle_watchlist(
    req: WatchlistToggleRequest,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    """Toggles movie presence in the profile's server watchlist."""
    async with AsyncSession(engine) as db:
        await require_profile_access(db, auth_session, req.profile_id)
        stmt = select(WatchlistItem).where(
            WatchlistItem.profile_id == req.profile_id,
            WatchlistItem.movie_id == req.movie_id
        )
        result = await db.exec(stmt)
        item = result.first()

        if item:
            await db.delete(item)
            status = "removed"
        else:
            item = WatchlistItem(
                profile_id=req.profile_id,
                movie_id=req.movie_id,
                created_at=datetime.utcnow().isoformat()
            )
            db.add(item)
            status = "added"

        await db.commit()

        stmt_all = select(WatchlistItem).where(WatchlistItem.profile_id == req.profile_id)
        result_all = await db.exec(stmt_all)
        items_all = result_all.all()
        items_all.sort(key=lambda x: x.created_at, reverse=True)
        response = {"status": status, "watchlist": [x.movie_id for x in items_all]}

    await record_authoritative_signal(
        req.profile_id,
        req.movie_id,
        "watchlist_add" if status == "added" else "watchlist_remove",
    )
    return response


@app.get("/api/discover", response_model=List[DiscoverMovieResponse])
async def get_discover_movies(
    category: str = "action",
    type: str = "movie",
    profile_id: Optional[str] = Query(None),
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    """Fetches trending movies or series from TMDB for the discover rows."""
    from services.tmdb import tmdb_client
    if profile_id:
        await authorize_profile(profile_id, auth_session)
    return await tmdb_client.discover_media(category, type, profile_id)

@app.get("/api/search", response_model=List[DiscoverMovieResponse])
async def search_tmdb_movies(query: str, user = Depends(get_current_user)):
    """Searches movies from TMDB for search suggestion results and caches posters/backdrops."""
    from services.tmdb import tmdb_client
    if not query:
        return []
    return await tmdb_client.search_media(query)

@app.get("/api/tmdb/{media_type}/{tmdb_id}")
async def get_tmdb_metadata(media_type: str, tmdb_id: int, user = Depends(get_current_user)):
    """Fetch detailed movie or TV show metadata from TMDB API."""
    from services.tmdb import tmdb_client
    try:
        if media_type.lower() in ("series", "tv"):
            data = await tmdb_client.fetch_show_metadata(tmdb_id)
        else:
            data = await tmdb_client.fetch_movie_metadata(tmdb_id)
        return data
    except Exception as e:
        logger.error(f"[API] Failed to fetch TMDB metadata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/series/{tmdb_id}/episodes", response_model=List[EpisodeResponse])
async def get_series_episodes(tmdb_id: int, user = Depends(get_current_user)):
    """Fetches real seasons and episodes for a TV series from TMDB, enriched with local catalog data if available."""
    from services.tmdb import tmdb_client

    async with AsyncSession(engine) as db:
        stmt = select(Episode).where(Episode.movie_id == f"tv_{tmdb_id}").order_by(Episode.season_number, Episode.episode_number)
        result = await db.exec(stmt)
        local_episodes = result.all()

    local_map = {
        (e.season_number, e.episode_number): e
        for e in local_episodes
    }

    try:
        show_data = await tmdb_client._get(f"/tv/{tmdb_id}")
        if not show_data:
            raise HTTPException(status_code=404, detail="TV Series not found in TMDB")

        seasons = show_data.get("seasons", [])
        active_seasons = []
        today = datetime.now().date()
        for s in seasons:
            if s.get("season_number", 0) <= 0:
                continue
            air_date = s.get("air_date")
            if air_date:
                try:
                    air_dt = datetime.strptime(air_date, "%Y-%m-%d").date()
                    if air_dt > today:
                        continue
                except ValueError:
                    pass
            active_seasons.append(s)
        if not active_seasons and seasons:
            active_seasons = seasons

        all_episodes = []

        async def fetch_season_episodes(season_num: int):
            season_data = await tmdb_client._get(f"/tv/{tmdb_id}/season/{season_num}")
            if not season_data or not season_data.get("episodes"):
                return []

            eps = []
            for ep in season_data.get("episodes", []):
                ep_air_date = ep.get("air_date")
                if ep_air_date:
                    try:
                        ep_air_dt = datetime.strptime(ep_air_date, "%Y-%m-%d").date()
                        if ep_air_dt > today:
                            continue
                    except ValueError:
                        pass
                ep_num = ep.get("episode_number", 1)
                still_path = ep.get("still_path")
                thumbnail_url = f"https://image.tmdb.org/t/p/w300{still_path}" if still_path else ""

                runtime = ep.get("runtime", 0) or 45
                duration_str = f"{runtime}m"

                local_ep = local_map.get((season_num, ep_num))
                if local_ep:
                    eps.append(
                        EpisodeResponse(
                            id=local_ep.id,
                            episode_number=local_ep.episode_number,
                            season_number=local_ep.season_number,
                            title=local_ep.title,
                            description=local_ep.description,
                            thumbnail_url=local_ep.thumbnail_url or thumbnail_url,
                            video_url="" if local_ep.preview_task_id else local_ep.video_url,
                            duration=local_ep.duration,
                            preview_task_id=local_ep.preview_task_id,
                        )
                    )
                else:
                    eps.append(
                        EpisodeResponse(
                            id=f"ep_{tmdb_id}_s{season_num}_e{ep_num}",
                            episode_number=ep_num,
                            season_number=season_num,
                            title=ep.get("name") or f"Episode {ep_num}",
                            description=ep.get("overview") or "",
                            thumbnail_url=thumbnail_url,
                            video_url="",
                            duration=duration_str
                        )
                    )
            return eps

        tasks = [fetch_season_episodes(s.get("season_number", 1)) for s in active_seasons]
        results = await asyncio.gather(*tasks)

        for r in results:
            all_episodes.extend(r)

        all_episodes.sort(key=lambda x: (x.season_number, x.episode_number))
        return all_episodes

    except Exception as e:
        print(f"[API Series] Error fetching seasons/episodes from TMDB for {tmdb_id}: {e}")
        return [
            EpisodeResponse(
                id=e.id,
                episode_number=e.episode_number,
                season_number=e.season_number,
                title=e.title,
                description=e.description,
                thumbnail_url=e.thumbnail_url,
                video_url="" if e.preview_task_id else e.video_url,
                duration=e.duration,
                preview_task_id=e.preview_task_id,
            )
            for e in local_episodes
        ]

class ProfileSaveRequest(APIModel):
    id: str
    name: str
    avatar_color: Optional[str] = "from-blue-600 to-indigo-650"
    theme: Optional[str] = "netflix"
    pin_enabled: Optional[bool] = False
    pin: Optional[str] = None


class ProfileUnlockRequest(APIModel):
    pin: str


@app.get("/api/profiles", response_model=List[ProfileResponse])
async def get_profiles(user = Depends(get_current_user)):
    """Retrieves all profile records from the database."""
    async with AsyncSession(engine) as db:
        stmt = select(Profile)
        result = await db.exec(stmt)
        profiles = result.all()
        return [
            ProfileResponse(
                id=p.id,
                name=p.name,
                avatar_color=p.avatar_color,
                theme=p.theme,
                pin_enabled=bool(p.pin_enabled and p.pin_hash)
            )
            for p in profiles
        ]

@app.post("/api/profiles", response_model=ProfileResponse)
async def save_profile(req: ProfileSaveRequest, user = Depends(get_current_user)):
    """Creates a new profile or updates an existing profile configuration in the database."""
    from sqlalchemy import case, func, update

    clean_name = req.name.strip()
    if not clean_name or len(clean_name) > 40:
        raise HTTPException(status_code=422, detail={"code": "invalid_profile_name", "message": "Profile names must contain between 1 and 40 characters."})
    protection_requested = "pin_enabled" in req.model_fields_set
    pin_hash: Optional[str] = None
    if protection_requested and req.pin_enabled and req.pin:
        try:
            # Do not hold the SQLite writer while computing the password hash.
            pin_hash = hash_profile_pin(req.pin)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_profile_pin", "message": str(exc)}) from exc

    async with AsyncSession(engine) as db:
        values = {"name": clean_name, "avatar_color": req.avatar_color, "theme": req.theme}
        stmt = update(Profile).where(Profile.id == req.id)
        current_version = func.coalesce(Profile.pin_version, 0)
        has_hash = Profile.pin_hash.is_not(None) & (Profile.pin_hash != "")
        protected = Profile.pin_enabled.is_(True) & has_hash
        if protection_requested:
            if req.pin_enabled:
                values["pin_enabled"] = True
                if pin_hash:
                    values["pin_hash"] = pin_hash
                    values["pin_version"] = current_version + 1
                else:
                    # A form that retains its PIN must retain the current stored
                    # hash, including a concurrent save's newer PIN.
                    stmt = stmt.where(has_hash)
                    values["pin_version"] = case((protected, current_version), else_=current_version + 1)
            else:
                values["pin_enabled"] = False
                values["pin_hash"] = None
                values["pin_version"] = case((protected, current_version + 1), else_=current_version)

        # The first database operation acquires the writer. Compute every PIN
        # generation from the stored row in this same transaction, never from an
        # ORM snapshot read before another profile save committed.
        result = await db.execute(stmt.values(**values).execution_options(synchronize_session=False))
        profile = await db.get(Profile, req.id)
        if result.rowcount == 0 and profile:
            raise HTTPException(status_code=422, detail={"code": "profile_pin_required", "message": "Enter a 4 to 8 digit PIN before enabling profile protection."})
        if profile is None:
            if req.pin_enabled and not pin_hash:
                raise HTTPException(status_code=422, detail={"code": "profile_pin_required", "message": "Enter a 4 to 8 digit PIN before enabling profile protection."})
            profile = Profile(
                id=req.id,
                name=clean_name,
                avatar_color=req.avatar_color,
                theme=req.theme,
                pin_enabled=bool(req.pin_enabled),
                pin_hash=pin_hash,
                pin_version=1 if req.pin_enabled and pin_hash else 0,
            )
            db.add(profile)

        await db.commit()
        await db.refresh(profile)
        return ProfileResponse(
            id=profile.id,
            name=profile.name,
            avatar_color=profile.avatar_color,
            theme=profile.theme,
            pin_enabled=bool(profile.pin_enabled and profile.pin_hash),
        )


@app.post("/api/profiles/{profile_id}/select")
async def select_profile(
    profile_id: str,
    auth_session: AuthSession = Depends(get_current_session),
    user = Depends(get_current_user),
):
    """Selects an unprotected profile or reuses this session's current protected-profile grant."""
    async with AsyncSession(engine) as db:
        profile = await require_profile_access(db, auth_session, profile_id)
        await grant_profile_access(db, auth_session, profile)
        return {"selected": True}


@app.post("/api/profiles/{profile_id}/unlock")
async def unlock_profile(
    profile_id: str,
    req: ProfileUnlockRequest,
    request: Request,
    user = Depends(get_current_user),
    auth_session: AuthSession = Depends(get_current_session),
):
    """Verifies a protected profile PIN without returning or exposing the stored hash."""
    identity = f"{user.id}:{profile_id}:{client_ip(request)}"
    async with AsyncSession(engine) as db:
        await enforce_rate_limit(db, "profile_pin", identity)
        profile = await db.get(Profile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail={"code": "profile_not_found", "message": "That profile does not exist."})
        # Clearing a previous failure can commit and expire the ORM profile.
        # Preserve exactly the protection generation whose PIN we verified; a
        # concurrent PIN change must never upgrade this grant to its new version.
        verified_profile = Profile(
            id=profile.id, name=profile.name, pin_enabled=profile.pin_enabled,
            pin_hash=profile.pin_hash, pin_version=profile.pin_version,
        )
        if not verified_profile.pin_enabled or not verified_profile.pin_hash:
            await clear_rate_limit(db, "profile_pin", identity)
            await grant_profile_access(db, auth_session, verified_profile)
            return {"verified": True}
        if not verify_profile_pin(req.pin, profile.pin_hash):
            await fail_rate_limit(db, "profile_pin", identity, limit=5, window_seconds=300)
            raise HTTPException(status_code=401, detail={"code": "invalid_profile_pin", "message": "The profile PIN was not accepted."})
        await clear_rate_limit(db, "profile_pin", identity)
        await grant_profile_access(db, auth_session, verified_profile)
        return {"verified": True}

@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str, user = Depends(get_current_user)):
    """Deletes a profile from the database."""
    from sqlalchemy import update

    if profile_id == "1":
        raise HTTPException(status_code=403, detail={"code": "permanent_profile", "message": "The administrator profile cannot be deleted."})
    async with AsyncSession(engine) as db:
        # Acquire the writer before any profile/grant lookup. A grant that
        # committed first is cleared by this UPDATE; a later grant cannot pass
        # its exact protection predicate after this transaction deletes the row.
        await db.execute(
            update(AuthSession)
            .where(AuthSession.selected_profile_id == profile_id)
            .values(selected_profile_id=None, selected_profile_pin_version=None)
            .execution_options(synchronize_session=False)
        )
        stmt = select(Profile).where(Profile.id == profile_id)
        result = await db.exec(stmt)
        profile = result.first()
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        attempts = (
            await db.exec(select(ViewingAttempt).where(ViewingAttempt.profile_id == profile_id))
        ).all()
        attempt_ids = [attempt.id for attempt in attempts]
        if attempt_ids:
            await db.execute(delete(PlaybackMilestone).where(PlaybackMilestone.attempt_id.in_(attempt_ids)))
        profile_models = (
            PlaybackSession,
            PlaybackRun,
            WatchlistItem,
            TelemetryEvent,
            ProfileTaste,
            ProfileVibeVector,
            ProfileMediaPreference,
            ProfileOnboardingPreference,
            RecommendationExposure,
            ViewingAttempt,
            ProfileRecommendation,
            RecommendationRefreshState,
            RecommendationRuntimeMetric,
        )
        for model in profile_models:
            await db.execute(delete(model).where(model.profile_id == profile_id))
        await db.delete(profile)
        await db.commit()
        return {"status": "deleted"}


# ----------------- System Settings API -----------------

class SystemSettingsRequest(APIModel):
    storage_engine: str
    rclone_remote_path: str
    hevc_compression_mode: str = "auto"

class SystemSettingsResponse(APIModel):
    storage_engine: str
    rclone_remote_path: str
    hevc_compression_mode: str
    drive_configured: bool = False
    drive_reachable: Optional[bool] = None
    drive_error_code: Optional[str] = None
    drive_health_state: str = "unknown"
    google_drive_audience: str = "external"
    google_drive_publishing_status: str = "production"

async def _drive_settings_status() -> tuple[bool, Optional[bool], Optional[str]]:
    configured = rclone_service.config_path.exists() and ":" in settings.RCLONE_REMOTE_PATH
    if not configured:
        return False, None, None
    result = await rclone_service.run("about", settings.RCLONE_REMOTE_PATH.split(":", 1)[0] + ":", "--json", timeout=12)
    return True, result.ok, result.error_code

@app.get("/api/system/settings", response_model=SystemSettingsResponse)
async def get_system_settings(user = Depends(get_current_user)):
    """Retrieves current server storage engine and Rclone settings."""
    drive_configured, drive_reachable, drive_error_code = await _drive_settings_status()
    return SystemSettingsResponse(
        storage_engine=settings.STORAGE_ENGINE,
        rclone_remote_path=settings.RCLONE_REMOTE_PATH,
        hevc_compression_mode=settings.HEVC_COMPRESSION_MODE,
        drive_configured=drive_configured,
        drive_reachable=drive_reachable,
        drive_error_code=drive_error_code,
        drive_health_state=str(rclone_service.cloud_health()["state"]),
        google_drive_audience=settings.GOOGLE_DRIVE_AUDIENCE,
        google_drive_publishing_status=settings.GOOGLE_DRIVE_PUBLISHING_STATUS,
    )

@app.post("/api/system/settings", response_model=SystemSettingsResponse)
async def save_system_settings(req: SystemSettingsRequest, session = Depends(require_recent_reauth)):
    """Updates server storage engine settings and persists them to settings.json."""
    if req.storage_engine not in ["LOCAL", "CLOUD"]:
        raise HTTPException(status_code=400, detail="Invalid storage engine value. Must be LOCAL or CLOUD.")

    if req.hevc_compression_mode not in ["auto", "on", "off"]:
        raise HTTPException(status_code=400, detail="Invalid hevc compression mode. Must be auto, on, or off.")
    if req.storage_engine == "CLOUD":
        if req.rclone_remote_path != settings.RCLONE_REMOTE_PATH:
            raise HTTPException(status_code=400, detail="Google Drive targets must be changed through the guided Drive connection flow.")
        configured, reachable, error_code = await _drive_settings_status()
        if not configured or not reachable:
            raise HTTPException(status_code=422, detail={"code": error_code or "drive_not_configured", "message": "Connect and test Google Drive before enabling cloud storage."})

    previous = (settings.STORAGE_ENGINE, settings.RCLONE_REMOTE_PATH, settings.HEVC_COMPRESSION_MODE)
    settings.STORAGE_ENGINE = req.storage_engine
    settings.RCLONE_REMOTE_PATH = req.rclone_remote_path
    settings.HEVC_COMPRESSION_MODE = req.hevc_compression_mode
    try:
        settings.save_to_json()
    except OSError as exc:
        settings.STORAGE_ENGINE, settings.RCLONE_REMOTE_PATH, settings.HEVC_COMPRESSION_MODE = previous
        raise HTTPException(status_code=500, detail={"code": "settings_save_failed", "message": "Server settings could not be saved."}) from exc
    drive_configured, drive_reachable, drive_error_code = await _drive_settings_status()
    return SystemSettingsResponse(
        storage_engine=settings.STORAGE_ENGINE,
        rclone_remote_path=settings.RCLONE_REMOTE_PATH,
        hevc_compression_mode=settings.HEVC_COMPRESSION_MODE,
        drive_configured=drive_configured,
        drive_reachable=drive_reachable,
        drive_error_code=drive_error_code,
        drive_health_state=str(rclone_service.cloud_health()["state"]),
        google_drive_audience=settings.GOOGLE_DRIVE_AUDIENCE,
        google_drive_publishing_status=settings.GOOGLE_DRIVE_PUBLISHING_STATUS,
    )

@app.post("/api/system/drive/test")
async def test_system_drive(session = Depends(require_recent_reauth)):
    configured, reachable, error_code = await _drive_settings_status()
    if not configured:
        raise HTTPException(status_code=409, detail={"code": "drive_not_configured", "message": "Google Drive has not been configured."})
    if not reachable:
        raise HTTPException(status_code=422, detail={"code": error_code or "drive_unreachable", "message": "Google Drive could not be reached."})
    return {"configured": True, "reachable": True, "remotePath": settings.RCLONE_REMOTE_PATH}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
