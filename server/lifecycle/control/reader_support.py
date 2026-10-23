"""Inspect a bound protected reader's declared formats without executing it."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import uuid

from .entry import CONTROL, EntryError, _decode, _protected, _read, qualified_support
from .rpc import ControlError


def require_previous_reader(execution) -> None:
    """Previous-runtime acceptance needs an explicitly compatible bound reader.

    Data-only compatibility inspection cannot select a control version or import
    its modules. The independently authenticated entry remains the only loader.
    """
    _require_reader(execution, resumption=False)


def require_resumption_reader(execution) -> None:
    """Refuse a bound reader that cannot validate replacement-supervisor grants."""
    _require_reader(execution, resumption=True)


def _require_reader(execution, *, resumption: bool) -> None:
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
                or support.get('schema_version') not in {3, 4} or support.get('management_journal_versions') != [1, 2]
                or resumption and support.get('schema_version') != 4):
            raise ControlError('control_bound_reader_incompatible')
    except (EntryError, OSError, ValueError, TypeError) as exc:
        raise ControlError('control_bound_reader_incompatible') from exc
