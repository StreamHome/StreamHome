import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getHealth, login, verify2FA } from "../api/auth";
import { useAuthStore } from "../stores/authStore";
import { LoginPage } from "./LoginPage";
import { ApiError } from "../api/client";

vi.mock("../api/auth", () => ({ getHealth: vi.fn(), login: vi.fn(), verify2FA: vi.fn() }));

function renderLogin() {
  return render(<MemoryRouter initialEntries={["/login"]}><Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route path="/profiles" element={<p>Profile gallery</p>} />
  </Routes></MemoryRouter>);
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useAuthStore.setState({ token: null, email: null, isAuthenticated: false, isHydrated: true });
    vi.mocked(getHealth).mockResolvedValue({ status: "ready", version: "1.0.0", buildId: "test-build", serverTime: Date.now() / 1000 });
  });

  it("submits local credentials, supports password visibility, and completes login", async () => {
    vi.mocked(login).mockResolvedValue({ email: "admin@example.test" });
    renderLogin();

    const password = screen.getByLabelText("Password");
    expect(password.getAttribute("type")).toBe("password");
    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(password.getAttribute("type")).toBe("text");
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(password, { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(login).toHaveBeenCalledWith({ email: "admin@example.test", password: "secret" }, expect.any(AbortSignal)));
    expect(await screen.findByText("Profile gallery", {}, { timeout: 1500 })).toBeTruthy();
    expect(useAuthStore.getState()).toMatchObject({ token: null, email: "admin@example.test", isAuthenticated: true });
    expect(localStorage.getItem("streamhome_token")).toBeNull();
  });

  it("does not claim readiness while the backend database is busy", async () => {
    vi.mocked(getHealth).mockRejectedValue(new ApiError("The server database is busy.", 503, "server_busy"));
    renderLogin();

    expect(await screen.findByRole("button", { name: "Server busy. Check again." })).toBeTruthy();
    expect(screen.queryByText("Server ready")).toBeNull();
  });

  it("moves into TOTP verification and distributes a pasted six-digit code", async () => {
    vi.mocked(login).mockResolvedValue({ requires2fa: true, email: "admin@example.test", challengeToken: "challenge", expiresInSeconds: 300, message: "TOTP required" });
    vi.mocked(verify2FA).mockResolvedValue({ email: "admin@example.test" });
    renderLogin();

    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Verify your identity" })).toBeTruthy();

    fireEvent.change(await screen.findByLabelText("Six-digit authenticator code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify authenticator" }));
    await waitFor(() => expect(verify2FA).toHaveBeenCalledWith({ challengeToken: "challenge", method: "totp", code: "123456" }, expect.any(AbortSignal)));
  });

  it.each(["totp", "recovery"])("returns retry focus after a rejected %s code has been re-enabled", async (method) => {
    vi.mocked(login).mockResolvedValue({ requires2fa: true, email: "admin@example.test", challengeToken: "challenge", expiresInSeconds: 300 });
    let rejectAttempt!: (error: Error) => void;
    vi.mocked(verify2FA).mockReturnValue(new Promise((_, reject) => { rejectAttempt = reject; }));
    renderLogin();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByLabelText("Six-digit authenticator code");
    if (method === "recovery") fireEvent.click(screen.getByRole("button", { name: "Use a recovery code" }));
    const field = screen.getByLabelText(method === "totp" ? "Six-digit authenticator code" : "Recovery code") as HTMLInputElement;
    fireEvent.change(field, { target: { value: method === "totp" ? "000000" : "ABCD-EFGH-IJKL-MNOP" } });
    const submit = screen.getByRole("button", { name: method === "totp" ? "Verify authenticator" : "Verify recovery code" });
    submit.focus();
    fireEvent.click(submit);
    expect(field.disabled).toBe(true);
    rejectAttempt(new ApiError("That code is not valid. Try the latest code.", 401, "invalid_code"));
    await screen.findByRole("alert");
    await waitFor(() => expect(document.activeElement).toBe(field));
    expect(field.disabled).toBe(false);
    expect(field.value).toBe("");
    fireEvent.change(field, { target: { value: "123456" } });
    expect(field.value).toBe("123456");
  });

  it("announces authentication failures and allows returning from TOTP", async () => {
    vi.mocked(login).mockRejectedValueOnce(new Error("Invalid credentials"));
    renderLogin();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Invalid credentials");

    vi.mocked(login).mockResolvedValueOnce({ requires2fa: true, email: "admin@example.test", challengeToken: "challenge", expiresInSeconds: 300, message: "TOTP required" });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Verify your identity" })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /Back to sign in/ }));
    expect(await screen.findByRole("heading", { name: "Sign in to StreamHome" })).toBeTruthy();
  });

  it("remembers only an opted-in email and reports Caps Lock", async () => {
    vi.mocked(login).mockResolvedValue({ email: "admin@example.test" });
    renderLogin();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    const password = screen.getByLabelText("Password");
    fireEvent.change(password, { target: { value: "secret" } });
    const capsEvent = new KeyboardEvent("keyup", { key: "A", bubbles: true });
    Object.defineProperty(capsEvent, "getModifierState", { value: () => true });
    fireEvent(password, capsEvent);
    expect(screen.getByText("Caps Lock is on")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Remember email on this device"));
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(localStorage.getItem("streamhome_remembered_email")).toBe("admin@example.test"));
  });

  it("supports recovery-code mode and displays structured lockout timing", async () => {
    vi.mocked(login).mockResolvedValueOnce({ requires2fa: true, email: "admin@example.test", challengeToken: "challenge", expiresInSeconds: 300, message: "TOTP required" });
    vi.mocked(verify2FA).mockRejectedValueOnce(new ApiError("Too many failed attempts.", 429, "account_locked", 90));
    renderLogin();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(await screen.findByRole("button", { name: "Use a recovery code" }));
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "ABCD-EFGH-IJKL-MNOP" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify recovery code" }));
    expect((await screen.findByRole("alert")).textContent).toContain("1:30");
  });
  it("requires a fresh password after a factor challenge expires", async () => {
    vi.mocked(login).mockResolvedValueOnce({ requires2fa: true, email: "admin@example.test", challengeToken: "expired-challenge", expiresInSeconds: 300 });
    vi.mocked(verify2FA).mockRejectedValueOnce(new ApiError("Verification expired. Enter your password again.", 401, "challenge_expired"));
    renderLogin();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.change(await screen.findByLabelText("Six-digit authenticator code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify authenticator" }));
    const password = await screen.findByLabelText("Password");
    expect((password as HTMLInputElement).value).toBe("");
    expect(password.getAttribute("type")).toBe("password");
    await waitFor(() => expect(screen.queryByLabelText("Six-digit authenticator code")).toBeNull());
    expect((await screen.findByRole("alert")).textContent).toContain("expired");
    expect(screen.getByRole("button", { name: "Continue" }).hasAttribute("disabled")).toBe(true);
  });

});
