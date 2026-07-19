import { apiDelete, apiGet, apiPost } from "./client";

export interface SetupStatus {
  required: boolean;
  unlocked: boolean;
  webPort: number;
  serverVersion: string;
  mediaPath: string;
  databasePath: string;
  publicUrl?: string;
  driveCallbackUrl?: string;
  driveGuideUrl?: string;
}

export interface ReadinessCheck { id: string; ready: boolean; detail: string }
export interface SetupReadiness { ready: boolean; checks: ReadinessCheck[] }

export interface SetupCompleteRequest {
  email: string;
  password: string;
  tmdb_token: string;
  web_port: number;
  public_url: string;
  totp_secret?: string;
  totp_code?: string;
  backup_enabled: boolean;
  auto_update_enabled: boolean;
  hevc_compression_mode: "auto" | "on" | "off";
  storage_engine: "LOCAL" | "CLOUD";
  rclone_remote_path?: string;
  drive_job_id?: string;
}

export interface SetupCompleteResponse {
  complete: true;
  restartScheduled: boolean;
  webPort: number;
  recoveryCodes: string[];
  ingestionToken: string;
}

export type DriveJobStatus =
  | "authorizing"
  | "exchanging_code"
  | "selecting_folder"
  | "testing"
  | "ready"
  | "failed"
  | "cancelled"
  | "expired";

export interface DriveSetupJob {
  id: string;
  status: DriveJobStatus;
  remoteName: string;
  selectedPath: string;
  progress: string;
  errorCode?: string | null;
  audience: "external" | "internal";
  publishingStatus: "testing" | "production";
  expiresAt: number;
}

export interface DriveFolder { name: string; path: string; id?: string }
export interface DriveFolderList { path: string; folders: DriveFolder[] }
export interface DriveTestResult {
  valid: true;
  remotePath: string;
  quota?: { total?: number; used?: number; free?: number; trashed?: number } | null;
  job: DriveSetupJob;
}

const setupOptions = { credentials: "same-origin" as const };

export const getSetupStatus = () => apiGet<SetupStatus>("/api/setup/status", setupOptions);
export const unlockSetup = (code: string) => apiPost<void>("/api/setup/unlock", { code }, setupOptions);
export const getSetupReadiness = () => apiGet<SetupReadiness>("/api/setup/readiness", setupOptions);
export const validateSetupTMDB = (token: string) => apiPost<{ valid: true }>("/api/setup/tmdb/validate", { token }, setupOptions);
export const beginSetupTOTP = (email: string) => apiPost<{ secret: string; provisioningUri: string }>("/api/setup/totp/begin", { email }, setupOptions);
export const verifySetupTOTP = (secret: string, code: string) => apiPost<{ valid: true }>("/api/setup/totp/verify", { secret, code }, setupOptions);

export const startDriveOAuth = (payload: {
  clientId: string;
  clientSecret: string;
  remoteName: string;
  audience: "external" | "internal";
  publishingStatus: "testing" | "production";
  publicUrl: string;
}) => apiPost<{ jobId: string; authorizationUrl: string; expiresAt: number }>("/api/setup/rclone/drive/oauth/start", {
  client_id: payload.clientId,
  client_secret: payload.clientSecret,
  remote_name: payload.remoteName,
  audience: payload.audience,
  publishing_status: payload.publishingStatus,
  public_url: payload.publicUrl,
}, setupOptions);

export const getDriveJob = (jobId: string) => apiGet<DriveSetupJob>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}`, setupOptions);
export const cancelDriveJob = (jobId: string) => apiDelete<void>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}`, setupOptions);
export const listDriveFolders = (jobId: string, path = "") => apiGet<DriveFolderList>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/folders?path=${encodeURIComponent(path)}`, setupOptions);
export const createDriveFolder = (jobId: string, path: string) => apiPost<{ path: string }>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/folders`, { path }, setupOptions);
export const selectDriveFolder = (jobId: string, path: string) => apiPost<DriveSetupJob>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/select-folder`, { path }, setupOptions);
export const testDriveFolder = (jobId: string) => apiPost<DriveTestResult>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/test`, undefined, setupOptions);
export const activateDrive = (jobId: string) => apiPost<{ valid: true; remotePath: string; job: DriveSetupJob }>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/activate`, undefined, setupOptions);

export const completeSetup = (payload: SetupCompleteRequest) => apiPost<SetupCompleteResponse>("/api/setup/complete", payload, setupOptions);
