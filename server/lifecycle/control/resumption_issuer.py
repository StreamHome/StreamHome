"""Live root issuer evidence subordinate to administrator and scope custody."""
from __future__ import annotations

import os
from pathlib import Path
import time

from .administration_lock import OWNER, parse_owner
from .privileged import require_root
from .resumption_contract import digest, parse_plan
from .rpc import ControlError
from .stopped_repair import current_scope, parse_scope, require_scope
from .tickets import read_protected_json, read_stop_epoch


def current_issuer(installation_id: str, stop_epoch: str) -> dict:
    require_root()
    scope = current_scope(installation_id, stop_epoch)
    owner = parse_owner(read_protected_json(OWNER, maximum=4096))
    if (owner['status'] != 'active' or owner['pid'] != os.getpid() or owner['pid'] != scope['pid']
            or owner['start_identity'] != scope['start_identity']
            or owner['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or read_stop_epoch() != stop_epoch):
        raise ControlError('control_resumption_issuer_changed')
    return {'owner': owner, 'scope': scope}


def require_issuer(evidence: dict, plan: dict) -> None:
    require_root()
    parse_plan(plan)
    if not isinstance(evidence, dict) or set(evidence) != {'owner', 'scope'}:
        raise ControlError('control_resumption_issuer_changed')
    owner = parse_owner(evidence['owner'])
    scope = parse_scope(evidence['scope'], plan['installation_id'])
    if (digest(evidence) != plan['issuer_sha256'] or owner['status'] != 'active'
            or owner['pid'] != scope['pid'] or owner['start_identity'] != scope['start_identity']
            or owner['boot_id'] != plan['boot_id']
            or owner != read_protected_json(OWNER, maximum=4096)
            or plan['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or read_stop_epoch() != plan['stop_epoch'] or time.monotonic() >= plan['expires_at_monotonic']):
        raise ControlError('control_resumption_issuer_changed')
    require_scope(scope, plan['installation_id'])
