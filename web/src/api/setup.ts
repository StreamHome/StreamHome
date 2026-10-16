import { ApiError, apiDelete, apiGet, apiPost, type ApiRequestInit } from "./client";

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
  tmdb_validation_token: string;
  web_port: number;
  public_url: string;
  totp_enrollment_id?: string;
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

export async function getSetupStatus(options?: ApiRequestInit): Promise<SetupStatus> {
  const value = await apiGet<SetupStatus>("/api/setup/status", { ...options, ...setupOptions });
  if (!value || typeof value.required !== "boolean" || typeof value.unlocked !== "boolean"
      || !Number.isInteger(value.webPort) || value.webPort < 1 || value.webPort > 65535
      || typeof value.serverVersion !== "string" || typeof value.mediaPath !== "string" || typeof value.databasePath !== "string"
      || [value.publicUrl, value.driveCallbackUrl, value.driveGuideUrl].some((field) => field != null && typeof field !== "string")) {
    throw new ApiError("The server returned an invalid setup status. Try checking again.", 0, "invalid_response");
  }
  return { ...value, publicUrl: value.publicUrl ?? undefined, driveCallbackUrl: value.driveCallbackUrl ?? undefined, driveGuideUrl: value.driveGuideUrl ?? undefined };
}
export const unlockSetup = (code: string, driveJobId?: string) => apiPost<void>("/api/setup/unlock", { code, drive_job_id: driveJobId || undefined }, setupOptions);
export async function getSetupReadiness(): Promise<SetupReadiness> {
  const value = await apiGet<SetupReadiness>("/api/setup/readiness", setupOptions);
  if (!value || typeof value.ready !== "boolean" || !Array.isArray(value.checks)
      || !value.checks.every((check) => check && typeof check.id === "string" && typeof check.ready === "boolean" && typeof check.detail === "string")) {
    throw new ApiError("The server returned invalid readiness checks. Run the checks again.", 0, "invalid_response");
  }
  return value;
}
export async function validateSetupTMDB(token: string): Promise<{ valid: true; validationToken: string }> {
  const value = await apiPost<{ valid: true; validationToken: string }>("/api/setup/tmdb/validate", { token }, setupOptions);
  if (!value || value.valid !== true || typeof value.validationToken !== "string" || !value.validationToken.trim()) {
    throw new ApiError("The server did not confirm token validation. Validate the token again.", 0, "invalid_response");
  }
  return value;
}
export interface SetupTOTPEnrollment {
  enrollmentId: string;
  manualKey: string;
  qrImageUrl: string;
  expiresAt: number;
}

const nonBlank = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;

export async function beginSetupTOTP(email: string): Promise<SetupTOTPEnrollment> {
  const value = await apiPost<SetupTOTPEnrollment>("/api/setup/totp/begin", { email }, setupOptions);
  if (!value || !nonBlank(value.enrollmentId) || !nonBlank(value.manualKey) || !nonBlank(value.qrImageUrl)
      || typeof value.expiresAt !== "number" || !Number.isFinite(value.expiresAt)) {
    throw new ApiError("Enrollment returned an incomplete response. Check its status before trying again.", 0, "invalid_response");
  }
  return value;
}

export async function verifySetupTOTP(enrollmentId: string, code: string): Promise<{ valid: true; enrollmentId: string }> {
  const value = await apiPost<{ valid: true; enrollmentId: string }>("/api/setup/totp/verify", { enrollment_id: enrollmentId, code }, setupOptions);
  if (!value || value.valid !== true || !nonBlank(value.enrollmentId) || value.enrollmentId !== enrollmentId) {
    throw new ApiError("Verification returned an incomplete response. Check its status before trying again.", 0, "invalid_response");
  }
  return value;
}
export const cancelSetupTOTP = (enrollmentId: string) => apiDelete<void>(`/api/setup/totp/enrollments/${encodeURIComponent(enrollmentId)}`, setupOptions);

export async function startDriveOAuth(payload: {
  clientId: string;
  clientSecret: string;
  remoteName: string;
  audience: "external" | "internal";
  publishingStatus: "testing" | "production";
  publicUrl: string;
}): Promise<{ jobId: string; authorizationUrl: string; expiresAt: number }> {
  const value = await apiPost<{ jobId: string; authorizationUrl: string; expiresAt: number }>("/api/setup/rclone/drive/oauth/start", {
  client_id: payload.clientId,
  client_secret: payload.clientSecret,
  remote_name: payload.remoteName,
  audience: payload.audience,
  publishing_status: payload.publishingStatus,
  public_url: payload.publicUrl,
  }, setupOptions);
  if (!value || !nonBlank(value.jobId) || !nonBlank(value.authorizationUrl)
      || typeof value.expiresAt !== "number" || !Number.isFinite(value.expiresAt)) {
    throw new ApiError("Drive connection returned an incomplete response. Check its status before trying again.", 0, "invalid_response");
  }
  return value;
}

