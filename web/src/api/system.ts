import { apiGet, type ApiRequestInit, apiPost } from "./client";
import type { SystemSettings } from "../types/api";

export const getSettings = (options?: ApiRequestInit) => apiGet<SystemSettings>("/api/system/settings", options);
export const updateSettings = (data: SystemSettings) => apiPost<SystemSettings>("/api/system/settings", data);
export const testGoogleDrive = () => apiPost<{ configured: true; reachable: true; remotePath: string }>("/api/system/drive/test");
