import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { login, verify2FA } from "../api/auth";
import { useAuthStore } from "../stores/authStore";
import { LoginPage } from "./LoginPage";

vi.mock("../api/auth", () => ({ login: vi.fn(), verify2FA: vi.fn() }));

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
  });

  it("submits local credentials, supports password visibility, and completes login", async () => {
    vi.mocked(login).mockResolvedValue({ accessToken: "token", tokenType: "bearer", email: "admin@example.test" });
    renderLogin();

    const password = screen.getByLabelText("Password");
    expect(password.getAttribute("type")).toBe("password");
    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(password.getAttribute("type")).toBe("text");
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(password, { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(login).toHaveBeenCalledWith({ email: "admin@example.test", password: "secret" }));
    expect(await screen.findByText("Profile gallery", {}, { timeout: 1500 })).toBeTruthy();
    expect(useAuthStore.getState().token).toBe("token");
  });

  it("moves into TOTP verification and distributes a pasted six-digit code", async () => {
    vi.mocked(login).mockResolvedValue({ requires2fa: true, email: "admin@example.test", message: "TOTP required" });
    vi.mocked(verify2FA).mockResolvedValue({ accessToken: "verified", tokenType: "bearer", email: "admin@example.test" });
    renderLogin();

    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Verify your identity" })).toBeTruthy();

    fireEvent.paste(await screen.findByLabelText("Six-digit TOTP code"), { clipboardData: { getData: () => "12 34-56" } });
    await waitFor(() => expect(verify2FA).toHaveBeenCalledWith({ email: "admin@example.test", code: "123456" }));
    expect(screen.getByText("6/6 digits")).toBeTruthy();
  });

  it("announces authentication failures and allows returning from TOTP", async () => {
    vi.mocked(login).mockRejectedValueOnce(new Error("Invalid credentials"));
    renderLogin();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Invalid credentials");

    vi.mocked(login).mockResolvedValueOnce({ requires2fa: true, email: "admin@example.test", message: "TOTP required" });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Verify your identity" })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Back to sign in" }));
    expect(await screen.findByRole("heading", { name: "Sign in to StreamHome" })).toBeTruthy();
  });
});
