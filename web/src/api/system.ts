import { apiGet, apiPost } from "./client";
import type { SystemSettings } from "../types/api";

export const getSettings = () => apiGet<SystemSettings>("/api/system/settings");
export const updateSettings = (data: SystemSettings) => apiPost<SystemSettings>("/api/system/settings", data);
export const testGoogleDrive = () => apiPost<{ configured: true; reachable: true; remotePath: string }>("/api/system/drive/test");
