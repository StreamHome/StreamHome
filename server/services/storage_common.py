"""Shared, side-effect-free storage policy value contracts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

DISK_RESERVE_BYTES = 2 * 1024 ** 3
DRAFT_FIELDS = frozenset({'offload_enabled', 'local_budget_bytes', 'cloud_budget_bytes', 'prefetch_next_episode', 'hevc_compression_mode'})


class StorageError(ValueError):
    def __init__(self, code: str, message: str | None = None, status_code: int = 409):
        self.code, self.message, self.status_code = code, message or code.replace('_', ' ').capitalize(), status_code
        super().__init__(self.message)


def utc(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def request_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', value):
        raise StorageError('storage_invalid_request_id', status_code=422)
    return value


def canonical_draft(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != DRAFT_FIELDS:
        raise StorageError('storage_invalid_policy', status_code=422)
    result = dict(value)
    for field in ('offload_enabled', 'prefetch_next_episode'):
        if type(result[field]) is not bool:
            raise StorageError('storage_invalid_policy', status_code=422)
    for field in ('local_budget_bytes', 'cloud_budget_bytes'):
        amount = result[field]
        if amount is not None and (type(amount) is not int or amount <= 0 or amount > 2 ** 63 - 1):
            raise StorageError('storage_invalid_budget', status_code=422)
    if result['hevc_compression_mode'] not in {'auto', 'on', 'off'}:
        raise StorageError('storage_invalid_hevc_mode', status_code=422)
    if result['offload_enabled'] and (result['local_budget_bytes'] is None or result['cloud_budget_bytes'] is None):
        raise StorageError('storage_budgets_required', 'Choose both total budgets before enabling automatic offloading.', 422)
    return result


def policy_value(settings) -> dict:
    stored = getattr(settings, 'STORAGE_POLICY', None)
    if not isinstance(stored, dict):
        stored = {}
    return {
        'revision': int(stored.get('revision', 0)),
        'offload_enabled': stored.get('offload_enabled') is True,
        'local_budget_bytes': stored.get('local_budget_bytes'),
        'cloud_budget_bytes': stored.get('cloud_budget_bytes'),
        'disk_reserve_bytes': DISK_RESERVE_BYTES,
        'prefetch_next_episode': stored.get('prefetch_next_episode') is True,
        'hevc_compression_mode': settings.HEVC_COMPRESSION_MODE,
    }


def operation_value(row) -> dict:
    return {key: getattr(row, key) for key in (
        'id', 'kind', 'status', 'phase', 'media_id', 'title', 'bytes_total',
        'bytes_done', 'released_bytes', 'error_code', 'message'
    )} | {'created_at': utc(row.created_at), 'updated_at': utc(row.updated_at)}


CLEANUP_RETENTION_VERSION = 1


def validate_cleanup_retention(plan: dict) -> None:
    """Reject the whole obsolete/mixed plan before any file can be released."""
    if not isinstance(plan, dict) or type(plan.get('retention_version')) is not int or plan['retention_version'] != CLEANUP_RETENTION_VERSION:
        raise StorageError('storage_cleanup_plan_expired', 'Storage protection changed. Review cleanup again.')
    candidates = plan.get('candidates')
    if not isinstance(candidates, list) or len(candidates) > 25:
        raise StorageError('storage_cleanup_plan_expired')
    for item in candidates:
        if not isinstance(item, dict) or item.get('kind') != 'verified_duplicate':
            raise StorageError('storage_retained_data_protected', 'Caches and subtitles are retained and cannot be released by cleanup.')
        if (not isinstance(item.get('asset_id'), str) or not isinstance(item.get('generation'), str)
                or type(item.get('bytes')) is not int or item['bytes'] < 0):
            raise StorageError('storage_cleanup_plan_expired')
