import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminSection, AppView } from "./queryState";
import { useViewScrollToTop } from "./useViewScrollToTop";

describe("view scroll restoration", () => {
  const scrollTo = vi.fn();

  beforeEach(() => {
    scrollTo.mockReset();
    Object.defineProperty(window, "scrollTo", { configurable: true, value: scrollTo });
    const root = document.createElement("div");
    root.id = "root";
    root.scrollTop = 120;
    root.scrollLeft = 30;
    document.body.appendChild(root);
  });

  afterEach(() => { document.getElementById("root")?.remove(); });

  it("resets only the active catalog panel on a view change and preserves same-view and rail positions", () => {
    const root = document.getElementById("root")!;
    root.innerHTML = '<div class="catalog-layout" data-catalog-present="false"><main data-catalog-scroll></main></div><div class="catalog-layout" data-catalog-present="true"><main data-catalog-scroll><div class="rail"></div></main></div>';
    const active = root.querySelector<HTMLElement>('[data-catalog-present="true"] > main')!;
    const retiring = root.querySelector<HTMLElement>('[data-catalog-present="false"] > main')!;
    const rail = root.querySelector<HTMLElement>(".rail")!;
    active.scrollTop = 540;
    retiring.scrollTop = 850;
    rail.scrollLeft = 320;
    const { rerender } = renderHook(({ view }: { view: AppView }) => useViewScrollToTop(view), { initialProps: { view: "home" as AppView } });
    expect(active.scrollTop).toBe(0);
    expect(retiring.scrollTop).toBe(850);
    expect(rail.scrollLeft).toBe(320);
    active.scrollTop = 700;
    rerender({ view: "home" });
    expect(active.scrollTop).toBe(700);
    rerender({ view: "movies" });
    expect(active.scrollTop).toBe(0);
    expect(retiring.scrollTop).toBe(850);
  });

  it("scrolls for a changed view but not a rerender of the same view", () => {
    const { rerender } = renderHook(({ view }: { view: AppView }) => useViewScrollToTop(view), { initialProps: { view: "home" as AppView } });
    expect(document.getElementById("root")?.scrollTop).toBe(0);
    expect(document.getElementById("root")?.scrollLeft).toBe(0);
    expect(scrollTo).toHaveBeenCalledTimes(1);
    rerender({ view: "home" });
    expect(scrollTo).toHaveBeenCalledTimes(1);
    rerender({ view: "movies" });
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 0, left: 0, behavior: "auto" });
    expect(scrollTo).toHaveBeenCalledTimes(2);
  });

  it("resets both scroll containers for admin section changes, while retaining same-section scroll", () => {
    const { rerender } = renderHook(({ section }: { section: AdminSection }) => useViewScrollToTop(section), { initialProps: { section: "updates" as AdminSection } });
    const root = document.getElementById("root")!;
    root.scrollTop = 670;
    root.scrollLeft = 40;
    rerender({ section: "updates" });
    expect(root.scrollTop).toBe(670);
    expect(root.scrollLeft).toBe(40);
    expect(scrollTo).toHaveBeenCalledTimes(1);
    rerender({ section: "recommendations" });
    expect(root.scrollTop).toBe(0);
    expect(root.scrollLeft).toBe(0);
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 0, left: 0, behavior: "auto" });
    expect(scrollTo).toHaveBeenCalledTimes(2);
    rerender({ section: "updates" });
    expect(scrollTo).toHaveBeenCalledTimes(3);
  });
});
