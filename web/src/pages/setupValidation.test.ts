import { describe, expect, it, vi } from "vitest";
import { formatSetupSecrets, parseSetupPublicOrigin, revealSetupStep, validSetupPort } from "./setupValidation";

describe("setup public origin admission", () => {
  it.each([
    ["https://WATCH.example:443/", "https://WATCH.example:443"],
    ["HTTPS://WATCH.example:443/", "https://WATCH.example:443"],
    ["http://localhost.:80/", "http://localhost.:80"],
    ["https://[2001:db8::1]:8443/", "https://[2001:db8::1]:8443"],
    ["http://[::1]", "http://[::1]"], ["http://[fc00::1]", "http://[fc00::1]"], ["http://[fdff::1234]", "http://[fdff::1234]"],
    ["http://127.255.0.1:3000", "http://127.255.0.1:3000"], ["http://10.0.0.1", "http://10.0.0.1"],
    ["http://172.16.0.1", "http://172.16.0.1"], ["http://172.31.255.255", "http://172.31.255.255"], ["http://192.168.1.2", "http://192.168.1.2"],
    [`https://${"a".repeat(63)}.${"b".repeat(63)}.example/`, `https://${"a".repeat(63)}.${"b".repeat(63)}.example`],
  ])("accepts supported origins and retains their netloc spelling: %s", (input, output) => expect(parseSetupPublicOrigin(input)).toBe(output));
  it.each(["https://", "https:///watch.example", "https://watch.example/path", "https://watch.example?", "https://watch.example#", "https://user@watch.example", "https://watch.example:", "https://watch.example:0", "https://watch.example:65536", "https://watch.example:1.5", "https://watch.example:NaN", "https://watch.example\\evil", "https://watch.example//", "http://watch.example", "http://172.15.255.255", "http://172.32.0.1", "http://192.169.0.1", "http://[fbff::1]", "http://[fe00::1]", "http://[2001:db8::1]", "https://127.1", "https://0x7f000001", "https://0177.0.0.1", "https://123", "https://watch..example", "https://watch.example: ", "https://[::1]:", "ftp://localhost", "https://watch.example/%2f"])('rejects malformed, repaired or disallowed origin %s', (input) => expect(parseSetupPublicOrigin(input)).toBeNull());
  it.each([0, -1, 65536, 1.1, NaN, Infinity])("rejects invalid web port %s", (value) => expect(validSetupPort(value)).toBe(false));
  it.each([1, 3000, 65535])("accepts integer web port %s", (value) => expect(validSetupPort(value)).toBe(true));
});

describe("setup accessible values", () => {
  it("labels the same synthetic secret text with and without recovery codes", () => {
    expect(formatSetupSecrets({ ingestionToken: "synthetic-token", recoveryCodes: [] })).toBe("MediaSender ingestion token:\nsynthetic-token\nKeep this token private. Use it only to connect MediaSender to this server.");
    expect(formatSetupSecrets({ ingestionToken: "synthetic-token", recoveryCodes: ["synthetic-recovery"] })).toContain("TOTP recovery codes:\nEach code can be used once. Keep these codes private.\nsynthetic-recovery");
  });
  it("reveals the active step inside its own scroll container only", () => {
    const container = document.createElement("ol"); const row = document.createElement("li");
    container.scrollTop = 20;
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue({ top: 100, bottom: 305 } as DOMRect);
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ top: 290, bottom: 350 } as DOMRect);
    revealSetupStep(container, row); expect(container.scrollTop).toBe(65);
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ top: 80, bottom: 140 } as DOMRect);
    revealSetupStep(container, row); expect(container.scrollTop).toBe(45);
  });
});
