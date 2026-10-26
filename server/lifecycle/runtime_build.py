"""Unprivileged construction and verification of permanent runtime generations.

Preparation never changes the active execution or the two public web links.
All package-manager processes run as the dedicated service account, with a fresh
environment and private logs. A failed generation remains inactive for repair.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tarfile
import time
from typing import Callable
import urllib.request
import urllib.error
import uuid
import zipfile

from . import state
from .control.support import NODE_VERSION, NPM_VERSION, NODE_SHA256, RCLONE_VERSION, RCLONE_SHA256


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class RuntimeBuildError(RuntimeError):
    code = "runtime_preparation_failed"


@dataclass(frozen=True)
class PreparedRuntime:
    bundle_id: str
    python_id: str
    node_id: str
    web_id: str
    manifest_sha256: str
    web_sha256: str


def generation(value: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value) or not uuid.UUID(value).int:
        raise RuntimeBuildError("The runtime generation is invalid.")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def source_inputs(root: Path, commit: str) -> dict[str, bytes]:
    """Snapshot committed inputs, verifying each open file against its Git blob.

    No working-tree filter, Git hook, credential helper or repository executable
    is used. A concurrent checkout/change is rejected before artifact activation.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeBuildError("The source commit identity is invalid.")
    environment = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0"}
    git = ["/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    head = subprocess.check_output(git + ["rev-parse", "HEAD"], cwd=root, env=environment, timeout=10).decode().strip()
    if head != commit:
        raise RuntimeBuildError("The application source changed after review.")
    listing = subprocess.check_output(git + ["ls-tree", "-rz", "--full-tree", commit, "--", "server", "web"], cwd=root, env=environment, timeout=15)
    if len(listing) > 2 * 1024 * 1024:
        raise RuntimeBuildError("The application source inventory exceeds its bound.")
    result, total = {}, 0
    for item in filter(None, listing.split(b"\0")):
        metadata, name = item.split(b"\t", 1)
        mode, kind, identity = metadata.decode("ascii").split()
        relative = PurePosixPath(os.fsdecode(name))
        if (mode not in {"100644", "100755"} or kind != "blob" or not re.fullmatch(r"[0-9a-f]{40}", identity)
                or relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in {"server", "web"}):
            raise RuntimeBuildError("The application source contains an unsupported tracked input.")
        descriptor = os.open(root / relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 10 * 1024 * 1024:
                raise RuntimeBuildError("An application source input exceeds its bound.")
            raw = handle.read(10 * 1024 * 1024 + 1)
            after = os.fstat(handle.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RuntimeBuildError("An application source input changed during inspection.")
        total += len(raw)
        if total > 64 * 1024 * 1024 or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest() != identity:
            raise RuntimeBuildError("Application source bytes do not match the reviewed commit.")
        result[relative.as_posix()] = raw
    if not all(name in result for name in ("server/requirements.txt", "server/requirements.lock", "web/package.json", "web/package-lock.json", "server/main.py")):
        raise RuntimeBuildError("The committed application source is incomplete.")
    return result


def require_supported_host() -> None:
    from .control.support import require_supported_platform, UnsupportedPlatform
    try:
        require_supported_platform()
    except UnsupportedPlatform as exc:
        raise RuntimeBuildError(str(exc)) from exc


def clean_environment(home: Path, node_bin: Path | None = None) -> dict[str, str]:
    paths = ([str(node_bin)] if node_bin is not None else []) + ["/usr/bin", "/bin"]
    return {
        "PATH": os.pathsep.join(paths), "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "TMPDIR": str(home / "tmp"),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1", "PIP_CONFIG_FILE": "/dev/null", "PYTHONUNBUFFERED": "1",
        "npm_config_userconfig": str(home / "user.npmrc"), "npm_config_globalconfig": str(home / "global.npmrc"),
        "npm_config_cache": str(home / "npm-cache"), "npm_config_update_notifier": "false",
        "npm_config_audit": "false", "npm_config_fund": "false", "CI": "true",
    }


def run_stage(arguments: list[str], *, cwd: Path, environment: dict[str, str], log: Path, timeout: int = 1800,
              checkpoint: Callable[[], None] | None = None) -> None:
    """Bound an owned child group and private output; never invoke a shell."""
    if not arguments or not Path(arguments[0]).is_absolute() or not 0 < timeout <= 3600:
        raise RuntimeBuildError("The runtime preparation command is invalid.")
    if checkpoint is not None:
        checkpoint()
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    process = None
    reaped = False
    try:
        with os.fdopen(fd, "wb") as output:
            process = subprocess.Popen(arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout
            while True:
                if checkpoint is not None:
                    checkpoint()
                if time.monotonic() > deadline or os.fstat(output.fileno()).st_size > 32 * 1024 * 1024:
                    raise RuntimeBuildError("Runtime preparation exceeded its time or output bound; inspect its private stage log.")
                try:
                    status = process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    continue
                reaped = True
                if status:
                    raise RuntimeBuildError("Runtime preparation failed; inspect its private stage log.")
                output.flush()
                os.fsync(output.fileno())
                return
    except OSError as exc:
        raise RuntimeBuildError("Runtime preparation could not start; inspect the inactive generation.") from exc
    finally:
        if process is not None and not reaped:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def download(url: str, destination: Path, expected: str, *, limit: int = 200 * 1024 * 1024,
             checkpoint: Callable[[], None] | None = None) -> None:
    if not url.startswith("https://"):
        raise RuntimeBuildError("Runtime downloads require authenticated HTTPS.")
    deadline = time.monotonic() + 900
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "StreamHome-control/1"})
    for attempt in range(3):
        if checkpoint is not None:
            checkpoint()
        if time.monotonic() >= deadline:
            raise RuntimeBuildError("The runtime download exceeded its time bound.")
        digest, total = hashlib.sha256(), 0
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as output, opener.open(request, timeout=45) as response:
                if not response.url.startswith("https://"):
                    raise RuntimeBuildError("The runtime download left HTTPS.")
                while True:
                    if checkpoint is not None:
                        checkpoint()
                    if time.monotonic() >= deadline:
                        raise RuntimeBuildError("The runtime download exceeded its time bound.")
                    # read1 returns available response bytes after one raw read.
                    # read(n) can keep filling n under a slow trickle and defer
                    # the absolute deadline check indefinitely.
                    block = response.read1(64 * 1024)
                    if not block:
                        if getattr(response, "length", None) not in {None, 0}:
                            raise http.client.IncompleteRead(b"")
                        break
                    total += len(block)
                    if total > limit or time.monotonic() >= deadline:
                        raise RuntimeBuildError("The runtime download exceeded its bound.")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != expected:
                raise RuntimeBuildError("Runtime download verification failed; the artifact was not executed.")
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            destination.unlink(missing_ok=True)
            # Retry only transient transport/status failures, with a fresh file
            # and digest. Certificate failures and permanent HTTP refusals never
            # downgrade verification or silently select another artifact.
            permanent_http = isinstance(exc, urllib.error.HTTPError) and exc.code not in {408, 429, 500, 502, 503, 504}
            invalid_certificate = isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError)
            if permanent_http or invalid_certificate or attempt == 2 or time.monotonic() >= deadline:
                raise RuntimeBuildError("The verified runtime download is unavailable; the inactive artifact was discarded.") from exc
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def tree_inventory(directory: Path) -> dict[str, str]:
    """Relative file/link evidence; sockets, devices and escaping links fail closed."""
    directory = directory.resolve(strict=True)
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            resolved = path.resolve(strict=True)
            # Python venv interpreters intentionally reference the system Python.
            if not resolved.is_relative_to(directory) and resolved != Path("/usr/bin/python3.12"):
                raise RuntimeBuildError("A runtime link escapes its generation.")
            result[relative] = "link:" + target
        elif stat.S_ISREG(mode):
            result[relative] = file_sha256(path)
        elif not stat.S_ISDIR(mode):
            raise RuntimeBuildError("A runtime generation contains a special file.")
    return result


def _node_archive(archive: Path, directory: Path) -> None:
    prefix = f"node-v{NODE_VERSION}-linux-x64"
    with tarfile.open(archive, "r:xz") as package:
        members = package.getmembers()
        if len(members) > 20000 or sum(member.size for member in members) > 512 * 1024 * 1024:
            raise RuntimeBuildError("The Node archive exceeds its extraction bound.")
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != prefix or not (member.isfile() or member.isdir() or member.issym()):
                raise RuntimeBuildError("The Node archive contains an unsupported entry.")
        package.extractall(directory, members=members, filter="data")
    extracted = directory / prefix
    for path in extracted.iterdir():
        path.rename(directory / path.name)
    extracted.rmdir()


def require_runtime_root(root: Path, *, create: bool = False) -> Path:
    """Do not follow an aliased or externally writable generation ancestor."""
    runtime = root/'runtime'
    try:
        if create:
            runtime.mkdir(mode=0o700, exist_ok=True)
        metadata = runtime.lstat()
    except OSError as exc:
        raise RuntimeBuildError('The bound runtime root is unavailable.') from exc
    if (not stat.S_ISDIR(metadata.st_mode) or runtime.is_symlink()
            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
        raise RuntimeBuildError('The bound runtime root is aliased or unprotected.')
    return runtime


def preparation_source(root: Path, source_root: Path | None) -> Path:
    """Read an exact target checkout without treating it as the runtime root.

    An alternate source is private operation-scoped cache evidence. This only
    selects committed build inputs; it does not authorize a release or cutover.
    """
    if source_root is None or source_root == root:
        return root
    from .layout import layout_for_root
    layout = layout_for_root(root)
    if not layout.managed or not isinstance(source_root, Path):
        raise RuntimeBuildError("A target checkout requires a managed installation.")
    base = layout.cache / 'update-preparation'
    try:
        relative = source_root.relative_to(base)
        if len(relative.parts) != 2 or relative.parts[1] != 'checkout':
            raise ValueError('unexpected target checkout path')
        generation(relative.parts[0])
        for directory in (layout.cache, base, source_root.parent, source_root):
            state._validate_directory(directory, 'private target source checkout')
        if source_root.resolve(strict=True) != source_root:
            raise ValueError('aliased target checkout')
    except (OSError, ValueError, state.LifecycleError) as exc:
        raise RuntimeBuildError("The target source checkout is not private and canonical.") from exc
    return source_root


def prepare_runtime(root: Path, *, build_id: str, application_commit: str, stage: Callable = None,
                    checkpoint: Callable[[], None] | None = None,
                    source_root: Path | None = None) -> PreparedRuntime:
    """Build a new inactive generation at its permanent path, never move a venv."""
    if checkpoint is not None:
        checkpoint()
    if os.geteuid() == 0:
        raise RuntimeBuildError("Application environments must be built as the dedicated service account.")
    require_supported_host()
    if root != Path("/opt/streamhome") or root.resolve(strict=True) != root:
        raise RuntimeBuildError("Runtime preparation requires the canonical managed root.")
    if not re.fullmatch(r"[0-9a-f]{40}", application_commit) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", build_id):
        raise RuntimeBuildError("The application build identity is invalid.")
    target_source = preparation_source(root, source_root)
    inputs = source_inputs(target_source, application_commit)
    if checkpoint is not None:
        checkpoint()
    from contextlib import contextmanager, nullcontext
    progress = stage or (lambda description: nullcontext())
    @contextmanager
    def stage(description):
        if checkpoint is not None:
            checkpoint()
        with progress(description):
            yield
        if checkpoint is not None:
            checkpoint()
    def execute(arguments, **kwargs):
        return run_stage(arguments, checkpoint=checkpoint, **kwargs)
    def retrieve(url, destination, expected):
        from .artifact_cache import cached_artifact
        return cached_artifact(destination, expected,
            fetch=lambda temporary: download(url, temporary, expected, checkpoint=checkpoint), checkpoint=checkpoint)
    ids = [str(uuid.uuid4()) for _ in range(4)]
    bundle, python_id, node_id, web_id = ids
    runtime = require_runtime_root(root, create=True)
    for category in ("bundles", "python", "node", "web"):
        (runtime / category).mkdir(mode=0o700, parents=True, exist_ok=True)
        state._validate_directory(runtime / category, "runtime generation parent")
    bundle_dir, python_dir, node_dir, web_dir = (runtime / category / identity for category, identity in zip(("bundles", "python", "node", "web"), ids))
    for directory in (bundle_dir, python_dir, node_dir, web_dir):
        directory.mkdir(mode=0o700)
    home = bundle_dir / "build-home"
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    captured = bundle_dir / "inputs"
    captured.mkdir(mode=0o700)
    for name in ("requirements.txt", "requirements.lock"):
        (captured / name).write_bytes(inputs["server/" + name])
    environment = clean_environment(home)
    with stage("Downloading and verifying the application Node.js runtime"):
        archive = bundle_dir / "node.tar.xz"
        retrieve(f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz", archive, NODE_SHA256)
        _node_archive(archive, node_dir)
        archive.unlink()
        for executable, expected in (([str(node_dir / "bin/node"), "--version"], "v" + NODE_VERSION),
                                     ([str(node_dir / "bin/node"), str(node_dir / "lib/node_modules/npm/bin/npm-cli.js"), "--version"], NPM_VERSION)):
            actual = subprocess.check_output(executable, env=environment, timeout=30, text=True).strip()
            if actual != expected:
                raise RuntimeBuildError("The application Node.js/npm versions do not match the supported runtime.")
    with stage("Creating the permanent Python environment"):
        execute(["/usr/bin/python3", "-m", "venv", str(python_dir / "venv")], cwd=root, environment=environment, log=bundle_dir / "python-create.log", timeout=120)
        execute([str(python_dir / "venv/bin/python"), "-m", "pip", "install", "-r", str(captured / "requirements.txt"), "-c", str(captured / "requirements.lock")], cwd=root, environment=environment, log=bundle_dir / "python-install.log")
        execute([str(python_dir / "venv/bin/python"), "-m", "pip", "check"], cwd=root, environment=environment, log=bundle_dir / "python-check.log", timeout=60)
    with stage("Installing web dependencies with the application npm"):
        source = web_dir / "source"
        source.mkdir(mode=0o700)
        # Only tracked web inputs are copied; active dist/dependencies and local
        # environment files are never copied into a candidate bundle.
        for name, raw in inputs.items():
            if not name.startswith("web/"):
                continue
            relative = PurePosixPath(name).relative_to("web")
            if any(part in {"node_modules", "dist", ".git", ".npmrc"} or part.startswith(".env") for part in relative.parts):
                raise RuntimeBuildError("A tracked web input is unsafe for runtime preparation.")
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(source / name, web_dir / name)
        environment = clean_environment(home, node_dir / "bin")
        npm = [str(node_dir / "bin/node"), str(node_dir / "lib/node_modules/npm/bin/npm-cli.js")]
        execute(npm + ["ci", "--include=dev"], cwd=web_dir, environment=environment, log=bundle_dir / "web-install.log")
    with stage("Building the paired web bundle"):
        environment.update(STREAMHOME_BUILD_ID=build_id, VITE_BUILD_ID=build_id)
        environment["PATH"] = str(web_dir / "node_modules/.bin") + os.pathsep + environment["PATH"]
        execute(npm + ["run", "build", "--", "--outDir", str(web_dir / "dist")], cwd=source, environment=environment, log=bundle_dir / "web-build.log")
        if (web_dir / "dist/.streamhome-build").read_text().strip() != build_id:
            raise RuntimeBuildError("The built web assets do not match the candidate release.")
        assets = tree_inventory(web_dir / "dist")
        state._atomic_write_json(web_dir / "assets.json", assets)
    with stage("Installing the application Rclone runtime"):
        tools = bundle_dir / "tools"
        tools.mkdir(mode=0o700)
        archive = bundle_dir / "rclone.zip"
        retrieve(f"https://downloads.rclone.org/v{RCLONE_VERSION}/rclone-v{RCLONE_VERSION}-linux-amd64.zip", archive, RCLONE_SHA256)
        name = f"rclone-v{RCLONE_VERSION}-linux-amd64/rclone"
        with zipfile.ZipFile(archive) as package:
            info = package.getinfo(name)
            if info.file_size > 150 * 1024 * 1024:
                raise RuntimeBuildError("The Rclone executable exceeds its size bound.")
            with package.open(info) as source, (tools / "rclone").open("xb") as destination:
                shutil.copyfileobj(source, destination)
        (tools / "rclone").chmod(0o700)
        archive.unlink()
    with stage("Verifying the complete runtime generation"):
        if preparation_source(root, source_root) != target_source or source_inputs(target_source, application_commit) != inputs:
            raise RuntimeBuildError("The application source changed during runtime preparation.")
        manifest = {
            "schema_version": 1, "bundle_id": bundle, "application_commit": application_commit, "application_build_id": build_id,
            "python_id": python_id, "node_id": node_id, "web_id": web_id,
            "node_version": NODE_VERSION, "npm_version": NPM_VERSION,
            "python": tree_inventory(python_dir), "node": tree_inventory(node_dir), "web": tree_inventory(web_dir),
            "tools": tree_inventory(bundle_dir / "tools"),
            "requirements_sha256": hashlib.sha256(inputs["server/requirements.lock"]).hexdigest(),
            "web_lock_sha256": hashlib.sha256(inputs["web/package-lock.json"]).hexdigest(),
            "source": {name: hashlib.sha256(raw).hexdigest() for name, raw in inputs.items()},
        }
        state._atomic_write_json(bundle_dir / "manifest.json", manifest)
        from .layout import layout_for_root
        from .runtime_recovery_cache import capture
        capture(bundle_dir, layout_for_root(root).cache / 'runtime-recovery', file_sha256(bundle_dir / 'manifest.json'),
                checkpoint=checkpoint or (lambda: None))
    return PreparedRuntime(bundle, python_id, node_id, web_id, file_sha256(bundle_dir / "manifest.json"), file_sha256(web_dir / "assets.json"))


def verify_runtime(root: Path, execution, *, source_root: Path | None = None) -> dict:
    """Verify a bound candidate before launch; inspection never repairs artifacts."""
    runtime = require_runtime_root(root)
    bundle = runtime / "bundles" / generation(execution.runtime_bundle_id)
    path = bundle / "manifest.json"
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024 or file_sha256(path) != execution.runtime_manifest_sha256:
        raise RuntimeBuildError("The bound runtime manifest is unavailable or changed.")
    manifest = json.loads(path.read_bytes())
    if (manifest.get("schema_version") != 1 or manifest.get("bundle_id") != execution.runtime_bundle_id
            or manifest.get("application_commit") != execution.application_commit or manifest.get("application_build_id") != execution.application_build_id
            or manifest.get("python_id") != execution.python_environment_id or manifest.get("node_id") != execution.node_environment_id or manifest.get("web_id") != execution.web_bundle_id):
        raise RuntimeBuildError("The runtime manifest does not bind the selected execution.")
    source = preparation_source(root, source_root)
    if {name: hashlib.sha256(raw).hexdigest() for name, raw in source_inputs(source, execution.application_commit).items()} != manifest.get("source"):
        raise RuntimeBuildError("The application source no longer matches its runtime generation.")
    for category, identity, key in (("python", execution.python_environment_id, "python"), ("node", execution.node_environment_id, "node"), ("web", execution.web_bundle_id, "web")):
        if tree_inventory(root / "runtime" / category / generation(identity)) != manifest.get(key):
            raise RuntimeBuildError("A bound runtime generation changed after preparation.")
    if tree_inventory(bundle / "tools") != manifest.get("tools") or file_sha256(root / "runtime/web" / execution.web_bundle_id / "assets.json") != execution.web_asset_manifest_sha256:
        raise RuntimeBuildError("The bound tools or web asset manifest changed.")
    return manifest
