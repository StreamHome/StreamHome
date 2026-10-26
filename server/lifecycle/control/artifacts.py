"""Verify publisher control packages as bounded data, without installing code.

Verification neither imports package modules nor executes Debian maintainer
scripts. Canonical MANAGEMENT permission and protected publication are separate
steps; a valid artifact alone cannot select a reader or start a service.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import gzip
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
from types import MappingProxyType
from typing import Mapping
import uuid

from .entry import EntryError, _decode, qualified_support
from .rpc import ControlError


PUBLISHER_SHA256 = '85ffa64c6c3ad266b69d915022b36b26145ae0b405954a6d84f88b99482fd12d'
LIMIT = 8 * 1024 * 1024
CONTROL = 'usr/local/libexec/streamhome/'
LAUNCHER = b'#!/bin/sh\nexec /usr/bin/python3 -I /usr/local/libexec/streamhome/provision --cli "$@"\n'
_SHA = re.compile(r'[0-9a-f]{64}')


@dataclass(frozen=True)
class VerifiedArtifact:
    bundle_id: str
    manifest_sha256: str
    artifact_sha256: str
    entry_sha256: str
    files: Mapping[str, bytes] = field(repr=False)
    package: bytes = field(repr=False)
    release: bytes = field(repr=False)
    signature: bytes = field(repr=False)
    publisher: bytes = field(repr=False)


def _read(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ControlError('control_artifact_invalid')
        raw = source.read(limit+1)
        after = os.fstat(source.fileno())
    if (len(raw) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ControlError('control_artifact_changed')
    return raw


def _json(raw: bytes) -> dict:
    try:
        return _decode(raw)
    except (EntryError, ValueError, UnicodeError, RecursionError) as exc:
        raise ControlError('control_artifact_invalid') from exc


def _verify_signature(key: bytes, release: bytes, signature: bytes) -> None:
    if hashlib.sha256(key).hexdigest() != PUBLISHER_SHA256 or len(signature) != 64:
        raise ControlError('control_artifact_untrusted')
    descriptors = []
    try:
        for raw in (key, release, signature):
            descriptor = os.memfd_create('streamhome-control-proof', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            descriptors.append(descriptor)
            with os.fdopen(os.dup(descriptor), 'wb') as output:
                output.write(raw)
            os.lseek(descriptor, 0, os.SEEK_SET)
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        paths = ['/proc/self/fd/'+str(descriptor) for descriptor in descriptors]
        result = subprocess.run(['/usr/bin/openssl', 'pkeyutl', '-verify', '-pubin', '-rawin',
            '-inkey', paths[0], '-in', paths[1], '-sigfile', paths[2]], pass_fds=tuple(descriptors),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LC_ALL': 'C'})
        if result.returncode:
            raise ControlError('control_artifact_untrusted')
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlError('control_artifact_untrusted') from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _archives(raw: bytes) -> dict[str, bytes]:
    if not raw.startswith(b'!<arch>\n'):
        raise ControlError('control_artifact_invalid')
    result = {}; offset = 8
    while offset < len(raw):
        header = raw[offset:offset+60]
        try:
            name = header[:16].decode('ascii').rstrip().removesuffix('/')
            size = int(header[48:58].decode('ascii').strip())
        except (ValueError, UnicodeError) as exc:
            raise ControlError('control_artifact_invalid') from exc
        if (len(header) != 60 or header[58:] != b'`\n' or name in result
                or name not in {'debian-binary', 'control.tar.gz', 'data.tar.gz'} or not 0 < size <= LIMIT):
            raise ControlError('control_artifact_invalid')
        offset += 60
        if offset+size > len(raw):
            raise ControlError('control_artifact_invalid')
        result[name] = raw[offset:offset+size];offset += size
        if size % 2:
            if raw[offset:offset+1] != b'\n':
                raise ControlError('control_artifact_invalid')
            offset += 1
    if set(result) != {'debian-binary', 'control.tar.gz', 'data.tar.gz'} or result['debian-binary'] != b'2.0\n':
        raise ControlError('control_artifact_invalid')
    return result


def _tar(raw: bytes, *, limit: int) -> dict[str, bytes]:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
            expanded = source.read(limit+1)
        if len(expanded) > limit:
            raise ControlError('control_artifact_invalid')
        result = {}; seen = set()
        with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
            for member in archive:
                name = member.name.removeprefix('./')
                relative = PurePosixPath(name)
                if (name in seen or len(seen) >= 2048 or relative.is_absolute() or '..' in relative.parts
                        or relative.as_posix() != name or member.uid != 0 or member.gid != 0
                        or member.mode & 0o7022 or not (member.isfile() or member.isdir())):
                    raise ControlError('control_artifact_invalid')
                seen.add(name)
                if member.isfile():
                    if not 0 <= member.size <= 4 * 1024 * 1024:
                        raise ControlError('control_artifact_invalid')
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ControlError('control_artifact_invalid')
                    with stream:result[name] = stream.read(member.size+1)
                    if len(result[name]) != member.size:
                        raise ControlError('control_artifact_invalid')
        return result
    except (OSError, ValueError, tarfile.TarError, EOFError) as exc:
        raise ControlError('control_artifact_invalid') from exc


def verify_bytes(package: bytes, release: bytes, signature: bytes, key: bytes) -> VerifiedArtifact:
    """Authenticate one complete immutable envelope; return only verified bytes."""
    if (not all(isinstance(value, bytes) for value in (package, release, signature, key))
            or not 0 < len(package) <= LIMIT or not 0 < len(release) <= 8192 or not 0 < len(key) <= 4096):
        raise ControlError('control_artifact_invalid')
    _verify_signature(key, release, signature)
    value = _json(release)
    fields = {'schema_version','format','bundle_id','manifest_sha256','artifact','artifact_sha256','publisher_sha256','entry_sha256'}
    try:
        identity = str(uuid.UUID(value['bundle_id']))
        valid = (set(value) == fields and type(value['schema_version']) is int and value['schema_version'] == 1
            and value['format'] == 'streamhome-control-bootstrap-deb-v1'
            and identity == value['bundle_id'] and uuid.UUID(identity).int
            and value['artifact'] == 'streamhome-control-bootstrap.deb'
            and all(isinstance(value[name], str) and _SHA.fullmatch(value[name])
                for name in ('manifest_sha256','artifact_sha256','publisher_sha256','entry_sha256'))
            and value['publisher_sha256'] == PUBLISHER_SHA256
            and hashlib.sha256(package).hexdigest() == value['artifact_sha256'])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ControlError('control_artifact_invalid') from exc
    if not valid:
        raise ControlError('control_artifact_invalid')
    archives = _archives(package)
    metadata = _tar(archives['control.tar.gz'], limit=512*1024)
    if set(metadata) != {'control', 'preinst', 'postinst'}:
        raise ControlError('control_artifact_invalid')
    files = _tar(archives['data.tar.gz'], limit=64*1024*1024)
    prefix = CONTROL+'versions/'+identity+'/'
    rescue = CONTROL+'rescue-versions/'+identity+'/'
    manifest_raw = files.get(prefix+'manifest.json', b'')
    if hashlib.sha256(manifest_raw).hexdigest() != value['manifest_sha256']:
        raise ControlError('control_artifact_invalid')
    manifest = _json(manifest_raw)
    try:
        if (set(manifest) != {'schema_version','bundle_id','entry_protocol','authority_versions','control_protocol','files','support'}
                or any(type(manifest[name]) is not int or manifest[name] != 1
                    for name in ('schema_version','entry_protocol','control_protocol'))
                or manifest['bundle_id'] != identity or manifest['authority_versions'] != [1,2]
                or any(type(number) is not int for number in manifest['authority_versions'])
                or not isinstance(manifest['files'], dict) or not 1 <= len(manifest['files']) <= 256):
            raise ControlError('control_artifact_incompatible')
        qualified_support(manifest['support'])
    except (EntryError, KeyError, TypeError) as exc:
        raise ControlError('control_artifact_incompatible') from exc
    expected = {CONTROL+'provision', CONTROL+'publisher.pem', CONTROL+'bootstrap-selection.json',
                'usr/local/bin/streamhome', prefix+'manifest.json', rescue+'manifest.json'}
    for name, digest in manifest['files'].items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or relative.as_posix() != name or '..' in relative.parts
                or relative.parts[:2] != ('lib','lifecycle') or relative.suffix != '.py'
                or any(parent.as_posix() in manifest['files'] for parent in relative.parents)
                or not isinstance(digest,str) or not _SHA.fullmatch(digest)):
            raise ControlError('control_artifact_invalid')
        for base in (prefix, rescue):
            expected.add(base+name)
            if hashlib.sha256(files.get(base+name,b'')).hexdigest() != digest:
                raise ControlError('control_artifact_invalid')
    required = {'lib/lifecycle/__init__.py','lib/lifecycle/state.py',
                'lib/lifecycle/control/__init__.py','lib/lifecycle/control/privileged.py',
                'lib/lifecycle/control/entry.py','lib/lifecycle/control/units.py'}
    selection = _json(files.get(CONTROL+'bootstrap-selection.json',b'{}'))
    if (not required <= set(manifest['files']) or set(files) != expected
            or files[rescue+'manifest.json'] != manifest_raw
            or files[CONTROL+'publisher.pem'] != key or files['usr/local/bin/streamhome'] != LAUNCHER
            or files[CONTROL+'provision'] != files.get(prefix+'lib/lifecycle/control/entry.py')
            or hashlib.sha256(files[CONTROL+'provision']).hexdigest() != value['entry_sha256']
            or type(selection.get('schema_version')) is not int
            or selection != {
                'schema_version':1,'bundle_id':identity,'manifest_sha256':value['manifest_sha256']}):
        raise ControlError('control_artifact_invalid')
    return VerifiedArtifact(identity,value['manifest_sha256'],value['artifact_sha256'],
        value['entry_sha256'],MappingProxyType(files),package,release,signature,key)


def verify_local(package: Path) -> VerifiedArtifact:
    """Read explicit local inputs without executing or installing their contents."""
    return verify_bytes(_read(package,LIMIT),_read(package.parent/'release.json',8192),
        _read(package.parent/'release.sig',128),_read(package.parent/'publisher.pem',4096))


def require_upgrade_support(previous_manifest: dict, candidate: VerifiedArtifact, *, previous_units: bytes | None = None) -> None:
    """Data-only protocol check; caller must authenticate the previous manifest.

    A newer schema number alone is never evidence of backward compatibility.
    Require the same entry/authority/control protocols and every previous declared
    readable format. This check grants no selection or canonical permission.
    """
    if type(candidate) is not VerifiedArtifact or not isinstance(previous_manifest,dict):
        raise ControlError('control_artifact_incompatible')
    proposed=_json(candidate.files[CONTROL+'versions/'+candidate.bundle_id+'/manifest.json'])
    try:
        for manifest in (previous_manifest,proposed):
            for name in ('schema_version','entry_protocol','control_protocol'):
                if type(manifest[name]) is not int or manifest[name] != 1:
                    raise ControlError('control_artifact_incompatible')
            if (manifest['authority_versions'] != [1,2]
                    or any(type(version) is not int for version in manifest['authority_versions'])):
                raise ControlError('control_artifact_incompatible')
            qualified_support(manifest['support'])
        before,after=previous_manifest['support'],proposed['support']
        if (before['schema_version'] > after['schema_version']
                or before['platform'] != after['platform']
                or any(after['deadlines_seconds'].get(name) != value
                    for name,value in before['deadlines_seconds'].items())
                or any(not isinstance(after.get(name),list) or not set(value) <= set(after[name])
                    for name,value in before.items() if isinstance(value,list))):
            raise ControlError('control_artifact_incompatible')
        previous_files=previous_manifest.get('files')
        units='lib/lifecycle/control/units.py'
        if (not isinstance(previous_files,dict) or not isinstance(previous_files.get(units),str)
                or not _SHA.fullmatch(previous_files[units])):
            raise ControlError('control_units_incompatible')
        if previous_files[units] != proposed['files'].get(units):
            if previous_units is None or hashlib.sha256(previous_units).hexdigest() != previous_files[units]:
                raise ControlError('control_units_incompatible')
            from .unit_payload import parse, compatible
            compatible(parse(previous_units), parse(candidate.files[CONTROL+'versions/'+candidate.bundle_id+'/'+units]))
    except (EntryError,KeyError,TypeError,ValueError) as exc:
        raise ControlError('control_artifact_incompatible') from exc
