"""Side-effect-free lifecycle evaluation and startup readiness primitives.

This module is the first StreamHome-owned code imported by the supported ASGI
entry point.  It deliberately depends only on the Python standard library and the
durable lifecycle authority in :mod:`lifecycle.state`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Awaitable, Callable, Mapping, Sequence
import uuid

from . import state
from .layout import layout_for_root


STARTUP_MODE_ENV = "STREAMHOME_LIFECYCLE_STARTUP_MODE"
OPERATION_ID_ENV = "STREAMHOME_LIFECYCLE_OPERATION_ID"
GENERATION_ENV = "STREAMHOME_LIFECYCLE_GENERATION"
INSTANCE_ROOT_ENV = "STREAMHOME_INSTANCE_ROOT"
FIRST_ADOPTION_ENV = "STREAMHOME_LIFECYCLE_FIRST_ADOPTION"
UPDATE_TRANSACTION_ENV = "STREAMHOME_UPDATE_TRANSACTION"
UPDATE_COMMIT_TOKEN_ENV = "STREAMHOME_UPDATE_COMMIT_TOKEN"


class StartupMode(str, Enum):
    NORMAL = "normal"
    UPDATE_VERIFY = "update_verify"
    RESTORE_VERIFY = "restore_verify"
    MANAGEMENT_VERIFY = "management_verify"
    RECOVERY_ONLY = "recovery_only"
    FAIL_CLOSED = "fail_closed"


class StartupAuthorityChangedError(RuntimeError):
    code = "startup_authority_changed"


class ReadinessCapability(str, Enum):
    DATABASE = "database"
    AUTHENTICATION = "authentication"
    CATALOG = "catalog"
    PLAYBACK = "playback"
    CLOUD = "cloud"
    BACKGROUND_WORK = "background_work"


ALL_RUNTIME_EFFECTS = frozenset(
    {
        "db_write",
        "database_snapshot",
        "durable_media_write",
        "cloud_write",
        "disposable_cache_write",
        "playback_stream",
        "config_write",
    }
)


@dataclass(frozen=True, slots=True)
class StartupContext:
    mode: StartupMode
    installation_root: Path
    inspected_schema_version: int | None
    generation: int | None
    operation_id: str | None
    operation_type: str | None
    lifecycle_phase: str | None
    owner_kind: str | None
    guarded_identity_validated: bool
    allowed_effects: frozenset[str]
    failure_code: str | None = None
    adoption_target_commit: str | None = None

    @property
    def imports_application(self) -> bool:
        return self.mode in {
            StartupMode.NORMAL,
            StartupMode.UPDATE_VERIFY,
            StartupMode.RESTORE_VERIFY,
            StartupMode.MANAGEMENT_VERIFY,
        }

    @property
    def guarded(self) -> bool:
        return self.mode in {StartupMode.UPDATE_VERIFY, StartupMode.RESTORE_VERIFY, StartupMode.MANAGEMENT_VERIFY}


@dataclass(frozen=True, slots=True)
class RecoveryStepResult:
    name: str
    succeeded: bool
    error_code: str | None
    capabilities: frozenset[ReadinessCapability]


@dataclass(frozen=True, slots=True)
class StartupRecoveryResult:
    completed: bool
    succeeded: bool
    mode: StartupMode
    required_steps: tuple[str, ...]
    completed_steps: tuple[str, ...]
    failed_step: str | None
    error_code: str | None
    capabilities: frozenset[ReadinessCapability]
    steps: tuple[RecoveryStepResult, ...]


RecoveryCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MandatoryRecoveryStep:
    name: str
    callback: RecoveryCallback
    capabilities: frozenset[ReadinessCapability] = frozenset()


def _safe_root(environ: Mapping[str, str]) -> Path:
    supplied = str(environ.get(INSTANCE_ROOT_ENV) or "").strip()
    return state.resolve_installation_root(supplied or None)


def _failure_context(
    root: Path,
    code: str,
    inspected: state.LifecycleState | None = None,
) -> StartupContext:
    operation = inspected.active_operation if inspected is not None else None
    return StartupContext(
        mode=StartupMode.FAIL_CLOSED,
        installation_root=root,
        inspected_schema_version=(inspected.schema_version if inspected is not None else None),
        generation=(inspected.generation if inspected is not None else None),
        operation_id=(operation.operation_id if operation is not None else None),
        operation_type=(operation.operation_type.value if operation is not None else None),
        lifecycle_phase=(operation.phase.value if operation is not None else None),
        owner_kind=(operation.owner_kind.value if operation is not None else None),
        guarded_identity_validated=False,
        allowed_effects=frozenset(),
        failure_code=code,
    )


def _legacy_first_adoption_context(
    root: Path,
    inspected: state.LifecycleState,
    environ: Mapping[str, str],
) -> StartupContext | None:
    if (
        str(environ.get(FIRST_ADOPTION_ENV) or "").lower() != "true"
        or inspected.generation != 0
        or inspected.active_operation is not None
    ):
        return None
    transaction = str(environ.get(UPDATE_TRANSACTION_ENV) or "").strip()
    commit_token = str(environ.get(UPDATE_COMMIT_TOKEN_ENV) or "").strip()
    try:
        if str(uuid.UUID(transaction)) != transaction or not commit_token:
            return None
    except (ValueError, AttributeError):
        return None
    journal_path = layout_for_root(root).durable_runtime / "update-state.json"
    from .update_controller import UpdateControllerError, read_update_journal

    try:
        payload = read_update_journal(journal_path)
    except UpdateControllerError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("transaction_id") != transaction
        or payload.get("lifecycle_generation") not in (None, "")
        or str(payload.get("lifecycle_phase") or "")
        or not isinstance(payload.get("target_commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", payload["target_commit"])
        or payload["target_commit"][:12] != environ.get("STREAMHOME_BUILD_ID")
    ):
        return None
    return StartupContext(
        mode=StartupMode.UPDATE_VERIFY,
        installation_root=root,
        inspected_schema_version=inspected.schema_version,
        generation=None,
        operation_id=transaction,
        operation_type=state.OperationType.UPDATE.value,
        lifecycle_phase=None,
        owner_kind=state.OwnerKind.UPDATER.value,
        guarded_identity_validated=True,
        allowed_effects=frozenset({"db_write"}),
        adoption_target_commit=payload["target_commit"],
    )


def _evaluate_startup(
    root: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    install: bool = False,
) -> StartupContext:
    """Inspect lifecycle authority and return one immutable startup decision."""

    selected_environ = os.environ if environ is None else environ
    try:
        resolved_root = state.resolve_installation_root(root) if root is not None else _safe_root(selected_environ)
    except state.LifecycleError:
        fallback = Path(root or selected_environ.get(INSTANCE_ROOT_ENV) or Path(__file__).resolve().parents[2])
        context = _failure_context(fallback, "invalid_root")
        if install:
            install_startup_context(context)
        return context

    try:
        inspected = state.inspect_state(resolved_root)
    except state.LifecycleError as exc:
        context = _failure_context(resolved_root, exc.code)
        if install:
            install_startup_context(context)
        return context

    requested_raw = str(selected_environ.get(STARTUP_MODE_ENV) or "").strip()
    operation_id = str(selected_environ.get(OPERATION_ID_ENV) or "").strip()
    generation_raw = str(selected_environ.get(GENERATION_ENV) or "").strip()
    operation = inspected.active_operation

    if operation is None:
        first_adoption = None
        if requested_raw == StartupMode.UPDATE_VERIFY.value and not operation_id and not generation_raw:
            first_adoption = _legacy_first_adoption_context(resolved_root, inspected, selected_environ)
        if first_adoption is not None:
            context = first_adoption
        elif requested_raw or operation_id or generation_raw:
            context = _failure_context(resolved_root, "unexpected_guarded_permit", inspected)
        else:
            context = StartupContext(
                mode=StartupMode.NORMAL,
                installation_root=resolved_root,
                inspected_schema_version=inspected.schema_version,
                generation=inspected.generation,
                operation_id=None,
                operation_type=None,
                lifecycle_phase=None,
                owner_kind=None,
                guarded_identity_validated=False,
                allowed_effects=ALL_RUNTIME_EFFECTS,
            )
        if install:
            install_startup_context(context)
        return context

    if operation.operation_type is state.OperationType.MANAGEMENT:
        context = _failure_context(resolved_root, "management_startup_unavailable", inspected)
        if requested_raw == StartupMode.MANAGEMENT_VERIFY.value:
            try:
                from .management_journal import ActivationMode, ManagementStage, execution_tuple_sha256, validate_active_journal
                from .management_store import inspect_management_journal
                journal = inspect_management_journal(resolved_root, operation.operation_id)
                validate_active_journal(journal, inspected)
                lease = state.read_controller_lease(resolved_root)
                if (
                    operation_id != operation.operation_id or generation_raw != str(inspected.generation)
                    or operation.phase is not state.LifecyclePhase.VERIFYING
                    or journal.activation_mode is not ActivationMode.CUTOVER
                    or journal.stage not in {ManagementStage.VERIFYING, ManagementStage.ACCEPTED, ManagementStage.SUCCEEDED}
                    or selected_environ.get("STREAMHOME_BUILD_ID") != journal.candidate.application_build_id
                    or selected_environ.get("STREAMHOME_RUNTIME_TUPLE_SHA256") != execution_tuple_sha256(journal.candidate)
                    or lease is None or not state.controller_lease_is_current(lease)
                    or lease.operation_id != operation.operation_id or lease.generation != inspected.generation
                    or lease.owner_kind is not state.OwnerKind.MANAGEMENT_CONTROLLER
                ):
                    raise ValueError("Management verification does not match its current owner and candidate.")
                state.validate_operation_with_credential(
                    resolved_root, operation_id=operation.operation_id, generation=inspected.generation,
                    expected_phase=state.LifecyclePhase.VERIFYING,
                )
                context = StartupContext(
                    StartupMode.MANAGEMENT_VERIFY, resolved_root, inspected.schema_version, inspected.generation,
                    operation.operation_id, operation.operation_type.value, operation.phase.value,
                    operation.owner_kind.value, True, frozenset({"db_write"}),
                )
            except (state.LifecycleError, ValueError, OSError):
                context = _failure_context(resolved_root, "management_guarded_permit_mismatch", inspected)
        if install:
            install_startup_context(context)
        return context

    try:
        requested = StartupMode(requested_raw)
        generation = int(generation_raw)
    except (TypeError, ValueError):
        context = _failure_context(resolved_root, "guarded_permit_missing", inspected)
        if install:
            install_startup_context(context)
        return context

    expected_modes: dict[state.OperationType, StartupMode] = {
        state.OperationType.UPDATE: StartupMode.UPDATE_VERIFY,
        state.OperationType.RESTORE: StartupMode.RESTORE_VERIFY,
    }
    expected_owners: dict[state.OperationType, state.OwnerKind] = {
        state.OperationType.UPDATE: state.OwnerKind.UPDATER,
        state.OperationType.RESTORE: state.OwnerKind.RESTORE_CONTROLLER,
    }
    expected_mode = expected_modes[operation.operation_type]
    if operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
        expected_mode = StartupMode.RECOVERY_ONLY
    if (
        requested is not expected_mode
        or operation.owner_kind is not expected_owners[operation.operation_type]
        or operation_id != operation.operation_id
        or generation != inspected.generation
    ):
        context = _failure_context(resolved_root, "guarded_permit_mismatch", inspected)
        if install:
            install_startup_context(context)
        return context
    try:
        state.validate_operation_with_credential(
            resolved_root,
            operation_id=operation.operation_id,
            generation=inspected.generation,
            expected_phase=operation.phase,
        )
    except state.LifecycleError as exc:
        context = _failure_context(resolved_root, exc.code, inspected)
        if install:
            install_startup_context(context)
        return context

    allowed = (
        frozenset({"db_write"})
        if requested in {StartupMode.UPDATE_VERIFY, StartupMode.RESTORE_VERIFY}
        else frozenset()
    )
    context = StartupContext(
        mode=requested,
        installation_root=resolved_root,
        inspected_schema_version=inspected.schema_version,
        generation=inspected.generation,
        operation_id=operation.operation_id,
        operation_type=operation.operation_type.value,
        lifecycle_phase=operation.phase.value,
        owner_kind=operation.owner_kind.value,
        guarded_identity_validated=True,
        allowed_effects=allowed,
    )
    if install:
        install_startup_context(context)
    return context


def evaluate_startup(
    root: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None,
    *, install: bool = False,
) -> StartupContext:
    """Apply managed launch identity checks before application import as well."""
    context = _evaluate_startup(root, environ, install=False)
    selected = os.environ if environ is None else environ
    if context.imports_application:
        try:
            from .layout import layout_for_root
            layout = layout_for_root(context.installation_root)
            if layout.managed:
                from .control.processes import RUNTIME_CGROUP, process_cgroup
                from .control.tickets import require_first_start
                from .execution_state import read_active_execution
                from .installation import InstallationStatus
                from .management_journal import execution_tuple_sha256
                from .management_store import inspect_management_journal
                from .runtime_build import file_sha256
                execution = (
                    inspect_management_journal(context.installation_root, context.operation_id).candidate
                    if context.mode is StartupMode.MANAGEMENT_VERIFY
                    else read_active_execution(context.installation_root).execution
                )
                import sys
                if (
                    os.geteuid() != layout.descriptor.descriptor.service_uid
                    or process_cgroup(os.getpid()) != RUNTIME_CGROUP
                    or selected.get("STREAMHOME_BUILD_ID") != execution.application_build_id
                    or selected.get("STREAMHOME_RUNTIME_TUPLE_SHA256") != execution_tuple_sha256(execution)
                    or sys.executable != execution.python_executable
                    or file_sha256(layout.application / "runtime/bundles" / execution.runtime_bundle_id / "manifest.json") != execution.runtime_manifest_sha256
                ):
                    raise ValueError("The managed launch does not match its bound runtime.")
                if layout.descriptor.descriptor.status is InstallationStatus.PROVISIONING:
                    require_first_start(layout, execution, selected.get("INVOCATION_ID", ""))
        except (OSError, ValueError, RuntimeError):
            context = _failure_context(context.installation_root, "managed_launch_unavailable", state.inspect_state(context.installation_root))
    if install:
        install_startup_context(context)
    return context


def revalidate_startup_context(
    context: StartupContext,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Require lifecycle authority to still match the pre-import decision."""

    current = evaluate_startup(context.installation_root, environ, install=False)
    if current != context:
        raise StartupAuthorityChangedError(
            "lifecycle authority changed after the pre-import startup decision"
        )


