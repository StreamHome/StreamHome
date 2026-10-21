#!/usr/bin/python3 -I
"""Unprivileged authenticated bootstrap for the fixed managed installation.

The first privileged programs are distro-owned install, sha256sum and OpenSSL.
Only a verified, root-staged publisher package may introduce privileged code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid


PUBLISHER_SHA256 = "85ffa64c6c3ad266b69d915022b36b26145ae0b405954a6d84f88b99482fd12d"
DISTRIBUTION = "https://raw.githubusercontent.com/StreamHome/StreamHome/main/distribution/control/linux-amd64/"
ENTRY = Path("/usr/local/libexec/streamhome/provision")
ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
LIMITS = {"publisher.pem": 4096, "release.json": 8192, "release.sig": 128, "streamhome-control-bootstrap.deb": 8 * 1024 * 1024}


class BootstrapError(RuntimeError):
    pass


def message(value: str) -> None:
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", value)
    print("[StreamHome] " + text, flush=True)


def decode_release(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BootstrapError("The publisher release contains duplicate fields.")
            result[key] = value
        return result
    if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMITS["release.json"]:
        raise BootstrapError("The publisher release has an invalid size.")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BootstrapError("Invalid publisher release number.")))
        required = {"schema_version", "format", "bundle_id", "manifest_sha256", "artifact", "artifact_sha256", "publisher_sha256", "entry_sha256"}
        if (not isinstance(value, dict) or set(value) != required or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["format"] != "streamhome-control-bootstrap-deb-v1" or value["artifact"] != "streamhome-control-bootstrap.deb"
                or not isinstance(value["bundle_id"], str) or str(uuid.UUID(value["bundle_id"])) != value["bundle_id"] or not uuid.UUID(value["bundle_id"]).int
                or any(not isinstance(value[name], str) or not re.fullmatch(r"[0-9a-f]{64}", value[name])
                       for name in ("manifest_sha256", "artifact_sha256", "publisher_sha256", "entry_sha256"))
                or value["publisher_sha256"] != PUBLISHER_SHA256):
            raise BootstrapError("The publisher release is incompatible or untrusted.")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BootstrapError("The publisher release is invalid JSON.") from exc
    return value


def require_platform() -> None:
    release = platform.freedesktop_os_release()
    if (sys.platform != "linux" or release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04"
            or platform.machine() != "x86_64" or platform.libc_ver()[0] != "glibc" or sys.version_info[:3] != (3, 12, 3)
            or Path("/proc/1/comm").read_text().strip() != "systemd" or not Path("/sys/fs/cgroup/cgroup.controllers").is_file()):
        raise BootstrapError("Managed installation requires Ubuntu 24.04 x86_64, Python 3.12.3, glibc, systemd and cgroup v2.")
    for executable in ("/usr/bin/python3", "/usr/bin/sudo", "/usr/bin/openssl", "/usr/bin/dpkg", "/usr/bin/dpkg-deb", "/usr/bin/install", "/usr/bin/sha256sum", "/usr/bin/env", "/usr/bin/cat"):
        if not Path(executable).is_file():
            raise BootstrapError("A required Ubuntu bootstrap program is missing: " + executable)


def protected_entry() -> bool:
    current = Path("/")
    for component in ENTRY.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        if (metadata.st_uid != 0 or metadata.st_mode & 0o7022
                or (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 if current == ENTRY else not stat.S_ISDIR(metadata.st_mode))):
            raise BootstrapError("The installed control entry or a parent is unprotected. Use an independently verified recovery artifact.")
    return True


def existing_evidence() -> bool:
    for name in ("/etc/streamhome/install.json", "/etc/streamhome/control/installation.json"):
        try:
            Path(name).lstat()
        except FileNotFoundError:
            continue
        return True
    return False


def legacy_hints() -> tuple[Path, ...]:
    """Bounded local discovery only; never execute a checkout or follow its Git URL."""
    candidates = [Path.cwd(), Path.home() / "StreamHome"]
    for name in ("STREAMHOME_INSTANCE_ROOT", "STREAMHOME_INSTALL_DIR"):
        value = os.environ.get(name)
        if value and len(value) <= 4096:
            candidates.append(Path(value).expanduser().absolute())
    result = []
    for candidate in candidates:
        if candidate in result or candidate == Path("/opt/streamhome"):
            continue
        try:
            # Existing conventional directories are preserved even if incomplete
            # or not readable. A symlink is evidence, never a fresh destination.
            candidate.lstat()
            if candidate == Path.cwd() and not ((candidate / "server/main.py").is_file() and (candidate / "start.sh").is_file()):
                continue
            result.append(candidate)
        except FileNotFoundError:
            continue
    return tuple(result)


def download(name: str, directory: Path) -> Path:
    if name not in LIMITS:
        raise BootstrapError("Unsupported bootstrap artifact.")
    path = directory / name
    # Honor normal host CA configuration. No insecure TLS option or alternate
    # production endpoint can be supplied through an installer environment var.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 120
    with opener.open(DISTRIBUTION + name, timeout=30) as response, path.open("xb") as output:
        if not response.geturl().startswith("https://raw.githubusercontent.com/StreamHome/StreamHome/"):
            raise BootstrapError("The bootstrap artifact redirected outside the official source.")
        count = 0
        while block := response.read(65536):
            count += len(block)
            if count > LIMITS[name] or time.monotonic() > deadline:
                raise BootstrapError("The bootstrap artifact exceeds its download bound.")
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o600)
    return path


def run(arguments: list[str], *, privileged: bool = False, timeout: int = 60, capture: bool = True) -> bytes:
    if privileged:
        arguments = ["/usr/bin/sudo", "--", "/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                     "HOME=/root", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", *arguments]
    result = subprocess.run(arguments, cwd="/", env=ENVIRONMENT, timeout=timeout, stdin=None,
                            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        raise BootstrapError("A bootstrap verification or installation command failed; protected evidence was retained for explicit recovery.")
    if capture and (len(result.stdout) > 32768 or len(result.stderr) > 32768):
        raise BootstrapError("A bootstrap command exceeded its output bound.")
    return result.stdout if capture else b""


def verify_download(directory: Path) -> dict:
    if hashlib.sha256((directory / "publisher.pem").read_bytes()).hexdigest() != PUBLISHER_SHA256:
        raise BootstrapError("The publisher key does not match the installer trust pin.")
    run(["/usr/bin/openssl", "pkeyutl", "-verify", "-pubin", "-rawin", "-inkey", str(directory / "publisher.pem"),
         "-in", str(directory / "release.json"), "-sigfile", str(directory / "release.sig")])
    release = decode_release((directory / "release.json").read_bytes())
    if hashlib.sha256((directory / release["artifact"]).read_bytes()).hexdigest() != release["artifact_sha256"]:
        raise BootstrapError("The bootstrap package does not match the signed release.")
    return release


def install_verified_control(directory: Path, release: dict) -> None:
    # Each sudo child executes an OS-owned binary. The application tree is never
    # a privileged import path and cannot alter root-staged bytes after checking.
    stage = Path("/root") / (".streamhome-bootstrap." + secrets.token_hex(24))
    run(["/usr/bin/install", "-d", "-m", "700", str(stage)], privileged=True)
    for name in LIMITS:
        run(["/usr/bin/install", "-m", "600", str(directory / name), str(stage / name)], privileged=True)
    if run(["/usr/bin/sha256sum", str(stage / "publisher.pem")], privileged=True).split()[0].decode() != PUBLISHER_SHA256:
        raise BootstrapError("The copied root publisher key failed verification.")
    run(["/usr/bin/openssl", "pkeyutl", "-verify", "-pubin", "-rawin", "-inkey", str(stage / "publisher.pem"),
         "-in", str(stage / "release.json"), "-sigfile", str(stage / "release.sig")], privileged=True)
    copied = decode_release(run(["/usr/bin/cat", str(stage / "release.json")], privileged=True))
    if copied != release:
        raise BootstrapError("The copied root release changed after review.")
    if run(["/usr/bin/sha256sum", str(stage / copied["artifact"])], privileged=True).split()[0].decode() != copied["artifact_sha256"]:
        raise BootstrapError("The copied root package failed verification.")
    message("Installing the authenticated independent control entry ...")
    run(["/usr/bin/dpkg", "-i", str(stage / copied["artifact"])], privileged=True, timeout=120)
    message("Installing the authenticated independent control entry: done")
    if not protected_entry() or hashlib.sha256(ENTRY.read_bytes()).hexdigest() != copied["entry_sha256"]:
        raise BootstrapError("The installed protected entry does not match the verified package.")


def extracted_library(directory: Path, release: dict) -> Path:
    extracted = directory / "verified-control"
    run(["/usr/bin/dpkg-deb", "--extract", str(directory / release["artifact"]), str(extracted)])
    return extracted / "usr/local/libexec/streamhome/versions" / release["bundle_id"] / "lib"


def inspect_existing_legacy(paths: tuple[Path, ...]) -> None:
    with tempfile.TemporaryDirectory(prefix="streamhome-existing-inspection-") as temporary:
        directory = Path(temporary)
        for name in LIMITS:
            download(name, directory)
        library = extracted_library(directory, verify_download(directory))
        code = "import sys; sys.path.insert(0,sys.argv[1]); from lifecycle.control.legacy_inspection import main; raise SystemExit(main(sys.argv[2:]))"
        value = json.loads(run(["/usr/bin/python3", "-I", "-c", code, str(library), *(str(path) for path in paths)], timeout=180))
        if (not isinstance(value, dict) or set(value) != {"status", "reason"}
                or value["status"] not in {"up_to_date", "update_available", "different_unverified", "ahead_of_release", "diverged", "policy_rejected", "check_unavailable", "stale_retry", "absent", "unavailable", "ambiguous"}
                or not isinstance(value["reason"], str) or len(value["reason"]) > 2048):
            raise BootstrapError("The read-only release check returned invalid evidence.")
        message("Release check: " + value["status"] + ". " + value["reason"])


def review_installation(directory: Path, release: dict, request_path: Path | None) -> tuple[Path, str] | None:
    library = extracted_library(directory, release)
    # This is still the ordinary user. Only the typed review result crosses the
    # boundary; the extracted Python files are never used as privileged code.
    code = "import sys; sys.path.insert(0,sys.argv[1]); from lifecycle.control.bootstrap_review import main; raise SystemExit(main(sys.argv[2:]))"
    arguments = ["/usr/bin/python3", "-I", "-c", code, str(library)]
    if request_path is not None:
        arguments.append(str(request_path.absolute()))
    value = json.loads(run(arguments, timeout=7200))
    if value == {"cancelled": True}:
        message("Cancelled before privileged installation; no service account or application was created.")
        return None
    if (not isinstance(value, dict) or set(value) != {"cancelled", "application_commit", "request"}
            or value["cancelled"] is not False or not isinstance(value["application_commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["application_commit"]) or not isinstance(value["request"], dict)):
        raise BootstrapError("The installation review was incomplete. No privileged installation was started.")
    request = directory / "installation-request.json"
    with request.open("x") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value["request"], handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    return request, value["application_commit"]


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install StreamHome at its fixed managed paths through an authenticated control package.")
    parser.add_argument("--request", type=Path, help="Explicit complete noninteractive installation request JSON.")
    parsed = parser.parse_args(arguments)
    if os.geteuid() == 0:
        raise BootstrapError("Run the bootstrap as your normal user. It requests sudo only for verified administrative steps.")
    require_platform()
    # Installed evidence always wins over the fresh-install path. This dispatch
    # performs no artifact download or package-manager action for a retry.
    evidence = existing_evidence()
    if evidence:
        if not protected_entry():
            raise BootstrapError("Existing StreamHome evidence requires the independently verified recovery artifact. No installation files were changed.")
        message("Inspecting the existing StreamHome installation; default retry does not reinstall or update it.")
        command = ["/usr/bin/python3", "-I", str(ENTRY), "--install"]
        run(command, privileged=True, timeout=180, capture=False)
        return 0
    legacy = legacy_hints()
    if legacy:
        for path in legacy:
            message("Existing legacy installation or checkout hint: " + str(path))
        try:
            inspect_existing_legacy(legacy)
        except (BootstrapError, OSError, ValueError, subprocess.SubprocessError):
            message("The isolated release check is unavailable. No update eligibility was established.")
        message("No installation files were changed. Use the existing installation's supported Updates workflow for an eligible update.")
        return 0
    if protected_entry():
        command = ["/usr/bin/python3", "-I", str(ENTRY), "--install"]
        if parsed.request is not None:
            command += ["--request", str(parsed.request.absolute())]
        run(command, privileged=True, timeout=7200, capture=False)
        return 0
    with tempfile.TemporaryDirectory(prefix="streamhome-bootstrap-") as temporary:
        directory = Path(temporary)
        message("Downloading the authenticated control package ...")
        for name in LIMITS:
            download(name, directory)
        release = verify_download(directory)
        message("Downloading and verifying the control package: done")
        reviewed = review_installation(directory, release, parsed.request)
        if reviewed is None:
            return 0
        request, commit = reviewed
        # Another invocation may have completed bootstrap during the download.
        if not protected_entry():
            install_verified_control(directory, release)
        arguments = ["/usr/bin/python3", "-I", str(ENTRY), "--install", "--request", str(request), "--source-commit", commit]
        run(arguments, privileged=True, timeout=7200, capture=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BootstrapError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, BootstrapError):
            message(str(exc))
        else:
            message("Bootstrap could not complete. No mutable checkout script was elevated; inspect the retained installation intent before retrying.")
        raise SystemExit(2)
