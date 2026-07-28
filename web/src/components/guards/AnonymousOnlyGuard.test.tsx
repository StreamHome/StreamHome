import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "../../stores/authStore";
import { AnonymousOnlyGuard } from "./AnonymousOnlyGuard";

function renderGuard() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<AnonymousOnlyGuard><p>Login page</p></AnonymousOnlyGuard>} />
        <Route path="/profiles" element={<p>Profile selection</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AnonymousOnlyGuard", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, email: null, isAuthenticated: false, isHydrated: false });
  });

  it("waits for server-backed session hydration before showing the login page", () => {
    renderGuard();

    expect(screen.getByLabelText("Loading authentication")).toBeTruthy();
    expect(screen.queryByText("Login page")).toBeNull();
    expect(screen.queryByText("Profile selection")).toBeNull();
  });

  it("shows the login page after hydration confirms there is no session", () => {
    useAuthStore.setState({ isAuthenticated: false, isHydrated: true });
    renderGuard();

    expect(screen.getByText("Login page")).toBeTruthy();
    expect(screen.queryByText("Profile selection")).toBeNull();
  });

  it("redirects an authenticated session to profile selection", () => {
    useAuthStore.setState({ email: "admin@example.test", isAuthenticated: true, isHydrated: true });
    renderGuard();

    expect(screen.getByText("Profile selection")).toBeTruthy();
    expect(screen.queryByText("Login page")).toBeNull();
  });
});
