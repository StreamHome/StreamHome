from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from . import state


EXIT_OK = 0
EXIT_INVALID_ARGUMENTS = 2
EXIT_CORRUPT_STATE = 3
EXIT_OWNERSHIP_MISMATCH = 4
EXIT_CONFLICT = 5
EXIT_UNSUPPORTED_SCHEMA = 6
EXIT_PERSISTENCE = 7
EXIT_INVALID_ROOT = 8


class LifecycleArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ValueError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = LifecycleArgumentParser(prog="streamhome-lifecycle")
    parser.add_argument("--root", type=Path, help="StreamHome installation root")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("inspect")
    commands.add_parser("startup-decision")
    commands.add_parser("startup-allowed")
    commands.add_parser("restore-recovery-needed")
    commands.add_parser("update-recovery-needed")
    commands.add_parser("update-recovery-required")

    claim = commands.add_parser("claim")
    claim.add_argument("--operation-type", required=True, choices=[item.value for item in state.OperationType])
    claim.add_argument("--owner-kind", required=True, choices=[item.value for item in state.OwnerKind])
    claim.add_argument("--operation-id")

    validate_owner = commands.add_parser("validate-owner")
    _add_expected_owner_arguments(validate_owner, phase_required=False)

    complete = commands.add_parser("complete")
    _add_expected_owner_arguments(complete)
    complete.add_argument(
        "--expected-operation-type",
        choices=[item.value for item in state.OperationType],
    )

    transition = commands.add_parser("transition")
    _add_expected_owner_arguments(transition)
    transition.add_argument("--next-phase", required=True, choices=[item.value for item in state.LifecyclePhase])
    return parser


def _add_expected_owner_arguments(
    parser: argparse.ArgumentParser,
    *,
    phase_required: bool = True,
) -> None:
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument(
        "--expected-phase",
        required=phase_required,
        choices=[item.value for item in state.LifecyclePhase],
    )


def _emit(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _success(command: str, lifecycle_state: state.LifecycleState) -> dict[str, Any]:
    return {"ok": True, "command": command, "state": state.public_projection(lifecycle_state)}


def _run(arguments: argparse.Namespace) -> dict[str, Any]:
    root = arguments.root
    if arguments.command == "inspect":
        return _success("inspect", state.inspect_state(root))
    if arguments.command == "startup-decision":
        # Preserve strict authority error exits while delegating startup policy to
        # the same bootstrap evaluator used by the ASGI entry point.
        state.inspect_state(root)
        from .bootstrap import evaluate_startup, public_startup_projection

        context = evaluate_startup(root, install=False)
        return {
            "ok": True,
            "command": "startup-decision",
            **public_startup_projection(context),
        }
    if arguments.command == "startup-allowed":
        state.inspect_state(root)
        from .bootstrap import StartupMode, evaluate_startup, public_startup_projection

        context = evaluate_startup(root, install=False)
        if context.mode not in {StartupMode.NORMAL, StartupMode.UPDATE_VERIFY}:
            raise state.LifecycleConflictError("normal startup is not authorized")
        return {"ok": True, "command": "startup-allowed", **public_startup_projection(context)}
    if arguments.command == "restore-recovery-needed":
        current = state.inspect_state(root)
        operation = current.active_operation
        if operation is None or operation.operation_type is not state.OperationType.RESTORE:
            raise state.LifecycleConflictError("restore recovery is not active")
        if operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
            raise state.LifecycleConflictError("restore recovery requires operator action")
        return _success("restore-recovery-needed", current)
    if arguments.command == "update-recovery-needed":
        current = state.inspect_state(root)
        operation = current.active_operation
        if operation is None or operation.operation_type is not state.OperationType.UPDATE:
            raise state.LifecycleConflictError("update recovery is not active")
        if operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
            raise state.LifecycleConflictError("update recovery requires operator action")
        return _success("update-recovery-needed", current)
    if arguments.command == "update-recovery-required":
        current = state.inspect_state(root)
        operation = current.active_operation
        if (
            operation is None
            or operation.operation_type is not state.OperationType.UPDATE
            or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED
        ):
            raise state.LifecycleConflictError("update operator recovery is not required")
        return _success("update-recovery-required", current)
    if arguments.command == "claim":
        result = state.claim_operation_with_credential(
            root,
            operation_type=arguments.operation_type,
            owner_kind=arguments.owner_kind,
            operation_id=arguments.operation_id,
        )
        return _success("claim", result.state)
    if arguments.command == "validate-owner":
        current = state.validate_operation_with_credential(
            root,
            operation_id=arguments.operation_id,
            generation=arguments.generation,
            expected_phase=arguments.expected_phase,
        )
        return _success("validate-owner", current)
    if arguments.command == "transition":
        current = state.transition_operation_with_credential(
            root,
            operation_id=arguments.operation_id,
            generation=arguments.generation,
            expected_phase=arguments.expected_phase,
            next_phase=arguments.next_phase,
        )
        return _success("transition", current)
    if arguments.command == "complete":
        result = state.complete_operation_with_credential(
            root,
            operation_id=arguments.operation_id,
            generation=arguments.generation,
            expected_phase=arguments.expected_phase,
            expected_operation_type=getattr(arguments, "expected_operation_type", None),
        )
        response = _success("complete", result.state)
        if result.credential_cleanup_error:
            response["warning"] = result.credential_cleanup_error
        return response
    raise ValueError("invalid_arguments")


def _error_exit(error: Exception) -> int:
    if isinstance(error, state.UnsupportedSchemaError):
        code, exit_code = error.code, EXIT_UNSUPPORTED_SCHEMA
    elif isinstance(error, state.CorruptStateError):
        code, exit_code = error.code, EXIT_CORRUPT_STATE
    elif isinstance(error, state.OwnershipError):
        code, exit_code = error.code, EXIT_OWNERSHIP_MISMATCH
    elif isinstance(error, state.LifecycleConflictError):
        code, exit_code = error.code, EXIT_CONFLICT
    elif isinstance(error, state.PersistenceError):
        code, exit_code = error.code, EXIT_PERSISTENCE
    elif isinstance(error, state.InvalidRootError):
        code, exit_code = error.code, EXIT_INVALID_ROOT
    else:
        code, exit_code = "invalid_arguments", EXIT_INVALID_ARGUMENTS
    _emit({"ok": False, "error": code}, stream=sys.stderr)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments: argparse.Namespace | None = None
    try:
        arguments = _parser().parse_args(argv)
        _emit(_run(arguments))
        return EXIT_OK
    except (ValueError, state.LifecycleError) as exc:
        command = arguments.command if arguments is not None else ""
        if command == "startup-decision" and isinstance(
            exc, (state.CorruptStateError, state.InvalidRootError, state.PersistenceError)
        ):
            _emit({"ok": False, "command": "startup-decision", "decision": "fail_closed", "error": exc.code})
        return _error_exit(exc)


if __name__ == "__main__":
    raise SystemExit(main())
