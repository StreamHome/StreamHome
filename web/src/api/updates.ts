import { apiDelete, apiGet, apiPost, apiPut } from "./client";
import type { UpdatePolicy, UpdateStatus } from "../types/api";

export function normalizeUpdateStatus(status: UpdateStatus): UpdateStatus {
  const clock = status.serverClock;
  const validClock = clock && [clock.timeZone, clock.utcOffset, clock.localTime].every((value) => typeof value === "string" && value.trim().length > 0);
  return { ...status, serverClock: validClock ? clock : null };
}

export const getUpdateStatus = () => apiGet<UpdateStatus>("/api/update/status").then(normalizeUpdateStatus);
export const checkForUpdates = () => apiPost<UpdateStatus>("/api/update/check").then(normalizeUpdateStatus);
export const installUpdate = (mode: "when_idle" | "now", retryFailedTarget = false) =>
  apiPost<UpdateStatus>("/api/update/install", { retry_failed_target: retryFailedTarget, mode }).then(normalizeUpdateStatus);
export const cancelPendingUpdate = () => apiDelete<UpdateStatus>("/api/update/pending").then(normalizeUpdateStatus);
export const updateUpdatePolicy = (policy: UpdatePolicy) =>
  apiPut<UpdateStatus>("/api/update/policy", {
    automatic_updates: policy.automaticUpdates,
    idle_minutes: policy.idleMinutes,
    check_interval_hours: policy.checkIntervalHours,
    maintenance_start: policy.maintenanceStart,
    maintenance_end: policy.maintenanceEnd,
  }).then(normalizeUpdateStatus);

export function reportBrowserPresence(visible: boolean): Promise<void> {
  return fetch("/api/update/presence", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ visible }),
    keepalive: !visible,
  }).then((response) => {
    if (!response.ok && response.status !== 401) throw new Error("Presence update failed");
  });
}
