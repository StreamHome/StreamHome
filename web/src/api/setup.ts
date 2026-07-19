import { apiGet, apiPost } from "./client";

export interface SetupStatus {
  required: boolean;
  unlocked: boolean;
  webPort: number;
  serverVersion: string;
  mediaPath: string;
  databasePath: string;
}

export interface ReadinessCheck { id: string; ready: boolean; detail: string }
export interface SetupReadiness { ready: boolean; checks: ReadinessCheck[] }

export interface SetupCompleteRequest {
  email: string;
  password: string;
  tmdb_token: string;
  web_port: number;
  totp_secret?: string;
  totp_code?: string;
  backup_enabled: boolean;
  auto_update_enabled: boolean;
  hevc_compression_mode: "auto" | "always" | "never";
  storage_engine: "LOCAL" | "CLOUD";
  rclone_remote_path?: string;
}

export interface SetupCompleteResponse {
  complete: true;
  restartScheduled: boolean;
  webPort: number;
  recoveryCodes: string[];
  ingestionToken: string;
}

export interface RcloneQuestion {
  name: string;
  help: string;
  type: string;
  required: boolean;
  sensitive: boolean;
  defaultValue: string;
  examples: Array<{ value: string; help: string }>;
}
export interface RcloneConfigStep { complete: boolean; flowToken: string; question?: RcloneQuestion; remote?: string }

export const getSetupStatus = () => apiGet<SetupStatus>("/api/setup/status", { credentials: "same-origin" });
export const unlockSetup = (code: string) => apiPost<void>("/api/setup/unlock", { code }, { credentials: "same-origin" });
export const getSetupReadiness = () => apiGet<SetupReadiness>("/api/setup/readiness", { credentials: "same-origin" });
export const validateSetupTMDB = (token: string) => apiPost<{ valid: true }>("/api/setup/tmdb/validate", { token }, { credentials: "same-origin" });
export const beginSetupTOTP = (email: string) => apiPost<{ secret: string; provisioningUri: string }>("/api/setup/totp/begin", { email }, { credentials: "same-origin" });
export const verifySetupTOTP = (secret: string, code: string) => apiPost<{ valid: true }>("/api/setup/totp/verify", { secret, code }, { credentials: "same-origin" });
export const getSetupRcloneRemotes = () => apiGet<{ available: boolean; remotes: string[]; error?: string }>("/api/setup/rclone/remotes", { credentials: "same-origin" });
export const getSetupRcloneProviders = () => apiGet<{ providers: Array<{ id: string; name: string }> }>("/api/setup/rclone/providers", { credentials: "same-origin" });
export const startSetupRcloneConfig = (name: string, provider: string) => apiPost<RcloneConfigStep>("/api/setup/rclone/config/start", { name, provider }, { credentials: "same-origin" });
export const continueSetupRcloneConfig = (flowToken: string, result: string) => apiPost<RcloneConfigStep>("/api/setup/rclone/config/continue", { flow_token: flowToken, result }, { credentials: "same-origin" });
export const cancelSetupRcloneConfig = (flowToken: string) => apiPost<void>("/api/setup/rclone/config/cancel", { flow_token: flowToken }, { credentials: "same-origin" });
export const testSetupRclone = (remotePath: string) => apiPost<{ valid: true; remotePath: string }>("/api/setup/rclone/test", { remote_path: remotePath }, { credentials: "same-origin" });
export const completeSetup = (payload: SetupCompleteRequest) => apiPost<SetupCompleteResponse>("/api/setup/complete", payload, { credentials: "same-origin" });
