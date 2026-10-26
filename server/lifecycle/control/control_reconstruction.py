"""Exact protected control-copy reconstruction; canonical permission is separate.

The caller must keep the original UID controller's maintenance fence and a live
root issuer. These effects never select a version, touch application data, accept
an operation or start a unit. The root repair bridge invokes them only after
independent UID observation; capability advertisement remains closed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from . import artifacts, artifact_stage, entry
from .rpc import ControlError


def _metadata(path: Path):
    return path.lstat()


def fingerprint(path: Path) -> str:
    """Bounded no-follow root metadata; preserve damaged bytes without reading them."""
    rows=[];pending=[(path,'.')]
    while pending:
        current,relative=pending.pop()
        try:metadata=_metadata(current)
        except FileNotFoundError:
            rows.append([relative,'missing']);continue
        if (len(rows)>=2048 or len(relative)>4096 or metadata.st_uid!=0
                or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode))
                or stat.S_ISDIR(metadata.st_mode) and metadata.st_mode & 0o7022):
            raise ControlError('control_reconstruction_untrusted')
        rows.append([relative,metadata.st_dev,metadata.st_ino,metadata.st_mode,metadata.st_nlink,
            metadata.st_size,metadata.st_mtime_ns,metadata.st_ctime_ns,
            hashlib.sha256(os.fsencode(os.readlink(current))).hexdigest() if stat.S_ISLNK(metadata.st_mode) else None])
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(current) as children:
                names=sorted((child.name for child in children),reverse=True)
            pending.extend((current/name,name if relative=='.' else relative+'/'+name) for name in names)
    return hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()


def exact_artifact(package: Path, selection: dict) -> artifacts.VerifiedArtifact:
    """Require the same independently protected rescue and publisher identity."""
    verified=artifacts.verify_local(package)
    if selection!={'schema_version':1,'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256}:
        raise ControlError('control_reconstruction_identity_changed')
    # This never imports damaged normal code or trusts package identity alone.
    rescue,manifest,_=entry.verify_bundle(selection,rescue=True)
    prefix=artifacts.CONTROL+'versions/'+verified.bundle_id+'/'
    if entry._read(rescue/'manifest.json')!=verified.files[prefix+'manifest.json']:
        raise ControlError('control_reconstruction_identity_changed')
    return verified


def reconstruct(package: Path, selection: dict, *, expected_artifact_sha256: str, expected_fingerprint: str,
                checkpoint, quiescent, record) -> dict:
    """One exact normal-tree effect under externally retained canonical custody.

    ``record`` durably publishes the generation-bound root receipt selected by
    the caller, before rename and after verification. Failure never deletes the
    reviewed tree or retries an earlier rename without a new observation.
    """
    from .privileged import require_root
    from .provisioning import administrative_lock
    require_root()
    with administrative_lock(reuse=True):
        verified=exact_artifact(package,selection)
        if verified.artifact_sha256!=expected_artifact_sha256:
            raise ControlError('control_reconstruction_identity_changed')
        target=entry.CONTROL/'versions'/verified.bundle_id
        parent=artifact_stage.root_directory(target.parent)
        try:
            quiescent();checkpoint()
            if fingerprint(target)!=expected_fingerprint:
                raise ControlError('control_reconstruction_review_changed')
            attempt=str(uuid.uuid4())
            quarantine=target.with_name('.repair-'+target.name+'-'+attempt)
            present=target.exists() or target.is_symlink()
            value={'schema_version':1,'attempt_id':attempt,'selection':selection,
                'artifact_sha256':verified.artifact_sha256,'filesystem_sha256':expected_fingerprint,
                'quarantine':quarantine.name if present else None,'stage':'rename_planned'}
            record(value)
            quiescent();checkpoint()
            if fingerprint(target)!=expected_fingerprint or quarantine.exists() or quarantine.is_symlink():
                raise ControlError('control_reconstruction_review_changed')
            if present:
                os.rename(target.name,quarantine.name,src_dir_fd=parent,dst_dir_fd=parent);os.fsync(parent)
            record({**value,'stage':'reconstructing'})
            quiescent();checkpoint()
            prefix=artifacts.CONTROL+'versions/'+verified.bundle_id+'/'
            files={name[len(prefix):]:raw for name,raw in verified.files.items() if name.startswith(prefix)}
            artifact_stage._publish_inactive(target,files)
            quiescent();checkpoint()
            entry.verify_bundle(selection)
            record({**value,'stage':'complete'})
            return {**value,'stage':'complete'}
        finally:os.close(parent)
