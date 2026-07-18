import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MotionProvider } from "../../motion/motionSystem";
import { CategoryFilterRail } from "./CategoryFilterRail";

const matchMedia = () => ({ matches: false, media: "(prefers-reduced-motion: reduce)", onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true });

describe("category filter rail", () => {
  it("exposes one pressed category and reports semantic selections", () => {
    Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });
    const onSelect = vi.fn();
    render(<MotionProvider><CategoryFilterRail variant="shared" active="all" onSelect={onSelect} options={[
      { value: "recommended", label: "Recommended", kind: "virtual" },
      { value: "all", label: "All Releases", kind: "virtual" },
      { value: "Action", label: "Action", kind: "genre" },
    ]} /></MotionProvider>);
    expect(screen.getByRole("navigation", { name: "Catalog categories" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "All Releases", pressed: true })).toBeTruthy();
    expect(screen.getAllByRole("button", { pressed: false })).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Action" }));
    expect(onSelect).toHaveBeenCalledWith("Action");
  });
});
