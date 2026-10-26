import type { DownloadEvent, DownloadFailure } from "../types/api";

const codes = new Set([
  "SOURCE_FORBIDDEN", "SOURCE_UNAUTHORIZED", "SOURCE_NOT_FOUND", "SOURCE_RATE_LIMITED",
  "SOURCE_UNAVAILABLE", "SOURCE_UNREACHABLE", "UNSAFE_SOURCE", "STORAGE_UNAVAILABLE",
  "INSUFFICIENT_STORAGE", "INVALID_MEDIA_SOURCE", "INVALID_AUDIO_SOURCE", "MISSING_SOURCE",
  "FFMPEG_UNAVAILABLE", "FFMPEG_OPTION_UNSUPPORTED", "INGESTION_INTERRUPTED", "CLOUD_MOVE_FAILED",
  "MEDIA_PROBE_FAILED", "MEDIA_SOURCE_FAILED", "MEDIA_PROCESSING_FAILED", "FFMPEG_EXECUTION_FAILED",
  "QUEUE_INTERNAL_ERROR", "INGESTION_FAILED", "storage_filesystem_unavailable", "storage_unsafe_path",
  "storage_measurement_unknown", "storage_cloud_measurement_unknown", "storage_target_changed",
  "storage_account_capacity_unknown", "storage_local_budget_exceeded", "storage_cloud_budget_exceeded",
  "storage_disk_space_insufficient", "storage_account_quota_exceeded", "storage_reservation_missing",
  "storage_reservation_exceeded", "storage_invalid_reservation",
]);
const stages = new Set(["source_preparation", "source_resource", "probe", "download", "publication", "storage", "source_validation"]);
const roles = new Set(["video", "audio", "subtitle"]);
const publicText = (value: unknown): value is string => typeof value === "string" && value.length > 0
  && value.length <= 400 && !/[<>\x00-\x1f]|:\/\/|\b(?:authorization|cookie|token|password)\s*[:=]/i.test(value);

export function normalizeDownloadFailure(value: unknown): DownloadFailure | undefined {
  if (!value || typeof value !== "object") return undefined;
  const item = value as Record<string, unknown>;
  if (typeof item.code !== "string" || !codes.has(item.code) || !publicText(item.message) || !publicText(item.guidance)) return undefined;
  const result: DownloadFailure = { code: item.code, message: item.message, guidance: item.guidance };
  if (typeof item.stage === "string" && stages.has(item.stage)) result.stage = item.stage;
  if (typeof item.input_role === "string" && roles.has(item.input_role)) result.inputRole = item.input_role;
  if (typeof item.http_status === "number" && Number.isInteger(item.http_status) && item.http_status >= 100 && item.http_status <= 599) result.httpStatus = item.http_status;
  if (result.code === "SOURCE_FORBIDDEN" && result.httpStatus === 403 && item.provider === "cloudflare" && item.provider_code === "1005") {
    result.provider = "cloudflare";
    result.providerCode = "1005";
  }
  return result;
}

export function normalizeDownload(value: unknown): DownloadEvent | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const progress = Number(item.progress);
  if (item.id == null || typeof item.title !== "string" || !Number.isFinite(progress)) return null;
  const result: DownloadEvent = {
    id: String(item.id), title: item.title,
    status: typeof item.status === "string" ? item.status : "Pending",
    progress: Math.min(Math.max(progress, 0), 100),
    speed: typeof item.speed === "string" ? item.speed : "—",
    eta: typeof item.eta === "string" ? item.eta : "—",
  };
  if (result.status.toLowerCase() === "failed") {
    const failure = normalizeDownloadFailure(item.failure);
    if (failure) result.failure = failure;
  }
  return result;
}
