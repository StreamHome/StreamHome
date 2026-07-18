import { afterEach, describe, expect, it, vi } from "vitest";
import { get2FAStatus, login, setup2FA } from "./auth";

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
    respond({ secret: "ABC", provisioning_uri: "otpauth://totp/example" });
    await expect(setup2FA()).resolves.toEqual({ secret: "ABC", provisioningUri: "otpauth://totp/example" });
  });
});
