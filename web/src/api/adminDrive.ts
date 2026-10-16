import { ApiError, apiDelete, apiGet, apiPost } from "./client";
import type { DriveFolderList, DriveSetupJob, DriveTestResult } from "./setup";

export interface AdminDriveJob extends Omit<DriveSetupJob, "status"> {
  status: DriveSetupJob["status"] | "saving" | "saved";
  pendingOperation: string | null;
  generation: number;
  pendingFolderPath?: string | null;
  libraryState?: "unknown" | "empty" | "nonempty" | "streamhome";
}
export interface AdminDriveSetup {
  configured: boolean;
  publicUrl: string;
  callbackUrl: string;
  guideUrl: string;
  job: AdminDriveJob | null;
}
export interface AdminDriveCredentials {
  client_id: string;
  client_secret: string;
  audience: "external" | "internal";
  publishing_status: "testing" | "production";
}
const root = "/api/system/drive";
const statuses = new Set(["authorizing", "exchanging_code", "selecting_folder", "testing", "ready", "failed", "cancelled", "expired", "saving", "saved"]);
const nonBlank = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));
function invalid(): never { throw new ApiError("The server did not confirm the Drive response. Check status before another change.", 0, "invalid_response"); }
export function isAdminDriveJob(value: unknown, id?: string): value is AdminDriveJob {
  return record(value) && nonBlank(value.id) && (id === undefined || value.id === id)
    && typeof value.status === "string" && statuses.has(value.status) && nonBlank(value.remoteName)
    && typeof value.selectedPath === "string" && typeof value.progress === "string"
    && ["external", "internal"].includes(value.audience as string)
    && ["testing", "production"].includes(value.publishingStatus as string)
    && typeof value.expiresAt === "number" && Number.isFinite(value.expiresAt)
    && (value.pendingFolderPath == null || typeof value.pendingFolderPath === "string")
    && (value.pendingOperation === null || nonBlank(value.pendingOperation))
    && (Number.isInteger(value.generation) && (value.generation as number) >= 0)
    && (value.errorCode == null || typeof value.errorCode === "string")
    && (value.libraryState === undefined || ["unknown", "empty", "nonempty", "streamhome"].includes(value.libraryState as string));
}
function httpUrl(value: unknown): value is string {
  if (!nonBlank(value)) return false;
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password; } catch { return false; }
}
export async function getAdminDriveSetup(signal?: AbortSignal): Promise<AdminDriveSetup> {
  const value = await apiGet<AdminDriveSetup>(`${root}/setup`, { signal });
  if (!record(value) || typeof value.configured !== "boolean" || typeof value.publicUrl !== "string"
      || (value.callbackUrl !== "" && !httpUrl(value.callbackUrl)) || !httpUrl(value.guideUrl)
      || (value.job !== null && !isAdminDriveJob(value.job))) invalid();
  return value;
}
export async function startAdminDrive(payload: AdminDriveCredentials) {
  const value = await apiPost<{ jobId: string; authorizationUrl: string; expiresAt: number }>(`${root}/jobs`, payload);
  let url: URL;
  try { url = new URL(value.authorizationUrl); } catch { return invalid(); }
  if (!nonBlank(value.jobId) || url.origin !== "https://accounts.google.com" || url.username || url.password
      || typeof value.expiresAt !== "number" || !Number.isFinite(value.expiresAt)) invalid();
  return value;
}
const jobPath = (id: string) => `${root}/jobs/${encodeURIComponent(id)}`;
export async function getAdminDriveJob(id: string, signal?: AbortSignal) {
  const value = await apiGet<AdminDriveJob>(jobPath(id), { signal });
  if (!isAdminDriveJob(value, id)) invalid();
  return value;
}
export async function cancelAdminDrive(id: string) {
  const value = await apiDelete<AdminDriveJob>(jobPath(id));
  if (!isAdminDriveJob(value, id) || value.status !== "cancelled" || value.pendingOperation !== null) invalid();
  return value;
}
export async function listAdminDriveFolders(id: string, path = "", signal?: AbortSignal): Promise<DriveFolderList> {
  const value = await apiGet<DriveFolderList>(`${jobPath(id)}/folders?path=${encodeURIComponent(path)}`, { signal });
  if (!value || value.path !== path || !Array.isArray(value.folders) || !value.folders.every(folder => folder && nonBlank(folder.name) && nonBlank(folder.path) && (folder.id == null || typeof folder.id === "string"))) invalid();
  return value;
}
export async function createAdminDriveFolder(id: string, path: string) {
  const value = await apiPost<{ path: string }>(`${jobPath(id)}/folders`, { path });
  if (!value || value.path !== path) invalid();
  return value;
}
export async function selectAdminDriveFolder(id: string, path: string) {
  const value = await apiPost<AdminDriveJob>(`${jobPath(id)}/select-folder`, { path });
  if (!isAdminDriveJob(value, id) || value.selectedPath !== path || value.status !== "selecting_folder" || value.pendingOperation !== null) invalid();
  return value;
}
async function checkResult(id: string, action: "test" | "save") {
  const value = await apiPost<Omit<DriveTestResult, "job"> & { job: AdminDriveJob }>(`${jobPath(id)}/${action}`);
  if (!value || value.valid !== true || !nonBlank(value.remotePath) || !isAdminDriveJob(value.job, id)
      || value.job.pendingOperation !== null || value.job.status !== (action === "save" ? "saved" : "ready")
      || (value.quota != null && (!record(value.quota) || [value.quota.total, value.quota.used, value.quota.free, value.quota.trashed].some(amount => amount != null && (typeof amount !== "number" || !Number.isFinite(amount)))))) invalid();
  return value;
}
export const testAdminDriveFolder = (id: string) => checkResult(id, "test");
export const saveAdminDrive = (id: string) => checkResult(id, "save");
