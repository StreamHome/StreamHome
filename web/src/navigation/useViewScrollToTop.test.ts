import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppView } from "./queryState";
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
});
