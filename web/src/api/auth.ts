import { apiGet, apiPost } from "./client";
import type {
  AuthResponse,
  LoginRequest,
  LoginResponse,
  TwoFASetupResponse,
  TwoFAStatusResponse,
  VerifyRequest,
} from "../types/api";

type RawAuthResponse = {
  accessToken?: string;
  tokenType?: string;
  email: string;
  requires_2fa?: boolean;
  requires2fa?: boolean;
  message?: string;
};

function normalizeAuthResponse(raw: RawAuthResponse): AuthResponse {
  if (raw.requires_2fa || raw.requires2fa) {
    return {
      requires2fa: true,
      email: raw.email,
      message: raw.message ?? "TOTP code required.",
    };
  }
  if (!raw.accessToken) throw new Error("Authentication response did not include an access token.");
  return {
    accessToken: raw.accessToken,
    tokenType: raw.tokenType ?? "bearer",
    email: raw.email,
  };
}

export async function login(data: LoginRequest): Promise<AuthResponse> {
  return normalizeAuthResponse(await apiPost<RawAuthResponse>("/api/auth/login", data));
}

export async function verify2FA(data: VerifyRequest): Promise<LoginResponse> {
  const response = normalizeAuthResponse(await apiPost<RawAuthResponse>("/api/auth/verify", data));
  if ("requires2fa" in response) throw new Error("Verification did not complete authentication.");
  return response;
}

export async function get2FAStatus(): Promise<TwoFAStatusResponse> {
  const raw = await apiGet<{ two_factor_enabled?: boolean; twoFactorEnabled?: boolean; email: string }>("/api/auth/2fa/status");
  return { twoFactorEnabled: raw.two_factor_enabled ?? raw.twoFactorEnabled ?? false, email: raw.email };
}

export async function setup2FA(): Promise<TwoFASetupResponse> {
  const raw = await apiPost<{ secret: string; provisioning_uri?: string; provisioningUri?: string }>("/api/auth/2fa/setup");
  return { secret: raw.secret, provisioningUri: raw.provisioning_uri ?? raw.provisioningUri ?? "" };
}

export const verifySetup2FA = (code: string) => apiPost<{ message: string }>("/api/auth/2fa/verify-setup", { code });
export const disable2FA = (code: string) => apiPost<{ message: string }>("/api/auth/2fa/disable", { code });
