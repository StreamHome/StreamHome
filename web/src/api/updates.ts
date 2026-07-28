import { apiDelete, apiGet, apiPost, apiPut } from "./client";
import type { UpdatePolicy, UpdateStatus } from "../types/api";

export const getUpdateStatus = () => apiGet<UpdateStatus>("/api/update/status");
export const checkForUpdates = () => apiPost<UpdateStatus>("/api/update/check");
export const installUpdate = (mode: "when_idle" | "now", retryFailedTarget = false) =>
  apiPost<UpdateStatus>("/api/update/install", { retry_failed_target: retryFailedTarget, mode });
export const cancelPendingUpdate = () => apiDelete<UpdateStatus>("/api/update/pending");
export const updateUpdatePolicy = (policy: UpdatePolicy) =>
  apiPut<UpdateStatus>("/api/update/policy", {
    automatic_updates: policy.automaticUpdates,
    idle_minutes: policy.idleMinutes,
    check_interval_hours: policy.checkIntervalHours,
    maintenance_start: policy.maintenanceStart,
    maintenance_end: policy.maintenanceEnd,
  });

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
