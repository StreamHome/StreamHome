"""Protected first-install OS orchestration; application work always drops UID."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
import uuid

from ..configure_wizard import installation_wizard, parse_installation_request, parse_request_json
from ..installation import InstallationDescriptor, InstallationStatus, inspect_installation
from ..deployment_config import DeploymentConfigError
from ..port_check import ListenerCheckError, check_web_port
from ..terminal import Progress, open_console
from .privileged import descriptor, require_root, run_worker, systemctl, unit_snapshot, _OS_ENV
from .provisioning_contract import JOURNAL_PATH, SERVICE_DIRECTORIES, ProvisioningIntent, advance_intent, identity, parse_intent
from .root_files import atomic_root_bytes, atomic_root_json, root_directory
from .rpc import ControlError, request
from .support import PREPARATION_STAGES, require_supported_platform
from .tickets import read_protected_json


JOURNAL = Path(JOURNAL_PATH)


@contextmanager
def administrative_lock():
    """Serialize privileged administrators; stop hooks deliberately bypass it."""
    require_root()
    parent = root_directory(Path("/etc/streamhome/control"), create=True)
    handle = None
    owner = None
    try:
        handle = os.open("administration.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=parent)
        metadata = os.fstat(handle)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o7177 or metadata.st_nlink != 1):
            raise ControlError("control_administrator_lock_untrusted")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControlError("control_administrator_busy") from exc
        from .administration_lock import enter_owner
        owner = enter_owner()
        yield
    finally:
        try:
            if owner is not None:
                from .administration_lock import release_owner
                release_owner(owner)
        finally:
            if handle is not None:
                os.close(handle)
            os.close(parent)


def read_intent() -> ProvisioningIntent:
    return parse_intent(read_protected_json(JOURNAL))


def report_continuation(*, manifest: dict, manifest_sha256: str) -> None:
    """Report only a trusted intent belonging to this immutable control build."""
    require_root()
    intent = read_intent()
    if intent.control_bundle_id != manifest["bundle_id"] or intent.control_manifest_sha256 != manifest_sha256:
        return
    if intent.stage == "complete":
        return
    selected = inspect_installation()
    registered = selected is not None and selected.descriptor.status is InstallationStatus.READY
    command = "--complete-install" if registered or intent.stage == "registered" else "--resume-install"
    print("Recorded installation stage: " + intent.stage + ". The explicit continuation rechecks ownership and recovery evidence:", file=sys.stderr)
    print("  sudo /usr/local/libexec/streamhome/provision " + command + " " + intent.installation_id, file=sys.stderr)


def _persist(intent: ProvisioningIntent) -> ProvisioningIntent:
    intent = parse_intent(intent.payload())
    atomic_root_json(JOURNAL, intent.payload(), gid=intent.service_gid, mode=0o640)
    if len(intent.mirror_directories) == 2:
        from .provisioning_mirror import publish_mirror
        publish_mirror(intent)
    return intent


def _advance(intent: ProvisioningIntent, **changes) -> ProvisioningIntent:
    # Callers hold the root administrative lock. Recheck even there so a direct
    # root intervention cannot silently be overwritten by an older process.
    if read_intent() != intent:
        raise ControlError("control_installation_review_changed")
    return _persist(advance_intent(intent, **changes))


def _account_ids() -> tuple[int, int]:
    try:
        pwd.getpwnam("streamhome")
    except KeyError:
        pass
    else:
        raise ControlError("control_existing_account_not_adopted")
    try:
        grp.getgrnam("streamhome")
    except KeyError:
        pass
    else:
        raise ControlError("control_existing_group_not_adopted")
    used = {item.pw_uid for item in pwd.getpwall()} | {item.gr_gid for item in grp.getgrall()}
    for candidate in range(999, 99, -1):
        if candidate not in used:
            return candidate, candidate
    raise ControlError("control_service_id_unavailable")


def _fixed_os(arguments: list[str], *, timeout: int = 30) -> None:
    result = subprocess.run(arguments, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, env=_OS_ENV)
    if result.returncode or len(result.stdout) + len(result.stderr) > 1024 * 1024:
        raise ControlError("control_installation_os_step_failed")


def _select_public_source() -> str:
    from . import privileged
    nobody = pwd.getpwnam("nobody")
    if nobody.pw_uid != 65534 or nobody.pw_gid != 65534:
        raise ControlError("control_source_probe_account_unqualified")
    arguments = ["/usr/bin/python3", "-I", "/usr/local/libexec/streamhome/provision",
                 *(["--rescue"] if privileged._RESCUE_MODE else []), "--select-public-source"]
    result = subprocess.run(arguments, cwd="/", input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**_OS_ENV, "HOME": "/nonexistent"}, user=nobody.pw_uid, group=nobody.pw_gid, extra_groups=[], umask=0o077, timeout=30)
    if result.returncode or len(result.stdout) > 4096 or len(result.stderr) > 32768:
        raise ControlError("control_source_revision_unavailable")
    value = json.loads(result.stdout)
    if (not isinstance(value, dict) or set(value) != {"application_commit"}
            or not isinstance(value["application_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["application_commit"])):
        raise ControlError("control_source_revision_invalid")
    return value["application_commit"]


def _install_system_dependencies() -> None:
    """Fixed distro dependencies only; never install or replace system Node/npm."""
    environment = {**_OS_ENV, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
    parent = root_directory(JOURNAL.parent)
    try:
        for arguments in (
            ["/usr/bin/apt-get", "update"],
            ["/usr/bin/apt-get", "install", "--yes", "--no-install-recommends", "python3-venv", "git", "ffmpeg", "ca-certificates", "openssl", "polkitd"],
        ):
            descriptor_fd = os.open("system-dependencies-" + secrets.token_hex(16) + ".log",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            process, reaped = None, False
            try:
                with os.fdopen(descriptor_fd, "wb") as output:
                    process = subprocess.Popen(arguments, cwd="/", stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                               env=environment, start_new_session=True)
                    deadline = time.monotonic() + 1800
                    while True:
                        if time.monotonic() > deadline or os.fstat(output.fileno()).st_size > 32 * 1024 * 1024:
                            raise ControlError("control_system_dependencies_exceeded_bound")
                        try:
                            result = process.wait(timeout=0.25)
                        except subprocess.TimeoutExpired:
                            continue
                        reaped = True
                        if result:
                            raise ControlError("control_system_dependencies_failed")
                        break
            finally:
                if process is not None and not reaped:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
    finally:
        os.close(parent)


def _ensure_account(intent: ProvisioningIntent) -> None:
    marker = "StreamHome installation " + intent.installation_id
    try:
        group = grp.getgrnam("streamhome")
    except KeyError:
        if intent.stage != "intent":
            raise ControlError("control_service_account_changed")
        _fixed_os(["/usr/sbin/groupadd", "--system", "--gid", str(intent.service_gid), "streamhome"])
        group = grp.getgrnam("streamhome")
    if group.gr_gid != intent.service_gid or group.gr_mem:
        raise ControlError("control_service_account_changed")
    try:
        account = pwd.getpwnam("streamhome")
    except KeyError:
        if intent.stage != "intent":
            raise ControlError("control_service_account_changed")
        _fixed_os(["/usr/sbin/useradd", "--system", "--uid", str(intent.service_uid), "--gid", str(intent.service_gid),
                   "--no-create-home", "--home-dir", "/var/lib/streamhome", "--shell", "/usr/sbin/nologin",
                   "--comment", marker, "--password", "!", "streamhome"])
        account = pwd.getpwnam("streamhome")
    if ((account.pw_uid, account.pw_gid, account.pw_dir, account.pw_shell, account.pw_gecos)
            != (intent.service_uid, intent.service_gid, "/var/lib/streamhome", "/usr/sbin/nologin", marker)
            or [item.gr_name for item in grp.getgrall() if "streamhome" in item.gr_mem]):
        raise ControlError("control_service_account_changed")


def _create_service_directory(intent: ProvisioningIntent, path: Path) -> ProvisioningIntent:
    """Bind inode in the root journal before giving the service any write access."""
    if str(path) not in SERVICE_DIRECTORIES:
        raise ControlError("control_installation_destination_invalid")
    parent = root_directory(path.parent)
    child = None
    try:
        bindings = {name: (device, inode) for name, device, inode in intent.directories}
        try:
            child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            if str(path) in bindings:
                raise ControlError("control_installation_directory_missing")
            os.mkdir(path.name, 0o700, dir_fd=parent)
            os.fsync(parent)
            child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        metadata = os.fstat(child)
        if str(path) not in bindings:
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700 or os.listdir(child):
                raise ControlError("control_existing_directory_not_adopted")
            intent = _advance(intent, directories=tuple(sorted((*intent.directories, (str(path), metadata.st_dev, metadata.st_ino)))))
            bindings[str(path)] = metadata.st_dev, metadata.st_ino
        if bindings[str(path)] != (metadata.st_dev, metadata.st_ino):
            raise ControlError("control_installation_directory_changed")
        if metadata.st_uid == 0:
            contents = os.listdir(child)
            if contents and str(path) == "/var/lib/streamhome" and contents == ["provisioning"]:
                from .provisioning_mirror import publish_mirror
                publish_mirror(intent)
                contents = []
            if contents or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ControlError("control_existing_directory_not_adopted")
            os.fchown(child, intent.service_uid, intent.service_gid)
            os.fchmod(child, 0o700)
            os.fsync(child)
        elif (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) != (intent.service_uid, intent.service_gid, 0o700):
            raise ControlError("control_installation_directory_changed")
        linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (linked.st_dev, linked.st_ino) != bindings[str(path)]:
            raise ControlError("control_installation_directory_changed")
        return intent
    finally:
        if child is not None:
            os.close(child)
        os.close(parent)


def _validate_service_directories(intent: ProvisioningIntent) -> None:
    for name, device, inode in intent.directories:
        path = Path(name)
        parent = root_directory(path.parent)
        child = None
        try:
            child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            metadata = os.fstat(child)
            if ((metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
                    != (device, inode, intent.service_uid, intent.service_gid, 0o700)):
                raise ControlError("control_installation_directory_changed")
        finally:
            if child is not None:
                os.close(child)
            os.close(parent)


def _publish_provisional_descriptor(intent: ProvisioningIntent) -> None:
    candidate = InstallationDescriptor(intent.installation_id, InstallationStatus.PROVISIONING, intent.service_uid, intent.service_gid)
    existing = inspect_installation()
    if existing is not None and existing.descriptor != candidate:
        raise ControlError("control_installation_identity_changed")
    if existing is None:
        atomic_root_json(Path("/etc/streamhome/install.json"), candidate.payload())


def _unit_status(intent: ProvisioningIntent) -> dict:
    output = systemctl("show", f"streamhome-provision@{intent.operation_id}.service", "--property=ActiveState,SubState,MainPID,InvocationID,Result")
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if set(fields) != {"ActiveState", "SubState", "MainPID", "InvocationID", "Result"} or not re.fullmatch(r"[0-9]+", fields["MainPID"]):
        raise ControlError("control_unit_invalid")
    return fields


def _prepare_application(intent: ProvisioningIntent) -> dict:
    unit = _unit_status(intent)
    if unit["ActiveState"] in {"inactive", "failed"}:
        # Worker retries reuse a durably prepared tuple. They never replace an
        # existing initial execution record with a newly built environment.
        systemctl("start", f"streamhome-provision@{intent.operation_id}.service", timeout=30)
    deadline, next_progress = time.monotonic() + 3600, 0
    progress, context, current_stage = Progress(sys.stdout), None, None
    try:
        while time.monotonic() < deadline:
            unit = _unit_status(intent)
            if unit["ActiveState"] in {"inactive", "failed"}:
                if unit["Result"] != "success":
                    raise ControlError("control_installation_preparation_failed")
                result = run_worker(["initial-result", intent.operation_id], timeout=120)
                if context is not None:
                    context.__exit__(None, None, None)
                    context = None
                return result
            if time.monotonic() >= next_progress:
                next_progress = time.monotonic() + 2
                from .progress import read_display_progress
                value = read_display_progress('install', intent.service_uid, intent.operation_id)
                if (isinstance(value, dict) and value.get("stage") in PREPARATION_STAGES
                        and value.get("status") in {"running", "done"}):
                    if value["stage"] != current_stage:
                        if context is not None:
                            context.__exit__(None, None, None)
                        current_stage = value["stage"]
                        context = progress.stage(current_stage)
                        context.__enter__()
                    if value["status"] == "done" and context is not None:
                        context.__exit__(None, None, None)
                        context = None
            time.sleep(0.5)
        raise ControlError("control_installation_preparation_timeout")
    except BaseException:
        if context is not None:
            context.__exit__(*sys.exc_info())
        raise


def before_start() -> int:
    """Consume the root first-start permit before the supervisor can execute."""
    require_root()
    if Path("/proc/self/cgroup").read_text().splitlines() != ["0::/system.slice/streamhome.service"]:
        raise ControlError("control_unit_hook_forbidden")
    selected = descriptor()
    if selected.status is InstallationStatus.READY:
        # A direct systemctl start must respect the same administrator exclusion
        # as the CLI. Release before scheduling the independent observer so its
        # own lock acquisition cannot race a still-held ExecStartPre lock.
        with administrative_lock():
            pass
        from .startup import schedule_pending
        schedule_pending()
        return 0
    invocation = os.environ.get("INVOCATION_ID", "")
    intent = read_intent()
    from .administration import root_stop_epoch
    if (intent.installation_id != selected.installation_id or intent.stage != "first_start_requested"
            or intent.start_permit != "issued" or not re.fullmatch(r"[0-9a-f]{32}", invocation) or not int(invocation, 16)
            or intent.start_boot_id != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            or intent.start_stop_epoch != root_stop_epoch()):
        raise ControlError("control_first_start_denied")
    # The administrative parent holds its own lock while systemctl waits for
    # this hook. This child has a separate permit lock and performs a root CAS.
    parent = root_directory(JOURNAL.parent)
    lock = None
    try:
        lock = os.open("first-start.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        metadata = os.fstat(lock)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o7177 or metadata.st_nlink != 1:
            raise ControlError("control_start_permit_untrusted")
        fcntl.flock(lock, fcntl.LOCK_EX)
        intent = _advance(intent, start_permit="consumed", start_invocation=invocation)
        atomic_root_json(Path("/etc/streamhome/control/first-start.json"), {
            "schema_version": 1, "action": "first_start", "installation_id": intent.installation_id,
            "operation_id": intent.operation_id, "execution_sha256": intent.execution_sha256,
            "invocation": invocation, "boot_id": intent.start_boot_id,
            "stop_epoch": intent.start_stop_epoch,
            "expires_at_monotonic": time.monotonic() + 900, "nonce": secrets.token_hex(32),
        }, gid=intent.service_gid, mode=0o640)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(parent)
    return 0


def _first_start(intent: ProvisioningIntent, *, expected_stop_epoch: str) -> ProvisioningIntent:
    from .administration import root_stop_epoch
    if (not isinstance(expected_stop_epoch, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_stop_epoch)
            or root_stop_epoch() != expected_stop_epoch
            or intent.start_permit != "absent" and intent.start_stop_epoch != expected_stop_epoch):
        raise ControlError("control_operator_stopped")
    if intent.start_permit == "absent":
        intent = _advance(intent, stage="first_start_requested", start_permit="issued",
                          start_boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(), start_stop_epoch=expected_stop_epoch)
    unit = unit_snapshot()
    if intent.start_permit == "issued":
        if unit["ActiveState"] not in {"inactive", "failed"} or int(unit["MainPID"]):
            raise ControlError("control_first_start_denied")
        from .notification import STARTUP_RECOVERY_SECONDS
        systemctl("start", "streamhome.service", timeout=STARTUP_RECOVERY_SECONDS + 30)
        intent = read_intent()
    # A consumed permit may observe the same live invocation after CLI failure.
    # It cannot automatically start another invocation, including after reboot.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        unit = unit_snapshot()
        if (intent.start_permit != "consumed" or unit["InvocationID"] != intent.start_invocation
                or unit["ActiveState"] not in {"active", "activating"} or int(unit["MainPID"]) <= 0
                or intent.start_stop_epoch != root_stop_epoch()
                or intent.start_boot_id != Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
            raise ControlError("control_first_start_recovery_required")
        try:
            from .privileged import supervisor_snapshot
            supervisor = supervisor_snapshot(descriptor(), unit)
            backend = supervisor.get("children", {}).get("backend")
            if isinstance(backend, dict):
                result = request(Path("/run/streamhome/backend.sock"), expected_uid=intent.service_uid,
                    expected_pid=backend["pid"], installation_id=intent.installation_id, invocation=unit["InvocationID"],
                    action="snapshot", arguments={})
                execution = result.get("execution")
                digest = hashlib.sha256(json.dumps(execution, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                if result.get("admitted") is True and result.get("generation") == 0 and digest == intent.execution_sha256:
                    web = supervisor.get("children", {}).get("web")
                    if not isinstance(web, dict):
                        raise ControlError("control_web_listener_missing")
                    live = run_worker(["initial-listener"], payload={"invocation": unit["InvocationID"], "web": web})
                    current_unit = unit_snapshot()
                    if (live.get("ready") is not True or live.get("execution_sha256") != intent.execution_sha256
                            or live.get("invocation") != intent.start_invocation
                            or current_unit["ActiveState"] != "active" or current_unit["InvocationID"] != intent.start_invocation
                            or current_unit["MainPID"] != unit["MainPID"] or root_stop_epoch() != intent.start_stop_epoch):
                        raise ControlError("control_first_start_not_ready")
                    # Journal first; a crash before the descriptor rename is
                    # reconciled forward by the exact explicit resume command.
                    return _advance(intent, stage="registered")
        except ControlError:
            pass
        time.sleep(0.25)
    raise ControlError("control_first_start_not_ready")


def _finish_registration(intent: ProvisioningIntent) -> None:
    existing = inspect_installation()
    provisional = InstallationDescriptor(intent.installation_id, InstallationStatus.PROVISIONING, intent.service_uid, intent.service_gid)
    ready = replace(provisional, status=InstallationStatus.READY)
    if intent.stage != "registered" or existing is None or existing.descriptor not in (provisional, ready):
        raise ControlError("control_installation_identity_changed")
    if existing.descriptor == provisional:
        atomic_root_json(Path("/etc/streamhome/install.json"), ready.payload())


def _require_install_scope(installation_id: str, epoch: str) -> None:
    identity(installation_id)
    if not isinstance(epoch, str) or not re.fullmatch(r"[0-9a-f]{64}", epoch):
        raise ControlError("control_installation_scope_invalid")
    name = "streamhome-install-" + installation_id + ".scope"
    if Path("/proc/self/cgroup").read_text().splitlines() != ["0::/system.slice/" + name]:
        raise ControlError("control_installation_scope_invalid")
    raw = systemctl("show", name, "--property=ActiveState,Transient,ControlGroup")
    fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if fields != {"ActiveState": "active", "Transient": "yes", "ControlGroup": "/system.slice/" + name}:
        raise ControlError("control_installation_scope_invalid")


def _run_install_scope(installation_id: str, epoch: str) -> int:
    """Give the root first-launch issuer an OS-cancellable lifetime.

    Distro package work finishes before this scope. Once service definitions are
    published, an inactive/coalesced direct runtime stop also stops the issuer;
    there is no untracked parent left that can launch after preparation exits.
    """
    identity(installation_id)
    if not isinstance(epoch, str) or not re.fullmatch(r"[0-9a-f]{64}", epoch):
        raise ControlError("control_installation_scope_invalid")
    from . import privileged
    name = "streamhome-install-" + installation_id + ".scope"
    arguments = ["/usr/bin/systemd-run", "--scope", "--quiet", "--collect", "--slice=system.slice", "--unit=" + name,
        "--description=StreamHome installation " + installation_id, "--property=StopPropagatedFrom=streamhome.service",
        "--property=RuntimeMaxSec=7200", "/usr/bin/python3", "-I", "/usr/local/libexec/streamhome/provision",
        *(["--rescue"] if privileged._RESCUE_MODE else []), "--resume-install-scoped", installation_id, epoch]
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, cwd="/", env=_OS_ENV, timeout=7230)
    except BaseException:
        _stop_install_scope_work(installation_id)
        raise
    if result.returncode:
        _stop_install_scope_work(installation_id)
        raise ControlError("control_installation_scope_stopped")
    return 0


def _stop_install_scope_work(installation_id: str) -> None:
    """A cancelled issuer must not abandon its separately owned builder unit."""
    intent = read_intent()
    if intent.installation_id != installation_id:
        raise ControlError("control_installation_review_changed")
    names = ("streamhome-install-" + installation_id + ".scope",
             "streamhome-provision@" + intent.operation_id + ".service")
    targets = tuple(name for name in names if systemctl("show", name, "--property=LoadState", "--value") != "not-found")
    if targets:
        systemctl("stop", *targets, timeout=30)
    from .stop import _require_empty
    for name in names:
        _require_empty(name)


def resume_install(installation_id: str, *, manifest: dict, manifest_sha256: str, scope_epoch: str | None = None) -> int:
    require_root()
    identity(installation_id)
    require_supported_platform()
    if scope_epoch is not None:
        _require_install_scope(installation_id, scope_epoch)
    handoff = None
    with administrative_lock():
        intent = read_intent()
        if (intent.installation_id != installation_id or intent.control_bundle_id != manifest["bundle_id"]
                or intent.control_manifest_sha256 != manifest_sha256 or intent.application_commit is None):
            raise ControlError("control_installation_review_changed")
        existing = inspect_installation()
        if existing is not None and existing.descriptor.status is InstallationStatus.READY:
            print("Registration is already committed. Use --complete-install " + intent.installation_id + " for the remaining OS integration.")
            raise ControlError("control_complete_install_required")
        from .administration import root_stop_epoch, stop_record
        marker = Path("/etc/streamhome/control/stop-epoch.json")
        expected_stop_epoch = root_stop_epoch() if marker.exists() or marker.is_symlink() else None
        if existing is not None and expected_stop_epoch is None:
            raise ControlError("control_stop_epoch_missing")
        if scope_epoch is not None and expected_stop_epoch != scope_epoch:
            raise ControlError("control_operator_stopped")
        progress = Progress(sys.stdout)
        if intent.stage == "intent":
            from .provisioning_mirror import reserve_mirror
            intent = reserve_mirror(intent, _advance)
            with progress.stage("Creating the dedicated StreamHome service account"):
                _ensure_account(intent)
                intent = _advance(intent, stage="account_ready")
        else:
            _ensure_account(intent)
            from .provisioning_mirror import publish_mirror
            publish_mirror(intent)
        if intent.stage == "account_ready":
            # Reserve the reviewed account ID before apt maintainer scripts can
            # allocate a previously free system UID/GID for another package.
            with progress.stage("Installing required Ubuntu packages"):
                _install_system_dependencies()
            with progress.stage("Preparing the fixed application, configuration and state directories"):
                for name in SERVICE_DIRECTORIES:
                    intent = _create_service_directory(intent, Path(name))
                intent = _advance(intent, stage="layout_ready")
        _validate_service_directories(intent)
        if intent.stage in {"layout_ready", "source_selected", "preparing_application", "application_prepared", "first_start_requested"}:
            if expected_stop_epoch is None:
                # Seed before publishing the first descriptor. Once a stop can
                # address this installation, no setup stage may replace its
                # marker or capture that newer stop as fresh start permission.
                if marker.exists() or marker.is_symlink():
                    raise ControlError("control_installation_review_changed")
                atomic_root_json(marker, stop_record(None), gid=intent.service_gid, mode=0o640)
                expected_stop_epoch = root_stop_epoch()
            if scope_epoch is None:
                handoff = expected_stop_epoch
        if handoff is None and intent.stage in {"layout_ready", "source_selected", "preparing_application", "application_prepared", "first_start_requested"}:
            _publish_provisional_descriptor(intent)
            result = run_worker(["initial-authority"])
            if result.get("generation") != 0:
                raise ControlError("control_operation_recovery_required")
            if intent.stage == "layout_ready":
                # Source was pinned in the root intent before account/package work.
                # Explicit resume never re-resolves the advancing public branch.
                intent = _advance(intent, stage="source_selected")
            if intent.stage == "source_selected":
                from .units import unit_files
                with progress.stage("Registering the protected StreamHome service definitions"):
                    for name, raw in unit_files().items():
                        parent = root_directory(Path(name).parent, create=True)
                        os.close(parent)
                        atomic_root_bytes(Path(name), raw)
                    systemctl("daemon-reload")
                    intent = _advance(intent, stage="preparing_application")
            if intent.stage == "preparing_application":
                print("[StreamHome] Preparing private application environments; progress is observed from the owned preparation unit.", flush=True)
                result = _prepare_application(intent)
                if (set(result) != {"installation_id", "operation_id", "execution_sha256", "application_commit"}
                        or result["installation_id"] != intent.installation_id or result["operation_id"] != intent.operation_id
                        or result["application_commit"] != intent.application_commit):
                    raise ControlError("control_initial_result_invalid")
                intent = _advance(intent, stage="application_prepared", execution_sha256=result["execution_sha256"])
            if intent.stage in {"application_prepared", "first_start_requested"}:
                with progress.stage("Starting and verifying the first StreamHome runtime"):
                    intent = _first_start(intent, expected_stop_epoch=expected_stop_epoch)
        if intent.stage == "registered":
            _finish_registration(intent)
            print("StreamHome is registered. Complete the recorded boot-service policy with:", flush=True)
            print("  sudo /usr/local/libexec/streamhome/provision --complete-install " + intent.installation_id, flush=True)
        elif intent.stage == "complete":
            print("StreamHome installation is already complete.")
    if handoff is not None:
        return _run_install_scope(installation_id, handoff)
    return 0


def complete_install(installation_id: str, *, manifest: dict, manifest_sha256: str, automatic: bool = False) -> int:
    """READY registration is followed by a distinct canonical OS-only operation."""
    require_root()
    identity(installation_id)
    from .administration import OPERATIONS, _boot_enabled, process_snapshot, root_stop_epoch, run_operation
    with administrative_lock():
        intent = read_intent()
        operation_id = intent.operation_id
        selected = descriptor()
        if (intent.installation_id != installation_id or intent.installation_id != selected.installation_id
                or selected.status is not InstallationStatus.READY or intent.stage not in {"registered", "complete"}
                or intent.control_bundle_id != manifest["bundle_id"] or intent.control_manifest_sha256 != manifest_sha256):
            raise ControlError("control_installation_review_changed")
        if intent.stage == "complete":
            print("StreamHome installation is already complete.")
            return 0
        if automatic:
            unit = unit_snapshot()
            if (intent.start_stop_epoch != root_stop_epoch() or unit["ActiveState"] != "active"
                    or unit["InvocationID"] != intent.start_invocation):
                raise ControlError("control_installation_completion_cancelled")
        _validate_service_directories(intent)
        from .provisioning_mirror import publish_mirror
        publish_mirror(intent)
        path = OPERATIONS / operation_id / "request.json"
        if path.exists() or path.is_symlink():
            result = run_worker(["operation-result", operation_id])
            if result.get("completed") is not True:
                # Observe/reconcile the exact existing controller; never create
                # a new claim or execute another operation under this identity.
                result = run_operation(read_protected_json(path), start=False)
        else:
            snapshot = run_worker(["inspect"], timeout=120)
            if snapshot["lifecycle"]["active_operation"] is not None:
                raise ControlError("control_operation_busy")
            execution_digest = hashlib.sha256(json.dumps(snapshot["execution"]["execution"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if (execution_digest != intent.execution_sha256 or snapshot["lifecycle"]["generation"] != 0
                    or snapshot["configuration"]["deployment"]["active"] != snapshot["configuration"]["deployment"]["desired"]):
                raise ControlError("control_installation_review_superseded")
            processes = process_snapshot()
            if processes["initial_service_state"] == "running" and processes["backend"] is None:
                raise ControlError("control_runtime_not_settled")
            payload = {"purpose": "complete_install", "operation_id": operation_id, "review_id": str(uuid.uuid4()),
                "expected_generation": snapshot["lifecycle"]["generation"],
                "expected_desired_revision": snapshot["configuration"]["deployment"]["desired"]["revision"],
                "deployment": snapshot["active_deployment"], "activate_deployment": False,
                "boot_enabled": intent.start_at_boot, "previous_boot_enabled": _boot_enabled(),
                "interruption_approved": False, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "stop_epoch": root_stop_epoch(), **processes}
            prepared = run_worker(["prepare-request"], payload=payload)
            if prepared.get("no_changes") is not False:
                raise ControlError("control_initial_os_completion_missing")
            result = run_operation(prepared["request"])
        if result.get("completed") is not True or result.get("stage") != "os_completed":
            raise ControlError("control_initial_os_completion_missing")
        _advance(intent, stage="complete")
        print("StreamHome installation completed. Application: /opt/streamhome; configuration: /etc/streamhome; state: /var/lib/streamhome.")
        print("Use streamhome for help, or sudo streamhome bootstrap to obtain the one-time setup code.")
    return 0


def install(*, request_path: Path | None, manifest: dict, manifest_sha256: str, source_commit: str | None = None) -> int:
    require_root()
    require_supported_platform()
    existing = inspect_installation()
    if existing is not None or JOURNAL.exists() or JOURNAL.is_symlink():
        print("An existing StreamHome installation or incomplete installation intent was found.")
        if JOURNAL.exists():
            intent = read_intent()
            print("Recorded installation: " + intent.installation_id + "; provisioning operation: " + intent.operation_id + "; stage: " + intent.stage)
            if intent.stage != "complete":
                command = "--complete-install" if intent.stage == "registered" else "--resume-install"
                print("Use the explicit continuation command: sudo /usr/local/libexec/streamhome/provision " + command + " " + intent.installation_id)
        if existing is not None and existing.descriptor.status is InstallationStatus.READY:
            from .administration import _verify_owned_units
            try:
                _verify_owned_units()
                result = run_worker(["release-check"], timeout=180)
                if result.get("status") == "update_available":
                    print("An eligible public update is available. Use the supported Updates workflow when ready; this installer did not apply it.")
                elif result.get("status") == "up_to_date":
                    print("StreamHome is already up to date with its configured public release.")
                else:
                    print("The release check did not establish an eligible update (" + str(result.get("status", "check_unavailable")) + "). No update or reinstall was performed.")
            except ControlError:
                print("The release check is unavailable. No update or reinstall was performed.")
        return 0
    def diagnostics(config):
        try:
            check_web_port(config.web_bind, config.web_port)
        except ListenerCheckError as exc:
            raise DeploymentConfigError(str(exc)) from exc
        return [f"Listener preflight passed: {config.web_bind}:{config.web_port}. External routing is not yet verified."]
    if request_path is None:
        with open_console() as console:
            draft = installation_wizard(console, diagnostics=diagnostics)
    else:
        handle = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(handle, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ControlError("control_request_invalid")
            draft = parse_installation_request(parse_request_json(source.read(32769)))
            diagnostics(draft.deployment)
    if draft is None:
        return 0
    selected_source = _select_public_source()
    if source_commit is not None and source_commit != selected_source:
        raise ControlError("control_source_changed_since_review")
    with administrative_lock():
        if inspect_installation() is not None or JOURNAL.exists() or JOURNAL.is_symlink():
            raise ControlError("control_installation_review_changed")
        uid, gid = _account_ids()
        # Never take ownership of pre-existing content, including an empty
        # unprivileged directory at one of these fixed destinations.
        if any(Path(name).exists() or Path(name).is_symlink() for name in SERVICE_DIRECTORIES):
            raise ControlError("control_existing_directory_not_adopted")
        intent = ProvisioningIntent(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), uid, gid,
            draft.deployment.payload(), draft.start_at_boot, manifest["bundle_id"], manifest_sha256, application_commit=selected_source)
        _persist(intent)
    result = resume_install(intent.installation_id, manifest=manifest, manifest_sha256=manifest_sha256)
    if result:
        return result
    return complete_install(intent.installation_id, manifest=manifest, manifest_sha256=manifest_sha256, automatic=True)
