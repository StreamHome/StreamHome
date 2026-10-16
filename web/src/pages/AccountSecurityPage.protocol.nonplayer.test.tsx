import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AccountSecurityPage } from "./AccountSecurityPage";

// Keep actual api/auth and api/client mounted: HTTP200 alone is not a valid resource.
const summary = { email: "owner@example.test", twoFactorEnabled: true, recoveryCodesRemaining: 8, sessionLifetimeDays: 30, previousLogin: null };
const session = { id: "current", current: true, createdAt: 1_720_000_000, lastSeenAt: 1_720_000_100, expiresAt: 1_725_000_000, ipAddress: "192.0.2.1", deviceLabel: "Protocol fixture browser" };
const activity = { id: "activity", type: "login_success", outcome: "success", createdAt: 1_720_000_000, ipAddress: "192.0.2.1", deviceLabel: "Protocol fixture device", details: null };
const paths = { summary: "/api/auth/security/summary", sessions: "/api/auth/sessions", events: "/api/auth/security/events" };
type Resource = keyof typeof paths;
let malformed: Resource | null;
let requests: string[];
let unexpected: string[];
let mutationResponses: Map<string, unknown>;
let currentSummary: typeof summary;
let mutations: string[];
beforeEach(() => {
  malformed = null; requests = []; unexpected = []; mutationResponses = new Map(); currentSummary = { ...summary }; mutations = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    requests.push(path);
    const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    if ((options?.method ?? "GET") !== "GET") {
      mutations.push(path);
      if (mutationResponses.has(path)) return json(mutationResponses.get(path));
      unexpected.push(path); throw new Error(`Unexpected fixture mutation: ${path}`);
    }
    if (path === "/api/auth/reauthenticate/status") return json({ reauthenticated: true, remainingSeconds: 600 });
    if (malformed && path === paths[malformed]) return json({});
    if (path === paths.summary) return json(currentSummary);
    if (path === paths.sessions) return json([session]);
    if (path === paths.events) return json({ events: [activity], nextCursor: null });
    unexpected.push(path); throw new Error(`Unexpected fixture request: ${path}`);
  }));
});
afterEach(() => { vi.unstubAllGlobals(); });
function mount() { return render(<MemoryRouter><AccountSecurityPage /></MemoryRouter>); }
const label = (resource: Resource) => resource === "summary" ? "account summary" : resource === "sessions" ? "sessions" : "security activity";
const count = (resource: Resource) => requests.filter((path) => path === paths[resource]).length;
function assertSummaryActionsBlocked() {
  expect((screen.getByRole("button", { name: "Save lifetime" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Update email" }) as HTMLButtonElement).disabled).toBe(true);
  const recovery = screen.queryByRole("button", { name: "Regenerate codes" });
  if (recovery) expect((recovery as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "Set up TOTP" })).toBeNull();
}
describe("actual account read protocol boundaries", () => {
  it.each(["summary", "sessions", "events"] as const)("rejects malformed cold %s HTTP200 independently, then retries only that resource", async (resource) => {
    malformed = resource; mount(); const retry = await screen.findByRole("button", { name: `Retry ${label(resource)}` });
    if (resource !== "summary") expect(await screen.findByText("8 codes remaining")).toBeTruthy();
    if (resource !== "sessions") expect(await screen.findByText("Protocol fixture browser")).toBeTruthy();
    if (resource !== "events") expect(await screen.findByRole("button", { name: /Login Success/ })).toBeTruthy();
    if (resource === "summary") { assertSummaryActionsBlocked(); expect(screen.queryByText("TOTP not enabled")).toBeNull(); expect(screen.queryByText("0 codes remaining")).toBeNull(); expect(screen.queryByText("60 days")).toBeNull(); expect(screen.queryByText("No earlier login recorded")).toBeNull(); }
    if (resource === "sessions") expect(screen.queryByText("Protocol fixture browser")).toBeNull();
    if (resource === "events") expect(screen.queryByText("No security activity has been recorded yet.")).toBeNull();
    malformed = null; fireEvent.click(retry);
    if (resource === "summary") await screen.findByText("8 codes remaining"); else if (resource === "sessions") await screen.findByText("Protocol fixture browser"); else await screen.findByRole("button", { name: /Login Success/ });
    for (const key of Object.keys(paths) as Resource[]) expect(count(key)).toBe(key === resource ? 2 : 1);
    expect(unexpected).toEqual([]);
  });
  it.each(["summary", "sessions", "events"] as const)("retains stale %s data on malformed refresh without granting freshness or crashing", async (resource) => {
    mount(); await screen.findByText("8 codes remaining"); await screen.findByText("Protocol fixture browser"); await screen.findByRole("button", { name: /Login Success/ });
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "draft@example.test" } }); fireEvent.change(screen.getByLabelText("Session lifetime in days"), { target: { value: "45" } });
    malformed = resource; fireEvent.click(screen.getByRole("button", { name: "Refresh" })); const retry = await screen.findByRole("button", { name: `Retry ${label(resource)}` });
    expect(screen.getByText("8 codes remaining")).toBeTruthy(); expect(screen.getByText("Protocol fixture browser")).toBeTruthy(); expect(screen.getByRole("button", { name: /Login Success/ })).toBeTruthy(); expect(screen.getByText(/Showing previously loaded information/)).toBeTruthy();
    if (resource === "summary") assertSummaryActionsBlocked();
    if (resource === "sessions") expect((screen.getByRole("button", { name: "Sign out" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText("Email address") as HTMLInputElement).value).toBe("draft@example.test"); expect((screen.getByLabelText("Session lifetime in days") as HTMLInputElement).value).toBe("45");
    malformed = null; fireEvent.click(retry); await waitFor(() => expect(screen.queryByRole("button", { name: `Retry ${label(resource)}` })).toBeNull());
    for (const key of Object.keys(paths) as Resource[]) expect(count(key)).toBe(key === resource ? 3 : 2);
    expect(unexpected).toEqual([]);
  });
});


describe("actual account mutation acknowledgement boundaries", () => {
  it.each([null, {}, { recoveryCodes: null }])("keeps malformed recovery generation %j unknown without crashing or replaying", async (body) => {
    mutationResponses.set("/api/auth/recovery-codes/regenerate", body);
    mount(); const generate = await screen.findByRole("button", { name: "Regenerate codes" });
    fireEvent.click(generate);
    await screen.findByRole("button", { name: "Check current account status" });
    expect(screen.getByRole("heading", { name: "Account and Security" })).toBeTruthy();
    expect(screen.queryByText("New recovery codes generated. Previous codes no longer work.")).toBeNull();
    expect((generate as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText("Save these recovery codes now")).toBeNull();
    fireEvent.click(generate);
    expect(mutations).toEqual(["/api/auth/recovery-codes/regenerate"]);
    expect(unexpected).toEqual([]);
  });
  it.each([null, {}, { message: "Enabled", recoveryCodes: [null] }])("does not publish malformed enrollment verification %j or undefined recovery codes", async (body) => {
    currentSummary = { ...summary, twoFactorEnabled: false, recoveryCodesRemaining: 0 };
    mutationResponses.set("/api/auth/2fa/setup", { enrollmentId: "fixture-enrollment", manualKey: "SYNTHETICKEY", qrImageUrl: "/synthetic-qr", expiresAt: Date.now() / 1000 + 600 });
    mutationResponses.set("/api/auth/2fa/verify-setup", body);
    mount(); fireEvent.click(await screen.findByRole("button", { name: "Set up TOTP" }));
    fireEvent.change(await screen.findByLabelText("TOTP setup code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Enable TOTP" }));
    await screen.findByRole("button", { name: "Check current account status" });
    expect(screen.getByRole("heading", { name: "Account and Security" })).toBeTruthy();
    expect(screen.queryByText("TOTP successfully enabled.")).toBeNull();
    expect((screen.getByRole("button", { name: "Enable TOTP" }) as HTMLButtonElement).disabled).toBe(true);
    expect(mutations.filter((path) => path.endsWith("verify-setup"))).toHaveLength(1);
    expect(unexpected).toEqual([]);
  });
  it("rejects an incomplete enrollment before displaying a broken QR or inventing a cancel identifier", async () => {
    currentSummary = { ...summary, twoFactorEnabled: false, recoveryCodesRemaining: 0 };
    mutationResponses.set("/api/auth/2fa/setup", {});
    mount(); fireEvent.click(await screen.findByRole("button", { name: "Set up TOTP" }));
    await screen.findByRole("button", { name: "Check current account status" });
    expect(screen.queryByLabelText("TOTP setup code")).toBeNull();
    expect(screen.queryByAltText(/Scan this QR/)).toBeNull();
    expect(mutations).toEqual(["/api/auth/2fa/setup"]);
    expect(unexpected).toEqual([]);
  });
  it("retains the email draft and confirmed identity after an incomplete email acknowledgement", async () => {
    mutationResponses.set("/api/auth/security/email", {});
    mount(); const input = await screen.findByLabelText("Email address");
    await screen.findByText("8 codes remaining");
    fireEvent.change(input, { target: { value: "changed@example.test" } });
    const form = input.closest("form")!;
    fireEvent.change(within(form).getByLabelText("Current password"), { target: { value: "synthetic-password" } });
    fireEvent.submit(form);
    await screen.findByRole("button", { name: "Check current account status" });
    expect((input as HTMLInputElement).value).toBe("changed@example.test");
    expect((screen.getByRole("button", { name: "Update email" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText(/undefined.*revoked/)).toBeNull();
    expect(mutations).toEqual(["/api/auth/security/email"]);
    expect(unexpected).toEqual([]);
  });
});
