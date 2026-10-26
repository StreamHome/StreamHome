"""Fixed installed command dispatch; root never imports the application tree."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time

from ..installation import InstallationEvidenceError, InstallationStatus, inspect_installation
from ..terminal import InputRequiredError, UserCancelled, open_console
from .rpc import ControlError, request


CONTROL = Path("/usr/local/libexec/streamhome")
_OS_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "SYSTEMD_PAGER": "cat", "SYSTEMD_COLORS": "0"}
_RESCUE_MODE = False


def require_root() -> None:
    if os.geteuid() != 0:
        raise ControlError("control_root_required")


def descriptor():
    snapshot = inspect_installation()
    if snapshot is None:
        raise ControlError("control_installation_missing")
    selected = snapshot.descriptor
    account = pwd.getpwuid(selected.service_uid)
    if account.pw_name != "streamhome" or account.pw_gid != selected.service_gid:
        raise ControlError("control_service_account_changed")
    return selected


def systemctl(*arguments: str, timeout: int = 15) -> str:
    # Call sites below provide fixed operations/unit names. No shell, supplied
    # executable, arbitrary unit or systemd property is accepted by the CLI.
    result = subprocess.run(["/usr/bin/systemctl", "--no-pager", *arguments], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_OS_ENV, cwd="/", timeout=timeout)
    if result.returncode or len(result.stdout) > 32768 or len(result.stderr) > 32768:
        raise ControlError("control_systemd_failed")
    return result.stdout.decode("utf-8", "strict").strip()


def unit_snapshot() -> dict[str, str]:
    output = systemctl("show", "streamhome.service", "--property=LoadState,ActiveState,SubState,MainPID,InvocationID,UnitFileState")
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if set(values) != {"LoadState", "ActiveState", "SubState", "MainPID", "InvocationID", "UnitFileState"}:
        raise ControlError("control_unit_invalid")
    if not re.fullmatch(r"[0-9]+", values["MainPID"]):
        raise ControlError("control_unit_invalid")
    if values["InvocationID"] and not re.fullmatch(r"[0-9a-f]{32}", values["InvocationID"]):
        raise ControlError("control_unit_invalid")
    return values


def supervisor_snapshot(selected, unit: dict | None = None) -> dict:
    unit = unit or unit_snapshot()
    if unit["ActiveState"] not in {"active", "activating"} or int(unit["MainPID"]) <= 0 or not unit["InvocationID"]:
        raise ControlError("control_service_stopped")
    value = request(selected.paths.runtime / "supervisor.sock", expected_uid=selected.service_uid,
                    expected_pid=int(unit["MainPID"]), installation_id=selected.installation_id,
                    invocation=unit["InvocationID"], action="snapshot", arguments={})
    if value.get("pid") != int(unit["MainPID"]) or value.get("invocation") != unit["InvocationID"] or value.get("installation_id") != selected.installation_id:
        raise ControlError("control_incarnation_changed")
    return value


def run_worker(arguments: list[str], *, payload: dict | None = None, timeout: int = 30,
               reader_operation_id: str | None = None, reader_rescue: bool = False,
               control_repair_review: bool = False) -> dict:
    """Launch the installed reader after dropping all supplementary groups/UID."""
    require_root()
    selected = descriptor()
    if (type(control_repair_review) is not bool or control_repair_review and (reader_operation_id is not None
            or reader_rescue or len(arguments)!=2 or arguments[0]!='bound-control-review')):
        raise ControlError('control_operation_reader_changed')
    if control_repair_review and (not isinstance(arguments[1],str)
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',arguments[1])
            or arguments[1]=='00000000-0000-0000-0000-000000000000'):
        raise ControlError('control_operation_invalid')
    if (type(reader_rescue) is not bool or reader_rescue and (reader_operation_id is None
            or not arguments or arguments[0] not in {'bound-control-review','bound-control-prepare','bound-control-observe','bound-control-result'})):
        raise ControlError('control_operation_reader_changed')
    reader = ['--worker']
    if reader_operation_id is not None:
        if (not isinstance(reader_operation_id,str)
                or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',reader_operation_id)
                or reader_operation_id == '00000000-0000-0000-0000-000000000000'):
            raise ControlError('control_operation_invalid')
        reader = ['--operation-rescue-worker' if reader_rescue else '--operation-worker',reader_operation_id]
    if control_repair_review:
        reader=['--control-repair-review-worker']
        arguments=arguments[1:]
    rescue = ['--rescue'] if _RESCUE_MODE and reader_operation_id is None and not control_repair_review else []
    result = subprocess.run(["/usr/bin/python3", "-I", str(CONTROL / "provision"), *rescue, *reader, *arguments],
                            input=b"" if payload is None else json.dumps(payload, allow_nan=False).encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/", env=_OS_ENV,
                            user=selected.service_uid, group=selected.service_gid, extra_groups=[], umask=0o077, timeout=timeout)
    if result.returncode or len(result.stdout) > 32768 or len(result.stderr) > 32768:
        raise ControlError("control_worker_failed")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ControlError("control_worker_invalid")
    return value


def bootstrap_code(*, renew: bool) -> None:
    require_root()
    # Open the controlling terminal before retrieving any secret. A pipe or
    # redirected stdout is never a bootstrap-code delivery channel.
    with open_console() as console:
        selected = descriptor()
        if selected.status is not InstallationStatus.READY:
            raise ControlError("control_installation_incomplete")
        unit = unit_snapshot()
        supervisor = supervisor_snapshot(selected, unit)
        backend = supervisor.get("children", {}).get("backend")
        if not isinstance(backend, dict) or type(backend.get("pid")) is not int or backend["pid"] <= 0:
            raise ControlError("control_runtime_unavailable")
        value = request(selected.paths.runtime / "backend.sock", expected_uid=selected.service_uid,
                        expected_pid=backend["pid"], installation_id=selected.installation_id,
                        invocation=unit["InvocationID"], action="bootstrap", arguments={"renew": renew})
        if (set(value) != {"code", "expiresAt", "remainingSeconds"}
                or not isinstance(value["code"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value["code"])
                or type(value["expiresAt"]) is not int or type(value["remainingSeconds"]) is not int
                or not 0 < value["remainingSeconds"] <= 900):
            raise ControlError("control_reply_invalid")
        console.write("One-time StreamHome bootstrap code:")
        console.write(value["code"])
        console.write(f"Expires in {value['remainingSeconds']} seconds. Existing unlocked setup sessions remain valid.")


def print_status(*, detail: bool = False, manifest: dict | None = None, manifest_sha256: str | None = None) -> int:
    if inspect_installation() is None:
        if os.geteuid() != 0:
            print("StreamHome registration is absent. Use sudo streamhome doctor to inspect a retained installation intent.")
            return 3
        from .provisioning import read_intent, report_continuation
        try:
            intent = read_intent()
        except ControlError as exc:
            if exc.code != "control_ticket_unavailable":
                raise
            print("No registered StreamHome installation or readable provisioning intent was found.")
            return 3
        print("StreamHome registration is incomplete.")
        print("Recorded installation stage: " + intent.stage)
        if manifest is not None and manifest_sha256 is not None:
            report_continuation(manifest=manifest, manifest_sha256=manifest_sha256)
        return 3
    selected = descriptor()
    unit = unit_snapshot()
    print(f"StreamHome: {unit['ActiveState']} ({unit['SubState']})")
    print(f"Installation: {selected.status.value}; application /opt/streamhome; state /var/lib/streamhome")
    print(f"Start at boot: {unit['UnitFileState']}")
    if os.geteuid() == 0:
        runtime_problem = unit["ActiveState"] == "failed"
        try:
            snapshot = supervisor_snapshot(selected, unit)
            print("Runtime: " + (snapshot.get("error_code") or ("running" if snapshot.get("children") else "maintenance")))
            runtime_problem = runtime_problem or bool(snapshot.get("error_code"))
            backend = snapshot.get("children", {}).get("backend")
            if isinstance(backend, dict):
                health = request(selected.paths.runtime / "backend.sock", expected_uid=selected.service_uid,
                    expected_pid=backend["pid"], installation_id=selected.installation_id,
                    invocation=unit["InvocationID"], action="snapshot", arguments={}, timeout=3)
                readiness = health.get("readiness")
                ready = isinstance(readiness, dict) and readiness.get("generalReady") is True and health.get("admitted") is True
                print("Application readiness: " + ("general" if ready else "not settled"))
                runtime_problem = runtime_problem or not ready
            else:
                print("Application readiness: maintenance")
                runtime_problem = True
        except ControlError as exc:
            print("Runtime: " + exc.code)
            runtime_problem = runtime_problem or unit["ActiveState"] in {"active", "activating"}
        if selected.status is InstallationStatus.PROVISIONING:
            from .provisioning import read_intent, report_continuation
            intent = read_intent()
            if intent.installation_id != selected.installation_id:
                raise ControlError("control_installation_identity_changed")
            print("Recorded installation stage: " + intent.stage)
            if manifest is not None and manifest_sha256 is not None:
                report_continuation(manifest=manifest, manifest_sha256=manifest_sha256)
            # A missing active execution is expected before preparation commits;
            # do not require application state just to diagnose this stage.
            return 3
        if detail:
            result = run_worker(["inspect"], timeout=120)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1 if runtime_problem or result.get("runtime_verified") is not True else 0
    return 3 if selected.status is InstallationStatus.PROVISIONING else 0


def cli(arguments: list[str], *, manifest: dict, manifest_sha256: str) -> int:
    result=_cli(arguments,manifest=manifest,manifest_sha256=manifest_sha256)
    if result==0 and arguments and arguments[0] in {'start','configure','recover','repair'} and '--review' not in arguments:
        from .cloudflare_connector import queue_after_change
        queue_after_change(manifest=manifest)
    return result


def _cli(arguments: list[str], *, manifest: dict, manifest_sha256: str) -> int:
    parser = argparse.ArgumentParser(prog="streamhome", description="Manage the fixed StreamHome installation and inspect its systemd service.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="Show installation and service status.")
    commands.add_parser("version", help="Show control identity; sudo also reads the private accepted application build.")
    commands.add_parser("doctor", help="Inspect installation, runtime and lifecycle evidence without changing it.")
    commands.add_parser("start", help="Start streamhome.service.")
    commands.add_parser("stop", help="Stop streamhome.service and close runtime launch admission.")
    tunnel=commands.add_parser('tunnel',help='Register or inspect isolated existing Cloudflare tunnel connectors.')
    tunnel_commands=tunnel.add_subparsers(dest='tunnel_command')
    tunnel_commands.add_parser('list',help='List protected connector references without secrets.')
    tunnel_commands.add_parser('status',help='Show owned connector services; routing is verified separately.')
    add=tunnel_commands.add_parser('add',help='Register an inactive connector from a private existing token file.')
    add.add_argument('--token-file',type=Path,required=True)
    add.add_argument('--name',default='Existing tunnel')
    configuration = commands.add_parser("configure", help="Review deployment/boot settings and choose activation or save only.")
    configuration.add_argument("--request", type=Path, help="Apply an explicit revision-bound noninteractive JSON request.")
    repair = commands.add_parser("repair", help="Review dependency repair or an interrupted first-start attempt.")
    repair.add_argument("--request", type=Path, help="Explicit noninteractive repair request with current identity and consent.")
    repair.add_argument('--operation', help='Repair only exact dependencies bound to this unresolved operation.')
    repair.add_argument('--review', action='store_true', help='Print the exact bound repair plan without mutation.')
    repair.add_argument('--control-artifact',type=Path,
        help='Restore only an exact signed control copy bound to --operation, using its independent rescue reader.')
    recovery = commands.add_parser("recover", help="Review recovery of an interrupted canonical operation.")
    recovery.add_argument("--operation", required=True, help="The exact unresolved operation UUID.")
    recovery_action = recovery.add_mutually_exclusive_group()
    recovery_action.add_argument('--rollback', dest='recovery_action', action='store_const', const='rollback',
        help='Review guarded restoration of the previous runtime before candidate acceptance.')
    recovery_action.add_argument('--abort', dest='recovery_action', action='store_const', const='safe_abort',
        help='Review guarded restoration for an unaccepted reservation that has not entered cutover.')
    recovery_input = recovery.add_mutually_exclusive_group()
    recovery_input.add_argument("--request", type=Path, help="An exact evidence-bound recovery consent request.")
    recovery_input.add_argument("--review", action="store_true", help="Print the current recovery review without changing the installation.")
    commands.add_parser("help", help="Show this help.")
    for name in ("bootstrap", "setup-code"):
        code = commands.add_parser(name, help="Show or explicitly renew the one-time first-setup code.")
        code.add_argument("--renew", action="store_true", help="Invalidate the previous code without invalidating unlocked setup sessions.")
    logs = commands.add_parser("logs", help="Read the StreamHome service journal.")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--component", choices=("runtime", "operations", "installation", "all"), default="runtime")
    logs.add_argument("--level", choices=("error", "warning", "info", "debug"), default="debug")
    parsed = parser.parse_args(arguments)
    if parsed.command in {None, "help"}:
        parser.print_help()
        return 0
    if parsed.command=='tunnel':
        if parsed.tunnel_command is None:
            tunnel.print_help();return 0
        from .cloudflare_connector import command
        return command(parsed.tunnel_command,manifest=manifest,token_file=getattr(parsed,'token_file',None),
            label=getattr(parsed,'name','Existing tunnel'))
    if parsed.command == "version":
        print("StreamHome control " + manifest["bundle_id"])
        print("Manifest SHA-256: " + manifest_sha256)
        if os.geteuid() == 0:
            try:
                identity = run_worker(["identity"])
                if (set(identity) != {"installation_id", "application_commit", "application_build_id", "control_bundle_id"}
                        or not isinstance(identity["application_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", identity["application_commit"])
                        or not isinstance(identity["application_build_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", identity["application_build_id"])):
                    raise ControlError("control_reply_invalid")
                print("Application build: " + identity["application_build_id"])
                print("Application commit: " + identity["application_commit"])
                print("Management mode: systemd")
            except ControlError:
                print("Application identity is unavailable; inspect sudo streamhome doctor for incomplete or invalid installation evidence.")
        else:
            print("Use sudo streamhome version to read the private accepted application identity.")
        return 0
    if parsed.command == "status":
        return print_status(manifest=manifest, manifest_sha256=manifest_sha256)
    if parsed.command == "doctor":
        require_root()
        return print_status(detail=True, manifest=manifest, manifest_sha256=manifest_sha256)
    if parsed.command == "configure":
        from .administration import configure
        from .provisioning import administrative_lock
        with administrative_lock():
            return configure(request_path=parsed.request,control_manifest=manifest)
    if parsed.command == "repair":
        require_root()
        if parsed.control_artifact is not None:
            raise ControlError('control_independent_reader_required')
        if parsed.operation is not None:
            from .bound_repair import repair as repair_bound
            from .provisioning import administrative_lock
            with administrative_lock():
                result = repair_bound(parsed.operation, request_path=parsed.request,
                    control_manifest_sha256=manifest_sha256, review_only=parsed.review)
            from .stopped_repair import StoppedRepairHandoff, run_scoped
            return run_scoped(result) if isinstance(result, StoppedRepairHandoff) else result
        if parsed.review:
            raise ControlError('control_operation_id_required')
        selected = descriptor()
        if selected.status is InstallationStatus.PROVISIONING:
            from .first_start_retry import retry_installation
            return retry_installation(selected.installation_id, manifest=manifest, manifest_sha256=manifest_sha256, request_path=parsed.request)
        from .repair import repair
        from .provisioning import administrative_lock
        with administrative_lock():
            result = repair(request_path=parsed.request, control_manifest_sha256=manifest_sha256)
        from .stopped_repair import StoppedRepairHandoff, run_scoped
        return run_scoped(result) if isinstance(result, StoppedRepairHandoff) else result
    if parsed.command == "recover":
        require_root()
        from .recover import recover
        from .provisioning import administrative_lock
        with administrative_lock():
            result = recover(parsed.operation, request_path=parsed.request, control_manifest_sha256=manifest_sha256,
                review_only=parsed.review, action=parsed.recovery_action or 'continue_recovery')
        from .stopped_repair import StoppedRepairHandoff, run_scoped
        return run_scoped(result) if isinstance(result, StoppedRepairHandoff) else result
    if parsed.command in {"bootstrap", "setup-code"}:
        bootstrap_code(renew=parsed.renew)
        return 0
    if parsed.command == "logs":
        if not 1 <= parsed.lines <= 1000:
            parser.error("--lines must be from 1 to 1000")
        from .logs import read_logs
        return read_logs(lines=parsed.lines, follow=parsed.follow, component=parsed.component, level=parsed.level)
    require_root()
    selected = descriptor()
    if parsed.command == "stop":
        from .administration import record_stop
        from .stop import stop_units
        unit = unit_snapshot()
        record_stop(unit["InvocationID"] or None)
        if unit["LoadState"] == "not-found" and int(unit["MainPID"]) == 0:
            stop_units(runtime_registered=False)
            print("Stop intent recorded; the runtime service has not been registered yet.")
            return 0
        if unit["ActiveState"] in {"active", "activating"} and int(unit["MainPID"]) > 0:
            try:
                request(selected.paths.runtime / "supervisor.sock", expected_uid=selected.service_uid,
                        expected_pid=int(unit["MainPID"]), installation_id=selected.installation_id,
                        invocation=unit["InvocationID"], action="stop_notice", arguments={})
            except ControlError:
                pass  # systemd retains stop custody even if the supervisor crashed.
        stop_units(runtime_registered=True)
        print("StreamHome stopped.")
        return 0
    if parsed.command == "start":
        if selected.status is not InstallationStatus.READY:
            raise ControlError("control_installation_incomplete")
        from .notification import STARTUP_RECOVERY_SECONDS
        systemctl("start", "streamhome.service", timeout=STARTUP_RECOVERY_SECONDS + 30)
        return print_status()
    raise ControlError("control_action_unsupported")


def dispatch(arguments: list[str], *, bundle: Path, manifest: dict, manifest_sha256: str, rescue: bool) -> int:
    global _RESCUE_MODE
    _RESCUE_MODE = rescue
    try:
        if arguments and arguments[0]=='--repair-control':
            require_root()
            parser=argparse.ArgumentParser(prog=str(CONTROL/'provision')+' --repair-control')
            parser.add_argument('--operation',required=True)
            parser.add_argument('--artifact',type=Path,required=True)
            parser.add_argument('--request',type=Path)
            parsed=parser.parse_args(arguments[1:])
            from .control_repair import repair as repair_control
            from .provisioning import administrative_lock
            with administrative_lock():
                result=repair_control(parsed.operation,parsed.artifact,bundle=bundle,manifest=manifest,
                    manifest_sha256=manifest_sha256,request_path=parsed.request)
            from .stopped_repair import StoppedRepairHandoff,run_scoped
            return run_scoped(result) if isinstance(result,StoppedRepairHandoff) else result
        if arguments and arguments[0]=='--maintain-control':
            require_root()
            parser=argparse.ArgumentParser(prog=str(CONTROL/'provision')+' --maintain-control')
            parser.add_argument('--artifact',type=Path,required=True)
            parser.add_argument('--request',type=Path)
            parsed=parser.parse_args(arguments[1:])
            from .control_maintenance import maintain
            from .provisioning import administrative_lock
            with administrative_lock():
                result=maintain(parsed.artifact,manifest=manifest,manifest_sha256=manifest_sha256,request_path=parsed.request)
            from .stopped_repair import StoppedRepairHandoff,run_scoped
            return run_scoped(result) if isinstance(result,StoppedRepairHandoff) else result
        if arguments and arguments[0] in {'--repair-stopped-scoped', '--repair-running-scoped', '--recover-stopped-scoped', '--repair-bound-scoped','--maintain-control-scoped','--repair-control-scoped'}:
            require_root()
            parser = argparse.ArgumentParser(prog='StreamHome stopped repair')
            parser.add_argument('installation_id')
            parser.add_argument('preparation_id')
            parser.add_argument('stop_epoch')
            parser.add_argument('--request', type=Path)
            if arguments[0] in {'--maintain-control-scoped','--repair-control-scoped'}:parser.add_argument('--artifact',type=Path,required=True)
            else:parser.add_argument('--action', choices=('continue_recovery', 'rollback', 'safe_abort'), default='continue_recovery')
            selected = parser.parse_args(arguments[1:])
            from .provisioning import administrative_lock
            with administrative_lock():
                if arguments[0]=='--repair-control-scoped':
                    from .control_repair import repair as repair_control
                    return repair_control(selected.preparation_id,selected.artifact,bundle=bundle,manifest=manifest,
                        manifest_sha256=manifest_sha256,request_path=selected.request,scope_epoch=selected.stop_epoch,
                        expected_installation_id=selected.installation_id)
                if arguments[0]=='--maintain-control-scoped':
                    from .control_maintenance import maintain
                    return maintain(selected.artifact,manifest=manifest,manifest_sha256=manifest_sha256,
                        request_path=selected.request,scope_epoch=selected.stop_epoch,operation_id=selected.preparation_id,
                        expected_installation_id=selected.installation_id)
                if arguments[0] == '--repair-bound-scoped':
                    from .bound_repair import repair as repair_bound
                    return repair_bound(selected.preparation_id, request_path=selected.request,
                        control_manifest_sha256=manifest_sha256, scope_epoch=selected.stop_epoch,
                        expected_installation_id=selected.installation_id)
                if arguments[0] == '--recover-stopped-scoped':
                    from .recover import recover
                    return recover(selected.preparation_id, request_path=selected.request,
                        control_manifest_sha256=manifest_sha256, action=selected.action,
                        scope_epoch=selected.stop_epoch, expected_installation_id=selected.installation_id)
                from .repair import repair
                return repair(request_path=selected.request, control_manifest_sha256=manifest_sha256,
                    scope_epoch=selected.stop_epoch, preparation_id=selected.preparation_id,
                    expected_installation_id=selected.installation_id,
                    running_scope=arguments[0] == '--repair-running-scoped')
        if arguments == ["--select-public-source"]:
            from .source_selection import select_public_source
            print(json.dumps(select_public_source(), sort_keys=True))
            return 0
        if arguments == ["--before-start"]:
            from .provisioning import before_start
            return before_start(control_manifest_sha256=manifest_sha256)
        if arguments == ["--after-start"]:
            from .startup import after_start
            return after_start()
        if arguments==['--reconcile-tunnel']:
            from .cloudflare_connector import reconcile
            return reconcile(manifest=manifest)
        if arguments and arguments[0] == "--activate-pending":
            if len(arguments) != 2:
                raise ControlError("control_message_invalid")
            from .startup import activate_pending
            try:return activate_pending(arguments[1])
            finally:
                from .cloudflare_connector import queue_after_change
                queue_after_change(manifest=manifest)
        if arguments and arguments[0] == "--provision-worker":
            selected = descriptor()
            if len(arguments) != 2 or os.geteuid() == 0 or os.geteuid() != selected.service_uid:
                raise ControlError("control_worker_forbidden")
            from .provision_worker import main
            return main(arguments[1], control_bundle_id=manifest["bundle_id"], control_manifest_sha256=manifest_sha256)
        if arguments and arguments[0] == "--prepare-runtime":
            selected = descriptor()
            if len(arguments) != 2 or os.geteuid() == 0 or os.geteuid() != selected.service_uid:
                raise ControlError("control_worker_forbidden")
            from .runtime_preparation import main
            return main(arguments[1], control_manifest_sha256=manifest_sha256)
        if arguments and arguments[0] == "--resume-install":
            if len(arguments) != 2:
                raise ControlError("control_message_invalid")
            from .provisioning import resume_install
            return resume_install(arguments[1], manifest=manifest, manifest_sha256=manifest_sha256)
        if arguments and arguments[0] == "--resume-install-scoped":
            if len(arguments) != 3:
                raise ControlError("control_message_invalid")
            from .provisioning import resume_install
            return resume_install(arguments[1], manifest=manifest, manifest_sha256=manifest_sha256, scope_epoch=arguments[2])
        if arguments and arguments[0] in {"--retry-first-start", "--retry-first-start-scoped"}:
            parser = argparse.ArgumentParser(prog=str(CONTROL / "provision") + " " + arguments[0])
            parser.add_argument("installation_id")
            if arguments[0] == "--retry-first-start-scoped":
                parser.add_argument("stop_epoch")
            parser.add_argument("--request", type=Path)
            parsed = parser.parse_args(arguments[1:])
            from .first_start_retry import retry_installation, retry_scoped
            if arguments[0] == "--retry-first-start-scoped":
                return retry_scoped(parsed.installation_id, parsed.stop_epoch, manifest=manifest, manifest_sha256=manifest_sha256, request_path=parsed.request)
            return retry_installation(parsed.installation_id, manifest=manifest, manifest_sha256=manifest_sha256, request_path=parsed.request)
        if arguments and arguments[0] == "--complete-install":
            if len(arguments) != 2:
                raise ControlError("control_message_invalid")
            from .provisioning import complete_install
            return complete_install(arguments[1], manifest=manifest, manifest_sha256=manifest_sha256)
        if arguments and arguments[0] == "--install":
            parser = argparse.ArgumentParser(prog=str(CONTROL / "provision") + " --install")
            parser.add_argument("--request", type=Path)
            parser.add_argument("--source-commit")
            parsed = parser.parse_args(arguments[1:])
            from .provisioning import install
            return install(request_path=parsed.request, manifest=manifest, manifest_sha256=manifest_sha256, source_commit=parsed.source_commit)
        if arguments == ["--unit-stopped"]:
            from .administration import unit_stopped
            return unit_stopped()
        if arguments and arguments[0] == "--supervisor":
            if len(arguments) != 1:
                raise ControlError("control_message_invalid")
            if os.geteuid() == 0:
                raise ControlError("control_supervisor_forbidden")
            if os.geteuid() != descriptor().service_uid:
                raise ControlError("control_supervisor_forbidden")
            from .supervisor import main
            return main(control_bundle_id=manifest["bundle_id"], control_manifest_sha256=manifest_sha256)
        if arguments and arguments[0] == "--worker":
            selected = descriptor()
            if os.geteuid() != selected.service_uid:
                raise ControlError("control_worker_forbidden")
            from .workers import main
            return main(arguments[1:], bundle=bundle, manifest=manifest, manifest_sha256=manifest_sha256)
        if arguments and arguments[0] == "--operation":
            selected = descriptor()
            if len(arguments) != 2 or os.geteuid() != selected.service_uid or os.geteuid() == 0:
                raise ControlError("control_controller_forbidden")
            from .. import state
            authority = state.inspect_state(selected.paths.application)
            active = authority.active_operation
            if active is not None and active.operation_type is state.OperationType.UPDATE:
                from ..managed_update import execute
                return execute(selected.paths.application, arguments[1], control_manifest_sha256=manifest_sha256)
            if active is not None and active.operation_type is state.OperationType.RESTORE:
                from ..managed_restore import execute
                return execute(selected.paths.application,arguments[1],control_manifest_sha256=manifest_sha256)
            from ..management_controller import main
            return main(arguments[1], control_manifest_sha256=manifest_sha256)
        if arguments and arguments[0] == "--cli":
            return cli(arguments[1:], manifest=manifest, manifest_sha256=manifest_sha256)
        parser = argparse.ArgumentParser(prog=str(CONTROL / "provision"), description="Protected StreamHome installation and recovery entry.",
            epilog="Installation: --install [--request FILE]; explicit continuation: --resume-install UUID or --complete-install UUID; commands: --cli [COMMAND].")
        parser.print_help()
        return 0 if not arguments else 2
    except (ControlError, InstallationEvidenceError, InputRequiredError, UserCancelled, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        code = getattr(exc, "code", "control_unavailable")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
            code = "control_unavailable"
        if isinstance(exc, InputRequiredError):
            code = "control_terminal_required"
        elif isinstance(exc, UserCancelled):
            code = "control_cancelled"
        print("StreamHome: " + code, file=sys.stderr)
        if code == "control_root_required":
            print("Use sudo streamhome for this administrative command.", file=sys.stderr)
        if code == "control_stop_before_first_start_retry":
            print("Use sudo streamhome stop before reviewing a new first-start attempt.", file=sys.stderr)
        if code == 'control_web_port_closing':
            print('Closing TCP connections still reserve the requested port. Wait and retry, or choose another port.', file=sys.stderr)
        if code == 'control_web_port_conflict':
            print('Choose another web port or inspect its owner. No process was stopped.', file=sys.stderr)
        if code=='control_maintenance_stop_required':
            print('Stop the managed service with sudo streamhome stop, then request a fresh recovery review.',file=sys.stderr)
        if code=='control_units_incompatible':
            print('This package changes the qualified systemd unit protocol. Select a compatible control package.',file=sys.stderr)
        if os.geteuid() == 0 and arguments and arguments[0] in {"--install", "--resume-install", "--complete-install", "--retry-first-start"}:
            try:
                from .provisioning import report_continuation
                report_continuation(manifest=manifest, manifest_sha256=manifest_sha256)
            except (ControlError, InstallationEvidenceError, OSError, ValueError, RuntimeError):
                pass  # Untrusted or absent evidence must never supply a command.
        return 2
