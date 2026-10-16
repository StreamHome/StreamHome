import { ApiError, apiDelete, apiGet, apiPost, apiPut } from "./client";
import type {
  AuthResponse,
  LoginRequest,
  LoginResponse,
  TwoFASetupResponse,
  TwoFAStatusResponse,
  VerifyRequest,
  HealthResponse,
  ReauthResponse,
  SecuritySummary,
  AuthSessionInfo,
  SecurityEventsResponse,
  TwoFARequiredResponse,
  AccountEmailUpdateResponse,
  AccountSecurityUpdateResponse,
  SessionPolicyUpdateResponse,
  IntegrationCredentialCreateResponse,
  IntegrationCredentialInfo,
  IntegrationScope,
  IntegrationScopeDefinition,
} from "../types/api";

type RawAuthResponse = {
  email: string;
  requires_2fa?: boolean;
  requires2fa?: boolean;
  challengeToken?: string;
  expiresInSeconds?: number;
  message?: string;
  session?: { id: string; expiresAt: number };
  previousLogin?: LoginResponse["previousLogin"];
  validForSeconds?: number;
};

function normalizeAuthResponse(raw: RawAuthResponse): AuthResponse {
  if (raw.requires_2fa || raw.requires2fa) {
    return {
      requires2fa: true,
      email: raw.email,
      challengeToken: raw.challengeToken ?? "",
      expiresInSeconds: raw.expiresInSeconds ?? 300,
      message: raw.message ?? "TOTP code required.",
    };
  }
  return {
    email: raw.email,
    session: raw.session,
    previousLogin: raw.previousLogin,
  };
}

export async function login(data: LoginRequest, signal?: AbortSignal): Promise<AuthResponse> {
  return normalizeAuthResponse(await apiPost<RawAuthResponse>("/api/auth/login", data, { signal }));
}

export async function verify2FA(data: VerifyRequest, signal?: AbortSignal): Promise<LoginResponse> {
  const response = normalizeAuthResponse(await apiPost<RawAuthResponse>("/api/auth/verify", { challenge_token: data.challengeToken, method: data.method, code: data.code }, { signal }));
  if ("requires2fa" in response) throw new Error("Verification did not complete authentication.");
  return response;
}

export const getHealth = (signal?: AbortSignal) => apiGet<HealthResponse>("/api/health", { signal });

export async function beginReauthentication(password: string): Promise<TwoFARequiredResponse | ReauthResponse> {
  const raw = await apiPost<RawAuthResponse & Partial<ReauthResponse>>("/api/auth/reauthenticate", { password });
  if (raw.requires2fa) return normalizeAuthResponse(raw) as TwoFARequiredResponse;
  return { reauthenticated: true, validForSeconds: raw.validForSeconds ?? 600 };
}

export async function verifyReauthentication(data: VerifyRequest): Promise<ReauthResponse> {
  return apiPost<ReauthResponse>("/api/auth/verify", { challenge_token: data.challengeToken, method: data.method, code: data.code });
}

