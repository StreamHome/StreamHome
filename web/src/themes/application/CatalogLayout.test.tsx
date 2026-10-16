import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { AnimatePresence, motion } from "framer-motion";
import { describe, expect, it, vi } from "vitest";
import { CatalogLayout } from "./CatalogLayout";
import { THEME_DEFINITIONS } from "./themeRegistry";
import type { ThemeNavigationProps } from "./contracts";

const navigationProps: ThemeNavigationProps = {
  profile: { id: "1", name: "Admin", avatarColor: "#ff5f1f", theme: "ember", pinEnabled: false },
  activeView: "home",
  isAdmin: true,
  onView: () => undefined,
  onSearch: () => undefined,
  onEditProfile: () => undefined,
  onProfiles: () => undefined,
  onAdmin: () => undefined,
  onLogout: () => undefined,
};

describe("catalog layout dock", () => {
  it.each(["ember", "aurora", "cinema"] as const)("owns %s navigation after the content and preserves its mounted controls", (theme) => {
    const onView = vi.fn();
    const frame = (activeView: ThemeNavigationProps["activeView"]) => <CatalogLayout presentation={THEME_DEFINITIONS[theme]} navigationProps={{ ...navigationProps, activeView, onView }}><h1>Catalog</h1></CatalogLayout>;
    const { container, rerender, unmount } = render(frame("home"));
    const main = screen.getByRole("main", { name: "Catalog content" });
    const navigation = screen.getByRole("navigation", { name: "Catalog mobile" });
    expect(container.contains(navigation)).toBe(true);
    expect(main.nextElementSibling).toBe(navigation.parentElement);
    expect(document.querySelector(".mobile-navigation-layer")).toBeNull();
    const movies = navigation.querySelector<HTMLButtonElement>('button[aria-label="Movies"]')!;
    expect(movies.textContent).toBe("Movies");
    expect(movies.querySelector(".catalog-dock-icon")).not.toBeNull();
    movies.focus();
    fireEvent.click(movies);
    expect(onView).toHaveBeenCalledWith("movies");
    rerender(frame("movies"));
    expect(screen.getByRole("navigation", { name: "Catalog mobile" })).toBe(navigation);
    expect(document.activeElement).toBe(movies);
    expect(movies.getAttribute("aria-current")).toBe("page");
    expect(navigation.querySelector('button[aria-label="Home"]')?.hasAttribute("aria-current")).toBe(false);
    unmount();
    expect(navigation.isConnected).toBe(false);
  });

  it("keeps all six ordinary-profile destinations and their visible labels", () => {
    render(<CatalogLayout presentation={THEME_DEFINITIONS.ember} navigationProps={{ ...navigationProps, isAdmin: false }}><h1>Catalog</h1></CatalogLayout>);
    const dock = screen.getByRole("navigation", { name: "Catalog mobile" });
    const buttons = Array.from(dock.querySelectorAll("button"));
    expect(buttons.map((button) => button.textContent)).toEqual(["Home", "Movies", "Series", "My List", "Downloads", "Search"]);
    expect(dock.querySelector('button[aria-label="Admin"]')).toBeNull();
  });

  it("retires dock interaction and root-lock ownership when the catalog starts exiting", () => {
    const page = (open: boolean) => <AnimatePresence>{open && <motion.div key="catalog" exit={{ opacity: 0 }} transition={{ duration: 1 }}><CatalogLayout presentation={THEME_DEFINITIONS.ember} navigationProps={navigationProps}><h1>Catalog</h1></CatalogLayout></motion.div>}</AnimatePresence>;
    const { container, rerender } = render(page(true));
    expect(container.querySelector('[data-catalog-present="true"]')).not.toBeNull();
    expect(screen.getByRole("navigation", { name: "Catalog mobile" })).not.toBeNull();
    rerender(page(false));
    expect(container.querySelector(".catalog-layout")).not.toBeNull();
    expect(container.querySelector('[data-catalog-present="true"]')).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Catalog mobile" })).toBeNull();
  });

  it("lets Cinema observe the mobile content panel and return to desktop document scrolling", () => {
    Object.defineProperty(window, "scrollY", { configurable: true, value: 0 });
    const { container } = render(<CatalogLayout presentation={THEME_DEFINITIONS.cinema} navigationProps={navigationProps}><h1>Catalog</h1></CatalogLayout>);
    const layout = container.querySelector<HTMLElement>(".catalog-layout")!;
    const main = screen.getByRole("main", { name: "Catalog content" });
    const header = container.querySelector(".theme-nav--cinema")!;
    layout.style.display = "grid";
    main.scrollTop = 80;
    fireEvent.scroll(main);
    expect(header.getAttribute("data-scrolled")).toBe("true");
    main.scrollTop = 0;
    fireEvent.scroll(main);
    expect(header.getAttribute("data-scrolled")).toBe("false");
    layout.style.display = "block";
    Object.defineProperty(window, "scrollY", { configurable: true, value: 80 });
    fireEvent.resize(window);
    expect(header.getAttribute("data-scrolled")).toBe("true");
    Object.defineProperty(window, "scrollY", { configurable: true, value: 0 });
  });
});
