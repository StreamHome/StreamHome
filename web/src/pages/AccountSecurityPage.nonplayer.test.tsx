import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as auth from "../api/auth";
import { ApiError } from "../api/client";
import { AccountSecurityPage } from "./AccountSecurityPage";
import type { SecuritySummary } from "../types/api";
vi.mock("../api/auth", () => ({ getReauthenticationStatus: vi.fn(), beginReauthentication: vi.fn(), verifyReauthentication: vi.fn(), getSecuritySummary: vi.fn(), getAuthSessions: vi.fn(), getSecurityEvents: vi.fn(), revokeAuthSession: vi.fn(), revokeOtherSessions: vi.fn(), regenerateRecoveryCodes: vi.fn(), setup2FA: vi.fn(), verifySetup2FA: vi.fn(), cancelSetup2FA: vi.fn(), disable2FA: vi.fn(), updateAccountEmail: vi.fn(), updateAccountPassword: vi.fn(), updateSessionPolicy: vi.fn() }));
const summary: SecuritySummary = { email: "owner@example.test", twoFactorEnabled: true, recoveryCodesRemaining: 8, sessionLifetimeDays: 30, previousLogin: null };
const sessions = [{ id: "current", current: true, createdAt: 1_720_000_000, lastSeenAt: 1_720_000_100, expiresAt: 1_725_000_000, ipAddress: "fixture", deviceLabel: "Fixture browser" }];
const event = { id: "event", type: "login_success", outcome: "success", createdAt: 1_720_000_000, ipAddress: "fixture", deviceLabel: "Fixture device", details: null };
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; }); return { promise, resolve, reject }; }
function mount() { return render(<MemoryRouter><AccountSecurityPage /></MemoryRouter>); }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(auth.getReauthenticationStatus).mockResolvedValue({ reauthenticated: true, remainingSeconds: 600 }); vi.mocked(auth.getSecuritySummary).mockResolvedValue(summary); vi.mocked(auth.getAuthSessions).mockResolvedValue(sessions); vi.mocked(auth.getSecurityEvents).mockResolvedValue({ events: [event], nextCursor: null }); });
describe("independent account security resources", () => {
  it.each(["summary", "sessions", "events"] as const)("settles other resources when %s fails and retries only that resource", async (resource) => {
    const method = resource === "summary" ? auth.getSecuritySummary : resource === "sessions" ? auth.getAuthSessions : auth.getSecurityEvents;
    vi.mocked(method).mockRejectedValueOnce(new ApiError("Fixture unavailable", 503)); mount();
    const label = resource === "summary" ? "account summary" : resource === "sessions" ? "sessions" : "security activity";
    const retry = await screen.findByRole("button", { name: `Retry ${label}` });
    if (resource !== "summary") expect(await screen.findByText("8 codes remaining")).toBeTruthy();
    if (resource !== "sessions") expect(await screen.findByText("Fixture browser")).toBeTruthy();
    if (resource !== "events") expect(await screen.findByRole("button", { name: /Login Success/ })).toBeTruthy();
    if (resource === "summary") {
      expect(screen.queryByText("60 days")).toBeNull(); expect(screen.queryByText("0 codes remaining")).toBeNull(); expect(screen.queryByText("No earlier login recorded")).toBeNull(); expect(screen.queryByText("TOTP not enabled")).toBeNull();
      expect(screen.queryByRole("button", { name: "Set up TOTP" })).toBeNull(); expect((screen.getByRole("button", { name: "Save lifetime" }) as HTMLButtonElement).disabled).toBe(true);
    }
    fireEvent.click(retry); await waitFor(() => expect(method).toHaveBeenCalledTimes(2));
    expect(auth.getSecuritySummary).toHaveBeenCalledTimes(resource === "summary" ? 2 : 1); expect(auth.getAuthSessions).toHaveBeenCalledTimes(resource === "sessions" ? 2 : 1); expect(auth.getSecurityEvents).toHaveBeenCalledTimes(resource === "events" ? 2 : 1);
  });
  it("distinguishes pending unknown from successful false, zero and empty values", async () => {
    const pending = deferred<SecuritySummary>(); vi.mocked(auth.getSecuritySummary).mockReturnValue(pending.promise); vi.mocked(auth.getSecurityEvents).mockResolvedValue({ events: [], nextCursor: null });
    mount(); await screen.findByText("Fixture browser"); expect(screen.queryByText("TOTP not enabled")).toBeNull(); expect(screen.queryByText("0 codes remaining")).toBeNull();
    expect(await screen.findByText("No security activity has been recorded yet.")).toBeTruthy();
    await act(async () => pending.resolve({ ...summary, twoFactorEnabled: false, recoveryCodesRemaining: 0 }));
    expect(screen.getByText("TOTP not enabled")).toBeTruthy(); expect(screen.getByText("0 codes remaining")).toBeTruthy(); expect(screen.getByText("No earlier login recorded")).toBeTruthy(); expect(screen.getByRole("button", { name: "Set up TOTP" })).toBeTruthy();
  });
  it("retains stale sections while disabling summary and session dependent actions", async () => {
    mount(); await screen.findByText("8 codes remaining");
    vi.mocked(auth.getSecuritySummary).mockRejectedValue(new ApiError("Summary offline", 503)); vi.mocked(auth.getAuthSessions).mockRejectedValue(new ApiError("Sessions offline", 503));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" })); await screen.findByRole("button", { name: "Retry account summary" });
    expect(screen.getByText("8 codes remaining")).toBeTruthy(); expect(screen.getByText("Fixture browser")).toBeTruthy(); expect((screen.getByRole("button", { name: "Regenerate codes" }) as HTMLButtonElement).disabled).toBe(true); expect((screen.getByRole("button", { name: "Sign out" }) as HTMLButtonElement).disabled).toBe(true);
  });
  it("keeps dirty fields through a deferred initial read and resource retry", async () => {
    const pending = deferred<SecuritySummary>(); vi.mocked(auth.getSecuritySummary).mockReturnValueOnce(pending.promise); mount();
    const email = await screen.findByLabelText("Email address") as HTMLInputElement; const lifetime = screen.getByLabelText("Session lifetime in days") as HTMLInputElement;
    fireEvent.change(email, { target: { value: "draft@example.test" } }); fireEvent.change(lifetime, { target: { value: "45" } });
    await act(async () => pending.reject(new ApiError("Unavailable", 503))); fireEvent.click(screen.getByRole("button", { name: "Retry account summary" })); await screen.findByText("8 codes remaining");
    expect(email.value).toBe("draft@example.test"); expect(lifetime.value).toBe("45"); expect((screen.getByRole("button", { name: "Save lifetime" }) as HTMLButtonElement).disabled).toBe(false);
  });
  it("keeps newer email and lifetime edits after acknowledged mutations and failed refresh", async () => {
    const saveEmail = deferred<Awaited<ReturnType<typeof auth.updateAccountEmail>>>(); vi.mocked(auth.updateAccountEmail).mockReturnValue(saveEmail.promise); mount(); await screen.findByText("8 codes remaining");
    const email = screen.getByLabelText("Email address") as HTMLInputElement; const lifetime = screen.getByLabelText("Session lifetime in days") as HTMLInputElement;
    fireEvent.change(email, { target: { value: "submitted@example.test" } }); fireEvent.change(screen.getAllByLabelText("Current password")[0], { target: { value: "fixture" } }); fireEvent.click(screen.getByRole("button", { name: "Update email" }));
    fireEvent.change(email, { target: { value: "newer@example.test" } }); fireEvent.change(lifetime, { target: { value: "45" } }); vi.mocked(auth.getSecuritySummary).mockRejectedValue(new ApiError("Follow-up offline", 503));
    await act(async () => saveEmail.resolve({ email: "submitted@example.test", message: "Saved email.", otherSessionsRevoked: 0 })); await screen.findByRole("button", { name: "Retry account summary" });
    expect(email.value).toBe("newer@example.test"); expect(lifetime.value).toBe("45"); expect(screen.getByText(/Saved email/)).toBeTruthy(); expect(screen.getByText("owner@example.test")).toBeTruthy();
    vi.mocked(auth.getSecuritySummary).mockResolvedValue({ ...summary, email: "submitted@example.test" }); fireEvent.click(screen.getByRole("button", { name: "Retry account summary" })); await waitFor(() => expect((screen.getByRole("button", { name: "Save lifetime" }) as HTMLButtonElement).disabled).toBe(false));
    const savePolicy = deferred<Awaited<ReturnType<typeof auth.updateSessionPolicy>>>(); vi.mocked(auth.updateSessionPolicy).mockReturnValue(savePolicy.promise); fireEvent.click(screen.getByRole("button", { name: "Save lifetime" })); fireEvent.change(lifetime, { target: { value: "90" } });
    vi.mocked(auth.getSecuritySummary).mockRejectedValue(new ApiError("Policy refresh offline", 503)); await act(async () => savePolicy.resolve({ sessionLifetimeDays: 45, message: "Lifetime saved.", existingSessionsChanged: false })); await screen.findByRole("button", { name: "Retry account summary" }); expect(lifetime.value).toBe("90"); expect(screen.getByText("Lifetime saved.")).toBeTruthy(); expect(screen.getByText("30 days")).toBeTruthy();
  });
  it("retains acknowledged recovery codes when the summary refresh fails", async () => {
    vi.mocked(auth.regenerateRecoveryCodes).mockResolvedValue({ recoveryCodes: ["FIXTURE-ONCE"], remaining: 1 }); mount(); await screen.findByText("8 codes remaining"); vi.mocked(auth.getSecuritySummary).mockRejectedValue(new ApiError("Refresh offline", 503)); fireEvent.click(screen.getByRole("button", { name: "Regenerate codes" })); await screen.findByRole("button", { name: "Retry account summary" }); expect(screen.getByText("FIXTURE-ONCE")).toBeTruthy(); expect(screen.getByText("New recovery codes generated. Previous codes no longer work.")).toBeTruthy();
  });
  it("closes account overlays and rejects late reads when reauthentication expires", async () => {
    const summaryRead = deferred<SecuritySummary>(); const sessionsRead = deferred<typeof sessions>();
    mount(); await screen.findByText("8 codes remaining"); vi.mocked(auth.getSecuritySummary).mockReturnValue(summaryRead.promise); vi.mocked(auth.getAuthSessions).mockReturnValue(sessionsRead.promise);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" })); const invoker = screen.getByRole("button", { name: /Login Success/ }); invoker.focus(); fireEvent.click(invoker); await screen.findByRole("dialog");
    await act(async () => sessionsRead.reject(new ApiError("Confirm again", 401, "reauthentication_required")));
    expect(screen.queryByRole("dialog")).toBeNull(); expect(screen.queryByRole("navigation", { name: "Account tasks" })).toBeNull(); const heading = screen.getByRole("heading", { name: "Confirm your identity" }); expect(document.activeElement).toBe(heading); expect(document.body.style.overflow).toBe("");
    await act(async () => summaryRead.resolve({ ...summary, email: "late@example.test" })); expect(screen.queryByText("late@example.test")).toBeNull(); expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("preserves rows and cursor on pagination error, and retries locally", async () => {
    vi.mocked(auth.getSecurityEvents).mockResolvedValueOnce({ events: [event], nextCursor: 123 }).mockRejectedValueOnce(new ApiError("Earlier read failed", 503)).mockResolvedValueOnce({ events: [{ ...event, id: "older", type: "password_updated" }], nextCursor: null }); mount();
    fireEvent.click(await screen.findByRole("button", { name: "Load earlier activity" })); expect(await screen.findByRole("button", { name: "Retry earlier activity" })).toBeTruthy(); expect(screen.getByRole("button", { name: /Login Success/ })).toBeTruthy(); fireEvent.click(screen.getByRole("button", { name: "Retry earlier activity" })); await screen.findByRole("button", { name: /Password Updated/ }); expect(auth.getSecurityEvents).toHaveBeenNthCalledWith(2, 123); expect(auth.getSecurityEvents).toHaveBeenNthCalledWith(3, 123); expect(auth.getSecuritySummary).toHaveBeenCalledTimes(1);
  });
  it.each(["email", "lifetime", "recovery"] as const)("requires read-only reconciliation for an unknown %s mutation outcome", async (kind) => {
    const method = kind === "email" ? auth.updateAccountEmail : kind === "lifetime" ? auth.updateSessionPolicy : auth.regenerateRecoveryCodes;
    vi.mocked(method).mockRejectedValue(new ApiError("The response body timed out.", 0, "request_timeout")); mount(); await screen.findByText("8 codes remaining");
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "submitted@example.test" } }); fireEvent.change(screen.getAllByLabelText("Current password")[0], { target: { value: "fixture" } }); fireEvent.change(screen.getByLabelText("Session lifetime in days"), { target: { value: "45" } });
    fireEvent.click(screen.getByRole("button", { name: kind === "email" ? "Update email" : kind === "lifetime" ? "Save lifetime" : "Regenerate codes" }));
    const check = await screen.findByRole("button", { name: "Check current account status" }); expect(screen.getByText(/outcome is unconfirmed/)).toBeTruthy(); expect((screen.getByRole("button", { name: "Update password" }) as HTMLButtonElement).disabled).toBe(true);
    vi.mocked(auth.getSecuritySummary).mockRejectedValueOnce(new ApiError("Reconciliation unavailable", 503)); fireEvent.click(check); await screen.findByText(/Reconciliation unavailable/); expect(method).toHaveBeenCalledTimes(1); expect((screen.getByRole("button", { name: "Regenerate codes" }) as HTMLButtonElement).disabled).toBe(true);
    vi.mocked(auth.getSecuritySummary).mockResolvedValue({ ...summary, email: "submitted@example.test", sessionLifetimeDays: 45 }); fireEvent.click(check); await screen.findByText(/Current status was read successfully/); expect(method).toHaveBeenCalledTimes(1); expect((screen.getByLabelText("Email address") as HTMLInputElement).value).toBe("submitted@example.test"); expect(screen.getByText("45 days")).toBeTruthy(); if (kind === "recovery") expect(screen.getByText(/Unreturned codes cannot be recovered/)).toBeTruthy();
  });
  it("can reconcile again after reauthentication interrupts an unknown-outcome read", async () => {
    vi.mocked(auth.updateSessionPolicy).mockRejectedValue(new ApiError("Body response lost", 0, "request_timeout")); vi.mocked(auth.beginReauthentication).mockResolvedValue({ reauthenticated: true, validForSeconds: 600 }); mount(); await screen.findByText("8 codes remaining"); fireEvent.change(screen.getByLabelText("Session lifetime in days"), { target: { value: "45" } }); fireEvent.click(screen.getByRole("button", { name: "Save lifetime" })); const check = await screen.findByRole("button", { name: "Check current account status" });
    vi.mocked(auth.getAuthSessions).mockRejectedValueOnce(new ApiError("Confirm again", 403, "reauthentication_required")); fireEvent.click(check); await screen.findByRole("heading", { name: "Confirm your identity" }); fireEvent.change(screen.getByLabelText("Password", { exact: true }), { target: { value: "fixture" } }); fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    const nextCheck = await screen.findByRole("button", { name: "Check current account status" }); expect((nextCheck as HTMLButtonElement).disabled).toBe(false); fireEvent.click(nextCheck); await screen.findByText(/Current status was read successfully/); expect(auth.updateSessionPolicy).toHaveBeenCalledTimes(1);
  });
  it("does not publish deferred resources after unmount", async () => { const pending = deferred<SecuritySummary>(); vi.mocked(auth.getSecuritySummary).mockReturnValue(pending.promise); const view = mount(); await screen.findByText("Fixture browser"); view.unmount(); await act(async () => pending.resolve(summary)); expect(screen.queryByText("8 codes remaining")).toBeNull(); });
});
