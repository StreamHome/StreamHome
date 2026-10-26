"""Inspect a bound protected reader's declared formats without executing it."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import uuid

from .entry import CONTROL, EntryError, _decode, _protected, _read, qualified_support
from .rpc import ControlError
from . import entry


def require_previous_reader(execution) -> None:
    """Previous-runtime acceptance needs an explicitly compatible bound reader.

    Data-only compatibility inspection cannot select a control version or import
    its modules. The independently authenticated entry remains the only loader.
    """
    _require_reader(execution, resumption=False)


def require_resumption_reader(execution) -> None:
    """Refuse a bound reader that cannot validate replacement-supervisor grants."""
    _require_reader(execution, resumption=True)


def require_bound_repair_reader(execution) -> None:
    """The repaired parent must retain a reader that understands its new grant."""
    _require_reader(execution, resumption=False, bound_repair=True)


def require_managed_update_reader(execution) -> None:
    _require_reader(execution, resumption=False, managed_update=True)


def require_managed_restore_reader(execution) -> None:
    _require_reader(execution, resumption=False, managed_restore=True)


def require_parent_recovery_reader(execution) -> None:
    _require_reader(execution, resumption=False, parent_recovery=True)


def require_parent_resumption_reader(execution) -> None:
    _require_reader(execution, resumption=False, parent_resumption=True)


def require_parent_bound_repair_reader(execution) -> None:
    _require_reader(execution, resumption=False, parent_bound_repair=True)


def require_control_maintenance_reader(execution) -> None:
    try:
        if type(execution.control_protocol_version) is not int or execution.control_protocol_version != 1:
            raise ControlError('control_bound_reader_incompatible')
        _,manifest,_=entry.verify_bundle({'schema_version':1,'bundle_id':execution.control_bundle_id,
            'manifest_sha256':execution.control_manifest_sha256})
        versions=manifest['support'].get('control_maintenance_versions')
        if versions != [1] or any(type(value) is not int for value in versions):
            raise ControlError('control_bound_reader_incompatible')
    except (EntryError,OSError,ValueError,TypeError,AttributeError) as exc:
        raise ControlError('control_bound_reader_incompatible') from exc


def _require_reader(execution, *, resumption: bool, bound_repair: bool = False, managed_restore: bool = False,
                    parent_recovery: bool = False, parent_resumption: bool = False,
                    parent_bound_repair: bool = False, managed_update: bool = False) -> None:
    try:
        identity = uuid.UUID(execution.control_bundle_id)
        if (not identity.int or str(identity) != execution.control_bundle_id
                or not re.fullmatch(r'[0-9a-f]{64}', execution.control_manifest_sha256)):
            raise ControlError('control_bound_reader_incompatible')
        directory = CONTROL / 'versions' / execution.control_bundle_id
        current = Path('/')
        _protected(current, directory=True)
        for part in directory.parts[1:]:
            current /= part
            _protected(current, directory=True)
        raw = _read(directory / 'manifest.json')
        value = _decode(raw)
        support = value.get('support')
        qualified_support(support)
        if (hashlib.sha256(raw).hexdigest() != execution.control_manifest_sha256
                or value.get('bundle_id') != execution.control_bundle_id
                or support.get('schema_version') not in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13} or support.get('management_journal_versions') != [1, 2]
                or resumption and support.get('schema_version') not in {4, 5, 6, 7, 8, 9, 10, 11, 12, 13}
                or bound_repair and support.get('schema_version') not in {5, 6, 7, 8, 9, 10, 11, 12, 13}
                or managed_restore and support.get('schema_version') not in {6, 7, 8, 9, 10, 11, 12, 13}
                or parent_recovery and support.get('schema_version') not in {7,8,9,10,11,12,13}
                or parent_resumption and support.get('schema_version') not in {8,9,10,11,12,13}
                or managed_update and (support.get('schema_version') not in {10,11,12,13}
                    or support.get('managed_update_versions') != [1]
                    or support.get('managed_update_recovery_versions') != ([1,2,3,4] if support.get('schema_version')>=12 else [1,2,3])
                    or support.get('managed_update_environment_repair_versions') != [1])
                or parent_bound_repair and (support.get('schema_version') not in {9,10,11,12,13}
                    or support.get('managed_parent_environment_repair_versions') != [1])):
            raise ControlError('control_bound_reader_incompatible')
    except (EntryError, OSError, ValueError, TypeError) as exc:
        raise ControlError('control_bound_reader_incompatible') from exc
