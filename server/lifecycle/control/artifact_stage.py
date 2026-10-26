"""Inactive root-owned control preparation; never changes active selection.

Canonical permission, unit publication, cutover and acceptance are deliberately
outside this file. A damaged existing version is never repaired by preparation.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
import uuid

from . import artifacts, entry
from .root_files import root_directory
from .rpc import ControlError


CONTROL = Path('/usr/local/libexec/streamhome')


@dataclass(frozen=True)
class PreparedControl:
    bundle_id: str
    manifest_sha256: str
    artifact_sha256: str
    entry_sha256: str


def parse_repair_binding(value: dict) -> None:
    """Validate exact repair metadata only; this does not authenticate an artifact."""
    from . import resumption_contract as metadata
    artifact=value['control_artifact'];selection=value['control_selection']
    if not isinstance(artifact,dict) or set(artifact)!=set(PreparedControl.__dataclass_fields__):
        raise ControlError('control_reconstruction_identity_changed')
    metadata._uuid(artifact['bundle_id'])
    for name in ('manifest_sha256','artifact_sha256','entry_sha256'):metadata._digest(artifact[name])
    if (not isinstance(selection,dict) or type(selection.get('schema_version')) is not int
            or selection!={'schema_version':1,'bundle_id':artifact['bundle_id'],'manifest_sha256':artifact['manifest_sha256']}):
        raise ControlError('control_reconstruction_identity_changed')


def _sync(path: Path) -> None:
    descriptor = root_directory(path)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def _tree_matches(path: Path, expected: dict[str, bytes]) -> None:
    if entry._protected(path, directory=True).st_mode & 0o777 != 0o755:
        raise ControlError('control_prepared_version_changed')
    actual = set(); directories = set()
    expected_directories = {parent.as_posix() for name in expected
        for parent in PurePosixPath(name).parents if parent.as_posix() != '.'}
    for child in path.rglob('*'):
        relative = child.relative_to(path).as_posix()
        metadata = child.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_mode & 0o777 != 0o755:
                raise ControlError('control_prepared_version_changed')
            entry._protected(child,directory=True);directories.add(relative)
        else:
            if metadata.st_mode & 0o777 != 0o644:
                raise ControlError('control_prepared_version_changed')
            actual.add(relative)
            if relative not in expected or entry._read(child,artifacts.LIMIT) != expected[relative]:
                raise ControlError('control_prepared_version_changed')
    if actual != set(expected) or directories != expected_directories:
        raise ControlError('control_prepared_version_changed')


def _publish_inactive(path: Path, expected: dict[str, bytes]) -> None:
    """Called only under the root administrative lock; old versions stay intact."""
    if path.exists() or path.is_symlink():
        _tree_matches(path,expected)
        return
    staging = CONTROL/'staging'
    descriptor = root_directory(staging,create=True,mode=0o700);os.close(descriptor)
    temporary = staging/uuid.uuid4().hex
    temporary.mkdir(mode=0o700);temporary.chmod(0o700)
    # On error retain the private partial preparation for diagnosis. Never remove
    # another version or mistake a partial tree for a reusable verified one.
    for name,raw in sorted(expected.items()):
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or relative.as_posix() != name:
            raise ControlError('control_artifact_invalid')
        target = temporary/name
        parent = temporary
        for component in relative.parts[:-1]:
            parent /= component
            parent.mkdir(mode=0o755,exist_ok=True)
            parent.chmod(0o755)
        descriptor = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o644)
        with os.fdopen(descriptor,'wb') as output:
            os.fchmod(output.fileno(),0o644)
            output.write(raw);output.flush();os.fsync(output.fileno())
    for directory in sorted((p for p in temporary.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
        _sync(directory)
    temporary.chmod(0o755);_sync(temporary)
    parent = root_directory(path.parent,create=True);os.close(parent)
    if path.exists() or path.is_symlink():
        _tree_matches(path,expected)
        return  # Retain our unused private tree rather than deleting evidence.
    os.rename(temporary,path)
    _sync(staging);_sync(path.parent)
    _tree_matches(path,expected)


def cache_local(package: Path) -> PreparedControl:
    """Authenticate/cache an envelope without requiring or replacing normal code."""
    from .privileged import require_root
    from .provisioning import administrative_lock
    require_root()
    verified=artifacts.verify_local(package)
    with administrative_lock(reuse=True):
        descriptor=root_directory(CONTROL);os.close(descriptor)
        envelope={'streamhome-control-bootstrap.deb':verified.package,'release.json':verified.release,
            'release.sig':verified.signature,'publisher.pem':verified.publisher}
        _publish_inactive(CONTROL/'artifacts'/verified.artifact_sha256,envelope)
    return PreparedControl(verified.bundle_id,verified.manifest_sha256,verified.artifact_sha256,verified.entry_sha256)


def prepare_local(package: Path, *, previous_manifest: dict | None = None, previous_units: bytes | None = None) -> PreparedControl:
    """Verify and prepare complete inactive version/rescue copies as root.

    The signed envelope is retained independently for future exact reconstruction.
    No source, configuration, database, media, unit or selected/rescue pointer is
    read or changed here. Existing different/corrupt bytes cause a refusal. When
    preparing an upgrade, the caller supplies its authenticated loaded manifest;
    format compatibility is checked before any inactive files are published.
    """
    from .privileged import require_root
    from .provisioning import administrative_lock
    require_root()
    verified = artifacts.verify_local(package)
    if previous_manifest is not None:
        artifacts.require_upgrade_support(previous_manifest,verified,previous_units=previous_units)
    with administrative_lock(reuse=True):
        descriptor = root_directory(CONTROL);os.close(descriptor)
        envelope = {'streamhome-control-bootstrap.deb':verified.package,'release.json':verified.release,
                    'release.sig':verified.signature,'publisher.pem':verified.publisher}
        _publish_inactive(CONTROL/'artifacts'/verified.artifact_sha256,envelope)
        prefix = artifacts.CONTROL+'versions/'+verified.bundle_id+'/'
        files = {name[len(prefix):]:raw for name,raw in verified.files.items() if name.startswith(prefix)}
        for role in ('versions','rescue-versions'):
            _publish_inactive(CONTROL/role/verified.bundle_id,files)
    return PreparedControl(verified.bundle_id,verified.manifest_sha256,
        verified.artifact_sha256,verified.entry_sha256)


def require_cached(prepared: PreparedControl) -> artifacts.VerifiedArtifact:
    """Authenticate the retained envelope independently of damaged runtime copies."""
    from .privileged import require_root
    require_root()
    if type(prepared) is not PreparedControl:
        raise ControlError('control_artifact_invalid')
    for name in ('manifest_sha256','artifact_sha256','entry_sha256'):
        if not isinstance(getattr(prepared,name),str) or not artifacts._SHA.fullmatch(getattr(prepared,name)):
            raise ControlError('control_artifact_invalid')
    try:
        if str(uuid.UUID(prepared.bundle_id)) != prepared.bundle_id or not uuid.UUID(prepared.bundle_id).int:
            raise ValueError('invalid bundle')
    except (ValueError,TypeError,AttributeError) as exc:
        raise ControlError('control_artifact_invalid') from exc
    cache = CONTROL/'artifacts'/prepared.artifact_sha256
    for parent in (CONTROL,CONTROL/'artifacts',cache):entry._protected(parent,directory=True)
    envelope = {name:entry._read(cache/name,artifacts.LIMIT) for name in
        ('streamhome-control-bootstrap.deb','release.json','release.sig','publisher.pem')}
    verified = artifacts.verify_bytes(envelope['streamhome-control-bootstrap.deb'],envelope['release.json'],
        envelope['release.sig'],envelope['publisher.pem'])
    if prepared != PreparedControl(verified.bundle_id,verified.manifest_sha256,
            verified.artifact_sha256,verified.entry_sha256):
        raise ControlError('control_prepared_version_changed')
    _tree_matches(cache,envelope)
    return verified


def require_prepared(prepared: PreparedControl) -> artifacts.VerifiedArtifact:
    """Reverify root-owned cached bytes and both copies before later review."""
    verified=require_cached(prepared)
    prefix = artifacts.CONTROL+'versions/'+verified.bundle_id+'/'
    files = {name[len(prefix):]:raw for name,raw in verified.files.items() if name.startswith(prefix)}
    for role in ('versions','rescue-versions'):
        path = CONTROL/role/verified.bundle_id
        entry._protected(path.parent,directory=True);_tree_matches(path,files)
    return verified