const driveStatuses = new Set<DriveJobStatus>(["authorizing", "exchanging_code", "selecting_folder", "testing", "ready", "failed", "cancelled", "expired"]);
function isDriveJob(value: unknown, jobId: string): value is DriveSetupJob {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const job = value as Partial<DriveSetupJob>;
  return job.id === jobId && driveStatuses.has(job.status as DriveJobStatus)
    && nonBlank(job.remoteName) && typeof job.selectedPath === "string" && typeof job.progress === "string"
    && (job.audience === "external" || job.audience === "internal")
    && (job.publishingStatus === "testing" || job.publishingStatus === "production")
    && typeof job.expiresAt === "number" && Number.isFinite(job.expiresAt)
    && (job.errorCode == null || typeof job.errorCode === "string");
}
function invalidDriveResponse(): never {
  throw new ApiError("The Drive response was incomplete. Read its current status before another change.", 0, "invalid_response");
}
export async function getDriveJob(jobId: string, options?: ApiRequestInit): Promise<DriveSetupJob> {
  const value = await apiGet<DriveSetupJob>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}`, { ...options, ...setupOptions });
  if (!isDriveJob(value, jobId)) invalidDriveResponse();
  return value;
}
export const cancelDriveJob = (jobId: string) => apiDelete<void>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}`, setupOptions);
export async function listDriveFolders(jobId: string, path = "", options?: ApiRequestInit): Promise<DriveFolderList> {
  const value = await apiGet<DriveFolderList>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/folders?path=${encodeURIComponent(path)}`, { ...options, ...setupOptions });
  if (!value || typeof value.path !== "string" || !Array.isArray(value.folders)
      || !value.folders.every((folder) => folder && nonBlank(folder.name) && nonBlank(folder.path) && (folder.id == null || typeof folder.id === "string"))) invalidDriveResponse();
  return value;
}
export async function createDriveFolder(jobId: string, path: string): Promise<{ path: string }> {
  const value = await apiPost<{ path: string }>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/folders`, { path }, setupOptions);
  if (!value || !nonBlank(value.path)) invalidDriveResponse();
  return value;
}
export async function selectDriveFolder(jobId: string, path: string): Promise<DriveSetupJob> {
  const value = await apiPost<DriveSetupJob>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/select-folder`, { path }, setupOptions);
  if (!isDriveJob(value, jobId) || value.selectedPath !== path) invalidDriveResponse();
  return value;
}
function validDriveTest(value: DriveTestResult, jobId: string): boolean {
  return Boolean(value && value.valid === true && nonBlank(value.remotePath) && isDriveJob(value.job, jobId) && value.job.status === "ready"
    && (value.quota == null || (typeof value.quota === "object" && !Array.isArray(value.quota)
      && [value.quota.total, value.quota.used, value.quota.free, value.quota.trashed].every((amount) => amount == null || (typeof amount === "number" && Number.isFinite(amount))))));
}
export async function testDriveFolder(jobId: string): Promise<DriveTestResult> {
  const value = await apiPost<DriveTestResult>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/test`, undefined, setupOptions);
  if (!validDriveTest(value, jobId)) invalidDriveResponse();
  return value;
}
export async function activateDrive(jobId: string): Promise<{ valid: true; remotePath: string; job: DriveSetupJob }> {
  const value = await apiPost<{ valid: true; remotePath: string; job: DriveSetupJob }>(`/api/setup/rclone/drive/jobs/${encodeURIComponent(jobId)}/activate`, undefined, setupOptions);
  if (!validDriveTest(value, jobId)) invalidDriveResponse();
  return value;
}

export async function completeSetup(payload: SetupCompleteRequest): Promise<SetupCompleteResponse> {
  const response = await apiPost<SetupCompleteResponse>("/api/setup/complete", payload, setupOptions);
  if (!response || response.complete !== true || typeof response.restartScheduled !== "boolean"
      || !Number.isInteger(response.webPort) || response.webPort < 1 || response.webPort > 65535
      || !Array.isArray(response.recoveryCodes) || !response.recoveryCodes.every((code) => typeof code === "string")
      || typeof response.ingestionToken !== "string" || !response.ingestionToken) {
    // Initialization may have committed even when its show-once payload is lost.
    throw new ApiError("Initialization returned an incomplete response. Check setup status before taking further action.", 0, "invalid_response");
  }
  return response;
}
