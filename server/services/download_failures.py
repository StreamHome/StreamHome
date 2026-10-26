"""Finite public explanations; private worker messages never cross this boundary."""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from services.ingestion_errors import IngestionFailure


FAILURES = {
    "SOURCE_FORBIDDEN": ("The source provider refused access from this server.", "Check source permissions or contact the source provider."),
    "SOURCE_UNAUTHORIZED": ("The source provider rejected authentication.", "Refresh source authorization in the sending app before submitting again."),
    "SOURCE_NOT_FOUND": ("The media source was not found.", "Obtain a current source in the sending app."),
    "SOURCE_RATE_LIMITED": ("The source provider is limiting requests.", "Wait before obtaining and submitting a current source."),
    "SOURCE_UNAVAILABLE": ("The source provider is temporarily unavailable.", "Check source availability before submitting again."),
    "SOURCE_UNREACHABLE": ("The media source could not be reached.", "Check the server's connection and source availability."),
    "UNSAFE_SOURCE": ("The media source was rejected by the network policy.", "Use an accessible HTTP or HTTPS media source permitted by the server."),
    "STORAGE_UNAVAILABLE": ("The server could not check its storage.", "Ask the server operator to check storage availability."),
    "INSUFFICIENT_STORAGE": ("The server has insufficient space for this download.", "Ask the server operator to check available storage and capacity reservations."),
    "INVALID_MEDIA_SOURCE": ("The source does not contain supported playable video.", "Select another video source in the sending app."),
    "INVALID_AUDIO_SOURCE": ("The separate audio source could not be used.", "Select a valid audio source in the sending app."),
    "MISSING_SOURCE": ("No media source was supplied.", "Select a media source in the sending app."),
    "FFMPEG_UNAVAILABLE": ("A required media tool is unavailable on the server.", "Ask the server operator to check the media tools."),
    "FFMPEG_OPTION_UNSUPPORTED": ("The server's media tool rejected a required option.", "Ask the server operator to check media-tool compatibility."),
    "INGESTION_INTERRUPTED": ("The download was interrupted by server shutdown.", "Check server availability before submitting again."),
    "CLOUD_MOVE_FAILED": ("The uploaded media could not be verified.", "Ask the server operator to inspect cloud storage and the task record."),
}
for _code in ("MEDIA_PROBE_FAILED", "MEDIA_SOURCE_FAILED", "MEDIA_PROCESSING_FAILED", "FFMPEG_EXECUTION_FAILED", "QUEUE_INTERNAL_ERROR", "INGESTION_FAILED"):
    FAILURES[_code] = ("The download could not be completed.", "Ask the server operator to inspect this task using its reference.")
for _code in ("storage_filesystem_unavailable", "storage_unsafe_path", "storage_measurement_unknown", "storage_cloud_measurement_unknown", "storage_target_changed", "storage_account_capacity_unknown", "storage_local_budget_exceeded", "storage_cloud_budget_exceeded", "storage_disk_space_insufficient", "storage_account_quota_exceeded", "storage_reservation_missing", "storage_reservation_exceeded", "storage_invalid_reservation"):
    FAILURES[_code] = ("The server could not confirm sufficient storage capacity.", "Ask the server operator to check storage measurements and capacity.")

STAGES = {"source_preparation", "source_resource", "probe", "download", "publication", "storage", "source_validation"}
ROLES = {"video", "audio", "subtitle"}
MAX_DETAILS = 2048


def safe_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, allowed in (("stage", STAGES), ("input_role", ROLES)):
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            result[key] = item
    status = value.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    if (result.get("http_status") == 403 and value.get("provider") == "cloudflare"
            and value.get("provider_code") == "1005"):
        result.update(provider="cloudflare", provider_code="1005")
    return result


def encode_failure(failure: IngestionFailure) -> str:
    context = safe_context({key: getattr(failure, key, None) for key in
                            ("stage", "input_role", "http_status", "provider", "provider_code")})
    code = failure.code if failure.code in FAILURES else "INGESTION_FAILED"
    return json.dumps({"version": 1, "code": code, **context}, separators=(",", ":"))


def public_failure(status: str, details: str | None, legacy_message: str | None) -> dict | None:
    if status != "FAILED":
        return None
    value = None
    if isinstance(details, str) and len(details) <= MAX_DETAILS:
        try:
            decoded = json.loads(details)
            if (isinstance(decoded, dict) and type(decoded.get("version")) is int
                    and decoded["version"] == 1):
                value = decoded
        except (ValueError, RecursionError):
            pass
    code = "INGESTION_FAILED"
    context = {}
    if value is not None:
        candidate = value.get("code")
        if isinstance(candidate, str) and candidate in FAILURES:
            code = candidate
            context = safe_context(value)
    elif isinstance(legacy_message, str):
        candidate, separator, _ = legacy_message[:MAX_DETAILS].partition(" — ")
        if separator and candidate in FAILURES:
            code = candidate
    message, guidance = FAILURES[code]
    if code == "SOURCE_FORBIDDEN" and context.get("provider_code") == "1005":
        message = "The source provider reports that this server's network is blocked (Cloudflare 1005)."
        guidance = "Contact the source provider or server operator to review the network block."
    else:
        context.pop("provider", None)
        context.pop("provider_code", None)
    return {"code": code, "message": message, "guidance": guidance, **context}
