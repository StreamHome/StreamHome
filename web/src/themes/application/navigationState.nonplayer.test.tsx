import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MotionProvider } from "../../motion/motionSystem";
import { getThemeDefinition } from "./themeRegistry";
afterEach(cleanup);
describe("included desktop navigation state", () => {
  it.each(["ember", "aurora", "cinema"] as const)("exposes and updates the active %s route", (theme) => {
    const Navigation = getThemeDefinition(theme).Navigation;
    const onView = vi.fn();
    const props = { activeView: "movies" as const, query: "", profile: { id: "navigation-fixture", name: "Fixture", theme, pinEnabled: false, avatarColor: "#8b5cf6" }, isAdmin: false, onView, onSearch: vi.fn(), onAdmin: vi.fn(), onProfiles: vi.fn(), onEditProfile: vi.fn(), onLogout: vi.fn() };
    const { rerender } = render(<MotionProvider><Navigation {...props} /></MotionProvider>);
    const nav = screen.getByRole("navigation", { name: "Catalog" });
    const current = within(nav).getAllByRole("button").filter((button) => button.getAttribute("aria-current") === "page");
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toMatch(/Movies/);
    fireEvent.click(within(nav).getByRole("button", { name: "Series" }));
    expect(onView).toHaveBeenCalledWith("series");
    rerender(<MotionProvider><Navigation {...props} activeView="series" /></MotionProvider>);
    expect(within(nav).getByRole("button", { name: "Series" }).getAttribute("aria-current")).toBe("page");
    expect(within(nav).getByRole("button", { name: "Movies" }).hasAttribute("aria-current")).toBe(false);
  });
});