def require_current_guarded_authority(
    context: StartupContext,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Require the exact guarded operation and credential to remain authoritative."""

    if not context.guarded or not context.guarded_identity_validated:
        raise StartupAuthorityChangedError("guarded lifecycle authority is unavailable")
    revalidate_startup_context(context, environ)


def guarded_operation_completed(
    context: StartupContext,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Prove completion without manufacturing shared authority for legacy adoption."""
    from .update_controller import UpdateControllerError, read_update_journal

    try:
        current = state.inspect_state(context.installation_root)
        if current.active_operation is not None:
            return False
        if context.generation is not None:
            if context.mode is StartupMode.MANAGEMENT_VERIFY:
                from .management_journal import ManagementStage
                from .management_store import inspect_management_journal
                journal = inspect_management_journal(context.installation_root, context.operation_id)
                return (
                    current.generation == context.generation and journal.generation == context.generation
                    and journal.stage is ManagementStage.SUCCEEDED and journal.acceptance is not None
                )
            return current.generation == context.generation
        if (
            context.mode is not StartupMode.UPDATE_VERIFY
            or not context.guarded_identity_validated
            or context.operation_type != state.OperationType.UPDATE.value
            or context.owner_kind != state.OwnerKind.UPDATER.value
            or context.lifecycle_phase is not None
            or not context.adoption_target_commit
            or current.generation != 0
        ):
            return False
        require_current_guarded_authority(context, environ)
        journal = layout_for_root(context.installation_root).durable_runtime / "update-state.json"
        payload = read_update_journal(journal)
        committed_at = payload.get("runtime_committed_at")
        return (
            payload.get("transaction_id") == context.operation_id
            and payload.get("target_commit") == context.adoption_target_commit
            and payload.get("current_commit") == context.adoption_target_commit
            and payload.get("lifecycle_generation") in (None, "")
            and not payload.get("lifecycle_phase")
            and payload.get("phase") in {"starting", "recovering", "succeeded"}
            and payload.get("runtime_committed") is True
            and type(committed_at) in {int, float}
            and math.isfinite(committed_at)
            and committed_at > 0
        )
    except (state.LifecycleError, StartupAuthorityChangedError, UpdateControllerError,
            OSError, ValueError, TypeError, AttributeError, OverflowError):
        return False


async def run_mandatory_recovery(
    context: StartupContext,
    steps: Sequence[MandatoryRecoveryStep],
) -> StartupRecoveryResult:
    required = tuple(step.name for step in steps)
    completed: list[str] = []
    results: list[RecoveryStepResult] = []
    capabilities: set[ReadinessCapability] = set()
    for step in steps:
        try:
            await step.callback()
        except asyncio.CancelledError:
            results.append(RecoveryStepResult(step.name, False, "cancelled", frozenset()))
            return StartupRecoveryResult(
                completed=True,
                succeeded=False,
                mode=context.mode,
                required_steps=required,
                completed_steps=tuple(completed),
                failed_step=step.name,
                error_code="cancelled",
                capabilities=frozenset(capabilities),
                steps=tuple(results),
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "") or f"{step.name}_failed")
            results.append(RecoveryStepResult(step.name, False, code, frozenset()))
            return StartupRecoveryResult(
                completed=True,
                succeeded=False,
                mode=context.mode,
                required_steps=required,
                completed_steps=tuple(completed),
                failed_step=step.name,
                error_code=code,
                capabilities=frozenset(capabilities),
                steps=tuple(results),
            )
        completed.append(step.name)
        capabilities.update(step.capabilities)
        results.append(RecoveryStepResult(step.name, True, None, step.capabilities))
    return StartupRecoveryResult(
        completed=True,
        succeeded=True,
        mode=context.mode,
        required_steps=required,
        completed_steps=tuple(completed),
        failed_step=None,
        error_code=None,
        capabilities=frozenset(capabilities),
        steps=tuple(results),
    )


