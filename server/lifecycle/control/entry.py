#!/usr/bin/python3 -I
"""Installed publisher-authenticated entry/rescue loader (standard library only).

The installer distributes this file in the signed bootstrap artifact. Never run
the mutable checkout copy as root. Its installed parents and every loaded module
must be root-owned and not writable by the service account or other users.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time


CONTROL = Path("/usr/local/libexec/streamhome")
OPERATIONS = Path('/etc/streamhome/control/operations')


class EntryError(RuntimeError):
    pass


def qualified_support(support: dict) -> tuple[int, ...]:
    deadlines = {"startup_recovery": 900, "startup_extension": 30, "runtime_drain": 45, "runtime_stop": 60, "controller": 3600}
    if not isinstance(support, dict) or type(support.get("schema_version")) is not int or support["schema_version"] not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}:
        raise EntryError("The installed control support contract is incompatible.")
    fields = {"schema_version", "platform", "deadlines_seconds"}
    if support["schema_version"] >= 2:
        fields.add("provisioning_journal_versions")
        deadlines["installation"] = 7200
    if support['schema_version'] >= 3:
        fields.add('management_journal_versions')
    if support['schema_version'] >= 4:
        fields.update({'recovery_request_versions', 'resumption_plan_versions'})
    if support['schema_version'] >= 5:
        fields.update({'runtime_recovery_cache_versions', 'maintenance_actions'})
    if support['schema_version'] >= 6:
        fields.update({'descriptor_handoff_versions', 'managed_restore_versions'})
    if support['schema_version'] >= 7:
        fields.add('managed_parent_recovery_versions')
    if support['schema_version'] >= 9:
        fields.add('managed_parent_environment_repair_versions')
    if support['schema_version'] >= 10:
        fields.update({'managed_update_versions', 'managed_update_recovery_versions', 'managed_update_environment_repair_versions'})
    if support['schema_version'] >= 11:
        fields.add('control_maintenance_versions')
    if support['schema_version'] >= 12:
        fields.add('bound_control_repair_versions')
    if support['schema_version'] >= 13:
        fields.add('cloudflare_connector_versions')
    versions = [1] if support["schema_version"] == 1 else support.get("provisioning_journal_versions")
    if (set(support) != fields or support["platform"] != "ubuntu-24.04-x86_64-glibc-python-3.12.3-systemd-cgroup2"
            or support["deadlines_seconds"] != deadlines
            or any(type(value) is not int for value in support["deadlines_seconds"].values())
            or versions != ([1] if support["schema_version"] == 1 else [1, 2])
            or any(type(value) is not int for value in versions)):
        raise EntryError("The installed control support contract is incompatible.")
    if support['schema_version'] >= 3 and (support.get('management_journal_versions') != [1, 2]
            or any(type(value) is not int for value in support['management_journal_versions'])):
        raise EntryError('The installed management journal support contract is incompatible.')
    if support['schema_version'] >= 4:
        for name, expected in (('recovery_request_versions', [1,2,3,4,5,6] if support['schema_version']>=12 else [1, 2, 3, 4] if support['schema_version'] >= 5 else [1, 2, 3]),
                               ('resumption_plan_versions', [1,2,3] if support['schema_version'] >= 11 else [1, 2] if support['schema_version'] >= 8 else [1])):
            if support.get(name) != expected or any(type(value) is not int for value in support[name]):
                raise EntryError('The installed resumption support contract is incompatible.')
    if support['schema_version'] >= 5 and (support['runtime_recovery_cache_versions'] != [1]
            or any(type(value) is not int for value in support['runtime_recovery_cache_versions'])
            or support['maintenance_actions'] != (['repair_bound_environment','repair_bound_control'] if support['schema_version']>=12 else ['repair_bound_environment'])):
        raise EntryError('The installed bound repair support contract is incompatible.')
    if support['schema_version'] >= 6:
        for name in ('descriptor_handoff_versions', 'managed_restore_versions'):
            if support[name] != [1] or any(type(value) is not int for value in support[name]):
                raise EntryError('The installed managed restore support contract is incompatible.')
    if support['schema_version'] >= 7 and (support['managed_parent_recovery_versions'] != ([1,2,3,4] if support['schema_version']>=12 else [1,2,3] if support['schema_version'] >= 9 else [1,2] if support['schema_version'] >= 8 else [1])
            or any(type(value) is not int for value in support['managed_parent_recovery_versions'])):
        raise EntryError('The installed parent recovery support contract is incompatible.')
    if support['schema_version'] >= 9 and (support['managed_parent_environment_repair_versions'] != [1]
            or any(type(value) is not int for value in support['managed_parent_environment_repair_versions'])):
        raise EntryError('The installed parent environment repair support contract is incompatible.')
    if support['schema_version'] >= 10:
        for name, expected in (('managed_update_versions', [1]), ('managed_update_recovery_versions', [1,2,3,4] if support['schema_version']>=12 else [1,2,3]),
                ('managed_update_environment_repair_versions', [1])):
            if support[name] != expected or any(type(value) is not int for value in support[name]):
                raise EntryError('The installed managed update support contract is incompatible.')
    if support['schema_version'] >= 11 and (support['control_maintenance_versions'] != [1]
            or any(type(value) is not int for value in support['control_maintenance_versions'])):
        raise EntryError('The installed control maintenance support contract is incompatible.')
    if support['schema_version']>=12 and (support['bound_control_repair_versions']!=[1]
            or any(type(value) is not int for value in support['bound_control_repair_versions'])):
        raise EntryError('The installed exact control repair support contract is incompatible.')
    if support['schema_version']>=13 and (support['cloudflare_connector_versions']!=[1]
            or any(type(value) is not int for value in support['cloudflare_connector_versions'])):
        raise EntryError('The installed Cloudflare connector support contract is incompatible.')
    return tuple(versions)


def _protected(path: Path, *, directory: bool = False):
    metadata = path.lstat()
    if (metadata.st_uid != 0 or metadata.st_mode & 0o7022
            or (not stat.S_ISDIR(metadata.st_mode) if directory else not stat.S_ISREG(metadata.st_mode))
            or not directory and metadata.st_nlink != 1):
        raise EntryError("The installed control entry is not protected. Use the independently verified recovery artifact.")
    return metadata


def _read(path: Path, limit: int = 4 * 1024 * 1024) -> bytes:
    before = _protected(path)
    if before.st_size > limit:
        raise EntryError("The installed control evidence exceeds its bound.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
            raise EntryError("The installed control evidence changed.")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise EntryError("The installed control evidence exceeds its bound.")
    return raw


def _decode(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EntryError("The installed control evidence is ambiguous.")
            result[key] = value
        return result
    value = json.loads(raw.decode(), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise EntryError("The installed control evidence is invalid.")
    return value


def operation_selection(operation_id: str) -> dict | None:
    """Read only a root-published reader pin, never canonical service data.

    Existing operations without a pin retain the normal selection. An existing
    but damaged pin cannot fall back to a different reader during cutover.
    """
    if (not isinstance(operation_id,str)
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',operation_id)
            or operation_id == '00000000-0000-0000-0000-000000000000'):
        raise EntryError('The operation reader identity is invalid.')
    path = OPERATIONS/operation_id/'reader.json'
    parent=Path('/')
    _protected(parent,directory=True)
    for part in path.parent.parts[1:]:
        parent/=part
        try:_protected(parent,directory=True)
        except FileNotFoundError:return None
    # A dangling alias is existing untrusted evidence, not an absent pin.
    try:path.lstat()
    except FileNotFoundError:return None
    value=_decode(_read(path,4096))
    if (set(value) != {'schema_version','operation_id','bundle_id','manifest_sha256'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['operation_id'] != operation_id):
        raise EntryError('The operation reader pin is invalid.')
    return {name:value[name] for name in ('schema_version','bundle_id','manifest_sha256')}


def load_bundle(*, rescue: bool = False, operation_id: str | None = None,
                require_operation_pin: bool = False, operation_rescue: bool = False,
                bound_control_repair: bool = False) -> tuple[Path, dict, str]:
    if type(operation_rescue) is not bool or type(bound_control_repair) is not bool:
        raise EntryError('The operation rescue role is invalid.')
    if Path(__file__) != CONTROL / "provision":
        raise EntryError("Use the installed absolute control entry. Checkout scripts cannot run with elevated privileges.")
    current = Path("/")
    _protected(current, directory=True)
    for component in CONTROL.parts[1:]:
        current /= component
        _protected(current, directory=True)
    _protected(CONTROL / "provision")
    selection = operation_selection(operation_id) if operation_id is not None else None
    if require_operation_pin and selection is None:
        raise EntryError('The required operation reader pin is unavailable.')
    if operation_rescue and (not rescue or selection is None or not require_operation_pin):
        raise EntryError('Exact operation rescue requires its protected reader pin.')
    if bound_control_repair and (not rescue or operation_rescue):
        raise EntryError('The exact control repair reader role is invalid.')
    if rescue and selection is not None and not (operation_rescue or bound_control_repair):
        raise EntryError('A pinned operation must use its independently verified normal reader.')
    if selection is None:
        selection = _decode(_read(CONTROL / ("rescue.json" if rescue and not bound_control_repair else "selected.json"), 4096))
    bundle,manifest,digest=verify_bundle(selection,rescue=rescue)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(bundle / "lib"))
    return bundle,manifest,digest


def control_repair_role(operation_id: str) -> bool:
    """Select an exact physical rescue only from root-published repair metadata.

    This grants no canonical action. The UID controller separately validates
    the retained grant, scope, current original owner and maintenance evidence.
    """
    pin=operation_selection(operation_id)
    path=OPERATIONS/operation_id/'control-repair-reader.json'
    try:path.lstat()
    except FileNotFoundError:return False
    value=_decode(_read(path,4096))
    if (not isinstance(value,dict) or set(value)!={'schema_version','operation_id','selection','generation','grant_sha256','stage'}
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['operation_id']!=operation_id or pin is None or not isinstance(value['selection'],dict)
            or type(value['selection'].get('schema_version')) is not int or value['selection']!=pin
            or type(value['generation']) is not int or not 2<=value['generation']<2**63
            or not isinstance(value['grant_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['grant_sha256'])
            or not isinstance(value['stage'],str) or value['stage'] not in {'active','complete'}):
        raise EntryError('The exact control repair reader record is invalid.')
    if value['stage']=='complete':return False
    grant=_decode(_read(OPERATIONS/operation_id/('recovery-'+str(value['generation'])+'.json'),32768))
    digest=hashlib.sha256(json.dumps(grant,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    request=grant.get('request');expiry=grant.get('expires_at_monotonic')
    if (digest!=value['grant_sha256'] or type(grant.get('schema_version')) is not int or grant['schema_version'] not in {5,6}
            or grant.get('action')!='repair_bound_control' or not isinstance(request,dict)
            or request.get('operation_id')!=operation_id or type(request.get('expected_generation')) is not int
            or request['expected_generation']+1!=value['generation']
            or request.get('controller_control_manifest_sha256')!=pin['manifest_sha256']
            or request.get('boot_id')!=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or type(expiry) not in {int,float} or not math.isfinite(expiry) or not time.monotonic()<expiry<=time.monotonic()+900):
        raise EntryError('The exact control repair reader permission changed or expired.')
    return True


def verify_bundle(selection: dict, *, rescue: bool = False) -> tuple[Path, dict, str]:
    """Verify a protected installed tree as data, without imports or path changes."""
    current=Path('/')
    _protected(current,directory=True)
    for component in CONTROL.parts[1:]:
        current/=component;_protected(current,directory=True)
    if not isinstance(selection,dict) or set(selection) != {"schema_version", "bundle_id", "manifest_sha256"} or type(selection["schema_version"]) is not int or selection["schema_version"] != 1:
        raise EntryError("The installed control selection is invalid.")
    identity, digest = selection["bundle_id"], selection["manifest_sha256"]
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity) or identity == "00000000-0000-0000-0000-000000000000" or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise EntryError("The installed control identity is invalid.")
    versions = CONTROL / ("rescue-versions" if rescue else "versions")
    _protected(versions, directory=True)
    bundle = versions / identity
    _protected(bundle, directory=True)
    raw = _read(bundle / "manifest.json")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise EntryError("The installed control manifest changed. Use the independently verified rescue entry.")
    manifest = _decode(raw)
    if (set(manifest) != {"schema_version", "bundle_id", "entry_protocol", "authority_versions", "control_protocol", "files", "support"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["bundle_id"] != identity
            or any(type(manifest[name]) is not int or manifest[name] != 1 for name in ('entry_protocol','control_protocol'))
            or manifest["authority_versions"] != [1, 2]
            or any(type(version) is not int for version in manifest['authority_versions'])
            or not isinstance(manifest["files"], dict) or not 1 <= len(manifest["files"]) <= 256):
        raise EntryError("The installed control package is incompatible.")
    qualified_support(manifest["support"])
    for name, expected in manifest["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 3 or relative.parts[:2] != ("lib", "lifecycle") or relative.suffix != ".py" or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise EntryError("The installed control manifest contains an unsafe module.")
        current = bundle
        for component in relative.parts[:-1]:
            current /= component
            _protected(current, directory=True)
        if hashlib.sha256(_read(bundle / relative, 2 * 1024 * 1024)).hexdigest() != expected:
            raise EntryError("An installed control module changed. Use the independently verified rescue entry.")
    # Disallow unmanifested importable code, including namespace packages,
    # extension modules, sourceless bytecode and interpreter startup hooks.
    actual = set()
    for path in (bundle / "lib").rglob("*"):
        if path.is_dir():
            _protected(path, directory=True)
        elif path.suffix == ".py":
            actual.add(path.relative_to(bundle).as_posix())
        else:
            raise EntryError("The installed control package contains an unmanifested file.")
    if actual != set(manifest["files"]):
        raise EntryError("The installed control module inventory changed.")
    return bundle, manifest, digest


def main() -> int:
    arguments = sys.argv[1:]
    rescue = bool(arguments and arguments[0] == "--rescue")
    if rescue:
        arguments = arguments[1:]
    if arguments[:2]==['--cli','repair'] and any(item=='--control-artifact' or item.startswith('--control-artifact=') for item in arguments[2:]):
        # Choose the independent physical reader before importing CLI code.
        # The protected dispatcher performs root and full argument validation.
        normalized=[]
        for item in arguments[2:]:
            key,separator,value=item.partition('=')
            normalized.extend([key,value] if separator and key in {'--operation','--control-artifact','--request'} else [item])
        arguments=['--repair-control',*['--artifact' if item=='--control-artifact' else item for item in normalized]]
    operation_id = arguments[1] if len(arguments)==2 and arguments[0]=='--operation' else None
    bound_review=bool(len(arguments)==2 and arguments[0]=='--control-repair-review-worker')
    bound_repair=bool(bound_review or arguments and arguments[0] in {'--repair-control','--repair-control-scoped'})
    if bound_repair:
        if rescue:raise EntryError('Exact control repair cannot borrow the global rescue selection.')
        if bound_review:
            operation_id=arguments[1]
            arguments=['--worker','bound-control-review',operation_id]
        elif arguments[0]=='--repair-control-scoped':
            if len(arguments)<4:raise EntryError('The exact control repair operation is missing.')
            operation_id=arguments[2]
        else:
            if arguments.count('--operation')!=1 or arguments.index('--operation')+1>=len(arguments):
                raise EntryError('The exact control repair operation is missing.')
            operation_id=arguments[arguments.index('--operation')+1]
        rescue=True
    pinned_worker = len(arguments)>=3 and arguments[0]=='--operation-worker'
    operation_rescue = len(arguments)>=3 and arguments[0]=='--operation-rescue-worker'
    if operation_rescue:
        if rescue or arguments[2] not in {'bound-control-review','bound-control-prepare','bound-control-observe','bound-control-result'}:
            raise EntryError('The exact operation rescue worker action is invalid.')
        rescue=True
        pinned_worker=True
    if pinned_worker:
        operation_id=arguments[1]
        arguments=['--worker',*arguments[2:]]
    actor_repair=bool(operation_id is not None and not pinned_worker and not bound_repair and control_repair_role(operation_id))
    if actor_repair:
        if rescue:raise EntryError('The exact control repair actor cannot borrow a global rescue reader.')
        rescue=True
    bundle, manifest, digest = load_bundle(rescue=rescue,operation_id=operation_id,
        require_operation_pin=pinned_worker or actor_repair,operation_rescue=operation_rescue,
        bound_control_repair=bound_repair or actor_repair)
    from lifecycle.control.privileged import dispatch
    return dispatch(arguments, bundle=bundle, manifest=manifest, manifest_sha256=digest, rescue=rescue)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EntryError, OSError, ValueError) as exc:
        print("StreamHome control: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
