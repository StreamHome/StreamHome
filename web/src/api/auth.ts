import { apiDelete, apiGet, apiPost } from "./client";
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
} from "../types/api";

type RawAuthResponse = {
  accessToken?: string;
  tokenType?: string;
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
  if (!raw.accessToken) throw new Error("Authentication response did not include an access token.");
  return {
    accessToken: raw.accessToken,
    tokenType: raw.tokenType ?? "bearer",
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
export const getSecuritySummary = () => apiGet<SecuritySummary>("/api/auth/security/summary");
export const getAuthSessions = () => apiGet<AuthSessionInfo[]>("/api/auth/sessions");
export const revokeAuthSession = (id: string) => apiDelete<{ revoked: boolean; currentSession: boolean }>(`/api/auth/sessions/${encodeURIComponent(id)}`);
export const revokeOtherSessions = () => apiPost<{ revokedCount: number }>("/api/auth/sessions/revoke-others");
export const getSecurityEvents = (before?: number) => apiGet<SecurityEventsResponse>(`/api/auth/security/events${before ? `?before=${before}` : ""}`);
export const regenerateRecoveryCodes = () => apiPost<{ recoveryCodes: string[]; remaining: number }>("/api/auth/recovery-codes/regenerate");

export async function get2FAStatus(): Promise<TwoFAStatusResponse> {
  const raw = await apiGet<{ two_factor_enabled?: boolean; twoFactorEnabled?: boolean; email: string }>("/api/auth/2fa/status");
  return { twoFactorEnabled: raw.two_factor_enabled ?? raw.twoFactorEnabled ?? false, email: raw.email };
}

export async function setup2FA(): Promise<TwoFASetupResponse> {
  const raw = await apiPost<{ secret: string; provisioning_uri?: string; provisioningUri?: string }>("/api/auth/2fa/setup");
  return { secret: raw.secret, provisioningUri: raw.provisioning_uri ?? raw.provisioningUri ?? "" };
}

export const verifySetup2FA = (code: string) => apiPost<{ message: string; recoveryCodes: string[] }>("/api/auth/2fa/verify-setup", { code });
export const disable2FA = (code: string) => apiPost<{ message: string }>("/api/auth/2fa/disable", { code });