class StartupReadiness:
    """Process-local projection of startup progress; never an authority source."""

    def __init__(self, context: StartupContext):
        self._context = context
        self._lock = threading.RLock()
        self._recovery: StartupRecoveryResult | None = None
        self._guarded_ready = False
        self._general_ready = False
        self._workers_started = False
        self._startup_error_code: str | None = None

    @property
    def context(self) -> StartupContext:
        return self._context

    def record_recovery(self, result: StartupRecoveryResult) -> None:
        if result.mode is not self._context.mode:
            raise ValueError("recovery result mode does not match startup context")
        with self._lock:
            self._recovery = result
            self._guarded_ready = False
            self._general_ready = False
            self._workers_started = False
            self._startup_error_code = None
            if not result.succeeded:
                self._startup_error_code = result.error_code

    def mark_guarded_ready(self) -> None:
        with self._lock:
            if self._recovery is None or not self._recovery.succeeded or not self._context.guarded:
                raise RuntimeError("guarded readiness prerequisites are incomplete")
            self._guarded_ready = True

    def mark_workers_started(self) -> None:
        with self._lock:
            if self._recovery is None or not self._recovery.succeeded:
                raise RuntimeError("worker readiness prerequisites are incomplete")
            self._workers_started = True
            self._startup_error_code = None

    def mark_worker_start_failed(self, error_code: str = "worker_start_failed") -> None:
        with self._lock:
            self._workers_started = False
            self._guarded_ready = False
            self._general_ready = False
            self._startup_error_code = error_code

    def mark_general_ready(self) -> None:
        with self._lock:
            if self._recovery is None or not self._recovery.succeeded or not self._workers_started:
                raise RuntimeError("general readiness prerequisites are incomplete")
            self._guarded_ready = False
            self._general_ready = True

    def projection(self) -> dict[str, Any]:
        with self._lock:
            recovery = self._recovery
            capabilities = sorted(item.value for item in (recovery.capabilities if recovery else ()))
            if self._workers_started and ReadinessCapability.BACKGROUND_WORK.value not in capabilities:
                capabilities.append(ReadinessCapability.BACKGROUND_WORK.value)
                capabilities.sort()
            return {
                "live": True,
                "mode": self._context.mode.value,
                "recoveryCompleted": bool(recovery and recovery.completed),
                "recoverySucceeded": bool(recovery and recovery.succeeded),
                "guardedReady": self._guarded_ready,
                "generalReady": self._general_ready,
                "workersStarted": self._workers_started,
                "capabilities": capabilities,
                "errorCode": (
                    self._startup_error_code
                    or (recovery.error_code if recovery is not None and not recovery.succeeded else None)
                    or self._context.failure_code
                ),
            }