export const getReauthenticationStatus = () => apiGet<{ reauthenticated: boolean; remainingSeconds: number }>("/api/auth/reauthenticate/status");
export const logoutSession = () => apiPost<void>("/api/auth/logout");
const securityTime = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 8.64e12;
const securityText = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
export async function getSecuritySummary(): Promise<SecuritySummary> {
  const value = await apiGet<SecuritySummary>("/api/auth/security/summary");
  if (!value || !securityText(value.email) || typeof value.twoFactorEnabled !== "boolean"
      || !Number.isInteger(value.recoveryCodesRemaining) || value.recoveryCodesRemaining < 0
      || !Number.isInteger(value.sessionLifetimeDays) || value.sessionLifetimeDays < 1 || value.sessionLifetimeDays > 365
      || !(value.previousLogin === null || (value.previousLogin && typeof value.previousLogin === "object"
        && securityTime(value.previousLogin.at)
        && (value.previousLogin.ipAddress == null || typeof value.previousLogin.ipAddress === "string")
        && (value.previousLogin.deviceLabel == null || typeof value.previousLogin.deviceLabel === "string")))) {
    throw new ApiError("The server returned an invalid account summary. Try loading it again.", 0, "invalid_response");
  }
  return value;
}
const securityCount = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value) && value >= 0;
const recoveryCodeList = (value: unknown): value is string[] => Array.isArray(value) && value.length > 0 && value.every(securityText);
function invalidAccountChange(): never {
  // A malformed success body cannot prove whether a server mutation committed.
  throw new ApiError("The server returned an incomplete account-change response. Check account status before another change.", 0, "invalid_response");
}
export async function updateAccountEmail(email: string, currentPassword: string): Promise<AccountEmailUpdateResponse> {
  const value = await apiPut<AccountEmailUpdateResponse>("/api/auth/security/email", { email, current_password: currentPassword });
  if (!value || !securityText(value.email) || !securityText(value.message) || !securityCount(value.otherSessionsRevoked)) invalidAccountChange();
  return value;
}
export async function updateAccountPassword(currentPassword: string, newPassword: string): Promise<AccountSecurityUpdateResponse> {
  const value = await apiPut<AccountSecurityUpdateResponse>("/api/auth/security/password", { current_password: currentPassword, new_password: newPassword });
  if (!value || !securityText(value.message) || !securityCount(value.otherSessionsRevoked)) invalidAccountChange();
  return value;
}
export async function updateSessionPolicy(sessionLifetimeDays: number): Promise<SessionPolicyUpdateResponse> {
  const value = await apiPut<SessionPolicyUpdateResponse>("/api/auth/security/session-policy", { session_lifetime_days: sessionLifetimeDays });
  if (!value || !securityText(value.message) || !Number.isInteger(value.sessionLifetimeDays)
      || value.sessionLifetimeDays < 1 || value.sessionLifetimeDays > 365 || value.existingSessionsChanged !== false) invalidAccountChange();
  return value;
}
export async function getAuthSessions(): Promise<AuthSessionInfo[]> {
  const value = await apiGet<AuthSessionInfo[]>("/api/auth/sessions");
  if (!Array.isArray(value) || !value.every((item) => item && securityText(item.id)
      && securityTime(item.createdAt) && securityTime(item.lastSeenAt) && securityTime(item.expiresAt)
      && typeof item.ipAddress === "string" && typeof item.deviceLabel === "string" && typeof item.current === "boolean")) {
    throw new ApiError("The server returned an invalid session list. Try loading it again.", 0, "invalid_response");
  }
  return value;
}
export async function revokeAuthSession(id: string): Promise<{ revoked: boolean; currentSession: boolean }> {
  const value = await apiDelete<{ revoked: boolean; currentSession: boolean }>(`/api/auth/sessions/${encodeURIComponent(id)}`);
  if (!value || value.revoked !== true || typeof value.currentSession !== "boolean") invalidAccountChange();
  return value;
}
export async function revokeOtherSessions(): Promise<{ revokedCount: number }> {
  const value = await apiPost<{ revokedCount: number }>("/api/auth/sessions/revoke-others");
  if (!value || !securityCount(value.revokedCount)) invalidAccountChange();
  return value;
}
export async function getSecurityEvents(before?: number): Promise<SecurityEventsResponse> {
  const value = await apiGet<SecurityEventsResponse>(`/api/auth/security/events${before ? `?before=${before}` : ""}`);
  if (!value || !Array.isArray(value.events) || !(value.nextCursor === null || securityTime(value.nextCursor))
      || !value.events.every((item) => item && securityText(item.id) && securityText(item.type) && securityText(item.outcome)
        && securityTime(item.createdAt) && typeof item.ipAddress === "string" && typeof item.deviceLabel === "string"
        && (item.details == null || (typeof item.details === "object" && !Array.isArray(item.details))))) {
    throw new ApiError("The server returned invalid security activity. Try loading it again.", 0, "invalid_response");
  }
  return value;
}
export async function regenerateRecoveryCodes(): Promise<{ recoveryCodes: string[]; remaining: number }> {
  const value = await apiPost<{ recoveryCodes: string[]; remaining: number }>("/api/auth/recovery-codes/regenerate");
  if (!value || !recoveryCodeList(value.recoveryCodes) || !securityCount(value.remaining)) invalidAccountChange();
  return value;
}
export const getIntegrationCredentials = (signal?: AbortSignal) => apiGet<IntegrationCredentialInfo[]>("/api/auth/integrations", { signal });
export const getIntegrationScopes = (signal?: AbortSignal) => apiGet<IntegrationScopeDefinition[]>("/api/auth/integrations/scopes", { signal });
export const createIntegrationCredential = (name: string, scopes: IntegrationScope[], expiresInDays: number | null) =>
  apiPost<IntegrationCredentialCreateResponse>("/api/auth/integrations", { name, scopes, expires_in_days: expiresInDays });
export const updateIntegrationCredential = (id: string, name: string, scopes: IntegrationScope[]) =>
  apiPut<IntegrationCredentialInfo>(`/api/auth/integrations/${encodeURIComponent(id)}`, { name, scopes });
export const revokeIntegrationCredential = (id: string) =>
  apiDelete<{ revoked: boolean }>(`/api/auth/integrations/${encodeURIComponent(id)}`);

export async function get2FAStatus(): Promise<TwoFAStatusResponse> {
  const raw = await apiGet<{ two_factor_enabled?: boolean; twoFactorEnabled?: boolean; email: string }>("/api/auth/2fa/status");
  return { twoFactorEnabled: raw.two_factor_enabled ?? raw.twoFactorEnabled ?? false, email: raw.email };
}

export async function setup2FA(): Promise<TwoFASetupResponse> {
  const value = await apiPost<TwoFASetupResponse>("/api/auth/2fa/setup");
  if (!value || !securityText(value.enrollmentId) || !securityText(value.manualKey)
      || !securityText(value.qrImageUrl) || !securityTime(value.expiresAt)) invalidAccountChange();
  return value;
}

export async function verifySetup2FA(enrollmentId: string, code: string): Promise<{ message: string; recoveryCodes: string[] }> {
  const value = await apiPost<{ message: string; recoveryCodes: string[] }>("/api/auth/2fa/verify-setup", { enrollment_id: enrollmentId, code });
  if (!value || !securityText(value.message) || !recoveryCodeList(value.recoveryCodes)) invalidAccountChange();
  return value;
}
export const cancelSetup2FA = (enrollmentId: string) => apiDelete<void>(`/api/auth/2fa/enrollments/${encodeURIComponent(enrollmentId)}`);
export async function disable2FA(code: string): Promise<{ message: string }> {
  const value = await apiPost<{ message: string }>("/api/auth/2fa/disable", { code });
  if (!value || !securityText(value.message)) invalidAccountChange();
  return value;
}
