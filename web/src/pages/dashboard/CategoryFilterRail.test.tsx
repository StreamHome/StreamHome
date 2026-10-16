import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MotionProvider } from "../../motion/motionSystem";
import { CategoryFilterRail } from "./CategoryFilterRail";

const matchMedia = () => ({ matches: false, media: "(prefers-reduced-motion: reduce)", onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true });

describe("category filter rail", () => {
  it("scrolls only its own track for automatic landing and retains the legacy default", () => {
    const intoView = vi.fn(); const trackScroll = vi.fn();
    const previousIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    const previousScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: intoView });
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: trackScroll });
    Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });
    const options = [{ value: "recommended", label: "Recommended", kind: "virtual" as const, count: 3 }, { value: "Action", label: "Action", kind: "genre" as const, count: 2 }];
    try {
      const { rerender } = render(<MotionProvider><CategoryFilterRail preservePageScroll variant="shared" active="recommended" options={options} onSelect={() => undefined} /></MotionProvider>);
      expect(trackScroll).toHaveBeenCalledTimes(1); expect(intoView).not.toHaveBeenCalled();
      rerender(<MotionProvider><CategoryFilterRail preservePageScroll variant="shared" active="Action" options={options} onSelect={() => undefined} /></MotionProvider>);
      expect(trackScroll).toHaveBeenCalledTimes(2); expect(intoView).not.toHaveBeenCalled();
      rerender(<MotionProvider><CategoryFilterRail variant="shared" active="Action" options={options} onSelect={() => undefined} /></MotionProvider>);
      expect(intoView).toHaveBeenCalledTimes(1);
    } finally {
      if (previousIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", previousIntoView); else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
      if (previousScroll) Object.defineProperty(HTMLElement.prototype, "scrollTo", previousScroll); else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollTo;
    }
  });
  it("exposes one pressed category and reports semantic selections", () => {
    Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });
    const onSelect = vi.fn();
    render(<MotionProvider><CategoryFilterRail variant="shared" active="all" onSelect={onSelect} options={[
      { value: "recommended", label: "Recommended", kind: "virtual", count: 3 },
      { value: "all", label: "All Releases", kind: "virtual", count: 2 },
      { value: "Action", label: "Action", kind: "genre", count: 1 },
    ]} /></MotionProvider>);
    expect(screen.getByRole("navigation", { name: "Catalog categories" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "All Releases", pressed: true })).toBeTruthy();
    expect(screen.getAllByRole("button", { pressed: false })).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Action" }));
    expect(onSelect).toHaveBeenCalledWith("Action");
  });
});
