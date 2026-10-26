"""Prove installed control selection as data without importing selected code."""
from __future__ import annotations

import hashlib

from . import entry
from .rpc import ControlError


def require_selected(execution) -> dict:
    expected={'schema_version':1,'bundle_id':execution.control_bundle_id,
        'manifest_sha256':execution.control_manifest_sha256}
    try:
        value=entry._decode(entry._read(entry.CONTROL/'selected.json',4096))
        if value != expected or type(execution.control_protocol_version) is not int or execution.control_protocol_version != 1:
            raise ControlError('control_selected_reader_changed')
        _,manifest,digest=entry.verify_bundle(value)
        path=entry.CONTROL/'provision'
        if entry._protected(path).st_mode & 0o777 != 0o755:
            raise ControlError('control_selected_reader_changed')
        entry_digest=hashlib.sha256(entry._read(path)).hexdigest()
        if manifest['files'].get('lib/lifecycle/control/entry.py') != entry_digest:
            raise ControlError('control_selected_reader_changed')
        return {'bundle_id':execution.control_bundle_id,'manifest_sha256':digest,'entry_sha256':entry_digest}
    except (entry.EntryError,OSError,ValueError,TypeError,AttributeError) as exc:
        raise ControlError('control_selected_reader_changed') from exc
