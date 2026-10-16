import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useAuthStore } from "../../stores/authStore";
import { AuthGuard } from "./AuthGuard";
import { AnonymousOnlyGuard } from "./AnonymousOnlyGuard";
afterEach(cleanup);
beforeEach(() => useAuthStore.setState({ isHydrated: false, isAuthenticated: false, hydrationStatus: "pending", hydrationError: null }));
describe("visible session recovery", () => {
  it.each([AuthGuard, AnonymousOnlyGuard])("shows named pending and unavailable states before route content", (Guard) => {
    const { rerender } = render(<MemoryRouter><Guard><p>Route content</p></Guard></MemoryRouter>);
    expect(screen.getByRole("status").textContent).toMatch(/Checking/);
    expect(screen.queryByText("Route content")).toBeNull();
    const retry = vi.fn(async () => undefined);
    act(() => useAuthStore.setState({ hydrationStatus: "error", hydrationError: "The server took too long.", hydrate: retry }));
    rerender(<MemoryRouter><Guard><p>Route content</p></Guard></MemoryRouter>);
    expect(screen.getByRole("alert").textContent).toMatch(/too long/);
    expect(screen.queryByText("Route content")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry session check" }));
    expect(retry).toHaveBeenCalledWith({ force: true });
  });
});