def current_readiness_projection(
    readiness: StartupReadiness,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return readiness with guarded truth revalidated against durable authority."""

    projection = readiness.projection()
    if projection["guardedReady"]:
        try:
            require_current_guarded_authority(readiness.context, environ)
        except StartupAuthorityChangedError:
            projection = dict(projection)
            projection["guardedReady"] = False
            projection["errorCode"] = StartupAuthorityChangedError.code
    from .runtime_admission import managed_runtime_admitted
    managed_admitted = managed_runtime_admitted(readiness.context) if projection["generalReady"] else None
    if projection["generalReady"] and managed_admitted is False:
        projection = dict(projection)
        projection["generalReady"] = False
        projection["errorCode"] = "runtime_not_settled"
    if projection["generalReady"] and managed_admitted is None and readiness.context.mode in {
        StartupMode.UPDATE_VERIFY,
        StartupMode.RESTORE_VERIFY,
        StartupMode.MANAGEMENT_VERIFY,
    }:
        lifecycle_completed = guarded_operation_completed(readiness.context, environ)
        if not lifecycle_completed:
            projection = dict(projection)
            projection["generalReady"] = False
            projection["capabilities"] = [
                capability
                for capability in projection["capabilities"]
                if capability != ReadinessCapability.BACKGROUND_WORK.value
            ]
            projection["errorCode"] = f"{readiness.context.operation_type}_lifecycle_pending"
    return projection


_startup_context: StartupContext | None = None
_startup_readiness: StartupReadiness | None = None
_startup_lock = threading.Lock()


def install_startup_context(context: StartupContext) -> None:
    global _startup_context, _startup_readiness
    with _startup_lock:
        if _startup_context is not None and _startup_context != context:
            raise RuntimeError("startup context is already installed")
        if _startup_context is None:
            _startup_context = context
            _startup_readiness = StartupReadiness(context)


def require_startup_context() -> StartupContext:
    if _startup_context is None:
        raise RuntimeError("application import requires evaluated lifecycle startup context")
    return _startup_context


def get_startup_readiness() -> StartupReadiness:
    if _startup_readiness is None:
        raise RuntimeError("startup readiness is unavailable before lifecycle evaluation")
    return _startup_readiness


def public_startup_projection(context: StartupContext) -> dict[str, Any]:
    return {
        "decision": context.mode.value,
        "schema_version": context.inspected_schema_version,
        "generation": context.generation,
        "active_operation": (
            None
            if context.operation_id is None
            else {
                "operation_id": context.operation_id,
                "operation_type": context.operation_type,
                "phase": context.lifecycle_phase,
                "owner_kind": context.owner_kind,
            }
        ),
        "guarded_identity_validated": context.guarded_identity_validated,
        "error": context.failure_code,
    }


class MinimalStatusApplication:
    """Dependency-free fail-closed ASGI status surface."""

    def __init__(self, context: StartupContext):
        self.context = context

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope_type != "http":
            return
        path = str(scope.get("path") or "")
        live = path == "/api/health/live"
        status = 200 if live else 503
        payload = {
            "status": "live" if live else "unavailable",
            "mode": self.context.mode.value,
            "errorCode": self.context.failure_code,
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", b"5"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
