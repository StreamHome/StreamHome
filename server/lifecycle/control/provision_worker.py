"""Service-account first-install preparation in its own systemd cgroup."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import urllib.request

from .. import management_journal as contract, state
from ..command_runner import run_bounded
from ..configuration_store import ConfigurationStore, DeploymentRevision, _decode
from ..deployment_config import parse_deployment_payload
from ..execution_state import ExecutionRecord, parse_execution_record, publish_initial_execution, read_active_execution
from ..installation import InstallationStatus, inspect_installation
from ..layout import layout_for_root
from ..runtime_build import PreparedRuntime, prepare_runtime, source_inputs, verify_runtime
from ..runtime_links import bind_initial_runtime, verify_links
from ..setup_bootstrap import provision_eligibility
from .processes import process_cgroup
from .provisioning_contract import JOURNAL_PATH, identity, parse_intent
from .rpc import ControlError
from .tickets import read_protected_json
from .support import PREPARATION_STAGES


ROOT = Path("/opt/streamhome")
REPOSITORY = "https://github.com/StreamHome/StreamHome.git"
_GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0"}
_GIT = ["/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "credential.helper=", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]


def select_source() -> dict:
    result = run_bounded(_GIT + ["ls-remote", "--exit-code", REPOSITORY, "refs/heads/main"],
                         cwd=Path("/"), environment=_GIT_ENV, timeout=30, maximum_output=8192)
    try:
        commit, branch = result.stdout.decode("ascii").strip().split("\t")
    except (ValueError, UnicodeError) as exc:
        raise ControlError("control_source_revision_invalid") from exc
    if result.returncode or branch != "refs/heads/main" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ControlError("control_source_revision_invalid")
    return {"application_commit": commit}


def _checkout(intent) -> None:
    initialized = run_bounded(_GIT + ["init", "--quiet", str(ROOT)], cwd=Path("/"), environment=_GIT_ENV, timeout=15, maximum_output=8192)
    if initialized.returncode:
        raise ControlError("control_source_checkout_failed")
    origin = run_bounded(_GIT + ["-C", str(ROOT), "remote", "get-url", "origin"], cwd=Path("/"), environment=_GIT_ENV, timeout=15, maximum_output=8192)
    if origin.returncode:
        added = run_bounded(_GIT + ["-C", str(ROOT), "remote", "add", "origin", REPOSITORY], cwd=Path("/"), environment=_GIT_ENV, timeout=15, maximum_output=8192)
        if added.returncode:
            raise ControlError("control_source_checkout_failed")
    elif origin.stdout.decode("ascii").strip() != REPOSITORY:
        raise ControlError("control_source_repository_changed")
    for arguments in (["-C", str(ROOT), "fetch", "--no-tags", "--no-recurse-submodules", "--depth=1", REPOSITORY, intent.application_commit],
                      ["-C", str(ROOT), "checkout", "--quiet", "--detach", intent.application_commit]):
        result = run_bounded(_GIT + arguments, cwd=Path("/"), environment=_GIT_ENV, timeout=60, maximum_output=1024 * 1024)
        if result.returncode:
            raise ControlError("control_source_checkout_failed")
    source_inputs(ROOT, intent.application_commit)


def _review(operation_id: str):
    identity(operation_id)
    selected = inspect_installation()
    intent = parse_intent(read_protected_json(Path(JOURNAL_PATH)))
    if (selected is None or selected.descriptor.status is not InstallationStatus.PROVISIONING
            or os.geteuid() == 0 or selected.descriptor.service_uid != os.geteuid()
            or (intent.installation_id, intent.service_uid, intent.service_gid, intent.operation_id)
               != (selected.descriptor.installation_id, os.geteuid(), os.getegid(), operation_id)
            or intent.stage != "preparing_application"):
        raise ControlError("control_initial_preparation_forbidden")
    return intent


def _no_runtime_children() -> None:
    path = Path("/sys/fs/cgroup/system.slice/streamhome.service/cgroup.procs")
    try:
        contents = path.read_text()
    except FileNotFoundError:
        return
    if contents.strip():
        raise ControlError("control_initial_runtime_already_running")


def initial_authority() -> dict:
    layout = layout_for_root(ROOT)
    if not layout.managed or layout.descriptor.descriptor.status is not InstallationStatus.PROVISIONING:
        raise ControlError("control_initial_preparation_forbidden")
    directory = layout.lifecycle
    published = any((layout.state / name).exists() or (layout.state / name).is_symlink()
                    for name in ("active-runtime.json", "database.db", "setup-finalization.json", "runtime-links.json"))
    state._validate_directory(layout.domains, "initial configuration directory")
    published = published or bool(os.listdir(layout.domains))
    if not directory.exists() and not directory.is_symlink():
        if published:
            raise ControlError("control_initial_authority_missing")
        return {"initialized": False, "generation": 0}
    state._validate_directory(directory, "initial lifecycle directory")
    path = directory / "state.json"
    if not path.exists() and not path.is_symlink():
        entries = os.listdir(directory)
        if published or set(entries) - {"state.lock"}:
            raise ControlError("control_initial_authority_missing")
        if entries:
            metadata = (directory / "state.lock").lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o7177 or metadata.st_nlink != 1):
                raise ControlError("control_initial_authority_missing")
        return {"initialized": False, "generation": 0}
    authority = state.inspect_state(ROOT)
    if authority.schema_version != 2 or authority.generation != 0 or authority.active_operation is not None:
        raise ControlError("control_operation_recovery_required")
    return {"initialized": True, "generation": 0}


def _private_record(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_mode & 0o7177
                or before.st_nlink != 1 or before.st_size > 32768):
            raise ControlError("control_initial_result_untrusted")
        raw = handle.read(32769)
        after, linked = os.fstat(handle.fileno()), path.lstat()
        identity_of = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_gid, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns, item.st_nlink)
        if len(raw) > 32768 or identity_of(before) != identity_of(after) or identity_of(before) != identity_of(linked):
            raise ControlError("control_initial_result_changed")
    return _decode(raw)


def _initial_execution(intent, runtime: PreparedRuntime) -> ExecutionRecord:
    base = ROOT / "runtime"
    execution = contract.ExecutionTuple(
        application_commit=intent.application_commit, application_build_id=intent.application_commit[:12],
        deployment_revision=intent.revision_id, runtime_bundle_id=runtime.bundle_id, runtime_manifest_sha256=runtime.manifest_sha256,
        python_environment_id=runtime.python_id, python_executable=str(base / "python" / runtime.python_id / "venv/bin/python"),
        node_environment_id=runtime.node_id, node_executable=str(base / "node" / runtime.node_id / "bin/node"),
        web_bundle_id=runtime.web_id, web_dependency_target=str(base / "web" / runtime.web_id / "node_modules"),
        web_dist_target=str(base / "web" / runtime.web_id / "dist"), web_asset_manifest_sha256=runtime.web_sha256,
        control_bundle_id=intent.control_bundle_id, control_manifest_sha256=intent.control_manifest_sha256,
        control_protocol_version=1, policy_revisions=(), provider_revisions=(),
    )
    return parse_execution_record(ExecutionRecord(intent.installation_id, execution, intent.operation_id, 0).payload())


def _verify_prepared(intent, record: ExecutionRecord) -> None:
    if (record.installation_id != intent.installation_id or record.operation_id != intent.operation_id or record.generation != 0
            or record.execution.application_commit != intent.application_commit or record.execution.deployment_revision != intent.revision_id
            or record.execution.control_bundle_id != intent.control_bundle_id or record.execution.control_manifest_sha256 != intent.control_manifest_sha256
            or record.execution.policy_revisions or record.execution.provider_revisions):
        raise ControlError("control_initial_result_changed")
    verify_runtime(ROOT, record.execution)


def initial_result(operation_id: str) -> dict:
    intent = _review(operation_id)
    layout = layout_for_root(ROOT)
    initial_authority()
    record = read_active_execution(ROOT)
    _verify_prepared(intent, record)
    if record != parse_execution_record(_private_record(layout.state / "preparation/prepared.json")):
        raise ControlError("control_initial_result_changed")
    verify_links(ROOT, record.execution)
    return {"installation_id": record.installation_id, "operation_id": operation_id,
            "execution_sha256": contract.execution_tuple_sha256(record.execution), "application_commit": intent.application_commit}


def initial_retry_check() -> dict:
    from .processes import require_service_quiescent
    from .provisioning_contract import intent_sha256
    layout = layout_for_root(ROOT)
    selected = layout.descriptor.descriptor
    intent = parse_intent(read_protected_json(Path(JOURNAL_PATH)))
    if (os.geteuid() == 0 or os.geteuid() != selected.service_uid or selected.status is not InstallationStatus.PROVISIONING
            or intent.installation_id != selected.installation_id or intent.stage != "first_start_requested"
            or intent.start_permit not in {"issued", "consumed"}):
        raise ControlError("control_start_retry_unavailable")
    require_service_quiescent()
    if initial_authority() != {"initialized": True, "generation": 0}:
        raise ControlError("control_operation_recovery_required")
    record = read_active_execution(ROOT)
    _verify_prepared(intent, record)
    if (record != parse_execution_record(_private_record(layout.state / "preparation/prepared.json"))
            or intent.execution_sha256 != contract.execution_tuple_sha256(record.execution)):
        raise ControlError("control_initial_result_changed")
    verify_links(ROOT, record.execution)
    directory = os.open(layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        with ConfigurationStore(directory, selected.installation_id) as store:
            manifest = store.inspect()
            if (manifest.active != manifest.desired or manifest.active.revision != intent.revision_id
                    or manifest.quarantined or manifest.policy_revisions or manifest.provider_revisions
                    or store.read_revision(manifest.active).settings.payload() != intent.deployment):
                raise ControlError("control_initial_result_changed")
    finally:
        os.close(directory)
    require_service_quiescent()
    return {"installation_id": intent.installation_id, "intent_sha256": intent_sha256(intent),
            "generation": 0, "execution_sha256": intent.execution_sha256}


def initial_progress(operation_id: str) -> dict:
    _review(operation_id)
    path = layout_for_root(ROOT).state / "preparation/progress.json"
    value = _private_record(path)
    if (not isinstance(value, dict) or set(value) != {"stage", "status"}
            or value["stage"] not in PREPARATION_STAGES or value["status"] not in {"running", "done"}):
        raise ControlError("control_initial_progress_invalid")
    return value


def initial_listener(payload: dict) -> dict:
    from ..port_check import OwnedWebListener, check_web_port
    from .tickets import require_first_start
    if (not isinstance(payload, dict) or set(payload) != {"invocation", "web"}
            or not isinstance(payload["web"], dict) or set(payload["web"]) != {"pid", "start_identity"}):
        raise ControlError("control_request_invalid")
    layout = layout_for_root(ROOT)
    selected = layout.descriptor.descriptor
    intent = parse_intent(read_protected_json(Path(JOURNAL_PATH)))
    record = read_active_execution(ROOT)
    if (selected.status is not InstallationStatus.PROVISIONING or intent.stage != "first_start_requested"
            or intent.installation_id != selected.installation_id or intent.start_invocation != payload["invocation"]
            or intent.execution_sha256 != contract.execution_tuple_sha256(record.execution)):
        raise ControlError("control_first_start_denied")
    require_first_start(layout, record.execution, payload["invocation"])
    deployment = parse_deployment_payload(intent.deployment)
    owner = OwnedWebListener(selected.installation_id, selected.service_uid, payload["invocation"],
        payload["web"]["pid"], payload["web"]["start_identity"], deployment.web_bind, deployment.web_port)
    if not check_web_port(deployment.web_bind, deployment.web_port, owner=owner, installation=selected).reusable_owned_listener:
        raise ControlError("control_web_listener_missing")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{deployment.web_port}/api/health", timeout=2) as response:
        raw = response.read(32769)
        if len(raw) > 32768:
            raise ControlError("control_health_invalid")
        value = _decode(raw)
        if (not isinstance(value, dict) or value.get("readyKind") != "general" or value.get("buildId") != record.execution.application_build_id
                or response.headers.get("X-StreamHome-Web-Build") != record.execution.application_build_id):
            raise ControlError("control_web_identity_changed")
    return {"ready": True, "execution_sha256": intent.execution_sha256, "invocation": payload["invocation"]}


def main(operation_id: str, *, control_bundle_id: str, control_manifest_sha256: str) -> int:
    intent = _review(operation_id)
    if (process_cgroup(os.getpid()) != f"/system.slice/streamhome-provision@{operation_id}.service"
            or intent.control_bundle_id != control_bundle_id or intent.control_manifest_sha256 != control_manifest_sha256):
        raise ControlError("control_initial_preparation_forbidden")
    layout = layout_for_root(ROOT)
    initial_authority()
    directory = layout.state / "preparation"
    directory.mkdir(mode=0o700, exist_ok=True)
    state._validate_directory(directory, "initial preparation directory")
    lock = os.open(directory / "worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        metadata = os.fstat(lock)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o7177 or metadata.st_nlink != 1:
            raise ControlError("control_initial_preparation_lock_untrusted")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControlError("control_initial_preparation_busy") from exc

        def require_current():
            if _review(operation_id) != intent:
                raise ControlError("control_installation_review_changed")
            initial_authority()

        @contextmanager
        def stage(description):
            require_current()
            if description not in PREPARATION_STAGES:
                raise ControlError("control_initial_progress_invalid")
            state._atomic_write_json(directory / "progress.json", {"stage": description, "status": "running"})
            print("[StreamHome] " + description + " ...", flush=True)
            yield
            require_current()
            state._atomic_write_json(directory / "progress.json", {"stage": description, "status": "done"})
            print("[StreamHome] " + description + ": done", flush=True)

        _no_runtime_children()
        prepared_path = directory / "prepared.json"
        if not prepared_path.exists() and not prepared_path.is_symlink():
            if (layout.state / "active-runtime.json").exists() or (layout.state / "active-runtime.json").is_symlink():
                raise ControlError("control_initial_result_missing")
            with stage("Downloading the selected StreamHome source"):
                _checkout(intent)
            state.initialize_managed_authority(ROOT, installation_id=intent.installation_id)
            domains = os.open(layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                with ConfigurationStore(domains, intent.installation_id) as store:
                    store.initialize_provisional(ROOT, DeploymentRevision(intent.installation_id, intent.revision_id, parse_deployment_payload(intent.deployment)))
            finally:
                os.close(domains)
            runtime = prepare_runtime(ROOT, application_commit=intent.application_commit, build_id=intent.application_commit[:12], stage=stage)
            require_current()
            record = _initial_execution(intent, runtime)
            # Persist the full immutable candidate before either public binding.
            state._atomic_write_json(prepared_path, record.payload())
        else:
            record = parse_execution_record(_private_record(prepared_path))
        _verify_prepared(intent, record)
        require_current()
        _no_runtime_children()
        publish_initial_execution(ROOT, record)
        bind_initial_runtime(ROOT, record.execution, installation_operation_id=operation_id, assert_children_stopped=_no_runtime_children)
        provision_eligibility(layout.state, intent.installation_id)
        # This mirror is unprivileged evidence only. Root resumes from its own
        # protected journal, never from files beneath the service-owned state.
        state._atomic_write_json(directory / "initial-review.json", intent.payload())
        return 0
    finally:
        os.close(lock)
