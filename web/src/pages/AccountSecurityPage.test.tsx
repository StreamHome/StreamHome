import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as auth from "../api/auth";
import { AccountSecurityPage } from "./AccountSecurityPage";

vi.mock("../api/auth", () => ({
  getReauthenticationStatus: vi.fn(), beginReauthentication: vi.fn(), verifyReauthentication: vi.fn(),
  getSecuritySummary: vi.fn(), getAuthSessions: vi.fn(), getSecurityEvents: vi.fn(),
  revokeAuthSession: vi.fn(), revokeOtherSessions: vi.fn(), regenerateRecoveryCodes: vi.fn(),
  setup2FA: vi.fn(), verifySetup2FA: vi.fn(), disable2FA: vi.fn(),
}));

const summary = { email: "admin@example.test", twoFactorEnabled: true, recoveryCodesRemaining: 8, previousLogin: { at: 1_720_000_000, ipAddress: "10.0.0.2", deviceLabel: "Chrome on Windows" } };

function renderPage() {
  return render(<MemoryRouter initialEntries={[{ pathname: "/account/security", state: { returnTo: "/profiles" } }]}><Routes><Route path="/account/security" element={<AccountSecurityPage />} /><Route path="/profiles" element={<p>Profiles</p>} /></Routes></MemoryRouter>);
}

describe("AccountSecurityPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(auth.getSecuritySummary).mockResolvedValue(summary);
    vi.mocked(auth.getAuthSessions).mockResolvedValue([{ id: "current", current: true, createdAt: 1_720_000_000, lastSeenAt: 1_720_000_100, expiresAt: 1_725_000_000, ipAddress: "10.0.0.2", deviceLabel: "Chrome on Windows" }]);
    vi.mocked(auth.getSecurityEvents).mockResolvedValue({ events: [{ id: "event", type: "login_success", outcome: "success", createdAt: 1_720_000_000, ipAddress: "10.0.0.2", deviceLabel: "Chrome on Windows" }], nextCursor: null });
  });

  it("requires server-side reauthentication before loading sensitive details", async () => {
    vi.mocked(auth.getReauthenticationStatus).mockResolvedValue({ reauthenticated: false, remainingSeconds: 0 });
    vi.mocked(auth.beginReauthentication).mockResolvedValue({ requires2fa: true, challengeToken: "challenge", expiresInSeconds: 300, email: "admin@example.test", message: "TOTP required" });
    vi.mocked(auth.verifyReauthentication).mockResolvedValue({ reauthenticated: true, validForSeconds: 600 });
    renderPage();
    expect(await screen.findByRole("heading", { name: "Confirm your identity" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByLabelText("Authenticator code")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify factor" }));
    expect(await screen.findByText("Active sessions")).toBeTruthy();
    expect(auth.verifyReauthentication).toHaveBeenCalledWith({ challengeToken: "challenge", method: "totp", code: "123456" });
  });

  it("shows sessions, previous login, recovery count, and audit activity after fresh reauthentication", async () => {
    vi.mocked(auth.getReauthenticationStatus).mockResolvedValue({ reauthenticated: true, remainingSeconds: 500 });
    renderPage();
    expect(await screen.findByText("8 codes remaining")).toBeTruthy();
    expect(screen.getAllByText("Chrome on Windows").length).toBeGreaterThan(0);
    expect(screen.getByText("Login Success")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(screen.getByText("Profiles")).toBeTruthy());
  });
});
