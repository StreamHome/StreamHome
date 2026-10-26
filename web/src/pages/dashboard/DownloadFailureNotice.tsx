import React from "react";
import type { DownloadEvent } from "../../types/api";
import "./download-failure.css";

const stageLabels: Record<string, string> = {
  source_preparation: "Source preparation", source_resource: "Source resource",
  probe: "Media inspection", download: "Media download", publication: "Media publication",
  storage: "Storage check", source_validation: "Source validation",
};
const titles: Record<string, string> = {
  SOURCE_FORBIDDEN: "Source access denied", SOURCE_UNAUTHORIZED: "Source authentication rejected",
  SOURCE_NOT_FOUND: "Media source not found", SOURCE_RATE_LIMITED: "Source request limit reached",
  SOURCE_UNAVAILABLE: "Source temporarily unavailable", SOURCE_UNREACHABLE: "Source could not be reached",
  UNSAFE_SOURCE: "Source rejected by network policy", STORAGE_UNAVAILABLE: "Storage unavailable",
  INSUFFICIENT_STORAGE: "Insufficient storage", INVALID_MEDIA_SOURCE: "Unsupported video source",
  INVALID_AUDIO_SOURCE: "Invalid audio source", MISSING_SOURCE: "Missing media source",
  FFMPEG_UNAVAILABLE: "Media tool unavailable", FFMPEG_OPTION_UNSUPPORTED: "Media tool incompatibility",
  INGESTION_INTERRUPTED: "Download interrupted", CLOUD_MOVE_FAILED: "Cloud verification failed",
};

export function DownloadFailureNotice({ download }: { download: DownloadEvent }) {
  const failure = download.failure;
  const title = failure?.providerCode === "1005" ? "Server network blocked (Cloudflare 1005)"
    : failure ? (titles[failure.code] ?? (failure.code.startsWith("storage_") ? "Storage capacity could not be confirmed" : "Download could not finish")) : "Download could not finish";
  return <div className="download-failure">
    <p><strong>{title}{failure?.httpStatus && !failure.providerCode ? ` (HTTP ${failure.httpStatus})` : ""}</strong></p>
    <p>{failure?.message ?? "The download could not be completed."}</p>
    <p>{failure?.guidance ?? "Ask the server operator to inspect this task using its reference."}</p>
    <details>
      <summary>Details</summary>
      <dl>
        <dt>Task reference</dt><dd>{download.id}</dd>
        {failure && <><dt>Error code</dt><dd>{failure.code}</dd></>}
        {failure?.stage && <><dt>Stage</dt><dd>{stageLabels[failure.stage]}</dd></>}
        {failure?.inputRole && <><dt>Source</dt><dd>{failure.inputRole}</dd></>}
        {failure?.httpStatus && <><dt>HTTP status</dt><dd>{failure.httpStatus}</dd></>}
        {failure?.providerCode && <><dt>Provider error</dt><dd>Cloudflare {failure.providerCode}</dd></>}
      </dl>
    </details>
  </div>;
}
