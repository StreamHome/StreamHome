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


describe("account mutation wire validation", () => {
  it.each([null, {}])("does not acknowledge an incomplete successful account mutation %j", async (body) => {
    const auth = await import("./auth");
    const calls = [() => auth.updateAccountEmail("new@example.test", "synthetic"), () => auth.updateAccountPassword("synthetic", "replacement"), () => auth.updateSessionPolicy(45), () => auth.revokeAuthSession("fixture"), () => auth.revokeOtherSessions(), () => auth.setup2FA(), () => auth.verifySetup2FA("fixture", "123456"), () => auth.regenerateRecoveryCodes(), () => auth.disable2FA("123456")];
    for (const call of calls) { respond(body); await expect(call()).rejects.toMatchObject({ status: 0, code: "invalid_response" }); }
  });
  it("preserves valid zero-count acknowledgements and the unchanged-session policy contract", async () => {
    const auth = await import("./auth");
    respond({ email: "new@example.test", message: "Email updated.", otherSessionsRevoked: 0 });
    await expect(auth.updateAccountEmail("new@example.test", "synthetic")).resolves.toMatchObject({ otherSessionsRevoked: 0 });
    respond({ message: "Password updated.", otherSessionsRevoked: 0 });
    await expect(auth.updateAccountPassword("synthetic", "replacement")).resolves.toMatchObject({ otherSessionsRevoked: 0 });
    respond({ message: "Policy updated.", sessionLifetimeDays: 45, existingSessionsChanged: false });
    await expect(auth.updateSessionPolicy(45)).resolves.toMatchObject({ sessionLifetimeDays: 45, existingSessionsChanged: false });
    respond({ revoked: true, currentSession: false }); await expect(auth.revokeAuthSession("fixture")).resolves.toMatchObject({ currentSession: false });
    respond({ revokedCount: 0 }); await expect(auth.revokeOtherSessions()).resolves.toEqual({ revokedCount: 0 });
    respond({ recoveryCodes: ["synthetic-recovery"], remaining: 1 }); await expect(auth.regenerateRecoveryCodes()).resolves.toEqual({ recoveryCodes: ["synthetic-recovery"], remaining: 1 });
    respond({ message: "Enabled", recoveryCodes: ["synthetic-recovery"] }); await expect(auth.verifySetup2FA("fixture", "123456")).resolves.toMatchObject({ recoveryCodes: ["synthetic-recovery"] });
    respond({ message: "Disabled" }); await expect(auth.disable2FA("123456")).resolves.toEqual({ message: "Disabled" });
  });
  it.each([{ message: "Generated", recoveryCodes: [null], remaining: 1 }, { recoveryCodes: [], remaining: 0 }, { recoveryCodes: ["synthetic"], remaining: -1 }])("keeps malformed show-once material %j unconfirmed", async (body) => {
    const auth = await import("./auth"); respond(body); await expect(auth.regenerateRecoveryCodes()).rejects.toMatchObject({ code: "invalid_response" });
  });
});
