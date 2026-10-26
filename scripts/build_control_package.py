#!/usr/bin/env python3
"""Build/sign the minimal independent control bootstrap; never install or push.

The signing key is an explicit private input outside the artifact. Application
source, Git history, agent context, environments and runtime data are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import uuid


CONTROL = Path("usr/local/libexec/streamhome")
LAUNCHER = '#!/bin/sh\nexec /usr/bin/python3 -I /usr/local/libexec/streamhome/provision --cli "$@"\n'
PREINSTALL = '''#!/bin/sh
set -eu
/usr/bin/python3 -I - <<'PY'
from pathlib import Path
import stat
paths = [Path('/usr'), Path('/usr/local'), Path('/usr/local/bin'), Path('/usr/local/libexec'), Path('/usr/local/libexec/streamhome')]
control = paths[-1]
if control.exists() and not control.is_symlink():
    paths.extend(control.rglob('*'))
paths.append(Path('/usr/local/bin/streamhome'))
for path in paths:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        continue
    if metadata.st_uid != 0 or metadata.st_mode & 0o7022 or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
        raise SystemExit('StreamHome bootstrap refuses an unprotected or aliased installation path: ' + str(path))
PY
'''
POSTINSTALL = '''#!/bin/sh
set -eu
if [ "$1" != configure ]; then exit 0; fi
/usr/bin/python3 -I - <<'PY'
from pathlib import Path
import os
root = Path('/usr/local/libexec/streamhome')
raw = (root/'bootstrap-selection.json').read_bytes()
for name in ('selected.json', 'rescue.json'):
    path = root/name
    try:
        descriptor = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        continue
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
descriptor = os.open(root, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
'''


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write(path: Path, raw: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
    path.chmod(mode)


def build(source: Path, output: Path, signing_key: Path, bundle_id: str) -> dict:
    identity = uuid.UUID(bundle_id)
    if str(identity) != bundle_id or not identity.int:
        raise ValueError("A canonical nonzero control bundle ID is required.")
    metadata = signing_key.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o7177 or metadata.st_nlink != 1:
        raise ValueError("The signing key must be a private regular file.")
    if output.exists():
        raise ValueError("Choose a new artifact directory; existing release artifacts are never overwritten.")
    output.mkdir(mode=0o700, parents=True)
    with tempfile.TemporaryDirectory(prefix="streamhome-control-package-") as temporary:
        stage = Path(temporary)
        package = stage / "package"
        package.mkdir()
        version = "1.0.0+" + bundle_id.replace("-", "")
        write(package / "DEBIAN/control", (f"Package: streamhome-control-bootstrap\nVersion: {version}\nArchitecture: amd64\nMaintainer: StreamHome\nDepends: python3 (>= 3.12), openssl, ca-certificates\nDescription: Publisher-authenticated independent StreamHome control entry\n").encode())
        write(package / "DEBIAN/preinst", PREINSTALL.encode(), 0o755)
        write(package / "DEBIAN/postinst", POSTINSTALL.encode(), 0o755)
        versions = package / CONTROL / "versions" / bundle_id
        files = {}
        lifecycle = source / "server/lifecycle"
        for path in sorted(lifecycle.rglob("*.py")):
            if "__pycache__" in path.parts or path.is_symlink():
                continue
            relative = Path("lib/lifecycle") / path.relative_to(lifecycle)
            raw = path.read_bytes()
            write(versions / relative, raw)
            files[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
        manifest = {"schema_version": 1, "bundle_id": bundle_id, "entry_protocol": 1,
                    "authority_versions": [1, 2], "control_protocol": 1, "files": files,
                    "support": {"schema_version": 13, "platform": "ubuntu-24.04-x86_64-glibc-python-3.12.3-systemd-cgroup2",
                        "provisioning_journal_versions": [1, 2],
                        "management_journal_versions": [1, 2],
                        "recovery_request_versions": [1, 2, 3, 4, 5, 6], "resumption_plan_versions": [1, 2, 3],
                        "runtime_recovery_cache_versions": [1], "maintenance_actions": ["repair_bound_environment","repair_bound_control"],
                        "descriptor_handoff_versions": [1], "managed_restore_versions": [1],
                        "managed_parent_recovery_versions": [1, 2, 3, 4],
                        "managed_parent_environment_repair_versions": [1],
                        "managed_update_versions": [1], "managed_update_recovery_versions": [1, 2, 3, 4],
                        "managed_update_environment_repair_versions": [1],
                        "control_maintenance_versions": [1],
                        "bound_control_repair_versions": [1],
                        "cloudflare_connector_versions": [1],
                        "deadlines_seconds": {"startup_recovery": 900, "startup_extension": 30,
                            "runtime_drain": 45, "runtime_stop": 60, "controller": 3600, "installation": 7200}}}
        write(versions / "manifest.json", encoded(manifest))
        manifest_sha = digest(versions / "manifest.json")
        # The rescue reader is a physical copy. Damage to the selected version
        # cannot alter its modules through hardlinks, symlinks or a shared dir.
        rescue = package / CONTROL / "rescue-versions" / bundle_id
        shutil.copytree(versions, rescue, copy_function=shutil.copyfile)
        for path in rescue.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        selection = {"schema_version": 1, "bundle_id": bundle_id, "manifest_sha256": manifest_sha}
        write(package / CONTROL / "bootstrap-selection.json", encoded(selection))
        write(package / CONTROL / "provision", (lifecycle / "control/entry.py").read_bytes(), 0o755)
        write(package / "usr/local/bin/streamhome", LAUNCHER.encode(), 0o755)
        subprocess.run(["/usr/bin/openssl", "pkey", "-in", str(signing_key), "-pubout", "-out", str(output / "publisher.pem")], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        write(package / CONTROL / "publisher.pem", (output / "publisher.pem").read_bytes())
        artifact = output / "streamhome-control-bootstrap.deb"
        package.chmod(0o755)
        for path in package.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(stage), "TMPDIR": str(stage),
                       "LC_ALL": "C", "SOURCE_DATE_EPOCH": "0"}
        subprocess.run(["/usr/bin/dpkg-deb", "--build", "--root-owner-group", "--uniform-compression", "-Zgzip", str(package), str(artifact)], check=True, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        release = {"schema_version": 1, "format": "streamhome-control-bootstrap-deb-v1", "bundle_id": bundle_id,
                   "manifest_sha256": manifest_sha, "artifact": artifact.name, "artifact_sha256": digest(artifact),
                   "publisher_sha256": digest(output / "publisher.pem"), "entry_sha256": digest(package / CONTROL / "provision")}
        write(output / "release.json", encoded(release))
        subprocess.run(["/usr/bin/openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(signing_key), "-in", str(output / "release.json"), "-out", str(output / "release.sig")], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run(["/usr/bin/openssl", "pkeyutl", "-verify", "-pubin", "-rawin", "-inkey", str(output / "publisher.pem"), "-in", str(output / "release.json"), "-sigfile", str(output / "release.sig")], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--bundle-id", default=None)
    args = parser.parse_args()
    release = build(Path(__file__).resolve().parents[1], args.output.resolve(), args.signing_key.resolve(), args.bundle_id or str(uuid.uuid4()))
    print(json.dumps(release, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
