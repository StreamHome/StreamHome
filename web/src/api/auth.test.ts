import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createIntegrationCredential,
  get2FAStatus,
  login,
  revokeIntegrationCredential,
  setup2FA,
  updateIntegrationCredential,
} from "./auth";

afterEach(() => vi.unstubAllGlobals());

function respond(body: unknown) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })));
}

describe("authentication response normalization", () => {
  it("normalizes the server requires_2fa response", async () => {
    respond({ requires_2fa: true, email: "admin@example.test", message: "TOTP required" });
    await expect(login({ email: "admin@example.test", password: "secret" })).resolves.toEqual({ requires2fa: true, email: "admin@example.test", challengeToken: "", expiresInSeconds: 300, message: "TOTP required" });
  });

  it("normalizes TOTP status and setup keys", async () => {
    respond({ two_factor_enabled: true, email: "admin@example.test" });
    await expect(get2FAStatus()).resolves.toEqual({ twoFactorEnabled: true, email: "admin@example.test" });
    respond({ enrollmentId: "enrollment", manualKey: "ABC", qrImageUrl: "/api/auth/2fa/enrollments/enrollment/qr", expiresAt: 1_720_000_900 });
    await expect(setup2FA()).resolves.toEqual({ enrollmentId: "enrollment", manualKey: "ABC", qrImageUrl: "/api/auth/2fa/enrollments/enrollment/qr", expiresAt: 1_720_000_900 });
  });

  it("uses the multi-key create, update, and revoke contracts", async () => {
    const credential = {
      id: "credential-1",
      name: "Queue monitor",
      tokenHint: "shk_abcd…123456",
      scopes: ["downloads:read"],
      createdAt: 1_720_000_000,
      expiresAt: 1_727_776_000,
      revokedAt: null,
      lastUsedAt: null,
    };
    respond({ credential, token: "shk_show_once" });
    await createIntegrationCredential("Queue monitor", ["downloads:read"], 90);
    let [path, options] = vi.mocked(fetch).mock.calls[0];
    expect(path).toBe("/api/auth/integrations");
    expect(options?.method).toBe("POST");
    expect(JSON.parse(String(options?.body))).toEqual({ name: "Queue monitor", scopes: ["downloads:read"], expires_in_days: 90 });

    respond(credential);
    await updateIntegrationCredential("credential-1", "Queue monitor", ["downloads:read", "downloads:cancel"]);
    [path, options] = vi.mocked(fetch).mock.calls[0];
    expect(path).toBe("/api/auth/integrations/credential-1");
    expect(options?.method).toBe("PUT");
    expect(JSON.parse(String(options?.body))).toEqual({ name: "Queue monitor", scopes: ["downloads:read", "downloads:cancel"] });

    respond({ revoked: true });
    await revokeIntegrationCredential("credential-1");
    [path, options] = vi.mocked(fetch).mock.calls[0];
    expect(path).toBe("/api/auth/integrations/credential-1");
    expect(options?.method).toBe("DELETE");
  });
});
