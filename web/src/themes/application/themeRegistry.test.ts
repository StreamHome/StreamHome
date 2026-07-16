import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { THEME_DEFINITIONS } from "./themeRegistry";

const navigationProps = {
  profile: { id: "1", name: "Admin", avatarColor: "#ff5f1f", theme: "gemini", pinEnabled: false, pin: null },
  activeView: "home" as const,
  isAdmin: true,
  onView: () => undefined,
  onSearch: () => undefined,
  onProfiles: () => undefined,
  onAdmin: () => undefined,
  onLogout: () => undefined,
};

describe("theme definition registry", () => {
  it("keeps every theme on a distinct presentation contract", () => {
    const definitions = Object.values(THEME_DEFINITIONS);
    expect(new Set(definitions.map((theme) => theme.Navigation)).size).toBe(4);
    expect(new Set(definitions.map((theme) => theme.heroVariant)).size).toBe(4);
    expect(new Set(definitions.map((theme) => theme.cardVariant)).size).toBe(4);
    expect(new Set(definitions.map((theme) => theme.detailsVariant)).size).toBe(4);
    expect(new Set(definitions.map((theme) => theme.playerVariant)).size).toBe(4);
    expect(THEME_DEFINITIONS.ember.Application).not.toBe(THEME_DEFINITIONS.cinema.Application);
    expect(THEME_DEFINITIONS.aurora.Application).toBe(THEME_DEFINITIONS.cinema.Application);
    expect(THEME_DEFINITIONS.cinema.Application).toBe(THEME_DEFINITIONS.gemini.Application);
    expect(definitions.every((theme) => theme.motion.view && theme.motion.billboard && theme.motion.cardHover.scale > 1)).toBe(true);
  });

  it("opens the desktop Gemini profile menu upward from the bottom sidebar", () => {
    render(React.createElement(THEME_DEFINITIONS.gemini.Navigation, navigationProps));
    fireEvent.click(screen.getAllByLabelText("Open settings for Admin")[0]);
    expect(screen.getByRole("menu").getAttribute("data-placement")).toBe("top-start");
  });

  it("opens the Gemini mobile profile menu downward from the top header", () => {
    render(React.createElement(THEME_DEFINITIONS.gemini.Navigation, navigationProps));
    fireEvent.click(screen.getAllByLabelText("Open settings for Admin")[1]);
    expect(screen.getByRole("menu").getAttribute("data-placement")).toBe("bottom-end");
  });

  it("omits the profile-menu caret decoration from every theme", () => {
    for (const definition of Object.values(THEME_DEFINITIONS)) {
      const { container, unmount } = render(React.createElement(definition.Navigation, navigationProps));
      expect(container.querySelectorAll(".theme-profile-control > i")).toHaveLength(0);
      unmount();
    }
  });
});
